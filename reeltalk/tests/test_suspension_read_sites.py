"""The read-side suspension filter (moderation arc increment 4b).

Suspend hides content and deletes nothing (R102), so the hiding is done
entirely by read filters — and a filter that was not written is
indistinguishable from one that was until someone loads the page. Almost
every test here therefore renders a real surface and looks for the
content, rather than asserting a flag.

Ordered by the chokepoints 4a enumerated, then the standalone sites, then
the deliberate **non**-filters. Each site gets its own test on purpose:
the failure mode this increment exists to prevent is a suspended account
that vanished from the feed and is still sitting on the film page, and one
combined test could pass while a single site was wide open.

Three kinds of test here look backwards and are worth naming:

* **The non-filter tests.** The moderator queue and the recipient's own
  notification ledger are deliberately *not* filtered. Asserting the
  suspended thing is still visible pins a decision, not a bug.
* **The restore tests.** Several sites assert unsuspending brings the
  content back. Suspend is reversible only because hiding is not
  deleting, and that symmetry is worth holding rather than assuming.
* **The resolver-is-not-a-filter test.** ``_resolve_profile_user`` still
  resolves a suspended account, because R102 asks the profile to *show*
  the suspended state.
"""

import pytest
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import FieldError
from django.test import Client, RequestFactory
from django.urls import reverse

from reeltalk.activitypub.identity import person_document
from reeltalk.activitypub.mirrors import resolve_known_actor, resolve_sender
from reeltalk.core.models import (
    Film,
    Status,
    like_counts,
    live_reviews,
    reply_counts,
    toggle_like,
)
from reeltalk.mentions.models import StatusMention
from reeltalk.mentions.parser import resolve_typed_handle
from reeltalk.moderation.models import Report
from reeltalk.notifications.models import Notification, notify
from reeltalk.tests.members import member, site_admin

User = get_user_model()
PASSWORD = "s3cretpass"

AP = {"HTTP_ACCEPT": "application/activity+json"}


# --- fixtures ----------------------------------------------------------


@pytest.fixture
def alice(db):
    """The member who gets suspended."""
    return member(localname="alice", password=PASSWORD)


@pytest.fixture
def viewer(db):
    """A plain member who follows alice and has content of their own."""
    return member(localname="viewer", password=PASSWORD)


@pytest.fixture
def third(db):
    return member(localname="third", password=PASSWORD)


@pytest.fixture
def mod(db):
    return member(localname="mod", password=PASSWORD, is_moderator=True)


@pytest.fixture
def siteadmin(db):
    """The instance needs a superuser before ``index`` stops redirecting to
    the first-run wizard. Which is exactly the ``has_admin()`` edge this
    increment flags rather than fixes: that check is
    ``filter(is_superuser=True).exists()`` and knows nothing about
    suspension, so a suspended-only admin would send every page to
    ``/setup/`` for the same reason these tests were doing."""
    return site_admin(localname="root", password=PASSWORD)


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def review(alice, dune):
    return Status.objects.create(
        user=alice,
        film=dune,
        status_type="review",
        content="<p>Great film</p>",
        raw_content="Great film",
    )


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


def suspended(user):
    user.suspend(reason="spam ring")
    user.refresh_from_db()
    return user


def body(resp):
    return resp.content.decode()


# --- the cost of the derived property (R102b) --------------------------


def test_filtering_on_is_active_raises_field_error():
    """The price of ``is_active`` being a property, pinned so nobody
    rediscovers it in production.

    ``AbstractBaseUser`` ships ``is_active = True`` as a plain class
    attribute, so before R102b there was no column behind it at all.
    Deriving it from ``suspended_at`` makes the *instance* correct and the
    *query* impossible. Both directions are pinned because a filter that
    silently returned everything would be worse than one that raises.
    """
    with pytest.raises(FieldError):
        list(User.objects.filter(is_active=True))
    with pytest.raises(FieldError):
        list(User.objects.filter(is_active=False))


def test_the_query_that_works_instead_is_suspended_at_isnull(alice, viewer):
    suspended(alice)
    active = list(
        User.objects.filter(suspended_at__isnull=True).values_list(
            "localname", flat=True
        )
    )
    assert "alice" not in active
    assert "viewer" in active


