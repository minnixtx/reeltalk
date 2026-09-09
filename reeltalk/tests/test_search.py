"""Global search tests (M2 increment 4, D6) — TMDB HTTP mocked.

Covers the local tsvector fallback, the results page (TMDB-backed with
degradation + blocked-film exclusion), the click-through create-or-match
route, the one-click watchlist POST, and the suggest JSON endpoint.
"""

from io import BytesIO

import pytest
import responses
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from PIL import Image

from reeltalk.core.catalog import search_local
from reeltalk.core.models import Film, ShelfFilm, mark_watched, shelve_to_watchlist

User = get_user_model()

SEARCH_URL = "https://api.themoviedb.org/3/search/movie"


def _tiny_jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (10, 10), (90, 60, 120)).save(buf, format="JPEG")
    return buf.getvalue()


def _search_payload(rows, page=1, total_pages=1):
    return {
        "page": page,
        "total_results": len(rows),
        "total_pages": total_pages,
        "results": [
            {
                "id": row["tmdb_id"],
                "title": row["title"],
                "release_date": f"{row['year']}-06-25" if row.get("year") else None,
                "poster_path": row.get("poster_path"),
            }
            for row in rows
        ],
    }


def _mock_search(rows, page=1, total_pages=1):
    responses.add(
        responses.GET,
        SEARCH_URL,
        json=_search_payload(rows, page, total_pages),
        status=200,
    )


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


