"""Staff email notification for new reports (2E).

Every test here exists to pin one of the six traps or one of the settled shape
decisions (D-c, D-d, D-e, D-f, R115, R116). Read in that light rather than as
a list of assertions.

The three that matter most:

* ``test_filing_a_report_does_not_send_anything_from_the_request`` — D-e. The
  send is not in the request, so a dead SMTP server cannot roll back a report
  or slow a submit. Proven by the outbox being empty *at the moment of filing*.
* ``test_the_console_backend_warning_fires_when_staff_email_is_enabled`` —
  trap 1. The console backend prints to stdout and reports success, so a
  misconfigured deploy is otherwise indistinguishable from a working one.
* ``test_a_reporter_who_blocked_every_moderator_still_triggers_the_email`` —
  R99's whole reason for existing, shown absent here by construction. The
  recipient set is permission-derived and sends to addresses, so a block
  cannot mute the queue.

**Mock-only vs live.** Everything in this file runs against Django's locmem
mail backend. It proves the *recipient set, the dedup, the opt-out, the
exclusions and the enqueue*. It does not prove that a real mail server accepts
what we hand it — that is the live proof, done separately with real SMTP
credentials, and nothing here should be read as covering it.
"""

import logging

import pytest
from django.core import mail
from django.test import TestCase, override_settings
from django_q.models import OrmQ, SignedPackage

from reeltalk.core.models import Film, Status
from reeltalk.moderation.decorators import may_moderate
from reeltalk.moderation.models import (
    Report,
    dismiss_report,
    file_remote_report,
    file_report,
)
from reeltalk.moderation.notify import (
    build_report_email,
    console_backend_active,
    notify_staff_of_report,
    staff_email_recipients,
)
from reeltalk.moderation.tasks import SEND_FUNC, send_report_email
from reeltalk.notifications.models import Notification
from reeltalk.social.admin import UserAdmin
from reeltalk.social.models import User

NOTIFY_LOGGER = "reeltalk.moderation.notify"

CONSOLE_MAILERS = {
    "default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"}
}
SMTP_MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "OPTIONS": {
            "host": "smtp.example.test",
            "port": 587,
            "username": "u",
            "password": "p",
            "use_tls": True,
        },
    }
}


# --- fixtures ------------------------------------------------------------


@pytest.fixture
def member(db):
    """A plain member who files the reports."""
    return User.objects.create_user(
        localname="witness", password="s3cretpass", email="witness@example.test"
    )


@pytest.fixture
def accused(db):
    """The account being reported."""
    return User.objects.create_user(
        localname="accused", password="s3cretpass", email="accused@example.test"
    )


@pytest.fixture
def mod(db):
    """A moderator with an address. is_staff stays False, per R100."""
    return User.objects.create_user(
        localname="warden",
        password="s3cretpass",
        email="warden@example.test",
        is_moderator=True,
    )


@pytest.fixture
def mod2(db):
    return User.objects.create_user(
        localname="deputy",
        password="s3cretpass",
        email="deputy@example.test",
        is_moderator=True,
    )


