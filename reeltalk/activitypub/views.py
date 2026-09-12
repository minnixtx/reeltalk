"""ActivityPub views (M4 increments 2-4, R40/R41 + increment 4).

Thin handlers over ``identity`` / ``collections`` / ``objects``: the actor
endpoint (Person document with content negotiation), webfinger, nodeinfo, and
the collections — outbox (paginated Create activities), followers/following
(Person collections), and the per-user + shared inboxes. All unauthenticated:
these are the endpoints other instances fetch from (and post to) to federate.

Inbox POSTs (increment 4) run the delivery pipeline: resolve the sender from
the signature's keyid (mirroring first-contact remote users from their Person
document), verify the signature, then dedup + dispatch in ``inbox``.
"""

import json

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
from .inbox import process_inbound_activity
from .mirrors import resolve_sender
from .objects import create_activity
from .signatures import extract_key_id, verify_request


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


def _person_item(person, request):
    """The Person entry for one followers/following collection item.

    A local user gets the full Person document (R40). A remote mirror emits a
    minimal Person document whose id is the mirror's home-instance actor URL
    (R42) — the wire id other instances use to identify that user; we do not
    re-serialize a mirror's summary/image, which it does not carry.
    """
    if person.local:
        return person_document(person, request)
    return {
        "@context": [
            "https://www.w3.org/ns/activitystreams",
            "https://w3id.org/security/v1",
        ],
        "id": person.actor_url,
        "type": "Person",
        "name": person.display_name or person.localname,
    }


def _person_collection(request, user, collection_url, related):
    """Serve a followers/following relation as an OrderedCollection of Persons.

    Includes both local users (full Person documents) and remote mirrors (a
    minimal Person document keyed on the mirror's home actor URL — increment
    5). Ordered by id so pages are stable.
    """
    persons = list(related.all().order_by("id"))

    def items_by_offset(start, count):
        page = persons[start : start + count]
        return [_person_item(person, request) for person in page], len(persons)

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


def _handle_inbox_post(request):
    """Verify + process one inbound activity delivery (M4 increment 4).

    Order matters: the sender is resolved from the signature's keyid — a
    first-contact remote user is mirrored from their Person document as part
    of this — and the signature is verified against the resolved user's
    public key before the body is parsed or anything is recorded. Status
    codes: 401 for a missing, unresolvable, or invalid signature; 400 for a
    signed but unparseable JSON body; 202 Accepted for everything else
    (handled, gracefully ignored, or duplicate — the outcome is not exposed
    to the sender).
    """
    key_id = extract_key_id(request)
    if not key_id:
        return HttpResponse(status=401)
    sender = resolve_sender(key_id, request)
    if sender is None or not sender.public_key:
        return HttpResponse(status=401)
    if not verify_request(request, sender.public_key):
        return HttpResponse(status=401)
    try:
        activity = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)
    process_inbound_activity(activity, sender, request)
    return HttpResponse(status=202)


def _inbox_response(request):
    """Inbox route (R40/R41, real handling in increment 4).

    GET is not a valid inbox operation (405); POST runs the delivery
    pipeline above. Both the per-user and shared inboxes process deliveries
    identically — dedup by activity id makes it harmless when a remote posts
    to both.
    """
    if request.method == "POST":
        return _handle_inbox_post(request)
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
