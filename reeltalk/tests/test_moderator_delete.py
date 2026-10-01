"""The moderator content action (moderation arc increment 3, R103/R106).

Ordered by how each failure would bite:

* **``can_act_on`` from both actor positions.** R103 is a rule about *who
  is asking*, so a set that exercises only one position proves nothing about
  the other. A moderator may never reach the site admin's content or their
  own; the site admin may reach anyone's. The ordering inside the function
  (superuser checked before self) is pinned by the admin-acts-on-own-content
  case — swap the two checks and only that test goes red.
* **The route refuses before it writes.** Every refusal asserts the post is
  still there afterwards, because a 403 that deleted the row on the way out
  would read as a pass.
* **The delete is a soft-delete.** The row survives as a tombstone with its
  identity and its content cleared. That is what lets the ~19
  ``deleted=False`` read sites do the whole job with no new read-path code
  — so they are tested by *rendering them and looking for the content*, not
  by asserting the flag value.
* **The audit record.** ``delete_status`` on every row of the pile, with
  who and when, distinct from ``dismiss`` (R106).
* **Federation, and the one place it must not go.** A local author's delete
  is delivered signed **as the author**, never as the moderator who
  clicked. A mirrored author's delete is **not** broadcast, and the reason
  is pinned at the primitive as well: a mirror holds no private key, the
  key loader raises ``ValueError``, and that is not a ``RequestException``,
  so the delivery loop's except-clause cannot swallow it.

The session-proof rule from the earlier increments is followed throughout:
probe users come from ``create_user()``, every authenticated assertion
carries a ``sessionid`` check, and the login POST takes ``username``.
"""

import json

import pytest
import requests
import responses
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory
from django.urls import reverse

from reeltalk.activitypub.broadcast import broadcast_status_delete
from reeltalk.core.models import Film, Status
from reeltalk.moderation.decorators import can_act_on, can_impose_severity
from reeltalk.moderation.models import (
    Report,
    delete_reported_status,
    dismiss_report,
    file_report,
)
from reeltalk.notifications.models import Notification
from reeltalk.tests.members import member, site_admin

User = get_user_model()
PASSWORD = "s3cretpass"

REMOTE_INBOX = "https://remote.example/users/dana/inbox"
OTHER_INBOX = "https://other.example/users/eve/inbox"


@pytest.fixture
def alice(db):
    """The author of the reported post."""
    return member(localname="alice", password=PASSWORD)


@pytest.fixture
def bob(db):
    """The reporting member."""
    return member(localname="bob", password=PASSWORD)


@pytest.fixture
def rob(db):
    """A second reporting member, for cases where bob is already taken."""
    return member(localname="rob", password=PASSWORD)


@pytest.fixture
def mod(db):
    """A moderator — deliberately NOT staff, so R100's decoupling holds."""
    return member(localname="mod", password=PASSWORD, is_moderator=True)


@pytest.fixture
def siteadmin(db):
    return site_admin(localname="root", password=PASSWORD)


@pytest.fixture
def mod2(db):
    """A second moderator, for the peer case: peers do not moderate peers."""
    return member(localname="mod2", password=PASSWORD, is_moderator=True)


@pytest.fixture
def admin_door(db):
    """An account holding Django's /admin/ door without being the site admin.

    Not a fourth kind of user — the three kinds are admin, moderator and
    regular user. This is the shape that decides which side of the line
    ``is_staff`` puts an account on for R103b, and it is on the admin's
    side: a moderator never touches it.
    """
    return member(localname="doorkeeper", password=PASSWORD, is_staff=True)


@pytest.fixture
def bare_superuser(db):
    """A superuser created **without** ``is_staff``.

    Legal in Django, and the case that makes the admin half of the R103b
    shield bite on its own: ``create_superuser`` sets ``is_staff`` too, so
    without this fixture a moderator is kept off the admin only by the
    admin-door clause, and deleting the ``is_superuser`` clause would turn
    no test red. The shield names the admin *kind*, not merely the door.
    """
    return User.objects.create(
        localname="bare", password=PASSWORD, is_superuser=True, is_staff=False
    )


