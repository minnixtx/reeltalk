"""The cookie Secure flag follows the transport (R74).

The whole point is that one deployment answers both https://reeltalk.minnix.dev
and a plain http://<lan-ip>:3030, and only the first of those can carry a
Secure cookie. So the flag is decided per request, off the scheme the trusted
proxy gate left behind -- and the strict, unconditional mode stays a config
flip rather than a code change.
"""

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

User = get_user_model()

TRUSTED_PROXY = override_settings(
    TRUSTED_PROXIES=["192.168.1.141/32"],
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
)


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


def wire_value(response, name):
    """The Set-Cookie line as it would actually go over the wire."""
    return response.cookies[name].output(header="Set-Cookie:").strip()


def login(client, **kwargs):
    return client.post(
        reverse("login"),
        {"username": "admin", "password": "s3cretpass"},
        **kwargs,
    )


# --- plain HTTP: no Secure, so the LAN endpoint can hold a session --------


@pytest.mark.django_db
def test_session_cookie_over_plain_http_is_not_secure(client, admin_user):
    response = login(client)
    assert response.cookies[settings.SESSION_COOKIE_NAME].value
    assert "Secure" not in wire_value(response, settings.SESSION_COOKIE_NAME)


@pytest.mark.django_db
def test_plain_http_cookie_keeps_httponly_and_lax(client, admin_user):
    # Dropping Secure must not quietly drop the other two protections.
    line = wire_value(login(client), settings.SESSION_COOKIE_NAME)
    assert "HttpOnly" in line
    assert "SameSite=Lax" in line


@pytest.mark.django_db
def test_csrf_cookie_over_plain_http_is_not_secure(client):
    response = client.get(reverse("login"))
    assert "Secure" not in wire_value(response, settings.CSRF_COOKIE_NAME)


# --- HTTPS: Secure comes back ---------------------------------------------


@pytest.mark.django_db
def test_session_cookie_over_https_is_secure(client, admin_user):
    line = wire_value(login(client, secure=True), settings.SESSION_COOKIE_NAME)
    assert "Secure" in line
    assert "HttpOnly" in line
    assert "SameSite=Lax" in line


@pytest.mark.django_db
def test_csrf_cookie_over_https_is_secure(client):
    response = client.get(reverse("login"), secure=True)
    assert "Secure" in wire_value(response, settings.CSRF_COOKIE_NAME)


@TRUSTED_PROXY
@pytest.mark.django_db
def test_a_trusted_proxy_reporting_https_restores_secure(client, admin_user):
    # Cleartext transport, but the operator's terminator says the browser's
    # side was https -- so the cookie is marked Secure for the browser's sake.
    line = wire_value(
        login(client, HTTP_X_FORWARDED_PROTO="https", REMOTE_ADDR="192.168.1.141"),
        settings.SESSION_COOKIE_NAME,
    )
    assert "Secure" in line


@TRUSTED_PROXY
@pytest.mark.django_db
def test_a_spoofed_https_from_a_stranger_does_not_mark_the_cookie_secure(
    client, admin_user
):
    # The gate strips the header, so the policy sees plain http and leaves the
    # cookie un-secured. Composition of the two middlewares, proven end to end.
    line = wire_value(
        login(client, HTTP_X_FORWARDED_PROTO="https", REMOTE_ADDR="203.0.113.9"),
        settings.SESSION_COOKIE_NAME,
    )
    assert "Secure" not in line


# --- the strict mode stays reachable by config ----------------------------


@override_settings(COOKIES_FOLLOW_SCHEME=False)
@pytest.mark.django_db
def test_strict_mode_marks_secure_even_over_plain_http(client, admin_user):
    # This is the posture that makes LAN-HTTP login impossible; kept available
    # so reverting to it is an env change, not a code change.
    line = wire_value(login(client), settings.SESSION_COOKIE_NAME)
    assert "Secure" in line


@override_settings(SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
@pytest.mark.django_db
def test_https_raises_secure_even_when_the_static_setting_is_off(client, admin_user):
    # The per-scheme rule can raise the flag, not only lower it: a request
    # that really arrived over https gets a Secure cookie whatever the static
    # session setting says, so a plain-HTTP-friendly config cannot leak a
    # session over an encrypted-then-downgraded path.
    line = wire_value(login(client, secure=True), settings.SESSION_COOKIE_NAME)
    assert "Secure" in line


@override_settings(SECURE_COOKIES=False, SESSION_COOKIE_SECURE=False)
@pytest.mark.django_db
def test_turning_securing_off_leaves_https_unsecured(client, admin_user):
    # With SECURE_COOKIES off the middleware is inert -- it never adds Secure.
    line = wire_value(login(client, secure=True), settings.SESSION_COOKIE_NAME)
    assert "Secure" not in line
