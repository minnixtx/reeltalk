"""The verification send path and the single address writer (2F-2).

Read against the traps in PROGRESS §2F rather than as a list of assertions.
The four that matter:

* ``test_nothing_leaves_the_box_inside_the_signup_request`` — the send is
  enqueued, never inline, so a dead mail server cannot roll back or slow a
  signup (2E's D-e, carried forward).
* ``test_the_console_backend_guard_fires_in_the_signup_request`` — trap 1,
  escalated. The console backend reports success, and here a silent
  non-send means an account that can never be verified, so the shout has
  to be in the request the person is standing in, not only in the worker.
* ``test_a_failed_send_records_the_error_on_its_token_and_reraises`` —
  trap 6. A red task row with nothing written on the token is the same as
  no record at all.
* ``test_the_notice_carries_no_link_at_all`` — R125's shape. The tamper
  notice goes to an address an attacker may still control, so it must be
  unactionable.

**Mock-only vs live.** Everything here runs on Django's locmem backend. It
proves the decision, the guard, the enqueue, the message contents, the
recorded outcome and the single-writer property. It does not prove a real
mail server accepts what we hand it — that is §2F-4, and nothing here
should be read as covering it.
"""

import logging

import pytest
from django.core import mail
from django.db import transaction
from django.test import TestCase, override_settings
from django_q.models import OrmQ, SignedPackage

from reeltalk.notifications.models import Notification
from reeltalk.social.models import (
    AddressMismatchError,
    EmailVerificationToken,
    Invite,
    User,
)
from reeltalk.social.tasks import (
    SEND_NOTICE_FUNC,
    SEND_TOKEN_FUNC,
    send_address_change_notice,
    send_verification_token,
)
from reeltalk.social.verify import (
    VERIFY_PATH,
    build_address_change_email,
    build_verification_email,
    change_email,
    send_verification_email,
    verification_url,
)
from reeltalk.tests.members import site_admin as create_site_admin

VERIFY_LOGGER = "reeltalk.social.verify"

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

PASSWORD = "s3cretpass"

SIGNUP = {
    "localname": "joiner",
    "display_name": "Joiner",
    "email": "joiner@example.test",
    "password1": PASSWORD,
    "password2": PASSWORD,
}


def _member(localname="member", email=None):
    return User.objects.create_user(
        localname=localname,
        password=PASSWORD,
        email=email if email is not None else f"{localname}@example.test",
    )


def _admin(localname="overlord"):
    # Verified. ``admin_client`` force-logs this account in, and under R119 an
    # unverified superuser has no session to give — the client would be
    # anonymous and the admin POSTs below would come back as a redirect to the
    # login page with the row untouched, which reads exactly like a broken
    # save rather than a missing identity.
    return create_site_admin(
        localname=localname, password=PASSWORD, email=f"{localname}@example.test"
    )


def _queued_packages():
    # ``args`` comes back as a tuple from the signed package.
    return [list(SignedPackage.loads(q.payload)["args"]) for q in OrmQ.objects.all()]


def _queued_funcs():
    return [SignedPackage.loads(q.payload)["func"] for q in OrmQ.objects.all()]


# --- the one helper, at all three creation routes --------------------------


@pytest.mark.django_db
def test_signup_queues_a_verification_email_for_the_new_account(client):
    _admin()
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = client.post("/signup/", SIGNUP)
    assert resp.status_code == 302

    joiner = User.objects.get(localname="joiner")
    token = EmailVerificationToken.objects.get(user=joiner)
    assert _queued_funcs() == [SEND_TOKEN_FUNC]
    assert _queued_packages() == [[token.pk]]


@pytest.mark.django_db
def test_the_setup_wizard_sends_through_the_same_helper(client):
    # The first-run admin is the account that owns the instance, so it is
    # the worst one to silently fail to verify. Same helper, same task, not
    # a second copy of the send logic.
    payload = dict(SIGNUP, localname="firstadmin", email="first@example.test")
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = client.post("/setup/", payload)
    assert resp.status_code == 302

    admin = User.objects.get(localname="firstadmin")
    assert admin.is_superuser is True
    assert _queued_funcs() == [SEND_TOKEN_FUNC]
    assert _queued_packages() == [[EmailVerificationToken.objects.get(user=admin).pk]]