@pytest.fixture
def remote_follower(db):
    """A non-local follower, so the delivery loop actually reaches a send."""
    return User.objects.create(
        localname="eve@other.example",
        local=False,
        actor_url="https://other.example/users/eve",
        inbox_url=OTHER_INBOX,
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


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


def report_payload(category="spam", comment="looks like junk"):
    return {"category": category, "comment": comment}


def delete_url(report):
    return reverse("moderation-delete-status", args=[report.id])


def report_a_post(reporter, status, comment="looks like junk"):
    """File through the real member route and hand back that reporter's row.

    Keyed on the reporter as well as the status, because a two-reporter
    test would otherwise blow up on ``MultipleObjectsReturned`` — and a
    helper that only works for one reporter would quietly make every
    multi-reporter case wrong.
    """
    logged_in(reporter).post(
        reverse("report-status", args=[status.id]), report_payload("spam", comment)
    )
    return Report.objects.get(reporter=reporter, target_status=status)


def mirror_account(localname, host):
    """A mirrored remote account — no private key, by construction."""
    return User.objects.create(
        localname=f"{localname}@{host}",
        local=False,
        actor_url=f"https://{host}/users/{localname}",
        inbox_url=f"https://{host}/users/{localname}/inbox",
    )


def mirror_post(author, film, *, note_id, content="<p>remote spam</p>"):
    return Status.objects.create(
        user=author,
        film=film,
        status_type="review",
        content=content,
        raw_content="remote spam",
        local=False,
        origin_id=note_id,
        remote_url=f"https://remote.example/notes/{note_id}",
    )


# --- can_act_on: both actor positions (R103) -----------------------------


def test_a_moderator_may_act_on_an_ordinary_member(mod, alice):
    assert can_act_on(mod, alice) is True


def test_a_moderator_may_not_act_on_the_site_admin(mod, siteadmin):
    # The escalation path R100 exists to keep absent, read from this end.
    assert can_act_on(mod, siteadmin) is False


def test_a_moderator_may_not_act_on_themselves(mod):
    assert can_act_on(mod, mod) is False


def test_a_moderator_may_not_act_on_another_moderator(mod, mod2):
    # Peers do not moderate peers.
    assert can_act_on(mod, mod2) is False


def test_a_moderator_may_not_act_on_an_account_holding_the_admin_door(mod, admin_door):
    # R103b: is_staff is not a fourth user type, but it does decide which
    # side of the line an account sits on. A moderator's reach is a regular
    # user, and an account with the /admin/ door is not one.
    assert can_act_on(mod, admin_door) is False


def test_a_moderator_may_act_on_everything_that_is_actually_a_regular_user(
    mod, alice, bob
):
    # The complement of the three shields: an account with no flag at all is
    # the only thing a moderator can reach. Written as a loop so a future
    # flag added to the regular-user fixtures shows up here rather than
    # quietly narrowing the moderator's reach with nobody noticing.
    for target in (alice, bob):
        assert target.is_superuser is False
        assert target.is_moderator is False
        assert target.is_staff is False
        assert can_act_on(mod, target) is True


def test_a_moderator_may_not_act_on_a_superuser_who_lacks_the_admin_door(
    mod, bare_superuser
):
    # The admin half of the shield is not riding on is_staff. A superuser
    # with the door closed is still the admin, and still untouchable.
    assert bare_superuser.is_staff is False
    assert bare_superuser.is_superuser is True
    assert can_act_on(mod, bare_superuser) is False


def test_a_superuser_without_the_admin_door_may_still_act_on_anyone(
    bare_superuser, alice, mod, admin_door
):
    # And the actor half reads is_superuser too, not the door: this account
    # has the full admin power for moderation despite is_staff being False.
    for target in (alice, mod, admin_door):
        assert can_act_on(bare_superuser, target) is True


def test_the_site_admin_may_act_on_an_ordinary_member(siteadmin, alice):
    assert can_act_on(siteadmin, alice) is True


def test_the_site_admin_may_act_on_a_moderator(siteadmin, mod):
    assert can_act_on(siteadmin, mod) is True


def test_the_site_admin_may_act_on_an_account_holding_the_admin_door(
    siteadmin, admin_door
):
    assert can_act_on(siteadmin, admin_door) is True


def test_the_site_admin_may_act_on_their_own_content(siteadmin):
    # "The site admin acts on anyone" is literal, and this is the case that
    # pins the ordering inside can_act_on: the superuser check runs *before*
    # the self check. Swap those two lines and this is the only test that
    # goes red — which is the point of having it.
    assert can_act_on(siteadmin, siteadmin) is True


def test_a_plain_member_may_act_on_nobody(alice, bob, mod, siteadmin, admin_door):
    for target in (bob, mod, siteadmin, admin_door):
        assert can_act_on(alice, target) is False


def test_an_anonymous_visitor_may_act_on_nobody(alice, siteadmin):
    # AnonymousUser has no is_moderator, so this also proves the
    # is_authenticated check short-circuits rather than raising.
    assert can_act_on(AnonymousUser(), alice) is False
    assert can_act_on(AnonymousUser(), siteadmin) is False


# --- can_impose_severity: the two account severities (R114) -------------


def test_the_two_predicates_are_not_the_same_function(siteadmin):
    """The split itself is the thing worth pinning.

    R114 had to land as a *second* predicate rather than a change to
    ``can_act_on``, because the admin's answer to "may you moderate this
    account" stays yes — they delete reported posts and forward reports
    about it — while the answer to "may you suspend or ban it" is now
    never. Asserting both halves on one subject is the only way to show the
    rules actually diverge instead of one having swallowed the other.
    """
    assert can_act_on(siteadmin, siteadmin) is True
    assert can_impose_severity(siteadmin, siteadmin) is False


def test_a_second_superuser_cannot_suspend_or_ban_the_first(siteadmin, bare_superuser):
    """R114 binds superuser-to-superuser, not just moderator-to-admin.

    A moderator could never reach the admin anyway (R103b's bottom line),
    so the only actor the new rule actually constrains is another
    superuser — and ``bare_superuser`` has ``is_staff=False`` to prove the
    rule reads ``is_superuser`` and not the admin door.
    """
    assert can_impose_severity(bare_superuser, siteadmin) is False
    assert can_impose_severity(siteadmin, bare_superuser) is False


def test_a_moderator_still_cannot_impose_severity_on_anyone_they_could_not_before(
    mod, siteadmin, mod2, admin_door
):
    """R114 widens nothing. Every refusal ``can_act_on`` already issued
    against a privileged target still refuses the heavier verb."""
    for target in (siteadmin, mod2, admin_door, mod):
        assert can_act_on(mod, target) is False
        assert can_impose_severity(mod, target) is False


def test_a_moderator_may_still_impose_severity_on_a_regular_user(mod, alice):
    """The rule is about the admin, not a freeze on moderator reach."""
    assert can_impose_severity(mod, alice) is True


def test_the_admin_may_still_impose_severity_on_everyone_else(siteadmin, alice, mod):
    for target in (alice, mod):
        assert can_impose_severity(siteadmin, target) is True


def test_nobody_may_impose_severity_on_the_instance_representative(siteadmin, alice):
    """R113's shield survives the new one — and both hold for the same
    reason: an account that is infrastructure rather than a person must
    not be destroyed by the moderation system that depends on it."""
    from reeltalk.moderation.representative import instance_representative

    rep = instance_representative()
    assert can_act_on(siteadmin, rep) is False
    assert can_impose_severity(siteadmin, rep) is False
    assert can_impose_severity(alice, rep) is False


def test_can_impose_severity_refuses_a_missing_target(alice):
    assert can_impose_severity(alice, None) is False
    assert can_impose_severity(AnonymousUser(), None) is False


# --- the route refuses whom it must, and writes nothing -------------------


def test_a_member_cannot_delete_and_the_post_survives(bob, review):
    report = report_a_post(bob, review)
    resp = logged_in(bob).post(delete_url(report), {})
    assert resp.status_code == 403
    review.refresh_from_db()
    assert review.deleted is False
    assert "<p>Great film</p>" in review.content
    assert report.resolved_at is None


def test_anonymous_cannot_delete_and_is_sent_to_log_in(bob, review):
    report = report_a_post(bob, review)
    resp = Client().post(delete_url(report), {})
    assert resp.status_code == 302
    assert reverse("login") in resp.headers["Location"]
    review.refresh_from_db()
    assert review.deleted is False


def test_a_get_on_the_delete_route_deletes_nothing(bob, review, mod):
    report = report_a_post(bob, review)
    resp = logged_in(mod).get(delete_url(report))
    assert resp.status_code == 405
    review.refresh_from_db()
    assert review.deleted is False
    assert report.resolved_at is None


def test_a_moderator_cannot_delete_the_site_admins_post(bob, siteadmin, dune, mod):
    admin_post = Status.objects.create(
        user=siteadmin,
        film=dune,
        status_type="review",
        content="<p>admin review</p>",
        raw_content="admin review",
    )
    report = report_a_post(bob, admin_post)
    resp = logged_in(mod).post(delete_url(report), {"note": "trying anyway"})
    assert resp.status_code == 403
    admin_post.refresh_from_db()
    assert admin_post.deleted is False
    assert Report.unresolved().count() == 1, "a refused delete closed the report"


def test_a_moderator_cannot_delete_their_own_reported_post(bob, mod, dune):
    own = Status.objects.create(
        user=mod,
        film=dune,
        status_type="review",
        content="<p>mod's own post</p>",
        raw_content="mod's own post",
    )
    report = report_a_post(bob, own)
    resp = logged_in(mod).post(delete_url(report), {})
    assert resp.status_code == 403
    own.refresh_from_db()
    assert own.deleted is False


def test_a_moderator_cannot_delete_another_moderators_post(bob, mod2, dune, mod):
    # The peer shield has to hold at the route, not only in the predicate:
    # hiding the button is not the guarantee.
    peer_post = Status.objects.create(
        user=mod2,
        film=dune,
        status_type="review",
        content="<p>another moderator's post</p>",
        raw_content="another moderator's post",
    )
    report = report_a_post(bob, peer_post)
    resp = logged_in(mod).post(delete_url(report), {"note": "trying anyway"})
    assert resp.status_code == 403
    peer_post.refresh_from_db()
    assert peer_post.deleted is False
    assert Report.unresolved().count() == 1, "a refused delete closed the report"


def test_a_moderator_cannot_delete_an_account_holding_the_admin_doors_post(
    bob, admin_door, dune, mod
):
    door_post = Status.objects.create(
        user=admin_door,
        film=dune,
        status_type="review",
        content="<p>admin-door post</p>",
        raw_content="admin-door post",
    )
    report = report_a_post(bob, door_post)
    resp = logged_in(mod).post(delete_url(report), {})
    assert resp.status_code == 403
    door_post.refresh_from_db()
    assert door_post.deleted is False
    assert Report.unresolved().count() == 1, "a refused delete closed the report"


def test_the_site_admin_can_delete_a_moderators_post(bob, siteadmin, mod, dune):
    mod_post = Status.objects.create(
        user=mod,
        film=dune,
        status_type="review",
        content="<p>mod post</p>",
        raw_content="mod post",
    )
    report = report_a_post(bob, mod_post)
    resp = logged_in(siteadmin).post(delete_url(report), {"note": "removed"})
    assert resp.status_code == 302
    mod_post.refresh_from_db()
    assert mod_post.deleted is True
    assert Report.objects.get(pk=report.pk).resolved_by_id == siteadmin.pk


def test_deleting_an_unknown_report_404s(mod):
    resp = logged_in(mod).post(reverse("moderation-delete-status", args=[999999]), {})
    assert resp.status_code == 404


def test_a_profile_report_has_no_post_to_delete(bob, alice, mod):
    logged_in(bob).post(
        reverse("report-user", args=[alice.localname]), report_payload()
    )
    report = Report.objects.get(target_user=alice)
    assert report.target_status is None
    resp = logged_in(mod).post(delete_url(report), {})
    assert resp.status_code == 404
    assert Report.unresolved().count() == 1


def test_an_already_deleted_post_is_not_deleted_a_second_time(bob, review, mod):
    report = report_a_post(bob, review)
    review.delete()
    resp = logged_in(mod).post(delete_url(report), {})
    assert resp.status_code == 404
    report.refresh_from_db()
    assert report.resolved_at is None, "a no-op delete closed the report"


def test_a_resolved_report_is_not_a_live_delete_handle(bob, review, mod):
    # A report that has already been decided is history, not a work item,
    # and must not stay a way to destroy a post indefinitely.
    report = report_a_post(bob, review)
    dismiss_report(report, by_user=mod, note="was fine")
    resp = logged_in(mod).post(delete_url(report), {})
    assert resp.status_code == 404
    review.refresh_from_db()
    assert review.deleted is False
    assert Report.objects.get(pk=report.pk).action == "dismiss"


def test_a_report_on_an_already_deleted_post_can_still_be_dismissed(bob, review, mod):
    # The 404 above must not dead-end the card: dismiss still closes it.
    report = report_a_post(bob, review)
    review.delete()
    resp = logged_in(mod).post(
        reverse("moderation-dismiss", args=[report.id]), {"note": "already gone"}
    )
    assert resp.status_code == 302
    report.refresh_from_db()
    assert report.resolved_at is not None
    assert report.action == "dismiss"


# --- the delete itself ---------------------------------------------------


def test_the_post_is_soft_deleted_and_its_content_cleared(bob, review, mod):
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {"note": "spam review"})
    review.refresh_from_db()
    assert review.deleted is True
    assert review.content == ""
    assert review.raw_content == ""
    assert review.deleted_date is not None
    # The row is kept: a hard delete would break the report's FK and the
    # stable wire identity the Delete activity was built from.
    assert Status.objects.filter(pk=review.pk).exists()


