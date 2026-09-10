"""TMDB-CSV file import (PLAN.md §3.5, D9) and export (D10).

The canonical film-list shape is TMDB's own ten-column export format: the
same header ReelTalk's export emits and its import accepts, so exports
round-trip as a no-op (D10). Import is synchronous and single-transaction —
no TMDB API calls during the import itself (D9): rows become ID stubs that
the D11 backfill (``reeltalk.core.catalog.backfill_films``) fills in after
the transaction commits. The views live in ``reeltalk.core.views``; these
functions are plain and directly testable.
"""

from __future__ import annotations

import csv
import io
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction

from reeltalk.core.models import (
    Film,
    Shelf,
    ShelfFilm,
    Status,
    shelve_to_watchlist,
)
from reeltalk.core.tasks import enqueue_backfill
from reeltalk.core.tmdb import is_configured

# The canonical film-list CSV shape: TMDB's own export format (D9). Column
# order in an incoming file is not significant — membership is what counts.
TMDB_CSV_HEADER = [
    "TMDb ID",
    "IMDb ID",
    "Type",
    "Name",
    "Release Date",
    "Season Number",
    "Episode Number",
    "Rating",
    "Your Rating",
    "Date Rated",
]

# The import runs in one transaction and renders every row into the results
# table, so bound the file size well past any realistic library (D9).
MAX_IMPORT_ROWS = 20_000


class TmdbCsvError(ValueError):
    """A user-facing import rejection: an unrecognized header or a file over
    the row cap."""


def parse_tmdb_csv(text: str) -> list[dict]:
    """Parse a TMDB-style CSV into row dicts, validating up front (D9).

    The canonical ten-column header must be present — missing columns are
    rejected with a message naming them. Raises :class:`TmdbCsvError` for an
    unrecognized header or a file over the row cap.
    """
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    missing = [column for column in TMDB_CSV_HEADER if column not in header]
    if missing:
        raise TmdbCsvError(
            "Not a recognized TMDB export — missing column(s): " + ", ".join(missing)
        )
    rows = list(reader)
    if len(rows) > MAX_IMPORT_ROWS:
        raise TmdbCsvError(
            f"That file has {len(rows):,} rows; the import is limited to "
            f"{MAX_IMPORT_ROWS:,}. Split it into smaller files."
        )
    return rows


def parse_release_year(release_date: str | None) -> int | None:
    """A TMDB release date ("1976-05-20T00:00:00Z") down to its year."""
    if not release_date or len(release_date) < 4:
        return None
    try:
        return int(release_date[:4])
    except ValueError:
        return None


def parse_tmdb_rating(raw: str | None) -> Decimal | None:
    """A TMDB "Your Rating" (1–10 scale) onto the 0.5–5 star scale (D9).

    The value is rounded to the nearest whole TMDB point and halved — a 7
    becomes 3.5 stars — keeping half-star steps. Empty, non-numeric, or
    out-of-range values read as unrated.
    """
    if raw is None or not str(raw).strip():
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    if not (Decimal("1") <= value <= Decimal("10")):
        return None
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2


