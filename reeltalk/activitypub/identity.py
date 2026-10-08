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

# The ActivityStreams public audience collection. Named once because two
# builders now emit it and a receiver that has to spot it must not be left
# matching a string that drifted in one of them. Mastodon's own
# ``TagManager::COLLECTIONS[:public]`` is the same IRI, and its inbound
# ``public_collection?`` also accepts the short ``as:Public`` / ``Public``
# forms -- we send the full IRI, which every implementation reads.
PUBLIC_COLLECTION = "https://www.w3.org/ns/activitystreams#Public"

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
            # ``toot:suspended`` is the only non-standard term we publish,
            # and it is here because the vocabulary is already the de facto
            # one for this flag — the peer's own Person document declares
            # the same mapping, which is how this was chosen rather than
            # guessed. A strict processor needs the prefix declared or it
            # drops the term as undefined.
            {"toot": "http://joinmastodon.org/ns#"},
        ],
        "id": actor,
        "type": "Person",
        "preferredUsername": user.localname,
        "name": user.display_name or user.localname,
        "url": actor,
        # Emitted **always**, never only when true, for the reason spelled
        # out on ``actor_update_activity``: omitting it on an unsuspend
        # would leave a peer unable to tell "cleared" from "never spoken".
        "suspended": user.suspended_at is not None,
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


def actor_update_activity(user, request) -> dict:
    """An ``Update(Person)`` announcing this actor's current document.

    The suspension broadcast (R102): a peer that already holds our Person
    document needs to be told its state changed, because nothing else on
    the wire will. The document carried by the activity is the same
    ``person_document`` the route serves, so the peer cannot receive an
    update whose body disagrees with what it would get by fetching the
    actor URL itself.

    **The ``suspended`` flag is emitted always, not only when true.** The
    peer's own vocabulary declares it — ``"suspended": "toot:suspended"``
    was read off ``upallnight.minnix.dev``'s Person ``@context``, not
    recalled — and Mastodon omits the key when the account is fine. We do
    the opposite on purpose: an ``Update(Person)`` that *omits* the flag
    makes "this account was unsuspended" indistinguishable from "this
    server does not speak the flag", which is R88's silence-that-kills
    shape. Sending ``false`` is the only way an unsuspend says anything at
    all. Whether a given peer **honours** an inbound ``suspended: true`` is
    that peer's business and is not claimed anywhere here.

    The audience is the actor's remote followers, which is what
    ``broadcast_actor_update`` does.
    """
    return {
        "@context": "https://www.w3.org/ns/activitystreams",
        "type": "Update",
        "actor": absolute_uri(actor_path(user.localname)),
        "object": person_document(user, request),
    }


def person_delete_activity(user) -> dict:
    """A ``Delete`` claiming this actor itself, for the ban broadcast (R102).

    Shaped against what the peer's handler actually accepts rather than
    against what an ``Update`` looks like, because the two are not
    interchangeable and getting it wrong fails silently. Mastodon's
    ``ActivityPub::Activity::Delete#perform`` opens with::

        return delete_person if @account.uri == object_uri

    where ``@account`` is the **signature-verified** sender. Three
    consequences, each of which this shape satisfies:

    * **The ``object`` is the bare actor URI, not the Person document.**
      A peer compares the object's URI against the signer's; an embedded
      document would not match and the activity would fall through to the
      status-delete branch and find nothing.
    * **The actor must be the banned user, and the signature must be theirs.**
      A ``Delete(Person)`` signed by the moderator about someone else does
      not delete that person — it fails the ``==`` test and is a no-op. Same
      rule increment 3 pinned for the status delete and 4b for the actor
      update: the identity on the wire is the actor the statement describes,
      never whoever pressed the button. It follows that a *mirror* cannot be
      banned by us at all — we hold no private key for another instance's
      identity.
    * **The activity id is deterministic**, ``<actor>#delete``, matching
      Mastodon's ``DeleteActorSerializer``. A ban is a single fact about one
      actor rather than a stream of distinct events, so a replayed delivery
      dedupes on the receiving side instead of arriving as new news.

    ``to`` names the public collection because the statement is about a
    public actor and its removal is not addressed to anyone in particular —
    again matching the peer's own serializer, which emits the same.
    """
    actor = absolute_uri(actor_path(user.localname))
    return {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{actor}#delete",
        "type": "Delete",
        "actor": actor,
        "to": [PUBLIC_COLLECTION],
        "object": actor,
    }


def flag_activity(*, representative, report_id: int, comment: str, object_uris) -> dict:
    """A ``Flag`` — an instance telling another about one of *their* accounts.

    Shaped against the peer's own ``ActivityPub::FlagSerializer`` rather
    than invented, because a ``Flag`` the receiving instance cannot parse is
    a report that silently never happened. Their serializer emits exactly
    ``{id, type, actor, content, object}``, where ``object`` is a **flat
    array of URIs** — the target account's URI plus the URIs of the
    statuses being reported — and their inbound handler walks that array
    sorting the URIs into accounts and statuses itself. An embedded object,
    or a single non-array ``object``, is a shape they do not read.

    **The actor is the instance representative, never the reporter (R104).**
    This is the whole reason the representative exists. R104's reason is a
    doxxing vector: a foreign admin who can see who reported their user
    holds a person they can retaliate against, and the reporter never
    agreed to be introduced to another server's moderation. The reporter's
    identity appears nowhere in this document — not in ``actor``, not in
    ``id``, not in ``content``. What *does* cross the wire is the reporter's
    own words, because the report is worthless without them; Mastodon sends
    ``object.comment`` as ``content`` for exactly that reason, and masking
    the identity has never meant masking the complaint.

    **The activity id carries our report id on our own host.** Mastodon's
    ``report_uri`` keeps the incoming ``id`` only when its host matches the
    verified sender's, then stores it on the report row. Minting ours as
    ``<canonical origin>/reports/<pk>/`` therefore survives the trip and
    arrives in *their* table pointing back at *our* row — which is what
    makes a live proof checkable from their side rather than something we
    have to take on trust. An id on some other host would be dropped by
    their own check.

    ``object_uris`` is assembled by the caller because resolving a status to
    the URL that identifies it *to another instance* needs the request and
    the status's own ``remote_url`` (see
    :func:`~reeltalk.activitypub.objects.note_reference`), and this
    function's job is to mint the activity, not to decide which URL a
    foreign server should be able to resolve.
    """
    return {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": absolute_uri(f"/reports/{report_id}/"),
        "type": "Flag",
        "actor": absolute_uri(actor_path(representative.localname)),
        "content": comment or "",
        "object": list(object_uris),
    }
