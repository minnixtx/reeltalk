"""Auth view tests: signup, login, logout, first-run setup wizard (R12)."""

import pytest
from django.contrib.auth import SESSION_KEY, get_user_model
from django.urls import reverse

from reeltalk.social.models import EmailVerificationToken
from reeltalk.tests.members import site_admin

User = get_user_model()

VALID_SIGNUP = {
    "localname": "alice",
    "display_name": "Alice",
    "email": "alice@example.com",
    "password1": "correct-horse-battery-9",
    "password2": "correct-horse-battery-9",
}


@pytest.fixture
def admin_user(db):
    return site_admin(localname="admin", password="s3cretpass")


# --- first-run setup wizard -------------------------------------------------


@pytest.mark.django_db
def test_index_redirects_to_setup_on_fresh_instance(client):
    response = client.get(reverse("index"))
    assert response.status_code == 302
    assert response.url == reverse("setup")


@pytest.mark.django_db
def test_index_serves_home_once_admin_exists(client, admin_user):
    response = client.get(reverse("index"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_setup_get_renders_form_on_fresh_instance(client):
    response = client.get(reverse("setup"))
    assert response.status_code == 200
    assert "set up your instance" in response.content.decode().lower()


@pytest.mark.django_db
def test_setup_post_creates_admin_but_does_not_log_it_in(client):
    # R119: the wizard no longer logs the new admin in, and this is the
    # sharpest edge of the whole increment rather than a detail of it. The
    # admin is created, ``has_admin()`` is immediately true so the wizard is
    # gone, and the only way into /admin/ is the link in the mail. Which is
    # why EMAIL_* has to be configured *before* the wizard runs.
    response = client.post(reverse("setup"), VALID_SIGNUP)
    assert response.status_code == 302
    admin = User.objects.get(localname="alice")
    assert admin.is_superuser is True
    assert admin.is_staff is True
    # The session holds nobody. Asserting the absence is the point: a test
    # that only checked the redirect would pass against a view that logged the
    # admin in and redirected anyway.
    assert SESSION_KEY not in client.session
    assert response.url == reverse("verify-resend")
    # And a real token was minted against the address, so the admin is not
    # stranded — the mail is the way back in.
    token = EmailVerificationToken.objects.get(user=admin)
    assert token.email == "alice@example.com"
    assert token.is_live


@pytest.mark.django_db
def test_setup_redirects_away_once_admin_exists(client, admin_user):
    response = client.get(reverse("setup"))
    assert response.status_code == 302
    assert response.url == reverse("index")


# --- signup -----------------------------------------------------------------


@pytest.mark.django_db
def test_signup_blocked_before_setup(client):
    # Fresh instance: the only account path is the setup wizard (R12).
    response = client.get(reverse("signup"))
    assert response.status_code == 302
    assert response.url == reverse("setup")


@pytest.mark.django_db
def test_signup_creates_local_user_but_does_not_log_it_in(client, admin_user):
    # R119: signup stops logging you in. The account exists and is fully
    # created; what it does not have is proof it can receive mail, which is
    # now the price of a session.
    response = client.post(reverse("signup"), VALID_SIGNUP)
    assert response.status_code == 302
    # New members land on the resend page, not the feed and not /welcome/ —
    # the first thing they see is the thing they need if the mail is lost.
    assert response.url == reverse("verify-resend")
    user = User.objects.get(localname="alice")
    assert user.local is True
    assert user.is_superuser is False
    assert SESSION_KEY not in client.session
    assert user.email_verified is False
    assert EmailVerificationToken.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_signup_requires_an_email_address(client, admin_user):
    # R118, and the reason it is not optional any more: an account with no
    # address can never be verified, so an optional field here would be
    # manufacturing members who can never sign in.
    response = client.post(reverse("signup"), {**VALID_SIGNUP, "email": ""})
    assert response.status_code == 200
    assert "email" in response.context["form"].errors
    assert not User.objects.filter(localname="alice").exists()


@pytest.mark.django_db
def test_signup_rejects_an_address_another_account_already_has(client, admin_user):
    # Told at the form rather than as a database error. The partial unique
    # index is still the real guard; this is the human-facing half.
    User.objects.create_user(
        localname="existing",
        email="alice@example.com",
        password="s3cretpass",
    )
    response = client.post(reverse("signup"), VALID_SIGNUP)
    assert response.status_code == 200
    assert "email" in response.context["form"].errors
    assert not User.objects.filter(localname="alice").exists()


@pytest.mark.django_db
def test_signup_rejects_password_mismatch(client, admin_user):
    data = {**VALID_SIGNUP, "password2": "different-password-1"}
    response = client.post(reverse("signup"), data)
    assert response.status_code == 200
    assert "password2" in response.context["form"].errors
    assert not User.objects.filter(localname="alice").exists()


@pytest.mark.django_db
def test_signup_rejects_weak_password(client, admin_user):
    data = {**VALID_SIGNUP, "password1": "abc", "password2": "abc"}
    response = client.post(reverse("signup"), data)
    assert response.status_code == 200
    assert "password1" in response.context["form"].errors


@pytest.mark.django_db
def test_signup_rejects_taken_localname_case_insensitively(client, admin_user):
    data = {**VALID_SIGNUP, "localname": "Admin"}
    response = client.post(reverse("signup"), data)
    assert response.status_code == 200
    assert "localname" in response.context["form"].errors


@pytest.mark.parametrize("localname", ["bad name!", "-leading", ".dotfirst"])
@pytest.mark.django_db
def test_signup_rejects_bad_localname(client, admin_user, localname):
    data = {**VALID_SIGNUP, "localname": localname}
    response = client.post(reverse("signup"), data)
    assert response.status_code == 200
    assert "localname" in response.context["form"].errors


# --- login / logout ---------------------------------------------------------


@pytest.mark.django_db
def test_login_get_renders_form(client):
    response = client.get(reverse("login"))
    assert response.status_code == 200


# Django's AuthenticationForm always names its field "username" (label taken
# from USERNAME_FIELD) and maps it onto localname internally.


@pytest.mark.django_db
def test_login_with_valid_credentials(client, admin_user):
    response = client.post(
        reverse("login"), {"username": "admin", "password": "s3cretpass"}
    )
    assert response.status_code == 302
    assert client.session[SESSION_KEY] == str(admin_user.id)


@pytest.mark.django_db
def test_login_rejects_invalid_credentials(client, admin_user):
    response = client.post(reverse("login"), {"username": "admin", "password": "wrong"})
    assert response.status_code == 200
    assert SESSION_KEY not in client.session


@pytest.mark.django_db
def test_logout_post_ends_session(client, admin_user):
    client.force_login(admin_user)
    response = client.post(reverse("logout"))
    assert response.status_code == 302
    assert SESSION_KEY not in client.session


@pytest.mark.django_db
def test_logout_get_rejected(client, admin_user):
    # Django >=5: logout is POST-only.
    client.force_login(admin_user)
    response = client.get(reverse("logout"))
    assert response.status_code == 405


# --- custom user integrates with admin auth ---------------------------------


@pytest.mark.django_db
def test_admin_site_accepts_custom_user(client, admin_user):
    client.force_login(admin_user)
    response = client.get("/admin/")
    assert response.status_code in (200, 302)
