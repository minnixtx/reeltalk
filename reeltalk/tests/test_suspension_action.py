"""The suspend / unsuspend action and its broadcast (increment 4b).

Split from the read-filter file because these test a *verb* rather than a
filter, and the things that can go wrong are different: a filter that was
not written hides nothing, whereas an action that is mis-guarded destroys
the wrong account, or fires after the local write and leaves the two
halves of a decision disagreeing.

Ordered by how each failure would bite:

* **The guard, from both actor positions.** R103b is a rule about who is
  asking, so exercising only one position proves nothing about the other.
  A moderator reaches only a regular user; the site admin reaches anyone.
  Every refusal asserts the account is *not* suspended afterwards, because
  a 403 that suspended on the way out would read as a pass.
* **The audit record.** The whole pile resolves with who, when, what and
  the note — the report row is the only record this arc keeps (R106).
* **Federation, and its loudness.** The update goes out signed as the
  suspended user, never the moderator who clicked. A failure is written
  into the audit line and shown, because "suspended here and nobody else
  knows" is the fact R108 refuses to let be quiet.
* **The lift.** Unsuspend restores everything, because suspend deleted
  nothing — and it lives on the profile rather than the queue, which is
  itself worth pinning.
"""

import json

import pytest
import responses
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from reeltalk.activitypub.broadcast import broadcast_actor_update
from reeltalk.core.models import Film, Status
from reeltalk.moderation.models import Report
from reeltalk.notifications.models import Notification

User = get_user_model()
PASSWORD = "s3cretpass"
REMOTE_INBOX = "https://remote.example/users/dana/inbox"


# --- fixtures ----------------------------------------------------------


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password=PASSWORD)


@pytest.fixture
def bob(db):
    """The reporting member."""
    return User.objects.create_user(localname="bob", password=PASSWORD)


@pytest.fixture
def rob(db):
    """A second reporter, for the pile tests."""
    return User.objects.create_user(localname="rob", password=PASSWORD)


@pytest.fixture
def viewer(db):
    return User.objects.create_user(localname="viewer", password=PASSWORD)


@pytest.fixture
def mod(db):
    return User.objects.create_user(
        localname="mod", password=PASSWORD, is_moderator=True
    )


@pytest.fixture
def mod2(db):
    return User.objects.create_user(
        localname="mod2", password=PASSWORD, is_moderator=True
    )


@pytest.fixture
def siteadmin(db):
    return User.objects.create_superuser(localname="root", password=PASSWORD)


@pytest.fixture
def admin_door(db):
    """Holds Django's /admin/ door without being the site admin — R103b
    puts it on the admin's side of the line."""
    return User.objects.create_user(
        localname="doorkeeper", password=PASSWORD, is_staff=True
    )


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
def remote_follower(db):
    """A non-local follower, so the delivery loop actually reaches a send."""
    return User.objects.create(
        localname="dana@remote.example",
        local=False,
        actor_url="https://remote.example/users/dana",
        inbox_url=REMOTE_INBOX,
    )


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


def body(resp):
    return resp.content.decode()


def report_a_post(reporter, status, comment="looks like junk"):
    logged_in(reporter).post(
        reverse("report-status", args=[status.id]),
        {"category": "spam", "comment": comment},
    )
    return Report.objects.get(reporter=reporter, target_status=status)


def report_a_member(reporter, target, comment="harasser"):
    logged_in(reporter).post(
        reverse("report-user", args=[target.localname]),
        {"category": "spam", "comment": comment},
    )
    return Report.objects.get(reporter=reporter, target_user=target)


def suspend_url(report):
    return reverse("moderation-suspend", args=[report.id])


def unsuspend_url(target):
    return reverse("user-unsuspend", args=[target.localname])


# --- the guard, from both actor positions (R103b) ---------------------


def test_a_moderator_suspends_a_regular_user_from_the_queue(alice, bob, review, mod):
    report = report_a_post(bob, review)
    resp = logged_in(mod).post(suspend_url(report), {"note": "spam ring"})
    assert resp.status_code == 302
    alice.refresh_from_db()
    assert alice.suspended_at is not None
    assert alice.suspension_origin == "local"
    assert alice.is_active is False


