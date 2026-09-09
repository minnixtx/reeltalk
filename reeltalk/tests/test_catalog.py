"""Create-or-match (D7) + import backfill (D11) tests — TMDB HTTP mocked.

Poster attachment is exercised with a real (tiny) JPEG so the ImageField's
Pillow validation passes; the backfill pacing constant is zeroed so tests do
not actually sleep.
"""

from io import BytesIO

import pytest
import responses
from django.core.files.base import ContentFile
from django.test import override_settings
from PIL import Image

from reeltalk.core.catalog import backfill_films, create_or_match_film
from reeltalk.core.models import Film


def _tiny_jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (10, 10), (120, 80, 40)).save(buf, format="JPEG")
    return buf.getvalue()


def _details(tmdb_id=78, **overrides):
    base = {
        "id": tmdb_id,
        "title": "Blade Runner",
        "release_date": "1982-06-25",
        "runtime": 117,
        "overview": "A blade runner must track down four replicas.",
        "poster_path": "/br-poster.jpg",
        "genres": [{"name": "Science Fiction"}],
        "credits": {
            "crew": [{"name": "Ridley Scott", "job": "Director"}],
            "cast": [{"name": "Harrison Ford"}, {"name": "Rutger Hauer"}],
        },
    }
    base.update(overrides)
    return base


def _mock_tmdb(details, poster_bytes=None):
    responses.add(
        responses.GET,
        f"https://api.themoviedb.org/3/movie/{details['id']}",
        json=details,
        status=200,
    )
    if details.get("poster_path"):
        responses.add(
            responses.GET,
            f"https://image.tmdb.org/t/p/w500{details['poster_path']}",
            body=poster_bytes if poster_bytes is not None else _tiny_jpeg(),
            status=200,
        )


# --- create_or_match_film (D7) ---------------------------------------------


@pytest.mark.django_db
@responses.activate
def test_create_or_match_existing_tmdb_id_returns_as_is():
    film = Film.objects.create(title="Blade Runner", year=1982, tmdb_id=78)
    # No mock registered — if the code called TMDB it would fail.
    assert create_or_match_film(78) == film
    assert len(responses.calls) == 0


@pytest.mark.django_db
@responses.activate
def test_create_or_match_backfills_manual_film_by_title_year():
    manual = Film.objects.create(title="Blade Runner", year=1982)  # no tmdb_id
    _mock_tmdb(_details())
    result = create_or_match_film(78, title="Blade Runner", year=1982)
    assert result == manual
    assert Film.objects.count() == 1  # no duplicate created
    result.refresh_from_db()
    assert result.tmdb_id == 78  # backfilled
    assert result.runtime == 117  # empty metadata filled
    assert result.genres == ["Science Fiction"]
    assert result.poster


@pytest.mark.django_db
@responses.activate
def test_create_or_match_keeps_existing_manual_metadata():
    # D7 fills only empty fields — an existing runtime is not overwritten.
    Film.objects.create(title="Blade Runner", year=1982, runtime=99)
    _mock_tmdb(_details(runtime=117))
    result = create_or_match_film(78, title="Blade Runner", year=1982)
    result.refresh_from_db()
    assert result.runtime == 99


@pytest.mark.django_db
@responses.activate
def test_create_or_match_creates_new_film():
    _mock_tmdb(_details())
    film = create_or_match_film(78, title="Blade Runner", year=1982)
    assert Film.objects.count() == 1
    assert film.tmdb_id == 78
    assert film.title == "Blade Runner"
    assert film.year == 1982
    assert film.runtime == 117
    assert film.directors == ["Ridley Scott"]
    assert list(film.cast) == ["Harrison Ford", "Rutger Hauer"]
    assert film.poster


# --- backfill_films (D11) ---------------------------------------------------


@pytest.mark.django_db
@override_settings(TMDB_API_KEY="")
@responses.activate
def test_backfill_noop_without_key(monkeypatch):
    monkeypatch.setattr("reeltalk.core.catalog.BACKFILL_REQUEST_INTERVAL", 0)
    film = Film.objects.create(title="X", tmdb_id=5)
    summary = backfill_films([film.id])
    assert summary == {"skipped_unconfigured": 1}
    assert len(responses.calls) == 0


@pytest.mark.django_db
@override_settings(TMDB_API_KEY="k")
@responses.activate
def test_backfill_skips_without_tmdb_id(monkeypatch):
    monkeypatch.setattr("reeltalk.core.catalog.BACKFILL_REQUEST_INTERVAL", 0)
    film = Film.objects.create(title="Manual")  # no tmdb_id
    summary = backfill_films([film.id])
    assert summary["no_tmdb_id"] == 1
    assert len(responses.calls) == 0


@pytest.mark.django_db
@override_settings(TMDB_API_KEY="k")
@responses.activate
def test_backfill_skips_already_complete(monkeypatch):
    monkeypatch.setattr("reeltalk.core.catalog.BACKFILL_REQUEST_INTERVAL", 0)
    film = Film.objects.create(title="Done", tmdb_id=9, description="<p>text</p>")
    film.poster.save("p.jpg", ContentFile(_tiny_jpeg()), save=True)
    summary = backfill_films([film.id])
    assert summary["already_complete"] == 1
    assert len(responses.calls) == 0


@pytest.mark.django_db
@override_settings(TMDB_API_KEY="k")
@responses.activate
def test_backfill_fetches_and_fills(monkeypatch):
    monkeypatch.setattr("reeltalk.core.catalog.BACKFILL_REQUEST_INTERVAL", 0)
    film = Film.objects.create(title="Blade Runner", year=1982, tmdb_id=78)
    _mock_tmdb(_details())
    summary = backfill_films([film.id])
    assert summary["backfilled"] == 1
    film.refresh_from_db()
    assert film.runtime == 117
    assert film.description  # filled from the overview
    assert film.poster


@pytest.mark.django_db
@override_settings(TMDB_API_KEY="k")
@responses.activate
def test_backfill_per_film_failure_continues(monkeypatch):
    monkeypatch.setattr("reeltalk.core.catalog.BACKFILL_REQUEST_INTERVAL", 0)
    good = Film.objects.create(title="Good", year=2000, tmdb_id=1)
    bad = Film.objects.create(title="Bad", year=2001, tmdb_id=2)
    # The good film resolves (no poster); the bad film's request is rejected.
    responses.add(
        responses.GET,
        "https://api.themoviedb.org/3/movie/1",
        json=_details(tmdb_id=1, poster_path=None),
        status=200,
    )
    responses.add(
        responses.GET, "https://api.themoviedb.org/3/movie/2", json={}, status=401
    )
    summary = backfill_films([good.id, bad.id])
    assert summary["backfilled"] == 1
    assert summary["failed"] == 1
