"""Local-catalog operations built on the TMDB client (PLAN.md §3.4).

The pure HTTP client lives in ``reeltalk.core.tmdb`` (R27); this module is
where that client meets the database — turning a TMDB film into a local
:class:`~reeltalk.core.models.Film` row and keeping imported films up to date:

- :func:`create_or_match_film` — D7 find-or-create for a search hit or
  click-through (exact ``tmdb_id`` → normalized title+year fallback that
  backfills a manual film → new row),
- :func:`backfill_films` — the D11 import backfill loop (fetch details +
  poster, fill empty fields, paced, per-film skip-on-error), and
- :func:`search_local` — the no-key / degraded-mode local-library search over
  the trigger-maintained tsvector (§3.2 weights).

The Django-Q2 task wrapper for the backfill lives in ``reeltalk.core.tasks``;
these functions are plain and directly testable.
"""

from __future__ import annotations

import logging
import time

from django.core.files.base import ContentFile
from django.db.models.expressions import RawSQL

from reeltalk.core.models import Film, derive_sort_title
from reeltalk.core.tmdb import (
    TmdbError,
    download_poster,
    film_fields_from_tmdb,
    get_film_details,
    is_configured,
)

logger = logging.getLogger(__name__)

# Seconds between TMDB detail fetches in the backfill — keeps a large import
# well under TMDB's ~50-requests-per-10-seconds limit (D11).
BACKFILL_REQUEST_INTERVAL = 0.25


def _attach_poster(film: Film, details: dict) -> None:
    """Download and store the poster for a film that doesn't have one yet."""
    if film.poster:
        return
    content = download_poster(details.get("poster_path"))
    if not content:
        return
    film.poster.save(f"tmdb-{film.tmdb_id}.jpg", ContentFile(content), save=False)
    film.save()


def _backfill_film_from_tmdb(film: Film, details: dict) -> None:
    """Fill this row's empty metadata from a TMDB detail payload (D7).

    Only empty fields are touched — a matched manual film keeps any metadata
    it already has. The TMDB id is backfilled when absent so the row is linked
    to its catalog entry (and D4 locks it from editing thereafter).
    """
    fields = film_fields_from_tmdb(details)
    changed = False
    if not film.tmdb_id:
        film.tmdb_id = details["id"]
        changed = True
    for name in (
        "year",
        "runtime",
        "raw_description",
        "description",
        "genres",
        "directors",
        "cast",
    ):
        value = fields[name]
        if not getattr(film, name) and value:
            setattr(film, name, value)
            changed = True
    if changed:
        film.save()
    _attach_poster(film, details)


def create_or_match_film(
    tmdb_id: int, *, title: str | None = None, year: int | None = None
) -> Film:
    """D7 find-or-create the local Film for a TMDB film.

    1. A row already carrying this ``tmdb_id`` is returned as-is.
    2. Otherwise the detail payload is fetched and a normalized title+year
       match against an existing (manual) film backfills that row with the
       TMDB id + empty metadata instead of creating a duplicate.
    3. With no match, a new Film is created from the details (+ poster).

    ``title``/``year`` are optional matching hints from the search hit; they
    only matter when the detail payload lacks one of them.
    """
    existing = Film.objects.filter(tmdb_id=tmdb_id).first()
    if existing is not None:
        return existing

    details = get_film_details(tmdb_id)
    fields = film_fields_from_tmdb(details)
    match_title = title or fields["title"]
    match_year = year if year is not None else fields["year"]

    matched = None
    if match_title and match_year is not None:
        matched = Film.objects.filter(
            sort_title=derive_sort_title(match_title), year=match_year
        ).first()

    if matched is not None:
        _backfill_film_from_tmdb(matched, details)
        return matched

    film = Film.objects.create(tmdb_id=tmdb_id, **fields)
    _attach_poster(film, details)
    return film


def backfill_films(film_ids) -> dict:
    """D11: fetch TMDB details + poster for films created by a file import.

    Idempotent and rate-limit-friendly: a film is skipped when it has no
    ``tmdb_id`` or is already complete (poster + description); a failure on one
    film is logged and skipped so the batch always runs to the end. A no-op
    when no API key is configured. Returns per-outcome counts.
    """
    ids = list(film_ids)
    if not is_configured():
        return {"skipped_unconfigured": len(ids)}

    summary = {
        "backfilled": 0,
        "already_complete": 0,
        "no_tmdb_id": 0,
        "failed": 0,
    }
    for film_id in ids:
        film = Film.objects.filter(id=film_id, tmdb_id__isnull=False).first()
        if film is None or not film.tmdb_id:
            summary["no_tmdb_id"] += 1
            continue
        if film.poster and film.description:
            summary["already_complete"] += 1
            continue
        try:
            details = get_film_details(film.tmdb_id)
            _backfill_film_from_tmdb(film, details)
            summary["backfilled"] += 1
        except TmdbError:
            logger.warning("TMDB backfill failed for film %s", film.id)
            summary["failed"] += 1
        time.sleep(BACKFILL_REQUEST_INTERVAL)
    return summary


def search_local(query: str, limit: int = 20) -> list[Film]:
    """Local-library search (D6 fallback): full-text over the tsvector.

    Queries the trigger-maintained ``search_vector`` column (§3.2 weights:
    title > subtitle > directors+cast > genres) via ``plainto_tsquery`` —
    plain user text, parameterized, so no query injection. Best rank first;
    empty/whitespace queries return nothing without hitting the database.
    """
    if not query.strip():
        return []
    ranked = (
        Film.objects.annotate(
            rank=RawSQL(
                "ts_rank(search_vector, plainto_tsquery('simple', %s))", [query]
            )
        )
        .filter(rank__gt=0)
        .order_by("-rank", "sort_title")[:limit]
    )
    return list(ranked)
