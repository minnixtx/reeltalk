"""ActivityPub object wire types (M4 increment 3, R41; Film per D15).

Serializers for the objects ReelTalk exchanges: the custom **Film** type (D15
— a clean break from book-era wire types) and **Note** (statuses: reviews,
ratings, comments), plus the ``Create`` activity that wraps a Note in an
outbox. Built fresh against the ActivityPub / ActivityStreams specs (R7);
federation targets are ReelTalk instances and current Mastodon (R39), so no
legacy server's extensions are needed.

Object ids use the day-one origin identity fields: a locally created object's
wire id is its ``origin_id`` (backfilled to the row's pk — see migration
``core/0006`` / ``social`` save paths), falling back to the pk while unset.
Reconstructing a *remote* object's home-instance URL from ``remote_id`` lands
with remote mirrors (increment 4); v0.1 serializes local objects only.
"""

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
    return absolute_uri(request, f"/film/{film_local_id(film)}/")


def note_url(request, status) -> str:
    return absolute_uri(request, f"/status/{note_local_id(status)}/")


def film_document(film, request) -> dict:
    """The **Film** wire document (D15).

    ``tmdbId``/``imdbId`` are carried so receiving ReelTalk instances can dedup
    per D7. Empty/absent fields are omitted rather than null. A remote film's
    home-instance id is not reconstructed here (increment 4); v0.1 serializes
    local films, whose id resolves to this instance's ``/film/<id>/``.
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
    stable URL; the fetch endpoint for it lands with broadcasting (increment 6)
    — remotes consume the object inline from the outbox / delivery in v0.1.
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
