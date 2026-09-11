"""ActivityPub identity + discovery views (M4 increment 2, R40).

Thin handlers over ``identity``: the actor endpoint (Person document with
content negotiation), webfinger, and nodeinfo. All GET, unauthenticated —
these are the endpoints other instances fetch to find us.
"""

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_GET

from reeltalk import __version__
from reeltalk.social.models import SiteSettings, User

from .identity import absolute_uri, accepts_activitypub, actor_path, person_document


def _local_user(localname: str) -> "User | None":
    # Case-insensitive: our identity model treats case variants as one user
    # (signup rejects insensitive duplicates), and Mastodon lowercases
    # usernames. The stored spelling wins — responses carry it back so
    # remotes learn the canonical case.
    return User.objects.filter(local=True, localname__iexact=localname).first()


@require_GET
def actor(request, localname):
    """The user's actor URL (R40): Person JSON-LD for ActivityPub clients,
    a redirect to the films page for everyone else."""
    user = _local_user(localname)
    if user is None:
        return HttpResponse(status=404)
    if accepts_activitypub(request):
        return JsonResponse(
            person_document(user, request),
            content_type="application/activity+json",
        )
    return redirect(f"/user/{user.localname}/films/")


@require_GET
def webfinger(request):
    """RFC 6454: ``?resource=acct:<localname>@<domain>`` → JRD document."""
    resource = request.GET.get("resource", "")
    if not resource.startswith("acct:"):
        return HttpResponse(status=404)
    localname, _, domain = resource[len("acct:") :].partition("@")
    # Only identities on this instance resolve here (R40).
    if not domain or domain.lower() != settings.DOMAIN.lower():
        return HttpResponse(status=404)
    user = _local_user(localname)
    if user is None:
        return HttpResponse(status=404)
    actor = absolute_uri(request, actor_path(user.localname))
    doc = {
        "subject": f"acct:{user.localname}@{settings.DOMAIN}",
        "aliases": [actor],
        "links": [
            {
                "rel": "lrdd",
                "type": "application/link-descriptions+json",
                "template": absolute_uri(request, "/.well-known/webfinger")
                + "?resource={uri}",
            },
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": actor,
            },
            {"rel": "self", "type": "application/activity+json", "href": actor},
        ],
    }
    return JsonResponse(doc, content_type="application/jrd+json")


@require_GET
def nodeinfo_index(request):
    """The NodeInfo discovery document — points at the 2.0 document."""
    doc = {
        "links": [
            {
                "rel": "http://nodeinfo.digip.org/spec/2.0",
                "href": absolute_uri(request, "/nodeinfo/2.0"),
            }
        ]
    }
    return JsonResponse(doc)


@require_GET
def nodeinfo_2_0(request):
    """The NodeInfo 2.0 document (§3.6): software, protocols, registration."""
    doc = {
        "version": "2.0",
        "software": {"name": "reeltalk", "version": __version__},
        "protocols": ["activitypub"],
        # The instance's open-registration policy follows the signup policy.
        "openRegistration": SiteSettings.get_instance().signup_policy
        == SiteSettings.OPEN,
        "metadata": {},
    }
    return JsonResponse(doc)
