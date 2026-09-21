"""Transport-security posture: proxy scheme trust + cookie flags (R74).

Half one (``reeltalk/proxy_trust.py``) decides who may tell us a request
arrived over https. Half two is what we then do about it: the session and CSRF
cookies carry ``Secure`` so they can never ride a plain-HTTP channel, and
``SameSite=Lax`` is pinned rather than left to the default.

The pairing matters on its own: ``SECURE_PROXY_SSL_HEADER`` is only set when
``TRUSTED_PROXIES`` is non-empty, so a deploy that forgets the trust list can
never end up believing a forwarded scheme from an arbitrary client.

The last section checks what the gate is actually worth downstream: a remote's
RFC 9421 ``@target-uri`` signature verifies only when the gate put the
terminator's scheme in place, because that is the only scheme we now read.
"""

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from reeltalk.activitypub import crypto, signatures
from reeltalk.proxy_trust import FORWARDED_PROTO_META, TrustedProxySchemeMiddleware

# The deployed shape: a TLS terminator whose address we know, plus the header
# Django is allowed to read *because* of that. Both derive from TRUSTED_PROXIES
# in settings.py, so tests override them together the way the env does.
DEPLOYED = override_settings(
    TRUSTED_PROXIES=["192.168.1.141/32"],
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
)

NO_PROXY = override_settings(TRUSTED_PROXIES=[], SECURE_PROXY_SSL_HEADER=None)


def pass_through(remote_addr, forwarded=None, *, trusted=("192.168.1.141/32",)):
    """Run one request through the middleware; return the request passed on.

    ``remote_addr=None`` means *genuinely absent*. RequestFactory fills
    ``REMOTE_ADDR`` with ``127.0.0.1`` whether or not you pass one, so
    without the explicit delete below a "missing address" case silently
    becomes a loopback case — and against a trust list that contains
    ``0.0.0.0/0``, loopback is trusted, so the header is kept.

    The scheme is read inside the override on purpose. ``request.scheme`` is
    a cached property, so a test that reads it after this block exits gets a
    value computed against the *ambient* ``SECURE_PROXY_SSL_HEADER`` — which
    the operator's ``.env`` sets. Pinning it here keeps every assertion in
    this file about the settings under test rather than the deploy config.
    """
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
        if remote_addr is None:
            del request.META["REMOTE_ADDR"]
        middleware(request)
        seen["scheme"] = request.scheme
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


# --- what the gate buys: a signed ``@target-uri`` that means something -----


SIGNED_TARGET = "https://reeltalk.example/user/alice/inbox/"
SIGNED_KEY_ID = "https://reeltalk.example/user/alice/#main-key"
SIGNED_BODY = b'{"type": "Follow"}'


def verify_signed_inbox_post(remote_addr, forwarded) -> bool:
    """Verify a post signed for the https target, delivered by ``remote_addr``.

    The signature is fixed; only the delivering peer and what it claims about
    the scheme vary — exactly the axis the gate is drawn on. This is the
    composition increment 2 depends on: ``@target-uri`` now reads
    ``request.scheme``, so it can only match what a remote signed if the gate
    put the terminator's forwarded scheme there.
    """
    private_pem, public_pem = crypto.generate_keypair()
    signed = signatures.sign_request(
        "POST", SIGNED_TARGET, private_pem, key_id=SIGNED_KEY_ID, body=SIGNED_BODY
    )
    request = RequestFactory().post(
        "/user/alice/inbox/",
        data=SIGNED_BODY,
        content_type="application/activity+json",
        REMOTE_ADDR=remote_addr,
    )
    request.META["HTTP_HOST"] = "reeltalk.example"
    if forwarded is not None:
        request.META[FORWARDED_PROTO_META] = forwarded
    for name, value in signed.items():
        request.META[f"HTTP_{name.upper().replace('-', '_')}"] = value
    TrustedProxySchemeMiddleware(lambda r: HttpResponse("ok"))(request)
    return signatures.verify_request(request, public_pem)


@DEPLOYED
def test_an_https_signed_post_verifies_behind_the_trusted_proxy():
    # The real cutover path: the terminator terminates TLS and reports https,
    # so the https target the sender signed is the URI we reconstruct.
    assert verify_signed_inbox_post("192.168.1.141", "https") is True


@DEPLOYED
def test_an_https_signed_post_fails_from_an_untrusted_peer():
    # Same bytes, different peer: the scheme stays http, the reconstructed
    # target no longer matches what was signed, and the activity is rejected
    # rather than accepted on a scheme the sender never delivered over.
    assert verify_signed_inbox_post("203.0.113.9", "https") is False


# --- cookie flags (settings level; per-request behaviour in
#     test_cookie_policy.py, since the Secure flag now follows the scheme) ---


def test_cookies_are_secure_by_default():
    assert settings.SECURE_COOKIES is True
    assert settings.COOKIES_FOLLOW_SCHEME is True
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.CSRF_COOKIE_SECURE is True
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"


def test_the_cookie_policy_runs_right_after_the_gate():
    # It keys off request.is_secure(), so it must never be reordered ahead of
    # the middleware that makes that value trustworthy.
    gate = settings.MIDDLEWARE.index(
        "reeltalk.proxy_trust.TrustedProxySchemeMiddleware"
    )
    cookies = settings.MIDDLEWARE.index(
        "reeltalk.cookie_policy.SchemeAwareCookieMiddleware"
    )
    assert cookies == gate + 1