@pytest.fixture
def film(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def review(accused, film):
    return Status.objects.create(
        user=accused,
        film=film,
        status_type="review",
        content="<p>ZIGGURAT-POST-BODY</p>",
        raw_content="ZIGGURAT-POST-BODY",
    )


def file_against_status(reporter, status, comment="PLAINTIFF-COMMENT"):
    return file_report(
        reporter=reporter,
        target_status=status,
        category="spam",
        comment=comment,
    )


def make_peer(handle, **extra):
    """A remote mirror that can actually be stored.

    ``unique_actor_url_for_remote_mirrors`` forbids two non-local users both
    carrying an empty ``actor_url``, so every peer here gets a distinct one.
    Without it the second peer in a test dies on a UniqueViolation that has
    nothing to do with what the test is about.
    """
    return User.objects.create_user(
        localname=handle,
        password="s3cretpass",
        local=False,
        actor_url=f"https://{handle.rsplit('@', 1)[-1]}/actors/{handle.split('@')[0]}",
        **extra,
    )


# --- R115: the default ----------------------------------------------------


def test_report_email_defaults_on_for_a_new_member(member):
    """R115. The moderator grant is the consent, so nobody has to turn it on."""
    assert member.report_email is True


def test_report_email_defaults_on_for_a_moderator(mod):
    assert mod.report_email is True


def test_report_email_defaults_on_for_a_superuser(db):
    admin = User.objects.create_superuser(localname="chief", password="s3cretpass")
    assert admin.report_email is True


def test_the_default_lives_on_the_field_not_in_the_send_path(db):
    """R115's mechanism, pinned at the field.

    The point of putting the default on the model field rather than in the
    notify path is that *every* creation route lands on the same value —
    signup, the setup wizard, the admin form, a fixture, a bare
    ``objects.create()``. A bare create is the least-documented route and the
    one a send-path default would miss, so it is the one tested here.
    """
    field = User._meta.get_field("report_email")
    assert field.default is True
    bare = User.objects.create(localname="bare", email="bare@example.test")
    assert bare.report_email is True


# --- D-f: the recipient set ----------------------------------------------


def test_recipients_are_the_moderators_with_the_flag_and_an_address(
    member, accused, review, mod
):
    report, _ = file_against_status(member, review)
    assert staff_email_recipients(report) == [mod]


def test_a_plain_member_is_never_a_recipient(member, accused, review):
    report, _ = file_against_status(member, review)
    assert staff_email_recipients(report) == []


def test_staff_and_superuser_qualify_because_may_moderate_says_so(
    member, accused, review, db
):
    """Not a separate list — the same predicate that gates ``/moderate/``."""
    staff = User.objects.create_user(
        localname="staffer",
        password="s3cretpass",
        email="s@example.test",
        is_staff=True,
    )
    root = User.objects.create_superuser(
        localname="rooty", password="s3cretpass", email="r@example.test"
    )
    report, _ = file_against_status(member, review)
    got = {u.pk for u in staff_email_recipients(report)}
    assert got == {staff.pk, root.pk}
    assert may_moderate(staff) and may_moderate(root)


def test_the_recipient_set_never_diverges_from_may_moderate(
    member, accused, review, mod, mod2, db
):
    """Cross-checks D-f across every role/opt/address combination.

    If someone later adds a fourth flag to ``may_moderate`` and forgets this
    path, or gives the notify path its own idea of who counts, this goes red
    rather than quietly dropping a moderator.
    """
    no_mail = User.objects.create_user(
        localname="nomail", password="s3cretpass", is_moderator=True
    )
    opted_out = User.objects.create_user(
        localname="quiet",
        password="s3cretpass",
        email="quiet@example.test",
        is_moderator=True,
        report_email=False,
    )
    staff = User.objects.create_user(
        localname="staffer",
        password="s3cretpass",
        email="s@example.test",
        is_staff=True,
    )
    everyone = [member, accused, mod, mod2, no_mail, opted_out, staff]
    report, _ = file_against_status(member, review)
    got = {u.pk for u in staff_email_recipients(report)}
    expected = {
        u.pk
        for u in everyone
        if may_moderate(u) and u.report_email and u.email and u.pk != member.pk
    }
    assert got == expected


def test_the_reporter_is_excluded_even_when_they_are_a_moderator(accused, review, mod):
    """Trap 4. Moderators are members and file reports, so this is reachable."""
    report, _ = file_against_status(mod, review)
    assert report.reporter_id == mod.pk
    assert staff_email_recipients(report) == []


def test_a_moderator_with_no_email_address_is_skipped(accused, review, db):
    """Trap 2. ``email`` is ``blank=True``; the query must not send to ''."""
    User.objects.create_user(
        localname="ghost", password="s3cretpass", is_moderator=True
    )
    report, _ = file_against_status(
        User.objects.create_user(localname="filer", password="s3cretpass"), review
    )
    assert staff_email_recipients(report) == []


def test_a_moderator_who_opted_out_is_skipped(member, accused, review, mod, mod2):
    mod.report_email = False
    mod.save(update_fields=["report_email"])
    report, _ = file_against_status(member, review)
    assert staff_email_recipients(report) == [mod2]


# NOTE — 2E trap 3 retired by R118 (2F-1, 2026-09-30).
#
# This file used to carry ``test_two_staff_sharing_one_address_are_two_recipients``,
# which put two moderators on one address and asserted both got mailed. Its
# job was a tripwire against the recipient query deduping by address — a real
# hazard while ``email`` had no uniqueness rule, because two people in one
# mailbox would have looked like one recipient.
#
# R118 put a partial unique index on ``email``, so the state that test built
# cannot be created at all. The trap is now closed by the schema rather than
# handled by the code, which is a stronger position than the tripwire was.
# What still needs proving — that delivery is per-account, one message per
# moderator — is covered by
# ``test_the_recipient_set_never_diverges_from_may_moderate``, which uses two
# moderators on two distinct addresses. Recorded here so the missing test
# reads as a retired premise rather than an oversight.


# --- D-c: the dedup -------------------------------------------------------


def test_the_second_report_on_the_same_target_notifies_nobody(member, mod, review, db):
    """N members reporting one spammer produce one staff alert, not N."""
    carol = User.objects.create_user(
        localname="carol", password="s3cretpass", email="c@example.test"
    )
    with TestCase.captureOnCommitCallbacks(execute=True):
        first, _ = file_against_status(member, review)
    assert OrmQ.objects.count() == 1
    with TestCase.captureOnCommitCallbacks(execute=True):
        second, _ = file_against_status(carol, review)
    assert first.pk != second.pk
    assert OrmQ.objects.count() == 1
    assert (
        notify_staff_of_report(second)["reason"] == "target-already-has-an-open-report"
    )


def test_the_dedup_is_checked_before_enqueueing_not_inside_the_task(
    member, mod, review, db
):
    """D-c, specifically about *where* the check runs.

    If the check lived inside the task, a burst of N reports would put N
    tasks on the cluster before any of them ran and saw the siblings.
    Asserted here as: after the second filing, with the commit callbacks
    run, the queue still holds exactly one task.
    """
    carol = User.objects.create_user(
        localname="carol", password="s3cretpass", email="c@example.test"
    )
    with TestCase.captureOnCommitCallbacks(execute=True):
        file_against_status(member, review)
    assert OrmQ.objects.count() == 1
    with TestCase.captureOnCommitCallbacks(execute=True):
        file_against_status(carol, review)
    assert OrmQ.objects.count() == 1


def test_a_resolved_sibling_does_not_suppress_a_new_report(member, mod, review, db):
    """The dedup is over *unresolved* siblings, matching ``unresolved_siblings?``.

    Once the pile is closed, a fresh report on the same target is new work
    and staff are told again.
    """
    first, _ = file_against_status(member, review)
    dismiss_report(first, by_user=mod)
    carol = User.objects.create_user(
        localname="carol", password="s3cretpass", email="c@example.test"
    )
    second, _ = file_against_status(carol, review)
    assert notify_staff_of_report(second)["enqueued"] == 1


def test_a_report_on_a_different_target_still_notifies(member, mod, review, db):
    other_person = User.objects.create_user(
        localname="other", password="s3cretpass", email="o@example.test"
    )
    other_film = Film.objects.create(title="Alien", year=1979)
    other_review = Status.objects.create(
        user=other_person,
        film=other_film,
        status_type="review",
        content="<p>another</p>",
        raw_content="another",
    )
    file_against_status(member, review)
    second, _ = file_against_status(
        User.objects.create_user(localname="zoe", password="s3cretpass"), other_review
    )
    assert notify_staff_of_report(second)["enqueued"] == 1


def test_a_profile_report_and_a_status_report_are_different_targets(
    member, mod, accused, review
):
    """Consistent with ``target_key``: the card is the unit of work.

    A report about a person and a report about that person's post are
    different work, so neither suppresses the other — the same grouping
    ``dismiss_report`` uses.
    """
    file_against_status(member, review)
    profile_report, _ = file_report(
        reporter=User.objects.create_user(localname="kim", password="s3cretpass"),
        target_user=accused,
        category="other",
    )
    assert notify_staff_of_report(profile_report)["enqueued"] == 1


# --- D-e: enqueued, never inline ------------------------------------------


def test_filing_a_report_does_not_send_anything_from_the_request(member, mod, review):
    """D-e. The send is not in the request that filed the report.

    With the commit callbacks left un-run, the outbox is empty. That is the
    whole property: a dead SMTP server cannot roll back a report, slow the
    submit, or surface an error to the member who filed, because the send
    is not on this path at all.
    """
    file_against_status(member, review)
    assert mail.outbox == []
    assert OrmQ.objects.count() == 0


def test_the_commit_callback_enqueues_rather_than_sends(member, mod, mod2, review):
    """Stage two: queued on the cluster, still nothing sent.

    ``OrmQ`` holds the signed packages; ``mail.outbox`` is still empty
    because no worker has run them. This is the distinction the whole
    increment turns on — enqueued is not delivered.
    """
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review)
    assert mail.outbox == []
    assert OrmQ.objects.count() == 2
    for pkg in [SignedPackage.loads(q.payload) for q in OrmQ.objects.all()]:
        assert pkg["func"] == SEND_FUNC
        assert pkg["args"][0] == report.pk
        assert pkg["args"][1] in (mod.pk, mod2.pk)
        assert pkg["args"][1] != member.pk


