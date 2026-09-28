"""Where a banned account must stop surfacing (moderation arc increment 5).

Increment 4a enumerated the read sites and 4b filtered them all for
*suspension*. This applies the ban filter to the account-level subset of
that same map — deliberately not a re-enumeration, and deliberately not the
same filter.

The distinction that organises the whole file:

* **Content** is removed by soft-delete, so the ~19 ``deleted=False`` sites
  already handle it. Those are covered here only to prove the removal
  reached them, not because new code was needed.
* **The account** is what needs new work, because ban has no existing
  filter for "this person is gone". Each site below gets its own clause,
  and each is a place a banned account would otherwise keep appearing.
* **``410 Gone`` rather than ``404``** is the substance of most of it. For
  a peer the codes mean different things: 404 invites a retry and leaves
  the actor undetermined; 410 is the tombstone, and it is the statement
  the ``Delete(Person)`` already made. An endpoint that answered 404 after
  that delete would contradict it.

The separation from suspension is pinned at both ends: a suspended-but-not-
banned account must still get the suspended page 4b built, and a
banned-but-not-suspended account must get a tombstone rather than that
page. If either ever converges, one of these two tests fails.
"""

import pytest
from django.conf import settings
from django.contrib.auth import SESSION_KEY, authenticate, get_user_model
from django.test import Client
from django.urls import reverse

from reeltalk.core.models import Film, Status
from reeltalk.mentions.models import sync_status_mentions
from reeltalk.moderation.models import Report
from reeltalk.social.views import has_admin

User = get_user_model()
PASSWORD = "s3cretpass"
SIGNUP_PASSWORD = "correct-horse-battery-9"


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password=PASSWORD)


@pytest.fixture
def bob(db):
    return User.objects.create_user(localname="bob", password=PASSWORD)


@pytest.fixture
def mod(db):
    return User.objects.create_user(
        localname="mod", password=PASSWORD, is_moderator=True
    )


@pytest.fixture
def siteadmin(db):
    return User.objects.create_superuser(localname="root", password=PASSWORD)


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


@pytest.fixture
def banned(alice):
    alice.ban(reason="spam ring")
    alice.refresh_from_db()
    return alice


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


def body(resp):
    return resp.content.decode()


def profile_url(localname):
    return reverse("user-profile", args=[localname])


# --- the actor is gone, at every one of its own surfaces --------------


def test_the_profile_returns_410_gone(banned):
    resp = Client().get(profile_url("alice"))
    assert resp.status_code == 410


def test_the_ap_person_document_is_gone_too(banned):
    """Above the content-negotiation split on purpose.

    A peer that re-fetches the actor id we sent a ``Delete(Person)`` for
    must get the tombstone. Serving the Person document would contradict
    the delete; serving 404 would leave "never existed" and "was removed"
    indistinguishable, which is the exact distinction the ban is making.
    """
    resp = Client().get(profile_url("alice"), HTTP_ACCEPT="application/activity+json")
    assert resp.status_code == 410


def test_the_films_tab_is_gone_rather_than_redirected(banned):
    """A suspension redirects to the profile because the profile has
    something to say. A ban has nothing to say anywhere, and a 302 into a
    410 would make the tab's own status code wrong."""
    resp = Client().get(reverse("user-films", args=["alice"]))
    assert resp.status_code == 410


def test_webfinger_stops_resolving(banned):
    resp = Client().get(
        reverse("webfinger"), {"resource": f"acct:alice@{settings.DOMAIN}"}
    )
    assert resp.status_code == 410


def test_the_outbox_is_gone(banned):
    resp = Client().get(reverse("ap-outbox", args=["alice"]))
    assert resp.status_code == 410


def test_the_followers_collection_is_gone(banned):
    resp = Client().get(reverse("ap-followers", args=["alice"]))
    assert resp.status_code == 410


def test_the_following_collection_is_gone(banned):
    resp = Client().get(reverse("ap-following", args=["alice"]))
    assert resp.status_code == 410


def test_the_per_actor_inbox_is_gone(banned):
    """A peer delivering here gets the tombstone rather than a 202 that
    quietly goes nowhere."""
    resp = Client().post(
        reverse("ap-inbox", args=["alice"]), "{}", content_type="application/json"
    )
    assert resp.status_code == 410