def test_the_report_records_delete_status_who_and_when(bob, review, mod):
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {"note": "clearly spam"})
    report.refresh_from_db()
    assert report.resolved_at is not None
    assert report.resolved_by_id == mod.pk
    assert report.action == "delete_status"
    assert report.note == "clearly spam"


def test_the_recorded_action_is_delete_status_not_dismiss(bob, review, mod):
    # Distinct values, so an audit read can tell the two outcomes apart.
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {})
    report.refresh_from_db()
    assert report.action != Report.Action.DISMISS
    assert report.action == Report.Action.DELETE_STATUS


def test_deleting_resolves_the_whole_pile_as_deleted(bob, rob, review, mod):
    report_a_post(bob, review, "first words")
    report_a_post(rob, review, "second words")
    assert Report.unresolved().count() == 2
    logged_in(mod).post(delete_url(Report.objects.first()), {"note": "spam, all of it"})
    assert Report.unresolved().count() == 0
    for row in Report.objects.all():
        assert row.action == "delete_status"
        assert row.resolved_by_id == mod.pk
        assert row.note == "spam, all of it"


def test_a_note_is_optional_on_a_deletion(bob, review, mod):
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {})
    report.refresh_from_db()
    assert report.note == ""
    assert report.action == "delete_status"


def test_a_deletion_writes_no_notification(bob, review, mod):
    # R99 still holds: acting on a report is not a notification either.
    report = report_a_post(bob, review)
    before = Notification.objects.count()
    logged_in(mod).post(delete_url(report), {})
    assert Notification.objects.count() == before


