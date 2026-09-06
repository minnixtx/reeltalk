"""Auth view tests: signup, login, logout, first-run setup wizard (R12)."""

import pytest
from django.contrib.auth import SESSION_KEY, get_user_model
from django.urls import reverse

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
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


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
def test_setup_post_creates_admin_and_logs_in(client):
    response = client.post(reverse("setup"), VALID_SIGNUP)
    assert response.status_code == 302
    admin = User.objects.get(localname="alice")
    assert admin.is_superuser is True
    assert admin.is_staff is True
    # The session now holds the new admin (SESSION_KEY is the stable public
    # name for Django's private "_auth_user_id" session key).
    assert client.session[SESSION_KEY] == str(admin.id)


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
def test_signup_creates_local_user_and_logs_in(client, admin_user):
    response = client.post(reverse("signup"), VALID_SIGNUP)
    assert response.status_code == 302
    user = User.objects.get(localname="alice")
    assert user.local is True
    assert user.is_superuser is False
    assert client.session[SESSION_KEY] == str(user.id)


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