def test_is_active_tracks_the_state_without_a_column(alice):
    assert alice.is_active is True
    alice.suspend()
    assert alice.is_active is False
    alice.unsuspend()
    assert alice.is_active is True


def test_a_suspended_user_cannot_log_in_through_the_real_backend(db):
    """Driven through the actual auth backend, not by reading the flag."""
    user = member(localname="loginme", password=PASSWORD)
    assert authenticate(username="loginme", password=PASSWORD) is not None
    user.suspend()
    assert authenticate(username="loginme", password=PASSWORD) is None


def test_an_already_issued_session_is_cut_on_the_next_click(alice, siteadmin):
    """R102b's whole point: no session-store work, and no grace period.

    The session row is still present and still valid-looking. What cuts it
    is ``get_user()`` running ``user_can_authenticate`` on every request
    against the derived property.
    """
    client = logged_in(alice)
    assert client.get(reverse("index")).status_code == 200
    assert client.session.get("_auth_user_id") == str(alice.pk)

    alice.suspend(reason="spam ring")

    resp = client.get(reverse("notifications"))
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


def test_the_session_row_survives_the_suspend(alice, siteadmin):
    """Cut, not cleared — which is why the cutoff is per-request rather
    than a sweep, and why unsuspend restores without re-login."""
    client = logged_in(alice)
    client.get(reverse("index"))
    key = client.session.session_key
    alice.suspend()
    from django.contrib.sessions.models import Session

    assert Session.objects.filter(session_key=key).exists()


# --- chokepoint 1: feed_member_ids -------------------------------------


def test_a_suspended_account_leaves_feed_membership(alice, viewer):
    viewer.follows.add(alice)
    assert alice.pk in viewer.feed_member_ids()
    suspended(alice)
    assert alice.pk not in viewer.feed_member_ids()


def test_the_home_feed_stops_rendering_a_suspended_followees_review(
    alice, viewer, review, siteadmin
):
    viewer.follows.add(alice)
    assert "Great film" in body(logged_in(viewer).get(reverse("index")))
    suspended(alice)
    assert "Great film" not in body(logged_in(viewer).get(reverse("index")))


def test_unsuspending_puts_the_review_straight_back_in_the_feed(
    alice, viewer, review, siteadmin
):
    viewer.follows.add(alice)
    suspended(alice)
    alice.unsuspend()
    assert "Great film" in body(logged_in(viewer).get(reverse("index")))


def test_the_admin_cannot_be_suspended_so_the_lockout_state_is_unreachable(
    db,
):
    """R114 resolves the flagged ``has_admin()`` question by prevention.

    This file's flagged item was the suspension half of the same edge the
    ban file hit: ``has_admin()`` is ``filter(is_superuser=True).exists()``
    and reads nothing about suspension, so a suspended sole admin left the
    instance reading as "set up" while nobody could sign in to run it. The
    tests above have to create a superuser just to get a 200 out of
    ``index`` — the same mechanism from the other side.

    The owner settled it by removing the premise rather than picking between
    the two bad fixes: the admin can never be suspended, so no code path
    reaches the state. ``has_admin()`` is deliberately left unchanged — it
    is no longer load-bearing for reachability, because reachability is
    guaranteed upstream of it by the guard in ``User.suspend()``.
    """
    from reeltalk.social.models import AdminImmunityError
    from reeltalk.social.views import has_admin

    admin = site_admin(localname="lonely_root", password=PASSWORD)
    assert has_admin() is True
    with pytest.raises(AdminImmunityError):
        admin.suspend()
    admin.refresh_from_db()
    assert admin.suspended_at is None
    assert has_admin() is True
    assert authenticate(username="lonely_root", password=PASSWORD) is not None


def test_has_admin_still_counts_a_suspended_superuser_written_in_by_hand(db):
    """Defense in depth for a state R114 makes unreachable through code.

    ``has_admin()``'s semantics are unchanged by the rule, and a row can
    still get this way by a direct database edit on the host. Pinned so
    that anyone who hand-writes one can see what the instance will do —
    reads as configured, while that admin cannot sign in — rather than
    discovering it during an outage.
    """
    from django.utils import timezone

    from reeltalk.social.views import has_admin

    admin = site_admin(localname="lonely_root", password=PASSWORD)
    User.objects.filter(pk=admin.pk).update(suspended_at=timezone.now())
    admin.refresh_from_db()
    assert has_admin() is True
    assert authenticate(username="lonely_root", password=PASSWORD) is None