# --- federation ----------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_local_authors_delete_is_broadcast_to_their_remote_followers():
    alice = member(localname="alice", password=PASSWORD)
    bob = member(localname="bob", password=PASSWORD)
    mod = member(localname="mod", password=PASSWORD, is_moderator=True)
    follower = mirror_account("dana", "remote.example")
    follower.follows.add(alice)
    film = Film.objects.create(title="Dune", year=2021)
    status = Status.objects.create(
        user=alice,
        film=film,
        status_type="review",
        content="<p>Great film</p>",
        raw_content="Great film",
    )
    report = report_a_post(bob, status)
    responses.add(responses.POST, REMOTE_INBOX)
    resp = logged_in(mod).post(delete_url(report), {"note": "spam"})
    assert resp.status_code == 302
    assert len(responses.calls) == 1
    sent = json.loads(responses.calls[0].request.body)
    assert sent["type"] == "Delete"
    assert sent["object"]["id"] == f"http://testserver/status/{status.pk}/"


@responses.activate
@pytest.mark.django_db
def test_the_delete_is_signed_as_the_author_not_the_moderator():
    """The trap this increment was built around, pinned on the wire.

    A moderator's delete must tombstone the author's post and go out as the
    author. Signed as the moderator instead, peers would see a stranger
    claiming to delete someone else's Note, and the author's followers would
    be receiving it from an actor they never subscribed to.
    """
    alice = member(localname="alice", password=PASSWORD)
    bob = member(localname="bob", password=PASSWORD)
    mod = member(localname="mod", password=PASSWORD, is_moderator=True)
    follower = mirror_account("dana", "remote.example")
    follower.follows.add(alice)
    film = Film.objects.create(title="Dune", year=2021)
    status = Status.objects.create(
        user=alice,
        film=film,
        status_type="review",
        content="<p>Great film</p>",
        raw_content="Great film",
    )
    report = report_a_post(bob, status)
    responses.add(responses.POST, REMOTE_INBOX)
    logged_in(mod).post(delete_url(report), {})

    assert len(responses.calls) == 1
    call = responses.calls[0]
    sent = json.loads(call.request.body)
    # The activity's actor is the post's author...
    assert sent["actor"] == "http://testserver/user/alice/"
    # ...and so is the signing key, which is the half a peer actually checks.
    signature_input = call.request.headers["Signature-Input"]
    assert "user/alice/#main-key" in signature_input
    assert "user/mod" not in signature_input


