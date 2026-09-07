"""Status + watch rules tests (PLAN.md §3.2–3.3, decisions D1/D3/D5)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from reeltalk.core.models import (
    Film,
    Shelf,
    ShelfFilm,
    Status,
    mark_watched,
    validate_star_rating,
)

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def film(db):
    return Film.objects.create(title="Dune", year=2021)


def comment(user, film, content="nice"):
    return Status.objects.create(
        user=user, film=film, status_type=Status.Type.COMMENT, content=content
    )


# --- basics ------------------------------------------------------------------


@pytest.mark.django_db
def test_statuses_order_newest_first(user, film):
    older = comment(user, film, "older")
    older.published_date = timezone.now() - timedelta(hours=2)
    newer = comment(user, film, "newer")
    assert list(Status.objects.all()) == [newer, older]


@pytest.mark.django_db
def test_soft_delete_keeps_tombstone_and_clears_content(user, film):
    status = comment(user, film, "<p>secret</p>")
    status.raw_content = "secret"
    status.delete()
    status.refresh_from_db()
    # The row stays (federation tombstone, §3.2) with identity intact…
    assert Status.objects.filter(pk=status.pk).exists()
    assert status.user_id == user.id
    assert status.film_id == film.id
    # …and the user content is cleared.
    assert status.deleted is True
    assert status.content == ""
    assert status.raw_content == ""
    assert status.deleted_date is not None


@pytest.mark.django_db
def test_soft_delete_is_idempotent(user, film):
    status = comment(user, film)
    status.delete()
    first_deleted_date = status.deleted_date
    status.delete()
    status.refresh_from_db()
    assert status.deleted_date == first_deleted_date


@pytest.mark.django_db
def test_rating_only_entry_requires_a_rating(user, film):
    with pytest.raises(ValueError):
        Status.objects.create(
            user=user, film=film, status_type=Status.Type.REVIEW_RATING
        )
    assert Status.objects.count() == 0


@pytest.mark.django_db
def test_typed_status_requires_a_film(user):
    with pytest.raises(ValueError):
        Status.objects.create(
            user=user, status_type=Status.Type.COMMENT, content="floating"
        )


@pytest.mark.django_db
def test_written_review_may_lack_a_rating(user, film):
    status = Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        content="<p>no stars</p>",
    )
    assert status.rating is None
    assert status.is_review


@pytest.mark.django_db
@pytest.mark.parametrize("rating", [Decimal("0.25"), Decimal("5.5")])
def test_rating_field_bounds(user, film, rating):
    status = Status(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        content="x",
        rating=rating,
    )
    with pytest.raises(ValidationError):
        status.full_clean()


# --- D5: one review per user per film -----------------------------------------


@pytest.mark.django_db
def test_one_review_per_user_per_film(user, film):
    Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=Decimal("4"),
        content="a",
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Status.objects.create(
                user=user,
                film=film,
                status_type=Status.Type.REVIEW_RATING,
                rating=Decimal("3"),
            )


@pytest.mark.django_db
def test_deleted_review_does_not_block_a_new_one(user, film):
    old = Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=Decimal("4"),
        content="a",
    )
    old.delete()  # soft
    new = Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW_RATING,
        rating=Decimal("3"),
    )
    assert new.id != old.id


@pytest.mark.django_db
def test_multiple_comments_are_allowed(user, film):
    comment(user, film)
    comment(user, film, "also nice")
    Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=Decimal("4"),
        content="great",
    )
    assert Status.objects.filter(film=film).count() == 3


# --- §3.3: mark watched --------------------------------------------------------


@pytest.mark.django_db
def test_mark_watched_shelves_and_creates_rating_only_entry(user, film):
    entry = mark_watched(user, film, rating="4.5")
    assert entry.status_type == Status.Type.REVIEW_RATING
    assert entry.rating == Decimal("4.5")
    assert entry.content == ""
    watched = Shelf.objects.get(user=user, identifier=Shelf.READ)
    row = ShelfFilm.objects.get(film=film)
    assert row.shelf == watched
    assert row.user == user


@pytest.mark.django_db
def test_mark_watched_with_text_creates_a_written_review(user, film):
    entry = mark_watched(
        user, film, rating="3", content="<p>Great.</p>", raw_content="Great."
    )
    assert entry.status_type == Status.Type.REVIEW
    assert entry.content == "<p>Great.</p>"
    assert entry.raw_content == "Great."


@pytest.mark.django_db
def test_mark_watched_validates_rating_before_any_write(user, film):
    for bad in (None, "", Decimal("0"), Decimal("5.5"), Decimal("3.25")):
        with pytest.raises(ValueError):
            mark_watched(user, film, rating=bad)
    assert ShelfFilm.objects.count() == 0
    assert Status.objects.count() == 0


@pytest.mark.django_db
def test_mark_watched_removes_the_film_from_the_watchlist(user, film):
    watchlist = Shelf.objects.get(user=user, identifier=Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=watchlist, film=film, user=user)
    mark_watched(user, film, rating="3")
    assert not ShelfFilm.objects.filter(shelf=watchlist, film=film).exists()
    assert ShelfFilm.objects.filter(film=film).count() == 1


@pytest.mark.django_db
def test_refinishing_updates_the_review_in_place(user, film):
    first = mark_watched(user, film, rating="3")
    assert first.status_type == Status.Type.REVIEW_RATING
    second = mark_watched(
        user, film, rating="4.5", content="<p>Great.</p>", raw_content="Great."
    )
    # D5 rule 4: same row, not a new one — the type stays rating-only even
    # though it now carries text (legacy behavior).
    assert second.id == first.id
    assert second.status_type == Status.Type.REVIEW_RATING
    assert second.rating == Decimal("4.5")
    assert second.content == "<p>Great.</p>"
    assert second.edited_date is not None
    assert Status.objects.filter(user=user, film=film).count() == 1
    assert ShelfFilm.objects.filter(film=film).count() == 1


@pytest.mark.django_db
def test_refinishing_with_empty_text_keeps_existing_content(user, film):
    mark_watched(user, film, rating="3", content="<p>First.</p>", raw_content="First.")
    again = mark_watched(user, film, rating="5")
    again.refresh_from_db()
    assert again.content == "<p>First.</p>"
    assert again.raw_content == "First."
    assert again.rating == Decimal("5")


@pytest.mark.django_db
def test_mark_watched_after_a_deleted_review_creates_a_new_row(user, film):
    old = Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=Decimal("4"),
        content="a",
    )
    old.delete()  # soft
    new = mark_watched(user, film, rating="2.5")
    assert new.id != old.id
    assert new.status_type == Status.Type.REVIEW_RATING


# --- validate_star_rating ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4.5", Decimal("4.5")),
        (3, Decimal("3")),
        (2.5, Decimal("2.5")),
        ("0.5", Decimal("0.5")),
        ("5", Decimal("5")),
    ],
)
def test_validate_star_rating_accepts_form_inputs(raw, expected):
    assert validate_star_rating(raw) == expected


@pytest.mark.parametrize("bad", [None, "", "0", "5.5", "3.25", "-1"])
def test_validate_star_rating_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        validate_star_rating(bad)


# --- merge/absorb interaction (R16) --------------------------------------------


@pytest.mark.django_db
def test_merge_repoints_statuses(user, film):
    canonical = Film.objects.create(title="Dune Part Two", year=2024)
    status = comment(user, film, "nice")
    watchlist = Shelf.objects.get(user=user, identifier=Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=watchlist, film=film, user=user)
    film.merge_into(canonical)
    status.refresh_from_db()
    assert status.film_id == canonical.id
    assert Status.objects.count() == 1


@pytest.mark.django_db
def test_a_film_with_statuses_cannot_be_deleted(user, film):
    comment(user, film)
    with pytest.raises(ProtectedError):
        film.delete()
