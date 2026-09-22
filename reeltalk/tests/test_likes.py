"""Likes, local only (feed interactions increment 3, R83 decision 4).

Four contracts, in the order they bite:

* **Binary, with day-one identity.** One ``Like`` row per (user, status),
  enforced at the DB, and a local like mints ``origin_id`` = its own pk on
  create (R41) so increment 5 can build an outbound ``Like`` without
  re-shaping the table. Unliking deletes the row, so a re-like mints a
  *new* identity rather than looking like a redelivery of the old one.
* **The toggle endpoint** answers JSON and refuses what the UI withholds:
  a remote mirror 404s here exactly as it shows no button on the page, so
  a hand-made request cannot write a like this instance cannot deliver.
* **The control gates on ``interactive``** — the narrow flag R84 warns
  about. A mirror row keeps its link to the post page and gets no control.
* **Counts are batched.** ``feed_entries`` returns a Python list, so a
  per-row count would be one query per row on every home page; the tests
  pin that the query count does not grow with the feed.

The home-page absence tests all go through ``_home``, which asserts 200
before handing back the body — without a superuser ``/`` answers an empty
302 to the setup wizard (R12) and an absence assertion on it proves
nothing.
"""

import json
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from reeltalk.core.models import (
    Film,
    Like,
    Shelf,
    ShelfFilm,
    Status,
    feed_entries,
    like_counts,
    liked_ids,
    mark_watched,
    shelve_to_watchlist,
    toggle_like,
)

User = get_user_model()


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
    # R12: / redirects to /setup/ until a superuser exists.
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


def _review(user, film, content="<p>Spice must be liked.</p>", rating="4.5"):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=rating,
        content=content,
        raw_content="Spice must be liked.",
    )


def _mirror(dune, localname="carol@remote.example"):
    carol = _remote_user(localname)
    status = Status.objects.create(
        user=carol,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Their review of Dune.</p>",
        local=False,
        remote_url=f"https://remote.example/status/{localname}",
    )
    return carol, status


def _login(localname):
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


def _home(client) -> str:
    """The rendered home feed, with the 200 asserted before the body is used."""
    response = client.get("/")
    assert response.status_code == 200
    return response.content.decode()


# --- The model: binary, with day-one AP identity -----------------------------


@pytest.mark.django_db
def test_like_mints_its_own_origin_id_on_create(alice, dune):
    # R41/R42 day-one discipline: the pk is the origin id, set at create,
    # so increment 5 has a stable identity to build the outbound Like from.
    like = Like.objects.create(user=alice, status=_review(alice, dune))
    assert like.origin_id == like.pk


@pytest.mark.django_db
def test_a_remote_users_like_is_not_minted_an_origin_id(dune):
    # The gate is the author's locality, not the row's existence: a like
    # that arrived from another instance carries its home's identity, not
    # one we minted here.
    carol, status = _mirror(dune)
    like = Like.objects.create(user=carol, status=status)
    assert like.origin_id is None


@pytest.mark.django_db
def test_unique_constraint_blocks_a_second_like_from_the_same_user(alice, dune):
    # Decision 4: one Like/Favourite state per user per Status — the DB,
    # not the view, is what makes a second one impossible.
    status = _review(alice, dune)
    Like.objects.create(user=alice, status=status)
    with pytest.raises(IntegrityError):
        Like.objects.create(user=alice, status=status)


@pytest.mark.django_db
def test_two_users_can_like_the_same_status(alice, bob, dune):
    status = _review(alice, dune)
    Like.objects.create(user=alice, status=status)
    Like.objects.create(user=bob, status=status)
    assert Like.objects.filter(status=status).count() == 2


@pytest.mark.django_db
def test_reliking_mints_a_new_identity(alice, dune):
    # Delete-then-create is what keeps a re-like from looking like a
    # redelivery of the first one (objects.py:update_activity dedups on
    # the activity id), so the second like must be a different object.
    status = _review(alice, dune)
    assert toggle_like(alice, status) is True
    first_pk = Like.objects.get(user=alice, status=status).pk
    assert toggle_like(alice, status) is False
    assert toggle_like(alice, status) is True
    second_pk = Like.objects.get(user=alice, status=status).pk
    assert Like.objects.filter(user=alice, status=status).count() == 1
    assert first_pk != second_pk