def test_the_site_admin_may_suspend_a_regular_user(alice, bob, review, siteadmin):
    """The admin acts on the same queue with the same guard, and the guard
    reads who is asking — so the same POST that a moderator may make
    against alice, the admin may also make."""
    report = report_a_post(bob, review)
    assert (
        logged_in(siteadmin).post(suspend_url(report), {"note": "admin"}).status_code
        == 302
    )
    alice.refresh_from_db()
    assert alice.suspended_at is not None


def test_the_site_admin_may_suspend_a_moderator(bob, mod, dune, siteadmin):
    status = Status.objects.create(
        user=mod,
        film=dune,
        status_type="review",
        content="<p>mod review</p>",
        raw_content="mod review",
    )
    report = report_a_post(bob, status)
    assert logged_in(siteadmin).post(suspend_url(report), {}).status_code == 302
    mod.refresh_from_db()
    assert mod.suspended_at is not None


def test_a_member_cannot_suspend_and_the_account_is_untouched(alice, bob, review):
    report = report_a_post(bob, review)
    resp = logged_in(bob).post(suspend_url(report), {"note": "take them down"})
    assert resp.status_code == 403
    alice.refresh_from_db()
    assert alice.suspended_at is None


def test_an_anonymous_visitor_is_sent_to_log_in(alice, bob, review):
    report = report_a_post(bob, review)
    resp = Client().post(suspend_url(report), {})
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]
    alice.refresh_from_db()
    assert alice.suspended_at is None


def test_a_moderator_cannot_suspend_the_site_admin(bob, siteadmin, dune, mod):
    status = Status.objects.create(
        user=siteadmin,
        film=dune,
        status_type="review",
        content="<p>admin review</p>",
        raw_content="admin review",
    )
    report = report_a_post(bob, status)
    resp = logged_in(mod).post(suspend_url(report), {})
    assert resp.status_code == 403
    siteadmin.refresh_from_db()
    assert siteadmin.suspended_at is None


def test_a_moderator_cannot_suspend_a_peer_moderator(bob, mod2, dune, mod):
    status = Status.objects.create(
        user=mod2,
        film=dune,
        status_type="review",
        content="<p>peer review</p>",
        raw_content="peer review",
    )
    report = report_a_post(bob, status)
    assert logged_in(mod).post(suspend_url(report), {}).status_code == 403
    mod2.refresh_from_db()
    assert mod2.suspended_at is None


def test_a_moderator_cannot_suspend_an_account_holding_the_admin_door(
    bob, admin_door, dune, mod
):
    status = Status.objects.create(
        user=admin_door,
        film=dune,
        status_type="review",
        content="<p>staff review</p>",
        raw_content="staff review",
    )
    report = report_a_post(bob, status)
    assert logged_in(mod).post(suspend_url(report), {}).status_code == 403
    admin_door.refresh_from_db()
    assert admin_door.suspended_at is None


def test_a_moderator_cannot_suspend_themselves(dune):
    actor = User.objects.create_user(
        localname="selfmod", password=PASSWORD, is_moderator=True
    )
    other = User.objects.create_user(localname="filer", password=PASSWORD)
    status = Status.objects.create(
        user=actor,
        film=dune,
        status_type="review",
        content="<p>mine</p>",
        raw_content="mine",
    )
    report = report_a_post(other, status)
    assert logged_in(actor).post(suspend_url(report), {}).status_code == 403
    actor.refresh_from_db()
    assert actor.suspended_at is None


