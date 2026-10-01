"""Decide which forwarded headers to believe, and whose address a limit keys on.

Two questions share one trust rule here, and the sharing is the point: the
middleware asks whether a peer may tell us a request arrived over https, and
:func:`client_ip` asks whose address a per-source limit should count. One
parse of ``settings.TRUSTED_PROXIES``, one answer, nothing able to drift
between them.

**The scheme half.** Verified on Django 6.1.1: there is no
``SECURE_TRUSTED_PROXIES`` setting, and ``SECURE_PROXY_SSL_HEADER`` trusts
``X-Forwarded-Proto`` from *any* client that bothers to send it. Set on its
own it lets a remote caller make ``request.scheme`` / ``is_secure()`` claim
https over a plain-HTTP connection, which is exactly the header a TLS
terminator is supposed to be the only source of (D14: TLS termination is
the operator's, so the app must know which peer that is). This middleware is
first in ``MIDDLEWARE`` and drops the header unless ``REMOTE_ADDR`` is inside
one of ``TRUSTED_PROXIES``. What survives to ``SECURE_PROXY_SSL_HEADER`` can
therefore only have come from the operator's terminator; everyone else keeps
the real transport scheme.

**The client-address half.** ``REMOTE_ADDR`` alone is the wrong answer on a
deploy that actually sits behind a terminator, because there the peer of
every request is the proxy and a limit keyed on it counts **all** real users
as one source: the first 25 people through the tunnel would throttle the
26th. So we walk ``X-Forwarded-For`` **right-to-left**, skipping hops that
are themselves trusted proxies, and return the first address that is not.
That is ``mod_remoteip``'s algorithm, and the reason it replaced the
leftmost-entry rule is that every real proxy *appends* rather than replaces —
nginx's ``$proxy_add_x_forwarded_for`` keeps whatever the client already
sent and adds ``$remote_addr`` on the right — so the leftmost entry is the
one entry in the chain a stranger definitely controls.

**What the private-range default buys, and what it costs.**
``TRUSTED_PROXIES`` defaults to the private/unique-local/loopback ranges so
the common ``docker compose up`` with a reverse proxy on the same network
needs no configuration at all. The safety argument is that a
public-internet client cannot *present* a private source address — return
routing fails and the TCP handshake never completes — so trusting those
ranges only ever trusts machines genuinely on the local network. The cost is
symmetric and worth stating plainly: the walk cannot tell "private because it
is a proxy" from "private because it is a laptop next door", so a LAN client
behind a proxy resolves to the **proxy**, not to itself. ``mod_remoteip``
behaves identically. For an abuse-rate floor that is the right trade; this
is not an identity system, and R107's rule that dedup is not rate limiting
applies with equal force to forwarded headers.
"""

import logging
from ipaddress import ip_address, ip_network

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

FORWARDED_PROTO_META = "HTTP_X_FORWARDED_PROTO"
FORWARDED_FOR_META = "HTTP_X_FORWARDED_FOR"

#: How many proxied requests to watch before judging whether every user is
#: collapsing onto one address. Low enough to surface a misconfiguration
#: seconds after boot, high enough that a couple of page loads (which produce
#: several distinct assets) is not mistaken for a whole user base.
COLLAPSE_SAMPLE = 50


def _parse(value):
    """``value`` as an address, or ``None``. Never raises."""
    if not value:
        return None
    try:
        return ip_address(value)
    except ValueError:
        return None


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
    addr = _parse(remote_addr)
    if addr is None:
        return False
    return any(addr in net for net in _trusted_networks())


def forwarded_chain(request) -> list[str]:
    """The ``X-Forwarded-For`` entries, left to right, as the chain wrote them.

    Blank entries are dropped rather than carried through: a proxy that emits
    ``"203.0.113.9, "` `` (trailing comma, empty hop) is a formatting
    artifact, not a hop worth a place in the walk or in a report.
    """
    raw = request.META.get(FORWARDED_FOR_META, "")
    if not raw:
        return []
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def resolve_chain(peer: str, chain) -> tuple[str, list[tuple[str, str]]]:
    """Resolve the client address from a peer plus a forwarded chain.

    Returns ``(resolved, trace)``. ``trace`` is the walk itself, right to
    left, as ``(entry, verdict)`` with verdict one of ``"trusted"`` (skipped,
    it is one of ours), ``"client"`` (returned) or ``"malformed"`` (the walk
    stopped and fell back to the peer). :func:`client_ip` wants only the
    first half; ``manage.py check_client_ip`` prints the second, because the
    thing a deployer needs is not the answer but the *reason* for it.

    The walk, in order:

    * An **untrusted peer** ends it before it starts. Nobody downstream of us
      can vouch for what it claims about addresses further up the chain, so
      the chain is not read at all — the same rule that makes the scheme
      header get stripped rather than believed.
    * A **trusted** entry is skipped: it is a proxy we know, and the real
      client is further left.
    * The first **untrusted, parseable** entry is the client.
    * A **malformed** entry stops the walk and returns the peer. This is the
      one case worth justifying rather than glossing: an unparseable entry is
      not trusted, so the naive rule would *return* it — and since the
      string came from a header, the caller would hold the throttle key.
      Falling back to the peer puts the caller in the shared bucket instead
      of handing it a fresh one, which is the difference between a
      misconfiguration being harmless and a bypass being one header wide.
    * A chain in which **every** hop is trusted means the request originated
      inside our own network, so the peer is the honest answer.
    """
    if not is_trusted_proxy(peer):
        return peer, []
    trace: list[tuple[str, str]] = []
    for entry in reversed(list(chain)):
        if _parse(entry) is None:
            trace.append((entry, "malformed"))
            return peer, trace
        if is_trusted_proxy(entry):
            trace.append((entry, "trusted"))
            continue
        trace.append((entry, "client"))
        return entry, trace
    return peer, trace


