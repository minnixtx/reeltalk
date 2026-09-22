"""The per-post page and the feed rows that open it (increment 2, R83/R84).

Three contracts here, all from the six §2A decisions:

* ``/status/<id>/`` serves a human page to browsers, for **local statuses
  and remote mirrors alike** (decision 3) — a federated review is now
  readable on this instance instead of having no page at all.
* The page is **publicly readable, anonymously** (decision 6), with the
  same per-viewer block rule as ``film_detail`` (R56).
* A feed row **links** on ``entry.status_id is not None`` and never on
  ``entry.interactive`` (R84). The mirror case is the one that pins this:
  a mirror row is deliberately non-interactive (decision 5) and must still
  be openable, so the two flags are asserted together.

Replies are rendered but empty by default — nothing writes ``reply_parent``
until increment 4 — so the reply tests hand-build the threading state.
"""

from datetime import timedelta

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
)

User = get_user_model()

AP = {"HTTP_ACCEPT": "application/activity+json"}


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return User.objects.create_user(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def admin(db):
    # R12: / redirects to /setup/ until a superuser exists, so every test
    # that reads the home page needs one — otherwise the body is an empty
    # 302 and an absence assertion on it proves nothing.
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


def _remote_user(localname: str = "carol@remote.example") -> User:
    """A remote mirror account, as federation creates it (no local password)."""
    user = User(
        localname=localname,
        local=False,
        actor_url=f"https://remote.example/users/{localname.split('@')[0]}",
        inbox_url=f"https://remote.example/users/{localname.split('@')[0]}/inbox",
    )
    user.set_unusable_password()
    user.save()
    return user


def _review(user, film, content="<p>Spice must be reviewed.</p>", rating="4.5"):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=rating,
        content=content,
        raw_content="Spice must be reviewed.",
    )


def _reply(parent, user, content, at=None):
    reply = Status.objects.create(
        user=user,
        film=parent.film,
        status_type=Status.Type.COMMENT,
        content=content,
        reply_parent=parent,
    )
    if at is not None:
        reply.published_date = at
        reply.save(update_fields=["published_date"])
    return reply


def _login(localname):
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


# --- The page itself ---------------------------------------------------------


@pytest.mark.django_db
def test_post_page_renders_the_status_in_full(alice, dune):
    status = _review(alice, dune)
    body = _login("alice").get(f"/status/{status.pk}/").content.decode()
    assert "Spice must be reviewed." in body
    assert "alice" in body
    assert "Dune (2021)" in body
    assert "width:90%" in body  # the 4.5/5 star fill


@pytest.mark.django_db
def test_post_page_is_readable_anonymously(alice, dune):
    # R83 decision 6: public like film pages (R56), not members-only.
    status = _review(alice, dune)
    response = Client().get(f"/status/{status.pk}/")
    assert response.status_code == 200
    assert "Spice must be reviewed." in response.content.decode()


@pytest.mark.django_db
def test_post_page_serves_a_remote_mirror_to_browsers(dune):
    # R83 decision 3: the HTML arm drops the local=True filter, which is
    # what makes a federated review resolvable here at all.
    carol = _remote_user()
    mirror = Status.objects.create(
        user=carol,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Their review of Dune.</p>",
        local=False,
        remote_url="https://remote.example/status/55",
    )
    response = Client().get(f"/status/{mirror.pk}/")
    assert response.status_code == 200
    assert "Their review of Dune." in response.content.decode()
    assert "text/html" in response["Content-Type"]


@pytest.mark.django_db
def test_post_page_404s_for_a_deleted_status(alice, dune):
    status = _review(alice, dune)
    status.delete()
    assert Client().get(f"/status/{status.pk}/").status_code == 404
    assert Client().get(f"/status/{status.pk}/", **AP).status_code == 404


@pytest.mark.django_db
def test_post_page_404s_for_an_unknown_id(db):
    assert Client().get("/status/424242/").status_code == 404


# --- Block-aware rendering ---------------------------------------------------


@pytest.mark.django_db
def test_post_page_is_hidden_from_a_viewer_who_blocked_the_author(alice, bob, dune):
    status = _review(alice, dune)
    bob.blocks.add(alice)
    assert _login("bob").get(f"/status/{status.pk}/").status_code == 404


@pytest.mark.django_db
def test_post_page_still_visible_to_others_when_the_author_is_blocked(alice, bob, dune):
    # Blocking is per-viewer state (R56): it removes the page for the
    # blocker only, never for everyone else or for anonymous readers.
    status = _review(alice, dune)
    bob.blocks.add(alice)
    assert Client().get(f"/status/{status.pk}/").status_code == 200
    User.objects.create_user(localname="dave", password="s3cretpass")
    assert _login("dave").get(f"/status/{status.pk}/").status_code == 200