def test_the_site_admin_may_suspend_themselves(dune, siteadmin):
    """R103b checks the admin *before* every shield, so the admin's reach
    includes themselves. Pinned because that ordering is load-bearing:
    swap the superuser check and the self check and this is the case that
    changes."""
    other = User.objects.create_user(localname="filer2", password=PASSWORD)
    status = Status.objects.create(
        user=siteadmin,
        film=dune,
        status_type="review",
        content="<p>mine</p>",
        raw_content="mine",
    )
    report = report_a_post(other, status)
    assert logged_in(siteadmin).post(suspend_url(report), {}).status_code == 302
    siteadmin.refresh_from_db()
    assert siteadmin.suspended_at is not None


# --- the audit record (R106) ----------------------------------------


def test_the_suspend_is_recorded_on_every_row_of_the_pile(alice, bob, rob, review, mod):
    first = report_a_post(bob, review, "one")
    report_a_post(rob, review, "two")
    logged_in(mod).post(suspend_url(first), {"note": "coordinated spam"})
    rows = Report.objects.filter(target_user=alice)
    assert rows.count() == 2
    for row in rows:
        assert row.action == Report.Action.SUSPEND
        assert row.resolved_by_id == mod.pk
        assert row.resolved_at is not None
        assert row.note == "coordinated spam"


def test_the_suspend_action_is_distinct_from_a_dismiss(alice, bob, review, mod):
    report = report_a_post(bob, review)
    logged_in(mod).post(suspend_url(report), {"note": "n"})
    report.refresh_from_db()
    assert report.action == "suspend"
    assert report.action != Report.Action.DISMISS


def test_the_reason_is_written_to_the_account_too(alice, bob, review, mod):
    report = report_a_post(bob, review)
    logged_in(mod).post(suspend_url(report), {"note": "ZPROBE_reason"})
    alice.refresh_from_db()
    assert alice.suspension_reason == "ZPROBE_reason"


# --- the pile and the replay guards ---------------------------------


def test_a_resolved_report_is_not_a_live_suspend_handle(alice, bob, review, mod):
    first = report_a_post(bob, review)
    logged_in(mod).post(suspend_url(first), {"note": "one"})
    # Re-read before lifting: the suspend wrote to the row, and the
    # in-memory fixture instance is still the pre-suspend one, so
    # ``unsuspend()`` on it would be a silent no-op that un-tests this.
    alice.refresh_from_db()
    assert alice.suspended_at is not None
    alice.unsuspend()
    first.refresh_from_db()
    assert first.resolved_at is not None
    resp = logged_in(mod).post(suspend_url(first), {"note": "again"})
    assert resp.status_code == 404
    alice.refresh_from_db()
    assert alice.suspended_at is None


def test_an_already_suspended_target_reports_the_replay(alice, bob, review, mod):
    alice.suspend(reason="already")
    report = report_a_member(bob, alice)
    resp = logged_in(mod).post(suspend_url(report), {"note": "again"})
    assert resp.status_code == 302
    messages = [m.message for m in get_messages(resp)]
    assert any("already suspended" in m for m in messages)


def get_messages(resp):
    from django.contrib.messages import get_messages as _gm

    return _gm(resp.wsgi_request)


def test_a_remote_target_is_refused_and_not_suspended(db, bob, mod):
    remote = User.objects.create(
        localname="dana@remote.example",
        local=False,
        actor_url="https://remote.example/users/dana",
        inbox_url=REMOTE_INBOX,
    )
    report = report_a_member(bob, remote)
    resp = logged_in(mod).post(suspend_url(report), {"note": "no"})
    assert resp.status_code == 404
    remote.refresh_from_db()
    assert remote.suspended_at is None


# --- the queue card's control --------------------------------------


def test_the_suspend_disclosure_renders_for_a_reachable_target(alice, bob, review, mod):
    report = report_a_post(bob, review)
    page = body(logged_in(mod).get(reverse("moderation")))
    assert "Suspend this account" in page
    assert "stops them signing in" in page
    assert suspend_url(report) in page


def test_the_disclosure_is_collapsed_by_default(alice, bob, review, mod):
    """The whole answer to the ``.btn`` collision: the lit suspend button
    sits inside a closed ``<details>``, so it is never rendered beside
    increment 3's lit "Delete post"."""
    report_a_post(bob, review)
    page = body(logged_in(mod).get(reverse("moderation")))
    assert "<details" in page
    assert "open" not in page.split("suspend-disclosure")[1][:40]


