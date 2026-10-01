"""Transport-security posture: proxy scheme trust + cookie flags (R74).

Half one (``reeltalk/proxy_trust.py``) decides who may tell us a request
arrived over https. Half two is what we then do about it: the session and CSRF
cookies carry ``Secure`` so they can never ride a plain-HTTP channel, and
``SameSite=Lax`` is pinned rather than left to the default.

The pairing matters on its own: ``SECURE_PROXY_SSL_HEADER`` is only set when
``TRUSTED_PROXIES`` is non-empty, so a deploy that forgets the trust list can
never end up believing a forwarded scheme from an arbitrary client.

The client-address half — ``client_ip()``, the one input every per-source
limit keys on — had **no tests at all** until this increment. Its tests
drive the real chain rather than a header: a client that may forge, behind a
proxy that reproduces nginx's ``$proxy_add_x_forwarded_for`` *append*
semantics. That modelling is deliberate. The live spoof round trip has never
been performed against a running instance, so this suite is what stands in
for it, and a hand-built two-entry header would not have — the bug *is* the
append, and a test that models a proxy which replaces arrives at a topology
that does not exist while the forgery it should have caught stays untested.

The last section checks what the gate is actually worth downstream: a remote's
RFC 9421 ``@target-uri`` signature verifies only when the gate put the
terminator's scheme in place, because that is the only scheme we now read.
"""

import logging
from io import StringIO

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from reeltalk.activitypub import crypto, signatures
from reeltalk.proxy_trust import (
    COLLAPSE_SAMPLE,
    FORWARDED_FOR_META,
    FORWARDED_PROTO_META,
    TrustedProxySchemeMiddleware,
    client_ip,
    forwarded_chain,
    is_trusted_proxy,
)
from reeltalk.social.models import EmailVerificationToken
from reeltalk.tests.members import unverified_member

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


# --- whose address a per-source limit keys on: client_ip() ---------------

NPM = "192.168.1.141"
PRIVATE = list(settings.PRIVATE_NETWORKS)


def proxied_request(peer, forwarded_for=None):
    """A request as the app actually receives it: a peer, plus its chain."""
    extra = {"REMOTE_ADDR": peer}
    if forwarded_for is not None:
        extra[FORWARDED_FOR_META] = forwarded_for
    return RequestFactory().get("/", **extra)


def nginx_append(client_header, real_client):
    """nginx's ``$proxy_add_x_forwarded_for``, reproduced rather than described.

    Every ``client_ip`` test below goes through here instead of writing an
    ``X-Forwarded-For`` literal, because the whole defect lives in this one
    piece of proxy behaviour: the directive **keeps whatever the client
    already sent** and appends ``$remote_addr`` on the right. A test that
    hand-built ``"1.2.3.4, 203.0.113.9"`` would be indistinguishable from
    one describing a proxy that *replaces* the header — and against that
    imaginary topology the forgery this suite exists to catch never arrives,
    so the test would pass while the bug stayed live.
    """
    if client_header:
        return f"{client_header}, {real_client}"
    return real_client


def resolve(client_header, real_client, peer, trusted):
    """Send one request down the real chain: client -> appending proxy -> app."""
    header = nginx_append(client_header, real_client)
    with override_settings(TRUSTED_PROXIES=list(trusted)):
        return client_ip(proxied_request(peer, header))


# -- the deployment table from the record, row by row ---------------------


def test_row_1_no_proxy_at_all_resolves_to_the_peer():
    # No proxy means no forwarded header in the first place, so this is
    # modelled without one rather than with an empty one.
    with override_settings(TRUSTED_PROXIES=[]):
        assert client_ip(proxied_request("8.8.7.6")) == "8.8.7.6"


def test_row_2_one_proxy_reporting_a_clean_client():
    assert resolve(None, "203.0.113.9", NPM, [f"{NPM}/32"]) == "203.0.113.9"


def test_row_3_a_forged_leftmost_entry_is_skipped_for_the_appended_one():
    # The headline case, and the exact chain the running NPM produces: the
    # client invents 1.2.3.4, the proxy appends the real address to its
    # right, and the right-to-left walk never reaches the invention. The
    # leftmost-entry rule returned "1.2.3.4" here, which is why
    # ``RESEND_IP_LIMIT`` was defeatable by rotating one header.
    resolved = resolve("1.2.3.4", "203.0.113.9", NPM, [f"{NPM}/32"])
    assert resolved == "203.0.113.9"
    assert resolved != "1.2.3.4"