def test_a_410_is_not_a_404_anywhere_on_the_actor(banned):
    """The distinction is the whole point of the tombstone, so it is pinned
    across every surface at once rather than trusted to five separate
    branches each having chosen the right code."""
    urls = [
        profile_url("alice"),
        reverse("user-films", args=["alice"]),
        reverse("ap-outbox", args=["alice"]),
        reverse("ap-followers", args=["alice"]),
        reverse("ap-following", args=["alice"]),
    ]
    for url in urls:
        assert Client().get(url).status_code == 410, url


# --- the separation from suspension, both directions ----------------


def test_a_suspended_but_not_banned_account_still_gets_its_suspended_page(alice, bob):
    """4b's page must survive this increment.

    The ban branch sits above the suspension branch in the profile view, so
    a bug that keyed the tombstone off the wrong column, or that treated any
    moderation state as "gone", would take out the suspended page here.
    """
    alice.suspend(reason="cooling off")
    resp = Client().get(profile_url("alice"))
    assert resp.status_code == 200
    assert "suspended" in body(resp).lower()


def test_a_banned_account_does_not_get_the_suspended_page(banned):
    body_text = body(Client().get(profile_url("alice")))
    assert "This account has been suspended." not in body_text


def test_a_banned_account_is_not_reported_as_suspended(banned):
    assert banned.suspended_at is None
    assert banned.banned_at is not None


# --- the account must stop surfacing inside other people's data ------


def test_a_banned_follower_is_dropped_from_the_followers_collection(bob, banned):
    """The one place a banned account could still appear after its own
    endpoints went: a public list naming the relationship between somebody
    else and an account that no longer exists."""
    bob.follows.add(banned)
    resp = logged_in(bob).get(reverse("ap-followers", args=["bob"]))
    assert resp.status_code == 200
    assert "user/alice" not in str(resp.json())


def test_a_banned_followee_is_dropped_from_the_following_collection(bob, banned):
    bob.follows.add(banned)
    resp = logged_in(bob).get(reverse("ap-following", args=["bob"]))
    assert "user/alice" not in str(resp.json())


def test_feed_membership_excludes_a_banned_member(bob, banned):
    """Statuses are handled by ``deleted``; **shelf events** are what this
    clause actually catches. They derive from ``ShelfFilm`` rows and never
    pass through ``deleted``, so without this a banned member's "added X to
    their Watchlist" would keep appearing in their followers' feeds."""
    bob.follows.add(banned)
    assert banned.pk not in bob.feed_member_ids()


def test_feed_membership_excludes_a_suspended_member_too(bob, alice):
    """Both columns exclude, and each is independently necessary — dropping
    either clause lets one of the two states back into the feed."""
    alice.suspend()
    bob.follows.add(alice)
    assert alice.pk not in bob.feed_member_ids()


def test_the_banned_users_review_is_gone_from_the_film_page(alice, review, dune):
    """Reached through the existing ``deleted`` filter rather than new
    code — asserted anyway, because this is the claim R102 actually makes
    about content."""
    page = body(Client().get(reverse("film", args=[dune.id])))
    assert "Great film" in page
    alice.ban(reason="spam")
    after = body(Client().get(reverse("film", args=[dune.id])))
    assert "Great film" not in after


# --- mention chips: stored HTML, no query to filter -----------------


def test_a_stored_mention_chip_does_not_500_after_the_target_is_banned(
    bob, banned, dune
):
    """The brief's explicit requirement. Mention chips are baked into
    ``Status.content`` HTML at write time, so there is no query here to
    filter — the row still exists, the chip still renders, and the page
    must simply not fall over."""
    status = Status.objects.create(
        user=bob,
        film=dune,
        status_type="comment",
        content='<p>agree with <a href="/user/alice/">@alice</a></p>',
        raw_content="agree with @alice",
    )
    sync_status_mentions(status, [banned])
    resp = Client().get(reverse("status", args=[status.id]))
    assert resp.status_code == 200
    assert "@alice" in body(resp)


