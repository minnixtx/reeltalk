"""Read-side helper for the cursor-paged home feed (§2I increment 2, R132).

``feed_entries`` returns ``(entries, next_cursor)`` now, because the home feed
is paged. The overwhelming majority of the suite wants the whole feed with no
paging — it is asserting on shape, membership and aggregation, none of which a
page boundary is supposed to change — so those call sites use ``unpaged``
rather than indexing a tuple forty times. The index is what hides what the
second element is for; a name says it.

Tests that ARE about paging call ``feed_entries`` directly and unpack, so the
cursor is visible at the call site.
"""

from reeltalk.core.models import feed_entries


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
