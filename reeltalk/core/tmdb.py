"""TMDB v3 client (PLAN.md §3.4).

Server-side only — every call is made by the web process with the operator's
shared API key (D8), never from the browser. The client exposes three things
the rest of M2 builds on:

- ``search_films`` — one page of movie-search results with total counts,
- ``get_film_details`` — a film's full payload (credits + images appended),
- ``download_poster`` / ``film_fields_from_tmdb`` — poster bytes and the
  mapping from a detail payload onto :class:`~reeltalk.core.models.Film` fields.

Failures surface as :class:`TmdbError` subtypes so a view can show a
sensible, user-facing message (bad key vs rate limit vs network) instead of a
stack trace. This module makes no database calls itself; the create-or-match
and backfill logic that acts on films lives in ``reeltalk.core.models`` /
the views and imports from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings

from reeltalk.core.utils import render_markdown

API_BASE = "https://api.themoviedb.org/3"
# w500 is the poster size the film page displays; the CDN serves any width.
IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
REQUEST_TIMEOUT = 10


class TmdbError(Exception):
    """Base class for user-facing TMDB failures."""


class TmdbAuthError(TmdbError):
    """The instance's API key was rejected (HTTP 401)."""


class TmdbRateLimitError(TmdbError):
    """TMDB throttled us (HTTP 429) — retry after a short pause."""


class TmdbNetworkError(TmdbError):
    """No usable response: network failure or an unexpected HTTP status."""


def is_configured() -> bool:
    """Whether the instance has a TMDB API key set (D8)."""
    return bool(settings.TMDB_API_KEY)


def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET a TMDB endpoint and return the parsed JSON payload.

    Raises the matching :class:`TmdbError` subtype on failure so callers can
    distinguish a bad key from a rate limit from a network problem.
    """
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as err:
        raise TmdbNetworkError("Could not reach TMDB; try again shortly.") from err
    if resp.status_code == 401:
        raise TmdbAuthError("The instance's TMDB API key is invalid or missing.")
    if resp.status_code == 429:
        raise TmdbRateLimitError("TMDB rate limit reached; try again in a minute.")
    if not resp.ok:
        raise TmdbNetworkError(f"TMDB request failed (HTTP {resp.status_code}).")
    return resp.json()


@dataclass
class SearchResult:
    """One row from a TMDB movie search."""

    tmdb_id: int
    title: str
    year: int | None
    poster_url: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SearchResult:
        year = None
        if data.get("release_date"):
            year = int(data["release_date"][:4])
        poster_path = data.get("poster_path")
        return cls(
            tmdb_id=int(data["id"]),
            title=data["title"],
            year=year,
            poster_url=f"{IMAGE_BASE}{poster_path}" if poster_path else None,
        )


@dataclass
class FilmSearchResults:
    """One page of TMDB movie search results plus the pagination counts."""

    rows: list[SearchResult]
    page: int
    total_pages: int


def search_films(query: str, page: int = 1) -> FilmSearchResults:
    """Search TMDB for films matching ``query`` (D6 primary catalog)."""
    data = _get(
        f"{API_BASE}/search/movie",
        {
            "api_key": settings.TMDB_API_KEY,
            "query": query,
            "include_adult": "false",
            "page": page,
        },
    )
    return FilmSearchResults(
        rows=[SearchResult.from_api(row) for row in data.get("results", [])],
        page=page,
        total_pages=data.get("total_pages", 0),
    )


def get_film_details(tmdb_id: int) -> dict[str, Any]:
    """Fetch a film's full payload with credits and images appended."""
    return _get(
        f"{API_BASE}/movie/{tmdb_id}",
        {
            "api_key": settings.TMDB_API_KEY,
            "append_to_response": "credits,images",
        },
    )


def download_poster(poster_path: str | None) -> bytes | None:
    """Download a poster from the TMDB image CDN; None when there is no path."""
    if not poster_path:
        return None
    try:
        resp = requests.get(f"{IMAGE_BASE}{poster_path}", timeout=REQUEST_TIMEOUT)
    except requests.RequestException as err:
        raise TmdbNetworkError("Could not download the film poster.") from err
    if not resp.ok:
        raise TmdbNetworkError("Could not download the film poster.")
    return resp.content


def film_fields_from_tmdb(details: dict[str, Any]) -> dict[str, Any]:
    """Map a TMDB movie detail payload onto Film field values (§3.4).

    Returns values ready to store on a :class:`Film` (the caller adds the
    ``tmdb_id``). The overview is plain text: it is kept verbatim in
    ``raw_description`` and rendered to sanitized HTML for ``description`` so
    every description in the system passes through one code path. Cast is
    capped at ten names so we don't store every extra.
    """
    crew = details.get("credits", {}).get("crew", [])
    cast = details.get("credits", {}).get("cast", [])
    overview = details.get("overview") or ""
    year = None
    if details.get("release_date"):
        year = int(details["release_date"][:4])
    return {
        "title": details["title"],
        "year": year,
        "runtime": details.get("runtime") or None,
        "raw_description": overview,
        "description": render_markdown(overview),
        "genres": [g["name"] for g in details.get("genres", [])],
        "directors": [p["name"] for p in crew if p.get("job") == "Director"],
        # Cap the cast list so we don't store every extra.
        "cast": [p["name"] for p in cast[:10]],
    }
