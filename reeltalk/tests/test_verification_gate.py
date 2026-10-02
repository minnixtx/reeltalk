"""R119's gate, R122's exit, and the routes between them (2F-3).

What gets pinned here, and why each one earns its place:

* **The gate refuses and admits, with no exception for the admin.** R119 was
  offered a ``is_superuser`` exemption on R114's reasoning and the owner
  declined it, so the admin case is tested rather than assumed — a gate with
  a hole in it looks identical to a gate from the outside.
* **An existing session is cut on its next click.** That is not a bonus of
  the chosen seam, it is the seam: ``get_user()`` runs ``user_can_authenticate``
  on every request, so moving a signed-in member's address logs them out.
* **The login page names the real reason.** Django's stock copy sends an
  unverified member to a password reset that does not exist (R121).
* **The verify link is specific; the resend form is not.** The enumeration
  surface is the one where you submit an address. The token path needs a
  ~192-bit secret to reach and is free to explain itself.
* **The resend cooldown has two axes.** Per-address alone is not a throttle
  against a caller cycling addresses (R107: dedup is not rate limiting).
* **The recovery surfaces stay reachable through the gate, by name** — the
  gate is only acceptable because the exit exists, so the exit is pinned
  rather than trusted to "by construction".
* **The session key changes across the confirm boundary**, anti-fixation.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import SESSION_KEY
from django.core import mail
from django.test import Client, TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from reeltalk.social import views as social_views
from reeltalk.social.backends import (
    REFUSAL_CREDENTIALS,
    REFUSAL_INACTIVE,
    REFUSAL_UNVERIFIED,
    EmailVerificationBackend,
)
from reeltalk.social.models import EmailVerificationToken, User
from reeltalk.social.password_reset import RESET_PATH
from reeltalk.social.verify import (
    RESEND_ADDRESS_COOLDOWN_MINUTES,
    RESEND_IP_LIMIT,
    VERIFY_PATH,
    address_in_cooldown,
    change_email,
    ip_budget_spent,
    request_resend,
    verification_url,
)
from reeltalk.tests.members import (
    PASSWORD,
    member,
    site_admin,
    unverified_member,
)

RESEND_URL = reverse("verify-resend")


def _backend():
    return EmailVerificationBackend()


def _rewind(token, **fields):
    """Backdate a token's ``auto_now_add`` clock.

    ``created_at`` is ``auto_now_add``, so a plain assignment followed by a
    normal ``save()`` is overwritten on write. Naming the field in
    ``update_fields`` is what lets a test reach the clock it needs to move.
    """
    for name, value in fields.items():
        setattr(token, name, value)
    token.save(update_fields=list(fields))


# --- the gate refuses and admits ----------------------------------------


@pytest.mark.django_db
def test_an_unverified_member_cannot_authenticate():
    unverified_member(localname="nope", password=PASSWORD)
    assert _backend().authenticate(None, username="nope", password=PASSWORD) is None


@pytest.mark.django_db
def test_a_verified_member_can_authenticate():
    user = member(localname="yes", password=PASSWORD)
    assert _backend().authenticate(None, username="yes", password=PASSWORD) == user


@pytest.mark.django_db
def test_the_admin_gets_no_exemption_from_the_gate():
    # R119, tested rather than asserted. ``is_superuser`` changes nothing
    # here: the owner was offered the exemption and said no, so an
    # unverified admin is refused exactly like an unverified member.
    admin = unverified_member(localname="boss", password=PASSWORD, is_superuser=True)
    assert admin.is_superuser is True
    assert admin.email_verified is False
    assert _backend().authenticate(None, username="boss", password=PASSWORD) is None
    # And the same account gets in once it has actually verified, so the
    # refusal above is about verification and not about the role.
    admin.email_verified_at = timezone.now()
    admin.verified_email = admin.email
    admin.save(update_fields=["email_verified_at", "verified_email"])
    assert _backend().authenticate(None, username="boss", password=PASSWORD) == admin


@pytest.mark.django_db
def test_suspension_and_ban_still_refuse_a_verified_account():
    # The three refusals stay independent. Verification does not rescue a
    # suspended account, and suspension does not undo a verification.
    suspended = member(localname="paused", password=PASSWORD)
    suspended.suspend(reason="cooling off")
    assert suspended.email_verified is True
    assert _backend().authenticate(None, username="paused", password=PASSWORD) is None

    banned = member(localname="gone", password=PASSWORD)
    banned.ban(reason="bad day")
    assert banned.email_verified is True
    assert _backend().authenticate(None, username="gone", password=PASSWORD) is None


# --- why_refused: the diagnostic the login page needs -------------------


@pytest.mark.django_db
def test_why_refused_names_each_refusal():
    member(localname="verified", password=PASSWORD)
    unverified_member(localname="pending", password=PASSWORD)
    banned = member(localname="banned", password=PASSWORD)
    banned.ban(reason="gone")

    b = _backend()
    assert b.why_refused("verified", PASSWORD) is None
    assert b.why_refused("pending", PASSWORD) == REFUSAL_UNVERIFIED
    assert b.why_refused("banned", PASSWORD) == REFUSAL_INACTIVE
    assert b.why_refused("nobody", PASSWORD) == REFUSAL_CREDENTIALS
    assert b.why_refused("pending", "wrong-password-1") == REFUSAL_CREDENTIALS


@pytest.mark.django_db
def test_why_refused_returns_a_string_and_never_a_user():
    # The diagnostic must not be a way to obtain an account object. It
    # reports; it does not hand anything over.
    user = unverified_member(localname="reportonly", password=PASSWORD)
    reason = _backend().why_refused("reportonly", PASSWORD)
    assert isinstance(reason, str)
    assert not isinstance(reason, User)
    assert reason == REFUSAL_UNVERIFIED
    assert user.pk is not None


# --- the gate reaches sessions that already exist -----------------------


@pytest.mark.django_db
def test_a_signed_in_member_is_cut_off_when_their_address_moves():
    # The consequence of putting the gate in ``user_can_authenticate``
    # rather than only at ``authenticate()``: ``get_user()`` runs it on
    # every request, so the session does not outlive the proof.
    #
    # Probed through a page that *requires* a session. A public page like
    # /about/ answers 200 either way and would prove nothing — the gate cuts
    # the identity, not the transport, so the assertion has to be made
    # where an anonymous visitor is turned away.
    admin = site_admin(localname="editor", password=PASSWORD)
    victim = member(localname="resident", password=PASSWORD)
    client = Client()
    assert client.login(username="resident", password=PASSWORD)
    assert client.session[SESSION_KEY] == str(victim.pk)
    assert client.get(reverse("notifications")).status_code == 200

    change_email(victim, "moved@example.test", changed_by=admin)

    victim.refresh_from_db()
    assert victim.email_verified is False
    after = client.get(reverse("notifications"), follow=False)
    assert after.status_code == 302
    assert "/login/" in after["Location"]


@pytest.mark.django_db
def test_the_cut_session_carries_no_identity_even_though_the_cookie_survives():
    # The cookie is still there and still holds the user id -- what changed is
    # that the backend will not hand the account back. Asserting the identity
    # is gone rather than that the cookie was cleared, because nothing cleared
    # it, and a test that looked for a cleared cookie would be testing a
    # mechanism that does not exist.
    victim = member(localname="carried", password=PASSWORD)
    client = Client()
    assert client.login(username="carried", password=PASSWORD)
    held = client.session[SESSION_KEY]
    assert held == str(victim.pk)

    admin = site_admin(localname="mover", password=PASSWORD)
    change_email(victim, "carried-new@example.test", changed_by=admin)

    assert client.session[SESSION_KEY] == held
    resp = client.get(reverse("profile-edit"), follow=False)
    assert resp.status_code == 302
    assert resp["Location"].startswith("/login/")


# --- the login page distinguishes the two failures --------------------


@pytest.mark.django_db
def test_wrong_password_gets_the_generic_message(client):
    unverified_member(localname="person", password=PASSWORD)
    resp = client.post(reverse("login"), {"username": "person", "password": "nope-123"})
    assert resp.status_code == 200
    form = resp.context["form"]
    assert form.not_verified is False
    assert any("correct" in str(e).lower() for e in form.errors["__all__"])
    assert "not confirmed" not in resp.content.decode()


@pytest.mark.django_db
def test_a_correct_password_on_an_unverified_account_names_the_real_reason(client):
    # The whole point of the custom form. Same credentials that would have
    # produced "enter a correct username and password" before, and now the
    # page says what is actually wrong and links to the thing that fixes it.
    unverified_member(localname="person", password=PASSWORD)
    resp = client.post(reverse("login"), {"username": "person", "password": PASSWORD})
    assert resp.status_code == 200
    form = resp.context["form"]
    assert form.not_verified is True
    body = resp.content.decode()
    assert "not confirmed its email address" in body
    assert f'href="{RESEND_URL}"' in body
    # And nobody got a session.
    assert SESSION_KEY not in client.session


@pytest.mark.django_db
def test_an_unverified_admin_sees_the_same_thing_at_the_admin_login(client):
    # The admin is the person least able to act on a "wrong password" about
    # a password they know is right, so the admin's own login page names the
    # real obstacle. ``is_staff`` is in the fixture because that is what the
    # admin is made of — and because the admin form only tells this story to
    # accounts that could have reached the admin.
    unverified_member(
        localname="boss", password=PASSWORD, is_superuser=True, is_staff=True
    )
    resp = client.post(
        "/admin/login/", {"username": "boss", "password": PASSWORD}, follow=False
    )
    assert resp.status_code == 200
    assert "not confirmed its email address" in resp.content.decode()
    assert SESSION_KEY not in client.session


@pytest.mark.django_db
def test_the_admin_login_stays_silent_about_a_member_it_could_never_admit(
    client,
):
    # The other half of the copy decision. A plain member has no path into
    # the admin no matter what they verify, so telling them "your password
    # was right but your email is unverified" would only make this form a
    # better password oracle than Django's default one. They get exactly
    # what Django's own admin form says.
    unverified_member(localname="passerby", password=PASSWORD)
    resp = client.post(
        "/admin/login/",
        {"username": "passerby", "password": PASSWORD},
        follow=False,
    )
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "not confirmed its email address" not in body
    assert "staff account" in body
    assert SESSION_KEY not in client.session


@pytest.mark.django_db
def test_a_verified_member_logs_in_through_the_same_form(client):
    user = member(localname="inbound", password=PASSWORD)
    resp = client.post(
        reverse("login"),
        {"username": "inbound", "password": PASSWORD},
        follow=False,
    )
    assert resp.status_code == 302
    # The session really holds this account, decoded rather than assumed.
    assert client.session[SESSION_KEY] == str(user.pk)


# --- the verify link --------------------------------------------------


@pytest.mark.django_db
def test_the_route_is_bound_to_the_contract_path_in_the_mail():
    # 2F-2's ``VERIFY_PATH`` is a contract between increments: the link in
    # the mail must resolve to a real view. Pinned through the resolver on
    # the URL the mail actually carries, rather than by eyeballing two
    # strings that happen to match today.
    from urllib.parse import urlsplit

    user = member(localname="contract")
    token = EmailVerificationToken.mint(user)
    url = verification_url(token)
    assert VERIFY_PATH in url

    match = resolve(urlsplit(url).path)
    assert match.func is social_views.verify_link
    assert match.kwargs["code"] == token.code


@pytest.mark.django_db
def test_following_a_live_link_verifies_the_account():
    user = unverified_member(localname="clicker", password=PASSWORD)
    token = EmailVerificationToken.mint(user)
    assert user.email_verified is False

    resp = Client().get(f"{VERIFY_PATH}{token.code}/")
    assert resp.status_code == 200
    assert "Email verified" in resp.content.decode()

    user.refresh_from_db()
    token.refresh_from_db()
    assert user.email_verified is True
    assert user.verified_email == user.email
    assert user.email_verified_at is not None
    assert token.used_at is not None
    assert token.is_live is False


@pytest.mark.django_db
def test_a_link_cannot_be_spent_twice():
    user = unverified_member(localname="twice", password=PASSWORD)
    token = EmailVerificationToken.mint(user)
    client = Client()
    client.get(f"{VERIFY_PATH}{token.code}/")
    user.refresh_from_db()
    first_verified_at = user.email_verified_at

    second = client.get(f"{VERIFY_PATH}{token.code}/")
    assert second.status_code == 200
    assert "already been used" in second.content.decode()
    user.refresh_from_db()
    # The replay changed nothing — not the timestamp, not the state.
    assert user.email_verified_at == first_verified_at


@pytest.mark.django_db
def test_an_expired_link_says_so_with_its_date():
    # Specific copy is allowed here because reaching this page required a
    # token. Telling someone their link expired on a named date, with a way
    # to get a new one, is the difference between a recoverable annoyance
    # and a dead end.
    user = unverified_member(localname="stale", password=PASSWORD)
    token = EmailVerificationToken.mint(user)
    _rewind(token, expires_at=timezone.now() - timedelta(hours=1))

    body = Client().get(f"{VERIFY_PATH}{token.code}/").content.decode()
    assert "expired" in body
    assert f'href="{RESEND_URL}"' in body
    user.refresh_from_db()
    assert user.email_verified is False


@pytest.mark.django_db
def test_a_superseded_link_points_at_the_newest_one():
    user = unverified_member(localname="resend", password=PASSWORD)
    old = EmailVerificationToken.mint(user)
    EmailVerificationToken.mint(user)

    body = Client().get(f"{VERIFY_PATH}{old.code}/").content.decode()
    assert "replaced by a newer one" in body
    user.refresh_from_db()
    assert user.email_verified is False


@pytest.mark.django_db
def test_a_link_cannot_verify_an_address_it_was_not_minted_for():
    # R120's binding, driven through the route rather than only the model.
    #
    # Two shapes, because the route reaches the mismatch branch only when
    # something wrote the address *without* going through ``change_email``:
    # the single writer supersedes live tokens as it moves the address, so
    # the normal path reports "replaced by a newer one" instead. Both are
    # asserted, because the second is the defence-in-depth case -- a writer
    # that bypasses the single writer must still not be able to verify an
    # unproven address.
    user = unverified_member(localname="typo", password=PASSWORD)
    token = EmailVerificationToken.mint(user)
    admin = site_admin(localname="fixer", password=PASSWORD)
    change_email(user, "corrected@example.test", changed_by=admin)

    body = Client().get(f"{VERIFY_PATH}{token.code}/").content.decode()
    assert "replaced by a newer one" in body
    user.refresh_from_db()
    assert user.email_verified is False

    # Now the bypass: a direct write that does not supersede. The binding is
    # the only thing left standing between the old link and the new address.
    other = unverified_member(localname="bypassed", password=PASSWORD)
    stale = EmailVerificationToken.mint(other)
    other.email = "other-new@example.test"
    other.save(update_fields=["email"])
    assert EmailVerificationToken.objects.filter(
        pk=stale.pk, superseded_at__isnull=True
    ).exists()

    body = Client().get(f"{VERIFY_PATH}{stale.code}/").content.decode()
    assert "different address" in body
    other.refresh_from_db()
    assert other.email_verified is False
    assert other.verified_email != other.email


@pytest.mark.django_db
def test_an_unknown_code_is_refused_without_touching_anyone(db):
    before = User.objects.filter(email_verified_at__isnull=False).count()
    body = Client().get(f"{VERIFY_PATH}{'z' * 43}/").content.decode()
    assert "not valid" in body
    assert User.objects.filter(email_verified_at__isnull=False).count() == before


@pytest.mark.django_db
def test_the_verify_endpoint_is_get_only():
    user = unverified_member(localname="poster", password=PASSWORD)
    token = EmailVerificationToken.mint(user)
    resp = Client().post(f"{VERIFY_PATH}{token.code}/")
    assert resp.status_code == 405
    user.refresh_from_db()
    assert user.email_verified is False


@pytest.mark.django_db
def test_the_session_key_changes_across_the_confirm_boundary():
    # Anti-session-fixation: verification changes what the account *is*, so
    # the session that arrived with it should not survive unchanged.
    user = unverified_member(localname="cycled", password=PASSWORD)
    token = EmailVerificationToken.mint(user)
    client = Client()
    client.get(reverse("login"))
    key_before = client.session.session_key
    assert key_before

    client.get(f"{VERIFY_PATH}{token.code}/")
    key_after = client.session.session_key
    assert key_after != key_before
    user.refresh_from_db()
    assert user.email_verified is True


# --- the resend route: both axes --------------------------------------


@pytest.mark.django_db
def test_the_resend_page_renders_logged_out(client):
    resp = client.get(RESEND_URL)
    assert resp.status_code == 200
    assert 'name="email"' in resp.content.decode()


@pytest.mark.django_db
def test_a_resend_mints_and_queues_a_fresh_token(client):
    user = unverified_member(localname="asker", password=PASSWORD)
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = client.post(RESEND_URL, {"email": "asker@example.test"})
    assert resp.status_code == 302
    assert EmailVerificationToken.objects.filter(user=user).count() == 1
    token = EmailVerificationToken.objects.get(user=user)
    assert token.is_live


@pytest.mark.django_db
def test_the_resend_records_the_requesting_source_on_the_token(client):
    # The per-IP axis has to be countable from the durable table, and that
    # only works if the source is actually written down.
    unverified_member(localname="sourced", password=PASSWORD)
    client.post(RESEND_URL, {"email": "sourced@example.test"})
    token = EmailVerificationToken.objects.get(user__localname="sourced")
    assert token.request_ip == "127.0.0.1"


@pytest.mark.django_db
def test_the_address_cooldown_refuses_inside_the_window(db):
    user = unverified_member(localname="patient", password=PASSWORD)
    request_resend("patient@example.test", "10.0.0.1")
    assert EmailVerificationToken.objects.filter(user=user).count() == 1
    assert address_in_cooldown("patient@example.test") is True

    result = request_resend("patient@example.test", "10.0.0.2")
    assert result["reason"] == "address-cooldown"
    assert EmailVerificationToken.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_the_address_cooldown_admits_outside_the_window(db):
    user = unverified_member(localname="later", password=PASSWORD)
    request_resend("later@example.test", "10.0.0.1")
    token = EmailVerificationToken.objects.get(user=user)
    _rewind(
        token,
        created_at=timezone.now()
        - timedelta(minutes=RESEND_ADDRESS_COOLDOWN_MINUTES + 1),
    )
    assert address_in_cooldown("later@example.test") is False

    request_resend("later@example.test", "10.0.0.1")
    assert EmailVerificationToken.objects.filter(user=user).count() == 2


@pytest.mark.django_db
def test_a_failed_send_does_not_make_the_member_wait_out_the_window(db):
    # The reason the cooldown reads ``send_error`` rather than ``created_at``
    # alone: mail that never left the box must not cost the member a wait.
    user = unverified_member(localname="failed", password=PASSWORD)
    request_resend("failed@example.test", "10.0.0.1")
    token = EmailVerificationToken.objects.get(user=user)
    token.send_error = "SMTPServerDisconnected: connection refused"
    token.save(update_fields=["send_error"])

    assert address_in_cooldown("failed@example.test") is False
    result = request_resend("failed@example.test", "10.0.0.1")
    assert result["enqueued"] == 1


@pytest.mark.django_db
def test_the_ip_axis_throttles_a_source_cycling_many_addresses(db):
    # The axis a per-address cooldown cannot provide: each address is fresh,
    # so nothing throttles it -- until the source runs out of budget.
    for i in range(RESEND_IP_LIMIT):
        unverified_member(localname=f"cycler{i}", email=f"cycler{i}@example.test")
        result = request_resend(f"cycler{i}@example.test", "10.9.9.9")
        assert result["enqueued"] == 1, f"send {i} should have gone out"

    assert ip_budget_spent("10.9.9.9") is True
    unverified_member(localname="one_more", email="one_more@example.test")
    blocked = request_resend("one_more@example.test", "10.9.9.9")
    assert blocked["reason"] == "ip-limited"
    assert not EmailVerificationToken.objects.filter(
        user__localname="one_more"
    ).exists()


@pytest.mark.django_db
def test_a_different_source_is_not_blocked_by_another_ones_budget(db):
    for i in range(RESEND_IP_LIMIT):
        unverified_member(localname=f"busy{i}", email=f"busy{i}@example.test")
        request_resend(f"busy{i}@example.test", "10.8.8.8")

    unverified_member(localname="fresh_source", email="fresh_source@example.test")
    result = request_resend("fresh_source@example.test", "10.7.7.7")
    assert result["enqueued"] == 1
    assert ip_budget_spent("10.7.7.7") is False


@pytest.mark.django_db
def test_an_unknown_ip_is_not_a_free_pass(db):
    # An empty source must not silently skip the axis by being uncountable in
    # the wrong direction: with no source recorded, nothing accumulates, so
    # the function declines to throttle rather than blocking every anonymous
    # request. The per-address axis still applies, which is the one that
    # protects the member.
    assert ip_budget_spent("") is False


# --- the response never reveals whether the address exists ------------


def _resend_page(email):
    client = Client()
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = client.post(RESEND_URL, {"email": email})
    assert resp.status_code == 302
    page = client.get(resp["Location"])
    messages = [str(m) for m in page.context["messages"]]
    return page.content.decode(), messages


@pytest.mark.django_db
def test_the_resend_response_is_identical_for_a_known_and_an_unknown_address(client):
    member(localname="known", email="known@example.test")
    known_body, known_msgs = _resend_page("known@example.test")
    unknown_body, unknown_msgs = _resend_page("nobody-at-all@example.test")

    assert known_msgs == unknown_msgs
    assert known_msgs, "expected a uniform confirmation message"
    assert "If that address has an account here" in known_msgs[0]
    # Nothing in either page names the account, the localname, or the fact
    # of registration.
    assert "known" not in unknown_body
    assert "no such" not in unknown_body.lower()
    assert "not registered" not in unknown_body.lower()


@pytest.mark.django_db
def test_a_throttled_resend_says_the_same_thing_as_a_successful_one(client):
    # The cooldown must not become an existence oracle: "we just sent one"
    # confirms the address is registered, which is the exact leak R122
    # closes. So the throttled answer is indistinguishable from the sent one.
    member(localname="twice_asked", email="twice_asked@example.test")
    first_body, first_msgs = _resend_page("twice_asked@example.test")
    second_body, second_msgs = _resend_page("twice_asked@example.test")
    assert first_msgs == second_msgs
    assert (
        EmailVerificationToken.objects.filter(user__localname="twice_asked").count()
        == 1
    )


@pytest.mark.django_db
def test_the_resend_form_rejects_a_non_address(client):
    resp = client.post(RESEND_URL, {"email": "not-an-email"})
    assert resp.status_code == 200
    assert "email" in resp.context["form"].errors


# --- the recovery surfaces stay reachable through the gate ------------


@pytest.mark.django_db
def test_the_named_exempt_routes_are_reachable_with_an_unverified_account(client):
    # Walking the declared list rather than spot-checking, so adding
    # ``login_required`` to any of these turns this red instead of silently
    # deleting the only exit from the lockout.
    #
    # The property is "not bounced to the sign-in page", not "answers 200":
    # ``/setup/`` legitimately redirects to the home page once an instance
    # has an admin, and that is not the gate. What must never happen is any
    # of these routes throwing the caller at ``/login/`` -- because a person
    # who cannot sign in is exactly who these pages are for, and a bounce
    # there is a loop with no door.
    site_admin(localname="exempt_admin", password=PASSWORD)
    locked = unverified_member(localname="locked", password=PASSWORD)
    token = EmailVerificationToken.mint(locked)

    paths = {
        "login": reverse("login"),
        "signup": reverse("signup"),
        "setup": reverse("setup"),
        "verify-resend": reverse("verify-resend"),
        "verify-link": f"{VERIFY_PATH}{token.code}/",
        # 2G's two routes belong on this list for the same reason the
        # resend route does: the person who needs them cannot sign in.
        # A throwaway live code is used because the route takes one; the
        # property under test is "not bounced to sign-in", not "the code
        # was valid".
        "password-reset": reverse("password-reset"),
        "password-reset-confirm": f"{RESET_PATH}{'c' * 43}/",
    }
    # The list and the test must cover the same set, or the list can shrink
    # without anyone noticing.
    assert set(paths) == set(social_views.GATED_EXEMPT_URLS)

    for name in social_views.GATED_EXEMPT_URLS:
        anon = Client()
        resp = anon.get(paths[name], follow=False)
        location = resp.get("Location", "")
        assert resp.status_code in (200, 302), f"{name}: {resp.status_code}"
        assert "/login/" not in location, f"{name} bounced to sign-in"
        assert resp.status_code != 403, f"{name} refused outright"


@pytest.mark.django_db
def test_the_resend_post_is_reachable_without_a_session():
    # R122's whole premise: the person who needs this is not signed in, so
    # an authenticated-only resend could not rescue the case it exists for.
    unverified_member(localname="homeless", password=PASSWORD)
    anon = Client()
    with TestCase.captureOnCommitCallbacks(execute=True):
        resp = anon.post(RESEND_URL, {"email": "homeless@example.test"})
    assert resp.status_code == 302
    assert EmailVerificationToken.objects.filter(user__localname="homeless").exists()


@pytest.mark.django_db
def test_nothing_sends_inside_the_resend_request(client):
    # Same property as the creation routes: the request queues, the worker
    # talks to the mail server.
    unverified_member(localname="queued_only", password=PASSWORD)
    client.post(RESEND_URL, {"email": "queued_only@example.test"})
    assert mail.outbox == []