# --- chokepoint 2: live_reviews (rails, trending, genre pills) ---------


def test_live_reviews_excludes_a_suspended_authors_review(alice, review):
    assert review in list(live_reviews())
    suspended(alice)
    assert review not in list(live_reviews())


def test_the_index_rail_stops_trending_a_film_on_a_suspended_review_alone(
    alice, dune, review
):
    from reeltalk.core.models import trending_films

    assert trending_films()[0].review_count == 1
    suspended(alice)
    assert trending_films() == []


def test_unsuspending_restores_the_rail_count(alice, dune, review):
    from reeltalk.core.models import trending_films

    suspended(alice)
    alice.unsuspend()
    assert trending_films()[0].review_count == 1


def test_the_genre_pill_count_drops_a_suspended_review(alice, dune, review):
    from reeltalk.core.models import popular_genres

    before = dict(popular_genres())
    suspended(alice)
    after = dict(popular_genres())
    assert sum(before.values()) >= sum(after.values())


# --- chokepoint 3: the profile resolver is NOT a filter ----------------


def test_the_profile_resolver_still_finds_a_suspended_account(alice):
    """R102 asks for a suspended *state* on the profile, which the account
    cannot show if it does not resolve. Filtering here would have been the
    cheap chokepoint and would have silently dropped a settled decision, so
    the resolver answers "who is this" and the views decide what to show.
    """
    from reeltalk.social.views import _resolve_profile_user

    suspended(alice)
    found = _resolve_profile_user("alice")
    assert found is not None
    assert found.pk == alice.pk


def test_the_profile_page_shows_a_suspended_state_not_the_profile(alice, review):
    suspended(alice)
    page = body(Client().get(reverse("user-profile", args=["alice"])))
    assert "This account has been suspended" in page
    assert "Great film" not in page


def test_the_suspended_profile_renders_no_follow_or_block_controls(alice):
    """The suspended template has no member controls at all, so a member
    cannot act on a suspended account from the one page that shows them."""
    suspended(alice)
    page = body(Client().get(reverse("user-profile", args=["alice"])))
    assert reverse("user-follow", args=["alice"]) not in page
    assert reverse("user-block", args=["alice"]) not in page


def test_the_suspended_profile_is_visible_anonymously_too(alice):
    """Not a 404 — an anonymous visitor gets the same explanation."""
    suspended(alice)
    resp = Client().get(reverse("user-profile", args=["alice"]))
    assert resp.status_code == 200
    assert "This account has been suspended" in body(resp)


def test_the_person_document_still_serves_with_the_flag(alice):
    """A peer must be able to fetch the actor id our ``Update(Person)``
    points at. 404ing it would leave the broadcast unresolvable."""
    suspended(alice)
    resp = Client().get(reverse("user-profile", args=["alice"]), **AP)
    assert resp.status_code == 200
    assert resp.json()["type"] == "Person"
    assert resp.json()["suspended"] is True


def test_the_suspended_profile_redirects_its_films_tab(alice, review):
    suspended(alice)
    resp = Client().get(reverse("user-films", args=["alice"]))
    assert resp.status_code == 302
    assert resp["Location"] == reverse("user-profile", args=["alice"])


def test_a_suspended_account_found_at_find_user_lands_on_the_suspended_state(
    alice, viewer
):
    """``find_user``'s duplicate local lookup was collapsed into the profile
    resolver, so a suspended account resolves to the page that explains it
    rather than to one that 404s unexpectedly."""
    suspended(alice)
    resp = logged_in(viewer).post(
        reverse("find-user"), {"q": f"alice@{settings.DOMAIN}"}
    )
    assert resp.status_code == 302
    assert resp["Location"] == reverse("user-profile", args=["alice"])


# --- chokepoint 4: _local_user (collections / webfinger / inbox) ------