def _parse_tmdb_id(raw: str | None) -> int | None:
    """The row's TMDb ID as an int, or None when absent or not a number."""
    raw = (raw or "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def find_or_create_film_stub(row: dict) -> tuple[Film, bool]:
    """The local Film for a CSV row, created from the row's data only (D9).

    No TMDB API calls during import: matching follows D7's order (tmdb_id →
    imdb_id → normalized title+year); a miss creates an ID stub that the D11
    backfill fills in later. A match backfills empty identifiers/year from
    the row instead of creating a duplicate. Returns ``(film, created)``.
    """
    name = (row.get("Name") or "").strip()
    tmdb_id = _parse_tmdb_id(row.get("TMDb ID"))
    imdb_id = (row.get("IMDb ID") or "").strip()
    year = parse_release_year(row.get("Release Date"))

    film = Film.find_match(
        tmdb_id=tmdb_id, imdb_id=imdb_id or None, title=name or None, year=year
    )
    if film is None:
        return (
            Film.objects.create(
                title=name, year=year, tmdb_id=tmdb_id, imdb_id=imdb_id or None
            ),
            True,
        )

    # Matched an existing film: backfill empty identifier fields from the row.
    changed = False
    if tmdb_id and not film.tmdb_id:
        film.tmdb_id = tmdb_id
        changed = True
    if imdb_id and not film.imdb_id:
        film.imdb_id = imdb_id
        changed = True
    if year is not None and not film.year:
        film.year = year
        changed = True
    if changed:
        film.save()
    return film, False


def import_row(
    user, row: dict, watchlist: Shelf, watched: Shelf
) -> tuple[str, str, Film | None]:
    """Process one CSV row; returns ``(status, note, film)`` for the results.

    Non-movie rows and missing names are skipped with a note (D9). An unrated
    row goes to the Watchlist — D1's mutual exclusion is enforced by
    ``shelve_to_watchlist``, which refuses a watched film. A rated row lands
    on Watched with a rating-only entry; an existing review is never touched
    (the user's live D5 review wins).
    """
    name = (row.get("Name") or "").strip()
    film_type = (row.get("Type") or "movie").strip().lower()

    if film_type and film_type != "movie":
        return "skipped", f"not a movie ({film_type})", None
    if not name:
        return "skipped", "missing name", None

    rating = parse_tmdb_rating(row.get("Your Rating"))
    film, created = find_or_create_film_stub(row)
    status = "created" if created else "matched"

    if rating is None:
        notes = {
            "added": "added to Watchlist",
            "already": "already on your Watchlist",
            "watched": "already in your Watched list",
        }
        return status, notes[shelve_to_watchlist(user, film)], film

    if Status.objects.filter(
        user=user, film=film, status_type__in=list(Status.REVIEW_TYPES), deleted=False
    ).exists():
        return status, "you already have a review; rating not imported", film

    ShelfFilm.objects.get_or_create(shelf=watched, film=film, defaults={"user": user})
    # D1: Watchlist and Watched are mutually exclusive.
    ShelfFilm.objects.filter(shelf=watchlist, film=film).delete()
    # A bulk import is data migration, not sharing — when federation lands
    # (M4) these entries must not broadcast.
    Status.objects.create(
        user=user, film=film, status_type=Status.Type.REVIEW_RATING, rating=rating
    )
    return status, f"Watched — {rating} stars", film


@transaction.atomic
def import_film_csv(user, rows: list[dict]) -> dict:
    """D9: import parsed TMDB-style CSV rows in one transaction.

    Every row is processed and rendered into a per-row result (line / name /
    status / note) plus summary counts. Imported films are ID stubs — the D11
    backfill for them (created *and* matched, so a re-import also heals stale
    stubs) is queued on commit when a TMDB key is configured. Returns
    ``{"results", "summary", "backfill_queued"}``.
    """
    watchlist = Shelf.objects.get(user=user, identifier=Shelf.TO_READ)
    watched = Shelf.objects.get(user=user, identifier=Shelf.READ)

    results: list[dict] = []
    film_ids: list[int] = []
    for line_number, row in enumerate(rows, start=2):
        status, note, film = import_row(user, row, watchlist, watched)
        results.append(
            {
                "line": line_number,
                "name": (row.get("Name") or "").strip(),
                "status": status,
                "note": note,
            }
        )
        if film is not None:
            film_ids.append(film.id)

    summary = {
        "total": len(results),
        "created": sum(1 for r in results if r["status"] == "created"),
        "matched": sum(1 for r in results if r["status"] == "matched"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
    }
    backfill_queued = bool(film_ids and is_configured())
    if backfill_queued:
        transaction.on_commit(lambda: enqueue_backfill(film_ids))
    return {"results": results, "summary": summary, "backfill_queued": backfill_queued}
