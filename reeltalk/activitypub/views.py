"""ActivityPub views (M4 increments 2-3, R40/R41).

Thin handlers over ``identity`` / ``collections`` / ``objects``: the actor
endpoint (Person document with content negotiation), webfinger, nodeinfo, and
the collections — outbox (paginated Create activities), followers/following
(Person collections), and the per-user + shared inboxes. All unauthenticated:
these are the endpoints other instances fetch from (and post to) to federate.
"""

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from reeltalk import __version__
from reeltalk.core.models import Status
from reeltalk.social.models import SiteSettings, User

from .collections import PAGE_SIZE, collection_document, page_document, parse_page
from .identity import (
    absolute_uri,
    accepts_activitypub,
    actor_path,
    followers_path,
    following_path,
    outbox_path,
    person_document,
)
from .objects import create_activity


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


# --- Collections (M4 increment 3, R41) ---------------------------------------
#
# Outbox / followers / following are read-side OrderedCollections: served
# without ``?page`` as the collection document, with ``?page=N`` as one page.
# The inboxes exist now so the Person document's URLs never 404 (R40); their
# POST handling (signature check + dedup + remote mirrors) lands in increment 4.


def _collection_response(request, user, collection_url, items_by_offset):
    """Serve a collection (no ``?page``) or one of its pages (``?page=N``).

    ``items_by_offset(start, count)`` returns the item documents for a slice —
    called only for a page request, so an un-paged GET costs just a count.
    """
    if "page" not in request.GET:
        total = items_by_offset(0, 0)[1]
        return JsonResponse(
            collection_document(collection_url, total),
            content_type="application/activity+json",
        )
    page = parse_page(request)
    start = (page - 1) * PAGE_SIZE
    items, _ = items_by_offset(start, PAGE_SIZE)
    return JsonResponse(
        page_document(collection_url, items, start + 1),
        content_type="application/activity+json",
    )


@require_GET
def outbox(request, localname):
    """The user's outbox — their non-deleted local statuses as Create(Note)."""
    user = _local_user(localname)
    if user is None:
        return HttpResponse(status=404)
    collection_url = absolute_uri(request, outbox_path(user.localname))
    statuses = (
        Status.objects.filter(user=user, deleted=False, local=True)
        .select_related("user", "film", "reply_parent")
        .order_by("-published_date", "-id")
    )

    def items_by_offset(start, count):
        page_items = [
            create_activity(status, user, request)
            for status in statuses[start : start + count]
        ]
        return page_items, statuses.count()

    return _collection_response(request, user, collection_url, items_by_offset)


def _person_collection(request, user, collection_url, related):
    """Serve a followers/following relation as an OrderedCollection of Persons.

    Local users only: a remote entry needs the followed actor's home URL, which
    the mirror does not store yet — those land with remote mirrors (increment
    4/5). Ordered by id so pages are stable.
    """
    persons = list(related.filter(local=True).order_by("id"))

    def items_by_offset(start, count):
        page = persons[start : start + count]
        return [person_document(person, request) for person in page], len(persons)

    return _collection_response(request, user, collection_url, items_by_offset)


@require_GET
def followers(request, localname):
    """The users who follow this one — an OrderedCollection of Person docs."""
    user = _local_user(localname)
    if user is None:
        return HttpResponse(status=404)
    url = absolute_uri(request, followers_path(user.localname))
    return _person_collection(request, user, url, user.followers.all())


@require_GET
def following(request, localname):
    """The users this one follows — an OrderedCollection of Person docs."""
    user = _local_user(localname)
    if user is None:
        return HttpResponse(status=404)
    url = absolute_uri(request, following_path(user.localname))
    return _person_collection(request, user, url, user.follows.all())


def _inbox_response(request):
    """Inbox route (R40/R41): exists now, processes activities in increment 4.

    GET is not a valid inbox operation (405); POST is accepted (202) so a
    remote's delivery never hits a 404 while the handler is still a stub. The
    real work — verifying the sender's signature, deduping by origin id, and
    creating/updating remote mirrors — lands in increment 4.
    """
    if request.method == "POST":
        return HttpResponse(status=202)
    response = HttpResponse(status=405)
    response["Allow"] = "POST"
    return response


@csrf_exempt
def inbox(request, localname):
    """The user's per-actor inbox — the target of inbound activity delivery."""
    if _local_user(localname) is None:
        return HttpResponse(status=404)
    return _inbox_response(request)


@csrf_exempt
def shared_inbox(request):
    """The instance-wide shared inbox (``endpoints.sharedInbox`` in Person)."""
    return _inbox_response(request)
