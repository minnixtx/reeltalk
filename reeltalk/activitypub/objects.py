"""ActivityPub object wire types (M4 increments 3 and 6; Film per D15).

Serializers for the objects ReelTalk exchanges: the custom **Film** type (D15
— a clean break from book-era wire types), **Note** (statuses: reviews,
ratings, comments), and **ShelfEvent** (a user putting a film on — or taking
it off — one of the D1 shelves; increment 6). Plus the activities that carry
them: ``Create`` / ``Update`` / ``Delete``, and the interaction pair
``Like`` / ``Undo(Like)`` (feed interactions increment 5). Built fresh
against the ActivityPub / ActivityStreams specs (R7); federation targets are
ReelTalk instances and current Mastodon (R39), so no legacy server's
extensions are needed.

Object ids use the day-one origin identity fields: a locally created object's
wire id is its ``origin_id`` (backfilled to the row's pk — see migration
``core/0006`` / ``social`` save paths), falling back to the pk while unset. A
*mirrored* remote object serves its home-instance URL instead — the full wire
URL stored as received on the mirror row (``remote_url``, increment 6; R42:
the integer ``remote_id`` alone cannot reconstruct a host).

Activity ids: a ``Create`` is stable per object (keyed on the origin identity)
so redeliveries dedup; an ``Update`` and a shelf event are unique per event
(uuid fragment) — a second edit, or an unshelve-then-re-shelve, must not
collide on the id or the receiving instance would drop it as a redelivery.
The *object* ids stay stable either way, so applying an activity is
idempotent by origin identity (increment 6).
"""

import uuid
from datetime import UTC

from django.utils.html import escape

from reeltalk.core.models import Status

from .identity import (
    PUBLIC_COLLECTION,
    absolute_uri,
    actor_path,
    followers_path,
    outbox_path,
)

_CONTEXT = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
]

# The ReelTalk extension vocabulary (L11 / R142). Declared as a prefix dict
# on the *Note's* own ``@context`` rather than left as a bare term, because a
# strict JSON-LD processor drops an undefined term instead of passing it
# through -- the same reason ``toot`` is declared in
# ``identity.person_document``. The IRI is the project's domain and not the
# instance's: every ReelTalk install publishes the same vocabulary, and a
# prefix IRI is never dereferenced, so the site behind it does not have to
# exist yet.
REELTALK_NS = "https://reeltalk.dev/ns#"

# The Note context widened with the one prefix the list extension needs. Only
# list Notes carry it; a review keeps the two-entry ``_CONTEXT`` untouched.
_LIST_CONTEXT = [*_CONTEXT, {"reeltalk": REELTALK_NS}]

# The key the extension rides under. Named here rather than typed at each site
# because the importer in ``statuses`` has to read exactly this term: a drift
# between what we write and what we look for does not raise anywhere, it just
# reads as "this Note is not a list" and mirrors a shapeless status.
LIST_EXTENSION = "reeltalk:list"

# How many ranked films get written into the human-readable body. The
# extension carries every item regardless; this caps only the prose a
# Mastodon reader sees, so a two-hundred-film list cannot hand a peer a
# document that trips its content limit and comes back looking broken with
# no way back to the real thing.
LIST_CONTENT_FILM_LIMIT = 50


def _iso(dt) -> str:
    """RFC 3339 (UTC, ``Z`` suffix) — the ActivityPub timestamp shape."""
    text = dt.astimezone(UTC).isoformat()
    return text.replace("+00:00", "Z")


def film_local_id(film) -> int:
    """The local identity a Film's wire id is built from (origin_id, else pk)."""
    return film.origin_id or film.pk


def note_local_id(status) -> int:
    """The local identity a Note's wire id is built from (origin_id, else pk)."""
    return status.origin_id or status.pk


def film_url(request, film) -> str:
    """A film's canonical wire id (M4 increment 6).

    A mirror serves its home-instance URL — the object's identity on the
    instance that created it (``remote_url``, stored as received); a local
    film's is its origin URL on this instance. References in Notes and
    ShelfEvents use this, so other instances resolve the film to their own
    row (local or mirrored) by D7 instead of creating a duplicate.
    """
    return film.remote_url or absolute_uri(f"/film/{film_local_id(film)}/")


def note_url(request, status) -> str:
    return absolute_uri(f"/status/{note_local_id(status)}/")


