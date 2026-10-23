"""ActivityPub views (M4 increments 2-4, R40/R41 + increment 4).

Thin handlers over ``identity`` / ``collections`` / ``objects``: webfinger,
nodeinfo, and the collections — outbox (paginated Create activities),
followers/following (Person collections), and the per-user + shared inboxes.
All unauthenticated: these are the endpoints other instances fetch from (and
post to) to federate. The bare actor URL /user/<localname>/ is served by the
social app's profile route (M5): Person document for AP clients, the human
profile page for browsers.

Inbox POSTs (increment 4) run the delivery pipeline: resolve the sender from
the signature's keyid (mirroring first-contact remote users from their Person
document), verify the signature, then dedup + dispatch in ``inbox``.
"""

import json

from django.conf import settings
from django.http import HttpResponse, HttpResponseGone, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from reeltalk import __version__
from reeltalk.core.models import Status
from reeltalk.proxy_trust import client_ip
from reeltalk.social.models import SiteSettings, User

from . import inbox_throttle
from .collections import PAGE_SIZE, collection_document, page_document, parse_page
from .identity import (
    absolute_uri,
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
    #
    # A suspended account resolves to nothing here (R102), which takes out
    # its outbox, followers, following, webfinger entry and per-user inbox
    # in one clause. That is the right shape for four of the five: a hidden
    # account has no public collections and should not answer webfinger.
    #
    # The fifth is the inbox, and the cost is deliberate: a peer delivering
    # to a suspended user's inbox now gets 404 rather than 202. Mastodon
    # force-Rejects a suspended account's inbound follows instead, and its
    # own comment says that part is **not reversible** — §2D told us not to
    # copy that without deciding it, so we do not. A 404 is the reversible
    # equivalent: nothing is cancelled, nothing is told, and unsuspend
    # reopens the inbox with no state to unwind.
    return User.objects.filter(
        local=True, localname__iexact=localname, suspended_at__isnull=True
    ).first()


def _banned_gone(user):
    """``410 Gone`` for a banned actor's endpoint, ``None`` otherwise.

    A helper rather than five copies of the same branch, but the reason it is
    a real 410 and not the 404 these views already return for a missing user
    belongs on each caller. For a peer, the two codes mean different things:
    404 invites a retry and leaves the actor's status undetermined, while 410
    is the tombstone — the actor was here and is permanently gone. That is the
    statement ``Delete(Person)`` already made, and an endpoint that answers
    404 after the delete would contradict it.

    Suspension deliberately does **not** use this. A suspended account keeps
    its 404-on-endpoints and its fetchable Person document carrying
    ``suspended: true``, because a suspend is reversible and a peer has to
    be able to resolve the actor id our ``Update(Person)`` points at. Ban
    removes the document; suspend flags it. Two states, two wire behaviour.
    """
    if user.banned_at is not None:
        return HttpResponseGone("This account has been removed.")
    return None


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
    gone = _banned_gone(user)
    if gone is not None:
        return gone
    actor = absolute_uri(actor_path(user.localname))
    doc = {
        "subject": f"acct:{user.localname}@{settings.DOMAIN}",
        "aliases": [actor],
        "links": [
            {
                "rel": "lrdd",
                "type": "application/link-descriptions+json",
                "template": absolute_uri("/.well-known/webfinger") + "?resource={uri}",
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
                "href": absolute_uri("/nodeinfo/2.0"),
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
        # The key is the NodeInfo 2.0 spelling -- plural -- because a
        # spec-conformant reader (Mastodon included) looks for
        # ``openRegistrations`` and reads nothing at all from a singular one.
        "openRegistrations": SiteSettings.get_instance().signup_policy
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
    gone = _banned_gone(user)
    if gone is not None:
        return gone
    collection_url = absolute_uri(outbox_path(user.localname))
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
    minimal Person document keyed on the mirror's home actor URL —
    increment 5). Ordered by id so pages are stable.

    A **suspended** member is dropped from the collection (R102), filtered
    here rather than at the two callers so the followers list and the
    following list cannot drift apart on the rule. Note this is a different
    filter from the one in ``_local_user``: that one hides the suspended
    account's *own* endpoints, this one hides suspended accounts from
    somebody else's collection. Both are needed, and a follower who is
    suspended disappears from everyone they follow's ``followers`` array
    while their own profile stays fetchable as a Person document carrying
    ``suspended: true``.

    A **banned** member is dropped by the same clause and for the same
    reason, from a different column. They are already gone from their own
    endpoints, so the only place they could still surface is inside somebody
    else's collection — a banned follower still appearing in the
    ``followers`` array of everyone they followed, which is a public list of
    a relationship to an account that no longer exists. Listing both columns
    here rather than folding ban into suspension is the point: the two states
    are independent, and a collection that hid only one would leak the other.
    """
    persons = list(
        related.all()
        .filter(suspended_at__isnull=True, banned_at__isnull=True)
        .order_by("id")
    )

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
    gone = _banned_gone(user)
    if gone is not None:
        return gone
    url = absolute_uri(followers_path(user.localname))
    return _person_collection(request, user, url, user.followers.all())


@require_GET
def following(request, localname):
    """The users this one follows — an OrderedCollection of Person docs."""
    user = _local_user(localname)
    if user is None:
        return HttpResponse(status=404)
    gone = _banned_gone(user)
    if gone is not None:
        return gone
    url = absolute_uri(following_path(user.localname))
    return _person_collection(request, user, url, user.follows.all())


def _inbox_too_large(request) -> bool:
    """Whether this inbox POST's body is over the instance's own bound (F7).

    Two checks, because a declared length is a claim and not a fact. The header
    is read first so an honest oversized POST is refused without ever buffering
    it; the real bytes are checked after, because a caller that declares 200
    bytes and sends 200 MB gets to send 200 MB, and the only thing between
    that and a parsed-into-memory megabyte is this line. The global
    ``DATA_UPLOAD_MAX_MEMORY_SIZE`` is two orders of magnitude larger and sized
    for poster uploads, so it is a backstop and not the inbox's bound.
    """
    declared = request.META.get("CONTENT_LENGTH")
    limit = settings.INBOX_MAX_BODY_BYTES
    if declared and declared.isdigit() and int(declared) > limit:
        return True
    return len(request.body) > limit


def _handle_inbox_post(request):
    """Verify + process one inbound activity delivery (M4 increment 4).

    Order is a security property here, not a preference. The two cheap refusals
    come first, because everything after them costs more than the request is
    worth: ``resolve_sender`` dials out to fetch a Person document for any
    keyid it has not seen, so an unauthenticated caller used to be able to buy
    an outbound fetch from us with a POST that had no chance of verifying (F7).
    The body bound and the per-source floor are both answered before that
    dial-out is even considered. Then the sender is resolved from the
    signature's keyid — a first-contact remote user is mirrored from their
    Person document as part of this — and the signature is verified against the
    resolved user's public key before the body is parsed or anything is
    recorded.

    Status codes: 413 for a body over ``INBOX_MAX_BODY_BYTES``; 429 with
    ``Retry-After`` once an address has spent its failure budget; 401 for a
    missing, unresolvable, or invalid signature; 400 for a signed but
    unparseable JSON body; 202 Accepted for everything else (handled,
    gracefully ignored, or duplicate — the outcome is not exposed to the
    sender).

    The refusal for a spent address is 429 rather than a 4xx on purpose. A peer
    treats 4xx as final and discards the activity — Mastodon's
    ``response_error_unsalvageable?`` is "400-499 except 401, 408 and 429" —
    so any other code here would delete the delivery on the sender's side and
    turn a temporary rate refusal into permanent content loss.
    """
    if _inbox_too_large(request):
        return HttpResponse(status=413)

    # The address comes from the trusted-proxy walk, never from a header read
    # here: a budget keyed on a forgeable address punishes only the honest.
    source = client_ip(request)
    if inbox_throttle.inbox_blocked(source):
        response = HttpResponse(status=429)
        response["Retry-After"] = str(inbox_throttle.retry_after_seconds(source))
        return response

    key_id = extract_key_id(request)
    if not key_id:
        # Nothing was spent on this request — no fetch, no verification — so it
        # is not counted. Charging free refusals would let noise spend the
        # budget a real peer needs.
        return HttpResponse(status=401)

    sender = resolve_sender(key_id, request)
    if sender is None or not sender.public_key:
        # This one cost an outbound fetch for an address we learned nothing
        # about. That is the cost being bounded.
        inbox_throttle.record_failure(source)
        return HttpResponse(status=401)
    if not verify_request(request, sender.public_key):
        inbox_throttle.record_failure(source)
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
    user = _local_user(localname)
    if user is None:
        return HttpResponse(status=404)
    gone = _banned_gone(user)
    if gone is not None:
        return gone
    return _inbox_response(request)


@csrf_exempt
def shared_inbox(request):
    """The instance-wide shared inbox (``endpoints.sharedInbox`` in Person)."""
    return _inbox_response(request)