@responses.activate
@pytest.mark.django_db
def test_a_mirrored_authors_post_is_removed_here_without_a_broadcast(remote_follower):
    mirror = mirror_account("dana", "remote.example")
    film = Film.objects.create(title="Dune", year=2021)
    status = mirror_post(mirror, film, note_id=993)
    # The mirror's own followers are non-local, so had the broadcast run it
    # would be *recorded* here. Zero calls is the proof it never did.
    remote_follower.follows.add(mirror)
    bob = member(localname="bob", password=PASSWORD)
    mod = member(localname="mod", password=PASSWORD, is_moderator=True)
    report = report_a_post(bob, status)
    responses.add(responses.POST, REMOTE_INBOX)
    responses.add(responses.POST, OTHER_INBOX)
    resp = logged_in(mod).post(delete_url(report), {"note": "removed here"})
    assert resp.status_code == 302
    assert len(responses.calls) == 0, "broadcast a Delete we cannot sign as"
    status.refresh_from_db()
    assert status.deleted is True
    assert Report.objects.get(pk=report.pk).action == "delete_status"


@responses.activate
@pytest.mark.django_db
def test_broadcasting_a_mirrored_delete_raises_without_the_locality_guard(
    remote_follower,
):
    """The hazard the locality check exists for, pinned at the primitive.

    A mirror's ``private_key`` is empty by construction, so the signer has
    nothing to sign with and the key loader raises ``ValueError``. That is
    **not** a ``RequestException``, so the delivery loop's except-clause
    cannot swallow it — an unconditional broadcast from the view would be a
    500 with the post already deleted and the report already closed. This
    test exists so that "simplifying" the view by dropping
    ``if status.user.local`` lands on a red test that says why.
    """
    mirror = mirror_account("dana", "remote.example")
    remote_follower.follows.add(mirror)
    film = Film.objects.create(title="Dune", year=2021)
    status = mirror_post(mirror, film, note_id=994)
    responses.add(responses.POST, REMOTE_INBOX)
    status.delete()
    request = RequestFactory().get("/")
    with pytest.raises(ValueError):
        broadcast_status_delete(request, status)


