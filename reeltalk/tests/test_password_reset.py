"""§2G — self-service password reset, the admin's writable credential, and
the session rule that both of them hang on.

What gets pinned here, and why each one earns its place:

* **Reset has its own budget.** The whole reason ``RESET_*`` exists rather
  than reusing ``RESEND_*`` is an availability attack: share the counters
  and the unauthenticated resend route becomes a way to jam password
  recovery shut. Two tests below spend the other route's budget to prove
  this one still works. That is the only way the property means anything.
* **An unverified account gets no reset, and the page says so without
  becoming an oracle.** The refusal must be invisible in the *response* and
  visible in the *copy shown to everyone*. Both halves get a test, because
  either one alone looks correct while the pair is what makes it safe.
* **The reset link is not a verification link.** Different table,
  different credential, and neither is spendable as the other.
* **A reset never verifies anything.** R123 keeps one writer of the
  verified state. A reset proves the recipient could read the mailbox,
  which is a weaker claim, and this flow does not get to make a stronger
  one. Pinned by completing a reset against an *unverified* account — a
  state no request path can produce — and checking the flag did not move.
* **Every password change kills the account's sessions, and nothing had to
  be built for that.** Django's ``get_user()`` compares the session's
  ``_auth_user_hash`` against the current password on every request. The
  tests below prove it in this app's real stack rather than trusting the
  docstring, and one of them reads the shipped source to hold the line that
  keeps it true: nobody calls ``update_session_auth_hash``.
* **The admin's password field is real now.** It used to be
  ``ReadOnlyPasswordHashField(disabled=True)`` — a display nobody could
  write through. These tests set a credential through the browser and then
  log in with it, because a field that merely renders is not a field.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import SESSION_KEY
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from django_q.models import OrmQ, SignedPackage

from reeltalk.social import password_reset
from reeltalk.social.models import (
    PASSWORD_RESET_TTL_HOURS,
    EmailVerificationToken,
    PasswordResetToken,
    User,
)
from reeltalk.social.password_reset import (
    RESET_ADDRESS_COOLDOWN_MINUTES,
    RESET_IP_LIMIT,
    RESET_PATH,
    classify_reset_code,
    complete_reset,
    request_reset,
    reset_address_in_cooldown,
    reset_ip_budget_spent,
    reset_url,
)
from reeltalk.social.passwords import set_password
from reeltalk.social.tasks import SEND_PW_NOTICE_FUNC
from reeltalk.social.verify import RESEND_ADDRESS_COOLDOWN_MINUTES, RESEND_IP_LIMIT
from reeltalk.tests.members import (
    PASSWORD,
    member,
    site_admin,
    unverified_member,
)

RESET_URL = reverse("password-reset")
STRONG = "a-strong-pass-phrase-9182"


def _rewind(token, **fields):
    """Backdate a token's ``auto_now_add`` clock, as 2F's helper does."""
    for name, value in fields.items():
        setattr(token, name, value)
    token.save(update_fields=list(fields))


def _clear_verification_cooldowns():
    """Push every existing verification token out of the resend window.

    Needed by the two starvation tests because ``member()`` reaches the
    verified state the only honest way — by minting and spending a real
    token — and that mint leaves a row inside the per-address cooldown the
    moment the account exists. Without this the fill loop never fills
    anything: every resend is refused for a reason that has nothing to do
    with the property under test.
    """
    EmailVerificationToken.objects.update(
        created_at=timezone.now()
        - timedelta(minutes=RESEND_ADDRESS_COOLDOWN_MINUTES + 1)
    )


def queued():
    """The pending cluster tasks, decoded — the observable for a send."""
    return [SignedPackage.loads(q.payload) for q in OrmQ.objects.all()]


def _rewind(token, **fields):
    """Backdate a token's ``auto_now_add`` clock, as 2F's helper does."""
    for name, value in fields.items():
        setattr(token, name, value)
    token.save(update_fields=list(fields))


def _confirm_url(code):
    return f"{RESET_PATH}{code}/"


def _reset_page(email):
    """Submit the address and read back the page the redirect lands on."""
    c = Client()
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = c.post(RESET_URL, {"email": email})
    assert resp.status_code == 302
    page = c.get(resp["Location"])
    return page.content.decode(), [str(m) for m in page.context["messages"]]


# --- the request page: it mints, and it records who asked ----------------


@pytest.mark.django_db
def test_the_reset_page_renders_logged_out(client):
    resp = client.get(RESET_URL)
    assert resp.status_code == 200
    assert 'name="email"' in resp.content.decode()


@pytest.mark.django_db
def test_a_reset_request_mints_and_queues_a_fresh_token(client):
    user = member(localname="resetme", password=PASSWORD)
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = client.post(RESET_URL, {"email": "resetme@example.test"})
    assert resp.status_code == 302
    token = PasswordResetToken.objects.get(user=user)
    assert token.is_live
    assert token.email == "resetme@example.test"


@pytest.mark.django_db
def test_the_reset_records_the_requesting_source_on_the_token(client):
    # The per-source axis is only countable from the durable table if the
    # source is actually written down, and it is written down from
    # client_ip() rather than from a header read.
    member(localname="sourced2g", password=PASSWORD)
    client.post(RESET_URL, {"email": "sourced2g@example.test"})
    token = PasswordResetToken.objects.get(user__localname="sourced2g")
    assert token.request_ip == "127.0.0.1"


@pytest.mark.django_db
def test_the_reset_link_points_at_the_confirm_route():
    user = member(localname="linked", password=PASSWORD)
    token = PasswordResetToken.mint(user)
    url = reset_url(token)
    assert url == f"http://testserver{RESET_PATH}{token.code}/"
    assert classify_reset_code(token.code)[0] == "live"


@pytest.mark.django_db
def test_the_reset_token_expires_on_its_own_clock_not_verifications():
    # 2G priced its window at half of verification's, deliberately. If the
    # two ever collapse to one number the pricing is gone and nothing else
    # here would notice.
    assert PASSWORD_RESET_TTL_HOURS == 24
    user = member(localname="clocked", password=PASSWORD)
    token = PasswordResetToken.mint(user)
    # ``created_at`` is stamped at save, a hair after the ``now`` mint()
    # computed the expiry from, so the comparison carries a tolerance.
    # What is being pinned is the length of the window, not the instant the
    # row was written.
    window = token.expires_at - token.created_at
    assert abs(window - timedelta(hours=PASSWORD_RESET_TTL_HOURS)) < timedelta(
        minutes=1
    )


# --- the response never reveals anything about the address ---------------


@pytest.mark.django_db
def test_the_reset_response_is_identical_for_a_known_and_an_unknown_address(client):
    member(localname="known2g", email="known2g@example.test")
    known_body, known_msgs = _reset_page("known2g@example.test")
    unknown_body, unknown_msgs = _reset_page("nobody-at-all-2g@example.test")

    assert known_msgs == unknown_msgs
    assert known_msgs, "expected a uniform confirmation message"
    assert "known2g" not in unknown_body
    assert "not registered" not in unknown_body.lower()


@pytest.mark.django_db
def test_an_unverified_account_gets_no_reset_mail(client):
    # The owner's rule. Under R119 a new password would not open this
    # account, so sending one would be a credential that does nothing.
    unverified_member(localname="unver2g", password=PASSWORD)
    request_reset("unver2g@example.test", "10.0.0.1")
    assert not PasswordResetToken.objects.filter(user__localname="unver2g").exists()


@pytest.mark.django_db
def test_the_unverified_refusal_is_indistinguishable_from_a_successful_send(client):
    # The refusal above must not be visible in the answer, or the page is a
    # lookup for which addresses are registered-but-unverified.
    unverified_member(localname="quiet2g", password=PASSWORD)
    member(localname="loud2g", email="loud2g@example.test")
    quiet_body, quiet_msgs = _reset_page("quiet2g@example.test")
    loud_body, loud_msgs = _reset_page("loud2g@example.test")
    assert quiet_msgs == loud_msgs
    assert "unverified" not in quiet_body.lower()
    assert "not verified" not in quiet_body.lower()


@pytest.mark.django_db
def test_the_reset_page_states_the_verification_precondition_to_everyone():
    """The copy that makes the uniform answer honest.

    Shown on the GET, before anyone has typed, so it discloses nothing
    about any particular address while still putting the right sentence in
    front of the person who needs it. If this goes away the uniform message
    becomes a lie to unverified members rather than a statement they can
    check for themselves.
    """
    c = Client()
    body = c.get(RESET_URL).content.decode()
    assert "verified email address" in body
    assert reverse("verify-resend") in body


@pytest.mark.django_db
def test_a_remote_account_gets_no_reset_mail(client):
    # A mirror has no password here to reset; it signs in at home. A link
    # would point at a page that can never work.
    remote = User.objects.create_user(
        localname="faraway", password=PASSWORD, email="faraway@elsewhere.test"
    )
    remote.local = False
    remote.save(update_fields=["local"])
    result = request_reset("faraway@elsewhere.test", "10.0.0.9")
    assert result["enqueued"] == 0
    assert result["reason"] == "remote-account"


@pytest.mark.django_db
def test_nothing_sends_inside_the_reset_request(client):
    member(localname="queued2g", password=PASSWORD)
    client.post(RESET_URL, {"email": "queued2g@example.test"})
    assert mail.outbox == []


# --- the two throttle axes ---------------------------------------------


@pytest.mark.django_db
def test_the_address_cooldown_refuses_inside_the_window(db):
    user = member(localname="patient2g", password=PASSWORD)
    request_reset("patient2g@example.test", "10.0.0.1")
    assert PasswordResetToken.objects.filter(user=user).count() == 1
    assert reset_address_in_cooldown("patient2g@example.test") is True

    result = request_reset("patient2g@example.test", "10.0.0.2")
    assert result["reason"] == "address-cooldown"
    assert PasswordResetToken.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_the_address_cooldown_admits_outside_the_window(db):
    user = member(localname="later2g", password=PASSWORD)
    request_reset("later2g@example.test", "10.0.0.1")
    window = timedelta(minutes=RESET_ADDRESS_COOLDOWN_MINUTES + 1)
    _rewind(
        PasswordResetToken.objects.get(user=user),
        created_at=timezone.now() - window,
    )
    assert reset_address_in_cooldown("later2g@example.test") is False
    request_reset("later2g@example.test", "10.0.0.1")
    assert PasswordResetToken.objects.filter(user=user).count() == 2


@pytest.mark.django_db
def test_a_failed_send_does_not_make_the_member_wait_out_the_window(db):
    user = member(localname="failed2g", password=PASSWORD)
    request_reset("failed2g@example.test", "10.0.0.1")
    token = PasswordResetToken.objects.get(user=user)
    token.send_error = "SMTPServerDisconnected: connection refused"
    token.save(update_fields=["send_error"])

    assert reset_address_in_cooldown("failed2g@example.test") is False
    assert request_reset("failed2g@example.test", "10.0.0.1")["enqueued"] == 1


@pytest.mark.django_db
def test_the_ip_axis_throttles_a_source_cycling_many_addresses(db):
    for i in range(RESET_IP_LIMIT):
        member(localname=f"cycler2g{i}", email=f"cycler2g{i}@example.test")
        assert (
            request_reset(f"cycler2g{i}@example.test", "10.90.0.1")["enqueued"] == 1
        ), f"send {i} should have gone out"

    assert reset_ip_budget_spent("10.90.0.1") is True
    member(localname="extra2g", email="extra2g@example.test")
    blocked = request_reset("extra2g@example.test", "10.90.0.1")
    assert blocked["reason"] == "ip-limited"
    assert not PasswordResetToken.objects.filter(user__localname="extra2g").exists()


@pytest.mark.django_db
def test_an_unknown_ip_is_not_a_free_pass(db):
    assert reset_ip_budget_spent("") is False


# --- the budgets are separate, which is the whole point of RESET_* -------


@pytest.mark.django_db
def test_a_full_resend_budget_does_not_starve_password_recovery(db):
    """R129's availability attack, run against the code.

    Fill the *verification resend* budget until that route refuses, then
    ask for a password reset on the same address from the same source. If
    the two shared a counter this is where recovery dies. It must not.
    """
    from reeltalk.social.verify import ip_budget_spent, request_resend

    user = member(localname="starved", password=PASSWORD)
    for i in range(RESEND_IP_LIMIT):
        member(localname=f"filler{i}", email=f"filler{i}@example.test")
    _clear_verification_cooldowns()

    source = "10.77.0.1"
    for i in range(RESEND_IP_LIMIT):
        assert request_resend(f"filler{i}@example.test", source)["enqueued"] == 1
    assert ip_budget_spent(source) is True

    result = request_reset("starved@example.test", source)
    assert result["enqueued"] == 1, "resend starved password recovery"
    assert PasswordResetToken.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_a_full_reset_budget_does_not_starve_verification_resend(db):
    # The other direction, because a shared counter is symmetric and the
    # test has to be too.
    from reeltalk.social.verify import request_resend

    member(localname="resender", password=PASSWORD)
    for i in range(RESET_IP_LIMIT):
        member(localname=f"rfiller{i}", email=f"rfiller{i}@example.test")
    _clear_verification_cooldowns()

    source = "10.78.0.1"
    for i in range(RESET_IP_LIMIT):
        assert request_reset(f"rfiller{i}@example.test", source)["enqueued"] == 1
    assert reset_ip_budget_spent(source) is True

    result = request_resend("resender@example.test", source)
    assert result["enqueued"] == 1, "reset starved verification resend"


# --- spending the link ---------------------------------------------------


@pytest.mark.django_db
def test_get_with_a_live_link_shows_the_password_form(client):
    user = member(localname="chooser", password=PASSWORD)
    token = PasswordResetToken.mint(user)
    resp = client.get(_confirm_url(token.code))
    assert resp.status_code == 200
    assert 'name="password1"' in resp.content.decode()


@pytest.mark.django_db
def test_posting_a_new_password_changes_it_and_spends_the_link(client):
    user = member(localname="rotator", password=PASSWORD)
    token = PasswordResetToken.mint(user)
    resp = client.post(
        _confirm_url(token.code),
        {"password1": STRONG, "password2": STRONG},
    )
    assert resp.status_code == 200
    user.refresh_from_db()
    assert user.check_password(STRONG) is True
    token.refresh_from_db()
    assert token.used_at is not None
    assert token.link_state == "used"


@pytest.mark.django_db
def test_the_old_password_stops_working(client):
    user = member(localname="oldpass", password=PASSWORD)
    token = PasswordResetToken.mint(user)
    client.post(_confirm_url(token.code), {"password1": STRONG, "password2": STRONG})
    user.refresh_from_db()
    assert user.check_password(PASSWORD) is False


@pytest.mark.django_db
def test_a_link_cannot_be_spent_twice(client):
    user = member(localname="twice2g", password=PASSWORD)
    token = PasswordResetToken.mint(user)
    url = _confirm_url(token.code)
    client.post(url, {"password1": STRONG, "password2": STRONG})
    second = client.post(
        url, {"password1": "another-strong-1", "password2": "another-strong-1"}
    )
    assert second.status_code == 200
    assert "already been used" in second.content.decode()
    user.refresh_from_db()
    assert user.check_password(STRONG) is True


@pytest.mark.django_db
def test_an_expired_link_says_so_with_its_date(client):
    user = member(localname="stale2g", password=PASSWORD)
    token = PasswordResetToken.mint(user)
    _rewind(token, expires_at=timezone.now() - timedelta(hours=1))
    resp = client.get(_confirm_url(token.code))
    assert resp.status_code == 200
    assert "expired" in resp.content.decode()
    assert 'name="password1"' not in resp.content.decode()


@pytest.mark.django_db
def test_a_superseded_link_points_at_the_newest_one(client):
    user = member(localname="older2g", password=PASSWORD)
    first = PasswordResetToken.mint(user)
    PasswordResetToken.mint(user)
    outcome, reason = classify_reset_code(first.code)
    assert outcome == "superseded"
    assert "newer" in reason


@pytest.mark.django_db
def test_an_unknown_code_is_refused_without_touching_anyone(db):
    outcome, reason = classify_reset_code("nobody-used-this-code-ever-12345")
    assert outcome == "invalid"
    assert reason is not None


@pytest.mark.django_db
def test_a_link_cannot_reset_an_address_it_was_not_minted_for(db):
    # The binding that makes the email column worth having. The admin may
    # correct an address at any moment; a link in the old mailbox must not
    # stay a credential against the account.
    from reeltalk.social.verify import change_email

    user = member(localname="moved2g", password=PASSWORD)
    token = PasswordResetToken.mint(user)
    change_email(user, "newhome@example.test", changed_by=user)

    result = complete_reset(token.code, STRONG)
    assert result["changed"] is False
    assert result["reason"] == "mismatch"
    user.refresh_from_db()
    assert user.check_password(PASSWORD) is True


@pytest.mark.django_db
def test_mismatched_confirmation_changes_nothing(client):
    user = member(localname="typo2g", password=PASSWORD)
    token = PasswordResetToken.mint(user)
    resp = client.post(
        _confirm_url(token.code),
        {"password1": STRONG, "password2": "something-else-entirely"},
    )
    assert resp.status_code == 200
    assert "did not match" in resp.content.decode()
    user.refresh_from_db()
    assert user.check_password(PASSWORD) is True
    token.refresh_from_db()
    assert token.used_at is None


@pytest.mark.django_db
def test_a_password_the_policy_refuses_changes_nothing(client):
    user = member(localname="weak2g", password=PASSWORD)
    token = PasswordResetToken.mint(user)
    resp = client.post(
        _confirm_url(token.code), {"password1": "1234", "password2": "1234"}
    )
    assert resp.status_code == 200
    user.refresh_from_db()
    assert user.check_password(PASSWORD) is True
    token.refresh_from_db()
    assert token.used_at is None


@pytest.mark.django_db
def test_a_reset_never_verifies_an_address(db):
    # R123's single writer, defended from the other side. This state is
    # unreachable through the request path — an unverified account is
    # refused a link — so the token is minted directly to ask the question
    # that actually matters: does spending this credential touch the
    # verified flag? It must not.
    user = unverified_member(localname="neververified", password=PASSWORD)
    assert user.email_verified is False
    token = PasswordResetToken.mint(user)
    result = complete_reset(token.code, STRONG)
    assert result["changed"] is True
    user.refresh_from_db()
    assert user.check_password(STRONG) is True
    assert user.email_verified is False


@pytest.mark.django_db
def test_a_verification_link_is_not_a_password_reset_link(client):
    user = member(localname="crosspurpose", password=PASSWORD)
    verify_token = EmailVerificationToken.mint(user)
    resp = client.get(_confirm_url(verify_token.code))
    assert resp.status_code == 200
    assert "not valid" in resp.content.decode()
    assert 'name="password1"' not in resp.content.decode()


@pytest.mark.django_db
def test_a_password_reset_link_is_not_a_verification_link(client):
    user = unverified_member(localname="crosspurpose2", password=PASSWORD)
    reset_token = PasswordResetToken.mint(user)
    resp = client.get(f"/account/verify/{reset_token.code}/")
    assert "not valid" in resp.content.decode()
    user.refresh_from_db()
    assert user.email_verified is False


# --- the session rule: every password change kills the account's sessions


@pytest.mark.django_db
def test_a_self_service_reset_kills_the_accounts_existing_session():
    """Not a bonus of the chosen seam — the seam itself, proved in this stack.

    Django's ``get_user()`` runs on every request and compares the
    ``_auth_user_hash`` in the session against an HMAC of the user's
    current password. Nothing in 2G writes that fact; this test exists
    because trusting a docstring about a security invariant is how you end
    up without it.
    """
    member(localname="evicted", password=PASSWORD)
    c = Client()
    assert c.login(username="evicted", password=PASSWORD) is True
    assert c.get("/preferences/profile/").context["user"].is_authenticated is True

    token = PasswordResetToken.objects.create(
        code="x" * 43,
        user=User.objects.get(localname="evicted"),
        email="evicted@example.test",
        expires_at=timezone.now() + timedelta(hours=1),
    )
    complete_reset(token.code, STRONG)

    resp = c.get("/preferences/profile/", follow=True)
    assert resp.context["user"].is_authenticated is False
    assert SESSION_KEY not in c.session


@pytest.mark.django_db
def test_an_admin_set_password_kills_that_members_session():
    admin = site_admin(localname="boss2g", password=STRONG)
    member(localname="kicked", password=PASSWORD)
    victim = Client()
    assert victim.login(username="kicked", password=PASSWORD) is True

    ac = Client()
    ac.force_login(admin)
    with TestCase.captureOnCommitCallbacks(execute=True):
        ac.post(
            f"/admin/social/user/{User.objects.get(localname='kicked').pk}/change/",
            {
                "display_name": "",
                "email": "kicked@example.test",
                "is_staff": "",
                "is_superuser": "",
                "is_moderator": "",
                "report_email": "",
                "password1": STRONG,
                "password2": STRONG,
            },
        )

    assert (
        victim.get("/preferences/profile/", follow=True)
        .context["user"]
        .is_authenticated
        is False
    )


@pytest.mark.django_db
def test_another_members_session_survives_victims_password_change():
    member(localname="target2g", password=PASSWORD)
    member(localname="bystander2g", password=PASSWORD)
    bystander = Client()
    assert bystander.login(username="bystander2g", password=PASSWORD)

    set_password(User.objects.get(localname="target2g"), STRONG, changed_by=None)

    bystander_user = bystander.get("/preferences/profile/").context["user"]
    assert bystander_user.is_authenticated is True


@pytest.mark.django_db
def test_an_admin_who_changes_their_own_password_is_signed_out_too():
    """No exception means no exception, including for the one holding the door.

    The accepted cost of R129's fourth decision, pinned so that it reads
    as a rule rather than a bug the next person files.
    """
    admin = site_admin(localname="selfadmin", password=STRONG)
    c = Client()
    c.force_login(admin)
    assert c.get("/admin/", follow=False).status_code == 200

    set_password(admin, "a-different-strong-1", changed_by=admin)

    assert c.get("/admin/", follow=False).status_code == 302
    assert "/admin/login/" in c.get("/admin/", follow=False)["Location"]


@pytest.mark.django_db
def test_nothing_in_the_shipped_code_preserves_a_session_across_a_password_change():
    """The rule that keeps the eviction working is a prohibition, so it is tested.

    ``update_session_auth_hash`` exists to keep the acting session alive
    through a password change — exactly the exception R129 rules out. A
    scan over the shipped package is the only test that can hold that line,
    because nothing else here would fail if someone added the call.

    Matched as a call or an import rather than as a bare mention, because
    this file's own prose and ``passwords.py``'s docstring both name the
    function in order to forbid it. A needle that matched those would be a
    test that can only be satisfied by deleting the explanation.
    """
    import re
    from pathlib import Path

    usage = re.compile(
        r"update_session_auth_hash\s*\("  # a call
        r"|(?:import|from)\b[^#\n]*\bupdate_session_auth_hash\b"  # an import
    )
    root = Path(__file__).resolve().parents[1]
    hits = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "migrations" not in path.parts
        and "tests" not in path.parts
        and usage.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == [], f"update_session_auth_hash is used in {hits}"


# --- the admin's credential field --------------------------------------


@pytest.mark.django_db
def test_the_admin_can_set_a_members_password_and_the_member_logs_in_with_it(client):
    admin = site_admin(localname="setter", password=STRONG)
    target = member(localname="settarget", password=PASSWORD)
    ac = Client()
    ac.force_login(admin)
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = ac.post(
            f"/admin/social/user/{target.pk}/change/",
            {
                "display_name": "",
                "email": "settarget@example.test",
                "is_staff": "",
                "is_superuser": "",
                "is_moderator": "",
                "report_email": "",
                "password1": STRONG,
                "password2": STRONG,
            },
        )
    assert resp.status_code == 302
    target.refresh_from_db()
    assert target.check_password(STRONG) is True
    assert target.check_password(PASSWORD) is False
    assert Client().login(username="settarget", password=STRONG) is True


@pytest.mark.django_db
def test_blank_password_boxes_leave_the_credential_alone(client):
    admin = site_admin(localname="untouched", password=STRONG)
    target = member(localname="kept", password=PASSWORD)
    before = target.password
    ac = Client()
    ac.force_login(admin)
    with TestCase.captureOnCommitCallbacks(execute=True):
        ac.post(
            f"/admin/social/user/{target.pk}/change/",
            {
                "display_name": "Kept",
                "email": "kept@example.test",
                "is_staff": "",
                "is_superuser": "",
                "is_moderator": "",
                "report_email": "",
                "password1": "",
                "password2": "",
            },
        )
    target.refresh_from_db()
    assert target.password == before
    assert target.check_password(PASSWORD) is True


@pytest.mark.django_db
def test_the_admin_change_form_rejects_a_mismatched_pair(client):
    admin = site_admin(localname="matcher", password=STRONG)
    target = member(localname="mismatched", password=PASSWORD)
    ac = Client()
    ac.force_login(admin)
    resp = ac.post(
        f"/admin/social/user/{target.pk}/change/",
        {
            "display_name": "",
            "email": "mismatched@example.test",
            "is_staff": "",
            "is_superuser": "",
            "is_moderator": "",
            "report_email": "",
            "password1": STRONG,
            "password2": "not-the-same-at-all",
        },
    )
    assert resp.status_code == 200
    target.refresh_from_db()
    assert target.check_password(PASSWORD) is True


@pytest.mark.django_db
def test_an_admin_set_password_queues_the_notice_the_member_needs(client):
    """Asserted against the queue, not the outbox.

    The request must not talk to a mail server, so the enqueue is the
    observable at this layer — the same reason 2F's admin-send test reads
    ``OrmQ`` rather than ``mail.outbox``.
    """
    admin = site_admin(localname="notifier", password=STRONG)
    target = member(localname="noticed", password=PASSWORD)
    ac = Client()
    ac.force_login(admin)
    with TestCase.captureOnCommitCallbacks(execute=True):
        ac.post(
            f"/admin/social/user/{target.pk}/change/",
            {
                "display_name": "",
                "email": "noticed@example.test",
                "is_staff": "",
                "is_superuser": "",
                "is_moderator": "",
                "report_email": "",
                "password1": STRONG,
                "password2": STRONG,
            },
        )
    funcs = [q["func"] for q in queued()]
    assert funcs == [SEND_PW_NOTICE_FUNC]
    assert [list(q["args"]) for q in queued()] == [[target.pk]]


@pytest.mark.django_db
def test_the_password_changed_notice_carries_no_link(db):
    # Same control R125 forces on the address-change mail: this goes to an
    # address the person who changed the password may still be able to
    # read, so anything clickable in it is something they could act on.
    #
    # The worker task is called directly here so the rendered body can be
    # read. That is the unit the assertion is about — the mail's shape, not
    # the queue's.
    from reeltalk.social.tasks import send_password_changed_notice

    admin = site_admin(localname="linkless", password=STRONG)
    target = member(localname="nolinks", password=PASSWORD)
    set_password(target, STRONG, changed_by=admin)
    send_password_changed_notice(target.pk)
    assert len(mail.outbox) == 1
    body = mail.outbox[0].body
    for needle in ("http://", "https://", "www.", "/account/", "click "):
        assert needle not in body, f"notice contains {needle!r}"
    assert mail.outbox[0].to == ["nolinks@example.test"]


@pytest.mark.django_db
def test_a_self_change_queues_no_notice(db):
    # The person who just typed a new password does not need to be told
    # they typed it. Checked on the queue rather than the outbox: nothing
    # reaches the outbox from this layer whatever happens, so an empty
    # outbox would prove nothing at all.
    user = member(localname="selfchanged", password=PASSWORD)
    with TestCase.captureOnCommitCallbacks(execute=True):
        set_password(user, STRONG, changed_by=user)
    assert queued() == []


@pytest.mark.django_db
def test_the_admin_page_shows_the_password_inputs_and_the_reset_ledger(client):
    admin = site_admin(localname="viewer", password=STRONG)
    target = member(localname="viewed", password=PASSWORD)
    PasswordResetToken.mint(target)
    ac = Client()
    ac.force_login(admin)
    html = ac.get(f"/admin/social/user/{target.pk}/change/").content.decode()
    assert 'name="password1"' in html
    assert "disabled" not in html.split('name="password1"')[1][:200]
    assert "Password reset sends" in html


# --- the route contract -------------------------------------------------


@pytest.mark.django_db
def test_the_reset_route_is_bound_to_the_contract_path_in_the_mail():
    assert RESET_PATH == "/account/password-reset/"
    assert reverse("password-reset") == RESET_PATH


@pytest.mark.django_db
def test_the_reset_routes_are_declared_exempt_from_the_gate():
    from reeltalk.social import views as social_views

    assert "password-reset" in social_views.GATED_EXEMPT_URLS
    assert "password-reset-confirm" in social_views.GATED_EXEMPT_URLS


@pytest.mark.django_db
def test_module_exposes_its_own_budget_names_not_resends():
    # A cheap guard against someone "tidying" the duplication away by
    # aliasing RESET_* back onto RESEND_*, which is the exact change that
    # re-opens the denial-of-service R129 separated them for.
    assert RESET_ADDRESS_COOLDOWN_MINUTES == 5
    assert RESET_IP_LIMIT == 25
    assert password_reset.RESET_ADDRESS_COOLDOWN_MINUTES is not None
    from reeltalk.social import verify

    assert verify.RESEND_IP_LIMIT == password_reset.RESET_IP_LIMIT
    assert verify.ip_budget_spent is not password_reset.reset_ip_budget_spent