@pytest.mark.parametrize("route", ["ap-outbox", "ap-followers", "ap-following"])
def test_the_ap_collections_404_for_a_suspended_account(alice, route):
    assert Client().get(reverse(route, args=["alice"])).status_code == 200
    suspended(alice)
    assert Client().get(reverse(route, args=["alice"])).status_code == 404


def test_webfinger_stops_answering_for_a_suspended_account(alice, settings):
    url = reverse("webfinger") + f"?resource=acct:alice@{settings.DOMAIN}"
    assert Client().get(url).status_code == 200
    suspended(alice)
    assert Client().get(url).status_code == 404


def test_a_suspended_users_inbox_refuses_delivery(alice):
    suspended(alice)
    resp = Client().post(
        reverse("ap-inbox", args=["alice"]),
        "{}",
        content_type="application/json",
    )
    assert resp.status_code == 404


# --- chokepoint 5: _resolve_actor (inbound attribution) ---------------


def test_a_suspended_local_actor_does_not_resolve_as_a_known_actor(alice):
    request = RequestFactory().get("/")
    url = "http://testserver/user/alice/"
    assert resolve_known_actor(url, request) is not None
    suspended(alice)
    assert resolve_known_actor(url, request) is None


def test_a_suspended_sender_cannot_be_resolved_from_a_keyid(alice):
    request = RequestFactory().get("/")
    key_id = "http://testserver/user/alice/#main-key"
    assert resolve_sender(key_id, request) is not None
    suspended(alice)
    assert resolve_sender(key_id, request) is None


# --- chokepoint 6: resolve_typed_handle (mentions) -------------------


def test_a_suspended_handle_stops_resolving(alice):
    assert resolve_typed_handle("alice") is not None
    suspended(alice)
    assert resolve_typed_handle("alice") is None


def test_a_new_post_mentioning_a_suspended_user_writes_no_mention_row(
    alice, viewer, review
):
    suspended(alice)
    before = StatusMention.objects.count()
    logged_in(viewer).post(
        reverse("status-reply", args=[review.pk]),
        {"content": "look at @alice over there"},
    )
    assert StatusMention.objects.count() == before


def test_a_new_post_mentioning_an_unsuspended_user_still_writes_one(
    alice, viewer, review
):
    before = StatusMention.objects.count()
    logged_in(viewer).post(
        reverse("status-reply", args=[review.pk]),
        {"content": "look at @alice over there"},
    )
    assert StatusMention.objects.count() == before + 1


# --- chokepoint 7: notify() refuses in both directions ---------------


def test_notify_refuses_to_address_a_suspended_recipient(alice, viewer):
    suspended(alice)
    assert notify(alice, viewer, Notification.Kind.LIKE) is None


def test_notify_refuses_to_record_a_suspended_actor(alice, viewer):
    suspended(alice)
    assert notify(viewer, alice, Notification.Kind.LIKE) is None


def test_notify_still_works_when_neither_side_is_suspended(viewer, third):
    assert notify(viewer, third, Notification.Kind.LIKE) is not None


def test_unsuspending_makes_notify_flow_again(alice, viewer):
    suspended(alice)
    assert notify(viewer, alice, Notification.Kind.LIKE) is None
    alice.unsuspend()
    assert notify(viewer, alice, Notification.Kind.LIKE) is not None


# --- chokepoint 8: the AP person collections -------------------------


def test_a_suspended_follower_disappears_from_someone_elses_collection(alice, viewer):
    """Checked on the paged view, because the collection's top-level
    document only carries ``first``/``last`` pointers — asserting against
    that would pass whether or not the follower was in the collection at
    all."""
    viewer.followers.add(alice)
    url = reverse("ap-followers", args=["viewer"]) + "?page=1"
    assert "alice" in body(Client().get(url, **AP))
    suspended(alice)
    assert "alice" not in body(Client().get(url, **AP))


def test_the_collection_filter_is_independent_of_the_endpoint_gate(alice, viewer):
    """The endpoint itself already 404s for a suspended actor, so a test
    that stopped there would not prove the *collection* filter exists. This
    checks the queryset the collection is built from directly."""
    alice.follows.add(viewer)
    suspended(viewer)
    kept = list(alice.follows.all().filter(suspended_at__isnull=True))
    assert viewer.pk not in [u.pk for u in kept]


