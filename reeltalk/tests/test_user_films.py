"""User films page tests (M1 increment 6; PLAN.md §3.3 rule 1, D1).

Covers the User films-page query API (films_on_shelf / all_films with the
rating annotation) and the view: exactly three tabs — All films / Watchlist /
Watched — plus tab filtering and public readability.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from reeltalk.core.models import Film, Shelf, ShelfFilm, Status, mark_watched

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
    return Film.objects.create(title="Dune", year=2021)


# --- model query API ----------------------------------------------------------


@pytest.mark.django_db
def test_films_on_shelf_returns_only_that_shelf(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    wanted = Film.objects.create(title="Dune", year=2021)
    watched = Film.objects.create(title="Blade Runner", year=1982)
    to_read = Shelf.objects.get(user=alice, identifier=Shelf.TO_READ)
    read = Shelf.objects.get(user=alice, identifier=Shelf.READ)
    ShelfFilm.objects.create(shelf=to_read, film=wanted)
    ShelfFilm.objects.create(shelf=read, film=watched)
    assert list(alice.films_on_shelf(Shelf.TO_READ)) == [wanted]
    assert list(alice.films_on_shelf(Shelf.READ)) == [watched]


@pytest.mark.django_db
def test_films_on_shelf_scoped_to_the_user(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    bob = User.objects.create_user(localname="bob", password="s3cretpass")
    shared = Film.objects.create(title="Dune", year=2021)
    # Bob shelved it on HIS watchlist — alice's page must not show it.
    ShelfFilm.objects.create(
        shelf=Shelf.objects.get(user=bob, identifier=Shelf.TO_READ), film=shared
    )
    assert list(alice.films_on_shelf(Shelf.TO_READ)) == []


@pytest.mark.django_db
def test_all_films_is_shelves_plus_statuses(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    wanted = Film.objects.create(title="Dune", year=2021)
    watched = Film.objects.create(title="Blade Runner", year=1982)
    reviewed_only = Film.objects.create(title="Arrival", year=2016)
    to_read = Shelf.objects.get(user=alice, identifier=Shelf.TO_READ)
    read = Shelf.objects.get(user=alice, identifier=Shelf.READ)
    ShelfFilm.objects.create(shelf=to_read, film=wanted)
    ShelfFilm.objects.create(shelf=read, film=watched)
    # A status without a shelf row still counts as a relationship (§3.5/D10).
    Status.objects.create(
        user=alice, film=reviewed_only, status_type=Status.Type.REVIEW, rating="4"
    )
    ids = {f.id for f in alice.all_films()}
    assert ids == {wanted.id, watched.id, reviewed_only.id}


@pytest.mark.django_db
def test_all_films_ignores_deleted_statuses(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    film = Film.objects.create(title="Arrival", year=2016)
    entry = Status.objects.create(
        user=alice, film=film, status_type=Status.Type.REVIEW_RATING, rating="4"
    )
    assert {f.id for f in alice.all_films()} == {film.id}
    entry.delete()  # soft — the tombstone doesn't count as a relationship
    assert list(alice.all_films()) == []


@pytest.mark.django_db
def test_all_films_carries_the_users_rating(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    watched = Film.objects.create(title="Dune", year=2021)
    wanted = Film.objects.create(title="Blade Runner", year=1982)
    mark_watched(alice, watched, rating="4.5")
    to_read = Shelf.objects.get(user=alice, identifier=Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=to_read, film=wanted)
    by_id = {f.id: f for f in alice.all_films()}
    assert by_id[watched.id].user_rating == Decimal("4.5")
    assert by_id[wanted.id].user_rating is None


# --- view ----------------------------------------------------------------------


@pytest.mark.django_db
def test_user_films_page_renders_three_tabs(client, user):
    # Publicly readable — no login required.
    resp = client.get("/user/alice/films/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "All films" in body
    assert "Watchlist" in body
    assert "Watched" in body


@pytest.mark.django_db
def test_user_films_tab_filtering(login, user, film):
    watched = Film.objects.create(title="Blade Runner", year=1982)
    login.post(f"/film/{film.id}/shelve/")  # Dune -> Watchlist
    mark_watched(user, watched, rating="3")  # Blade Runner -> Watched
    body = login.get("/user/alice/films/").content.decode()
    assert "Dune" in body and "Blade Runner" in body
    body = login.get("/user/alice/films/?tab=watchlist").content.decode()
    assert "Dune" in body and "Blade Runner" not in body
    body = login.get("/user/alice/films/?tab=watched").content.decode()
    assert "Blade Runner" in body and "Dune" not in body


@pytest.mark.django_db
def test_user_films_unknown_tab_falls_back_to_all(login, user, film):
    login.post(f"/film/{film.id}/shelve/")
    body = login.get("/user/alice/films/?tab=bogus").content.decode()
    assert "Dune" in body


@pytest.mark.django_db
def test_user_films_404_for_unknown_user(client):
    assert client.get("/user/nobody/films/").status_code == 404


@pytest.mark.django_db
def test_user_films_watched_tab_shows_rating(login, user, film):
    mark_watched(user, film, rating="4.5")
    body = login.get("/user/alice/films/?tab=watched").content.decode()
    # 4.5/5 -> the star fill is clipped to 90%.
    assert "width:90%" in body


@pytest.mark.django_db
def test_user_films_links_to_film_pages(login, user, film):
    login.post(f"/film/{film.id}/shelve/")
    body = login.get("/user/alice/films/").content.decode()
    assert f'href="/film/{film.id}/"' in body
