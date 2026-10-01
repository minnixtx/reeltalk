"""Believe a forwarded TLS scheme only from the proxy we actually sit behind.

Verified on Django 6.1.1: there is no ``SECURE_TRUSTED_PROXIES`` setting, and
``SECURE_PROXY_SSL_HEADER`` trusts ``X-Forwarded-Proto`` from *any* client that
bothers to send it. Set on its own it lets a remote caller make
``request.scheme`` / ``is_secure()`` claim https over a plain-HTTP connection,
which is exactly the header a TLS terminator is supposed to be the only source
of (D14: TLS termination is the operator's, so the app must know which peer
that is).

This middleware is first in ``MIDDLEWARE`` and drops the header unless
``REMOTE_ADDR`` is inside one of ``settings.TRUSTED_PROXIES``. What survives to
``SECURE_PROXY_SSL_HEADER`` can therefore only have come from the operator's
terminator; everyone else keeps the real transport scheme.
"""

from ipaddress import ip_address, ip_network

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

FORWARDED_PROTO_META = "HTTP_X_FORWARDED_PROTO"
FORWARDED_FOR_META = "HTTP_X_FORWARDED_FOR"


def is_trusted_proxy(remote_addr) -> bool:
    """True when ``remote_addr`` falls inside one of ``settings.TRUSTED_PROXIES``.

    Module-level rather than middleware-only because two different questions
    need the same answer: the middleware asks whether it may believe a
    forwarded header, and :func:`client_ip` asks whose address a
    per-source limit should key on. One parse, one trust rule, nothing able to
    drift between them.

    A garbage or missing address is **not** trusted rather than fatal — the
    same posture the middleware has always had, where an unreadable peer simply
    gets no benefit of the doubt.
    """
    if not remote_addr:
        return False
    try:
        addr = ip_address(remote_addr)
    except ValueError:
        return False
    return any(addr in net for net in _trusted_networks())


def client_ip(request) -> str:
    """The address to key a per-source limit on.

    ``REMOTE_ADDR`` alone is the wrong answer on a deploy that actually sits
    behind a terminator, which this one does. There the peer of every request
    is the proxy, so a limit keyed on it counts **all** real users as one
    source: the first 25 people through the tunnel would throttle the 26th.
    That is worse than no limit, because it reads as protection while
    punishing exactly the wrong party.

    So: believe ``X-Forwarded-For``'s leftmost entry **only** when
    ``REMOTE_ADDR`` is a trusted proxy, and fall back to ``REMOTE_ADDR``
    otherwise. The trust check is what makes that safe — an untrusted peer's
    ``X-Forwarded-For`` is a string it made up, and is ignored.

    **The limit of this, stated rather than hidden.** With one trusted
    terminator the leftmost entry is the client as that terminator reported it,
    which is the best available. Behind a chain of proxies an attacker who can
    set the header at the front can still shape it. This is a floor under
    one-source abuse, not an identity — R107's rule that dedup is not rate
    limiting applies with equal force to forwarded headers.
    """
    peer = request.META.get("REMOTE_ADDR") or ""
    if not is_trusted_proxy(peer):
        return peer
    forwarded = request.META.get(FORWARDED_FOR_META, "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        try:
            ip_address(first)
        except ValueError:
            # A terminator that sends something unparseable is misconfigured;
            # falling back to the real peer is honest and never worse.
            return peer
        return first
    return peer


class TrustedProxySchemeMiddleware:
    """Strip ``X-Forwarded-Proto`` from requests not sent by a trusted proxy."""

    def __init__(self, get_response):
        self.get_response = get_response
        # Warm the shared memo at startup so an invalid CIDR still fails loudly
        # here rather than on the first request that happens to ask.
        _trusted_networks()

    def __call__(self, request):
        if not is_trusted_proxy(request.META.get("REMOTE_ADDR")):
            request.META.pop(FORWARDED_PROTO_META, None)
        return self.get_response(request)

    def is_trusted(self, remote_addr) -> bool:
        """Kept as the middleware's own entry point; the rule lives in
        :func:`is_trusted_proxy`."""
        return is_trusted_proxy(remote_addr)


_NETWORK_MEMO: tuple[tuple[str, ...], list] = ((), [])


def _trusted_networks():
    """Parse ``TRUSTED_PROXIES`` once per distinct setting value.

    Keyed on the value rather than cached forever so an ``override_settings``
    in a test is picked up immediately — the same reason the middleware
    memoised this way before the rule moved here.
    """
    global _NETWORK_MEMO
    specs = tuple(settings.TRUSTED_PROXIES)
    if specs == _NETWORK_MEMO[0]:
        return _NETWORK_MEMO[1]
    try:
        networks = [ip_network(spec.strip()) for spec in specs]
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"TRUSTED_PROXIES contains an invalid CIDR: {exc}"
        ) from exc
    _NETWORK_MEMO = (specs, networks)
    return networks