@pytest.mark.django_db
def test_invite_acceptance_sends_through_the_same_helper(client):
    inviter = _admin()
    invite = Invite.mint(inviter)
    payload = dict(SIGNUP, localname="invitedone", email="invited@example.test")
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = client.post(f"/invite/{invite.code}/", payload)
    assert resp.status_code == 302

    joiner = User.objects.get(localname="invitedone")
    assert _queued_funcs() == [SEND_TOKEN_FUNC]
    assert _queued_packages() == [[EmailVerificationToken.objects.get(user=joiner).pk]]


@pytest.mark.django_db
def test_nothing_leaves_the_box_inside_the_signup_request(client):
    # The commit callbacks deliberately left un-run. The outbox being empty
    # here is the whole D-e property: the request that created the account
    # is not the request that talks to a mail server.
    _admin()
    client.post("/signup/", SIGNUP)
    assert mail.outbox == []
    assert OrmQ.objects.count() == 0
    # ...while the account and its token really were created. Scoped to the
    # joiner because ``_admin()`` is itself verified and so already holds a
    # spent token of its own — a global count would be measuring two people.
    assert User.objects.filter(localname="joiner").exists()
    assert EmailVerificationToken.objects.filter(user__localname="joiner").count() == 1


@pytest.mark.django_db
def test_the_helper_reports_the_send_it_queued(db):
    result = send_verification_email(_member())
    assert result["enqueued"] == 1
    assert result["reason"] == ""


@pytest.mark.django_db
def test_an_addressless_signup_is_refused_rather_than_built_unverifiable(client):
    # This test used to prove the opposite: that ``SignupForm.email`` was
    # ``required=False`` and an address-less signup was a normal working
    # thing. R118 closed that, and 2F-3 is the increment that made it
    # necessary rather than merely tidy — with the gate up, an account with no
    # address can never be verified, so it can never sign in. Accepting such a
    # signup would be manufacturing a brick one row at a time and charging the
    # member for it later.
    _admin()
    resp = client.post("/signup/", dict(SIGNUP, email=""))
    assert resp.status_code == 200  # re-rendered with the field error
    assert not User.objects.filter(localname="joiner").exists()
    assert not EmailVerificationToken.objects.filter(user__localname="joiner").exists()
    assert OrmQ.objects.count() == 0
    assert mail.outbox == []


@pytest.mark.django_db
def test_an_addressless_account_is_told_why_nothing_was_sent(db, caplog):
    # Not a silent skip: this is the account that will not be able to sign
    # in once 2F-3 goes up, and the operator needs to have heard about it
    # at the moment it was created.
    nobody = _member(localname="nobody", email="")
    with caplog.at_level(logging.WARNING, logger=VERIFY_LOGGER):
        result = send_verification_email(nobody)
    assert result == {"enqueued": 0, "reason": "no-address"}
    lines = [r.message for r in caplog.records if r.name == VERIFY_LOGGER]
    assert any("No verification email for @nobody" in line for line in lines)
    assert any("no email address" in line for line in lines)


# --- trap 1: the console backend ------------------------------------------


@pytest.mark.django_db
def test_the_console_backend_guard_fires_in_the_signup_request(client, caplog):
    # The guard is in the request, not only in the worker, and this asserts
    # *where* it fired: no task has run, yet the warning is already logged.
    _admin()
    with caplog.at_level(logging.WARNING, logger=VERIFY_LOGGER):
        with override_settings(MAILERS=CONSOLE_MAILERS):
            client.post("/signup/", SIGNUP)
    lines = [r.message for r in caplog.records if r.name == VERIFY_LOGGER]
    assert any("WILL NOT BE DELIVERED" in line for line in lines)
    assert any("console" in line for line in lines)
    assert mail.outbox == []


