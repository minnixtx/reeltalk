"""The home feed becomes a cursor-paged list (§2I increment 2, R132).

Three things are pinned here, and each is the reason a different plausible
implementation was rejected.

**Paging the computed list, not the query (decision 1).** ``feed_entries``
assembles the whole grouped list and then slices it, so the R37 aggregation is
blind to page boundaries. The straddle test is the load-bearing one: a 25-row
bulk import landing on a page boundary still renders as ONE aggregate at the
same index, paged or not. Had aggregation been per page, the ``absorbed`` set
would be page-local and a rating-only status of a film absorbed on page 1 would
surface again as its own row on page 2 — a correctness break, not a cosmetic
one. Slicing the shelf rows *before* grouping turns both the straddle test and
the concatenation test red; that pair is what pins the decision.

**Cursor, never offset (decision 2).** ``?page=N`` counts positions, and
positions move the moment anyone posts. The cursor carries the row's own sort
key, so "next page" means "everything strictly older" and a post landing
mid-scroll can neither duplicate a row nor eat one.

**A defined tie order (the agreed side effect).** The old sort was stable on
``date`` alone, so same-timestamp rows kept insertion order. A cursor needs a
total order, so the key is now ``(date, kind_rank, source_id)``. That can move
a same-timestamp pair relative to today — the owner accepted that explicitly
when the eight decisions were taken (R132) — and the tie tests below pin the
new order so it is defined rather than arbitrary.
"""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from reeltalk.core.models import (
    FEED_BULK_WINDOW,
    FEED_PAGE_SIZE,
    KIND_RANK_SHELF,
    KIND_RANK_STATUS,
    Film,
    Shelf,
    ShelfFilm,
    Status,
    decode_feed_cursor,
    encode_feed_cursor,
    feed_entries,
)
from reeltalk.core.views import GENRE_PAGE_SIZE
from reeltalk.tests.feed import all_pages, unpaged
from reeltalk.tests.members import member, site_admin

PAGE = FEED_PAGE_SIZE


@pytest.fixture
def admin(db):
    # R12: / redirects to /setup/ until a superuser exists, so without this
    # every render assertion below proves nothing about the feed.
    return site_admin(localname="admin", password="s3cretpass")


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return member(localname="bob", password="s3cretpass")


# --- Fixture helpers -------------------------------------------------------


def ident(entry):
    """A row's identity: what paging must carry over unchanged, in unchanged
    order. ``source_id`` is in it because it is what the cursor is cut from."""
    return (
        entry.kind,
        entry.user.localname,
        entry.film.pk if entry.film else None,
        entry.date,
        entry.source_id,
    )


def film_id(entry):
    return entry.film.pk if entry.film else None


def _shelf(user, identifier=Shelf.TO_READ):
    return Shelf.objects.get(user=user, identifier=identifier)


def _spread_shelve(
    user, n, newest_at, *, gap=timedelta(minutes=10), identifier=Shelf.TO_READ
):
    """``n`` shelf events ending at ``newest_at`` and running ``gap`` apart into
    the past.

    Two things the fixtures depend on: every row is older than ``newest_at``
    (so a caller can place a group "behind" a timestamp by passing it as
    ``newest_at``), and ``gap`` is wider than ``FEED_BULK_WINDOW`` so R37 never
    aggregates these into one entry — each row is its own feed entry.
    """
    shelf = _shelf(user, identifier)
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


def _bulk_shelve(user, n, at, *, identifier=Shelf.TO_READ, step_seconds=1):
    """``n`` shelf events seconds apart — the shape of a file import, which R37
    collapses into a single aggregate."""
    shelf = _shelf(user, identifier)
    for i in range(1, n + 1):
        film = Film.objects.create(
            title=f"{user.localname} Bulk {i:02d}", year=2000 + i
        )
        ShelfFilm.objects.create(
            shelf=shelf,
            film=film,
            user=user,
            shelved_date=at + timedelta(seconds=i * step_seconds),
        )


