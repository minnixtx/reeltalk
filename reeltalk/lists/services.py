"""The write path for lists (§2K increments 1 and 3).

Every mutation of a list goes through one of these functions, and nothing else
writes these tables. That is not ceremony: L1 makes a list freely editable
forever, which means the interesting cases are all *repeated* edits, and a
rank or a face maintained in two places is where a feature like this rots. Each
function is small, atomic, and idempotent where the door that calls it can be
pressed twice.

Every function that changes something also stamps the post face's
``edited_date`` — see ``_stamp_edited`` for why that belongs on the face rather
than only on ``FilmList.updated_date``, and why a call that changed nothing
must not stamp.

No view, URL or template is here — increment 1 has no visual surface on
purpose (§2K: the moment ``Status.Type.LIST`` exists, list rows are already in
every follower's timeline, so the design has to arrive before that is
visible).
"""

from collections.abc import Iterable

from django.db import transaction
from django.utils import timezone

from reeltalk.core.models import Film, Status
from reeltalk.core.utils import render_markdown
from reeltalk.lists.models import FilmList, ListItem, ListSave

# The two directions ``move`` understands. Named because a typo in a bare
# string literal at a call site would otherwise read as a third, silent one.
MOVE_UP = "up"
MOVE_DOWN = "down"


@transaction.atomic
def create_list(
    user,
    *,
    title: str,
    description: str = "",
    films: Iterable[Film] = (),
) -> FilmList:
    """Make a list: its row, its post face, and optionally its first films.

    The face is created **first** because ``FilmList.status`` is required (L9
    makes the face part of what a list is, not an accessory to it). It is a
    ``LIST`` status with no film, which is exactly what the narrowed anchoring
    rule in ``Status.save`` permits and nothing else does.

    ``description`` is the markdown **source**. It is rendered here and both
    halves are written together, so no caller can store a description without
    its source or leave the pair out of step — the reason ``Film`` carries
    ``raw_description`` at all (R18).

    The face gets no content: see ``FilmList`` for why the list's text lives in
    exactly one place.
    """
    face = Status.objects.create(user=user, status_type=Status.Type.LIST)
    film_list = FilmList.objects.create(
        user=user,
        title=title,
        status=face,
        description=render_markdown(description),
        raw_description=description,
    )
    # ``_append_films`` rather than ``add_films``: a list born with films is
    # not a list that was *edited*, and a face stamped at creation would go
    # out on the wire (increment 6) reporting an edit that never happened.
    _append_films(film_list, films)
    return film_list


def _stamp_edited(film_list: FilmList) -> None:
    """Mark the list's post face as edited.

    ``Status.edited_date`` is the field the wire reports as ``editedTime``
    (increment 6) and the one a reader can be shown as "edited", so the stamp
    goes on the face and not only on ``FilmList.updated_date`` — the face is
    the half other instances and the feed actually see, and a list whose text
    changed while its face stayed unmarked would broadcast as untouched.

    Callers stamp **only when the call changed something**. ``add_films`` with
    everything already present, ``remove_film`` on a film that is not in the
    list, and ``move`` at either end all return without stamping, so pressing
    a button that does nothing cannot make a list look edited.
    """
    face = film_list.status
    face.edited_date = timezone.now()
    face.save(update_fields=["edited_date"])


def _append_films(film_list: FilmList, films: Iterable[Film]) -> list[ListItem]:
    """Append films in the order given, skipping any already in the list.

    Skipping rather than raising is what makes the add door idempotent: L5 puts
    a single TMDB-search door on the list page, and a member who searches the
    same title twice, or double-clicks, must not get an IntegrityError where
    they expected a no-op. The same discipline as ``shelve_to_watchlist`` (R19).

    Ranks are handed out from one above the current maximum, so an append never
    collides with an existing position and the caller's order is preserved.
    """
    existing = list(
        ListItem.objects.filter(film_list=film_list).values_list("film_id", "rank")
    )
    present = {film_id for film_id, _ in existing}
    rank = max((rank for _, rank in existing), default=0) + 1
    created: list[ListItem] = []
    for film in films:
        if film.pk in present:
            continue
        created.append(
            ListItem.objects.create(film_list=film_list, film=film, rank=rank)
        )
        present.add(film.pk)
        rank += 1
    return created


def add_films(film_list: FilmList, films: Iterable[Film]) -> list[ListItem]:
    """Add films to an existing list, appending them in the order given.

    ``_append_films`` plus the edited stamp — the split exists so
    ``create_list`` can fill a brand-new list without marking it edited.
    Returns the rows it actually created, which is empty when every film was
    already in the list, and nothing is stamped in that case.
    """
    created = _append_films(film_list, films)
    if created:
        _stamp_edited(film_list)
    return created


def remove_film(film_list: FilmList, film: Film) -> bool:
    """Take a film out of the list. True if it was in it, False if it wasn't.

    The rank gap is left open deliberately. Ranks order the rows; they are not
    the number the page prints (``ListItem`` says what that costs). Closing the
    gap would rewrite every row below the removal on each one, for a tidy
    column of integers nobody is allowed to depend on.
    """
    deleted, _ = ListItem.objects.filter(film_list=film_list, film=film).delete()
    if deleted:
        _stamp_edited(film_list)
    return deleted > 0


