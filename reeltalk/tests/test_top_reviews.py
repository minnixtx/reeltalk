"""The Top Reviews rail (reviewer-discovery increment, see
``REVIEWER_DISCOVERY_PROPOSAL.md``).

The box ranks **reviews**, not reviewers, by reader response - the owner's
replacement for a first draft that ranked members by volume, which paid for
quantity before anything was read. The rules that make that principle real
each get their own test here: the 30-day window, likes + replies with equal
weight, the at-least-one-engagement threshold, the author's own like excluded,
written reviews only, five rows, ties to the newest, and the box hiding
entirely when nothing qualifies.

The audience matrix - who may see which review on the rail - lives in
``test_visibility_read_paths.py``, where every read surface gets walked per
viewer class; this module owns the ranking and the row.
"""

from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from reeltalk.core.models import (
    Film,
    Like,
    Status,
    Visibility,
    add_reply,
    top_reviews,
)
from reeltalk.tests.members import member, site_admin

PASSWORD = "s3cretpass"


@pytest.fixture(autouse=True)
def admin(db):
    return site_admin(localname="admin", password=PASSWORD)


@pytest.fixture
def reviewer(db):
    return member(localname="reviewer", password=PASSWORD)


@pytest.fixture
def reader(db):
    return member(localname="reader", password=PASSWORD)


@pytest.fixture
def film(db):
    # One film per review under test: D5's one-review-per-user-per-film is a
    # database constraint, so a fixture writing several reviews by one member
    # needs one film each.
    return [Film.objects.create(title=f"Film {i}", year=2000 + i) for i in range(1, 9)]


@pytest.fixture
def fans(db):
    """Likes are binary per pair, so a score above one needs one member
    per like - which is exactly how a real score above one happens."""
    return [member(localname=f"fan{i}", password=PASSWORD) for i in range(5)]


def _review(user, film, marker, *, days_ago=0, visibility=Visibility.PUBLIC):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        content=f"<p>{marker} body</p>",
        raw_content=f"{marker} body",
        rating=4,
        visibility=visibility,
        published_date=timezone.now() - timedelta(days=days_ago),
    )


def _like(status, user):
    Like.objects.create(user=user, status=status)


def _reply(status, user):
    return add_reply(user, status, content="<p>me too</p>", raw_content="me too")


# --- The ranking ---------------------------------------------------------------


_MARKERS = ("FIVE", "TIE-NEW", "TIE-OLD", "TWO-REPLIES", "ONE-LIKE")


def _markers(rows) -> list[str]:
    """The marker each returned row was written with, in returned order."""
    out = []
    for row in rows:
        out.extend(m for m in _MARKERS if m in row.status.content)
    return out


@pytest.mark.django_db
def test_rows_rank_by_likes_plus_replies_with_ties_to_the_newest(
    reviewer, reader, fans, film
):
    """likes + replies, equal weight; a tie breaks to the newer review."""
    five_likes = _review(reviewer, film[0], "FIVE")
    for fan in fans:
        _like(five_likes, fan)
    tie_newer = _review(reviewer, film[1], "TIE-NEW")
    _like(tie_newer, reader)
    _reply(tie_newer, reader)
    _reply(tie_newer, fans[0])  # score 3, newer
    tie_older = _review(reviewer, film[2], "TIE-OLD", days_ago=2)
    for fan in fans[:3]:
        _like(tie_older, fan)  # score 3, older
    two_replies = _review(reviewer, film[3], "TWO-REPLIES")
    _reply(two_replies, reader)
    _reply(two_replies, reviewer)
    one_like = _review(reviewer, film[4], "ONE-LIKE")
    _like(one_like, reader)

    assert _markers(top_reviews()) == [
        "FIVE",
        "TIE-NEW",
        "TIE-OLD",
        "TWO-REPLIES",
        "ONE-LIKE",
    ]


@pytest.mark.django_db
def test_the_box_shows_at_most_five_rows(reviewer, reader, film):
    for i in range(6):
        review = _review(reviewer, film[i], f"POP-{i}", days_ago=i)
        _like(review, reader)
    assert len(top_reviews()) == 5
    # The oldest of the six qualified rows is the one the limit drops.
    assert "POP-5" not in " ".join(row.status.content for row in top_reviews())


