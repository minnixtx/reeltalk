"""The write path for lists (§2K increment 1).

Every mutation of a list goes through one of these six functions, and nothing
else writes these tables. That is not ceremony: L1 makes a list freely editable
forever, which means the interesting cases are all *repeated* edits, and a
rank or a face maintained in two places is where a feature like this rots. Each
function is small, atomic, and idempotent where the door that calls it can be
pressed twice.

No view, URL or template is here — increment 1 has no visual surface on
purpose (§2K: the moment ``Status.Type.LIST`` exists, list rows are already in
every follower's timeline, so the design has to arrive before that is
visible).
"""

from collections.abc import Iterable

from django.db import transaction

from reeltalk.core.models import Film, Status
from reeltalk.core.utils import render_markdown
from reeltalk.lists.models import FilmList, ListItem

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
    add_films(film_list, films)
    return film_list


def add_films(film_list: FilmList, films: Iterable[Film]) -> list[ListItem]:
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


def remove_film(film_list: FilmList, film: Film) -> bool:
    """Take a film out of the list. True if it was in it, False if it wasn't.

    The rank gap is left open deliberately. Ranks order the rows; they are not
    the number the page prints (``ListItem`` says what that costs). Closing the
    gap would rewrite every row below the removal on each one, for a tidy
    column of integers nobody is allowed to depend on.
    """
    deleted, _ = ListItem.objects.filter(film_list=film_list, film=film).delete()
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
    return True


def rename(film_list: FilmList, title: str) -> FilmList:
    """Change a list's title.

    Only the title. The post face carries no text of its own, so there is
    nothing to keep in step here — which is the point of that choice (§2K L9).
    """
    film_list.title = title
    film_list.save(update_fields=["title", "updated_date"])
    return film_list


@transaction.atomic
def soft_delete_list(film_list: FilmList) -> FilmList:
    """Take a list down: its own row and its post face, together.

    Both halves go because the face is the list's social self. Leaving it live
    would strand a post in every follower's timeline that leads to a list which
    is gone — the offer-with-no-route shape R85 exists to prevent.

    The items and the saves are **not** touched. ``deleted`` hides the list from
    every reader, which is the observable half of L2 for a soft delete; the
    CASCADE on ``ListSave.film_list`` is the hard-delete half. A reader of
    "lists you saved" must therefore filter on ``film_list__deleted=False``
    (increment 5) rather than assume a save row means a saveable list.
    """
    film_list.delete()
    film_list.status.delete()
    return film_list