def test_row_4_a_cdn_with_its_ranges_configured_is_walked_through():
    # Cloudflare's egress sits between the client and the app's own proxy,
    # so the CDN is the hop the proxy appended. With its range trusted the
    # walk steps over it and lands on the real client. The /12 is chosen to
    # actually contain 104.28.4.4 — a /13 there covers only .16 through .23
    # and would silently turn this row into row 5.
    trusted = ["10.0.0.5/32", "104.16.0.0/12"]
    assert resolve("203.0.113.9", "104.28.4.4", "10.0.0.5", trusted) == "203.0.113.9"


def test_row_5_a_cdn_without_its_ranges_resolves_to_the_cdn():
    # The ✗ row, pinned as a known limitation rather than smoothed over.
    # With no CDN range in the list the walk stops at the first untrusted
    # hop, and that hop is the CDN's own egress address — so every user on
    # the internet shares one bucket. Nothing in the settings distinguishes
    # this from a working deploy, which is precisely why the collapse
    # warning below has to be learned from traffic rather than at startup.
    assert (
        resolve("203.0.113.9", "104.28.4.4", "10.0.0.5", ["10.0.0.5/32"])
        == "104.28.4.4"
    )


# -- forgery, harder than one header ------------------------------------


def test_a_flood_of_forged_entries_still_resolves_to_the_appended_address():
    forged = ", ".join(f"10.9.{n}.{n}" for n in range(1, 12))
    assert resolve(forged, "203.0.113.9", NPM, [f"{NPM}/32"]) == "203.0.113.9"


def test_rotating_the_header_cannot_move_the_throttle_key():
    # The non-vacuity proof for R122's per-IP axis. What the throttle needs
    # is not that any single forgery fails but that a thousand of them all
    # land on the *same* key. Were the walk ever to regress to leftmost,
    # this set would gain forty members and the budget would reset on every
    # request — which is the bug this increment closes.
    keys = {resolve(f"1.2.3.{n}", "203.0.113.9", NPM, [f"{NPM}/32"]) for n in range(40)}
    assert keys == {"203.0.113.9"}


def test_garbage_a_client_injects_never_becomes_the_resolved_address():
    # Injected garbage always lands to the *left* of what the proxy appends,
    # so the walk stops at the real address before it can reach any of it.
    for junk in ["not-an-ip", "999.1.1.1", "203.0.113.9, oops", "1.2.3.4,,5.6.7.8"]:
        assert resolve(junk, "203.0.113.9", NPM, [f"{NPM}/32"]) == "203.0.113.9"


def test_an_untrusted_peer_cannot_use_a_forwarded_chain_at_all():
    # Same header bytes, untrusted deliverer: the chain is not read, so the
    # peer stands. The identical rule that makes the scheme header get
    # stripped rather than believed — one trust answer, two questions.
    assert resolve("8.8.8.8", "8.8.8.8", "203.0.113.99", [f"{NPM}/32"]) == (
        "203.0.113.99"
    )


# -- chain hygiene ------------------------------------------------------


def test_blank_entries_are_dropped_from_the_chain():
    with override_settings(TRUSTED_PROXIES=[f"{NPM}/32"]):
        request = proxied_request(NPM, "  , 203.0.113.9, ")
        assert forwarded_chain(request) == ["203.0.113.9"]
        assert client_ip(request) == "203.0.113.9"


def test_a_malformed_rightmost_entry_falls_back_to_the_peer_not_the_garbage():
    # Only a broken proxy can put garbage at the right end of the chain,
    # and what it must not be allowed to produce is a throttle key of the
    # caller's choosing. Falling back to the peer puts that deploy in one
    # shared bucket; returning the garbage would hand every such request a
    # fresh, attacker-selected key, which is a bypass wearing a
    # misconfiguration's clothes.
    with override_settings(TRUSTED_PROXIES=[f"{NPM}/32"]):
        resolved = client_ip(proxied_request(NPM, "203.0.113.9, not-an-ip"))
    assert resolved == NPM
    assert resolved != "not-an-ip"


def test_a_chain_of_only_trusted_hops_resolves_to_the_peer():
    assert resolve("10.0.0.9", NPM, NPM, PRIVATE) == NPM


def test_a_lan_client_behind_a_proxy_resolves_to_the_proxy():
    # The cost of the private-range default, put in a test rather than
    # buried in prose. The walk cannot tell "private because it is a proxy"
    # from "private because it is the laptop next door", so a LAN client
    # behind a proxy shares the proxy's bucket instead of getting its own.
    # ``mod_remoteip`` behaves identically, and for a floor under
    # internet-originated abuse that is the right side of the trade — but it
    # is a trade, and this is where it is recorded.
    assert resolve(None, "192.168.1.50", NPM, PRIVATE) == NPM


