"""Shelf/ShelfFilm + binary default shelves tests (PLAN.md §3.2, decision D1)."""

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from reeltalk.core.models import (
    Film,
    Shelf,
    ShelfFilm,
    shelve_to_watchlist,
    unshelve_from_watchlist,
)

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


def shelf_of(user, identifier):
    return Shelf.objects.get(user=user, identifier=identifier)


# --- default shelves (D1) ---------------------------------------------------


@pytest.mark.django_db
def test_new_local_user_gets_the_two_default_shelves(user):
    shelves = {s.identifier: s for s in Shelf.objects.filter(user=user)}
    assert set(shelves) == {"to-read", "read"}
    assert shelves["to-read"].name == "Watchlist"
    assert shelves["read"].name == "Watched"


@pytest.mark.django_db
def test_remote_user_gets_no_default_shelves(db):
    User.objects.create_user(localname="remote", local=False)
    assert Shelf.objects.count() == 0


@pytest.mark.django_db
def test_resaving_user_does_not_duplicate_shelves(user):
    user.save()
    assert Shelf.objects.filter(user=user).count() == 2


# --- ShelfFilm ---------------------------------------------------------------


@pytest.mark.django_db
def test_shelf_film_defaults_user_to_shelf_owner(user):
    film = Film.objects.create(title="Dune", year=2021)
    row = ShelfFilm.objects.create(shelf=shelf_of(user, Shelf.TO_READ), film=film)
    assert row.user == user


@pytest.mark.django_db
def test_a_film_cannot_be_on_one_shelf_twice(user):
    film = Film.objects.create(title="Dune", year=2021)
    shelf = shelf_of(user, Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=shelf, film=film)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ShelfFilm.objects.create(shelf=shelf, film=film)


@pytest.mark.django_db
def test_m2m_add_bypasses_through_save_so_needs_through_defaults(user):
    """Django's M2M add() bulk-inserts without calling the through model's
    save(), so the actor must be passed explicitly (R14)."""
    film = Film.objects.create(title="Dune", year=2021)
    shelf = shelf_of(user, Shelf.TO_READ)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            shelf.films.add(film)
    shelf.films.add(film, through_defaults={"user": user})
    assert ShelfFilm.objects.filter(shelf=shelf, film=film).count() == 1


@pytest.mark.django_db
def test_unshelve_removes_the_row(user):
    film = Film.objects.create(title="Dune", year=2021)
    shelf = shelf_of(user, Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=shelf, film=film)
    shelf.films.remove(film)
    assert not ShelfFilm.objects.filter(shelf=shelf, film=film).exists()


@pytest.mark.django_db
def test_film_shelves_reverse_relation(user):
    """film.shelves lists the shelves a film sits on (used by the films page
    and the finish flow in later increments)."""
    watchlist = shelf_of(user, Shelf.TO_READ)
    watched = shelf_of(user, Shelf.READ)
    film = Film.objects.create(title="Dune", year=2021)
    ShelfFilm.objects.create(shelf=watchlist, film=film)
    assert list(film.shelves.all()) == [watchlist]
    ShelfFilm.objects.create(shelf=watched, film=film)
    assert set(film.shelves.all()) == {watchlist, watched}


# --- Watchlist shelve/unshelve helpers (D1 mutual exclusion) -----------------


@pytest.mark.django_db
def test_shelve_to_watchlist_adds_a_row(user):
    film = Film.objects.create(title="Dune", year=2021)
    assert shelve_to_watchlist(user, film) == "added"
    row = ShelfFilm.objects.get(film=film)
    assert row.shelf.identifier == Shelf.TO_READ
    assert row.user == user


@pytest.mark.django_db
def test_shelve_to_watchlist_is_idempotent(user):
    film = Film.objects.create(title="Dune", year=2021)
    shelve_to_watchlist(user, film)
    assert shelve_to_watchlist(user, film) == "already"
    assert ShelfFilm.objects.filter(film=film).count() == 1


@pytest.mark.django_db
def test_shelve_to_watchlist_refused_when_watched(user):
    """D1: a watched film cannot also be on the watchlist."""
    film = Film.objects.create(title="Dune", year=2021)
    watched = shelf_of(user, Shelf.READ)
    ShelfFilm.objects.create(shelf=watched, film=film)
    assert shelve_to_watchlist(user, film) == "watched"
    # No watchlist row was created.
    assert not ShelfFilm.objects.filter(
        shelf__identifier=Shelf.TO_READ, film=film
    ).exists()


@pytest.mark.django_db
def test_unshelve_from_watchlist_removes_and_reports(user):
    film = Film.objects.create(title="Dune", year=2021)
    shelve_to_watchlist(user, film)
    assert unshelve_from_watchlist(user, film) is True
    assert not ShelfFilm.objects.filter(film=film).exists()
    # Second call: nothing left to remove.
    assert unshelve_from_watchlist(user, film) is False