# --- The toggle --------------------------------------------------------------


@pytest.mark.django_db
def test_toggle_like_turns_it_on_then_off(alice, dune):
    status = _review(alice, dune)
    assert toggle_like(alice, status) is True
    assert Like.objects.filter(user=alice, status=status).count() == 1
    assert toggle_like(alice, status) is False
    assert Like.objects.filter(user=alice, status=status).count() == 0


@pytest.mark.django_db
def test_toggle_survives_a_concurrent_like_instead_of_erroring(alice, dune):
    # The double-click guard. The delete reports nothing removed while a
    # row is in fact there — the interleaving two racing requests produce —
    # so the create hits the unique constraint and the toggle still
    # answers "liked" rather than 500ing. Patched on the manager rather
    # than on a queryset built here: toggle_like builds its own, and a
    # patch on a different instance would not bite at all.
    status = _review(alice, dune)
    existing = Like.objects.create(user=alice, status=status)

    class _LostTheRace:
        def delete(self):
            return 0, {}

    with mock.patch.object(Like.objects, "filter", return_value=_LostTheRace()):
        assert toggle_like(alice, status) is True
    assert Like.objects.filter(user=alice, status=status).count() == 1
    assert Like.objects.get(user=alice, status=status).pk == existing.pk


@pytest.mark.django_db
def test_toggle_reports_the_viewers_own_state_not_the_total(alice, bob, dune):
    # Bob's like does not make Alice's toggle return False.
    status = _review(alice, dune)
    Like.objects.create(user=bob, status=status)
    assert toggle_like(alice, status) is True
    assert toggle_like(alice, status) is False


# --- The batched count -------------------------------------------------------


@pytest.mark.django_db
def test_like_counts_group_by_status(alice, bob, dune):
    first = _review(alice, dune)
    second = Status.objects.create(
        user=alice,
        film=Film.objects.create(title="Dune Part Two", year=2024),
        status_type=Status.Type.REVIEW,
        content="<p>Second.</p>",
    )
    Like.objects.create(user=bob, status=first)
    got = like_counts([first.pk, second.pk])
    assert got == {first.pk: 1}  # a zero-like status is absent, not zero


@pytest.mark.django_db
def test_like_counts_of_no_statuses_is_empty(db):
    assert like_counts([]) == {}


@pytest.mark.django_db
def test_like_counts_are_the_same_for_every_viewer(alice, bob, dune):
    # The home-rail rule from ``index``: a blocked user's like still counts
    # toward the tally; only the lists hide their rows.
    status = _review(alice, dune)
    Like.objects.create(user=bob, status=status)
    bob.blocks.add(alice)
    assert like_counts([status.pk]) == {status.pk: 1}
    assert bob.pk not in liked_ids(alice, [status.pk])


@pytest.mark.django_db
def test_liked_ids_is_empty_for_an_anonymous_caller(alice, dune):
    # The helper guards its own boundary: an AnonymousUser has no likes,
    # and must not turn into an unfiltered query over everyone's.
    status = _review(alice, dune)
    Like.objects.create(user=alice, status=status)
    assert liked_ids(AnonymousUser(), [status.pk]) == set()
    assert liked_ids(alice, [status.pk]) == {status.pk}