@transaction.atomic
def move(item: ListItem, direction: str) -> bool:
    """Swap an item with its immediate neighbour. True if it moved.

    Adjacent swap only (L3): there is no drag-and-drop layer in this stack, so
    up/down *is* the reorder UI, and the operation the UI can issue is exactly
    the operation implemented here — no server-side re-flow that the client
    cannot express.

    The neighbour is found by rank, not by position, so it stays correct across
    the gaps ``remove_film`` leaves behind. The swap is two writes in one
    transaction: either both rows agree or neither moves.

    Returns False at the ends rather than raising — "already first" is a normal
    outcome of pressing the button, not a caller error. An unrecognised
    direction *is* a caller error and raises.
    """
    if direction == MOVE_UP:
        neighbour = (
            ListItem.objects.filter(film_list_id=item.film_list_id, rank__lt=item.rank)
            .order_by("-rank", "-id")
            .first()
        )
    elif direction == MOVE_DOWN:
        neighbour = (
            ListItem.objects.filter(film_list_id=item.film_list_id, rank__gt=item.rank)
            .order_by("rank", "id")
            .first()
        )
    else:
        raise ValueError(f"Unknown move direction: {direction!r}")

    if neighbour is None:
        return False

    item.rank, neighbour.rank = neighbour.rank, item.rank
    item.save(update_fields=["rank"])
    neighbour.save(update_fields=["rank"])
    _stamp_edited(item.film_list)
    return True


def rename(film_list: FilmList, title: str) -> FilmList:
    """Change a list's title.

    Only the title. The post face carries no text of its own, so there is
    nothing to keep in step here — which is the point of that choice (§2K L9).
    """
    film_list.title = title
    film_list.save(update_fields=["title", "updated_date"])
    _stamp_edited(film_list)
    return film_list


def set_description(film_list: FilmList, description: str) -> FilmList:
    """Change a list's description. L1 makes it editable forever, like the title.

    The mirror of ``rename``, with one extra thing in it: the description is a
    markdown/HTML pair, and ``create_list`` owns that pair rather than leaving
    it to the form. Same rule here — **pass the markdown source in** and let
    the service render it. A form that rendered in ``clean_description`` the
    way ``FilmForm`` does would hand us already-rendered HTML to render again,
    and the description would come out doubled (or escaped, depending on the
    renderer). The form validates; this function is the only place the pair is
    written.

    Clearing the description is a legal edit and takes both halves to blank, so
    a cleared list renders no description block rather than a stale one.
    """
    film_list.description = render_markdown(description)
    film_list.raw_description = description
    film_list.save(update_fields=["description", "raw_description", "updated_date"])
    _stamp_edited(film_list)
    return film_list


@transaction.atomic
def soft_delete_list(film_list: FilmList) -> FilmList:
    """Take a list down: its own row and its post face, together.

    Both halves go because the face is the list's social self. Leaving it live
    would strand a post in every follower's timeline that leads to a list which
    is gone — the offer-with-no-route shape R85 exists to prevent.

    The items and the saves are **not** touched. ``deleted`` hides the list from
    every reader, which is the observable half of L2 for a soft delete; the
    CASCADE on ``ListSave.film_list`` is the hard-delete half. R140 1 is what
    reads that surviving pointer: the Saved tab shows the deleted list with a
    dismissible notice instead of filtering it out, so a saver learns their
    pointer went dead rather than watching it disappear.
    """
    film_list.delete()
    film_list.status.delete()
    return film_list


# --- saving (increment 5, L2/L6/R140 1) -----------------------------------


def save_list(user, film_list: FilmList) -> bool:
    """Point the user's Saved tab at this list. True if that was new.

    ``get_or_create`` rather than ``create``: the unique constraint on
    ``(user, film_list)`` would reject a second insert with ``IntegrityError``
    and turn a double-click into a 500. Skipping instead is the same
    discipline as ``_append_films`` and ``shelve_to_watchlist`` (R19) — the
    second call is a genuine no-op that still succeeds.

    Returns whether a row was actually created so the caller can tell "saved
    it" from "already had it" without a second query.

    **Nothing here notifies.** L6 is absolute: saving is silent. There is no
    ``Notification.Kind`` for it and no producer anywhere, and the proof is
    the ledger count being identical before and after, not the absence of a
    display.

    Nor does this stamp the face as edited. A save is one member's own
    bookkeeping about somebody else's list — it changes nothing about the
    list, and ``_stamp_edited`` feeds ``editedTime`` on the wire, so
    stamping here would broadcast an edit that never happened.
    """
    _, created = ListSave.objects.get_or_create(user=user, film_list=film_list)
    return created


def unsave_list(user, film_list: FilmList) -> bool:
    """Drop the user's pointer at this list. True if there was one.

    Idempotent by construction: a delete that matched nothing is a no-op that
    still succeeds, so a double press cannot fail. The dismissal marker goes
    with the row — it only ever meant something attached to that save.
    """
    deleted, _ = ListSave.objects.filter(user=user, film_list=film_list).delete()
    return deleted > 0


def dismiss_deleted_notice(user, film_list: FilmList) -> bool:
    """Retire the "this list was deleted" notice on the user's own pointer.

    True if this call set the marker, False if it was already set. The caller
    has already established the row exists; the ``notice_dismissed_at__isnull``
    clause in the update is what makes a second dismiss a no-op rather than a
    restamp, so the button can be pressed twice without moving a timestamp.

    Per-saver by construction and not by care: the marker is on the
    ``(user, film_list)`` row, so one member dismissing says nothing about any
    other member's notice. A marker anywhere else — on the ``FilmList``, on a
    shared flag — would let one dismissal silence everybody, which is exactly
    the wrong shape for a per-viewer acknowledgement.
    """
    updated = ListSave.objects.filter(
        user=user, film_list=film_list, notice_dismissed_at__isnull=True
    ).update(notice_dismissed_at=timezone.now())
    return updated > 0
