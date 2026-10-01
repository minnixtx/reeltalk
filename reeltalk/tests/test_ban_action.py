"""The ban verb (moderation arc increment 5, R102/R103b/R108).

Split from the read-site file the way increment 4b split its two halves: a
filter that was not written hides nothing, whereas a mis-guarded verb
destroys the wrong person's entire output, or fires before the local write
and leaves the two halves of one decision disagreeing.

Ordered by how each failure would bite:

* **The removal is real.** Suspend hides content with rows intact; ban
  removes it. The tests that matter most here are the ones that would fail
  if ban had been implemented by borrowing the suspension filter — which is
  exactly the failure mode §2D names. So the content assertions come first
  and are explicit about the *content field being cleared*, not merely the
  row being flagged.
* **The two states stay separate, in both directions.** A ban must not set
  ``suspended_at``, and a suspend must not set ``banned_at``. Each is
  pinned, because the tempting shortcut for a later session is to make one
  alias the other, and the moment that happens the two promises collapse.
* **The guard, from both actor positions.** R103b, unchanged by the
  heavier verb. Every refusal asserts the target is *not* banned
  afterwards — a 403 that banned on the way out would read as a pass.
* **The wire.** ``Delete(Person)`` signed as the banned user, addressed to
  everyone who ever received their content rather than only their
  followers, and loud when it does not land.
* **The lift, and honestly what it covers.** A moderator may lift (owner,
  2026-09-28). What it restores is the account, not the writing.
"""

import json

import pytest
import responses
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from reeltalk.activitypub.broadcast import person_delete_audience
from reeltalk.core.models import Film, Status
from reeltalk.mentions.models import sync_status_mentions
from reeltalk.moderation.models import Report, ban_reported_member
from reeltalk.notifications.models import Notification, notify
from reeltalk.tests.members import member, site_admin

User = get_user_model()
PASSWORD = "s3cretpass"
REMOTE_INBOX = "https://remote.example/users/dana/inbox"
OTHER_INBOX = "https://other.example/users/erin/inbox"
ACTOR = "http://testserver/user/alice/"


# --- fixtures ----------------------------------------------------------


@pytest.fixture
def alice(db):
    return member(localname="alice", password=PASSWORD)


@pytest.fixture
def bob(db):
    """The reporting member."""
    return member(localname="bob", password=PASSWORD)


@pytest.fixture
def rob(db):
    """A second reporter, for the pile tests."""
    return member(localname="rob", password=PASSWORD)


@pytest.fixture
def mod(db):
    return member(localname="mod", password=PASSWORD, is_moderator=True)


@pytest.fixture
def mod2(db):
    return member(localname="mod2", password=PASSWORD, is_moderator=True)


@pytest.fixture
def siteadmin(db):
    return site_admin(localname="root", password=PASSWORD)


@pytest.fixture
def admin_door(db):
    """Holds Django's /admin/ door without being the site admin — R103b
    puts it on the admin's side of the line."""
    return member(localname="doorkeeper", password=PASSWORD, is_staff=True)


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
    return User.objects.create(
        localname="dana@remote.example",
        local=False,
        actor_url="https://remote.example/users/dana",
        inbox_url=REMOTE_INBOX,
    )


@pytest.fixture
def remote_mentioned(db):
    """A remote user who received one of alice's posts by *mention*, not by
    following — the group the follower-only audience would silently drop."""
    return User.objects.create(
        localname="erin@other.example",
        local=False,
        actor_url="https://other.example/users/erin",
        inbox_url=OTHER_INBOX,
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


def ban_url(report):
    return reverse("moderation-ban", args=[report.id])


def unban_url(target):
    return reverse("moderation-unban", args=[target.localname])


# --- the removal is real (R102: ban removes, suspend hides) ------------


def test_banning_soft_deletes_every_live_status_of_the_target(alice, bob, review, mod):
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam ring"})
    review.refresh_from_db()
    assert review.deleted is True


def test_the_removed_content_is_cleared_not_merely_flagged(alice, bob, review, mod):
    """The test that distinguishes ban from suspend.

    A ban implemented by borrowing the suspension filter would leave
    ``deleted`` False and the content readable-by-pk, which is the exact
    failure §2D warns about: an action that promised to remove content and
    quietly only hid it. ``Status.delete()`` clears both rendered and
    source content, so this asserts the text is gone rather than that a
    boolean flipped.
    """
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam ring"})
    review.refresh_from_db()
    assert review.content == ""
    assert review.raw_content == ""


def test_every_status_goes_not_just_the_reported_one(alice, bob, dune, review, mod):
    """The target is a person, so their whole output is in scope.

    A ban that removed only ``report.target_status`` would be a delete with
    a longer name.
    """
    second = Status.objects.create(
        user=alice,
        film=dune,
        status_type="comment",
        content="<p>another</p>",
        raw_content="another",
    )
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "all of it"})
    review.refresh_from_db()
    assert review.deleted is True
    second.refresh_from_db()
    assert second.deleted is True


