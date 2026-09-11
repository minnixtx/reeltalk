"""ActivityPub identity and discovery (M4 increment 2, R40).

The actor URL convention everything else in federation hangs off: a local
user's actor is the public page ``/user/<localname>/``, which serves the
Person document to ActivityPub clients (content negotiation) and redirects
browsers to the films page. The user's collections sit under the same path
(``inbox/``, ``outbox/``, ``followers/``, ``following/`` — their routes land
in increments 3-4), and the instance has one shared inbox at ``/inbox/``.

URLs are built from the request so they carry the host as sent and follow
``X-Forwarded-Proto`` when the operator proxy forwards it (D14) — the same
scheme rule as signature verification (R39). The wire document itself is
built fresh against the ActivityPub spec (R7); federation targets are
ReelTalk instances and current Mastodon, so no legacy server's extensions
are needed (R39).
"""

# Media types that mark a request as coming from an ActivityPub client.
AP_MEDIA_TYPES = ("application/activity+json", "application/ld+json")

# R12 localname charset: [a-zA-Z0-9._-], 1-30 chars. The route regexes share
# this pattern so the actor page and the films page agree on what a valid
# localname looks like (the older ``<str>`` converter rejected dots).
LOCALNAME_RE = r"[a-zA-Z0-9._-]+"


def actor_path(localname: str) -> str:
    return f"/user/{localname}/"


def inbox_path(localname: str) -> str:
    return f"/user/{localname}/inbox/"


def outbox_path(localname: str) -> str:
    return f"/user/{localname}/outbox/"


def followers_path(localname: str) -> str:
    return f"/user/{localname}/followers/"


def following_path(localname: str) -> str:
    return f"/user/{localname}/following/"


def shared_inbox_path() -> str:
    return "/inbox/"


def absolute_uri(request, path: str) -> str:
    """Absolute URL for a site path.

    The scheme follows ``X-Forwarded-Proto`` when the operator proxy
    forwards it (D14), else the request scheme; the authority is the host
    as sent (``get_host()`` reads the Host header, falling back to the
    server name) — so documents served behind the proxy carry public URLs.
    """
    forwarded = request.headers.get("X-Forwarded-Proto", "")
    scheme = forwarded.split(",")[0].strip() or request.scheme
    return f"{scheme}://{request.get_host()}{path}"


def accepts_activitypub(request) -> bool:
    """True when the Accept header names an ActivityPub media type.

    Wildcards deliberately do not count: a browser's ``*/*`` must keep
    getting the HTML page, only an explicit AP client gets the document.
    """
    accept = request.headers.get("Accept", "")
    return any(
        part.split(";")[0].strip().lower() in AP_MEDIA_TYPES
        for part in accept.split(",")
    )


def person_document(user, request) -> dict:
    """The ActivityPub Person wire document for a local user (§3.6).

    ``publicKey.id`` is the actor URL with a ``#main-key`` fragment (R39) —
    the same keyid outgoing signatures carry. ``summary``/``image`` are
    omitted while empty. The collection URLs (inbox/outbox/followers/
    following, sharedInbox) are part of the stable shape from day one even
    though their routes land in increments 3-4.
    """
    actor = absolute_uri(request, actor_path(user.localname))
    doc = {
        "@context": [
            "https://www.w3.org/ns/activitystreams",
            "https://w3id.org/security/v1",
        ],
        "id": actor,
        "type": "Person",
        "preferredUsername": user.localname,
        "name": user.display_name or user.localname,
        "url": actor,
        "inbox": absolute_uri(request, inbox_path(user.localname)),
        "outbox": absolute_uri(request, outbox_path(user.localname)),
        "followers": absolute_uri(request, followers_path(user.localname)),
        "following": absolute_uri(request, following_path(user.localname)),
        "endpoints": {"sharedInbox": absolute_uri(request, shared_inbox_path())},
        "publicKey": {
            "id": f"{actor}#main-key",
            "owner": actor,
            "publicKeyPem": user.public_key,
        },
    }
    if user.summary:
        doc["summary"] = user.summary
    if user.avatar:
        doc["image"] = absolute_uri(request, user.avatar.url)
    return doc
