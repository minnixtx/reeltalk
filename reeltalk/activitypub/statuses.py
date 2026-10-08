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
duplicate; a freshly created film mirror also downloads the document's poster
when present (R46 — best-effort, never failing the delivery). A **ShelfEvent**
creates or removes the mirrored ``ShelfFilm`` row
on the sender's D1 shelf (mirrors receive their shelves from federation —
R15 — so the first event creates the shelf it names).

**List mirrors (lists increment 7).** A ReelTalk list arrives as a
``Note`` carrying the ``reeltalk:list`` extension (L11), so it stays on
this module's existing ``Create``/``Update`` path rather than needing a new
activity type. The extension is read and the prose is not: ``content`` is a
rendering this instance also generates, so importing from it would couple
the importer to our own markup and break the first time the body is
restyled. A mirrored list is **two rows with two different origin urls** —
the ``Status`` face (``status_type=LIST``, keyed on the Note's id) and the
``FilmList`` it fronts (keyed on the extension's own ``url``) — plus one
``ListItem`` per film that resolves. Films resolve **from identifiers
alone, never by fetching**; see ``_sync_list_items`` for why that is the
whole difference between a list import and fifty HTTP requests.

**Failure semantics.** An unresolvable film reference (a review must be
anchored) raises :class:`RemoteObjectError`: the handler's transaction rolls
back *including* the dedup row, so a transient fetch failure is retryable by
the sender instead of silently dropping content. A **list** item that will
not resolve is dropped and logged instead of raised — the same failure at
fifty-fold would mean one bad film loses the whole list, forever (owner
decision, increment 7). Malformed-but-harmless shapes
(a note with no id, an unknown object type, an unknown shelf identifier) are
ignored gracefully — §3.6: unsupported shapes create nothing and never raise.
"""

import logging
import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlparse

import requests
from django.core.files.base import ContentFile
from django.utils import timezone

from reeltalk import __version__
from reeltalk.core.models import (
    Film,
    Shelf,
    ShelfFilm,
    Status,
    resolve_film_id,
)
from reeltalk.lists.models import FilmList, ListItem
from reeltalk.lists.services import soft_delete_list
from reeltalk.mentions.models import sync_status_mentions
from reeltalk.mentions.notify import record_mentions
from reeltalk.mentions.parser import mentions_from_tags
from reeltalk.notifications.models import Notification, notify

from .identity import reference_url
from .mirrors import fetch_image_bytes, image_storage_name
from .objects import LIST_EXTENSION

logger = logging.getLogger(__name__)

# Same reachability budget as the inbound Person-document fetch (mirrors).
REQUEST_TIMEOUT = 10

# A federated poster is a w500-class image (~100–300 KB); anything bigger is
# treated as hostile or broken and skipped rather than stored.
POSTER_MAX_BYTES = 10 * 1024 * 1024

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
    (R42's posture for mirrors). A freshly created mirror downloads the
    document's ``image`` poster when present (R46) — best-effort, and D7-
    matched rows are never backfilled with one.
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
    _attach_remote_poster(film, doc.get("image"))
    return film


def _attach_remote_poster(film: Film, image_url) -> None:
    """Download and store the poster carried by a remote Film document (R46).

    Best-effort by design — an unsupported scheme, network failure, non-2xx
    status, oversized body, or non-image payload is logged and skipped: the
    mirror row exists without a poster instead of failing the whole delivery.
    Called only for a freshly created mirror; existing mirrors and D7-matched
    local rows are never touched (mirrors stay create-only, R42). The
    defensive download itself is shared with the avatar refresh (M5) in
    ``mirrors.fetch_image_bytes``.
    """
    if not isinstance(image_url, str) or not image_url:
        return
    data = fetch_image_bytes(image_url, POSTER_MAX_BYTES)
    if data is None:
        return
    film.poster.save(
        image_storage_name(image_url, f"mirror-{film.pk}.jpg"),
        ContentFile(data),
        save=True,
    )


def _known_film_for_reference(ref, request) -> "Film | None":
    """The Film we already hold for a reference URL — **no fetch**.

    The two lookups from ``_film_for_reference`` that cost nothing but a
    query, lifted out so a caller can ask the no-network question on its
    own: a URL on this instance resolves to the local row (absorbed ids
    following the MergedFilm chain), any other URL to the mirror we hold of
    it. ``None`` means "not among the rows we have", which here is an answer
    rather than a failure — what a gap costs is the caller's decision, and
    the list importer has a different one from the review path's.
    """
    if not isinstance(ref, str) or not ref:
        return None
    base = ref.split("#", 1)[0]
    parsed = urlparse(base)
    if parsed.netloc.lower() == request.get_host().lower():
        match = _FILM_PATH_RE.match(parsed.path)
        if not match:
            return None
        return Film.objects.filter(pk=resolve_film_id(int(match.group(1)))).first()
    return Film.objects.filter(remote_url=base).first()


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
    film = _known_film_for_reference(ref, request)
    if film is not None:
        return film
    base = ref.split("#", 1)[0]
    parsed = urlparse(base)
    if parsed.netloc.lower() == request.get_host().lower():
        # Ours, and the no-fetch lookup came back empty: either the URL is
        # not a film URL at all, or the row it names is gone.
        if not _FILM_PATH_RE.match(parsed.path):
            raise RemoteObjectError(f"Unrecognized film URL shape: {ref!r}")
        raise RemoteObjectError(f"No local film at {ref!r}")
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


# --- Note references --------------------------------------------------------

_STATUS_PATH_RE = re.compile(r"^/status/(\d+)/?$")


def resolve_status_reference(ref, request) -> "Status | None":
    """Resolve a Note reference (a wire URL) to a Status we already have.

    The inverse of ``objects.note_reference``. A URL on this instance
    resolves to the local row by its origin identity — ``origin_id`` first,
    then the pk, which is exactly the ``origin_id or pk`` rule the URL was
    built from — and any other URL resolves to the mirror we hold of it by
    ``remote_url``.

    **No fetch.** An object we do not already have stays unknown and returns
    ``None``, so processing an activity cannot be made to pull arbitrary
    remote documents by naming them (the same posture as
    ``mirrors.resolve_known_actor``, and unlike ``_film_for_reference``,
    which does fetch because a review cannot be mirrored without its film).
    What a caller does with ``None`` is the graceful-ignore answer of §3.6.
    """
    if not isinstance(ref, str) or not ref:
        return None
    base = ref.split("#", 1)[0]
    if not base:
        return None
    parsed = urlparse(base)
    if parsed.netloc.lower() == request.get_host().lower():
        match = _STATUS_PATH_RE.match(parsed.path)
        if not match:
            return None
        wanted = int(match.group(1))
        return (
            Status.objects.filter(origin_id=wanted).first()
            or Status.objects.filter(pk=wanted).first()
        )
    return Status.objects.filter(local=False, remote_url=base).first()


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

    ``inReplyTo`` is read on ingest (increment 6), so a remote conversation
    arrives as a thread instead of a pile of top-level notes. The parent is
    resolved among rows we already have — the same no-fetch posture the
    ``Like`` handler keeps — which means a reply whose parent has not reached
    us lands flat. That is exactly what every remote note did before this
    field was read at all, so an unresolved parent is never worse than the
    status quo, and a resolved one is strictly better.
    """
    home_url = note.get("id")
    if not isinstance(home_url, str) or not home_url:
        # Without an id there is no origin identity to key on — ignore.
        return None
    parent = resolve_status_reference(reference_url(note.get("inReplyTo")), request)
    content = note.get("content") or ""
    list_ext = note.get(LIST_EXTENSION)
    if list_ext is not None:
        # L11: this Note is a list's post face. The extension is
        # authoritative about that, **ahead of** the shape mapping below —
        # which for a film-less note with content yields ``None``, and a
        # list face stored as ``status_type=None`` is precisely the
        # shapeless status this increment was written to stop producing:
        # the feed row, the ``film_list`` reverse lookup and the
        # ``/status/ → /list/`` redirect all key off the type. The branch
        # also resolves no film. A list is not anchored to one film (L13),
        # so a ``film`` key on a list Note is noise, and chasing it would
        # put a network fetch on the path for something the list cannot
        # use — and a ``rating`` on a list's face means nothing, so it is
        # dropped rather than stored on a row no review surface reads.
        film = None
        rating_raw = None
        status_type = Status.Type.LIST
    else:
        film_ref = note.get("film")
        if film_ref:
            film = _film_for_reference(film_ref, request)
        elif parent is not None:
            # A reply inherits the film of the turn it answers, exactly as
            # ``add_reply`` does for a locally composed one. It is what keeps a
            # remote conversation attached to the film it is about instead of
            # dropping off every film-anchored surface while looking perfectly
            # fine on its own post page — and it is what lets a threaded remote
            # review have the anchor ``Status.save`` demands.
            film = parent.film
        else:
            film = None
        rating_raw = note.get("rating")
        if rating_raw is not None:
            if film is None:
                raise RemoteObjectError("A review requires a resolvable film reference")
            status_type = Status.Type.REVIEW if content else Status.Type.REVIEW_RATING
        elif content:
            status_type = Status.Type.COMMENT if film is not None else None
        else:
            # An empty note mirrors nothing. A list note never lands here:
            # increment 6 always composes a body — an empty list says "No
            # films in this list yet" — and even a peer that sent us a
            # content-less one still declared a face, which is what the
            # extension is for.
            return None

    # Mentions are read off the wire here — this function ignored ``note["tag"]``
    # entirely until increment 4. They must be read from the tag array rather
    # than re-parsed from ``content``, because the wire carries rendered HTML
    # and a mirror keeps ``raw_content=""``: the typed text a parser would
    # need does not exist on this side. Resolution is existing-rows-only (M-b),
    # so an unknown href is dropped rather than chased — the same no-fetch
    # posture as the ``inReplyTo`` above (R89).
    mentioned = mentions_from_tags(note.get("tag"), request)

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
        if list_ext is not None:
            # ``Update`` has to reach the ``FilmList`` and must not stop at
            # the face. A rename, a description change, a film added or
            # removed, and a reorder all arrive as ``Update``; if only the
            # mirrored prose moved, the list page and its own post face would
            # disagree permanently, and the disagreement would be invisible
            # until somebody put the two side by side.
            _mirror_list(sender, list_ext, existing, request)
        # A remote edit can newly mention someone, and M-e says that
        # notifies. This is the one place the standing "the update branch
        # never notifies" rule looks like it is being broken, and it is not:
        # that rule is about the **reply** kind — the ``notify(Kind.REPLY)``
        # further down stays create-only, because an edit to a reply nobody
        # asked about is not a fresh "replied to you". The mention kind gets
        # its own guard inside ``record_mentions``, keyed on
        # ``(recipient, status)``, so ten edits naming the same member write
        # one row. The rule and the guard are different mechanisms for
        # different kinds; neither is loosened here.
        sync_status_mentions(existing, mentioned)
        record_mentions(existing, mentioned)
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
        reply_parent=parent,
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
    if list_ext is not None:
        # The ``Create`` half of the same contract the ``Update`` branch
        # above keeps: the list row is made together with its face, not left
        # to a later ``Update`` that may never arrive.
        _mirror_list(sender, list_ext, status, request)
    # The inbound mention producer (mentions increment 4). ``status.user``
    # is the verified sender, so the mention is attributed to who actually
    # signed the activity rather than to the note's self-declared
    # ``attributedTo`` — the same attribution rule as every other mirror
    # write. M-c is exercised through the status: when a remote member
    # replies to one of our posts *and* @mentions its author, the reply row
    # below already covers them and ``record_mentions`` drops them here.
    sync_status_mentions(status, mentioned)
    record_mentions(status, mentioned)
    # A remote reply to one of our posts (notifications increment 2, R92):
    # the federated half of the reply pair. The create branch only — this
    # function also serves ``Update`` of an existing mirror, and an edit to
    # a reply nobody asked about is not a fresh "replied to you". The
    # ``parent is not None`` test is what keeps a plain top-level note from
    # notifying anyone: with no parent there is no one the note answered.
    if parent is not None:
        notify(parent.user, sender, Notification.Kind.REPLY, status)
    return status


# --- List mirrors (lists increment 7, L11 / L12) ---------------------------

# ``FilmList.title`` is ``varchar(200)``. A peer sending a longer name is a
# DatabaseError rather than a validation error, and a DatabaseError inside an
# inbox handler means the sender retries into the same wall forever. Clamping
# at the field's own limit keeps the import possible; read off the field
# rather than hard-coded so the two cannot drift apart.
_LIST_TITLE_MAX_LEN = FilmList._meta.get_field("title").max_length


def _list_rank(raw, position: int) -> int:
    """An item's rank, falling back to its position in the array.

    ``reeltalk:rank`` is what we publish and what a ReelTalk peer sends, but
    the inbox contract is not to raise on an unfamiliar shape. A missing,
    non-integer or non-positive rank falls back to the item's 1-based position
    in ``orderedItems`` — which is not inventing an ordering so much as
    reading the one the array itself asserts. Ranks are allowed to collide
    (there is deliberately no unique constraint on ``(film_list, rank)``),
    so a mixed batch stays storable.

    ``bool`` is excluded on purpose: ``isinstance(True, int)`` is True in
    Python, and a wire ``true`` is not rank one.
    """
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return position
    return raw


def _resolve_list_item(raw, request) -> "Film | None":
    """One ``orderedItems`` entry to a Film we already have — no fetching.

    Two steps, both pure database reads, tried in the order ``_mirror_film``
    itself uses: the entry's own ``film`` url first (most precise — if we
    mirror that exact row, that is the film), then D7's identifier match on
    ``tmdbId`` / ``imdbId`` / title+year, which is exactly why increment 6
    put those ids on every item.
    """
    if not isinstance(raw, dict):
        return None
    film = _known_film_for_reference(raw.get("film"), request)
    if film is not None:
        return film
    tmdb = raw.get("tmdbId")
    imdb = raw.get("imdbId")
    title = raw.get("name")
    year = raw.get("year")
    return Film.find_match(
        tmdb_id=tmdb if isinstance(tmdb, int) and not isinstance(tmdb, bool) else None,
        imdb_id=imdb if isinstance(imdb, str) and imdb else None,
        title=title if isinstance(title, str) and title else None,
        year=year if isinstance(year, int) and not isinstance(year, bool) else None,
    )


def _sync_list_items(film_list: FilmList, ext: dict, request) -> None:
    """Make the mirror's ranked rows match the wire exactly. Diff, not append.

    **This is where the fan-out is refused.** A list of N films we have never
    seen would be N synchronous GETs inside one inbound POST if each item
    went through ``_film_for_reference``, and one 404 would roll back the
    whole list. So the list path resolves only against rows already here
    (``_resolve_list_item``) and **never opens a connection**. Films we
    already have — most of them, on any instance that has imported a
    watchlist — cost zero fetches; films we do not have are dropped and
    logged, and the rest of the list lands. That is the owner's decision for
    this increment, and it is the opposite of the single-film answer the
    review path makes, where a review without its film is no review at all.

    Diffing rather than appending is what makes an ``Update`` correct: a
    remote rename keeps the row, a removed film's row is **deleted** (not
    zero-ranked), a reorder rewrites the ranks that arrived, and a film added
    since the last delivery appears. Re-running the same payload writes
    nothing, which is the idempotence the inbox contract asks for.

    A missing or non-list ``orderedItems`` leaves the rows **untouched**
    rather than clearing them. That is a different fact from an empty array,
    which is a deliberately emptied list and does clear them: treating a
    malformed partial update as "emptied" would let a broken peer destroy a
    mirror it meant to refresh.
    """
    raw_items = ext.get("orderedItems")
    if not isinstance(raw_items, list):
        if "orderedItems" in ext:
            logger.info(
                "List mirror %s: orderedItems is not an array; rows left alone",
                film_list.remote_url,
            )
        return

    wanted: dict[int, int] = {}
    dropped: list[str] = []
    for position, raw in enumerate(raw_items, start=1):
        film = _resolve_list_item(raw, request)
        if film is None:
            name = raw.get("name") if isinstance(raw, dict) else None
            dropped.append(f"#{position} {name or '(unidentifiable)'}")
            continue
        if film.pk in wanted:
            # Two entries resolving to the same Film — duplicate tmdb ids, or
            # a peer whose own list has a dup. ``one_row_per_film_per_list``
            # says only one of them can land, so the first wins and the
            # collision is reported rather than discovered as an
            # IntegrityError in production.
            dropped.append(f"#{position} (duplicate of {film.title})")
            continue
        wanted[film.pk] = _list_rank(
            raw.get("reeltalk:rank") if isinstance(raw, dict) else None, position
        )

    existing = {
        item.film_id: item for item in ListItem.objects.filter(film_list=film_list)
    }
    for film_id, rank in wanted.items():
        item = existing.pop(film_id, None)
        if item is None:
            ListItem.objects.create(film_list=film_list, film_id=film_id, rank=rank)
        elif item.rank != rank:
            ListItem.objects.filter(pk=item.pk).update(rank=rank)
    if existing:
        ListItem.objects.filter(pk__in=[item.pk for item in existing.values()]).delete()

    if dropped:
        logger.info(
            "List mirror %s: dropped %d unresolved item(s): %s",
            film_list.remote_url,
            len(dropped),
            ", ".join(dropped),
        )


def _apply_list_fields(film_list: FilmList, *, title: str, description: str) -> None:
    """Apply an extension's title and description onto an existing mirror."""
    update_fields = []
    if film_list.title != title:
        film_list.title = title
        update_fields.append("title")
    if film_list.description != description:
        film_list.description = description
        update_fields.append("description")
    if update_fields:
        update_fields.append("updated_date")
        film_list.save(update_fields=update_fields)


def _mirror_list(sender, ext, face: Status, request) -> "FilmList | None":
    """Build or refresh the ``FilmList`` behind a mirrored list face.

    Two rows, two different origin urls: the caller owns the ``Status`` face,
    keyed on the Note's id; this owns the ``FilmList``, keyed on the
    extension's ``url`` under ``unique_remote_url_for_list_mirrors`` — the
    same partial-unique discipline ``Film`` and ``Status`` use, so the
    database holds the idempotency line rather than this function.

    ``raw_description`` stays empty on a mirror. The markdown source is a
    fact about the origin member's editing session, not about the list, and
    ``summary`` arrives already rendered — the same split a mirrored review's
    ``raw_content=""`` makes (R18).

    ``origin_id`` is deliberately not stamped. ``FilmList.save()`` sets it
    only for a local list; a mirror claims no local origin, the same posture
    every other mirror row takes.

    Returns the mirror row, or ``None`` when the extension carried nothing a
    list can be built from — no ``url`` to key identity on, or no ``name``
    to call it by. The face is still a ``LIST`` status either way: the
    extension's *presence* is the peer's declaration of what the Note is,
    so a half-broken extension yields a list face with no list behind it,
    which is the degraded state increment 6 already handles on the way out.
    """
    if not isinstance(ext, dict):
        return None
    home_url = ext.get("url")
    if not isinstance(home_url, str) or not home_url:
        logger.info("List extension carries no url; no list mirrored")
        return None
    title = ext.get("name")
    if not isinstance(title, str) or not title.strip():
        logger.info("List extension at %s carries no name; no list mirrored", home_url)
        return None
    description = ext.get("summary")
    if not isinstance(description, str):
        description = ""

    film_list = FilmList.objects.filter(remote_url=home_url).first()
    if film_list is not None:
        if film_list.status_id != face.pk:
            # The extension names a list we already mirror under a different
            # face. Re-pointing it would take that face's list away, and a
            # second row for the same remote url is impossible under the
            # partial unique. Neither is a call this instance gets to make
            # about somebody else's object, so the mirror stays as it is.
            logger.info(
                "List url %s already mirrored under face %s, not %s; left alone",
                home_url,
                film_list.status_id,
                face.pk,
            )
            return None

    if film_list is None:
        face_holder = FilmList.objects.filter(status=face).first()
        if face_holder is not None:
            # The face already fronts a list. Same reasoning as above, read
            # from the other side — and the guard that keeps this from being a
            # OneToOne IntegrityError over a peer that reuses a face url.
            logger.info(
                "Face %s already fronts list %s; extension url %s ignored",
                face.pk,
                face_holder.pk,
                home_url,
            )
            return face_holder
        film_list = FilmList(
            user=sender,
            status=face,
            local=False,
            remote_url=home_url,
            remote_id=_remote_id_from_url(home_url),
            title=title[:_LIST_TITLE_MAX_LEN],
            description=description,
            raw_description="",
        )
        film_list.save()
    else:
        _apply_list_fields(
            film_list, title=title[:_LIST_TITLE_MAX_LEN], description=description
        )

    _sync_list_items(film_list, ext, request)
    return film_list


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


def _tombstone_mirror(status: Status) -> None:
    """Take a mirrored status down, and its list with it if it fronts one.

    Until increment 7 this handler knew about the ``Status`` alone, so a
    remote list deletion tombstoned the face and left the ``FilmList``
    **live**: ``/list/<pk>/`` kept rendering a list whose post was gone, its
    Save pointers still resolved, and nothing ever told a saver the list had
    been deleted. R140 1's dismissible deleted-notice is exactly the thing
    that should fire there, and it cannot fire on a row nobody marked deleted.

    ``soft_delete_list`` is the same call the local ``POST /list/<id>/delete/``
    route makes, so both halves go down together here exactly as they do
    there, and the two paths cannot drift. It is idempotent on both rows, so
    a redelivered ``Delete`` still changes nothing.
    """
    film_list = getattr(status, "film_list", None)
    if film_list is not None:
        soft_delete_list(film_list)
        return
    status.delete()


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
                # A list's ``FilmList`` goes down with it — see
                # ``_tombstone_mirror``.
                _tombstone_mirror(status)
    elif obj_type == "ShelfEvent":
        _apply_shelf_event(sender, obj, request, added=False)