@responses.activate
@pytest.mark.django_db
def test_an_unreachable_follower_does_not_lose_the_deletion():
    alice = member(localname="alice", password=PASSWORD)
    bob = member(localname="bob", password=PASSWORD)
    mod = member(localname="mod", password=PASSWORD, is_moderator=True)
    follower = mirror_account("dana", "remote.example")
    follower.follows.add(alice)
    film = Film.objects.create(title="Dune", year=2021)
    status = Status.objects.create(
        user=alice,
        film=film,
        status_type="review",
        content="<p>Great film</p>",
        raw_content="Great film",
    )
    report = report_a_post(bob, status)
    responses.add(
        responses.POST, REMOTE_INBOX, body=requests.exceptions.ConnectTimeout("boom")
    )
    resp = logged_in(mod).post(delete_url(report), {"note": "dead peer"})
    assert resp.status_code == 302
    status.refresh_from_db()
    assert status.deleted is True
    assert Report.objects.get(pk=report.pk).resolved_at is not None


# --- the read sites: the post is actually gone ----------------------------


def test_the_deleted_post_is_gone_from_the_home_feed(
    alice, bob, review, mod, siteadmin
):
    # ``siteadmin`` is here for the setup gate, not the assertion: with no
    # superuser at all the home page redirects to /setup/ and every feed
    # assertion would be made against an empty 302 body.
    bob.follows.add(alice)
    assert "Great film" in logged_in(bob).get(reverse("index")).content.decode()
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {})
    assert "Great film" not in logged_in(bob).get(reverse("index")).content.decode()


def test_the_deleted_review_is_gone_from_the_film_page(bob, dune, review, mod):
    film_url = reverse("film", args=[dune.id])
    assert "Great film" in Client().get(film_url).content.decode()
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {})
    assert "Great film" not in Client().get(film_url).content.decode()
    # The film page itself still renders — the review left, the page did not.
    assert Client().get(film_url).status_code == 200


