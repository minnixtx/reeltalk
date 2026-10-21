"""HTTP for untrusted remote URLs: connect only to globally routable IPs.

Check the actual socket destination, including on redirects, and connect to
the checked address without a second hostname lookup. TLS still uses the
original hostname for SNI and certificate verification.

Private destinations are refused unless their network is named in
settings.FEDERATION_ALLOWED_NETWORKS, the opt-in for a peer that genuinely
shares the instance's network (same env-list shape as TRUSTED_PROXIES).
"""

import ipaddress
import socket

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.util.connection import create_connection

MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_ALLOW_MEMO: tuple[tuple[str, ...], list] = ((), [])


def _allowed_networks():
    """Parse ``FEDERATION_ALLOWED_NETWORKS`` once per distinct setting value.

    Keyed on the value rather than cached forever so an ``override_settings``
    in a test is picked up immediately -- the memo shape proxy_trust uses for
    TRUSTED_PROXIES.
    """
    global _ALLOW_MEMO
    specs = tuple(settings.FEDERATION_ALLOWED_NETWORKS)
    if specs == _ALLOW_MEMO[0]:
        return _ALLOW_MEMO[1]
    try:
        networks = [
            ipaddress.ip_network(spec.strip()) for spec in specs if spec.strip()
        ]
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"FEDERATION_ALLOWED_NETWORKS contains an invalid CIDR: {exc}"
        ) from exc
    _ALLOW_MEMO = (specs, networks)
    return networks


def _parse_literal(literal):
    # getaddrinfo can hand back a scoped IPv6 literal (fe80::1%eth0). The
    # scope id names a local interface, not part of the destination, and
    # ip_address() raises ValueError on literals it cannot parse at all --
    # strip the scope, and treat anything still unparseable as refused
    # rather than letting a ValueError escape the OSError contract below.
    try:
        return ipaddress.ip_address(literal.split("%", 1)[0])
    except ValueError:
        return None


def _blocked(ip):
    """True when the checked address may not be dialed.

    Global unicast always passes. Multicast is always refused. Everything
    else (loopback, RFC1918, link-local, unique-local, cloud metadata)
    passes only when its own network is named in the opt-in allowlist.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if ip.is_multicast or (mapped and mapped.is_multicast):
        return True
    if ip.is_global and not (mapped and not mapped.is_global):
        return False
    candidates = [ip] + ([mapped] if mapped else [])
    return not any(
        candidate in network
        for network in _allowed_networks()
        for candidate in candidates
        if candidate.version == network.version
    )


class PublicConnectionMixin:
    def _new_conn(self):
        try:
            addresses = socket.getaddrinfo(
                self._dns_host, self.port, type=socket.SOCK_STREAM
            )
            if not addresses:
                raise OSError("No addresses for remote host")
            for _, _, _, _, address in addresses:
                ip = _parse_literal(address[0])
                if ip is None or _blocked(ip):
                    raise OSError("Remote destination is not a public IP address")
            last_error = None
            for _, _, _, _, address in addresses:
                try:
                    return create_connection(
                        (address[0], self.port),
                        self.timeout,
                        source_address=self.source_address,
                        socket_options=self.socket_options,
                    )
                except OSError as exc:
                    last_error = exc
            raise last_error
        except OSError as exc:
            raise NewConnectionError(self, str(exc)) from exc


class PublicHTTPConnection(PublicConnectionMixin, HTTPConnection):
    pass


class PublicHTTPSConnection(PublicConnectionMixin, HTTPSConnection):
    pass


class PublicHTTPPool(HTTPConnectionPool):
    ConnectionCls = PublicHTTPConnection


class PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = PublicHTTPSConnection


class PublicHTTPAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        # Do not mutate urllib3's process-wide pool class mapping.
        self.poolmanager.pool_classes_by_scheme = {
            "http": PublicHTTPPool,
            "https": PublicHTTPSPool,
        }

    def send(self, request, **kwargs):
        response = super().send(request, **kwargs)
        if not kwargs.get("stream"):
            try:
                chunks = []
                size = 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise requests.RequestException(
                            "Remote response exceeds size cap"
                        )
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                response._content_consumed = True
            finally:
                response.close()
        return response


def session():
    client = requests.Session()
    # Environment proxies could resolve destinations outside this guard.
    client.trust_env = False
    client.mount("http://", PublicHTTPAdapter())
    client.mount("https://", PublicHTTPAdapter())
    return client


def get(url, **kwargs):
    with session() as client:
        return client.get(url, **kwargs)


def post(url, **kwargs):
    # A signed activity must not be forwarded to a different URL by redirect.
    kwargs.setdefault("allow_redirects", False)
    with session() as client:
        return client.post(url, **kwargs)