def test_one_task_per_recipient_not_one_task_for_the_lot(member, mod, mod2, review):
    """A single looping task would let one bad address abort the others."""
    with TestCase.captureOnCommitCallbacks(execute=True):
        file_against_status(member, review)
    assert OrmQ.objects.count() == 2


def test_a_rolled_back_report_enqueues_nothing(member, mod, review):
    """The on_commit hook means a report that never committed sends no mail."""
    from django.db import transaction

    try:
        with transaction.atomic():
            file_against_status(member, review)
            raise RuntimeError("simulated rollback")
    except RuntimeError:
        pass
    assert Report.objects.count() == 0
    assert OrmQ.objects.count() == 0
    assert mail.outbox == []


# --- the send itself, and what the message carries -------------------------


def test_the_task_sends_one_message_per_recipient(member, mod, mod2, review):
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review)
    for recipient in (mod, mod2):
        send_report_email(report.pk, recipient.pk)
    assert sorted(mail.outbox[0].to + mail.outbox[1].to) == sorted(
        [mod.email, mod2.email]
    )


def test_the_email_is_a_pointer_and_not_a_mirror(member, mod, review):
    """Trap 5. Link, target, category, count — not the reported content.

    The reported post's text and the reporter's comment are deliberately
    absent: this mail may be forwarded somewhere that should not be carrying
    the accusation, and the recipient can read both in one click.
    """
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review)
    message = build_report_email(report, mod)
    body = message.body
    assert "/moderate/" in body
    assert "@accused" in body
    assert "Spam" in body
    assert "ZIGGURAT-POST-BODY" not in body
    assert "PLAINTIFF-COMMENT" not in message.subject
    assert "ZIGGURAT-POST-BODY" not in message.subject