def test_following_a_stored_mention_chip_lands_on_the_tombstone(bob, banned, dune):
    """The chip stays live markup and leads to a 410, which is the honest
    answer: the person was here and is not now. Better than rewriting every
    stored post to strip chips, which would be a content migration for a
    cosmetic gain."""
    status = Status.objects.create(
        user=bob,
        film=dune,
        status_type="comment",
        content='<p>agree with <a href="/user/alice/">@alice</a></p>',
        raw_content="agree with @alice",
    )
    sync_status_mentions(status, [banned])
    assert Client().get("/user/alice/").status_code == 410
    assert status.mentions.count() == 1


def test_status_mention_rows_survive_the_ban_without_breaking_anything(
    bob, banned, dune
):
    """The FK still resolves — the user row is kept, which is exactly why
    nothing here dangles."""
    status = Status.objects.create(
        user=bob,
        film=dune,
        status_type="comment",
        content='<p>hi <a href="/user/alice/">@alice</a></p>',
        raw_content="hi @alice",
    )
    sync_status_mentions(status, [banned])
    banned.ban(reason="spam")
    row = status.mentions.select_related("user").first()
    assert row.user.pk == banned.pk


# --- the localname reservation (R40: case variants are one identity) -


def test_a_banned_localname_cannot_be_re_registered_exactly(client, siteadmin, banned):
    resp = client.post(
        reverse("signup"),
        {
            "localname": "alice",
            "display_name": "New Alice",
            "email": "",
            "password1": SIGNUP_PASSWORD,
            "password2": SIGNUP_PASSWORD,
        },
    )
    assert resp.status_code == 200  # re-rendered with errors
    assert User.objects.filter(localname="alice").count() == 1


def test_a_banned_localname_cannot_be_re_registered_as_a_case_variant(
    client, siteadmin, banned
):
    """The reservation is worthless if it is case-sensitive.

    R40 makes ``alice`` and ``Alice`` one identity, and signup already
    rejects insensitive duplicates — so reserving only the exact spelling
    would hand the banned identity to a new registrant who then federates
    as the same actor. This is the test that makes the case-insensitivity a
    requirement rather than an intention.
    """
    resp = client.post(
        reverse("signup"),
        {
            "localname": "Alice",
            "display_name": "Not Alice",
            "email": "",
            "password1": SIGNUP_PASSWORD,
            "password2": SIGNUP_PASSWORD,
        },
    )
    assert resp.status_code == 200
    squat = User.objects.filter(localname__iexact="alice").exclude(pk=banned.pk)
    assert not squat.exists()


def test_the_reservation_holds_because_the_row_is_kept(client, siteadmin, banned):
    """What actually does the reserving, written down so a future "clean up
    banned rows" change breaks this loudly.

    There is no separate reservation table. The banned user row keeps its
    ``localname``, the column is unique, and signup's existing
    case-insensitive check sees it. Delete the row and the name is free
    again — which is why R102 keeps it.
    """
    assert User.objects.filter(localname="alice").exists()
    resp = client.post(
        reverse("signup"),
        {
            "localname": "ALICE",
            "display_name": "Squat",
            "email": "",
            "password1": SIGNUP_PASSWORD,
            "password2": SIGNUP_PASSWORD,
        },
    )
    assert resp.status_code == 200
    assert User.objects.filter(localname__iexact="alice").count() == 1


def test_an_unrelated_name_still_signs_up_fine(client, siteadmin, banned):
    resp = client.post(
        reverse("signup"),
        {
            "localname": "carol",
            "display_name": "Carol",
            "email": "",
            "password1": SIGNUP_PASSWORD,
            "password2": SIGNUP_PASSWORD,
        },
    )
    assert resp.status_code == 302
    assert User.objects.filter(localname="carol").exists()


# --- the sign-in cut -----------------------------------------------


def test_a_banned_member_cannot_authenticate(banned):
    assert authenticate(username="alice", password=PASSWORD) is None


def test_a_banned_member_is_told_the_usual_generic_failure(banned):
    """No "you are banned" — that would confirm the account exists, and the
    login form's posture is deliberately generic."""
    resp = Client().post(reverse("login"), {"username": "alice", "password": PASSWORD})
    assert resp.status_code == 200
    assert "banned" not in body(resp).lower()