def _feed_region(body: str) -> str:
    """Just the ``<ul class="review-list">`` inner HTML.

    Scoped on purpose: the rail on the same page links to films too, so a
    whole-body match for film hrefs would mix the feed with trending and could
    pass on the rail's content rather than the feed's.
    """
    start = body.index('<ul class="review-list">') + len('<ul class="review-list">')
    end = body.index("</ul>", start)
    return body[start:end]


def _row_count(region: str) -> int:
    return region.count('<li class="review"')


def _row_film_ids(region: str):
    """The film each rendered row points at, in render order.

    ``data-status`` cannot carry this for a shelf event with no post behind it
    (it renders ``"none"`` for every such row), which would make any comparison
    between two pages of watchlist rows vacuous.
    """
    return [int(m) for m in re.findall(r'href="/film/(\d+)/"', region)]


def _member_client() -> Client:
    client = Client()
    assert client.login(username="alice", password="s3cretpass"), (
        "alice did not sign in — the page below would render anonymously, "
        "and every assertion on it would pass vacuously"
    )
    return client


def _is_bulk(entry) -> bool:
    return bool(entry.film) and "Bulk" in entry.film.title


# --- Contract ------------------------------------------------------------


def test_page_size_matches_the_genre_page_size():
    """R132 decision 3: 20, matching the genre subfeed. Invisible under endless
    scroll, so it is a first-paint knob and nothing more."""
    assert FEED_PAGE_SIZE == 20 == GENRE_PAGE_SIZE


def test_no_limit_returns_the_whole_feed_and_no_cursor(alice):
    _spread_shelve(alice, PAGE + 5, timezone.now() - timedelta(hours=2))
    entries, next_cursor = feed_entries(alice)
    assert len(entries) == PAGE + 5
    assert next_cursor is None, "an unpaged read has no next page to point at"


def test_cursor_round_trips_the_sort_key(alice):
    _spread_shelve(alice, 2, timezone.now() - timedelta(hours=2))
    last = unpaged(alice)[-1]
    assert decode_feed_cursor(encode_feed_cursor(last)) == last.sort_key


def test_kind_rank_keys_off_kind_and_never_off_list_position(alice):
    """A rank derived from position would move between requests, which is fatal
    for a cursor. This asserts both arms of the rank."""
    _spread_shelve(alice, 1, timezone.now() - timedelta(hours=2))
    assert unpaged(alice)[0].kind_rank == KIND_RANK_SHELF

    status = Status.objects.create(
        user=alice,
        film=Film.objects.create(title="Standalone", year=2020),
        status_type=Status.Type.COMMENT,
        content="<p>hello</p>",
        published_date=timezone.now(),
    )
    status_entry = next(e for e in unpaged(alice) if e.status_id == status.pk)
    assert status_entry.kind_rank == KIND_RANK_STATUS


# --- Concatenation identity ----------------------------------------------


def test_pages_concatenate_to_the_unpaged_list_exactly(alice):
    """The headline claim: paging loses nothing and duplicates nothing.

    The ground truth is the FIXTURE, not ``feed_entries`` itself. Comparing the
    paged walk only against ``unpaged(alice)`` looked sufficient and was not: a
    mutation that truncates the shelf rows before grouping corrupts both sides
    the same way and the equality still holds. Asserting the row count and the
    covered film set against what was actually created is what makes this test
    able to see a lost row at all.
    """
    n = PAGE * 2 + 7
    films = _spread_shelve(alice, n, timezone.now() - timedelta(hours=3))
    got = all_pages(alice, limit=PAGE)

    assert len(got) == n, f"paging produced {len(got)} rows for {n} fixture rows"
    assert {film_id(e) for e in got} == {f.pk for f in films}
    assert len({e.source_id for e in got}) == n, "a row appeared on two pages"
    assert [ident(e) for e in got] == [ident(e) for e in unpaged(alice)]
    assert [e.date for e in got] == sorted((e.date for e in got), reverse=True)