@pytest.mark.django_db
def test_the_guard_stays_quiet_when_a_real_smtp_backend_is_configured(client, caplog):
    _admin()
    with caplog.at_level(logging.WARNING, logger=VERIFY_LOGGER):
        with override_settings(MAILERS=SMTP_MAILERS):
            client.post("/signup/", SIGNUP)
    lines = [r.message for r in caplog.records if r.name == VERIFY_LOGGER]
    assert not any("WILL NOT BE DELIVERED" in line for line in lines)


@pytest.mark.django_db
def test_the_worker_shouts_too_when_the_backend_is_console(db, caplog):
    # An operator chasing "nobody got the link" reads the worker log, not
    # the web container, so the shout has to be on both sides.
    user = _member()
    with override_settings(MAILERS=CONSOLE_MAILERS):
        token = EmailVerificationToken.mint(user)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=VERIFY_LOGGER):
            send_verification_token(token.pk)
    lines = [r.message for r in caplog.records if r.name == VERIFY_LOGGER]
    assert any("IS NOT BEING DELIVERED" in line for line in lines)


# --- the message ----------------------------------------------------------


@pytest.mark.django_db
def test_the_verification_email_carries_its_own_token_link(db):
    user = _member()
    token = EmailVerificationToken.mint(user)
    message = build_verification_email(user, token)
    assert message.to == [token.email]
    assert verification_url(token) in message.body
    assert token.code in message.body
    assert "72" in message.body


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN="https://reel.example.test")
def test_the_link_is_minted_from_the_canonical_origin_not_the_request(client):
    # A verification link has to be openable by whoever receives the mail,
    # which may be nobody on the LAN the admin happened to be browsing
    # from. Same rule R108 applies to every published identifier, applied
    # to a credential that leaves the box inside an email.
    #
    # Proved through the request rather than around it: the client presents
    # ``testserver``, the canonical origin is set to something else, and
    # the mail that comes out must carry the canonical one.
    _admin()
    with TestCase.captureOnCommitCallbacks(execute=True):
        client.post("/signup/", SIGNUP)
    joiner = User.objects.get(localname="joiner")
    token = EmailVerificationToken.objects.get(user=joiner)
    url = verification_url(token)
    assert url.startswith("https://reel.example.test")

    send_verification_token(token.pk)
    body = mail.outbox[-1].body
    assert "https://reel.example.test/account/verify/" in body
    assert "testserver" not in body


@pytest.mark.django_db
def test_the_link_path_is_the_one_2f3_must_bind_to(db):
    # The path is a contract between two increments, so it is pinned here
    # rather than being a literal buried in a template.
    user = _member()
    token = EmailVerificationToken.mint(user)
    assert verification_url(token).endswith(f"{VERIFY_PATH}{token.code}/")


@pytest.mark.django_db
def test_the_verification_email_has_no_unsubscribe_link(db):
    # R121: verification mail is not unsubscribable. The gap belongs to
    # the staff report mail, whose threat model is a logged-out recipient
    # and a one-click link, not this one. Checked in the body, the subject
    # and the headers, because a header is exactly where such a link hides
    # from a body-only assertion.
    user = _member()
    token = EmailVerificationToken.mint(user)
    message = build_verification_email(user, token)
    haystack = (message.subject + message.body + repr(message.extra_headers)).lower()
    assert "unsubscribe" not in haystack.replace("no unsubscribe", "")


@pytest.mark.django_db
def test_the_email_goes_to_the_address_the_token_is_bound_to(db):
    user = _member(localname="bound", email="bound@example.test")
    token = EmailVerificationToken.mint(user)
    assert build_verification_email(user, token).to == ["bound@example.test"]


# --- the worker records the outcome ---------------------------------------


@pytest.mark.django_db
def test_a_successful_send_stamps_its_token(db):
    user = _member()
    token = EmailVerificationToken.mint(user)
    assert token.send_state == "queued"
    assert token.sent_at is None

    send_verification_token(token.pk)
    token.refresh_from_db()
    assert token.sent_at is not None
    assert token.send_error == ""
    assert token.send_state == "sent"