def _mock_details(details):
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
            body=_tiny_jpeg(),
            status=200,
        )


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def user(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def login(client, user):
    assert client.login(username="alice", password="s3cretpass")
    return client


# --- search_local (D6 fallback) ---------------------------------------------


@pytest.mark.django_db
def test_search_local_finds_by_title():
    Film.objects.create(title="Blade Runner", year=1982)
    Film.objects.create(title="Dune", year=2021)
    results = search_local("blade")
    assert [f.title for f in results] == ["Blade Runner"]


@pytest.mark.django_db
def test_search_local_empty_query_returns_nothing():
    Film.objects.create(title="Blade Runner", year=1982)
    assert search_local("") == []
    assert search_local("   ") == []


@pytest.mark.django_db
def test_search_local_matches_names_and_genres():
    Film.objects.create(
        title="The Terminator",
        year=1984,
        directors=["James Cameron"],
        cast=["Arnold Schwarzenegger"],
        genres=["Action"],
    )
    assert [f.title for f in search_local("cameron")] == ["The Terminator"]
    assert [f.title for f in search_local("action")] == ["The Terminator"]


@pytest.mark.django_db
def test_search_local_no_match_returns_empty():
    Film.objects.create(title="Blade Runner", year=1982)
    assert search_local("zebra") == []


# --- Search page -------------------------------------------------------------


@pytest.mark.django_db
def test_search_page_empty_query_shows_hint(client):
    resp = client.get("/search/")
    assert resp.status_code == 200
    assert "Use the search box" in resp.content.decode()


@responses.activate
@override_settings(TMDB_API_KEY="fake-key")
@pytest.mark.django_db
def test_search_page_tmdb_results_for_authenticated_user(login):
    _mock_search(
        [
            {"tmdb_id": 78, "title": "Blade Runner", "year": 1982},
            {"tmdb_id": 346, "title": "Blade Runner 2049", "year": 2017},
        ]
    )
    resp = login.get("/search/?q=blade+runner")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Blade Runner" in body
    assert "(1982)" in body
    assert "/search/film/78/" in body  # click-through link
    assert "watchlist-btn" in body  # one-click action


@responses.activate
@override_settings(TMDB_API_KEY="fake-key")
@pytest.mark.django_db
def test_search_page_anonymous_sees_results_without_actions(client):
    _mock_search([{"tmdb_id": 78, "title": "Blade Runner", "year": 1982}])
    resp = client.get("/search/?q=blade+runner")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Blade Runner" in body
    assert "/search/film/78/" not in body  # no click-through link
    assert "watchlist-btn" not in body  # no one-click action


@responses.activate
@override_settings(TMDB_API_KEY="fake-key")
@pytest.mark.django_db
def test_search_page_excludes_locally_blocked_films(login, user):
    blocked = Film.objects.create(title="Blade Runner", year=1982, tmdb_id=78)
    user.blocked_films.add(blocked)
    _mock_search(
        [
            {"tmdb_id": 78, "title": "Blade Runner", "year": 1982},
            {"tmdb_id": 346, "title": "Blade Runner 2049", "year": 2017},
        ]
    )
    resp = login.get("/search/?q=blade+runner")
    body = resp.content.decode()
    assert "Blade Runner 2049" in body
    assert "/search/film/78/" not in body


@responses.activate
@override_settings(TMDB_API_KEY="fake-key")
@pytest.mark.django_db
def test_search_page_tmdb_failure_degrades_to_local(login):
    film = Film.objects.create(title="Blade Runner", year=1982)
    responses.add(responses.GET, SEARCH_URL, status=500)
    resp = login.get("/search/?q=blade+runner")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "TMDB request failed (HTTP 500)" in body  # user-facing message
    assert f"/film/{film.id}/" in body  # local fallback row links to the film page


@responses.activate
@override_settings(TMDB_API_KEY="")
@pytest.mark.django_db
def test_search_page_without_key_is_local_only(client):
    Film.objects.create(title="Dune", year=2021)
    resp = client.get("/search/?q=dune")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Dune" in body
    assert len(responses.calls) == 0  # no TMDB call at all


# --- Click-through (D7 create-or-match) ---------------------------------------


@pytest.mark.django_db
def test_clickthrough_anonymous_redirects_to_login(client):
    resp = client.get("/search/film/78/")
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


@responses.activate
@pytest.mark.django_db
def test_clickthrough_existing_film_makes_no_api_call(login):
    film = Film.objects.create(title="Blade Runner", year=1982, tmdb_id=78)
    resp = login.get("/search/film/78/")
    assert resp.status_code == 302
    assert resp["Location"] == f"/film/{film.id}/"
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_clickthrough_creates_new_film(login):
    _mock_details(_details())
    resp = login.get("/search/film/78/")
    assert resp.status_code == 302
    film = Film.objects.get(tmdb_id=78)
    assert film.title == "Blade Runner"
    assert film.year == 1982
    assert resp["Location"] == f"/film/{film.id}/"
    detail = login.get(resp["Location"])
    assert "Added “Blade Runner” to your library." in detail.content.decode()


@responses.activate
@pytest.mark.django_db
def test_clickthrough_tmdb_error_shows_message(login):
    responses.add(responses.GET, "https://api.themoviedb.org/3/movie/78", status=401)
    resp = login.get("/search/film/78/")
    assert resp.status_code == 302
    assert resp["Location"] == "/search/"
    assert Film.objects.count() == 0


# --- One-click watchlist POST --------------------------------------------------


@pytest.mark.django_db
def test_watchlist_anonymous_redirects_to_login(client):
    resp = client.post("/search/watchlist/78/")
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


@responses.activate
@pytest.mark.django_db
def test_watchlist_get_is_not_allowed(login):
    resp = login.get("/search/watchlist/78/")
    assert resp.status_code == 405


@responses.activate
@pytest.mark.django_db
def test_watchlist_adds_new_film(login, user):
    _mock_details(_details())
    resp = login.post("/search/watchlist/78/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "added"}
    film = Film.objects.get(tmdb_id=78)
    assert ShelfFilm.objects.filter(
        user=user, film=film, shelf__identifier="to-read"
    ).exists()


@responses.activate
@pytest.mark.django_db
def test_watchlist_already_is_idempotent(login, user):
    film = Film.objects.create(title="Blade Runner", year=1982, tmdb_id=78)
    assert shelve_to_watchlist(user, film) == "added"
    resp = login.post("/search/watchlist/78/")  # existing row: no API call
    assert resp.status_code == 200
    assert resp.json() == {"status": "already"}
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_watchlist_refuses_watched_film(login, user):
    film = Film.objects.create(title="Blade Runner", year=1982, tmdb_id=78)
    mark_watched(user, film, rating="4")
    resp = login.post("/search/watchlist/78/")
    assert resp.status_code == 409
    assert resp.json() == {"status": "watched"}


@responses.activate
@pytest.mark.django_db
def test_watchlist_tmdb_error_returns_502(login):
    responses.add(responses.GET, "https://api.themoviedb.org/3/movie/78", status=401)
    resp = login.post("/search/watchlist/78/")
    assert resp.status_code == 502
    assert "TMDB API key" in resp.json()["error"]


# --- Suggest endpoint ----------------------------------------------------------


@pytest.mark.django_db
def test_suggest_short_query_returns_empty(client):
    resp = client.get("/search/suggest/?q=a")
    assert resp.status_code == 200
    assert resp.json() == {"results": []}


@responses.activate
@override_settings(TMDB_API_KEY="fake-key")
@pytest.mark.django_db
def test_suggest_tmdb_rows_link_to_clickthrough_for_users(login):
    _mock_search([{"tmdb_id": 78, "title": "Blade Runner", "year": 1982}])
    resp = login.get("/search/suggest/?q=blade")
    data = resp.json()
    assert data["results"][0] == {
        "title": "Blade Runner",
        "year": 1982,
        "tmdb_id": 78,
        "url": "/search/film/78/",
    }


@responses.activate
@override_settings(TMDB_API_KEY="fake-key")
@pytest.mark.django_db
def test_suggest_anonymous_rows_link_to_search_page(client):
    _mock_search([{"tmdb_id": 78, "title": "Blade Runner", "year": 1982}])
    resp = client.get("/search/suggest/?q=blade")
    url = resp.json()["results"][0]["url"]
    assert url.startswith("/search/?q=")


@responses.activate
@override_settings(TMDB_API_KEY="fake-key")
@pytest.mark.django_db
def test_suggest_caps_at_eight_rows(login):
    rows = [
        {"tmdb_id": 100 + i, "title": f"Blade Runner {i}", "year": 1982 + i}
        for i in range(10)
    ]
    _mock_search(rows)
    resp = login.get("/search/suggest/?q=blade")
    assert len(resp.json()["results"]) == 8


@responses.activate
@override_settings(TMDB_API_KEY="")
@pytest.mark.django_db
def test_suggest_local_when_no_key(client):
    film = Film.objects.create(title="Dune", year=2021)
    resp = client.get("/search/suggest/?q=dune")
    data = resp.json()
    assert data["results"] == [
        {"title": "Dune", "year": 2021, "tmdb_id": None, "url": f"/film/{film.id}/"}
    ]
    assert len(responses.calls) == 0


@responses.activate
@override_settings(TMDB_API_KEY="fake-key")
@pytest.mark.django_db
def test_suggest_falls_back_to_local_on_tmdb_failure(client):
    film = Film.objects.create(title="Dune", year=2021)
    responses.add(responses.GET, SEARCH_URL, status=500)
    resp = client.get("/search/suggest/?q=dune")
    data = resp.json()
    assert data["results"][0]["url"] == f"/film/{film.id}/"