def test_the_email_names_the_reporter_so_staff_know_its_origin(member, mod, review):
    """A member report and a peer-server report look different, on purpose."""
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review)
    assert "@witness" in build_report_email(report, mod).body


def test_the_open_count_reflects_the_pile_at_send_time(member, mod, review, db):
    """The count earns its place because of the dedup.

    Only the first filing notifies, so a count computed at send time can be
    greater than one — it tells the moderator how many people complained
    while the alert was still in the queue.
    """
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review)
    carol = User.objects.create_user(
        localname="carol", password="s3cretpass", email="c@example.test"
    )
    file_against_status(carol, review)
    assert Report.unresolved_for_target(report.target_key).count() == 2
    assert "\nOpen reports on this target : 2" in build_report_email(report, mod).body


# --- trap 6: the audit line ----------------------------------------------


def test_the_send_records_an_audit_line_on_the_report(member, mod, review):
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review)
    send_report_email(report.pk, mod.pk)
    report.refresh_from_db()
    assert "Staff email sent to @warden." in report.note


def test_the_audit_line_is_appended_to_an_existing_note(member, mod, review):
    """The audit line must not clobber what is already on the row.

    The reporter's own words live in ``Report.comment``, not in ``note``,
    so "appended, not replacing" is tested against a pre-existing *note* —
    a forward outcome already recorded on the same pile. Both lines must
    survive, and the comment must be untouched by any of it.
    """
    from reeltalk.moderation.models import record_forward_outcome

    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review, comment="MY ORIGINAL COMMENT")
    record_forward_outcome(report, "Forwarded to other.test.")
    send_report_email(report.pk, mod.pk)
    report.refresh_from_db()
    assert "Forwarded to other.test." in report.note
    assert "Staff email sent to @warden." in report.note
    assert report.comment == "MY ORIGINAL COMMENT"


def test_the_audit_line_names_the_handle_not_the_address(member, mod, review):
    """The queue shows this note to every moderator; it is not an address book."""
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review)
    send_report_email(report.pk, mod.pk)
    report.refresh_from_db()
    assert mod.email not in report.note


def test_the_audit_line_lands_on_the_whole_open_pile(member, mod, review, db):
    """Same target-scoping as ``record_forward_outcome`` — the pile is the unit."""
    with TestCase.captureOnCommitCallbacks(execute=True):
        first, _ = file_against_status(member, review)
    carol = User.objects.create_user(
        localname="carol", password="s3cretpass", email="c@example.test"
    )
    second, _ = file_against_status(carol, review)
    send_report_email(first.pk, mod.pk)
    second.refresh_from_db()
    assert "Staff email sent to @warden." in second.note