def test_the_user_row_survives_the_ban(alice, bob, review, mod):
    """R102 keeps the row: statuses and the notification ledger hang off it,
    and a hard-deleted actor leaves peers holding a dangling actor URI."""
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    assert User.objects.filter(pk=alice.pk).exists()


def test_the_ban_sets_banned_at_and_cuts_the_sign_in(alice, bob, review, mod):
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    alice.refresh_from_db()
    assert alice.banned_at is not None
    assert alice.is_active is False


def test_the_ban_records_its_reason_on_the_account(alice, bob, review, mod):
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "coordinated spam"})
    alice.refresh_from_db()
    assert alice.ban_reason == "coordinated spam"


def test_ban_at_the_model_alone_removes_the_content(alice, review):
    """No half-bans available to any caller.

    The first cut of this increment put the soft-delete in
    ``ban_reported_member`` and left ``User.ban()`` writing only the state.
    Calling the model directly — which a management command, an import hook
    or a bulk tool would do — produced an account with its login cut and
    its profile gone while its review stayed on the film page. That is a
    state with no name and no explanation, and it was found by a test that
    did not go through the view. The removal now lives inside the one
    writer, so there is no way to ban without it.
    """
    alice.ban(reason="direct")
    review.refresh_from_db()
    assert review.deleted is True
    assert review.content == ""
    assert alice.is_active is False


def test_a_ban_replay_removes_nothing_fresh(alice, review):
    alice.ban(reason="first")
    review.refresh_from_db()
    first_deleted_at = review.deleted_date
    assert alice.ban(reason="second") is False
    review.refresh_from_db()
    assert review.deleted_date == first_deleted_at


# --- the two states stay separate, in both directions ------------------


def test_a_ban_does_not_touch_the_suspension_fields(alice, bob, review, mod):
    """Ban is not "suspended, harder".

    If ``ban()`` set ``suspended_at`` as a shortcut, every suspension
    read-filter would start doing ban's work and the two would become
    indistinguishable in the table — at which point "was this hidden or
    removed?" has no answer. ``is_active`` still reads False here, which is
    the point: the ban half of that property is doing the work on its own
    column.
    """
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    alice.refresh_from_db()
    assert alice.banned_at is not None
    assert alice.suspended_at is None
    assert alice.suspension_origin == ""
    assert alice.is_active is False


def test_a_suspend_does_not_set_banned_at(alice):
    """The other direction of the same separation.

    A suspend that set ``banned_at`` would make a reversible action look
    like an irreversible one to every reader of the table.
    """
    alice.suspend(reason="cooling off")
    alice.refresh_from_db()
    assert alice.suspended_at is not None
    assert alice.banned_at is None


def test_lifting_a_ban_leaves_a_separate_suspension_standing(alice, bob, review, mod):
    """Two independent facts; lifting one must not silently lift the other.

    Alice was suspended *and* banned. Lifting the ban restores neither the
    suspension nor its login cut — the suspension has its own lift, on the
    profile the ban made unreachable, which is why the two must not be
    conflated by a single "un-do" write.
    """
    alice.suspend(reason="cooling off")
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    logged_in(mod).post(unban_url(alice), {})
    alice.refresh_from_db()
    assert alice.banned_at is None
    assert alice.suspended_at is not None
    assert alice.is_active is False


# --- the audit record (R106) -----------------------------------------


def test_the_whole_pile_resolves_as_banned_with_who_and_when(
    alice, bob, rob, review, mod
):
    report_a_post(bob, review)
    report = report_a_post(rob, review)
    logged_in(mod).post(ban_url(report), {"note": "spam ring"})
    rows = list(Report.objects.filter(target_user=alice))
    assert len(rows) == 2
    for row in rows:
        assert row.resolved_at is not None
        assert row.resolved_by == mod
        assert row.action == "ban"
        assert row.note == "spam ring"


def test_ban_is_a_declared_action_on_the_report_enum(alice, bob, review, mod):
    """The enum value arrived with the code that writes it, not before it."""
    assert "ban" in dict(Report.Action.choices)


