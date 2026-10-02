"""Credential-surface throttling: login, admin login, and signup.

Every test here drives a real HTTP request through the real route, so each
surface is exercised at its own seam — the shared login mixin for
``/login/`` and ``/admin/login/``, the signup view for ``/signup/``. A
test that only touched ``/login/`` would prove nothing about the other two,
which is the failure mode the three-way split exists to avoid.

Source addresses are set per request with ``REMOTE_ADDR``. The addresses
used are public (TEST-NET, ``203.0.113.0/24``), so ``client_ip()``
resolves each straight to the peer and ignores any forwarded header — which
is itself one of the properties under test.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from reeltalk.social import attempts
from reeltalk.social.attempts import (
    ADMIN_LOGIN_IP_LIMIT,
    LOGIN_IP_LIMIT,
    LOGIN_IP_WINDOW_MINUTES,
    SIGNUP_IP_LIMIT,
)
from reeltalk.social.models import CredentialAttempt, SiteSettings
from reeltalk.tests.members import PASSWORD, member, site_admin, unverified_member

REVEAL = "Too many attempts from this address"


def _login(client, ip, *, username, password):
    return client.post(
        reverse("login"),
        {"username": username, "password": password},
        REMOTE_ADDR=ip,
    )


def _admin_login(client, ip, *, username, password):
    return client.post(
        "/admin/login/",
        {"username": username, "password": password},
        REMOTE_ADDR=ip,
    )


def _signup(client, ip, *, name, email):
    return client.post(
        reverse("signup"),
        {
            "localname": name,
            "display_name": "",
            "email": email,
            "password1": "s3cretpass",
            "password2": "s3cretpass",
        },
        REMOTE_ADDR=ip,
    )


def _count(surface, ip):
    return CredentialAttempt.objects.filter(surface=surface, source_ip=ip).count()


# --- the public sign-in surface -------------------------------------


@pytest.mark.django_db
def test_wrong_passwords_up_to_the_limit_are_ordinary_failures(client):
    member(localname="target", password=PASSWORD)
    ip = "203.0.113.11"
    for _ in range(LOGIN_IP_LIMIT):
        resp = _login(client, ip, username="target", password="nope")
        assert resp.status_code == 200
        assert REVEAL not in resp.content.decode()
    assert _count(CredentialAttempt.LOGIN, ip) == LOGIN_IP_LIMIT


@pytest.mark.django_db
def test_the_next_wrong_password_after_the_limit_is_blocked(client):
    member(localname="target", password=PASSWORD)
    ip = "203.0.113.12"
    for _ in range(LOGIN_IP_LIMIT):
        _login(client, ip, username="target", password="nope")
    resp = _login(client, ip, username="target", password="nope")
    assert REVEAL in resp.content.decode()
    assert attempts.attempts_blocked(CredentialAttempt.LOGIN, ip) is True


@pytest.mark.django_db
def test_a_correct_login_clears_the_address_counter(client):
    member(localname="clearer", password=PASSWORD)
    ip = "203.0.113.13"
    for _ in range(LOGIN_IP_LIMIT - 1):
        _login(client, ip, username="clearer", password="nope")
    assert _count(CredentialAttempt.LOGIN, ip) == LOGIN_IP_LIMIT - 1
    resp = _login(client, ip, username="clearer", password=PASSWORD)
    assert resp.status_code == 302
    assert _count(CredentialAttempt.LOGIN, ip) == 0, (
        "a good login must reset the budget"
    )


@pytest.mark.django_db
def test_a_blocked_address_frees_itself_when_the_window_passes(client):
    member(localname="waiter", password=PASSWORD)
    ip = "203.0.113.14"
    for _ in range(LOGIN_IP_LIMIT):
        _login(client, ip, username="waiter", password="nope")
    assert attempts.attempts_blocked(CredentialAttempt.LOGIN, ip) is True
    # Age every attempt past the window; no admin action, no clearing hand.
    CredentialAttempt.objects.filter(
        surface=CredentialAttempt.LOGIN, source_ip=ip
    ).update(created_at=timezone.now() - timedelta(minutes=LOGIN_IP_WINDOW_MINUTES + 1))
    assert attempts.attempts_blocked(CredentialAttempt.LOGIN, ip) is False
    resp = _login(client, ip, username="waiter", password=PASSWORD)
    assert resp.status_code == 302


@pytest.mark.django_db
def test_one_blocked_address_does_not_spill_to_another(client):
    member(localname="shared", password=PASSWORD)
    spent = "203.0.113.21"
    other = "203.0.113.22"
    for _ in range(LOGIN_IP_LIMIT):
        _login(client, spent, username="shared", password="nope")
    assert attempts.attempts_blocked(CredentialAttempt.LOGIN, spent) is True
    assert attempts.attempts_blocked(CredentialAttempt.LOGIN, other) is False
    resp = _login(client, other, username="shared", password=PASSWORD)
    assert resp.status_code == 302


@pytest.mark.django_db
def test_correct_password_on_an_unverified_account_is_not_counted(client):
    # The password was right, so it is not a guess against the budget.
    # Counting it would let someone poking at their own not-yet-verified
    # account drain their address for a reason unrelated to guessing.
    unverified_member(localname="pending", password=PASSWORD)
    ip = "203.0.113.23"
    for _ in range(LOGIN_IP_LIMIT + 3):
        resp = _login(client, ip, username="pending", password=PASSWORD)
        assert resp.status_code == 200
    assert _count(CredentialAttempt.LOGIN, ip) == 0
    assert attempts.attempts_blocked(CredentialAttempt.LOGIN, ip) is False


@pytest.mark.django_db
def test_the_throttle_says_the_same_thing_about_a_real_account_and_a_ghost(client):
    # The reveal names the address, never an account, so a blocked attempt
    # against a name that exists reads exactly like one against a name that
    # does not. This is the enumeration surface §2F/§2G refused to open.
    member(localname="real", password=PASSWORD)
    real_ip = "203.0.113.71"
    ghost_ip = "203.0.113.72"
    for _ in range(LOGIN_IP_LIMIT):
        _login(client, real_ip, username="real", password="nope")
        _login(client, ghost_ip, username="nobodyhere", password="nope")
    real_body = _login(
        client, real_ip, username="real", password="nope"
    ).content.decode()
    ghost_body = _login(
        client, ghost_ip, username="nobodyhere", password="nope"
    ).content.decode()
    assert REVEAL in real_body
    assert REVEAL in ghost_body


@pytest.mark.django_db
def test_a_spoofed_forwarded_header_does_not_escape_the_bucket(client):
    member(localname="target2", password=PASSWORD)
    peer = "203.0.113.61"  # untrusted peer: client_ip() ignores the header
    for _ in range(LOGIN_IP_LIMIT):
        _login(client, peer, username="target2", password="nope")
    assert attempts.attempts_blocked(CredentialAttempt.LOGIN, peer) is True
    resp = client.post(
        reverse("login"),
        {"username": "target2", "password": "nope"},
        REMOTE_ADDR=peer,
        HTTP_X_FORWARDED_FOR="9.9.9.9",
    )
    assert REVEAL in resp.content.decode()


# --- the admin sign-in surface --------------------------------------


@pytest.mark.django_db
def test_admin_login_throttles_on_its_own_budget(client):
    site_admin(localname="boss", password=PASSWORD)
    ip = "203.0.113.31"
    for _ in range(ADMIN_LOGIN_IP_LIMIT):
        resp = _admin_login(client, ip, username="boss", password="nope")
        assert REVEAL not in resp.content.decode()
    resp = _admin_login(client, ip, username="boss", password="nope")
    assert REVEAL in resp.content.decode()
    assert attempts.attempts_blocked(CredentialAttempt.ADMIN, ip) is True


@pytest.mark.django_db
def test_a_correct_admin_login_clears_the_admin_counter(client):
    site_admin(localname="boss2", password=PASSWORD)
    ip = "203.0.113.32"
    for _ in range(ADMIN_LOGIN_IP_LIMIT - 1):
        _admin_login(client, ip, username="boss2", password="nope")
    assert _count(CredentialAttempt.ADMIN, ip) == ADMIN_LOGIN_IP_LIMIT - 1
    resp = _admin_login(client, ip, username="boss2", password=PASSWORD)
    assert resp.status_code == 302
    assert _count(CredentialAttempt.ADMIN, ip) == 0


@pytest.mark.django_db
def test_a_correct_password_on_a_non_staff_account_neither_counts_nor_clears(client):
    # A member with a correct password cannot reach the admin, so this is
    # not a password guess (the password was right) and not a success (the
    # staff check refused them). ``confirm_login_allowed`` raises before
    # either the clear or the record, so the attempt is neutral: it does
    # not reward a fresh budget, and it does not count as a guess.
    member(localname="plainjane", password=PASSWORD)
    ip = "203.0.113.33"
    resp = _admin_login(client, ip, username="plainjane", password=PASSWORD)
    assert resp.status_code == 200  # refused: not a staff account
    assert _count(CredentialAttempt.ADMIN, ip) == 0


# --- the signup surface ---------------------------------------------


@pytest.mark.django_db
def test_signup_throttles_attempts_that_reach_the_checks(client):
    site_admin(localname="root", password=PASSWORD)  # satisfies has_admin; OPEN
    assert SiteSettings.get_instance().signup_policy == SiteSettings.OPEN
    ip = "203.0.113.41"
    for i in range(SIGNUP_IP_LIMIT):
        resp = _signup(client, ip, name=f"newbie{i}", email=f"newbie{i}@e.test")
        assert resp.status_code == 302
        assert REVEAL not in resp.content.decode()
    # A fresh, free name from the now-spent address is still blocked.
    resp = _signup(client, ip, name="freshname", email="fresh@e.test")
    assert REVEAL in resp.content.decode()
    assert attempts.attempts_blocked(CredentialAttempt.SIGNUP, ip) is True


@pytest.mark.django_db
def test_a_blocked_signup_does_not_leak_that_a_name_is_taken(client):
    site_admin(localname="root2", password=PASSWORD)
    member(localname="existing", password=PASSWORD)  # occupies the name
    ip = "203.0.113.42"
    for i in range(SIGNUP_IP_LIMIT):
        _signup(client, ip, name=f"free{i}", email=f"free{i}@e.test")
    resp = _signup(client, ip, name="existing", email="someone@e.test")
    body = resp.content.decode()
    assert REVEAL in body
    assert "already taken" not in body, "a blocked signup must not answer the oracle"


@pytest.mark.django_db
def test_a_signup_that_reaches_no_uniqueness_check_is_not_counted(client):
    site_admin(localname="root3", password=PASSWORD)
    ip = "203.0.113.43"
    # Malformed name (fails the format regex before any lookup) and a
    # malformed email (fails field validation, so clean_email never runs):
    # neither uniqueness query executes, so nothing is counted.
    resp = client.post(
        reverse("signup"),
        {
            "localname": "!!!bad!!!",
            "display_name": "",
            "email": "not-an-email",
            "password1": "s3cretpass",
            "password2": "s3cretpass",
        },
        REMOTE_ADDR=ip,
    )
    assert resp.status_code == 200
    assert _count(CredentialAttempt.SIGNUP, ip) == 0


@pytest.mark.django_db
def test_a_signup_that_reached_the_checks_but_failed_password_still_counts(client):
    site_admin(localname="root4", password=PASSWORD)
    ip = "203.0.113.44"
    resp = client.post(
        reverse("signup"),
        {
            "localname": "goodname",
            "display_name": "",
            "email": "good@e.test",
            "password1": "s3cretpass",
            "password2": "mismatch",
        },
        REMOTE_ADDR=ip,
    )
    assert resp.status_code == 200  # the passwords didn't match
    assert _count(CredentialAttempt.SIGNUP, ip) == 1, (
        "reaching the name/email oracle is the counted event"
    )


@pytest.mark.django_db
def test_a_successful_signup_does_not_clear_the_signup_counter(client):
    site_admin(localname="root6", password=PASSWORD)
    ip = "203.0.113.45"
    _signup(client, ip, name="one", email="one@e.test")
    assert _count(CredentialAttempt.SIGNUP, ip) == 1
    # Registration is not a "correct credential" event, so it adds to the
    # budget rather than resetting it the way a good login does.
    _signup(client, ip, name="two", email="two@e.test")
    assert _count(CredentialAttempt.SIGNUP, ip) == 2


# --- the three budgets never drain one another -----------------------
#
# Asserted directly through ``attempts_blocked`` on the *other* surfaces
# rather than by driving an HTTP action on them: a login-then-signup client
# sequence is confounded by the auth redirect (a logged-in client hitting
# /signup/ bounces to the index without ever reaching the throttle), so a
# 302 there would prove nothing. The direct read is the unambiguous proof
# that filling one surface's count leaves the other two's budgets intact.


@pytest.mark.django_db
def test_exhausting_login_does_not_starve_signup_or_admin(client):
    member(localname="loginuser", password=PASSWORD)
    ip = "203.0.113.51"
    for _ in range(LOGIN_IP_LIMIT):
        _login(client, ip, username="loginuser", password="nope")
    assert attempts.attempts_blocked(CredentialAttempt.LOGIN, ip) is True
    assert attempts.attempts_blocked(CredentialAttempt.SIGNUP, ip) is False
    assert attempts.attempts_blocked(CredentialAttempt.ADMIN, ip) is False


@pytest.mark.django_db
def test_exhausting_signup_does_not_starve_login_or_admin(client):
    # An admin must exist or /signup/ redirects to the setup wizard and
    # never reaches the throttle at all.
    site_admin(localname="root7", password=PASSWORD)
    ip = "203.0.113.52"
    for i in range(SIGNUP_IP_LIMIT):
        _signup(client, ip, name=f"reg{i}", email=f"reg{i}@e.test")
    assert attempts.attempts_blocked(CredentialAttempt.SIGNUP, ip) is True
    assert attempts.attempts_blocked(CredentialAttempt.LOGIN, ip) is False
    assert attempts.attempts_blocked(CredentialAttempt.ADMIN, ip) is False


@pytest.mark.django_db
def test_exhausting_admin_does_not_starve_login_or_signup(client):
    site_admin(localname="root8", password=PASSWORD)
    ip = "203.0.113.53"
    for _ in range(ADMIN_LOGIN_IP_LIMIT):
        _admin_login(client, ip, username="root8", password="nope")
    assert attempts.attempts_blocked(CredentialAttempt.ADMIN, ip) is True
    assert attempts.attempts_blocked(CredentialAttempt.LOGIN, ip) is False
    assert attempts.attempts_blocked(CredentialAttempt.SIGNUP, ip) is False


# --- the reveal wording and the loud-failure guard -------------------


def test_reveal_message_rounds_the_wait_up():
    now = timezone.now()
    assert "a minute" in attempts.reveal_message(now + timedelta(seconds=30))
    assert "3 minutes" in attempts.reveal_message(
        now + timedelta(minutes=2, seconds=30)
    )
    assert "5 minutes" in attempts.reveal_message(now + timedelta(minutes=5))


def test_an_unknown_surface_raises_rather_than_running_unthrottled():
    # A surface with no budget is a wiring bug; defaulting it to unlimited
    # would recreate the "two of three surfaces covered" state silently.
    with pytest.raises(ValueError):
        attempts.attempts_blocked("bogus_surface", "203.0.113.99")