@pytest.mark.django_db
def test_a_failed_send_records_the_error_on_its_token_and_reraises(db, monkeypatch):
    # Trap 6. The row is written *before* the raise, so the operator gets
    # both the red task row and an explanation of which address was refused.
    user = _member()
    token = EmailVerificationToken.mint(user)

    def boom(self):
        raise OSError("smtp down")

    monkeypatch.setattr(mail.EmailMessage, "send", boom)
    with pytest.raises(OSError):
        send_verification_token(token.pk)

    token.refresh_from_db()
    assert token.sent_at is None
    assert "OSError" in token.send_error
    assert "smtp down" in token.send_error
    assert token.send_state == "failed"


@pytest.mark.django_db
def test_the_recorded_error_is_collapsed_to_one_line(db, monkeypatch):
    # SMTP diagnostics arrive multi-line. The whole string is kept — the
    # column is a TextField for exactly that — but on one line, so an admin
    # readout stays readable.
    user = _member()
    token = EmailVerificationToken.mint(user)

    def boom(self):
        raise OSError("554 5.7.1\n  Recipient address rejected\n  Sender mismatch")

    monkeypatch.setattr(mail.EmailMessage, "send", boom)
    with pytest.raises(OSError):
        send_verification_token(token.pk)
    token.refresh_from_db()
    assert "\n" not in token.send_error
    assert "Sender mismatch" in token.send_error


@pytest.mark.django_db
def test_sent_at_and_send_error_never_both_hold(db, monkeypatch):
    # The derivation of ``send_state`` depends on one row being one send
    # attempt. Both halves of that shown, not assumed.
    user = _member(localname="one", email="one@example.test")
    ok = EmailVerificationToken.mint(user)
    send_verification_token(ok.pk)
    ok.refresh_from_db()
    assert (ok.sent_at is not None) is True
    assert (ok.send_error != "") is False

    other = _member(localname="two", email="two@example.test")
    bad = EmailVerificationToken.mint(other)

    def boom(self):
        raise OSError("nope")

    monkeypatch.setattr(mail.EmailMessage, "send", boom)
    with pytest.raises(OSError):
        send_verification_token(bad.pk)
    bad.refresh_from_db()
    assert (bad.sent_at is not None) is False
    assert (bad.send_error != "") is True


@pytest.mark.django_db
def test_a_task_for_a_deleted_token_is_dropped_without_sending(db):
    # CASCADE takes the row when the account goes; the task must not raise
    # over a token that legitimately stopped existing.
    user = _member()
    token = EmailVerificationToken.mint(user)
    token_pk = token.pk
    user.delete()
    result = send_verification_token(token_pk)
    assert result == {"sent": False, "reason": "token-missing"}
    assert mail.outbox == []


@pytest.mark.django_db
def test_sending_verifies_nothing(db):
    # The mail only *asks*. Verification happens when the link is consumed,
    # which is 2F-3's route. A send that also verified would be R123's
    # forbidden hand-verify with extra steps.
    user = _member()
    token = EmailVerificationToken.mint(user)
    send_verification_token(token.pk)
    user.refresh_from_db()
    assert user.email_verified is False
    assert user.email_verified_at is None
    token.refresh_from_db()
    assert token.used_at is None


@pytest.mark.django_db
def test_the_send_path_does_not_touch_the_notification_ledger(db):
    # R99 holds: this mail is not a notification and never enters the
    # ledger. Counted across the whole cycle rather than asserted about.
    assert Notification.objects.count() == 0
    user = _member()
    token = EmailVerificationToken.mint(user)
    send_verification_token(token.pk)
    send_address_change_notice(user.pk, "someone-else@example.test")
    assert Notification.objects.count() == 0


# --- change_email: the single writer (R125) --------------------------------


@pytest.mark.django_db
def test_change_email_writes_the_new_address_normalised(db):
    user = _member()
    change_email(user, "  Moved@EXAMPLE.test  ", changed_by=_admin())
    user.refresh_from_db()
    assert user.email == "moved@example.test"