def test_the_suspend_button_takes_the_lit_control(alice, bob, review, mod):
    report_a_post(bob, review)
    page = body(logged_in(mod).get(reverse("moderation")))
    disclosure = page.split("suspend-disclosure")[1]
    assert 'class="btn"' in disclosure


def test_no_disclosure_for_a_target_the_moderator_cannot_reach(
    bob, siteadmin, dune, mod
):
    status = Status.objects.create(
        user=siteadmin,
        film=dune,
        status_type="review",
        content="<p>admin</p>",
        raw_content="admin",
    )
    report_a_post(bob, status)
    page = body(logged_in(mod).get(reverse("moderation")))
    assert "Suspend this account" not in page


def test_no_disclosure_for_an_already_suspended_target(alice, bob, review, mod):
    alice.suspend()
    report_a_member(bob, alice)
    page = body(logged_in(mod).get(reverse("moderation")))
    assert "Suspend this account" not in page


# --- the broadcast -------------------------------------------------


@responses.activate
def test_the_suspend_broadcasts_an_actor_update_to_remote_followers(
    alice, bob, review, remote_follower, mod
):
    remote_follower.follows.add(alice)
    report = report_a_post(bob, review)
    responses.add(responses.POST, REMOTE_INBOX)
    logged_in(mod).post(suspend_url(report), {"note": "spam"})
    assert len(responses.calls) == 1
    sent = json.loads(responses.calls[0].request.body)
    assert sent["type"] == "Update"
    assert sent["object"]["type"] == "Person"
    assert sent["object"]["suspended"] is True


@responses.activate
def test_the_actor_update_is_signed_as_the_suspended_user_not_the_moderator(
    alice, bob, review, remote_follower, mod
):
    """Same rule increment 3 pinned for the delete: the identity on the
    wire is the actor the document describes, never whoever clicked."""
    remote_follower.follows.add(alice)
    report = report_a_post(bob, review)
    responses.add(responses.POST, REMOTE_INBOX)
    logged_in(mod).post(suspend_url(report), {})
    call = responses.calls[0]
    assert json.loads(call.request.body)["actor"] == "http://testserver/user/alice/"
    signature_input = call.request.headers["Signature-Input"]
    assert "user/alice/#main-key" in signature_input
    assert "user/mod" not in signature_input


@responses.activate
def test_a_local_follower_gets_no_delivery(alice, bob, review, mod):
    """Local followers read the local rows; a POST would tell them nothing."""
    local = User.objects.create_user(localname="localfan", password=PASSWORD)
    local.follows.add(alice)
    report = report_a_post(bob, review)
    responses.add(responses.POST, REMOTE_INBOX)
    logged_in(mod).post(suspend_url(report), {})
    assert len(responses.calls) == 0


@responses.activate
def test_a_failed_broadcast_is_written_into_the_audit_line(
    alice, bob, review, remote_follower, mod
):
    """R108: loud and non-blocking. The suspend stands, and the fact that
    nobody was told is on the record rather than only in a log line."""
    remote_follower.follows.add(alice)
    report = report_a_post(bob, review)
    responses.add(responses.POST, REMOTE_INBOX, status=502)
    resp = logged_in(mod).post(suspend_url(report), {"note": "spam"})
    alice.refresh_from_db()
    assert alice.suspended_at is not None
    report.refresh_from_db()
    assert "[federation]" in report.note
    assert "remote.example" in report.note
    messages = [m.message for m in get_messages(resp)]
    assert any("not told" in m for m in messages)


@responses.activate
def test_a_successful_broadcast_leaves_no_federation_line_on_the_report(
    alice, bob, review, remote_follower, mod
):
    remote_follower.follows.add(alice)
    report = report_a_post(bob, review)
    responses.add(responses.POST, REMOTE_INBOX)
    logged_in(mod).post(suspend_url(report), {"note": "spam"})
    report.refresh_from_db()
    assert "[federation]" not in report.note