def test_the_deleted_post_is_gone_from_the_authors_films_tab(bob, review, mod):
    author = review.user
    url = reverse("user-films", args=[author.localname])
    assert "Dune" in logged_in(author).get(url).content.decode()
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {})
    assert "Dune" not in logged_in(author).get(url).content.decode()


def test_the_deleted_post_is_gone_from_the_outbox(bob, review, mod):
    author = review.user
    url = reverse("ap-outbox", args=[author.localname])
    note_url = f"http://testserver/status/{review.pk}/"
    assert note_url in logged_in(author).get(url, {"page": 1}).content.decode()
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {})
    assert note_url not in logged_in(author).get(url, {"page": 1}).content.decode()


def test_the_post_page_404s_after_the_delete(bob, review, mod):
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {})
    assert Client().get(reverse("status", args=[review.id])).status_code == 404


# --- the queue card ------------------------------------------------------


def test_the_delete_button_renders_for_a_moderator(bob, review, mod):
    report = report_a_post(bob, review)
    body = logged_in(mod).get(reverse("moderation")).content.decode()
    assert f'formaction="/moderate/{report.id}/delete-status/"' in body
    assert "Delete post" in body


def test_the_destructive_action_takes_the_lit_control_and_dismiss_the_plain_one(
    bob, review, mod
):
    # §2D's .btn trap: two severities must not render identically.
    report_a_post(bob, review)
    body = logged_in(mod).get(reverse("moderation")).content.decode()
    assert '<button type="submit">Dismiss</button>' in body
    assert '<button type="submit" class="btn">Dismiss</button>' not in body
    assert 'class="btn" formaction=' in body


def test_no_delete_button_for_a_post_the_moderator_may_not_touch(
    bob, siteadmin, dune, mod
):
    admin_post = Status.objects.create(
        user=siteadmin,
        film=dune,
        status_type="review",
        content="<p>admin review</p>",
        raw_content="admin review",
    )
    report_a_post(bob, admin_post)
    body = logged_in(mod).get(reverse("moderation")).content.decode()
    # The card is still there and still dismissible; only the delete is gone.
    assert "@root" in body
    assert "delete-status" not in body
    assert '<button type="submit">Dismiss</button>' in body


def test_no_delete_button_for_a_peer_moderators_post(bob, mod2, dune, mod):
    # Same card shape as the admin case: visible, dismissible, and with no
    # way to delete. A moderator browsing the queue should be able to see
    # what was reported about a peer without being offered the axe.
    peer_post = Status.objects.create(
        user=mod2,
        film=dune,
        status_type="review",
        content="<p>peer review</p>",
        raw_content="peer review",
    )
    report_a_post(bob, peer_post)
    body = logged_in(mod).get(reverse("moderation")).content.decode()
    assert "@mod2" in body
    assert "delete-status" not in body
    assert '<button type="submit">Dismiss</button>' in body


def test_no_delete_button_for_an_account_holding_the_admin_doors_post(
    bob, admin_door, dune, mod
):
    report_a_post(
        bob,
        Status.objects.create(
            user=admin_door,
            film=dune,
            status_type="review",
            content="<p>admin-door review</p>",
            raw_content="admin-door review",
        ),
    )
    body = logged_in(mod).get(reverse("moderation")).content.decode()
    assert "@doorkeeper" in body
    assert "delete-status" not in body
    assert '<button type="submit">Dismiss</button>' in body


def test_no_delete_button_when_the_post_is_already_gone(bob, review, mod):
    report_a_post(bob, review)
    review.delete()
    body = logged_in(mod).get(reverse("moderation")).content.decode()
    assert "has since been deleted" in body
    assert "delete-status" not in body


def test_the_card_shows_the_reporters_comment_before_the_delete(bob, review, mod):
    report_a_post(bob, review, "affiliate link")
    body = logged_in(mod).get(reverse("moderation")).content.decode()
    assert "affiliate link" in body
    assert "Great film" in body


def test_the_queue_is_empty_after_the_delete(bob, review, mod):
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {})
    body = logged_in(mod).get(reverse("moderation")).content.decode()
    assert "Nothing to review" in body
    assert "Great film" not in body


def test_a_mirrored_post_offers_a_here_only_delete(bob, mod):
    mirror = mirror_account("dana", "remote.example")
    film = Film.objects.create(title="Dune", year=2021)
    status = mirror_post(mirror, film, note_id=995)
    report_a_post(bob, status)
    body = logged_in(mod).get(reverse("moderation")).content.decode()
    assert "removes it here only" in body
    assert "delete-status" in body