def note_reference(request, status) -> str:
    """The wire URL that identifies ``status`` **to another instance**.

    The same rule ``film_url`` applies to films (R42): a mirror's identity is
    its home-instance URL as received, so a reference to one must carry that
    URL and not a copy of it minted here. ``note_url`` alone cannot do this —
    it builds ``/status/<origin_id or pk>/`` on our own host, which for a
    mirror is a URL that says "ours" about something that is not ours.

    This is the reference to use for anything pointing *at* a status from
    outside it: a ``Like``'s object, an ``inReplyTo``. ``note_url`` stays
    correct for a local status's own ``id``, which is only ever served for a
    status we authored (the AP arm of ``status_detail`` 404s a mirror).
    """
    return status.remote_url or note_url(request, status)


def film_document(film, request) -> dict:
    """The **Film** wire document (D15).

    ``tmdbId``/``imdbId`` are carried so receiving ReelTalk instances can dedup
    per D7. Empty/absent fields are omitted rather than null. A mirror's id is
    its home-instance URL as received (M4 increment 6); a local film's resolves
    to this instance's ``/film/<id>/``.
    """
    url = film_url(request, film)
    doc = {
        "@context": _CONTEXT,
        "id": url,
        "type": "Film",
        "name": film.title,
        "url": url,
    }
    if film.subtitle:
        doc["subtitle"] = film.subtitle
    if film.description:
        doc["summary"] = film.description
    if film.year is not None:
        doc["year"] = film.year
    if film.runtime is not None:
        doc["runtime"] = film.runtime
    for field in ("genres", "directors", "cast"):
        values = getattr(film, field)
        if values:
            doc[field] = list(values)
    if film.poster:
        doc["image"] = absolute_uri(film.poster.url)
    if film.tmdb_id is not None:
        doc["tmdbId"] = film.tmdb_id
    if film.imdb_id:
        doc["imdbId"] = film.imdb_id
    return doc


def mention_tag(user, request) -> dict:
    """One ``Mention`` tag, in the shape Mastodon reads and nothing more.

    Mastodon's ``MentionSerializer`` carries exactly ``type`` / ``href`` /
    ``name``, so a fourth field here would be ours alone and no peer owes it
    anything. ``href`` is the **actor URI**, never a ``/user/…`` page path:
    Mastodon resolves a mention on ``href`` and never on ``name``
    (``process_mention``, app/lib/activitypub/activity/create.rb), so a
    ``name`` nobody can resolve is harmless while an ``href`` nobody can
    reach loses the mention outright.

    The href follows the same local/remote split every other actor reference
    in this codebase uses — a local member's actor URI is built from their
    localname on this host, a mirror's is the ``actor_url`` received from
    its home instance. Building our own URL for a mirror would put a
    same-host URI on the wire about somebody who does not live here; it
    would still resolve in the end, through our profile page re-serving
    their real id, but it says the wrong thing on the way.

    ``name`` is ``"@" + localname``, which is Mastodon's ``acct`` form for
    free: ``@alice`` for a local member, ``@minnix@upallnight.minnix.dev``
    for a mirror, because a mirror's stored localname already carries the
    domain. It is built from the **stored** localname, never from whatever
    casing was typed, so the name and the href always describe the same
    account.
    """
    return {
        "type": "Mention",
        "href": actor_reference(user),
        "name": f"@{user.localname}",
    }


def actor_reference(user) -> str:
    """The actor URI for ``user``, on the host that actually owns the identity.

    The local/remote split ``mention_tag`` documents and every actor
    reference here needs: a local member's actor is minted from their
    localname on this host, a mirror's is the ``actor_url`` its home
    instance published. Minting ``absolute_uri(actor_path(mirror))`` would
    put a same-host URI on the wire about somebody who does not live here.

    Shared with :func:`status_audience` because an audience array gets the
    same rule for the same reason -- and an audience entry that names the
    wrong host is worse than a missing one, because a peer will resolve it
    and reach something that is not who the array claims.
    """
    if user.local:
        return absolute_uri(actor_path(user.localname))
    return user.actor_url