# --- chokepoint 9: the thread walk -----------------------------------


def reply(parent, user, text):
    return Status.objects.create(
        user=user,
        film=parent.film,
        status_type="comment",
        content=f"<p>{text}</p>",
        raw_content=text,
        reply_parent=parent,
    )


def test_a_suspended_reply_author_is_gone_from_the_thread(alice, viewer, review):
    reply(review, viewer, "agreed")
    assert "agreed" in body(logged_in(alice).get(reverse("status", args=[review.pk])))
    suspended(viewer)
    assert "agreed" not in body(
        logged_in(alice).get(reverse("status", args=[review.pk]))
    )


def test_a_reply_answering_a_suspended_author_is_labelled_not_named(
    alice, viewer, review
):
    """Naming the hidden handle would leak exactly the thing the hide is
    meant to remove, on a page whose whole point is that it is not here."""
    child = reply(review, viewer, "first")
    reply(child, alice, "answering viewer")
    suspended(viewer)
    page = body(logged_in(alice).get(reverse("status", args=[review.pk])))
    assert "answering viewer" in page
    assert "a hidden reply" in page


def test_a_live_reply_under_a_suspended_row_still_renders(alice, viewer, review):
    """Hiding the whole subtree would erase conversations other people are
    still having — more deletion than a hide decided."""
    child = reply(review, viewer, "hidden middle")
    reply(child, alice, "live below")
    suspended(viewer)
    page = body(logged_in(alice).get(reverse("status", args=[review.pk])))
    assert "live below" in page
    assert "hidden middle" not in page


# --- standalone: the film page review list ---------------------------


def test_a_suspended_reviewers_review_leaves_the_film_page(alice, dune, review):
    assert "Great film" in body(Client().get(reverse("film", args=[dune.pk])))
    suspended(alice)
    page = body(Client().get(reverse("film", args=[dune.pk])))
    assert "Great film" not in page
    assert Status.objects.get(pk=review.pk).deleted is False


# --- standalone: the post page root ---------------------------------


def test_a_suspended_authors_post_page_404s_to_everyone(alice, review):
    assert Client().get(reverse("status", args=[review.pk])).status_code == 200
    suspended(alice)
    assert Client().get(reverse("status", args=[review.pk])).status_code == 404
    assert (
        logged_in(third_user()).get(reverse("status", args=[review.pk])).status_code
        == 404
    )


def third_user():
    return member(localname="anoncheck", password=PASSWORD)


def test_a_suspended_authors_note_document_404s_too(alice, review):
    """The machine arm is included. A block cannot reach it because blocks
    are not federated; a suspension can, and R102 says the content is
    hidden."""
    suspended(alice)
    resp = Client().get(
        reverse("status", args=[review.pk]), HTTP_ACCEPT="application/activity+json"
    )
    assert resp.status_code == 404


def test_like_and_reply_routes_refuse_a_suspended_authors_post(alice, viewer, review):
    """R85 — the route refuses what the page withholds, so a hidden post
    cannot keep quietly accumulating interactions."""
    suspended(alice)
    client = logged_in(viewer)
    assert client.post(reverse("status-like", args=[review.pk])).status_code == 404
    assert (
        client.post(
            reverse("status-reply", args=[review.pk]), {"content": "hello"}
        ).status_code
        == 404
    )


# --- standalone: the tallies ----------------------------------------


def test_like_counts_exclude_a_suspended_likers_like(alice, viewer, review):
    """Read with ``.get`` rather than indexing: the grouped query returns no
    row at all for a zero tally, so a hidden-key failure and a zero would
    otherwise be indistinguishable here."""
    toggle_like(viewer, review)
    assert like_counts([review.pk]).get(review.pk, 0) == 1
    suspended(viewer)
    assert like_counts([review.pk]).get(review.pk, 0) == 0


def test_reply_counts_exclude_a_suspended_reply_author(alice, viewer, review):
    reply(review, viewer, "x")
    assert reply_counts([review.pk]).get(review.pk, 0) == 1
    suspended(viewer)
    assert reply_counts([review.pk]).get(review.pk, 0) == 0


