"""Transport-security posture: proxy scheme trust + cookie flags (R74).

Half one (``reeltalk/proxy_trust.py``) decides who may tell us a request
arrived over https. Half two is what we then do about it: the session and CSRF
cookies carry ``Secure`` so they can never ride a plain-HTTP channel, and
``SameSite=Lax`` is pinned rather than left to the default.

The pairing matters on its own: ``SECURE_PROXY_SSL_HEADER`` is only set when
``TRUSTED_PROXIES`` is non-empty, so a deploy that forgets the trust list can
never end up believing a forwarded scheme from an arbitrary client.
"""

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse

from reeltalk.proxy_trust import FORWARDED_PROTO_META, TrustedProxySchemeMiddleware

User = get_user_model()


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


# The deployed shape: a TLS terminator whose address we know, plus the header
# Django is allowed to read *because* of that. Both derive from TRUSTED_PROXIES
# in settings.py, so tests override them together the way the env does.
DEPLOYED = override_settings(
    TRUSTED_PROXIES=["192.168.1.141/32"],
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
)

NO_PROXY = override_settings(TRUSTED_PROXIES=[], SECURE_PROXY_SSL_HEADER=None)


def pass_through(remote_addr, forwarded=None, *, trusted=("192.168.1.141/32",)):
    """Run one request through the middleware; return the request passed on."""
    seen = {}

    def get_response(request):
        seen["request"] = request
        return HttpResponse("ok")

    with override_settings(
        TRUSTED_PROXIES=list(trusted),
        SECURE_PROXY_SSL_HEADER=(
            ("HTTP_X_FORWARDED_PROTO", "https") if trusted else None
        ),
    ):
        middleware = TrustedProxySchemeMiddleware(get_response)
        extra = {}
        if remote_addr is not None:
            extra["REMOTE_ADDR"] = remote_addr
        if forwarded is not None:
            extra[FORWARDED_PROTO_META] = forwarded
        request = RequestFactory().get("/", **extra)
        middleware(request)
    return seen["request"]


# --- who may assert the scheme ---------------------------------------------


@DEPLOYED
def test_forwarded_https_from_the_trusted_proxy_is_believed():
    request = pass_through("192.168.1.141", "https")
    assert request.scheme == "https"
    assert request.is_secure() is True


@DEPLOYED
def test_forwarded_proto_survives_for_a_trusted_proxy():
    # The header is what we vouched for; downstream code may still read it.
    request = pass_through("192.168.1.141", "https")
    assert request.META[FORWARDED_PROTO_META] == "https"


@DEPLOYED
def test_a_stranger_cannot_claim_https_with_the_same_header():
    # Same bytes, different peer: the real transport scheme wins.
    request = pass_through("203.0.113.9", "https")
    assert request.scheme == "http"
    assert request.is_secure() is False


@DEPLOYED
def test_the_stripped_header_is_gone_from_meta():
    request = pass_through("203.0.113.9", "https")
    assert FORWARDED_PROTO_META not in request.META


@DEPLOYED
def test_a_cidr_trusts_any_address_inside_it():
    request = pass_through("192.168.1.77", "https", trusted=("192.168.1.0/24",))
    assert request.scheme == "https"


@DEPLOYED
def test_an_address_outside_the_cidr_is_not_trusted():
    request = pass_through("192.168.2.141", "https", trusted=("192.168.1.0/24",))
    assert request.scheme == "http"


@DEPLOYED
def test_a_loopback_address_is_not_a_trusted_proxy():
    # 127.0.0.1 is the healthcheck's own address, not the terminator's; it is
    # only trusted if the operator explicitly lists it.
    request = pass_through("127.0.0.1", "https")
    assert request.scheme == "http"


@DEPLOYED
def test_an_ipv6_address_does_not_match_an_ipv4_trust_list():
    request = pass_through("::1", "https")
    assert request.scheme == "http"


def test_a_garbage_remote_addr_is_not_trusted_rather_than_fatal():
    assert pass_through("not-an-ip", "https", trusted=("0.0.0.0/0",)).scheme == "http"


def test_a_missing_remote_addr_is_not_trusted():
    assert pass_through(None, "https", trusted=("0.0.0.0/0",)).scheme == "http"


@NO_PROXY
def test_with_no_trusted_proxy_the_header_is_never_believed():
    # The fail-safe default: nothing forwarded is believed, and Django's own
    # header trust stays switched off.
    assert settings.SECURE_PROXY_SSL_HEADER is None
    request = pass_through("10.0.0.1", "https", trusted=())
    assert request.scheme == "http"


@DEPLOYED
def test_a_trusted_proxy_sending_plain_http_is_not_upgraded():
    request = pass_through("192.168.1.141", "http")
    assert request.scheme == "http"


@DEPLOYED
def test_a_trusted_proxy_sending_nothing_keeps_the_real_scheme():
    request = pass_through("192.168.1.141")
    assert request.scheme == "http"


def test_an_invalid_cidr_fails_loudly_at_startup():
    with pytest.raises(ImproperlyConfigured, match="TRUSTED_PROXIES"):
        pass_through("192.168.1.141", "https", trusted=("not-a-cidr",))


def test_the_gate_runs_before_anything_that_reads_the_scheme():
    # Being first in MIDDLEWARE is the whole mechanism: a later middleware that
    # reads request.scheme must already see the stripped header.
    assert settings.MIDDLEWARE[0] == (
        "reeltalk.proxy_trust.TrustedProxySchemeMiddleware"
    )


# --- cookie flags ---------------------------------------------------------


def test_cookies_are_secure_by_default():
    assert settings.SECURE_COOKIES is True
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.CSRF_COOKIE_SECURE is True
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"


@pytest.mark.django_db
def test_the_session_cookie_arrives_secure_httponly_and_lax(client, admin_user):
    client.post(reverse("login"), {"username": "admin", "password": "s3cretpass"})
    morsel = client.cookies[settings.SESSION_COOKIE_NAME]
    assert morsel["secure"] is True
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "Lax"


@pytest.mark.django_db
def test_the_csrf_cookie_arrives_secure(client):
    client.get(reverse("login"))
    morsel = client.cookies[settings.CSRF_COOKIE_NAME]
    assert morsel["secure"] is True