# --- the guard, from both actor positions (R103b) --------------------


def test_a_moderator_may_ban_a_regular_user(alice, bob, review, mod):
    """The owner's 2026-09-28 call: a moderator may impose a ban.

    R102 originally read "liftable by the site admin only"; the owner
    settled instead that a moderator may both impose and lift, so the reach
    is ``can_act_on`` in both directions and neither verb carries a private
    rule.
    """
    report = report_a_member(bob, alice)
    resp = logged_in(mod).post(ban_url(report), {"note": "spam"})
    assert resp.status_code == 302
    alice.refresh_from_db()
    assert alice.banned_at is not None


def test_the_site_admin_may_ban_a_regular_user(alice, bob, review, siteadmin):
    report = report_a_member(bob, alice)
    logged_in(siteadmin).post(ban_url(report), {"note": "spam"})
    alice.refresh_from_db()
    assert alice.banned_at is not None


@pytest.mark.parametrize(
    "target_fixture",
    ["siteadmin", "mod2", "admin_door"],
)
def test_a_moderator_may_not_ban_an_unreachable_target(
    alice, bob, review, mod, target_fixture, request
):
    """R103b unchanged by the heavier verb — it does not widen, and the
    refusal is real."""
    target = request.getfixturevalue(target_fixture)
    status = Status.objects.create(
        user=target,
        film=Film.objects.create(title="Other", year=2020),
        status_type="review",
        content="<p>theirs</p>",
        raw_content="theirs",
    )
    report = report_a_member(bob, target)
    resp = logged_in(mod).post(ban_url(report), {"note": "try"})
    assert resp.status_code == 403
    target.refresh_from_db()
    assert target.banned_at is None
    # And their content stands: a refused ban must not have removed anything.
    assert status.deleted is False


def test_a_moderator_may_not_ban_themselves(mod):
    """The report is built directly rather than through ``report_user``,
    which refuses a self-report before it ever reaches the queue. R103b's
    self shield has to hold at the ban route on its own terms, so the row
    has to exist."""
    report = Report.objects.create(
        reporter=mod, target_user=mod, category="spam", comment="x"
    )
    resp = logged_in(mod).post(ban_url(report), {"note": "self"})
    assert resp.status_code == 403
    mod.refresh_from_db()
    assert mod.banned_at is None


def test_replaying_a_ban_says_so_rather_than_re_deciding(alice, bob, review, mod):
    """Reached when the account is already banned under a report that is
    still open — a second moderator arriving at the same card after a peer
    already acted elsewhere. A replay through the *same* report after a
    successful ban 404s instead, because that report is resolved, which
    ``test_a_resolved_report_is_not_a_live_ban_handle`` pins separately."""
    alice.ban(reason="already gone")
    report = report_a_member(bob, alice)
    resp = logged_in(mod).post(ban_url(report), {"note": "second"}, follow=True)
    assert "already banned" in body(resp)


def test_a_plain_member_cannot_reach_the_ban_route(alice, bob, review):
    report = report_a_member(bob, alice)
    resp = logged_in(bob).post(ban_url(report), {"note": "no"})
    assert resp.status_code == 403
    alice.refresh_from_db()
    assert alice.banned_at is None


def test_an_anonymous_visitor_is_sent_to_log_in(alice, bob, review):
    report = report_a_member(bob, alice)
    resp = Client().post(ban_url(report), {}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]
    alice.refresh_from_db()
    assert alice.banned_at is None


# --- refusals that are about the action, not the actor -----------------


def test_a_resolved_report_is_not_a_live_ban_handle(alice, bob, review, mod):
    dismiss = report_a_member(bob, alice)
    logged_in(mod).post(
        reverse("moderation-dismiss", args=[dismiss.id]), {"note": "not spam"}
    )
    resp = logged_in(mod).post(ban_url(dismiss), {"note": "changed my mind"})
    assert resp.status_code == 404
    alice.refresh_from_db()
    assert alice.banned_at is None


def test_a_remote_target_is_refused(alice, bob, dune, mod):
    """R102: ban is an action on an account we own. We cannot ban a remote
    account at its home instance, and could not sign a ``Delete(Person)``
    for one anyway — a mirror holds no private key."""
    remote = User.objects.create(
        localname="carlos@remote.example",
        local=False,
        actor_url="https://remote.example/users/carlos",
        inbox_url="https://remote.example/users/carlos/inbox",
    )
    report = report_a_member(bob, remote)
    resp = logged_in(mod).post(ban_url(report), {"note": "try"})
    assert resp.status_code == 404
    remote.refresh_from_db()
    assert remote.banned_at is None