# -- the shipped default ------------------------------------------------


def test_the_shipped_default_is_the_private_ranges():
    assert settings.PRIVATE_NETWORKS == [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
        "127.0.0.0/8",
        "::1/128",
    ]


@pytest.mark.parametrize(
    "addr",
    ["10.1.2.3", "172.20.0.2", "192.168.1.141", "127.0.0.1", "fc00::1", "fd12::34"],
)
def test_the_default_trusts_every_private_range(addr):
    with override_settings(TRUSTED_PROXIES=PRIVATE):
        assert is_trusted_proxy(addr) is True


@pytest.mark.parametrize(
    "addr",
    [
        "8.8.8.8",
        "203.0.113.9",
        "104.28.4.4",
        "11.0.0.1",
        "172.32.0.1",
        "192.169.0.1",
        "2001:4860::8888",
    ],
)
def test_the_default_trusts_no_public_address(addr):
    # The whole safety argument for shipping this default rests on the
    # boundary holding: an address a public client can genuinely present is
    # never inside the trust list, so nobody off-network can be walked
    # through it. The near-misses (11/8, 172.32/12, 192.169/16) are here
    # because a sloppy range in the default would otherwise pass unnoticed.
    with override_settings(TRUSTED_PROXIES=PRIVATE):
        assert is_trusted_proxy(addr) is False


def test_a_compose_deploy_needs_no_configuration_at_all():
    # The reason the default exists: reverse proxy on the compose network,
    # real client on the internet, nothing set by hand.
    assert resolve(None, "203.0.113.9", "172.18.0.2", PRIVATE) == "203.0.113.9"


# -- the collapse diagnostic --------------------------------------------


def run_middleware(cases, caplog, trusted=(f"{NPM}/32",), sample=4):
    """Push each ``(peer, xff)`` case through the middleware; return it.

    ``sample`` is set after construction so a test can judge on four
    requests rather than the production fifty; the wiring to the real
    default is asserted on its own in
    :func:`test_the_middleware_starts_at_the_production_sample_size`.
    """
    caplog.set_level(logging.WARNING, logger="reeltalk.proxy_trust")
    with override_settings(TRUSTED_PROXIES=list(trusted)):
        middleware = TrustedProxySchemeMiddleware(lambda r: HttpResponse("ok"))
        middleware.sample_size = sample
        for peer, forwarded_for in cases:
            middleware(proxied_request(peer, forwarded_for))
    return middleware


def collapse_messages(caplog):
    return [r.getMessage() for r in caplog.records if "ONE SOURCE" in r.getMessage()]


def test_the_warning_fires_when_every_proxied_request_looks_like_one_source(caplog):
    run_middleware([(NPM, "203.0.113.9")] * 4, caplog)
    (message,) = collapse_messages(caplog)
    assert "203.0.113.9" in message
    assert "check_client_ip" in message


def test_the_warning_stays_quiet_once_addresses_vary(caplog):
    run_middleware([(NPM, f"203.0.113.{n}") for n in range(1, 5)], caplog)
    assert collapse_messages(caplog) == []


def test_the_warning_does_not_fire_below_the_sample_size(caplog):
    run_middleware([(NPM, "203.0.113.9")] * 3, caplog, sample=4)
    assert collapse_messages(caplog) == []


def test_the_warning_fires_only_once_per_process(caplog):
    run_middleware([(NPM, "203.0.113.9")] * 12, caplog)
    assert len(collapse_messages(caplog)) == 1


def test_the_warning_blames_the_proxy_when_no_client_address_arrives(caplog):
    # No X-Forwarded-For at all: the resolved address *is* the peer, which
    # is a different fault from a CDN and needs different words, because the
    # remedy is to make the proxy forward the header rather than to widen a
    # trust list.
    run_middleware([(NPM, None)] * 4, caplog)
    (message,) = collapse_messages(caplog)
    assert "no client address is reaching us through the proxy" in message
    assert "CDN sits in front" not in message


def test_the_warning_blames_a_cdn_when_the_address_is_upstream(caplog):
    run_middleware([(NPM, "104.28.4.4")] * 4, caplog)
    (message,) = collapse_messages(caplog)
    assert "CDN sits in front" in message
    assert "no client address is reaching us" not in message