def client_ip(request) -> str:
    """The address to key a per-source limit on.

    ``REMOTE_ADDR`` alone is the wrong answer on a deploy that actually sits
    behind a terminator, which this one does. There the peer of every request
    is the proxy, so a limit keyed on it counts **all** real users as one
    source: the first 25 people through the tunnel would throttle the 26th.
    That is worse than no limit, because it reads as protection while
    punishing exactly the wrong party.

    So: walk ``X-Forwarded-For`` right-to-left over the hops we trust and
    return the first one we do not, falling back to ``REMOTE_ADDR`` when the
    peer is untrusted or the chain says nothing usable. See
    :func:`resolve_chain` for the walk and this module's docstring for what
    the trust default costs.

    **The limit of this, stated rather than hidden.** An attacker who can set
    the header *and* presents an address we trust — that is, anyone already
    inside the private ranges — can still shape the chain. What the walk
    defeats is the cheap forgery from outside: the appended-real-address
    shape, where the attacker's invented entry sits to the left of the one
    entry the proxy wrote and is therefore skipped.
    """
    peer = request.META.get("REMOTE_ADDR") or ""
    resolved, _ = resolve_chain(peer, forwarded_chain(request))
    return resolved


class TrustedProxySchemeMiddleware:
    """Strip ``X-Forwarded-Proto`` from requests not sent by a trusted proxy.

    Also carries the collapse diagnostic (:meth:`_watch_for_collapse`), which
    lives here rather than in a middleware of its own because it needs the
    same trust answer this one already computes, and because inserting a third
    middleware ahead of :mod:`reeltalk.cookie_policy` would reorder a chain
    whose ordering is itself a tested invariant.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        # Warm the shared memo at startup so an invalid CIDR still fails loudly
        # here rather than on the first request that happens to ask.
        _trusted_networks()
        # A per-process sample, not a shared counter: this diagnostic only has
        # to fire once for a human to read it. The increments are unsynchronised
        # on purpose — under concurrency the count is approximate, and an
        # approximate "everything looks like one address" is still worth
        # warning about. Exactness belongs to the throttle, which counts in
        # Postgres, not here.
        self.sample_size = COLLAPSE_SAMPLE
        self._seen = 0
        self._resolved: set[str] = set()
        self._reported = False

    def __call__(self, request):
        trusted_peer = is_trusted_proxy(request.META.get("REMOTE_ADDR"))
        if not trusted_peer:
            request.META.pop(FORWARDED_PROTO_META, None)
        if trusted_peer:
            self._watch_for_collapse(request)
        return self.get_response(request)

    def _watch_for_collapse(self, request) -> None:
        """Warn once if every proxied request resolves to one address.

        A deploy cannot be told at startup that its proxy chain is wrong,
        because the failure is invisible without traffic: the settings look
        identical whether or not a CDN sits upstream of the proxy. So this
        watches the first ``sample_size`` proxied requests and warns when not
        one of them resolved to a distinct client.

        That is the signature of "your entire user base counts as one
        source" — either the proxy is not forwarding ``X-Forwarded-For`` at
        all, or it is behind a CDN whose ranges are not in
        ``TRUSTED_PROXIES``, so the CDN's egress address is what every
        limit keys on. It is also the signature of an instance that
        genuinely has one user, which is why the message says so instead of
        asserting a fault.
        """
        if self._reported or self._seen >= self.sample_size:
            return
        self._seen += 1
        self._resolved.add(client_ip(request))
        if self._seen < self.sample_size or len(self._resolved) > 1:
            return
        self._reported = True
        (only,) = self._resolved
        if only == (request.META.get("REMOTE_ADDR") or ""):
            detail = (
                " — the same address as the machine connected directly to "
                "us, which means no client address is reaching us through "
                "the proxy at all"
            )
        else:
            detail = (
                " — an address further up the chain, which usually means a "
                "CDN sits in front of the proxy"
            )
        logger.warning(
            "EVERY USER LOOKS LIKE ONE SOURCE: all %d proxied requests "
            "sampled resolved to the same client address (%s)%s. Every "
            "per-address limit on this instance will count your whole user "
            "base as a single caller, so the first few hundred requests "
            "throttle everyone behind them. If a CDN or a second proxy sits "
            "in front of this app, add its address ranges to "
            "TRUSTED_PROXIES. Run `python manage.py check_client_ip` for "
            "the configured ranges and the addresses we have actually been "
            "resolving. If this instance genuinely has one user, this "
            "warning is a false positive and can be ignored.",
            self.sample_size,
            only,
            detail,
        )


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