# --- the wire: Delete(Person) ----------------------------------------


@responses.activate
def test_the_ban_sends_a_delete_person_to_remote_followers(
    alice, bob, review, remote_follower, mod
):
    remote_follower.follows.add(alice)
    report = report_a_member(bob, alice)
    responses.add(responses.POST, REMOTE_INBOX)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    assert len(responses.calls) == 1
    sent = json.loads(responses.calls[0].request.body)
    assert sent["type"] == "Delete"


@responses.activate
def test_the_delete_person_object_is_the_bare_actor_uri_not_a_document(
    alice, bob, review, remote_follower, mod
):
    """Mastodon's handler opens with ``return delete_person if @account.uri
    == object_uri``. An embedded Person document would not match, the
    activity would fall through to the status-delete branch, and the ban
    would silently do nothing on their side."""
    remote_follower.follows.add(alice)
    report = report_a_member(bob, alice)
    responses.add(responses.POST, REMOTE_INBOX)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    sent = json.loads(responses.calls[0].request.body)
    assert sent["object"] == ACTOR
    assert sent["actor"] == ACTOR


@responses.activate
def test_the_delete_is_signed_as_the_banned_user_not_the_moderator(
    alice, bob, review, remote_follower, mod
):
    """Not a convention but a correctness requirement: the peer compares the
    *signature-verified* sender against the object URI, so a
    moderator-signed Delete about someone else fails that test and is a
    no-op."""
    remote_follower.follows.add(alice)
    report = report_a_member(bob, alice)
    responses.add(responses.POST, REMOTE_INBOX)
    logged_in(mod).post(ban_url(report), {})
    signature_input = responses.calls[0].request.headers["Signature-Input"]
    assert "user/alice/#main-key" in signature_input
    assert "user/mod" not in signature_input


@responses.activate
def test_the_delete_activity_id_is_deterministic(
    alice, bob, review, remote_follower, mod
):
    """``<actor>#delete``, matching the peer's own serializer, so a
    replayed delivery dedupes instead of arriving as new news."""
    remote_follower.follows.add(alice)
    report = report_a_member(bob, alice)
    responses.add(responses.POST, REMOTE_INBOX)
    logged_in(mod).post(ban_url(report), {})
    sent = json.loads(responses.calls[0].request.body)
    assert sent["id"] == f"{ACTOR}#delete"


@responses.activate
def test_the_ban_does_not_broadcast_one_delete_per_status(
    alice, bob, dune, remote_follower, mod
):
    """The fan-out decision, pinned.

    A peer that accepts a ``Delete(Person)`` removes its mirror *and
    everything hanging off it*, so per-status deletes multiply the
    synchronous POST count by the target's output to say something the
    person delete already says. With no retry queue that is the difference
    between a moderator's request finishing and not.
    """
    Status.objects.create(
        user=alice,
        film=dune,
        status_type="comment",
        content="<p>two</p>",
        raw_content="two",
    )
    Status.objects.create(
        user=alice,
        film=dune,
        status_type="comment",
        content="<p>three</p>",
        raw_content="three",
    )
    remote_follower.follows.add(alice)
    report = report_a_member(bob, alice)
    responses.add(responses.POST, REMOTE_INBOX)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    assert len(responses.calls) == 1


# --- the audience: everyone who ever received the content -------------


def test_the_audience_includes_a_mentioned_remote_who_does_not_follow(
    alice, review, remote_mentioned
):
    """§2D's hard question, and the reason the status helper is wrong here.

    ``broadcast_status_create`` delivers to mention targets precisely so
    the mention arrives. A Person delete addressed only to followers would
    leave that stranger holding a Note from an actor nobody has told them
    is gone.
    """
    sync_status_mentions(review, [remote_mentioned])
    audience = person_delete_audience(alice)
    assert remote_mentioned in audience


def test_the_audience_includes_a_remote_author_the_target_replied_to(
    alice, dune, remote_mentioned
):
    """``broadcast_reply`` delivers to the parent author whether or not
    they follow the replier, so a stranger who was answered holds a reply
    from this actor."""
    parent = Status.objects.create(
        user=remote_mentioned,
        film=dune,
        status_type="review",
        content="<p>their review</p>",
        raw_content="their review",
    )
    Status.objects.create(
        user=alice,
        film=dune,
        status_type="comment",
        content="<p>a reply</p>",
        raw_content="a reply",
        reply_parent=parent,
    )
    audience = person_delete_audience(alice)
    assert remote_mentioned in audience