def test_a_direct_deploy_is_never_sampled(caplog):
    # Untrusted peer means the peer genuinely is the client, so one address
    # across the whole sample reads as one user rather than a broken chain.
    # Warning there would teach deployers to ignore the only warning that
    # matters.
    run_middleware([("8.8.7.6", None)] * 8, caplog, trusted=[])
    assert collapse_messages(caplog) == []


def test_the_middleware_starts_at_the_production_sample_size():
    with override_settings(TRUSTED_PROXIES=[f"{NPM}/32"]):
        middleware = TrustedProxySchemeMiddleware(lambda r: HttpResponse("ok"))
    assert middleware.sample_size == COLLAPSE_SAMPLE


# -- manage.py check_client_ip ------------------------------------------


def command_output(*args):
    out = StringIO()
    call_command("check_client_ip", *args, stdout=out, stderr=out)
    return out.getvalue()


def resolved_line(text):
    for line in text.splitlines():
        if line.strip().startswith("resolved client IP"):
            return line.split()[-1]
    raise AssertionError(f"no resolved-client-IP line in:\n{text}")


@DEPLOYED
def test_check_client_ip_reports_the_effective_config(db):
    text = command_output()
    assert "192.168.1.141/32" in text
    assert "set by the operator" in text
    assert "HTTP_X_FORWARDED_PROTO" in text


@DEPLOYED
def test_check_client_ip_walks_a_chain_the_operator_pasted(db):
    text = command_output("--peer", NPM, "--xff", "1.2.3.4, 203.0.113.9")
    assert "THIS IS THE CLIENT" in text
    assert resolved_line(text) == "203.0.113.9"


@DEPLOYED
def test_check_client_ip_explains_an_untrusted_peer_without_walking(db):
    text = command_output("--peer", "203.0.113.99", "--xff", "8.8.8.8")
    assert "not a trusted proxy" in text
    assert "the header is not read at all" in text
    assert resolved_line(text) == "203.0.113.99"


@DEPLOYED
def test_check_client_ip_refuses_a_chain_with_no_peer(db):
    # A chain with no peer is not a chain, so the command refuses rather
    # than printing a resolution the operator might act on.
    with pytest.raises(CommandError, match="--xff needs --peer"):
        command_output("--xff", "203.0.113.9")


@DEPLOYED
def test_check_client_ip_flags_every_recent_send_resolving_to_one_address(db):
    member = unverified_member("collapse")
    for _ in range(3):
        EmailVerificationToken.mint(member, request_ip=NPM)
    text = command_output()
    assert "Every counted send resolved to one address" in text


@DEPLOYED
def test_check_client_ip_reports_distinct_addresses_as_healthy(db):
    member = unverified_member("spread")
    EmailVerificationToken.mint(member, request_ip="203.0.113.9")
    EmailVerificationToken.mint(member, request_ip="198.51.100.7")
    text = command_output()
    assert "Distinct addresses are being resolved" in text


@override_settings(TRUSTED_PROXIES=[], SECURE_PROXY_SSL_HEADER=None)
def test_check_client_ip_calls_an_empty_trust_list_what_it_is(db):
    # Empty is a supported posture rather than a hole, and the report has to
    # say so in the same words the docs use — a deployer reading "(empty)"
    # plus "no proxy trusted" should not have to go look up whether they
    # broke something.
    text = command_output()
    assert "(empty)" in text
    assert "No proxy trusted" in text


@DEPLOYED
def test_check_client_ip_reports_a_trusted_peer_with_nothing_to_walk(db):
    text = command_output("--peer", NPM)
    assert "nothing to walk" in text
    assert resolved_line(text) == NPM


@DEPLOYED
def test_check_client_ip_names_a_trusted_hop_it_skipped(db):
    with override_settings(TRUSTED_PROXIES=[f"{NPM}/32", "192.168.0.0/16"]):
        text = command_output("--peer", NPM, "--xff", "203.0.113.9, 192.168.1.50")
    assert "skipped, the client is further left" in text
    assert resolved_line(text) == "203.0.113.9"


@DEPLOYED
def test_check_client_ip_names_an_unparseable_hop_that_stopped_the_walk(db):
    text = command_output("--peer", NPM, "--xff", "203.0.113.9, not-an-ip")
    assert "unparseable" in text
    assert resolved_line(text) == NPM


@DEPLOYED
def test_check_client_ip_says_so_when_no_send_has_a_source(db):
    # A mint from a management command or a fixture has no source to name,
    # and the report must distinguish that from "one address for everyone"
    # -- the two look alike in a count of one and mean opposite things.
    member = unverified_member("sourceless")
    EmailVerificationToken.mint(member)
    text = command_output()
    assert "no source recorded" in text


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