# --- the whole loop, with the session proven ------------------------------


def test_the_whole_loop_member_reports_and_moderator_deletes(alice, bob, review, mod):
    """The increment's exit bar, each session proved live."""
    reporter = logged_in(bob)
    assert reporter.get(reverse("status", args=[review.id])).status_code == 200
    assert "sessionid" in reporter.cookies
    reporter.post(reverse("report-status", args=[review.id]), report_payload())
    assert Report.objects.count() == 1

    moderator = logged_in(mod)
    assert "sessionid" in moderator.cookies
    report = Report.objects.get()
    assert "Great film" in moderator.get(reverse("moderation")).content.decode()
    assert moderator.post(delete_url(report), {"note": "spam"}).status_code == 302

    review.refresh_from_db()
    assert review.deleted is True
    report.refresh_from_db()
    assert report.action == "delete_status"
    assert report.resolved_by_id == mod.pk
    assert "Nothing to review" in moderator.get(reverse("moderation")).content.decode()
    # The member who reported still cannot open the queue.
    assert reporter.get(reverse("moderation")).status_code == 403


def test_a_real_login_session_deletes_from_the_queue(review, mod, bob):
    """A real login POST rather than ``force_login``, then the delete.

    The login form takes ``username`` and the CSRF token rotates at login,
    so the token used for the delete is the one read *after* the login
    response. This is the path a browser actually takes.
    """
    client = Client()
    client.get(reverse("login"))
    before_token = client.cookies["csrftoken"].value
    resp = client.post(
        reverse("login"),
        {"username": "mod", "password": PASSWORD, "next": reverse("moderation")},
    )
    assert resp.status_code == 302
    assert client.session["_auth_user_id"] == str(mod.pk)
    assert "sessionid" in client.cookies
    assert client.cookies["csrftoken"].value != before_token

    report = report_a_post(bob, review)
    assert (
        client.post(delete_url(report), {"note": "cleared on review"}).status_code
        == 302
    )
    review.refresh_from_db()
    assert review.deleted is True
    assert Report.unresolved().count() == 0


def test_reporting_is_not_moderation(alice, bob, review):
    # The reporter's own session stays refused on both verbs.
    report = report_a_post(bob, review)
    assert logged_in(bob).post(delete_url(report), {}).status_code == 403
    assert (
        logged_in(bob)
        .post(reverse("moderation-dismiss", args=[report.id]), {})
        .status_code
        == 403
    )
    review.refresh_from_db()
    assert review.deleted is False


def test_dismiss_still_works_on_a_card_that_could_have_been_deleted(bob, review, mod):
    # The two verbs sharing one card must not interfere with each other.
    report = report_a_post(bob, review)
    assert (
        logged_in(mod)
        .post(reverse("moderation-dismiss", args=[report.id]), {"note": "fine"})
        .status_code
        == 302
    )
    report.refresh_from_db()
    assert report.action == "dismiss"
    review.refresh_from_db()
    assert review.deleted is False


def test_file_report_still_dedups_after_a_delete(bob, review, mod):
    # R107's dedup is not scoped to open reports: a deleted-and-resolved
    # report still blocks a re-file, so a member cannot push it back.
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {})
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    assert Report.objects.filter(reporter=bob, target_status=review).count() == 1
    assert Report.objects.get().action == "delete_status"


def test_a_deleted_status_cannot_be_reported_again(bob, review, mod):
    # report_status excludes tombstones, so the delete also closes the
    # re-report path rather than leaving a form that files nothing.
    report = report_a_post(bob, review)
    logged_in(mod).post(delete_url(report), {})
    resp = logged_in(bob).post(
        reverse("report-status", args=[review.id]), report_payload()
    )
    assert resp.status_code == 404


def test_file_report_helper_returns_no_status_for_a_member_report(bob, alice, mod):
    # The helper's None branch is what makes the view's 404 reachable, so
    # it is pinned at the helper rather than only inferred from the route.
    report, created = file_report(
        reporter=bob, target_user=alice, category="spam", comment="bot"
    )
    assert created is True
    status, count = delete_reported_status(report, by_user=mod)
    assert status is None
    assert count == 0
    assert Report.objects.get(pk=report.pk).resolved_at is None
