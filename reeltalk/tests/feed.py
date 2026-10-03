"""Read-side helpers for the cursor-paged home feed (§2I increment 2, R132).

``feed_entries`` returns ``(entries, next_cursor)`` now, because the home feed
is paged. The overwhelming majority of the suite wants the whole feed with no
paging — it is asserting on shape, membership and aggregation, none of which a
page boundary is supposed to change — so those call sites use ``unpaged``
rather than indexing a tuple forty times. The index is what hides what the
second element is for; a name says it.

Tests that ARE about paging call ``feed_entries`` directly and unpack, so the
cursor is visible at the call site. The two shelf-fixture builders below are
shared by the paging tests and the endless-scroll tests for the obvious reason:
they have to build the same feed to talk about the same pages.
"""

from datetime import timedelta

from reeltalk.core.models import Film, Shelf, ShelfFilm, feed_entries


def unpaged(user) -> list:
    """Every entry this user's feed contains — no limit, no cursor."""
    entries, _ = feed_entries(user)
    return entries


def all_pages(user, *, limit: int) -> list:
    """Walk the paged feed to its end and return the concatenation.

    The identity check this exists for: ``all_pages(user, limit=N)`` must equal
    ``unpaged(user)`` entry for entry, in the same order, with no duplicate and
    no gap. Anything else means the cursor is dropping rows or repeating them.
    """
    seen: list = []
    cursor = None
    # Bound the walk so a cursor that never advances fails here with a clear
    # message instead of hanging the suite forever.
    for _ in range(1000):
        entries, cursor = feed_entries(user, limit=limit, cursor=cursor)
        seen.extend(entries)
        if cursor is None:
            return seen
    raise AssertionError(
        f"feed for {user} never terminated: {len(seen)} rows over 1000 pages "
        f"at limit={limit} — the cursor is not advancing"
    )


def spread_shelve(
    user, n, newest_at, *, gap=timedelta(minutes=10), identifier=Shelf.TO_READ
):
    """``n`` shelf events ending at ``newest_at`` and running ``gap`` apart into
    the past, as distinct films.

    Two things the fixtures depend on: every row is older than ``newest_at``
    (so a caller can place a group "behind" a timestamp by passing it as
    ``newest_at``), and ``gap`` is wider than ``FEED_BULK_WINDOW`` so R37 never
    aggregates these into one entry — each row is its own feed entry.
    """
    shelf = Shelf.objects.get(user=user, identifier=identifier)
    films = []
    for i in range(1, n + 1):
        film = Film.objects.create(
            title=f"{user.localname} Spread {i:02d}", year=2000 + i
        )
        ShelfFilm.objects.create(
            shelf=shelf,
            film=film,
            user=user,
            shelved_date=newest_at - gap * (n - i),
        )
        films.append(film)
    return films


def bulk_shelve(
    user, n, at, *, identifier=Shelf.TO_READ, step_seconds=1, prefix="Bulk"
):
    """``n`` shelf events seconds apart — the shape of a file import, which R37
    collapses into a single aggregate entry."""
    shelf = Shelf.objects.get(user=user, identifier=identifier)
    for i in range(1, n + 1):
        film = Film.objects.create(
            title=f"{user.localname} {prefix} {i:02d}", year=2000 + i
        )
        ShelfFilm.objects.create(
            shelf=shelf,
            film=film,
            user=user,
            shelved_date=at + timedelta(seconds=i * step_seconds),
        )