def status_audience(status, mentioned_users=None) -> tuple[list[str], list[str]]:
    """The ``to``/``cc`` pair for a status, shaped to match the peer's own.

    **Why this exists.** A receiving Mastodon derives a status's visibility
    from these two arrays and from nothing else -- ``StatusParser#visibility``
    reads the public collection in ``to`` and calls it ``public``; the
    public collection in ``cc`` alone and it is ``unlisted``; ``to`` naming
    only our followers collection and it is ``private``; and an activity
    with neither falls through to **``direct``**. We emitted neither, so
    every status we sent arrived on a peer filed as a direct message --
    counted on the account, returned by nothing. That single fall-through
    is the whole of the R143 measurement (``statuses_count: 18`` against a
    profile endpoint that answers ``0``).

    The mapping is the peer's own ``TagManager.to``/``.cc`` for a public
    status, which is the only visibility this product has:

    * ``to`` is the public collection, alone.
    * ``cc`` is the author's **followers collection** -- one URI for the
      collection, not one entry per follower -- followed by every
      mentioned account's actor URI.

    The mention entries are not decoration. A peer that finds a tagged
    account absent from the audience marks that mention *silent*: recorded,
    no notification, not in a timeline. An addressed mention is what makes
    naming someone on the wire actually reach them.

    **This is an audience, not a delivery list.** Who gets a POST is
    ``broadcast._status_targets``, a different set answering a different
    question: it holds local ``User`` rows to sign deliveries for, while
    ``cc`` holds URIs describing who may see the post. They agree on every
    remote member and differ on every local one, so neither is derived from
    the other and both are needed.

    ``mentioned_users`` lets a caller pass a mention list it already loaded.
    ``note_document`` needs the same rows for its ``tag`` array, and the
    outbox serializes a page of these at a time -- re-reading the mention
    table per document for a second purpose is a needless query, not a
    cheap one.
    """
    if mentioned_users is None:
        mentioned_users = [
            mention.user for mention in status.mentions.select_related("user")
        ]
    to = [PUBLIC_COLLECTION]
    cc = [absolute_uri(followers_path(status.user.localname))]
    for user in mentioned_users:
        uri = actor_reference(user)
        if uri and uri not in cc:
            cc.append(uri)
    return to, cc


def note_document(status, request) -> dict:
    """The **Note** wire document for a status (review / rating / comment).

    v0.1 keeps one ``Note`` type on the wire: a receiving ReelTalk instance
    maps it back by shape — a ``rating`` makes it a review/rating-only entry,
    content without a rating a comment. ``film`` is the referenced film's wire
    id (a custom field); ``inReplyTo`` carries threading. The Note's id is a
    stable URL served at ``/status/<id>/`` (increment 6) — remotes usually
    consume the object inline from the outbox / delivery in v0.1.

    The ``to``/``cc`` pair rides on the **object** as well as on the
    activities that wrap it, because that is where the peer puts it
    (``NoteSerializer`` declares both) and a receiver that reads the Note
    rather than the envelope still has to be able to tell that this was a
    public post. See :func:`status_audience` for what each array means and
    why an address-less Note is the worst shape we could have sent.
    """
    # One read of the mention table serves both the audience and the ``tag``
    # array below. The outbox serializes a page of these documents at a
    # time, so the second read would be a query per row for no reason.
    mentioned_users = [
        mention.user for mention in status.mentions.select_related("user")
    ]
    to, cc = status_audience(status, mentioned_users)
    doc = {
        "@context": _CONTEXT,
        "id": note_url(request, status),
        "type": "Note",
        "attributedTo": absolute_uri(actor_path(status.user.localname)),
        "to": to,
        "cc": cc,
        "publishedTime": _iso(status.published_date),
    }
    if status.content:
        doc["content"] = status.content
    if status.edited_date is not None:
        doc["editedTime"] = _iso(status.edited_date)
    if status.rating is not None:
        doc["rating"] = float(status.rating)
    # FKs lazy-load on access; the outbox view select_related()s them so a
    # page of items costs no extra queries.
    if status.film_id is not None:
        doc["film"] = film_url(request, status.film)
    if status.reply_parent_id is not None:
        # The parent's *home* URL, not ours (``note_reference``). A local
        # reply to a mirrored post is the case that matters: the turn being
        # answered lives on another instance, and pointing at a URL we minted
        # for it would make the thread unresolvable there — and claim our own
        # identity for their object (R42).
        doc["inReplyTo"] = note_reference(request, status.reply_parent)
    mentions = [mention_tag(user, request) for user in mentioned_users]
    # Omitted when there are none, the way ``film_document`` omits what a
    # document does not carry. An empty ``tag`` array says nothing, and a
    # peer that iterates it pays for the privilege.
    if mentions:
        doc["tag"] = mentions
    # A list's post face is the one Note whose body is composed rather than
    # read, and the arm sits **last**, after the ordinary ``content`` arm, so
    # the composed body wins. The face deliberately stores no text of its own
    # (increment 2 keeps the title and description in exactly one place), and
    # even if such a row somehow carried content it must not stand in for the
    # ranked list.
    #
    # Composing from ``FilmList`` rather than off the face is also what makes
    # the ``Delete`` tombstone correct without a second code path.
    # ``Status.delete()`` clears ``content`` and ``raw_content``, but
    # ``FilmList.delete()`` keeps the title, the description and the items,
    # so a tombstone built off the wiped face would name nothing while one
    # built off the surviving list still says which list went away.
    if status.status_type == Status.Type.LIST:
        # ``getattr`` rather than a bare attribute access: a LIST status with
        # no backing ``FilmList`` is reachable -- ``test_lists.py`` creates
        # one directly to exercise the film-anchoring carve-out -- and the
        # outbox serializes every one of a user's statuses with no type
        # filter. A malformed row must degrade to the plain Note this code
        # sent before lists existed, not raise and take the whole outbox page
        # down for the user over one bad row.
        film_list = getattr(status, "film_list", None)
        if film_list is not None:
            doc["@context"] = _LIST_CONTEXT
            doc["content"] = list_note_content(film_list)
            doc[LIST_EXTENSION] = list_document(film_list, request)
    return doc


