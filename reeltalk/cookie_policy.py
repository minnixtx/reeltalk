"""Give cookies the ``Secure`` flag the transport actually deserves.

``SECURE_COOKIES`` is a global setting, but one instance can front two very
different transports: the public domain behind a TLS terminator, and a plain
``http://<lan-ip>:<port>`` endpoint the operator uses inside their own
network. With ``Secure`` set unconditionally, a browser refuses to store or
return the cookie over ``http://`` at all -- the ``csrftoken`` never comes
back, so the login POST 403s and the LAN endpoint cannot hold a session.

So the flag follows the scheme: a request that arrived over https gets
``Secure`` cookies, one that arrived over plain http does not. That means a
session started on the LAN in cleartext has a cookie readable by anyone on
that network path -- an explicit operator trade, which is why the strict
behaviour stays one env flip away (``COOKIES_FOLLOW_SCHEME=false``).

The scheme this keys on is only trustworthy because
``reeltalk.proxy_trust.TrustedProxySchemeMiddleware`` runs first and strips
a spoofed ``X-Forwarded-Proto`` from any peer that is not the operator's
terminator. This middleware must stay after it.
"""

from django.conf import settings


class SchemeAwareCookieMiddleware:
    """Add ``Secure`` on https responses, drop it on plain-http ones."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if settings.SECURE_COOKIES and settings.COOKIES_FOLLOW_SCHEME:
            if request.is_secure():
                self._mark_secure(response)
            else:
                self._clear_secure(response)
        return response

    @staticmethod
    def _mark_secure(response):
        for morsel in response.cookies.values():
            morsel["secure"] = True

    @staticmethod
    def _clear_secure(response):
        for morsel in response.cookies.values():
            if "secure" in morsel:
                del morsel["secure"]