def test_a_failure_is_recorded_and_reraised_so_the_task_row_is_red(
    member, mod, review, monkeypatch
):
    """Trap 6: swallowing the error would make a green task out of nothing sent."""
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review)

    def boom(self):
        raise OSError("smtp down")

    monkeypatch.setattr(mail.EmailMessage, "send", boom)
    with pytest.raises(OSError):
        send_report_email(report.pk, mod.pk)
    report.refresh_from_db()
    assert "FAILED" in report.note
    assert "smtp down" in report.note
    assert "Staff email sent" not in report.note


# --- trap 1: the console backend ------------------------------------------


def test_the_console_backend_is_detected(db):
    with override_settings(MAILERS=CONSOLE_MAILERS):
        assert console_backend_active() is True
    with override_settings(MAILERS=SMTP_MAILERS):
        assert console_backend_active() is False


def test_the_console_backend_warning_fires_when_staff_email_is_enabled(
    member, mod, review, caplog
):
    """Trap 1, the one that matters most.

    Printing to stdout IS a successful send as far as Django is concerned,
    so the queued task goes green while delivering nothing. Without this
    warning an operator who never set EMAIL_HOST would have no way to find
    out — R88's "202 Accepted is not delivery" lesson in a new costume.
    """
    with caplog.at_level(logging.WARNING, logger=NOTIFY_LOGGER):
        with override_settings(MAILERS=CONSOLE_MAILERS):
            file_against_status(member, review)
    messages = [r.message for r in caplog.records if r.name == NOTIFY_LOGGER]
    assert any("WILL NOT BE DELIVERED" in m for m in messages)
    assert any("console" in m.lower() for m in messages)


def test_the_console_warning_does_not_fire_with_a_real_smtp_backend(
    member, mod, review, caplog
):
    with caplog.at_level(logging.WARNING, logger=NOTIFY_LOGGER):
        with override_settings(MAILERS=SMTP_MAILERS):
            file_against_status(member, review)
    assert not any(
        "WILL NOT BE DELIVERED" in r.message
        for r in caplog.records
        if r.name == NOTIFY_LOGGER
    )


def test_the_worker_shouts_too_when_the_backend_is_console(member, mod, review, caplog):
    """An operator chasing this reads the worker log, not the web container."""
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=NOTIFY_LOGGER):
        with override_settings(MAILERS=CONSOLE_MAILERS):
            send_report_email(report.pk, mod.pk)
    assert any(
        "IS NOT BEING DELIVERED" in r.message
        for r in caplog.records
        if r.name == NOTIFY_LOGGER
    )


def test_no_eligible_recipients_is_logged_not_swallowed(
    member, accused, review, caplog
):
    """Trap 2's visibility requirement, on the notify side.

    A report exists and nobody is going to hear about it. That must be a
    recorded fact, not an empty outbox somebody has to interpret.
    """
    with caplog.at_level(logging.WARNING, logger=NOTIFY_LOGGER):
        file_against_status(member, review)
    assert any(
        "NO STAFF NOTIFICATION" in r.message
        for r in caplog.records
        if r.name == NOTIFY_LOGGER
    )


# --- D-d: the inbound peer Flag path --------------------------------------


def test_an_inbound_peer_report_notifies_staff(mod, accused, db):
    """D-d. Same queue, same urgency, and the case where nobody is watching is worst."""
    peer = make_peer("peer@other.test")
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, created = file_remote_report(
            peer, target_user=accused, comment="stolen"
        )
    assert created
    assert OrmQ.objects.count() == 1
    pkg = SignedPackage.loads(OrmQ.objects.get().payload)
    assert pkg["func"] == SEND_FUNC
    assert pkg["args"][0] == report.pk


def test_a_deduped_inbound_report_notifies_nobody(mod, accused, db):
    peer = make_peer("peer@other.test")
    with TestCase.captureOnCommitCallbacks(execute=True):
        file_remote_report(peer, target_user=accused, comment="stolen")
    peer2 = make_peer("peer2@other.test")
    with TestCase.captureOnCommitCallbacks(execute=True):
        second, created = file_remote_report(
            peer2, target_user=accused, comment="stolen"
        )
    assert created
    assert OrmQ.objects.count() == 1