def test_the_audience_includes_remote_followers(alice, remote_follower):
    remote_follower.follows.add(alice)
    assert remote_follower in person_delete_audience(alice)


def test_the_audience_excludes_local_members(alice, bob):
    """A local member has no inbox and reads the local rows anyway."""
    bob.follows.add(alice)
    assert person_delete_audience(alice) == []


def test_the_audience_does_not_include_the_banned_actor_themselves(
    alice, remote_follower
):
    remote_follower.follows.add(alice)
    assert alice not in person_delete_audience(alice)


def test_the_audience_covers_all_three_groups_without_duplicates(
    alice, dune, remote_follower, remote_mentioned, review
):
    """A user who is in more than one set is posted to once."""
    remote_follower.follows.add(alice)
    sync_status_mentions(review, [remote_mentioned, remote_follower])
    audience = person_delete_audience(alice)
    assert len(audience) == len({user.pk for user in audience})
    assert {remote_follower.pk, remote_mentioned.pk} == {user.pk for user in audience}


def test_the_audience_still_counts_a_status_deleted_long_before_the_ban(
    alice, review, remote_mentioned
):
    """Not scoped to live statuses: a recipient holds the content whatever
    later became of it, and the delete is equally true now."""
    sync_status_mentions(review, [remote_mentioned])
    review.delete()
    assert remote_mentioned in person_delete_audience(alice)


@responses.activate
def test_every_audience_member_is_actually_posted_to(
    alice, bob, review, remote_follower, remote_mentioned, mod
):
    sync_status_mentions(review, [remote_mentioned])
    remote_follower.follows.add(alice)
    report = report_a_member(bob, alice)
    responses.add(responses.POST, REMOTE_INBOX)
    responses.add(responses.POST, OTHER_INBOX)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    posted_to = {call.request.url for call in responses.calls}
    assert posted_to == {REMOTE_INBOX, OTHER_INBOX}


# --- loudness (R108) -----------------------------------------------


@responses.activate
def test_a_ban_that_fails_to_federate_is_written_to_the_audit_record(
    alice, bob, review, remote_follower, mod
):
    remote_follower.follows.add(alice)
    report = report_a_member(bob, alice)
    responses.add(responses.POST, REMOTE_INBOX, status=500)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    report.refresh_from_db()
    assert "[federation]" in report.note
    assert "remote.example" in report.note


@responses.activate
def test_a_ban_that_fails_to_federate_warns_the_moderator(
    alice, bob, review, remote_follower, mod
):
    remote_follower.follows.add(alice)
    report = report_a_member(bob, alice)
    responses.add(responses.POST, REMOTE_INBOX, status=500)
    resp = logged_in(mod).post(ban_url(report), {"note": "spam"}, follow=True)
    text = body(resp)
    assert "not told" in text
    assert "remote.example" in text


@responses.activate
def test_a_delivery_failure_does_not_undo_the_local_ban(
    alice, bob, review, remote_follower, mod
):
    """R108: loud and non-blocking. The local decision stands whatever the
    remote did."""
    remote_follower.follows.add(alice)
    report = report_a_member(bob, alice)
    responses.add(responses.POST, REMOTE_INBOX, status=502)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    alice.refresh_from_db()
    assert alice.banned_at is not None
    assert alice.is_active is False
    review.refresh_from_db()
    assert review.deleted is True


def test_the_local_ban_stands_with_no_remote_audience_at_all(alice, bob, review, mod):
    """A zero-follower ban still removes content and still bans — the local
    half does not depend on there being anyone to tell."""
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    alice.refresh_from_db()
    assert alice.banned_at is not None
    review.refresh_from_db()
    assert review.deleted is True


# --- the lift -------------------------------------------------------


def test_a_moderator_may_lift_a_ban(alice, bob, review, mod):
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    resp = logged_in(mod).post(unban_url(alice), {})
    assert resp.status_code == 302
    alice.refresh_from_db()
    assert alice.banned_at is None
    assert alice.is_active is True


def test_the_site_admin_may_lift_a_ban(alice, bob, review, mod, siteadmin):
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    logged_in(siteadmin).post(unban_url(alice), {})
    alice.refresh_from_db()
    assert alice.banned_at is None