def list_url(film_list) -> str:
    """The list's canonical URL on this instance (L10).

    This is the identifier a receiving ReelTalk stores as the mirror's
    ``remote_url``, so it is minted from the canonical origin rather than
    from the request, exactly like every other published identity.
    """
    return absolute_uri(f"/list/{film_list.pk}/")


def _year_suffix(film) -> str:
    """``" (1951)"`` when there is a year, empty when there is not.

    Split out so the ranked line and the extension agree on what a film
    without a known year looks like rather than each inventing one.
    """
    return f" ({film.year})" if film.year is not None else ""


def list_item_document(item, request) -> dict:
    """One ranked film inside the ``reeltalk:list`` extension.

    Film identity reuses ``film`` / ``tmdbId`` / ``imdbId`` -- the very
    same terms ``film_document`` already publishes -- rather than inventing
    list-local ones. A receiving ReelTalk therefore maps a list item's film
    with the same code it uses for a review's film, which is the difference
    between an importer with one film-resolution path and one with two that
    can drift.

    ``reeltalk:rank`` is namespaced because rank is ours alone: ActivityStreams
    has no ordinal for ``orderedItems``, and a bare ``rank`` would be a term
    nothing can look up.
    """
    film = item.film
    doc = {
        "type": "reeltalk:ListItem",
        "reeltalk:rank": item.rank,
        "name": film.title,
        "film": film_url(request, film),
    }
    if film.year is not None:
        doc["year"] = film.year
    if film.tmdb_id is not None:
        doc["tmdbId"] = film.tmdb_id
    if film.imdb_id:
        doc["imdbId"] = film.imdb_id
    return doc


def list_document(film_list, request) -> dict:
    """The ``reeltalk:list`` extension object (L11).

    The standard AS terms carry what AS actually defines -- ``name`` for the
    title, ``summary`` for the description, ``orderedItems`` for the films --
    so a generic processor reads them without our vocabulary, and only the
    genuinely ReelTalk-specific piece (the rank) needs the prefix.

    ``summary`` is the **rendered HTML** the ``FilmList`` already stores, not
    the markdown source. A mirror gets HTML into ``FilmList.description`` the
    same way a mirrored review gets HTML into ``Status.content``; the
    ``raw_description`` half stays empty on a mirror because the markdown
    source is a fact about the origin member's editing session, not about the
    list.

    The full ranked set rides here even when the human-readable ``content``
    is capped, so the cap costs a peer nothing it needed.
    """
    doc = {
        "type": "reeltalk:List",
        "url": list_url(film_list),
        "name": film_list.title,
        "orderedItems": [
            list_item_document(item, request)
            for item in film_list.items.select_related("film").order_by("rank", "id")
        ],
    }
    if film_list.description:
        doc["summary"] = film_list.description
    return doc