def test_the_inbound_reporter_is_not_emailed_about_their_own_report(accused, db):
    """Trap 4 on the inbound path too: the sending server is the reporter."""
    peer = make_peer("mod@other.test", email="mod@other.test", is_moderator=True)
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_remote_report(peer, target_user=accused, comment="stolen")
    assert staff_email_recipients(report) == []


# --- R99 still holds ------------------------------------------------------


def test_the_notification_ledger_is_untouched(member, mod, review):
    """R99. No ``Kind.REPORT``, no ledger row, no unread badge.

    Counted across a full file-and-notify cycle rather than asserted from
    the absence of an import.
    """
    before = Notification.objects.count()
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review)
    send_report_email(report.pk, mod.pk)
    assert Notification.objects.count() == before


def test_a_reporter_who_blocked_every_moderator_still_triggers_the_email(
    member, mod, mod2, review
):
    """The trap that motivated R99, shown absent by construction.

    ``notify()`` refuses to deliver to a recipient who blocked the actor —
    right for a person's inbox, and on the moderation queue it would let a
    member mute the whole pile by blocking every moderator. This path
    computes recipients from the permission and sends to addresses, so a
    block changes nothing. If someone ever routes reports through
    ``notify()``, this goes red.
    """
    member.blocks.add(mod)
    member.blocks.add(mod2)
    assert mod in member.blocks.all()
    with TestCase.captureOnCommitCallbacks(execute=True):
        file_against_status(member, review)
    assert OrmQ.objects.count() == 2
    report = Report.objects.get()
    send_report_email(report.pk, mod.pk)
    send_report_email(report.pk, mod2.pk)
    assert sorted(m.to[0] for m in mail.outbox) == sorted([mod.email, mod2.email])


# --- R116: the toggle lives in UserAdmin ---------------------------------


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(localname="chiefadmin", password="s3cretpass")


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


def _roles_fields():
    for label, opts in UserAdmin.fieldsets:
        if label == "Roles":
            return opts["fields"]
    raise AssertionError("UserAdmin has no Roles fieldset")


def test_the_toggle_sits_in_the_roles_fieldset_beside_the_grant():
    """R116. One screen shows a moderator's whole capability."""
    fields = _roles_fields()
    assert "report_email" in fields
    assert fields.index("is_moderator") < fields.index("report_email")


def test_the_toggle_renders_on_the_admin_change_page(admin_client, mod):
    resp = admin_client.get(f"/admin/social/user/{mod.pk}/change/")
    assert resp.status_code == 200
    assert 'name="report_email"' in resp.content.decode()


def test_the_admin_shows_that_an_account_with_no_address_gets_nothing(admin_client, db):
    """Trap 2's visibility requirement, on the admin side.

    Without this the admin ticks a box, sees it saved, and has no way to
    know the mail will never go out.
    """
    ghost = User.objects.create_user(
        localname="ghosty", password="s3cretpass", is_moderator=True
    )
    resp = admin_client.get(f"/admin/social/user/{ghost.pk}/change/")
    assert resp.status_code == 200
    assert "no email address" in resp.content.decode()


def test_the_admin_shows_the_address_a_toggle_will_reach(admin_client, mod):
    resp = admin_client.get(f"/admin/social/user/{mod.pk}/change/")
    assert mod.email in resp.content.decode()


def test_the_admin_can_turn_the_toggle_off(admin_client, mod):
    payload = {
        "display_name": mod.display_name,
        "email": mod.email,
        "is_staff": "on" if mod.is_staff else "",
        "is_superuser": "on" if mod.is_superuser else "",
        "is_moderator": "on" if mod.is_moderator else "",
        "avatar": "",
    }
    resp = admin_client.post(f"/admin/social/user/{mod.pk}/change/", payload)
    assert resp.status_code == 302
    mod.refresh_from_db()
    assert mod.report_email is False
    assert mod.is_moderator is True


def test_turning_the_toggle_off_removes_only_that_moderator(member, mod, mod2, review):
    """The end-to-end shape of the live proof's step 3, in the suite."""
    mod.report_email = False
    mod.save(update_fields=["report_email"])
    with TestCase.captureOnCommitCallbacks(execute=True):
        report, _ = file_against_status(member, review)
    assert OrmQ.objects.count() == 1
    pkg = SignedPackage.loads(OrmQ.objects.get().payload)
    assert pkg["args"][1] == mod2.pk