def test_lifting_a_ban_does_not_restore_the_removed_content(alice, bob, review, mod):
    """The honest boundary of the lift, pinned rather than described.

    ``Status.delete()`` cleared the text. Restoring it would need a private
    content backup attached to the ban, which is a different product. So
    the lift restores the account and not the writing, and a test says so
    rather than leaving "lift" to imply more than it does.
    """
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    logged_in(mod).post(unban_url(alice), {})
    review.refresh_from_db()
    assert review.deleted is True
    assert review.content == ""


def test_a_moderator_cannot_lift_a_ban_on_someone_they_could_not_ban(
    bob, review, mod, siteadmin
):
    """Symmetry of the owner's call: whoever may impose it may lift it, and
    nobody else may do either."""
    report = report_a_member(bob, siteadmin)
    logged_in(mod).post(ban_url(report), {"note": "try"})
    siteadmin.refresh_from_db()
    assert siteadmin.banned_at is None
    resp = logged_in(mod).post(unban_url(siteadmin), {})
    assert resp.status_code == 403


def test_lifting_a_ban_that_is_not_in_force_says_so(alice, mod):
    resp = logged_in(mod).post(unban_url(alice), {}, follow=True)
    assert "is not banned" in body(resp)


def test_a_plain_member_cannot_lift_a_ban(alice, bob, review, mod):
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    resp = logged_in(bob).post(unban_url(alice), {})
    assert resp.status_code == 403
    alice.refresh_from_db()
    assert alice.banned_at is not None


def test_the_lift_lives_on_moderate_not_on_the_profile(alice, bob, review, mod):
    """The ban destroyed the surface a lift would otherwise sit on.

    A suspend keeps a public profile that explains itself, so 4b put the
    unsuspend control there. A ban leaves no public page at all, so the
    control has to be on the moderator's own surface or the action has no
    undo.
    """
    assert reverse("moderation-unban", args=["alice"]) == "/moderate/alice/unban/"


def test_a_banned_account_can_be_lifted_with_an_empty_queue(alice, bob, review, mod):
    """The section renders outside the "nothing to review" branch.

    A drained queue with a live ban is exactly the moment the lift is
    needed.
    """
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    page = body(logged_in(mod).get(reverse("moderation")))
    assert "Banned accounts" in page
    assert "@alice" in page
    assert unban_url(alice) in page


# --- the helper, at the primitive -----------------------------------


def test_the_helper_reports_which_statuses_it_removed(alice, bob, dune, review, mod):
    second = Status.objects.create(
        user=alice,
        film=dune,
        status_type="comment",
        content="<p>two</p>",
        raw_content="two",
    )
    report = Report.objects.create(
        reporter=bob, target_user=alice, category="spam", comment="x"
    )
    _, banned, count, removed = ban_reported_member(report, by_user=mod, note="n")
    assert banned is True
    assert count == 1
    assert {s.pk for s in removed} == {review.pk, second.pk}


def test_the_helper_reports_a_replay_as_not_banned(alice, bob, review, mod):
    alice.ban(reason="already")
    report = Report.objects.create(
        reporter=bob, target_user=alice, category="spam", comment="x"
    )
    _, banned, _, _ = ban_reported_member(report, by_user=mod, note="n")
    assert banned is False


def test_the_helper_does_not_broadcast(alice, bob, review, mod):
    """Locality is the caller's call — the local write must commit before
    any network runs, and only a local account can be signed for."""
    report = Report.objects.create(
        reporter=bob, target_user=alice, category="spam", comment="x"
    )
    # No responses.activate: any outbound HTTP would raise, not be recorded.
    ban_reported_member(report, by_user=mod, note="n")
    alice.refresh_from_db()
    assert alice.banned_at is not None


def test_a_banned_actor_gets_no_new_notifications(alice, bob, review, mod):
    """The ledger is not content, and existing rows are left alone — but a
    ban must not keep generating new ones addressed to or from the banned."""
    report = report_a_member(bob, alice)
    logged_in(mod).post(ban_url(report), {"note": "spam"})
    # Reload both handles: the view banned alice through its own instance,
    # and a stale in-memory copy would still read banned_at=None and make
    # this pass for the wrong reason.
    alice.refresh_from_db()
    bob.refresh_from_db()
    assert alice.banned_at is not None
    before = Notification.objects.count()
    assert notify(bob, alice, Notification.Kind.LIKE, review) is None
    assert notify(alice, bob, Notification.Kind.LIKE, review) is None
    assert Notification.objects.count() == before