@pytest.mark.django_db
def test_changing_an_address_notifies_the_old_one(db):
    user = _member(localname="notice", email="before@example.test")
    with TestCase.captureOnCommitCallbacks(execute=True):
        result = change_email(user, "after@example.test", changed_by=_admin())
    assert result["changed"] is True
    assert result["notified"] == "before@example.test"
    assert _queued_funcs() == [SEND_NOTICE_FUNC]
    assert _queued_packages() == [[user.pk, "before@example.test"]]

    send_address_change_notice(user.pk, "before@example.test")
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["before@example.test"]


@pytest.mark.django_db
def test_the_notice_goes_to_the_old_address_not_the_new_one(db):
    # The point of the control is that the person who is about to stop
    # receiving mail is the one who is told. Sending it to the new address
    # would tell the party who benefits from the change and nobody else.
    user = _member(localname="swapped", email="old@example.test")
    with TestCase.captureOnCommitCallbacks(execute=True):
        change_email(user, "new@example.test", changed_by=_admin())
    assert mail.outbox == []
    send_address_change_notice(user.pk, "old@example.test")
    assert mail.outbox[0].to == ["old@example.test"]
    assert "new@example.test" not in mail.outbox[0].to


@pytest.mark.django_db
def test_the_notice_carries_no_link_at_all(db):
    # R125's shape, asserted rather than trusted. The recipient is an
    # address that an attacker may still control, so anything actionable in
    # this mail is something they could act on too. Asserted on URL-shaped
    # things rather than on words: the copy is allowed to explain itself.
    user = _member(localname="tamper", email="old@example.test")
    message = build_address_change_email(user, "old@example.test")
    body = message.body
    assert "http://" not in body
    assert "https://" not in body
    assert "//" not in body
    assert VERIFY_PATH not in body
    assert ".com" not in body and ".test/" not in body


@pytest.mark.django_db
def test_the_notice_names_the_site_admin_not_a_member_handle(db):
    # The mail may land in a stranger's mailbox (a corrected typo, a
    # hijacked account). Another member's handle in it is a leak with no
    # upside; "the site administrator" is the truth and nothing more.
    admin = _admin(localname="overlord")
    user = _member(localname="memberone", email="old@example.test")
    change_email(user, "new@example.test", changed_by=admin)
    body = build_address_change_email(user, "old@example.test").body
    assert "overlord" not in body
    assert "site administrator" in body


@pytest.mark.django_db
def test_an_empty_old_address_skips_the_notice(db):
    # Nothing to tell. This is also the shape of giving the standing
    # address-less accounts their first address.
    user = _member(localname="wasblank", email="")
    with TestCase.captureOnCommitCallbacks(execute=True):
        result = change_email(user, "filled@example.test", changed_by=_admin())
    assert result["changed"] is True
    assert result["notified"] == ""
    assert OrmQ.objects.count() == 0
    assert mail.outbox == []


@pytest.mark.django_db
def test_an_unchanged_address_is_a_no_op(db):
    # A form that resubmits the same address must not produce a tamper
    # notice, or every unrelated profile save would mail the member a false
    # alarm about a change that never happened.
    user = _member(localname="same", email="same@example.test")
    EmailVerificationToken.mint(user)
    with TestCase.captureOnCommitCallbacks(execute=True):
        result = change_email(user, "same@example.test", changed_by=_admin())
    assert result["changed"] is False
    assert result["reason"] == "unchanged"
    assert OrmQ.objects.count() == 0
    assert mail.outbox == []
    user.refresh_from_db()
    assert user.email == "same@example.test"
    # ...and the live token is still live, because nothing happened.
    assert EmailVerificationToken.live().filter(user=user).count() == 1


@pytest.mark.django_db
def test_a_case_variant_of_the_same_address_is_still_a_no_op(db):
    # Compared after normalisation, so ``Same@X`` vs ``same@x`` does not
    # fire a notice about a change that is only capitalisation.
    user = _member(localname="casing", email="casing@example.test")
    with TestCase.captureOnCommitCallbacks(execute=True):
        result = change_email(user, "CASING@EXAMPLE.TEST", changed_by=_admin())
    assert result["changed"] is False
    assert OrmQ.objects.count() == 0
    assert mail.outbox == []