def list_note_content(film_list) -> str:
    """The human-readable body of a list's Note.

    L13 says a Mastodon user sees a post *about* the list rather than the
    list itself, so this is prose with a way back to the real thing -- not an
    attempt to reproduce the list page. The title and the film titles are
    escaped because they are plain text; ``description`` is **not**, because
    it is already sanitized HTML rendered at write time (R18) and escaping
    it again would show a member their own markup.

    The link is always present, and carries the count when the list is too
    long to write out, so a truncated post still says how much there is and
    where the rest lives. No ``request``: the link comes from
    ``list_url``/``absolute_uri``, which mint from ``CANONICAL_ORIGIN``.
    """
    parts = [f"<p><strong>{escape(film_list.title)}</strong></p>"]
    if film_list.description:
        parts.append(film_list.description)

    items = list(film_list.items.select_related("film").order_by("rank", "id"))
    if items:
        rows = "".join(
            f"<li>{escape(item.film.title)}{_year_suffix(item.film)}</li>"
            for item in items[:LIST_CONTENT_FILM_LIMIT]
        )
        parts.append(f"<ol>{rows}</ol>")

    url = list_url(film_list)
    remaining = len(items) - LIST_CONTENT_FILM_LIMIT
    if remaining > 0:
        parts.append(
            f"<p>Showing the first {LIST_CONTENT_FILM_LIMIT} of {len(items)} films. "
            f'The full list: <a href="{url}">{url}</a></p>'
        )
    elif items:
        parts.append(f'<p>The full list: <a href="{url}">{url}</a></p>')
    else:
        parts.append(f'<p>No films in this list yet: <a href="{url}">{url}</a></p>')
    return "".join(parts)


def create_activity(status, user, request) -> dict:
    """A ``Create`` activity wrapping a status's Note — one outbox item (R41).

    The activity id is stable (keyed on the status's origin identity) so
    receiving instances can dedup deliveries; the object rides inline.

    The activity carries the same ``to``/``cc`` the wrapped Note does, which
    is how the peer's ``CreateNoteSerializer`` is built. Both levels are
    populated because receivers are not uniform: the peer reads the object
    first and falls back to the activity, so either alone works there, but a
    receiver that looks only at the envelope must still find an audience.

    The pair is read off the built Note rather than computed a second time,
    so the two levels cannot disagree and the note's single mention read
    serves both.
    """
    note = note_document(status, request)
    return {
        "id": f"{absolute_uri(outbox_path(user.localname))}"
        f"#activity-{note_local_id(status)}",
        "type": "Create",
        "actor": absolute_uri(actor_path(user.localname)),
        "to": note["to"],
        "cc": note["cc"],
        "object": note,
        "publishedTime": _iso(status.published_date),
    }


def update_activity(status, user, request) -> dict:
    """An ``Update`` activity re-publishing a status's Note (M4 increment 6).

    The activity id is unique per event (uuid fragment): two edits of the same
    status must not collide on it, or the second edit would dedup as a
    redelivery of the first. The object's id stays stable, so applying the
    update is idempotent by origin identity on the receiving side.

    The audience is recomputed on every call from the current mention rows,
    the same way the delivery list is, so an edit that changes who is
    mentioned changes who the post is addressed to.
    """
    note = note_document(status, request)
    return {
        "id": f"{absolute_uri(outbox_path(user.localname))}"
        f"#update-{note_local_id(status)}-{uuid.uuid4().hex}",
        "type": "Update",
        "actor": absolute_uri(actor_path(user.localname)),
        "to": note["to"],
        "cc": note["cc"],
        "object": note,
    }


def delete_activity(status, user, request) -> dict:
    """A ``Delete`` activity for a status (M4 increment 6).

    The id is stable per status — a v0.1 status is deleted at most once (a
    soft-deleted review is replaced by a new row with its own identity). The
    object rides inline so the receiver can key the tombstone by origin id
    without a fetch.

    ``to`` is the public collection and there is deliberately **no** ``cc``,
    matching the peer's ``DeleteNoteSerializer``, which declares ``to`` and
    stops. A tombstone is not addressed to anybody in particular: everyone who
    could see the post needs to learn it is gone, which is what the public
    audience says, and naming a follower collection would imply the removal
    was aimed at those accounts rather than at the world.
    """
    return {
        "id": f"{absolute_uri(outbox_path(user.localname))}"
        f"#delete-{note_local_id(status)}",
        "type": "Delete",
        "actor": absolute_uri(actor_path(user.localname)),
        "to": [PUBLIC_COLLECTION],
        "object": note_document(status, request),
    }