# --- the lift ------------------------------------------------------


def test_a_moderator_lifts_a_suspension_from_the_profile(alice, siteadmin):
    alice.suspend(reason="wrong call")
    resp = logged_in(siteadmin).post(unsuspend_url(alice))
    assert resp.status_code == 302
    alice.refresh_from_db()
    assert alice.suspended_at is None
    assert alice.suspension_origin == ""
    assert alice.suspension_reason == ""
    assert alice.is_active is True


def test_a_member_cannot_unsuspend(alice):
    alice.suspend()
    member = User.objects.create_user(localname="member", password=PASSWORD)
    assert logged_in(member).post(unsuspend_url(alice)).status_code == 403
    alice.refresh_from_db()
    assert alice.suspended_at is not None


def test_a_moderator_cannot_lift_a_suspension_they_could_not_have_imposed(alice, mod2):
    """R103b's reach does not loosen because this direction is the
    friendly one — the account here is a peer moderator."""
    alice.is_moderator = True
    alice.save()
    alice.suspend()
    assert logged_in(mod2).post(unsuspend_url(alice)).status_code == 403
    alice.refresh_from_db()
    assert alice.suspended_at is not None


def test_unsuspending_someone_who_is_not_suspended_says_so(alice, siteadmin):
    resp = logged_in(siteadmin).post(unsuspend_url(alice))
    assert resp.status_code == 302
    messages = [m.message for m in get_messages(resp)]
    assert any("not suspended" in m for m in messages)


def test_unsuspending_broadcasts_the_flag_back_to_false(
    alice, siteadmin, remote_follower
):
    """The reason the flag is emitted always: an update that omitted it
    would make "cleared" indistinguishable from "never spoken"."""
    remote_follower.follows.add(alice)
    alice.suspend()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, REMOTE_INBOX)
        logged_in(siteadmin).post(unsuspend_url(alice))
        assert len(rsps.calls) == 1
        sent = json.loads(rsps.calls[0].request.body)
        assert sent["object"]["suspended"] is False


def test_unsuspending_restores_the_content_everywhere(alice, viewer, review, siteadmin):
    alice.suspend()
    viewer.follows.add(alice)
    assert "Great film" not in body(logged_in(viewer).get(reverse("index")))
    logged_in(siteadmin).post(unsuspend_url(alice))
    assert "Great film" in body(logged_in(viewer).get(reverse("index")))
    assert Client().get(reverse("status", args=[review.pk])).status_code == 200


# --- the primitive, pinned directly ---------------------------------


def test_broadcast_actor_update_skips_a_suspended_recipient(alice, remote_follower):
    """Unreachable from the UI today (suspend is local-only) and tested by
    constructing the state directly, so the branch increment 6 needs is
    covered rather than assumed."""
    alice.follows.add(remote_follower)
    remote_follower.suspend()
    # ``assert_all_requests_are_fired=False`` because a zero-call outcome
    # is the point here, and the mock's default would treat it as a failure.
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, REMOTE_INBOX)
        failures = broadcast_actor_update(Client().get("/").wsgi_request, alice)
        assert failures == []
        assert len(rsps.calls) == 0


def test_no_report_is_required_to_suspend_an_account(alice, siteadmin):
    """A direct suspend from the profile path leaves the account's own
    ``suspension_reason`` as the record, which is why 4a kept that column
    separate from the report note."""
    assert Report.objects.filter(target_user=alice).count() == 0
    alice.suspend(reason="direct")
    alice.refresh_from_db()
    assert alice.suspension_reason == "direct"
    assert Report.objects.filter(target_user=alice).count() == 0


def test_a_suspend_writes_no_notification(alice, bob, review, mod):
    """R99 holds through the heavier action: a suspend is not an event a
    person is owed."""
    before = Notification.objects.count()
    report = report_a_post(bob, review)
    logged_in(mod).post(suspend_url(report), {"note": "spam"})
    assert Notification.objects.count() == before