@pytest.mark.django_db
def test_blocking_is_one_way_the_author_still_sees_their_own_post(alice, bob, dune):
    status = _review(alice, dune)
    bob.blocks.add(alice)
    assert _login("alice").get(f"/status/{status.pk}/").status_code == 200


@pytest.mark.django_db
def test_post_page_hides_replies_from_users_the_viewer_blocked(alice, bob, dune):
    status = _review(alice, dune)
    _reply(status, bob, "<p>Bob weighed in.</p>")
    alice.blocks.add(bob)
    body = _login("alice").get(f"/status/{status.pk}/").content.decode()
    assert "Bob weighed in." not in body
    assert "Replies (0)" in body


# --- The reply list ----------------------------------------------------------


@pytest.mark.django_db
def test_post_page_shows_the_empty_reply_state(alice, dune):
    # Nothing writes reply_parent until increment 4; the page says so plainly
    # rather than rendering a hole where the thread will go.
    status = _review(alice, dune)
    body = _login("alice").get(f"/status/{status.pk}/").content.decode()
    assert "Replies (0)" in body
    assert "No replies yet." in body


@pytest.mark.django_db
def test_post_page_lists_replies_oldest_first(alice, bob, dune):
    status = _review(alice, dune)
    now = timezone.now()
    _reply(status, bob, "<p>First.</p>", at=now - timedelta(minutes=30))
    _reply(status, alice, "<p>Second.</p>", at=now - timedelta(minutes=10))
    body = _login("alice").get(f"/status/{status.pk}/").content.decode()
    assert "Replies (2)" in body
    assert body.index("First.") < body.index("Second.")


@pytest.mark.django_db
def test_post_page_excludes_deleted_replies(alice, bob, dune):
    status = _review(alice, dune)
    reply = _reply(status, bob, "<p>Take it back.</p>")
    reply.delete()
    body = _login("alice").get(f"/status/{status.pk}/").content.decode()
    assert "Take it back." not in body
    assert "Replies (0)" in body


# --- Feed rows open the page (R84) -------------------------------------------


def _home(client) -> str:
    """The rendered home feed.

    Asserting 200 here is load-bearing: without a superuser ``/`` answers an
    empty 302 to the setup wizard (R12), and an absence assertion against
    that body passes whether or not the template is correct.
    """
    response = client.get("/")
    assert response.status_code == 200
    return response.content.decode()


@pytest.mark.django_db
def test_folded_review_row_links_to_its_post_page(alice, dune, admin):
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    body = _home(_login("alice"))
    assert "Desert planet." in body
    assert f'href="/status/{review.pk}/"' in body


@pytest.mark.django_db
def test_standalone_comment_row_links_to_its_post_page(alice, dune, admin):
    comment = Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.COMMENT,
        content="<p>Idle thought.</p>",
    )
    body = _home(_login("alice"))
    assert "Idle thought." in body
    assert f'href="/status/{comment.pk}/"' in body


@pytest.mark.django_db
def test_remote_mirror_row_links_to_the_post_page_though_it_is_not_interactive(
    alice, dune, admin
):
    # The R84 guard. Gating the link on ``interactive`` would make every
    # mirror unclickable and undo decision 3, because decision 5 withholds
    # the *control* from a mirror — not the *page*.
    carol = _remote_user()
    alice.follows.add(carol)
    mirror = Status.objects.create(
        user=carol,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Their review.</p>",
        local=False,
        remote_url="https://remote.example/status/77",
    )
    entry = next(e for e in feed_entries(alice) if e.user == carol)
    assert entry.interactive is False
    assert entry.status_id == mirror.pk
    body = _home(_login("alice"))
    assert "Their review." in body
    assert f'href="/status/{mirror.pk}/"' in body


@pytest.mark.django_db
def test_bare_shelf_rows_do_not_link_to_a_post_page(alice, dune, admin):
    shelve_to_watchlist(alice, dune)
    assert all(e.status_id is None for e in feed_entries(alice))
    body = _home(_login("alice"))
    assert "to their Watchlist" in body  # the row really rendered…
    assert "/status/" not in body  # …and really carries no link


@pytest.mark.django_db
def test_bulk_aggregate_row_does_not_link_to_a_post_page(alice, admin):
    shelf = Shelf.objects.get(user=alice, identifier=Shelf.TO_READ)
    for i in range(3):
        # Explicit through rows: the M2M .add() bulk-insert bypasses
        # ShelfFilm.save() and its actor default (R14).
        ShelfFilm.objects.create(
            shelf=shelf,
            film=Film.objects.create(title=f"Bulk {i}", year=2000 + i),
            user=alice,
        )
    entries = feed_entries(alice)
    assert len(entries) == 1 and entries[0].status_id is None
    body = _home(_login("alice"))
    assert "2 other films" in body  # the aggregate really rendered…
    assert "/status/" not in body  # …and really carries no link