@pytest.mark.django_db
def test_changing_the_address_supersedes_live_tokens(db):
    # Not for safety — the address binding already refuses a stale link.
    # For making the state unreachable: no live credential sitting in a
    # mailbox pointing at an address this account no longer holds.
    user = _member(localname="killme", email="old@example.test")
    old = EmailVerificationToken.mint(user)
    assert old.is_live is True

    change_email(user, "new@example.test", changed_by=_admin())
    old.refresh_from_db()
    assert old.superseded_at is not None
    assert old.used_at is None
    assert old.is_live is False
    assert EmailVerificationToken.live().filter(user=user).count() == 0


@pytest.mark.django_db
def test_a_superseded_old_token_could_not_have_verified_the_new_address(db):
    # The belt-and-braces pair to the supersede: even if a stale link had
    # survived, consuming it must refuse rather than attest the new address.
    user = _member(localname="belt", email="old@example.test")
    old = EmailVerificationToken.mint(user)
    change_email(user, "new@example.test", changed_by=_admin())

    with pytest.raises(AddressMismatchError):
        old.consume()
    user.refresh_from_db()
    assert user.email_verified is False


@pytest.mark.django_db
def test_changing_the_address_invalidates_the_verified_state(db):
    # The derivation does this for free; asserted here so a future writer
    # cannot get it wrong without a test going red.
    user = _member(localname="verifiedone", email="old@example.test")
    token = EmailVerificationToken.mint(user)
    token.consume()
    user.refresh_from_db()
    assert user.email_verified is True

    change_email(user, "new@example.test", changed_by=_admin())
    user.refresh_from_db()
    assert user.email_verified is False
    # The old proof is left intact rather than erased: it is the record of
    # what was actually clicked, for the address that was actually verified.
    assert user.verified_email == "old@example.test"


@pytest.mark.django_db
def test_the_notice_is_only_queued_once_the_change_committed(db):
    # The on_commit hook means a rolled-back address change produces no
    # notice about a change that never happened.
    user = _member(localname="rolledback", email="old@example.test")
    try:
        with transaction.atomic():
            change_email(user, "new@example.test", changed_by=_admin())
            raise RuntimeError("simulated rollback")
    except RuntimeError:
        pass
    user.refresh_from_db()
    assert user.email == "old@example.test"
    assert mail.outbox == []


@pytest.mark.django_db
def test_the_notice_task_sends_to_the_address_it_was_given(db):
    user = _member(localname="noticetask", email="new@example.test")
    send_address_change_notice(user.pk, "old@example.test")
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["old@example.test"]
    assert "noticetask" in mail.outbox[0].body


@pytest.mark.django_db
def test_the_notice_task_for_a_deleted_account_is_dropped(db):
    user = _member(localname="gone", email="old@example.test")
    user_pk = user.pk
    user.delete()
    result = send_address_change_notice(user_pk, "old@example.test")
    assert result["sent"] is False
    assert mail.outbox == []


@pytest.mark.django_db
def test_a_failed_notice_is_logged_and_reraised(db, monkeypatch, caplog):
    # Log-only by decision, so the log is the entire record — it has to be
    # there, and the task row has to go red rather than green.
    user = _member(localname="noicetask2", email="new@example.test")

    def boom(self):
        raise OSError("relay refused")

    monkeypatch.setattr(mail.EmailMessage, "send", boom)
    with caplog.at_level(logging.ERROR, logger=VERIFY_LOGGER):
        with pytest.raises(OSError):
            send_address_change_notice(user.pk, "old@example.test")
    lines = [r.message for r in caplog.records if r.name == VERIFY_LOGGER]
    assert any("FAILED" in line and "relay refused" in line for line in lines)


# --- the admin routes through the single writer ----------------------------


@pytest.fixture
def admin_client(client, db):
    admin = _admin()
    client.force_login(admin)
    return client


