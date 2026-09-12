"""Status + shelf-event federation, inbound side (M4 increment 6).

The ``Create`` / ``Update`` / ``Delete`` handlers registered in
``inbox.HANDLERS``: a remote user's reviews and shelve events become local
mirror rows. Built fresh against the ActivityPub / ActivityStreams specs
(R7); federation targets are ReelTalk instances and current Mastodon (R39).

**Attribution.** The pipeline resolves and verifies the sender from the
signature's keyid before dispatch, so every mirror row is attributed to the
*verified sender* — never to a self-declared ``actor`` / ``attributedTo``
field, which an attacker could forge (the signature is the authority; the
same posture as the follow handlers).

**Idempotency, keyed by origin id.** The activity-wire-id dedup
(``DeliveredActivity``) catches exact redeliveries; object-level idempotency
catches everything else — an Update that arrives before its Create, or a
second activity referencing the same object. Mirrors are keyed on the object's
home-instance URL stored as received (``remote_url``; R42: the integer
``remote_id`` alone cannot reconstruct a host), and shelf events converge on
the single ``ShelfFilm`` row per (sender, film, shelf). Applying any of these
activities twice changes nothing.

**Object mapping.** A **Note** maps back by shape — a ``rating`` makes it a
review (content present) or rating-only entry; content without a rating is a
comment (a standalone note when it carries no film reference). A review
implies the film sits on its author's Watched shelf (D3: a rating is required
to mark watched), so mirroring one also mirrors that shelf row — the feed
then renders it as a "watched" entry with stars (R35) instead of a standalone
note. A **Film** document runs D7's find-match first, so the same movie stays
one row on this instance and a remote review anchors to our row rather than a
duplicate. A **ShelfEvent** creates or removes the mirrored ``ShelfFilm`` row
on the sender's D1 shelf (mirrors receive their shelves from federation —
R15 — so the first event creates the shelf it names).

**Failure semantics.** An unresolvable film reference (a review must be
anchored) raises :class:`RemoteObjectError`: the handler's transaction rolls
back *including* the dedup row, so a transient fetch failure is retryable by
the sender instead of silently dropping content. Malformed-but-harmless shapes
(a note with no id, an unknown object type, an unknown shelf identifier) are
ignored gracefully — §3.6: unsupported shapes create nothing and never raise.
"""

import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlparse

import requests
from django.utils import timezone

from reeltalk import __version__
from reeltalk.core.models import (
    Film,
    Shelf,
    ShelfFilm,
    Status,
    resolve_film_id,
)

# Same reachability budget as the inbound Person-document fetch (mirrors).
REQUEST_TIMEOUT = 10

# The D1 shelf identifiers a ShelfEvent may name (the only shelves v0.1 has).
_DEFAULT_SHELF_NAMES = {identifier: name for identifier, name in Shelf.DEFAULT_SHELVES}


class RemoteObjectError(Exception):
    """A referenced remote object could not be fetched or resolved.

    Raised inside a handler so the activity's transaction rolls back (dedup
    row included) and the sender's retry is not blocked by its own failed
    delivery — a transient failure stays retryable instead of dropping
    content.
    """


def _remote_id_from_url(url: str):
    """The integer id in a home object URL's path, when it carries one.

    ReelTalk ids are ``/film/<id>/`` / ``/status/<id>/``; other shapes leave
    ``remote_id`` unset — the full ``remote_url`` remains the authoritative
    identity either way.
    """
    segments = [segment for segment in urlparse(url).path.split("/") if segment]
    if segments and segments[-1].isdigit():
        return int(segments[-1])
    return None


