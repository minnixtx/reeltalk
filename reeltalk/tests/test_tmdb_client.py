"""TMDB client tests (PLAN.md §3.4) — HTTP mocked with ``responses``.

The API key is irrelevant to the mocked HTTP assertions (``responses`` matches
on the URL path, not the query string), so it is only set where a test asserts
on configuration itself — via ``override_settings``, which patches the module
Django's ``LazySettings`` actually reads.
"""

import pytest
import requests
import responses
from django.test import override_settings

from reeltalk.core.tmdb import (
    FilmSearchResults,
    SearchResult,
    TmdbAuthError,
    TmdbNetworkError,
    TmdbRateLimitError,
    download_poster,
    film_fields_from_tmdb,
    get_film_details,
    is_configured,
    search_films,
)

SEARCH_URL = "https://api.themoviedb.org/3/search/movie"


# --- configuration (D8) -----------------------------------------------------


@override_settings(TMDB_API_KEY="")
def test_is_configured_false_when_unset():
    assert is_configured() is False


@override_settings(TMDB_API_KEY="a-key")
def test_is_configured_true_when_set():
    assert is_configured() is True


# --- search (D6) ------------------------------------------------------------


@responses.activate
def test_search_films_success():
    responses.add(
        responses.GET,
        SEARCH_URL,
        json={
            "results": [
                {
                    "id": 78,
                    "title": "Blade Runner",
                    "release_date": "1982-06-25",
                    "poster_path": "/gajva2L0rPYkEWjzgFlBXCAVBE5.jpg",
                },
                {
                    "id": 335984,
                    "title": "Blade Runner 2049",
                    "release_date": None,
                    "poster_path": None,
                },
            ],
            "total_pages": 1,
        },
        status=200,
    )
    result = search_films("blade runner")
    assert isinstance(result, FilmSearchResults)
    assert result.page == 1
    assert result.total_pages == 1
    assert [r.tmdb_id for r in result.rows] == [78, 335984]
    first = result.rows[0]
    assert isinstance(first, SearchResult)
    assert first.title == "Blade Runner"
    assert first.year == 1982
    assert first.poster_url == (
        "https://image.tmdb.org/t/p/w500/gajva2L0rPYkEWjzgFlBXCAVBE5.jpg"
    )
    # A missing release_date / poster yields None, not a crash.
    assert result.rows[1].year is None
    assert result.rows[1].poster_url is None


@responses.activate
def test_search_films_empty_results():
    responses.add(
        responses.GET, SEARCH_URL, json={"results": [], "total_pages": 0}, status=200
    )
    assert search_films("zzzz").rows == []


@responses.activate
def test_search_films_bad_key_raises_auth_error():
    responses.add(
        responses.GET,
        SEARCH_URL,
        json={"status_message": "Invalid API key"},
        status=401,
    )
    with pytest.raises(TmdbAuthError):
        search_films("blade runner")


@responses.activate
def test_search_films_rate_limited():
    responses.add(responses.GET, SEARCH_URL, json={}, status=429)
    with pytest.raises(TmdbRateLimitError):
        search_films("blade runner")


@responses.activate
def test_search_films_network_error():
    responses.add(responses.GET, SEARCH_URL, body=requests.ConnectionError("down"))
    with pytest.raises(TmdbNetworkError):
        search_films("blade runner")


@responses.activate
def test_search_films_unexpected_status_is_network_error():
    responses.add(responses.GET, SEARCH_URL, json={}, status=500)
    with pytest.raises(TmdbNetworkError):
        search_films("blade runner")


# --- details ----------------------------------------------------------------


@responses.activate
def test_get_film_details_returns_payload():
    payload = {
        "id": 78,
        "title": "Blade Runner",
        "release_date": "1982-06-25",
        "runtime": 117,
        "overview": "A blade runner must track down and retire four replicas.",
        "genres": [{"id": 878, "name": "Science Fiction"}],
        "credits": {
            "crew": [
                {"name": "Ridley Scott", "job": "Director"},
                {"name": "Someone", "job": "Composer"},
            ],
            "cast": [{"name": "Harrison Ford"}],
        },
        "images": {"posters": []},
    }
    responses.add(
        responses.GET,
        "https://api.themoviedb.org/3/movie/78",
        json=payload,
        status=200,
    )
    assert get_film_details(78) == payload


@responses.activate
def test_get_film_details_bad_key():
    responses.add(
        responses.GET, "https://api.themoviedb.org/3/movie/78", json={}, status=401
    )
    with pytest.raises(TmdbAuthError):
        get_film_details(78)


# --- poster download --------------------------------------------------------


@responses.activate
def test_download_poster_success():
    responses.add(
        responses.GET,
        "https://image.tmdb.org/t/p/w500/abc.jpg",
        body=b"fake-jpeg-bytes",
        status=200,
    )
    assert download_poster("/abc.jpg") == b"fake-jpeg-bytes"


def test_download_poster_none_without_path():
    assert download_poster(None) is None
    assert download_poster("") is None


@responses.activate
def test_download_poster_failure_raises():
    responses.add(responses.GET, "https://image.tmdb.org/t/p/w500/abc.jpg", status=404)
    with pytest.raises(TmdbNetworkError):
        download_poster("/abc.jpg")


# --- field mapping (§3.4) ---------------------------------------------------


@pytest.mark.django_db
def test_film_fields_from_tmdb_mapping():
    details = {
        "title": "Blade Runner",
        "release_date": "1982-06-25",
        "runtime": 117,
        "overview": "A blade runner must track down four replicas.\nSecond line.",
        "genres": [{"name": "Science Fiction"}, {"name": "Drama"}],
        "credits": {
            "crew": [
                {"name": "Ridley Scott", "job": "Director"},
                {"name": "Not A Director", "job": "Editor"},
            ],
            # 12 cast members — only the first 10 should be kept.
            "cast": [{"name": f"Actor {i}"} for i in range(12)],
        },
    }
    fields = film_fields_from_tmdb(details)
    assert fields["title"] == "Blade Runner"
    assert fields["year"] == 1982
    assert fields["runtime"] == 117
    assert fields["genres"] == ["Science Fiction", "Drama"]
    assert fields["directors"] == ["Ridley Scott"]
    assert len(fields["cast"]) == 10
    assert fields["cast"][0] == "Actor 0"
    # The overview is kept verbatim and rendered to HTML for display.
    assert fields["raw_description"].startswith("A blade runner")
    assert "<p>" in fields["description"]


@pytest.mark.django_db
def test_film_fields_from_tmdb_missing_optional_values():
    details = {"title": "Mystery", "release_date": None, "runtime": 0}
    fields = film_fields_from_tmdb(details)
    assert fields["year"] is None
    assert fields["runtime"] is None
    assert fields["genres"] == []
    assert fields["directors"] == []
    assert fields["cast"] == []
    assert fields["raw_description"] == ""
    assert fields["description"] == ""
