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


class TrustedProxySchemeMiddleware:
    """Strip ``X-Forwarded-Proto`` from requests not sent by a trusted proxy."""

    def __init__(self, get_response):
        self.get_response = get_response
        # Keyed memo: the parse is per distinct setting value, not per request,
        # while still picking up an override_settings change (only MIDDLEWARE and
        # ROOT_URLCONF make Django rebuild the chain).
        self._parsed: tuple[tuple[str, ...], list] = ((), [])
        self._networks_for(self._specs())

    def __call__(self, request):
        if not self.is_trusted(request.META.get("REMOTE_ADDR")):
            request.META.pop(FORWARDED_PROTO_META, None)
        return self.get_response(request)

    def is_trusted(self, remote_addr) -> bool:
        """True when ``remote_addr`` is one of the configured proxy CIDRs."""
        if not remote_addr:
            return False
        try:
            addr = ip_address(remote_addr)
        except ValueError:
            return False
        return any(addr in net for net in self._networks_for(self._specs()))

    @staticmethod
    def _specs():
        return tuple(settings.TRUSTED_PROXIES)

    def _networks_for(self, specs):
        if specs == self._parsed[0]:
            return self._parsed[1]
        try:
            networks = [ip_network(spec.strip()) for spec in specs]
        except ValueError as exc:
            raise ImproperlyConfigured(
                f"TRUSTED_PROXIES contains an invalid CIDR: {exc}"
            ) from exc
        self._parsed = (specs, networks)
        return networks