def test_the_post_page_like_count_excludes_suspended_likers(alice, viewer, review):
    """The raw ``status.likes.count()`` on the post page is a second path
    to the same tally and needed its own clause."""
    toggle_like(viewer, review)
    assert (
        logged_in(alice).get(reverse("status", args=[review.pk])).context["like_count"]
        == 1
    )
    suspended(viewer)
    assert (
        logged_in(alice).get(reverse("status", args=[review.pk])).context["like_count"]
        == 0
    )


def test_the_like_toggle_response_reports_the_filtered_count(
    alice, viewer, third, review
):
    """The number the button updates to must be the number the page
    renders, or the button lies until the next reload."""
    toggle_like(third, review)
    suspended(third)
    resp = logged_in(viewer).post(reverse("status-like", args=[review.pk]))
    assert resp.json()["count"] == 1


# --- standalone: follow / block on a suspended target ---------------


def test_a_suspended_account_cannot_be_followed(alice, viewer):
    suspended(alice)
    logged_in(viewer).post(reverse("user-follow", args=["alice"]))
    assert viewer.follows.filter(pk=alice.pk).exists() is False


def test_an_existing_follow_can_still_be_removed_after_a_suspend(alice, viewer):
    """Unfollow must always work — it removes a subscription rather than
    creating one."""
    viewer.follows.add(alice)
    suspended(alice)
    logged_in(viewer).post(reverse("user-unfollow", args=["alice"]))
    assert viewer.follows.filter(pk=alice.pk).exists() is False


def test_blocking_a_suspended_account_still_works(alice, viewer):
    """Blocking is defensive personal state that surfaces nothing; refusing
    it would be a stranger answer than allowing it."""
    suspended(alice)
    logged_in(viewer).post(reverse("user-block", args=["alice"]))
    assert viewer.blocks.filter(pk=alice.pk).exists() is True


# --- the deliberate NON-filters -------------------------------------


def test_the_moderator_queue_keeps_showing_a_suspended_target(alice, viewer, review):
    """Audit, not an oversight (the brief names this as a deliberate
    non-filter). A moderator must still see the report that led to a
    suspension — and any report filed against someone who is already
    suspended — rather than the queue silently swallowing them."""
    logged_in(viewer).post(
        reverse("report-status", args=[review.pk]),
        {"category": "spam", "comment": "ZPROBE_junk_evidence"},
    )
    assert Report.unresolved().count() == 1
    suspended(alice)
    page = body(logged_in_mod().get(reverse("moderation")))
    assert "ZPROBE_junk_evidence" in page
    assert "alice" in page


def mod_account():
    return member(localname="queue_mod", password=PASSWORD, is_moderator=True)


def logged_in_mod():
    return logged_in(mod_account())


def test_the_notification_ledger_is_not_filtered_at_render_time(alice, viewer):
    """The write-side refusal in ``notify()`` is the filter. The ledger's
    own render already has a SET_NULL "Someone" precedent for a vanished
    actor, so a render-time suspension filter would add nothing and create
    a second source of truth about what the user is owed."""
    note = notify(viewer, alice, Notification.Kind.LIKE)
    assert note is not None
    suspended(alice)
    assert Notification.objects.filter(pk=note.pk).exists() is True
    page = body(logged_in(viewer).get(reverse("notifications")))
    assert "alice" in page


# --- the Person document's suspension flag --------------------------


@pytest.fixture
def rf():
    return RequestFactory()


def test_the_person_document_emits_suspended_even_when_false(alice, rf):
    doc = person_document(alice, rf.get("/"))
    assert "suspended" in doc
    assert doc["suspended"] is False


def test_the_person_document_declares_the_toot_prefix(alice, rf):
    assert {"toot": "http://joinmastodon.org/ns#"} in person_document(
        alice, rf.get("/")
    )["@context"]


def test_the_flag_flips_with_the_state_rather_than_disappearing(alice, rf):
    assert person_document(alice, rf.get("/"))["suspended"] is False
    alice.suspend()
    assert person_document(alice, rf.get("/"))["suspended"] is True
    alice.unsuspend()
    assert person_document(alice, rf.get("/"))["suspended"] is False
