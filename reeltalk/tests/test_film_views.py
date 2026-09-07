"""Film page view tests (PLAN.md §3.7) — detail, create/edit, D4 lock."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from reeltalk.core.models import Film, Shelf, ShelfFilm, Status

User = get_user_model()


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


@pytest.fixture
def film(db):
    return Film.objects.create(
        title="Dune",
        year=2021,
        runtime=155,
        genres=["Sci-Fi"],
        directors=["Denis Villeneuve"],
        description="<p>A desert epic.</p>",
    )


def review(user, film, rating="4.5", content="<p>Great.</p>"):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=rating,
        content=content,
    )


@pytest.mark.django_db
def test_film_detail_renders_metadata(client, film):
    resp = client.get(f"/film/{film.id}/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Dune" in body
    assert "(2021)" in body
    assert "155 min" in body
    assert "Denis Villeneuve" in body
    assert "A desert epic." in body


@pytest.mark.django_db
def test_film_detail_shows_all_users_reviews(client, user, film):
    other = User.objects.create_user(localname="bob", password="s3cretpass")
    review(user, film, rating="4.5", content="<p>Great.</p>")
    review(other, film, rating="3", content="<p>Meh.</p>")
    resp = client.get(f"/film/{film.id}/")
    body = resp.content.decode()
    assert "alice" in body
    assert "bob" in body
    assert "Great." in body
    assert "Meh." in body
    assert "Reviews (2)" in body


@pytest.mark.django_db
def test_film_detail_no_reviews_placeholder(client, film):
    resp = client.get(f"/film/{film.id}/")
    assert "No reviews yet." in resp.content.decode()


@pytest.mark.django_db
def test_film_detail_deleted_review_not_listed(client, user, film):
    entry = review(user, film)
    entry.delete()  # soft
    resp = client.get(f"/film/{film.id}/")
    body = resp.content.decode()
    assert "Great." not in body
    assert "No reviews yet." in body


@pytest.mark.django_db
def test_film_detail_resolves_merged_film(client, film):
    old_id = film.id  # captured before the merge clears the pk
    canonical = Film.objects.create(title="Dune Part Two", year=2024)
    film.merge_into(canonical)
    # The absorbed id now serves the canonical film.
    resp = client.get(f"/film/{old_id}/")
    assert resp.status_code == 200
    assert "Dune Part Two" in resp.content.decode()


@pytest.mark.django_db
def test_film_detail_404_for_missing(client):
    assert client.get("/film/9999/").status_code == 404


# --- create ------------------------------------------------------------------


VALID_FILM = {
    "title": "Blade Runner",
    "subtitle": "",
    "description": "A **neo-noir** future.",
    "year": "1982",
    "runtime": "117",
    "genres": "Sci-Fi, Drama",
    "directors": "Ridley Scott",
    "cast": "Harrison Ford, Rutger Hauer",
}


@pytest.mark.django_db
def test_film_create_requires_login(client):
    resp = client.get("/film/create/")
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


@pytest.mark.django_db
def test_film_create_get_renders_form(login):
    resp = login.get("/film/create/")
    assert resp.status_code == 200
    assert "New film" in resp.content.decode()


@pytest.mark.django_db
def test_film_create_saves_fields_and_markdown(login):
    resp = login.post("/film/create/", VALID_FILM)
    assert resp.status_code == 302
    film = Film.objects.get(title="Blade Runner")
    assert resp["Location"] == f"/film/{film.id}/"
    # Markdown rendered to HTML at write time; raw source kept (R18).
    assert film.description == "<p>A <strong>neo-noir</strong> future.</p>"
    assert film.raw_description == "A **neo-noir** future."
    assert film.genres == ["Sci-Fi", "Drama"]
    assert film.directors == ["Ridley Scott"]
    assert film.cast == ["Harrison Ford", "Rutger Hauer"]
    assert film.year == 1982
    assert film.runtime == 117


@pytest.mark.django_db
def test_film_create_dedups_on_title_year(login, film):
    # `film` fixture is Dune (2021); creating the same title+year redirects.
    resp = login.post(
        "/film/create/",
        {"title": "Dune", "year": "2021"},
    )
    assert resp.status_code == 302
    assert resp["Location"] == f"/film/{film.id}/"
    assert Film.objects.count() == 1


@pytest.mark.django_db
def test_film_create_invalid_renders_errors(login):
    resp = login.post("/film/create/", {"title": ""})
    assert resp.status_code == 200
    assert "This field is required." in resp.content.decode()
    assert Film.objects.count() == 0


# --- edit + D4 lock ------------------------------------------------------------


@pytest.mark.django_db
def test_film_edit_requires_login(client, film):
    resp = client.get(f"/film/{film.id}/edit/")
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


@pytest.mark.django_db
def test_film_edit_get_prefills_manual_film(login, film):
    resp = login.get(f"/film/{film.id}/edit/")
    body = resp.content.decode()
    assert 'value="Dune"' in body
    # Array fields pre-filled as comma-separated text.
    assert 'value="Sci-Fi"' in body
    assert 'value="Denis Villeneuve"' in body


@pytest.mark.django_db
def test_film_edit_updates_fields(login, film):
    resp = login.post(
        f"/film/{film.id}/edit/",
        {**VALID_FILM, "title": "Dune: Part One"},
    )
    assert resp.status_code == 302
    film.refresh_from_db()
    assert film.title == "Dune: Part One"
    assert film.year == 1982
    assert film.genres == ["Sci-Fi", "Drama"]


@pytest.mark.django_db
def test_film_edit_locked_for_tmdb_films(login, db):
    tmdb_film = Film.objects.create(title="Dune", year=2021, tmdb_id=491306)
    # GET is refused with a redirect back to the detail page.
    resp = login.get(f"/film/{tmdb_film.id}/edit/")
    assert resp.status_code == 302
    assert resp["Location"] == f"/film/{tmdb_film.id}/"
    # POST is refused too — metadata unchanged.
    resp = login.post(
        f"/film/{tmdb_film.id}/edit/", {"title": "Hacked", "year": "1999"}
    )
    assert resp.status_code == 302
    tmdb_film.refresh_from_db()
    assert tmdb_film.title == "Dune"
    assert tmdb_film.year == 2021


@pytest.mark.django_db
def test_film_edit_resolves_merged_film(login, film):
    old_id = film.id
    canonical = Film.objects.create(title="Dune Part Two", year=2024)
    film.merge_into(canonical)
    resp = login.get(f"/film/{old_id}/edit/")
    assert resp.status_code == 200
    assert 'value="Dune Part Two"' in resp.content.decode()


# --- shelve / unshelve (D1 mutual exclusion) ---------------------------------


@pytest.mark.django_db
def test_shelve_requires_login(client, film):
    resp = client.post(f"/film/{film.id}/shelve/")
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


@pytest.mark.django_db
def test_shelve_is_post_only(login, film):
    assert login.get(f"/film/{film.id}/shelve/").status_code == 405


@pytest.mark.django_db
def test_shelve_adds_to_watchlist(login, film):
    resp = login.post(f"/film/{film.id}/shelve/")
    assert resp.status_code == 302
    assert resp["Location"] == f"/film/{film.id}/"
    row = ShelfFilm.objects.get(film=film)
    assert row.shelf.identifier == "to-read"


@pytest.mark.django_db
def test_shelve_refused_when_watched(login, user, film):
    # Put the film on the user's Watched shelf directly.
    watched_shelf = Shelf.objects.get(user=user, identifier=Shelf.READ)
    ShelfFilm.objects.create(shelf=watched_shelf, film=film)
    resp = login.post(f"/film/{film.id}/shelve/")
    assert resp.status_code == 302
    # No watchlist row created (mutual exclusion).
    assert not ShelfFilm.objects.filter(shelf__identifier="to-read", film=film).exists()


@pytest.mark.django_db
def test_unshelve_removes_from_watchlist(login, film):
    login.post(f"/film/{film.id}/shelve/")
    assert ShelfFilm.objects.filter(film=film).count() == 1
    resp = login.post(f"/film/{film.id}/unshelve/")
    assert resp.status_code == 302
    assert not ShelfFilm.objects.filter(film=film).exists()


@pytest.mark.django_db
def test_detail_shows_correct_shelve_button(login, film):
    body = login.get(f"/film/{film.id}/").content.decode()
    assert "Add to Watchlist" in body
    login.post(f"/film/{film.id}/shelve/")
    body = login.get(f"/film/{film.id}/").content.decode()
    assert "Remove from Watchlist" in body
    assert "Add to Watchlist" not in body