def test_every_page_is_full_except_the_last(alice):
    _spread_shelve(alice, PAGE + 3, timezone.now() - timedelta(hours=3))
    sizes = []
    cursor = None
    for _ in range(10):
        entries, cursor = feed_entries(alice, limit=PAGE, cursor=cursor)
        sizes.append(len(entries))
        if cursor is None:
            break
    else:
        pytest.fail("feed did not terminate in 10 pages")
    assert sizes == [PAGE, 3]


def test_no_cursor_is_offered_when_nothing_is_left_behind(alice):
    _spread_shelve(alice, PAGE, timezone.now() - timedelta(hours=3))
    entries, next_cursor = feed_entries(alice, limit=PAGE)
    assert len(entries) == PAGE
    assert next_cursor is None, (
        "a cursor over an exactly-full feed points at an empty page"
    )


def test_a_post_arriving_mid_scroll_neither_duplicates_nor_drops_a_row(alice):
    """The reason for a cursor rather than an offset, stated as a test.

    ``before`` is page 1 plus everything behind it. A newer row then lands. The
    cursor is a key, not a count, so page 2 is still exactly ``before[PAGE:]``.
    An offset would have returned ``before[PAGE + 1 :]`` here and silently
    eaten the row that used to open page 2.
    """
    _spread_shelve(alice, PAGE * 2, timezone.now() - timedelta(hours=3))
    before = [ident(e) for e in unpaged(alice)]
    _, cursor = feed_entries(alice, limit=PAGE)
    assert cursor is not None

    ShelfFilm.objects.create(
        shelf=_shelf(alice),
        film=Film.objects.create(title="Just posted", year=2026),
        user=alice,
        shelved_date=timezone.now(),
    )

    page2 = [ident(e) for e in feed_entries(alice, limit=PAGE, cursor=cursor)[0]]
    assert len(page2) == PAGE
    assert page2 == before[PAGE:]


# --- Aggregation across the boundary (decision 1) -------------------------


@pytest.mark.parametrize("newer_rows", [PAGE - 1, PAGE])
def test_a_straddling_bulk_group_stays_one_entry_at_the_same_position(
    alice, newer_rows
):
    """A 25-row import landing on a page boundary is still ONE aggregate, at the
    same index, paged or not.

    ``newer_rows`` lands the aggregate last on page 1 (``PAGE - 1`` newer rows)
    and first on page 2 (``PAGE`` newer rows), so both edges of the boundary
    are covered.
    """
    assert 25 <= FEED_BULK_WINDOW.total_seconds(), (
        "fixture assumes a 1-second-step 25-row import stays inside the bulk window"
    )
    bulk_at = timezone.now() - timedelta(days=1)
    # ``newer_rows`` rows sit above the import, five below it, so the aggregate
    # lands exactly on the page boundary either side of it.
    _spread_shelve(
        alice, newer_rows, bulk_at + timedelta(hours=newer_rows), gap=timedelta(hours=1)
    )
    _bulk_shelve(alice, 25, bulk_at)
    _spread_shelve(alice, 5, bulk_at - timedelta(hours=6), gap=timedelta(hours=1))

    expected = [ident(e) for e in unpaged(alice)]
    got = all_pages(alice, limit=PAGE)
    assert [ident(e) for e in got] == expected
    # Fixture-level ground truth again: the import is 25 shelf rows that must
    # arrive as exactly one entry, so the whole feed is newer + 1 + older.
    assert len(got) == newer_rows + 1 + 5, (
        f"expected {newer_rows + 1 + 5} rows, got {len(got)} — the import or "
        "the surrounding rows were truncated"
    )

    bulk = [i for i, e in enumerate(got) if _is_bulk(e)]
    assert len(bulk) == 1, f"the straddling import produced {len(bulk)} rows, not one"
    assert bulk[0] == newer_rows