@pytest.mark.django_db
def test_feed_like_queries_do_not_grow_with_the_feed(alice):
    # The N+1 the brief names: feed_entries is a Python list, so the like
    # state must arrive in a fixed number of queries. Two interactive rows
    # and eight must cost the same number of queries against core_like.
    # Each call tops the library up to `total` rather than adding a fresh
    # batch, so the second measurement sees a bigger feed and not a
    # different one.
    def like_queries(total):
        made = Film.objects.filter(title__startswith="Feed like ").count()
        for i in range(made, total):
            film = Film.objects.create(title=f"Feed like {i}", year=2000 + i)
            mark_watched(alice, film, rating="4", content=f"<p>Review {i}</p>")
        with CaptureQueriesContext(connection) as ctx:
            entries = feed_entries(alice)
        assert len(entries) == total
        return sum(1 for q in ctx.captured_queries if "core_like" in q["sql"].lower())

    small = like_queries(2)
    large = like_queries(8)
    assert small > 0, "the batched like queries never ran — the test is vacuous"
    assert small == large


@pytest.mark.django_db
def test_feed_entries_carry_the_like_state(alice, bob, dune):
    status = _review(alice, dune)
    Like.objects.create(user=bob, status=status)
    bob.follows.add(alice)  # the feed rule: alice is in bob's members
    entry = next(e for e in feed_entries(bob) if e.status_id == status.pk)
    assert entry.like_count == 1
    assert entry.liked_by_viewer is True
    other = next(e for e in feed_entries(alice) if e.status_id == status.pk)
    assert other.like_count == 1
    assert other.liked_by_viewer is False


# --- The endpoint ------------------------------------------------------------


@pytest.mark.django_db
def test_like_endpoint_requires_login(alice, dune):
    status = _review(alice, dune)
    response = Client().post(f"/status/{status.pk}/like/")
    assert response.status_code == 302
    assert "/login/" in response.url


@pytest.mark.django_db
def test_like_endpoint_is_post_only(alice, dune):
    status = _review(alice, dune)
    assert _login("alice").get(f"/status/{status.pk}/like/").status_code == 405


@pytest.mark.django_db
def test_like_endpoint_toggles_and_answers_json(alice, dune):
    status = _review(alice, dune)
    client = _login("alice")
    first = client.post(f"/status/{status.pk}/like/")
    assert first["Content-Type"] == "application/json"
    assert json.loads(first.content) == {"liked": True, "count": 1}
    second = client.post(f"/status/{status.pk}/like/")
    assert json.loads(second.content) == {"liked": False, "count": 0}


@pytest.mark.django_db
def test_like_endpoint_reports_the_total_not_just_the_callers_state(alice, bob, dune):
    status = _review(alice, dune)
    Like.objects.create(user=bob, status=status)
    body = json.loads(_login("alice").post(f"/status/{status.pk}/like/").content)
    assert body == {"liked": True, "count": 2}


@pytest.mark.django_db
def test_like_endpoint_404s_for_a_remote_mirror(dune):
    # Decision 5 enforced server-side as well as in the template: the
    # control is hidden on a mirror, and the route refuses one too, so a
    # hand-made request cannot write a like this instance cannot deliver.
    _, mirror = _mirror(dune)
    User.objects.create_user(localname="dave", password="s3cretpass")
    assert _login("dave").post(f"/status/{mirror.pk}/like/").status_code == 404
    assert Like.objects.count() == 0


@pytest.mark.django_db
def test_like_endpoint_404s_for_a_deleted_status(alice, dune):
    status = _review(alice, dune)
    status.delete()
    assert _login("alice").post(f"/status/{status.pk}/like/").status_code == 404


@pytest.mark.django_db
def test_like_endpoint_404s_for_an_unknown_id(alice):
    assert _login("alice").post("/status/999999/like/").status_code == 404


@pytest.mark.django_db
def test_like_endpoint_needs_a_csrf_token(alice, dune):
    status = _review(alice, dune)
    client = Client(enforce_csrf_checks=True)
    assert client.login(username="alice", password="s3cretpass")
    assert client.post(f"/status/{status.pk}/like/").status_code == 403


# --- The feed row ------------------------------------------------------------


@pytest.mark.django_db
def test_folded_review_row_carries_a_like_control_for_the_review(alice, dune, admin):
    # The case increment 1 warned about: the row reads as a shelf event
    # and must still like the review underneath it.
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    body = _home(_login("alice"))
    assert f'class="like-btn" data-url="/status/{review.pk}/like/"' in body


