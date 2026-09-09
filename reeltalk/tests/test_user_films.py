"""User films page + home feed tests (M1 increment 6, R33).

The films page (PLAN.md §3.3 rule 1, D1): the User query API
(films_on_shelf / all_films with the rating annotation) and the view —
exactly three tabs, All films / Watchlist / Watched, tab filtering, public
readability. The home feed (§3.6/§3.7 v0.1): Status.feed_for (own + followed
users' statuses, newest first, no deleted), and the shelf events of R33 —
feed_entries derives "added to Watchlist" / "watched" entries from the
members' ShelfFilm rows, folding a watched film's D5 review into its watched
entry (R35).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from reeltalk.core.models import (
    Film,
    Shelf,
    ShelfFilm,
    Status,
    feed_entries,
    mark_watched,
    shelve_to_watchlist,
    unshelve_from_watchlist,
)

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


@pytest.fixture
def admin(db):
    # R12: / is gated behind the setup wizard until a superuser exists, so
    # home-page tests need one in place.
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


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


# --- minimal home feed (§3.6/§3.7 v0.1) ---------------------------------------


@pytest.mark.django_db
def test_feed_for_includes_own_and_followed(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    bob = User.objects.create_user(localname="bob", password="s3cretpass")
    carol = User.objects.create_user(localname="carol", password="s3cretpass")
    alice.follows.add(bob)
    dune = Film.objects.create(title="Dune", year=2021)
    blade = Film.objects.create(title="Blade Runner", year=1982)
    arrival = Film.objects.create(title="Arrival", year=2016)
    own = Status.objects.create(
        user=alice, film=dune, status_type=Status.Type.REVIEW_RATING, rating="4"
    )
    followed = Status.objects.create(
        user=bob,
        film=blade,
        status_type=Status.Type.REVIEW,
        rating="3",
        content="<p>Neo-noir.</p>",
    )
    stranger = Status.objects.create(
        user=carol, film=arrival, status_type=Status.Type.REVIEW_RATING, rating="5"
    )
    feed_ids = {s.id for s in Status.feed_for(alice)}
    assert feed_ids == {own.id, followed.id}
    assert stranger.id not in feed_ids


@pytest.mark.django_db
def test_feed_for_orders_newest_first(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    film = Film.objects.create(title="Dune", year=2021)
    old = Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW_RATING,
        rating="4",
        published_date=timezone.now() - timedelta(days=1),
    )
    new = Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.COMMENT,
        content="<p>Second thought.</p>",
    )
    feed = list(Status.feed_for(alice))
    assert [s.id for s in feed] == [new.id, old.id]


@pytest.mark.django_db
def test_feed_for_excludes_deleted(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    bob = User.objects.create_user(localname="bob", password="s3cretpass")
    alice.follows.add(bob)
    film = Film.objects.create(title="Dune", year=2021)
    entry = Status.objects.create(
        user=bob, film=film, status_type=Status.Type.REVIEW_RATING, rating="3"
    )
    assert {s.id for s in Status.feed_for(alice)} == {entry.id}
    entry.delete()  # soft — tombstones don't reach the feed
    assert list(Status.feed_for(alice)) == []


@pytest.mark.django_db
def test_home_feed_shows_own_and_followed(login, user, film, admin):
    bob = User.objects.create_user(localname="bob", password="s3cretpass")
    user.follows.add(bob)
    mark_watched(user, film, rating="4.5")  # alice's own rating-only entry
    other = Film.objects.create(title="Blade Runner", year=1982)
    Status.objects.create(
        user=bob,
        film=other,
        status_type=Status.Type.REVIEW,
        rating="3",
        content="<p>Neo-noir classic.</p>",
    )
    body = login.get("/").content.decode()
    assert "Dune" in body  # own entry with its film link
    assert "Neo-noir classic." in body  # followed user's review


@pytest.mark.django_db
def test_home_feed_excludes_strangers(login, user, film, admin):
    carol = User.objects.create_user(localname="carol", password="s3cretpass")
    Status.objects.create(
        user=carol, film=film, status_type=Status.Type.REVIEW_RATING, rating="5"
    )
    body = login.get("/").content.decode()
    assert "carol" not in body


@pytest.mark.django_db
def test_home_anonymous_has_no_feed(client, admin):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'class="feed"' not in body
    assert "Sign up" in body


# --- Home feed with shelf events (R33; shape per R35) -------------------------


@pytest.mark.django_db
def test_feed_entries_shows_watchlist_addition(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    dune = Film.objects.create(title="Dune", year=2021)
    assert shelve_to_watchlist(alice, dune) == "added"
    entries = feed_entries(alice)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "watchlist"
    assert entry.user == alice
    assert entry.film == dune
    assert entry.rating is None
    assert entry.content == ""


@pytest.mark.django_db
def test_feed_entries_watched_carries_the_review(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    dune = Film.objects.create(title="Dune", year=2021)
    mark_watched(
        alice,
        dune,
        rating="4.5",
        content="<p>Desert planet.</p>",
        raw_content="Desert planet.",
    )
    # The D5 review rides on the watched entry — one row, not two.
    entries = feed_entries(alice)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "watched"
    assert entry.rating == Decimal("4.5")
    assert entry.content == "<p>Desert planet.</p>"


@pytest.mark.django_db
def test_feed_entries_watched_without_review_is_bare(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    dune = Film.objects.create(title="Dune", year=2021)
    mark_watched(alice, dune, rating="3")
    Status.objects.get(user=alice, film=dune).delete()  # soft delete
    entries = feed_entries(alice)
    assert len(entries) == 1
    assert entries[0].kind == "watched"
    assert entries[0].rating is None
    assert entries[0].content == ""


@pytest.mark.django_db
def test_feed_entries_membership_own_plus_followed(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    bob = User.objects.create_user(localname="bob", password="s3cretpass")
    carol = User.objects.create_user(localname="carol", password="s3cretpass")
    alice.follows.add(bob)
    dune = Film.objects.create(title="Dune", year=2021)
    blade = Film.objects.create(title="Blade Runner", year=1982)
    arrival = Film.objects.create(title="Arrival", year=2016)
    shelve_to_watchlist(alice, dune)
    mark_watched(bob, blade, rating="4")
    shelve_to_watchlist(carol, arrival)  # a stranger's event — excluded
    got = {(e.user.localname, e.kind) for e in feed_entries(alice)}
    assert got == {("alice", "watchlist"), ("bob", "watched")}


@pytest.mark.django_db
def test_feed_entries_orders_newest_first_across_kinds(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    dune = Film.objects.create(title="Dune", year=2021)
    blade = Film.objects.create(title="Blade Runner", year=1982)
    to_read = Shelf.objects.get(user=alice, identifier=Shelf.TO_READ)
    ShelfFilm.objects.create(
        shelf=to_read,
        film=dune,
        user=alice,
        shelved_date=timezone.now() - timedelta(days=1),
    )
    mark_watched(alice, blade, rating="4")  # now — newer than the watchlist row
    entries = feed_entries(alice)
    assert [(e.kind, e.film.title) for e in entries] == [
        ("watched", "Blade Runner"),
        ("watchlist", "Dune"),
    ]


@pytest.mark.django_db
def test_feed_entries_watchlist_to_watched_transition(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    dune = Film.objects.create(title="Dune", year=2021)
    shelve_to_watchlist(alice, dune)
    assert {e.kind for e in feed_entries(alice)} == {"watchlist"}
    mark_watched(alice, dune, rating="4")  # D1: off Watchlist, onto Watched
    entries = feed_entries(alice)
    assert len(entries) == 1
    assert entries[0].kind == "watched"


@pytest.mark.django_db
def test_feed_entries_unshelve_removes_the_event(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    dune = Film.objects.create(title="Dune", year=2021)
    shelve_to_watchlist(alice, dune)
    assert unshelve_from_watchlist(alice, dune) is True
    assert feed_entries(alice) == []


@pytest.mark.django_db
def test_feed_entries_review_without_shelf_row_stands_alone(db):
    # Not reachable locally in v0.1 (mark_watched always shelves); remote
    # mirrors may bring it — the review still shows as its own entry.
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    dune = Film.objects.create(title="Dune", year=2021)
    Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Heard about it.</p>",
    )
    entries = feed_entries(alice)
    assert len(entries) == 1
    assert entries[0].kind == "status"
    assert entries[0].content == "<p>Heard about it.</p>"


@pytest.mark.django_db
def test_feed_entries_comment_on_watched_film_stays_separate(db):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    dune = Film.objects.create(title="Dune", year=2021)
    mark_watched(alice, dune, rating="4")
    Status.objects.create(
        user=alice, film=dune, status_type=Status.Type.COMMENT, content="<p>Update.</p>"
    )
    kinds = sorted(e.kind for e in feed_entries(alice))
    assert kinds == ["status", "watched"]


# --- Home page with shelf events ----------------------------------------------


@pytest.mark.django_db
def test_home_feed_renders_watchlist_event(login, user, film, admin):
    shelve_to_watchlist(user, film)
    body = login.get("/").content.decode()
    assert "added" in body
    assert "to their Watchlist" in body


@pytest.mark.django_db
def test_home_feed_renders_watched_event_with_review(login, user, film, admin):
    mark_watched(
        user,
        film,
        rating="4.5",
        content="<p>Desert planet.</p>",
        raw_content="Desert planet.",
    )
    body = login.get("/").content.decode()
    assert "watched" in body
    assert "width:90%" in body  # the 4.5/5 star fill
    assert "Desert planet." in body


@pytest.mark.django_db
def test_home_feed_watched_review_is_a_single_row(login, user, film, admin):
    mark_watched(user, film, rating="4", content="<p>Once.</p>", raw_content="Once.")
    body = login.get("/").content.decode()
    assert body.count("<p>Once.</p>") == 1