def test_an_absorbed_rating_only_status_never_resurfaces_on_a_later_page(alice):
    """R37's ``absorbed`` set must stay global across pages.

    A bulk *watched* import absorbs each film's rating-only status into the
    aggregate. Were aggregation per page, that absorption would be page-local
    and the absorbed status would reappear as its own row on whichever page the
    film's shelf row fell onto.
    """
    bulk_at = timezone.now() - timedelta(days=1)
    _spread_shelve(alice, PAGE, bulk_at + timedelta(hours=PAGE), gap=timedelta(hours=1))
    _bulk_shelve(alice, 25, bulk_at, identifier=Shelf.READ)
    _spread_shelve(alice, 5, bulk_at - timedelta(hours=6), gap=timedelta(hours=1))
    absorbed = set()
    for i in range(1, 26):
        status = Status.objects.create(
            user=alice,
            film=Film.objects.get(title=f"alice Bulk {i:02d}"),
            status_type=Status.Type.REVIEW_RATING,
            rating="4",
            published_date=bulk_at + timedelta(seconds=i),
        )
        absorbed.add(status.pk)

    pages = all_pages(alice, limit=PAGE)
    surfaced = [e.status_id for e in pages if e.status_id in absorbed]
    assert surfaced == [], "an absorbed rating-only status reached the paged feed"
    assert len([e for e in pages if _is_bulk(e)]) == 1


# --- A defined tie order (the agreed change) ------------------------------


def test_a_shelf_event_outranks_a_status_on_the_same_timestamp(alice):
    """Same instant, defined order: the shelf row first (``KIND_RANK_SHELF`` >
    ``KIND_RANK_STATUS``) — which is also the order the pre-paging feed produced
    by building shelf entries first."""
    at = timezone.now() - timedelta(hours=1)
    ShelfFilm.objects.create(
        shelf=_shelf(alice),
        film=Film.objects.create(title="Shelved Same Instant", year=2020),
        user=alice,
        shelved_date=at,
    )
    Status.objects.create(
        user=alice,
        film=Film.objects.create(title="Reviewed Same Instant", year=2021),
        status_type=Status.Type.REVIEW,
        rating=Decimal("4"),
        content="<p>Great.</p>",
        published_date=at,
    )
    entries = unpaged(alice)
    assert [e.kind for e in entries] == ["watchlist", "status"]
    assert entries[0].sort_key > entries[1].sort_key


def test_same_timestamp_shelf_events_order_by_source_id_descending(alice, bob):
    """Two shelf events at one instant order by their own row id, highest first —
    the later row comes earlier in a newest-first feed. Deterministic either
    way; the point is that it is a rule and not insertion order."""
    at = timezone.now() - timedelta(hours=1)
    alice.follows.add(bob)
    first = ShelfFilm.objects.create(
        shelf=_shelf(alice),
        film=Film.objects.create(title="Lower Id", year=2020),
        user=alice,
        shelved_date=at,
    )
    second = ShelfFilm.objects.create(
        shelf=_shelf(bob),
        film=Film.objects.create(title="Higher Id", year=2021),
        user=bob,
        shelved_date=at,
    )
    assert second.pk > first.pk
    assert [e.source_id for e in unpaged(alice)] == [second.pk, first.pk]