@pytest.mark.django_db
def test_liking_a_folded_row_likes_the_review_not_the_shelf_event(alice, dune, admin):
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    _login("alice").post(f"/status/{review.pk}/like/")
    like = Like.objects.get(user=alice)
    assert like.status_id == review.pk
    assert like.status.film_id == dune.id


@pytest.mark.django_db
def test_feed_row_shows_the_like_count(alice, bob, dune, admin):
    status = _review(alice, dune)
    Like.objects.create(user=bob, status=status)
    bob.follows.add(alice)  # otherwise alice's review is not in bob's feed
    body = _home(_login("bob"))
    assert "Liked" in body  # bob's own state…
    assert 'aria-pressed="true"' in body


@pytest.mark.django_db
def test_bare_shelf_row_has_no_like_control(alice, dune, admin):
    shelve_to_watchlist(alice, dune)
    body = _home(_login("alice"))
    assert "to their Watchlist" in body  # the row really rendered…
    assert "like-btn" not in body  # …and really carries no control


@pytest.mark.django_db
def test_bulk_aggregate_row_has_no_like_control(alice, admin):
    shelf = Shelf.objects.get(user=alice, identifier=Shelf.TO_READ)
    for i in range(3):
        ShelfFilm.objects.create(
            shelf=shelf,
            film=Film.objects.create(title=f"Bulk {i}", year=2000 + i),
            user=alice,
        )
    body = _home(_login("alice"))
    assert "2 other films" in body  # the aggregate really rendered…
    assert "like-btn" not in body  # …and really carries no control


@pytest.mark.django_db
def test_remote_mirror_row_has_no_control_but_keeps_its_link(alice, dune, admin):
    # The R84 split, with the control now actually existing: the mirror
    # loses the button and keeps the page.
    carol, mirror = _mirror(dune)
    alice.follows.add(carol)
    entry = next(e for e in feed_entries(alice) if e.user == carol)
    assert entry.interactive is False
    body = _home(_login("alice"))
    assert "Their review of Dune." in body
    assert f'href="/status/{mirror.pk}/"' in body
    assert "like-btn" not in body


# --- The post page -----------------------------------------------------------


@pytest.mark.django_db
def test_post_page_shows_the_control_and_count(alice, bob, dune):
    status = _review(alice, dune)
    Like.objects.create(user=bob, status=status)
    body = _login("alice").get(f"/status/{status.pk}/").content.decode()
    assert f'data-url="/status/{status.pk}/like/"' in body
    assert 'aria-pressed="false"' in body
    assert ">1<" in body  # the count, bob's like


@pytest.mark.django_db
def test_post_page_shows_a_liked_state_for_a_user_who_liked_it(alice, dune):
    status = _review(alice, dune)
    Like.objects.create(user=alice, status=status)
    body = _login("alice").get(f"/status/{status.pk}/").content.decode()
    assert 'aria-pressed="true"' in body
    assert "Liked" in body


@pytest.mark.django_db
def test_post_page_shows_no_control_for_a_remote_mirror(dune):
    _, mirror = _mirror(dune)
    User.objects.create_user(localname="dave", password="s3cretpass")
    body = _login("dave").get(f"/status/{mirror.pk}/").content.decode()
    assert "Their review of Dune." in body  # the page still renders…
    assert "like-btn" not in body  # …with no control


@pytest.mark.django_db
def test_post_page_shows_no_control_to_an_anonymous_visitor(alice, bob, dune):
    status = _review(alice, dune)
    Like.objects.create(user=bob, status=status)
    body = Client().get(f"/status/{status.pk}/").content.decode()
    assert "like-btn" not in body
    assert "1 like" in body  # the count is still a fact about the post


@pytest.mark.django_db
def test_post_page_shows_no_count_span_when_nothing_likes_it(alice, dune):
    status = _review(alice, dune)
    body = Client().get(f"/status/{status.pk}/").content.decode()
    assert "like-btn" not in body
    assert " like<" not in body
