"""ActivityPub identity and discovery (M4 increment 2, R40).

The actor URL convention everything else in federation hangs off: a local
user's actor is the public page ``/user/<localname>/``, which serves the
Person document to ActivityPub clients (content negotiation) and redirects
browsers to the films page. The user's collections sit under the same path
(``inbox/``, ``outbox/``, ``followers/``, ``following/`` — their routes land
in increments 3-4), and the instance has one shared inbox at ``/inbox/``.

URLs are minted from ``settings.CANONICAL_ORIGIN`` rather than from the request,
so an identifier never depends on which host or scheme the actor's browser
happened to use (see ``absolute_uri``). The wire document itself is built fresh
against the ActivityPub spec (R7); federation targets are
ReelTalk instances and current Mastodon, so no legacy server's extensions
are needed (R39).
"""

from django.conf import settings

from .crypto import public_key_multibase

# Media types that mark a request as coming from an ActivityPub client.
AP_MEDIA_TYPES = ("application/activity+json", "application/ld+json")

# R12 localname charset: [a-zA-Z0-9._-], 1-30 chars. The route regexes share
# this pattern so the actor page and the films page agree on what a valid
# localname looks like (the older ``<str>`` converter rejected dots).
LOCALNAME_RE = r"[a-zA-Z0-9._-]+"

# The human profile/films routes also match remote-mirror localnames, which
# are <preferredUsername>@<netloc> (M4 increment 4) — so the pattern adds '@'
# and ':' (the netloc carries the port when it is non-default). Federation
# routes stay on LOCALNAME_RE: a mirror's canonical wire document lives on
# its home instance, not here.
PROFILE_LOCALNAME_RE = r"[a-zA-Z0-9._@:-]+"


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


def absolute_uri(path: str) -> str:
    """Absolute URL for a site path, on the instance's canonical origin.

    Minted from ``settings.CANONICAL_ORIGIN``, never from the request. An
    ActivityPub identifier has to be resolvable by whoever receives it, and the
    host and scheme the acting browser happened to use say nothing about what a
    peer can reach: a moderator working from ``http://192.168.1.138:3030``
    would otherwise sign ``http://192.168.1.138:3030/user/alice/#main-key``
    and mint a ``Delete`` whose object id does not match the
    ``https://…/status/123/`` the peer already holds. Taking the request out of
    this function removes that whole class rather than guarding it, and it also
    means a spoofed ``X-Forwarded-Proto`` can no longer rewrite a published
    identity at all — the gate in ``TrustedProxySchemeMiddleware`` (R74) still
    governs ``request.is_secure()`` and the cookie flags, which is where the
    transport actually matters.
    """
    return f"{settings.CANONICAL_ORIGIN}{path}"


def reference_url(value) -> str | None:
    """The URL a wire reference field carries, in whichever shape it arrived.

    ActivityPub link properties have no single shape: the spec allows a bare
    IRI string, an embedded object with an ``id``, and any of those inside an
    array. In practice Mastodon sends a bare string for ``object`` and
    ``inReplyTo``, and a ReelTalk peer sends whatever it built. Every
    inbound resolver needs the same unwrap, so it lives here once rather
    than as a private helper in each handler module.

    For a list the first usable entry wins — a multi-valued reference is
    rare enough that no handler here acts on more than one, and picking
    consistently beats picking differently per call site.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        nested = value.get("id")
        return nested if isinstance(nested, str) and nested else None
    if isinstance(value, list):
        for item in value:
            nested = reference_url(item)
            if nested:
                return nested
    return None


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

    The key is published twice, under the **same** ``#main-key`` URI,
    because a PEM cannot say what algorithm it holds. ``publicKey`` /
    ``publicKeyPem`` is the legacy shape every instance reads; the FEP-521a
    ``assertionMethod`` ``Multikey`` carries the multicodec tag that tells a
    peer the key is Ed25519. Mastodon 4.7.2 needs the second: its
    ``publicKey`` ingest pins whatever it reads there to RSA, so without the
    typed entry it tries to verify our Ed25519 signatures with an RSA key
    and fails (R88). Sharing one URI is deliberate — Mastodon dedupes by URI
    and prefers the typed entry, so the two never disagree about which key a
    keyid names.
    """
    actor = absolute_uri(actor_path(user.localname))
    key_id = f"{actor}#main-key"
    doc = {
        "@context": [
            "https://www.w3.org/ns/activitystreams",
            "https://w3id.org/security/v1",
            # Defines Multikey / assertionMethod / publicKeyMultibase so a
            # strict JSON-LD processor does not drop the typed key.
            "https://www.w3.org/ns/cid/v1",
        ],
        "id": actor,
        "type": "Person",
        "preferredUsername": user.localname,
        "name": user.display_name or user.localname,
        "url": actor,
        "inbox": absolute_uri(inbox_path(user.localname)),
        "outbox": absolute_uri(outbox_path(user.localname)),
        "followers": absolute_uri(followers_path(user.localname)),
        "following": absolute_uri(following_path(user.localname)),
        "endpoints": {"sharedInbox": absolute_uri(shared_inbox_path())},
        "publicKey": {
            "id": key_id,
            "owner": actor,
            "publicKeyPem": user.public_key,
        },
    }
    # A local user always carries a key from User.save, so the empty case is
    # an unsaved or half-written row rather than something to fail the
    # document over. The typed entry is only publishable when there is a key
    # to encode.
    if user.public_key:
        doc["assertionMethod"] = [
            {
                "id": key_id,
                "type": "Multikey",
                "controller": actor,
                "publicKeyMultibase": public_key_multibase(user.public_key),
            }
        ]
    if user.summary:
        doc["summary"] = user.summary
    if user.avatar:
        doc["image"] = absolute_uri(user.avatar.url)
    return doc