def test_an_existing_session_is_cut_on_the_next_click(banned):
    """``is_active`` is derived and ``get_user()`` runs
    ``user_can_authenticate`` on every request, so an issued session dies
    without any session-store work."""
    client = logged_in(banned)
    banned.ban(reason="banned after login")
    resp = client.get(reverse("notifications"), follow_redirects=False)
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


def test_the_session_row_is_cut_not_swept(banned):
    """The session row survives — the cut is at authentication, not by
    clearing the store. That is what makes it cheap and immediate, and it
    is why unsuspend needs no session work either."""
    client = logged_in(banned)
    key = client.session.session_key
    banned.ban(reason="banned after login")
    client.get(reverse("notifications"), follow_redirects=False)
    from django.contrib.sessions.backends.db import SessionStore

    stored = SessionStore(session_key=key)
    assert stored.is_empty() is False
    # The identity is still in the row; the refusal happens when
    # ``get_user()`` runs ``user_can_authenticate`` on it, not by erasure.
    assert stored[SESSION_KEY] == str(banned.pk)


def test_a_suspended_member_is_also_still_cut(alice):
    """The suspension half of ``is_active`` still works after this
    increment widened the property."""
    alice.suspend()
    assert authenticate(username="alice", password=PASSWORD) is None


# --- follow / unfollow --------------------------------------------


def test_following_a_banned_account_is_refused(bob, banned):
    """The route refuses what the page withholds, and only the *follow* half.

    Asserted on the invariant rather than the flash message: the refusal
    sets a message and redirects to the profile, and the profile is now a
    ``410`` tombstone, so the message has nowhere to render. That is a
    cosmetic consequence of the tombstone rather than a hole — the writer
    refused, which is the whole guarantee — and it is only reachable by a
    hand-built POST anyway, since no page offers a follow button to a
    banned account.
    """
    resp = logged_in(bob).post(reverse("user-follow", args=["alice"]), {})
    assert resp.status_code == 302
    assert bob.follows.filter(pk=banned.pk).exists() is False


def test_unfollowing_a_banned_account_still_works(bob, banned):
    """Removing a subscription surfaces nothing and must never be blocked by
    somebody else's moderation state."""
    bob.follows.add(banned)
    logged_in(bob).post(reverse("user-unfollow", args=["alice"]), {})
    assert bob.follows.filter(pk=banned.pk).exists() is False


# --- flagged, not decided ----------------------------------------


def test_has_admin_still_counts_a_banned_only_admin(siteadmin):
    """FLAGGED FOR THE OWNER, NOT DECIDED — same open question 4b left for
    suspension, now with a second way to reach it.

    ``has_admin()`` is ``filter(is_superuser=True).exists()``. If the site
    admin is banned and nobody else is an admin, the instance still reads
    as "set up" while no administrator can sign in. Both candidate fixes
    have consequences the owner should weigh rather than an agent picking:
    filtering bans out of ``has_admin()`` makes the instance look
    *unconfigured* (redirecting to ``/setup/``, which could invite a
    second admin creation over a database that already has one); counting
    banned admins — the current behaviour — makes it look fine while it is
    unreachable.

    Reachable today only by a self-ban: a moderator cannot ban the admin
    (R103b). The test pins the behaviour so the decision is a change
    rather than a discovery.
    """
    siteadmin.ban(reason="self")
    assert has_admin() is True
    assert authenticate(username="root", password=PASSWORD) is None


def test_a_banned_moderator_still_leaves_the_admin_intact(siteadmin, mod):
    mod.ban(reason="spam moderator")
    assert has_admin() is True
    assert authenticate(username="root", password=PASSWORD) is not None


# --- the queue keeps showing what it acted on ---------------------


def test_the_queue_still_shows_a_banned_target_for_audit(banned, bob, review, mod):
    """4b's explicit non-filter for suspension extends to ban: the audit
    trail must show what was acted on even though the public surface is
    gone."""
    report = Report.objects.create(
        reporter=bob, target_user=banned, category="spam", comment="x"
    )
    logged_in(mod).post(reverse("moderation-ban", args=[report.id]), {"note": "spam"})
    page = body(logged_in(mod).get(reverse("moderation")))
    assert "@alice" in page
