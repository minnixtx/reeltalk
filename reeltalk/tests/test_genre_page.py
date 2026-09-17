"""Genre subfeed tests (M6 artwork sub-increment C, R64).

A "Popular Genres" pill opens ``/genre/<slug>/``: the instance's live reviews of
films carrying that genre, newest first, mirrors included. Only genres somebody
has reviewed resolve — anything else 404s, so a pill never leads to an empty
page. R55/R56 read-side rules hide blocked films and blocked authors from a
signed-in viewer; anonymous visitors see everything the instance holds.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from reeltalk.core.models import Film, Status
from reeltalk.core.views import GENRE_PAGE_SIZE

User = get_user_model()


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return User.objects.create_user(localname="bob", password="s3cretpass")


def review(user, film, *, content="", age_days=0):
    status = Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW if content else Status.Type.REVIEW_RATING,
        rating=Decimal("4.00"),
        content=content,
    )
    if age_days:
        when = timezone.now() - timedelta(days=age_days)
        Status.objects.filter(pk=status.pk).update(published_date=when)
    return status


# --- Membership -------------------------------------------------------------


@pytest.mark.django_db
def test_genre_page_lists_reviews_newest_first(client, alice, bob):
    older = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    newer = Film.objects.create(
        title="The Thing", year=1982, genres=["Horror", "Sci-Fi"]
    )
    review(alice, older, content="classic", age_days=9)
    review(bob, newer, content="better")

    body = client.get("/genre/horror/").content.decode()

    assert "The Thing" in body and "Alien" in body
    assert body.index("The Thing") < body.index("Alien")
    assert f'href="/film/{newer.id}/"' in body


@pytest.mark.django_db
def test_genre_page_excludes_other_genres_comments_and_deletions(client, alice):
    horror = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    comedy = Film.objects.create(title="Airplane!", year=1980, genres=["Comedy"])
    gone = review(alice, horror)
    gone.delete()
    review(alice, comedy)
    Status.objects.create(
        user=alice, film=horror, status_type=Status.Type.COMMENT, content="boo"
    )

    assert client.get("/genre/horror/").status_code == 404


@pytest.mark.django_db
def test_genre_page_404s_for_an_unknown_slug(client, alice):
    film = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    review(alice, film)

    assert client.get("/genre/comedy/").status_code == 404
    assert client.get("/genre/horror/").status_code == 200


@pytest.mark.django_db
def test_genre_page_404s_once_its_reviews_are_gone(client, alice):
    film = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    review(alice, film).delete()

    assert client.get("/genre/horror/").status_code == 404


# --- Pagination -------------------------------------------------------------


@pytest.mark.django_db
def test_genre_page_paginates(client, db):
    films = [
        Film.objects.create(title=f"Friday the 13th {i}", genres=["Horror"])
        for i in range(GENRE_PAGE_SIZE + 1)
    ]
    for index, film in enumerate(films):
        review(User.objects.create_user(localname=f"viewer{index}"), film)

    first = client.get("/genre/horror/").content.decode()
    second = client.get("/genre/horror/?page=2").content.decode()

    assert films[-1].title in first and films[0].title not in first
    assert films[0].title in second
    assert "Page 1 of 2" in first


@pytest.mark.django_db
def test_genre_page_rejects_a_missing_page(client, alice):
    film = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    review(alice, film)

    assert client.get("/genre/horror/?page=9").status_code == 404
    assert client.get("/genre/horror/?page=oops").status_code == 200


# --- Read-side blocks (R55/R56) ---------------------------------------------


@pytest.mark.django_db
def test_genre_page_hides_blocked_users_from_a_viewer(client, alice, bob):
    film = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    review(alice, film, content="mine")
    review(bob, film, content="his")
    assert client.login(username="bob", password="s3cretpass")

    viewer = User.objects.get(localname="bob")
    viewer.blocks.add(alice)

    body = client.get("/genre/horror/").content.decode()
    assert "mine" not in body


@pytest.mark.django_db
def test_genre_page_hides_blocked_films_from_a_viewer(client, alice):
    alien = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    thing = Film.objects.create(title="The Thing", year=1982, genres=["Horror"])
    review(alice, alien)
    review(alice, thing)
    assert client.login(username="alice", password="s3cretpass")

    viewer = User.objects.get(localname="alice")
    viewer.blocked_films.add(alien)

    body = client.get("/genre/horror/").content.decode()
    assert "The Thing" in body and "Alien" not in body


@pytest.mark.django_db
def test_genre_page_is_public(client, alice):
    film = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    review(alice, film)

    assert client.get("/genre/horror/").status_code == 200