def _parse_time(value):
    """An RFC 3339 timestamp from the wire, or None when absent/unusable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_list(value):
    """A wire array field as a Python list (non-lists read as empty)."""
    return list(value) if isinstance(value, list) else []


# --- Film references ---------------------------------------------------------


_FILM_PATH_RE = re.compile(r"^/film/(\d+)/?$")


def _fetch_film_document(url: str) -> dict:
    """GET a remote film's wire document (public — no signature needed).

    Raises :class:`RemoteObjectError` on any failure: unsupported scheme,
    network error, non-2xx status, non-JSON body, or a document that is not a
    usable Film (wrong type, missing id or name).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RemoteObjectError(f"Unsupported film URL scheme: {parsed.scheme!r}")
    try:
        resp = requests.get(
            url,
            headers={
                "Accept": "application/activity+json",
                # Identify the software so remote instances can filter by it.
                "User-Agent": f"reeltalk/{__version__}",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as err:
        raise RemoteObjectError(f"Could not reach {url}") from err
    if resp.status_code == 404:
        raise RemoteObjectError(f"No film at {url}")
    if not resp.ok:
        raise RemoteObjectError(f"Film fetch failed (HTTP {resp.status_code})")
    try:
        doc = resp.json()
    except ValueError as err:
        raise RemoteObjectError("Film document is not JSON") from err
    if not isinstance(doc, dict) or not doc.get("id"):
        raise RemoteObjectError("Not a usable Film document")
    doc_type = doc.get("type")
    types = [doc_type] if isinstance(doc_type, str) else list(doc_type or [])
    if "Film" not in types:
        raise RemoteObjectError(f"Not a Film document (type {doc_type!r})")
    if not doc.get("name"):
        raise RemoteObjectError("Film document carries no name")
    return doc


def _mirror_film(doc: dict) -> Film:
    """Create (or return the existing) mirror for a remote Film document.

    Idempotent by origin id: a row already carrying the same home URL is
    returned untouched. A D7 match against any existing row (local or mirror)
    wins over creating one — the same movie stays one row on this instance,
    so a remote review of it anchors to our row instead of a duplicate.
    Mirrors are create-only: metadata is not refreshed on later deliveries
    (R42's posture for mirrors).
    """
    home_url = doc["id"]
    existing = Film.objects.filter(remote_url=home_url).first()
    if existing is not None:
        return existing
    year = doc.get("year")
    runtime = doc.get("runtime")
    match = Film.find_match(
        tmdb_id=doc.get("tmdbId") if isinstance(doc.get("tmdbId"), int) else None,
        imdb_id=doc.get("imdbId") or None,
        title=doc.get("name"),
        year=year if isinstance(year, int) else None,
    )
    if match is not None:
        return match
    film = Film(
        title=doc["name"],
        subtitle=doc.get("subtitle") or "",
        description=doc.get("summary") or "",
        year=year if isinstance(year, int) else None,
        runtime=runtime if isinstance(runtime, int) else None,
        genres=_as_list(doc.get("genres")),
        directors=_as_list(doc.get("directors")),
        cast=_as_list(doc.get("cast")),
        tmdb_id=doc.get("tmdbId") if isinstance(doc.get("tmdbId"), int) else None,
        imdb_id=doc.get("imdbId") or "",
        remote_url=home_url,
        remote_id=_remote_id_from_url(home_url),
    )
    film.save()
    return film


def _film_for_reference(ref, request) -> Film:
    """Resolve a Note/ShelfEvent ``film`` reference to a local Film row.

    A URL on this instance (netloc compared against the request's host, so it
    works behind the operator proxy — D14) resolves to its local row, absorbed
    ids following the MergedFilm chain; an existing mirror is found by its
    stored home URL; otherwise the Film document is fetched from the URL and
    mirrored. Raises :class:`RemoteObjectError` when the reference cannot be
    resolved — the caller's activity processing rolls back so a transient
    failure can be retried.
    """
    if not isinstance(ref, str) or not ref:
        raise RemoteObjectError(f"Unusable film reference: {ref!r}")
    base = ref.split("#", 1)[0]
    parsed = urlparse(base)
    if parsed.netloc.lower() == request.get_host().lower():
        match = _FILM_PATH_RE.match(parsed.path)
        if not match:
            raise RemoteObjectError(f"Unrecognized film URL shape: {ref!r}")
        film = Film.objects.filter(pk=resolve_film_id(int(match.group(1)))).first()
        if film is None:
            raise RemoteObjectError(f"No local film at {ref!r}")
        return film
    mirror = Film.objects.filter(remote_url=base).first()
    if mirror is not None:
        return mirror
    return _mirror_film(_fetch_film_document(base))


# --- Shelves (mirrors receive theirs from federation — R15) ------------------


def _ensure_default_shelf(user, identifier: str) -> Shelf:
    """The user's D1 shelf with this identifier, created on first need.

    Remote mirrors get no shelves at creation (R15 — local users only); their
    shelves arrive from federation, so the first mirrored event naming a shelf
    creates that shelf row.
    """
    shelf, _ = Shelf.objects.get_or_create(
        user=user,
        identifier=identifier,
        defaults={"name": _DEFAULT_SHELF_NAMES[identifier]},
    )
    return shelf


def _ensure_shelf_row(user, identifier: str, film: Film) -> None:
    """The (film, shelf) row for a mirrored user — idempotent."""
    shelf = _ensure_default_shelf(user, identifier)
    ShelfFilm.objects.get_or_create(shelf=shelf, film=film, defaults={"user": user})


def _apply_shelf_event(sender, event: dict, request, *, added: bool) -> None:
    """Mirror a remote user's shelf membership change (idempotent).

    The effect is keyed by (verified sender, film, shelf identifier) — the
    origin identity of the event — so redeliveries and out-of-order
    deliveries converge on the same ``ShelfFilm`` row. An unknown shelf
    identifier (not one of the D1 pair) or a missing film reference is
    ignored gracefully.
    """
    identifier = event.get("shelf")
    if identifier not in _DEFAULT_SHELF_NAMES:
        return
    film_ref = event.get("film")
    if not film_ref:
        return
    film = _film_for_reference(film_ref, request)
    shelf = _ensure_default_shelf(sender, identifier)
    if added:
        ShelfFilm.objects.get_or_create(
            shelf=shelf, film=film, defaults={"user": sender}
        )
    else:
        ShelfFilm.objects.filter(shelf=shelf, film=film).delete()


# --- Status mirrors ----------------------------------------------------------


def _apply_note_fields(
    status: Status, *, content: str, rating_raw, status_type, edited
) -> None:
    """Apply a Note document's fields onto an existing mirror (idempotent)."""
    update_fields = []
    if status.content != content:
        status.content = content
        update_fields.append("content")
    new_rating = Decimal(str(rating_raw)) if rating_raw is not None else None
    if status.rating != new_rating:
        status.rating = new_rating
        update_fields.append("rating")
    if status.status_type != status_type:
        status.status_type = status_type
        update_fields.append("status_type")
    if status.edited_date != edited:
        status.edited_date = edited
        update_fields.append("edited_date")
    if update_fields:
        status.save(update_fields=update_fields)


def _mirror_status(sender, note: dict, request):
    """Create or update the mirror for a remote Note (idempotent by origin id).

    The mapping is by shape: a ``rating`` makes it a review (content present)
    or rating-only entry; content without a rating is a comment — standalone
    when the note carries no film reference. A review must be anchored, so an
    unresolvable film reference raises :class:`RemoteObjectError` (the
    activity rolls back and stays retryable). ``attributedTo`` on the wire is
    ignored — the row belongs to the verified sender.
    """
    home_url = note.get("id")
    if not isinstance(home_url, str) or not home_url:
        # Without an id there is no origin identity to key on — ignore.
        return None
    film_ref = note.get("film")
    film = _film_for_reference(film_ref, request) if film_ref else None
    rating_raw = note.get("rating")
    content = note.get("content") or ""
    if rating_raw is not None:
        if film is None:
            raise RemoteObjectError("A review requires a resolvable film reference")
        status_type = Status.Type.REVIEW if content else Status.Type.REVIEW_RATING
    elif content:
        status_type = Status.Type.COMMENT if film is not None else None
    else:
        # An empty note mirrors nothing.
        return None

    existing = Status.objects.filter(local=False, remote_url=home_url).first()
    if existing is not None:
        if existing.deleted:
            # A tombstone stays (v0.1 has no undelete path).
            return existing
        _apply_note_fields(
            existing,
            content=content,
            rating_raw=rating_raw,
            status_type=status_type,
            edited=_parse_time(note.get("editedTime")),
        )
        if existing.is_review and existing.film_id is not None:
            _ensure_shelf_row(sender, Shelf.READ, existing.film)
        return existing

    status = Status(
        user=sender,
        film=film,
        status_type=status_type,
        content=content,
        # The wire carries rendered HTML; there is no markdown source.
        raw_content="",
        rating=Decimal(str(rating_raw)) if rating_raw is not None else None,
        published_date=_parse_time(note.get("publishedTime")) or timezone.now(),
        edited_date=_parse_time(note.get("editedTime")),
        local=False,
        remote_url=home_url,
        remote_id=_remote_id_from_url(home_url),
    )
    status.save()
    if status.is_review:
        # A review implies the film is on its author's Watched shelf (D3);
        # mirror that row so the feed renders the event as "watched" with
        # stars (R35) instead of a standalone note.
        _ensure_shelf_row(sender, Shelf.READ, film)
    return status


# --- Inbound handlers (registered in inbox.HANDLERS) -------------------------


def _object_of(activity: dict):
    """The activity's object when it is an inline dict, else None."""
    obj = activity.get("object")
    return obj if isinstance(obj, dict) else None


def _mirror_object(sender, obj: dict, request) -> None:
    """Mirror one created/updated object by type; unknown types ignored (§3.6)."""
    obj_type = obj.get("type")
    if obj_type == "Note":
        _mirror_status(sender, obj, request)
    elif obj_type == "Film":
        _mirror_film(obj)
    elif obj_type == "ShelfEvent":
        _apply_shelf_event(sender, obj, request, added=True)


def handle_create(activity, sender, request):
    """A remote user created an object we mirror: Note, Film, or ShelfEvent.

    The object is attributed to the verified sender (see module docstring).
    Object types this instance does not mirror are ignored gracefully — §3.6:
    a remote sending shapes we do not handle must not crash or create content.
    """
    obj = _object_of(activity)
    if obj is None:
        return
    _mirror_object(sender, obj, request)


def handle_update(activity, sender, request):
    """A remote user updated a Note or Film we mirror (idempotent by origin id).

    An Update that arrives before its Create (out-of-order delivery) creates
    the mirror — the same mapping applies either way. Shelf events carry no
    update in v0.1; any other shape is ignored gracefully.
    """
    obj = _object_of(activity)
    if obj is None:
        return
    obj_type = obj.get("type")
    if obj_type == "Note":
        _mirror_status(sender, obj, request)
    elif obj_type == "Film":
        _mirror_film(obj)


def handle_delete(activity, sender, request):
    """A remote user deleted a Note or shelf event we mirror.

    A Note becomes a soft-delete tombstone (identity intact — §3.2); a
    ShelfEvent removes the mirrored ``ShelfFilm`` row. Film deletions are
    ignored gracefully in v0.1 (film rows are PROTECTed while referenced;
    removal is a merge/absorb concern). Redeliveries are no-ops: deleting an
    already-deleted status or a missing shelf row changes nothing.
    """
    obj = _object_of(activity)
    if obj is None:
        return
    obj_type = obj.get("type")
    if obj_type == "Note":
        home_url = obj.get("id")
        if isinstance(home_url, str) and home_url:
            status = Status.objects.filter(local=False, remote_url=home_url).first()
            if status is not None:
                # Soft-delete; a no-op when the row is already a tombstone.
                status.delete()
    elif obj_type == "ShelfEvent":
        _apply_shelf_event(sender, obj, request, added=False)