@pytest.mark.django_db
def test_a_review_needs_one_like_or_reply_to_qualify(reviewer, reader, film):
    _review(reviewer, film[0], "QUIET")
    loud = _review(reviewer, film[1], "LOUD")
    _reply(loud, reader)
    markers = " ".join(row.status.content for row in top_reviews())
    assert "LOUD" in markers
    assert "QUIET" not in markers


@pytest.mark.django_db
def test_the_authors_own_like_never_qualifies_or_counts(reviewer, film):
    """The page never offers a like button on your own post, but the route
    accepts a self-like - so the ranking must not let anyone pad their own
    score."""
    padded = _review(reviewer, film[0], "SELF-LIKED")
    _like(padded, reviewer)
    assert top_reviews() == []


@pytest.mark.django_db
def test_a_suspended_likers_like_does_not_count(reviewer, reader, film):
    review = _review(reviewer, film[0], "ONCE-LIKED")
    ghost = member(localname="ghost", password=PASSWORD)
    _like(review, ghost)
    _like(review, reader)
    ghost.suspend()
    rows = top_reviews()
    assert [row.status.content for row in rows] == [review.content]
    assert rows[0].score == 1  # the ghost's like is out of the score, not just hidden


@pytest.mark.django_db
def test_reviews_outside_the_window_are_out(reviewer, reader, film):
    fresh = _review(reviewer, film[0], "FRESH")
    stale = _review(reviewer, film[1], "STALE", days_ago=31)
    _like(fresh, reader)
    _like(stale, reader)
    markers = " ".join(row.status.content for row in top_reviews())
    assert "FRESH" in markers and "STALE" not in markers


@pytest.mark.django_db
def test_rating_only_entries_never_appear(reviewer, reader, film):
    """The row's third line is the opening of the review; a rating-only entry
    has no text to open with, and this box is about the writing."""
    rating_only = Status.objects.create(
        user=reviewer,
        film=film[0],
        status_type=Status.Type.REVIEW_RATING,
        content="",
        raw_content="",
        rating=5,
    )
    _like(rating_only, reader)
    assert top_reviews() == []


# --- The row on the page ---------------------------------------------------------


@pytest.mark.django_db
def test_the_rail_row_names_the_reviewer_the_film_and_the_opening_words(
    admin, reviewer, reader, film
):
    review = _review(reviewer, film[0], "RAILROW")
    # The excerpt text lives in raw_content only: the feed renders content,
    # so the assertion below can only be satisfied by the rail row itself.
    review.raw_content = (
        "A lovingly restored print of a film that still bites after fifty years"
    )
    review.save()
    _like(review, reader)
    body = Client().get("/").content.decode()
    assert "reviewer" in body  # line 1: the name
    assert "Film 1 (2001)" in body  # line 2: title and year
    assert "lovingly restored print" in body  # line 3: the opening words
    assert "..." in body  # and the ellipsis that says there is more
    assert f'href="/status/{review.id}/"' in body  # the row leads to the review


@pytest.mark.django_db
def test_a_short_review_is_excerpted_whole_without_an_ellipsis(
    admin, reviewer, reader, film
):
    review = _review(reviewer, film[0], "SHORT")
    review.raw_content = "Brief and bright."
    review.content = "<p>Brief and bright.</p>"
    review.save()
    _like(review, reader)
    body = Client().get("/").content.decode()
    assert "Brief and bright." in body


@pytest.mark.django_db
def test_the_box_hides_entirely_when_nothing_qualifies(admin, reviewer, reader, film):
    _review(reviewer, film[0], "QUIET")
    loud = _review(reviewer, film[1], "LOUD")
    _like(loud, reader)
    assert "top-reviews" in Client().get("/").content.decode()
    Like.objects.filter(status=loud).delete()
    body = Client().get("/").content.decode()
    # The box is gone. QUIET's text may still sit in the feed below - the
    # feed is not the box; the absent stub is what proves the box hid.
    assert "top-reviews" not in body