def test_a_tied_pair_sitting_on_the_page_boundary_pages_cleanly(alice):
    """The nastiest case for a cursor: the two rows that tie for one instant are
    the last row of page 1 and the first row of page 2. The cursor cut from
    page 1's last row must be *strictly* older, so the shelf row is not repeated
    and the status row is not skipped."""
    at = timezone.now() - timedelta(hours=1)
    _spread_shelve(
        alice,
        PAGE - 1,
        at + timedelta(minutes=10 * (PAGE - 1)),
        gap=timedelta(minutes=10),
    )
    shelf_row = ShelfFilm.objects.create(
        shelf=_shelf(alice),
        film=Film.objects.create(title="Boundary Shelf", year=2020),
        user=alice,
        shelved_date=at,
    )
    status_row = Status.objects.create(
        user=alice,
        film=Film.objects.create(title="Boundary Status", year=2021),
        status_type=Status.Type.COMMENT,
        content="<p>Boundary.</p>",
        published_date=at,
    )

    page1, cursor = feed_entries(alice, limit=PAGE)
    assert len(page1) == PAGE
    assert page1[-1].source_id == shelf_row.pk
    assert page1[-1].kind == "watchlist"
    assert cursor is not None

    page2, next_cursor = feed_entries(alice, limit=PAGE, cursor=cursor)
    assert page2[0].status_id == status_row.pk
    assert page2[0].kind == "status"
    assert next_cursor is None

    seen = [e.source_id for e in page1] + [e.source_id for e in page2]
    assert len(seen) == len(set(seen)), "the tied pair duplicated across the boundary"


# --- The rendered page ----------------------------------------------------


def test_page_one_renders_twenty_rows_and_a_real_older_link(alice, admin):
    _spread_shelve(alice, PAGE + 10, timezone.now() - timedelta(hours=3))
    body = _member_client().get("/").content.decode()
    assert "Log out" in body, "not a member render — nothing below proves anything"
    assert _row_count(_feed_region(body)) == PAGE
    assert 'class="feed-older"' in body


def test_following_the_older_link_renders_the_next_twenty_rows(alice, admin):
    """Page 2 of the rendered page is the same 20 rows page 2 of the function
    returns, in the same order — the view is not quietly re-sorting or
    re-slicing what ``feed_entries`` handed it."""
    _spread_shelve(alice, PAGE * 2 + 5, timezone.now() - timedelta(hours=3))
    client = _member_client()
    page1_body = client.get("/").content.decode()
    _, cursor = feed_entries(alice, limit=PAGE)
    expected, _ = feed_entries(alice, limit=PAGE, cursor=cursor)

    page2_body = client.get(f"/?c={cursor}").content.decode()
    assert "Log out" in page2_body
    page2_region = _feed_region(page2_body)
    assert _row_count(page2_region) == PAGE
    assert _row_film_ids(page2_region) == [film_id(e) for e in expected]
    assert set(_row_film_ids(_feed_region(page1_body))).isdisjoint(
        _row_film_ids(page2_region)
    ), "page 2 repeats rows from page 1"


def test_the_last_page_renders_no_older_link(alice, admin):
    _spread_shelve(alice, PAGE, timezone.now() - timedelta(hours=3))
    body = _member_client().get("/").content.decode()
    assert _row_count(_feed_region(body)) == PAGE
    assert 'class="feed-older"' not in body, "a link to a page that does not exist"


def test_a_mangled_cursor_renders_the_first_page_not_an_error(alice, admin):
    """A bad ``?c=`` is a first page, not a 500 and not an empty feed."""
    _spread_shelve(alice, PAGE + 5, timezone.now() - timedelta(hours=3))
    response = _member_client().get("/?c=not-a-cursor")
    assert response.status_code == 200
    assert _row_count(_feed_region(response.content.decode())) == PAGE


def test_the_older_link_is_an_anchor_not_a_script_handler(alice, admin):
    """Decision 6: no JS must still reach older rows, so the link has to be a
    real ``<a href>`` in the served HTML rather than a handler."""
    _spread_shelve(alice, PAGE + 5, timezone.now() - timedelta(hours=3))
    body = _member_client().get("/").content.decode()
    start = body.index('class="feed-older"')
    tag = body[body.rindex("<a", 0, start) : body.index(">", start) + 1]
    assert tag.startswith("<a ") and 'href="/?c=' in tag, f"not a plain anchor: {tag}"