def like_activity(liker, target, request, *, undo: bool) -> dict:
    """A ``Like`` activity, or the ``Undo(Like)`` wrapping one.

    The object is the target's wire URL as a **reference**, not an inline
    Note (``note_reference``): a like says something about someone else's
    post, and the only thing the receiver needs is which post. The actor is
    the liker, who is also the signer — so the receiving instance attributes
    the like to the verified sender, the same posture every inbound handler
    takes (never a self-declared ``actor``).

    Both ids carry a uuid fragment, per the rule ``update_activity`` states:
    an event-shaped activity must not collide with an earlier one of the same
    kind, or the receiver drops it as a redelivery. A like is exactly the
    case that looks safe and is not — like, unlike, re-like is a normal thing
    a member does, and each of those must arrive. Keying the id on the
    (liker, target) pair would make the re-like identical to the first like
    and silently lose it. Keying it on the ``Like`` row's own ``origin_id``
    would work today only because unliking deletes the row, so the re-like
    gets a fresh pk; that is a property of the delete path, not of the
    activity, and the id should not depend on it. A uuid makes the event
    identity unconditional.

    The inner ``Like`` inside the ``Undo`` is built fresh, so it does not
    carry the id of the ``Like`` we sent earlier. That is deliberate and
    matches how ``Undo(Follow)`` already works here: the receiver finds the
    like by (actor, target) — the unique ``(user, status)`` pair R83
    decision 4 put on the table *is* that key — not by resolving an id.

    **It carries no ``to`` and no ``cc``, and that is not an oversight.**
    This is the one builder in the file that stays address-less, and it was
    checked rather than left alone: the peer's own ``LikeSerializer`` emits
    ``id``/``type``/``actor``/``object`` and nothing else. A ``Like`` is not
    timeline content — ``broadcast_like`` delivers it to the post's author
    alone for exactly that reason — so giving it a public audience would
    both contradict that decision and make our favourites show up in public
    timelines on the peer. Adding addressing here would look like adopting
    the house idiom and would actually be the one place we diverge from it.
    """
    actor = absolute_uri(actor_path(liker.localname))
    like = {
        "id": f"{actor}#like-{uuid.uuid4().hex}",
        "type": "Like",
        "actor": actor,
        "object": note_reference(request, target),
    }
    if not undo:
        return like
    return {
        "id": f"{actor}#undo-like-{uuid.uuid4().hex}",
        "type": "Undo",
        "actor": actor,
        "object": like,
    }


def shelf_event_document(user, film, identifier: str, request) -> dict:
    """A **ShelfEvent** object — a user putting a film on one of the D1 shelves.

    The custom type follows D15's precedent (ReelTalk's own wire types). The
    object id is stable per (user, film, shelf): re-shelving after an unshelve
    reuses the same identity, so applying the event stays idempotent by origin
    id. ``film`` carries the referenced film's canonical wire id (the same
    custom field as Note); ``shelf`` is the D1 identifier.
    """
    return {
        "@context": _CONTEXT,
        "id": f"{absolute_uri(actor_path(user.localname))}"
        f"#shelve-{identifier}-{film_local_id(film)}",
        "type": "ShelfEvent",
        "film": film_url(request, film),
        "shelf": identifier,
    }


def shelf_event_activity(user, film, identifier: str, request, *, added: bool) -> dict:
    """A ``Create``/``Delete`` activity wrapping a ShelfEvent (M4 increment 6).

    The activity id is unique per event (uuid fragment): an unshelve followed
    by a re-shelve must not collide on it, or the re-shelve would dedup as a
    redelivery of the original. The object id stays stable (see
    ``shelf_event_document``).
    """
    return {
        "id": f"{absolute_uri(outbox_path(user.localname))}"
        f"#shelve-{identifier}-{film_local_id(film)}-{uuid.uuid4().hex}",
        "type": "Create" if added else "Delete",
        "actor": absolute_uri(actor_path(user.localname)),
        "object": shelf_event_document(user, film, identifier, request),
    }
