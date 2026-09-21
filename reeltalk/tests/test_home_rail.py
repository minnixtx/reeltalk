"""Home rail tests (M6 artwork sub-increment C, R61/R64).

"Trending Films" counts live reviews inside a strict 30-day window — written
reviews and rating-only entries alike (owner-confirmed) — and "Popular Genres"
covers only genres somebody has reviewed, so every pill leads to content. The
rail is visible to everyone but followable only by members (R81) — a stranger
sees the titles and genres as plain text, not links. The feed pane stays
signed-in and now opens with "Now Playing".
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from reeltalk.core.models import (
    TRENDING_LIMIT,
    Film,
    Status,
    popular_genres,
    trending_films,
)
from reeltalk.social.models import SiteSettings

User = get_user_model()


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def admin(db):
    # R12: / is gated behind the setup wizard until a superuser exists.
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return User.objects.create_user(localname="bob", password="s3cretpass")


def review(user, film, *, content="", rating=Decimal("4.00"), age_days=0):
    """A live D5 review — written when text is given, rating-only otherwise."""
    status = Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW if content else Status.Type.REVIEW_RATING,
        rating=rating,
        content=content,
    )
    if age_days:
        when = timezone.now() - timedelta(days=age_days)
        Status.objects.filter(pk=status.pk).update(published_date=when)
    return status


def comment(user, film):
    return Status.objects.create(
        user=user, film=film, status_type=Status.Type.COMMENT, content="hmm"
    )


# --- Trending Films (R61) ----------------------------------------------------


@pytest.mark.django_db
def test_trending_counts_written_and_rating_only_reviews(alice, bob):
    hot = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    cold = Film.objects.create(title="Blade Runner", year=1982, genres=["Sci-Fi"])
    review(alice, hot, content="masterpiece")
    review(bob, hot, rating=Decimal("3.00"))  # star-only counts too (R61)
    review(alice, cold)

    rows = trending_films()

    assert [(row.film.title, row.review_count) for row in rows] == [
        ("Alien", 2),
        ("Blade Runner", 1),
    ]


@pytest.mark.django_db
def test_trending_breaks_ties_on_the_newest_review(alice, bob):
    older = Film.objects.create(title="Older Buzz")
    newer = Film.objects.create(title="Newer Buzz")
    review(alice, older, age_days=10)
    review(bob, older, age_days=25)
    review(alice, newer, age_days=24)
    review(bob, newer, age_days=2)

    # Both carry two reviews — the one with the newest review leads.
    assert [row.film.title for row in trending_films()] == ["Newer Buzz", "Older Buzz"]


@pytest.mark.django_db
def test_trending_window_is_strict(alice):
    inside = Film.objects.create(title="Inside")
    outside = Film.objects.create(title="Outside")
    review(alice, inside, age_days=29)
    review(alice, outside, age_days=31)

    assert [row.film.title for row in trending_films()] == ["Inside"]


@pytest.mark.django_db
def test_trending_ignores_deleted_reviews_and_comments(alice):
    commented = Film.objects.create(title="Only Commented")
    deleted_on = Film.objects.create(title="Deleted Review")
    comment(alice, commented)
    review(alice, deleted_on).delete()

    assert trending_films() == []


@pytest.mark.django_db
def test_trending_respects_the_limit(alice):
    for index in range(TRENDING_LIMIT + 3):
        review(alice, Film.objects.create(title=f"Film {index}"))

    assert len(trending_films()) == TRENDING_LIMIT


# --- Popular Genres (R64) ----------------------------------------------------


@pytest.mark.django_db
def test_genres_cover_reviewed_films_only(alice):
    reviewed = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    Film.objects.create(title="Some Import", genres=["Horror", "Romance"])
    review(alice, reviewed)

    names = [genre.name for genre in popular_genres()]

    # Romance sits on an unreviewed film only — no pill, so no empty subfeed.
    assert names == ["Horror"]


@pytest.mark.django_db
def test_genres_rank_by_review_count_with_slugs(alice, bob):
    horror = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    scifi = Film.objects.create(title="Arrival", year=2016, genres=["Science Fiction"])
    review(alice, horror)
    review(bob, horror, content="scarier than expected")
    review(alice, scifi)

    rows = popular_genres()

    assert [(row.name, row.slug, row.review_count) for row in rows] == [
        ("Horror", "horror", 2),
        ("Science Fiction", "science-fiction", 1),
    ]


@pytest.mark.django_db
def test_genres_empty_without_reviews(alice):
    Film.objects.create(title="Shelved Only", genres=["Horror"])
    assert popular_genres() == []


# --- The home page itself ----------------------------------------------------


@pytest.mark.django_db
def test_anonymous_home_shows_the_rail_and_a_cta(client, admin):
    site = SiteSettings.get_instance()
    site.name = "My Film Club"
    site.save()
    film = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    review(alice, film)

    body = client.get("/").content.decode()

    # Each ticket graphic carries its panel title; the cards repeat it only for
    # assistive tech.
    assert "movie-ticket.png" in body
    assert 'class="visually-hidden">Trending Films' in body
    assert "popular-genres.png" in body
    assert 'class="visually-hidden">Popular Genres' in body
    # R81: for a stranger the rail is a showcase, not a doorway. The titles
    # and genres still render — that is what sells the instance — but nothing
    # in it can be followed.
    assert "Alien" in body
    assert "Horror" in body
    assert f'href="/film/{film.id}/"' not in body
    assert 'href="/genre/horror/"' not in body
    # No feed for anonymous visitors — the sign-up CTA holds its pane instead.
    assert "Now Playing" not in body
    assert 'href="/signup/"' in body


@pytest.mark.django_db
def test_anonymous_rail_rows_are_spans_not_anchors(client, admin):
    """Non-clickable means no anchor at all, not a link with a dead href.

    A styled-but-disabled ``<a>`` still takes a tab stop and still shows the
    target in the status bar; a ``<span>`` takes neither, which is the point.
    """
    film = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    review(alice, film)

    body = client.get("/").content.decode()

    assert '<span class="trending-film">' in body
    assert '<span class="genre-pill"' in body
    assert '<a class="trending-film"' not in body
    assert '<a class="genre-pill"' not in body


@pytest.mark.django_db
def test_signed_in_rail_links_to_films_and_genres(client, admin, alice):
    """The other half of the same switch, so the spans above aren't vacuous.

    Without this the rail could satisfy the anonymous assertions by never
    rendering a link for anybody.
    """
    film = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    review(alice, film)
    assert client.login(username="alice", password="s3cretpass")

    body = client.get("/").content.decode()

    assert f'<a class="trending-film" href="/film/{film.id}/"' in body
    assert '<a class="genre-pill" href="/genre/horror/"' in body


@pytest.mark.django_db
def test_signed_in_home_leads_with_now_playing(client, admin, alice):
    Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    assert client.login(username="alice", password="s3cretpass")

    body = client.get("/").content.decode()

    assert '<h1 class="pane-title">Now Playing</h1>' in body
    assert 'class="home-grid"' in body
    # The header wordmark covers the brand, so the landing intro is gone (R64).
    assert "landing-desc" not in body
    assert "Signed in as" not in body


@pytest.mark.django_db
def test_home_rail_survives_an_empty_instance(client, admin):
    body = client.get("/").content.decode()
    assert "No reviews in the last 30 days." in body
    assert "Genres appear once films start getting reviews." in body
