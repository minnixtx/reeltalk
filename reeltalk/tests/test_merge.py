"""Film merge/absorb tests (PLAN.md §3.2; D7 backfill semantics)."""

import pytest
from django.contrib.auth import get_user_model

from reeltalk.core.models import Film, MergedFilm, Shelf, ShelfFilm, resolve_film_id

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


def shelve(user, identifier, film):
    shelf = Shelf.objects.get(user=user, identifier=identifier)
    return ShelfFilm.objects.create(shelf=shelf, film=film)


# --- backfill semantics ------------------------------------------------------


@pytest.mark.django_db
def test_merge_backfills_empty_fields_on_canonical():
    canonical = Film.objects.create(title="Dune", year=2021)
    absorbed = Film.objects.create(
        title="Dune (manual)",
        subtitle="Expanded Edition",
        description="<p>A desert planet.</p>",
        runtime=166,
        tmdb_id=496243,
        imdb_id="tt0120737",
        genres=["Sci-Fi"],
    )
    changed = absorbed.merge_into(canonical)
    canonical.refresh_from_db()
    assert set(changed) == {
        "subtitle",
        "description",
        "runtime",
        "tmdb_id",
        "imdb_id",
        "genres",
    }
    assert canonical.subtitle == "Expanded Edition"
    assert canonical.description == "<p>A desert planet.</p>"
    assert canonical.runtime == 166
    assert canonical.tmdb_id == 496243
    assert canonical.imdb_id == "tt0120737"
    assert canonical.genres == ["Sci-Fi"]


@pytest.mark.django_db
def test_merge_keeps_canonical_values_when_set():
    canonical = Film.objects.create(
        title="Dune", year=2021, runtime=155, tmdb_id=496243
    )
    absorbed = Film.objects.create(title="Dune (dup)", year=2020, runtime=166)
    changed = absorbed.merge_into(canonical)
    canonical.refresh_from_db()
    assert "year" not in changed and "runtime" not in changed
    assert canonical.year == 2021
    assert canonical.runtime == 155
    assert canonical.tmdb_id == 496243
    assert canonical.title == "Dune"


@pytest.mark.django_db
def test_merge_unions_arrays_without_duplicates():
    canonical = Film.objects.create(
        title="Dune",
        year=2021,
        genres=["Sci-Fi"],
        directors=["Villeneuve"],
        cast=["Reeves"],
    )
    absorbed = Film.objects.create(
        title="Dune (dup)",
        year=2021,
        genres=["Sci-Fi", "Adventure"],
        directors=["Other"],
        cast=["Reeves", "Day"],
    )
    absorbed.merge_into(canonical)
    canonical.refresh_from_db()
    assert canonical.genres == ["Sci-Fi", "Adventure"]
    assert canonical.directors == ["Villeneuve", "Other"]
    assert canonical.cast == ["Reeves", "Day"]


@pytest.mark.django_db
def test_merge_never_touches_identity_or_provenance_fields():
    canonical = Film.objects.create(title="Dune", year=2021, origin_id=100)
    absorbed = Film.objects.create(title="Dune (dup)", year=2021, origin_id=200)
    changed = absorbed.merge_into(canonical)
    canonical.refresh_from_db()
    assert canonical.title == "Dune"
    assert canonical.sort_title == "dune"
    assert canonical.origin_id == 100
    assert "title" not in changed and "origin_id" not in changed


# --- related-row re-pointing -------------------------------------------------


@pytest.mark.django_db
def test_merge_repoints_shelf_films_preserving_row_data(user):
    canonical = Film.objects.create(title="Dune", year=2021)
    absorbed = Film.objects.create(title="Dune (dup)", year=2021)
    row = shelve(user, Shelf.TO_READ, absorbed)
    shelved_date = row.shelved_date
    absorbed.merge_into(canonical)
    row.refresh_from_db()
    assert row.film_id == canonical.id
    assert row.user == user
    assert row.shelved_date == shelved_date


@pytest.mark.django_db
def test_merge_repoints_shelves_across_users(user):
    bob = User.objects.create_user(localname="bob", password="s3cretpass")
    canonical = Film.objects.create(title="Dune", year=2021)
    absorbed = Film.objects.create(title="Dune (dup)", year=2021)
    shelve(user, Shelf.TO_READ, absorbed)
    shelve(user, Shelf.READ, absorbed)
    shelve(bob, Shelf.TO_READ, absorbed)
    absorbed.merge_into(canonical)
    rows = list(ShelfFilm.objects.filter(film=canonical).order_by("id"))
    assert [row.user for row in rows] == [user, user, bob]


@pytest.mark.django_db
def test_merge_dedups_when_both_films_on_same_shelf(user):
    canonical = Film.objects.create(title="Dune", year=2021)
    absorbed = Film.objects.create(title="Dune (dup)", year=2021)
    old_id = absorbed.id
    shelve(user, Shelf.TO_READ, canonical)
    shelve(user, Shelf.TO_READ, absorbed)
    absorbed.merge_into(canonical)
    assert ShelfFilm.objects.filter(film=canonical).count() == 1
    # delete() clears the instance's pk — the captured id is authoritative.
    assert not Film.objects.filter(id=old_id).exists()


@pytest.mark.django_db
def test_merge_repoints_blocked_films(user):
    canonical = Film.objects.create(title="Dune", year=2021)
    absorbed = Film.objects.create(title="Dune (dup)", year=2021)
    user.blocked_films.add(absorbed)
    absorbed.merge_into(canonical)
    assert list(user.blocked_films.all()) == [canonical]


@pytest.mark.django_db
def test_merge_dedups_block_when_user_blocked_both_films(user):
    canonical = Film.objects.create(title="Dune", year=2021)
    absorbed = Film.objects.create(title="Dune (dup)", year=2021)
    user.blocked_films.add(absorbed, canonical)
    absorbed.merge_into(canonical)
    assert user.blocked_films.count() == 1


# --- id mapping + deletion ---------------------------------------------------


@pytest.mark.django_db
def test_merge_records_id_mapping_and_deletes_absorbed():
    canonical = Film.objects.create(title="Dune", year=2021)
    absorbed = Film.objects.create(title="Dune (dup)", year=2021)
    old_id = absorbed.id
    absorbed.merge_into(canonical)
    assert not Film.objects.filter(id=old_id).exists()
    mapping = MergedFilm.objects.get(old_id=old_id)
    assert mapping.new_id == canonical.id
    assert resolve_film_id(old_id) == canonical.id


@pytest.mark.django_db
def test_merge_into_self_raises():
    film = Film.objects.create(title="Dune", year=2021)
    with pytest.raises(ValueError):
        film.merge_into(film)
