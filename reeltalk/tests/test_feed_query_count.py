"""The home feed must not spend one query per shelf row (§2S).

**Why invariance and not a ceiling.** A test asserting "the home page makes
fewer than N queries" passes both before and after the fix for any N loose
enough to accommodate the feed the test happens to build — it is vacuous in
exactly the way this bug is easy to reintroduce. The property that actually
breaks is **the query count does not move when the feed grows**. Before the
fix, each extra ``ShelfFilm`` row costs one more ``core_shelf`` SELECT, because
the grouping loop in ``feed_entries`` reads the lazy ``row.shelf`` FK on every
row and the query did not ``select_related("shelf")``. After the fix the count
is a constant of the feed's *shape*, not its size.

The second test pins the same thing from the other side: the signature of the
N+1 is a standalone ``SELECT ... FROM "core_shelf" WHERE "core_shelf"."id" = ?``.
With the JOIN in place the shelf table is only ever reached through the
``core_shelf_film`` query, so that statement cannot appear at all. Naming the
SQL is what makes a future regression fail with the offending query in the
failure message rather than an off-by-one count.

``spread_shelve`` is used deliberately over ``bulk_shelve``: its gap is wider
than ``FEED_BULK_WINDOW``, so R37 never aggregates the rows and every shelf row
reaches the grouping loop as its own row. That is the worst case for the lazy
FK, and the case the owner's 1,378-row import actually produced.
"""

from collections import Counter
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from reeltalk.core.models import feed_entries
from reeltalk.tests.feed import spread_shelve
from reeltalk.tests.members import member


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


def _shelf_queries_for(user):
    """Capture the SQL one ``feed_entries`` call spends, for a given feed size."""
    with CaptureQueriesContext(connection) as captured:
        feed_entries(user)
    return [q["sql"] for q in captured.captured_queries]


def test_feed_query_count_does_not_grow_with_shelf_rows(alice):
    """Adding shelf rows must add zero queries to building the feed.

    The count is captured twice with rows added in between, inside the same
    test, so nothing but the row count differs between the two measurements.
    """
    newest = timezone.now()
    spread_shelve(alice, 5, newest)
    small = _shelf_queries_for(alice)

    # Same shelf, same shape, more rows. Each one is its own feed entry, so
    # each one reaches the grouping loop that reads row.shelf.
    spread_shelve(alice, 20, newest - timedelta(hours=1))
    large = _shelf_queries_for(alice)

    # A multiset difference, not a set one. The per-row lookups are all
    # byte-identical to each other and one of them already appears in the
    # small capture, so a plain "in small" filter reports an empty list for
    # twenty extra queries — which is a useless failure message for the one
    # regression this test exists to catch.
    extra = Counter(large) - Counter(small)
    assert len(large) == len(small), (
        f"the feed's query count grew from {len(small)} to {len(large)} when "
        f"shelf rows were added — a per-row lookup is back; the added queries "
        f"were: {dict(extra)}"
    )


def test_the_shelf_table_is_touched_by_exactly_one_query(alice):
    """At most one query may reference the shelf table, whatever the row count.

    The N+1's signature is one ``SELECT ... FROM "core_shelf" WHERE
    "core_shelf"."id" = ?`` per shelf row, on top of the feed query that
    already JOINs the shelf in. Counting the queries that name ``"core_shelf"``
    catches both shapes: with the JOIN in place exactly one does, and each
    reintroduced lazy-FK lookup adds another.

    The quotes are load-bearing, not cosmetics. ``"core_shelffilm"`` does not
    contain the substring ``"core_shelf"`` because of its closing quote, so the
    match names the shelf table and never the shelf-row table.
    """
    spread_shelve(alice, 8, timezone.now())
    shelf_queries = [sql for sql in _shelf_queries_for(alice) if '"core_shelf"' in sql]
    assert len(shelf_queries) == 1, (
        f"expected exactly one query to touch the shelf table, got "
        f"{len(shelf_queries)} — more than one means the lazy shelf FK is "
        f"being resolved per row again: {shelf_queries}"
    )
