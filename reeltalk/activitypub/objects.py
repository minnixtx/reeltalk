"""ActivityPub object wire types (M4 increments 3 and 6; Film per D15).

Serializers for the objects ReelTalk exchanges: the custom **Film** type (D15
— a clean break from book-era wire types), **Note** (statuses: reviews,
ratings, comments), and **ShelfEvent** (a user putting a film on — or taking
it off — one of the D1 shelves; increment 6). Plus the activities that carry
them: ``Create`` / ``Update`` / ``Delete``. Built fresh against the
ActivityPub / ActivityStreams specs (R7); federation targets are ReelTalk
instances and current Mastodon (R39), so no legacy server's extensions are
needed.

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

from .identity import absolute_uri, actor_path, outbox_path

_CONTEXT = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
]


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
    return film.remote_url or absolute_uri(request, f"/film/{film_local_id(film)}/")


def note_url(request, status) -> str:
    return absolute_uri(request, f"/status/{note_local_id(status)}/")


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
        doc["image"] = absolute_uri(request, film.poster.url)
    if film.tmdb_id is not None:
        doc["tmdbId"] = film.tmdb_id
    if film.imdb_id:
        doc["imdbId"] = film.imdb_id
    return doc


def note_document(status, request) -> dict:
    """The **Note** wire document for a status (review / rating / comment).

    v0.1 keeps one ``Note`` type on the wire: a receiving ReelTalk instance
    maps it back by shape — a ``rating`` makes it a review/rating-only entry,
    content without a rating a comment. ``film`` is the referenced film's wire
    id (a custom field); ``inReplyTo`` carries threading. The Note's id is a
    stable URL served at ``/status/<id>/`` (increment 6) — remotes usually
    consume the object inline from the outbox / delivery in v0.1.
    """
    doc = {
        "@context": _CONTEXT,
        "id": note_url(request, status),
        "type": "Note",
        "attributedTo": absolute_uri(request, actor_path(status.user.localname)),
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
        doc["inReplyTo"] = note_url(request, status.reply_parent)
    return doc


def create_activity(status, user, request) -> dict:
    """A ``Create`` activity wrapping a status's Note — one outbox item (R41).

    The activity id is stable (keyed on the status's origin identity) so
    receiving instances can dedup deliveries; the object rides inline.
    """
    return {
        "id": f"{absolute_uri(request, outbox_path(user.localname))}"
        f"#activity-{note_local_id(status)}",
        "type": "Create",
        "actor": absolute_uri(request, actor_path(user.localname)),
        "object": note_document(status, request),
        "publishedTime": _iso(status.published_date),
    }


def update_activity(status, user, request) -> dict:
    """An ``Update`` activity re-publishing a status's Note (M4 increment 6).

    The activity id is unique per event (uuid fragment): two edits of the same
    status must not collide on it, or the second edit would dedup as a
    redelivery of the first. The object's id stays stable, so applying the
    update is idempotent by origin identity on the receiving side.
    """
    return {
        "id": f"{absolute_uri(request, outbox_path(user.localname))}"
        f"#update-{note_local_id(status)}-{uuid.uuid4().hex}",
        "type": "Update",
        "actor": absolute_uri(request, actor_path(user.localname)),
        "object": note_document(status, request),
    }


def delete_activity(status, user, request) -> dict:
    """A ``Delete`` activity for a status (M4 increment 6).

    The id is stable per status — a v0.1 status is deleted at most once (a
    soft-deleted review is replaced by a new row with its own identity). The
    object rides inline so the receiver can key the tombstone by origin id
    without a fetch.
    """
    return {
        "id": f"{absolute_uri(request, outbox_path(user.localname))}"
        f"#delete-{note_local_id(status)}",
        "type": "Delete",
        "actor": absolute_uri(request, actor_path(user.localname)),
        "object": note_document(status, request),
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
        "id": f"{absolute_uri(request, actor_path(user.localname))}"
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
        "id": f"{absolute_uri(request, outbox_path(user.localname))}"
        f"#shelve-{identifier}-{film_local_id(film)}-{uuid.uuid4().hex}",
        "type": "Create" if added else "Delete",
        "actor": absolute_uri(request, actor_path(user.localname)),
        "object": shelf_event_document(user, film, identifier, request),
    }