@pytest.mark.django_db
def test_an_admin_address_edit_produces_the_notice(admin_client):
    user = _member(localname="edited", email="before@example.test")
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = admin_client.post(
            f"/admin/social/user/{user.pk}/change/",
            {
                "display_name": user.display_name,
                "email": "after@example.test",
                "is_staff": "",
                "is_superuser": "",
            },
        )
    assert resp.status_code == 302
    user.refresh_from_db()
    assert user.email == "after@example.test"
    # The request queued the notice; it did not send it. Same rule as every
    # other mail in this project — the admin's save is not the thing that
    # talks to a mail server.
    assert _queued_funcs() == [SEND_NOTICE_FUNC]
    assert _queued_packages() == [[user.pk, "before@example.test"]]
    assert mail.outbox == []

    send_address_change_notice(user.pk, "before@example.test")
    assert mail.outbox[0].to == ["before@example.test"]


@pytest.mark.django_db
def test_an_admin_edit_that_leaves_the_address_alone_sends_nothing(admin_client):
    user = _member(localname="untouched", email="keep@example.test")
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = admin_client.post(
            f"/admin/social/user/{user.pk}/change/",
            {
                "display_name": "A Different Name",
                "email": "keep@example.test",
                "is_staff": "",
                "is_superuser": "",
            },
        )
    assert resp.status_code == 302
    user.refresh_from_db()
    assert user.display_name == "A Different Name"
    assert user.email == "keep@example.test"
    assert OrmQ.objects.count() == 0
    assert mail.outbox == []


@pytest.mark.django_db
def test_the_admin_edit_goes_through_change_email_not_a_direct_write(
    admin_client, monkeypatch
):
    # The routing itself, pinned. What matters is not that a notice happens
    # to be sent but that the admin cannot change an address without going
    # through the one function that sends it.
    from reeltalk.social import admin as social_admin

    seen = {}

    def spy(user, new_email, *, changed_by):
        seen["user"] = user.localname
        seen["new"] = new_email
        seen["by"] = changed_by.localname
        return {"changed": True, "notified": "", "reason": "spied"}

    monkeypatch.setattr(social_admin, "change_email", spy)
    user = _member(localname="routed", email="before@example.test")
    admin_client.post(
        f"/admin/social/user/{user.pk}/change/",
        {
            "display_name": user.display_name,
            "email": "after@example.test",
            "is_staff": "",
            "is_superuser": "",
        },
    )
    assert seen == {"user": "routed", "new": "after@example.test", "by": "overlord"}
    # The plain save wrote the OLD address; only the spy would have written
    # the new one, and it was monkeypatched out.
    user.refresh_from_db()
    assert user.email == "before@example.test"


@pytest.mark.django_db
def test_an_admin_edit_to_a_duplicate_address_is_refused_without_a_notice(admin_client):
    # The unique-when-set index is enforced at the form boundary, so a
    # collision is a validation error rather than a 500 and — crucially —
    # no notice about a change that did not happen.
    _member(localname="taken", email="taken@example.test")
    user = _member(localname="collider", email="mine@example.test")
    resp = admin_client.post(
        f"/admin/social/user/{user.pk}/change/",
        {
            "display_name": user.display_name,
            "email": "taken@example.test",
            "is_staff": "",
            "is_superuser": "",
        },
    )
    user.refresh_from_db()
    assert user.email == "mine@example.test"
    assert mail.outbox == []
    assert resp.status_code == 200


@pytest.mark.django_db
def test_the_admin_add_path_sends_no_verification_mail(admin_client):
    # Deliberate scope, not an oversight: R124's admin-triggered send is
    # 2F-3's surface. An admin creating an account is not a member signing
    # up, and the create form having a side-effecting mail was not asked for.
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = admin_client.post(
            "/admin/social/user/add/",
            {
                "localname": "madebyadmin",
                "display_name": "",
                "email": "made@example.test",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
    assert resp.status_code == 302
    assert User.objects.filter(localname="madebyadmin").exists()
    # The commit callbacks were captured and run, so "nothing was queued"
    # is a proof here rather than an artifact of nobody having run them.
    # Scoped to the created account: the admin in ``admin_client`` is itself
    # verified and carries a spent token of its own.
    assert not EmailVerificationToken.objects.filter(
        user__localname="madebyadmin"
    ).exists()
    assert OrmQ.objects.count() == 0
    assert mail.outbox == []
