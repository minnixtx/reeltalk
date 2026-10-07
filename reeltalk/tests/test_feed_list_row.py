"""The list's row in the home feed (§2K increment 4, R139).

Three lines, five posters, ``+N``: the verb in the head, the title, then the
poster strip. That is the visible half. The half that actually matters is the
gate underneath it, so most of this file is about one distinction.

**"Has no film" is not "is a list face", and the live feed proves it.** The
home feed holds two kinds of film-less row: a list's post face, whose reply
route accepts a reply, and a mirrored ``Note`` that arrived with no film
reference, whose reply route answers 400. A template that widened the reply
gate on "no film" would put a live-looking reply icon on the second kind —
R85's offer-with-no-route bug, reopened by an inference. So ``is_list_face``
is set from ``status_type`` inside ``feed_entries``, where the ``Status`` is
actually in hand, and every gate reads that.

Both directions are therefore proven separately, at the template AND at the
route: a list row gets the control and its reply lands; a film-less mirrored
``Note`` row gets no control and its reply is still refused. Proving only the
first lets the second regress with nothing failing, which is the shape
increment 2's ``test_a_post_with_no_film_offers_no_composer_and_the_route_refuses_one``
was written to stop.

The rest is the strip: the cap is :data:`LIST_FEED_POSTER_CAP` rather than a
number in the template, ``+N`` is measured against the list's real length
rather than the tiles drawn, and the posters arrive batched — three queries
for the whole page whatever it holds, asserted rather than hoped, because this
row sits on the page that already spends 1,378 queries on shelf lookups.
"""

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from reeltalk.core.models import (
    LIST_FEED_POSTER_CAP,
    Film,
    Like,
    Status,
    mark_watched,
)
from reeltalk.lists.models import FilmList
from reeltalk.lists.services import create_list
from reeltalk.tests.feed import all_pages, unpaged
from reeltalk.tests.members import member, site_admin

PASSWORD = "s3cretpass"


@pytest.fixture
def admin(db):
    # R12: / redirects to /setup/ until a superuser exists.
    return site_admin(localname="admin", password=PASSWORD)


@pytest.fixture
def maker(db):
    return member(localname="maker", password=PASSWORD)


@pytest.fixture
def viewer(db):
    return member(localname="viewer", password=PASSWORD)


@pytest.fixture
def carol(db):
    return member(localname="carol", password=PASSWORD)


def _films(n, prefix="Strip", *, poster=True):
    """``n`` films, each wearing a poster unless ``poster=False``.

    The poster half matters: the strip must draw a tile for a film that has
    no art rather than skipping it, so the tile count tracks the cap and the
    ``+N`` never has to care which films had posters.
    """
    made = []
    for i in range(n):
        film = Film.objects.create(title=f"{prefix} {i}", year=1970 + i)
        if poster:
            film.poster = f"posters/{prefix.lower()}-{i}.jpg"
            film.save(update_fields=["poster"])
        made.append(film)
    return made


def _login(localname) -> Client:
    client = Client()
    assert client.login(username=localname, password=PASSWORD), (
        f"{localname} did not sign in — the feed below would render "
        "anonymously and every assertion on it would pass vacuously"
    )
    return client


def _home(who) -> str:
    return _login(who).get("/").content.decode()


def _row_for(body, status_id) -> str:
    """One feed row's markup, scoped to that row.

    The scoping is checked rather than trusted: a slice that ran past its own
    ``</li>`` would let every assertion below read a neighbour's markup too,
    and a control that should be absent would go unnoticed in the row next
    door.
    """
    marker = f'<li class="review" data-status="{status_id}"'
    assert marker in body, f"the home feed holds no row for status {status_id}"
    start = body.index(marker)
    end = body.index("</li>", start)
    assert "<li" not in body[start + 1 : end], (
        f"the slice for status {status_id} ran past its own </li> — the "
        "assertions on it would be reading another row's markup"
    )
    return body[start:end]


def _entry_for(user, status_id):
    return next(e for e in unpaged(user) if e.status_id == status_id)


def _tiles(row) -> int:
    """How many poster tiles this row drew.

    Counted against the closing quote, never as a bare prefix: the container
    is ``.list-strip-posters``, and ``class="list-strip-poster"`` is its
    literal prefix — so a prefix count reports one tile too many, and every
    cap assertion would be off by exactly that.
    """
    return row.count('class="list-strip-poster"') + row.count(
        'class="list-strip-poster thumb-placeholder"'
    )


# --- The row's shape (R139) -------------------------------------------------


@pytest.mark.django_db
def test_a_list_row_renders_the_verb_the_title_and_its_posters(maker, viewer, admin):
    viewer.follows.add(maker)
    film_list = create_list(
        maker, title="Noir You Must See", films=_films(3, prefix="Noir")
    )
    row = _row_for(_home("viewer"), film_list.status_id)
    assert "created a new list" in row
    assert "Noir You Must See" in row
    assert _tiles(row) == 3
    assert "list-strip-count" not in row


@pytest.mark.django_db
def test_the_strip_caps_at_the_named_constant_and_says_plus_n(maker, viewer, admin):
    # The cap is a constant, not a number in the template: this test names
    # LIST_FEED_POSTER_CAP rather than a literal 5, so changing the constant
    # moves the row and the assertion together.
    viewer.follows.add(maker)
    film_list = create_list(
        maker,
        title="A Long One",
        films=_films(LIST_FEED_POSTER_CAP + 2, prefix="Long"),
    )
    row = _row_for(_home("viewer"), film_list.status_id)
    assert _tiles(row) == LIST_FEED_POSTER_CAP
    assert '<span class="list-strip-count">+2</span>' in row


@pytest.mark.django_db
def test_a_list_at_exactly_the_cap_shows_no_overflow(maker, viewer, admin):
    # The boundary: +N appears only when the list is LONGER than the cap,
    # not when it merely reaches it.
    viewer.follows.add(maker)
    film_list = create_list(
        maker, title="Exactly Enough", films=_films(LIST_FEED_POSTER_CAP, prefix="Cap")
    )
    row = _row_for(_home("viewer"), film_list.status_id)
    assert _tiles(row) == LIST_FEED_POSTER_CAP
    assert "list-strip-count" not in row


@pytest.mark.django_db
def test_the_overflow_counts_the_list_not_the_tiles_drawn(maker, viewer, admin):
    # The distinction the brief warns about: +N is the list's real length
    # minus the cap, never the tiles that happened to render. A poster-less
    # film must not be able to shift it.
    viewer.follows.add(maker)
    films = _films(LIST_FEED_POSTER_CAP + 1, prefix="Mix")
    films[1].poster = ""
    films[1].save(update_fields=["poster"])
    film_list = create_list(maker, title="One Without Art", films=films)

    entry = _entry_for(viewer, film_list.status_id)
    assert entry.list_strip.film_count == LIST_FEED_POSTER_CAP + 1
    assert len(entry.list_strip.posters) == LIST_FEED_POSTER_CAP
    assert entry.list_strip.overflow == 1

    row = _row_for(_home("viewer"), film_list.status_id)
    # The tile is still drawn, as a placeholder — so the strip is exactly
    # the cap wide whether or not the films inside it had posters.
    assert _tiles(row) == LIST_FEED_POSTER_CAP
    assert 'class="list-strip-poster thumb-placeholder"' in row
    assert '<span class="list-strip-count">+1</span>' in row


@pytest.mark.django_db
def test_a_poster_wears_an_img_and_a_missing_one_a_placeholder(maker, viewer, admin):
    viewer.follows.add(maker)
    with_art = _films(1, prefix="Art")
    bare = _films(1, prefix="Bare", poster=False)
    film_list = create_list(maker, title="Both Kinds", films=[with_art[0], bare[0]])
    row = _row_for(_home("viewer"), film_list.status_id)
    assert '<img class="list-strip-poster" src=' in row
    assert '<span class="list-strip-poster thumb-placeholder"' in row


@pytest.mark.django_db
def test_an_empty_list_renders_the_verb_and_title_but_no_strip(maker, viewer, admin):
    # An empty list is a real state (the editor lets you make one), and the
    # row must read as a list with nothing in it rather than a broken row.
    viewer.follows.add(maker)
    film_list = create_list(maker, title="An Empty List")
    row = _row_for(_home("viewer"), film_list.status_id)
    assert "created a new list" in row
    assert "An Empty List" in row
    assert "list-strip-poster" not in row
    assert "list-strip-count" not in row


@pytest.mark.django_db
def test_the_strip_never_renders_on_a_row_that_is_not_a_list(maker, viewer, admin):
    # A review alongside the list, so the "not a list" side of the branch
    # has a real row to check rather than an empty list of rows.
    viewer.follows.add(maker)
    create_list(maker, title="One List", films=_films(2, prefix="Pair"))
    reviewed = Film.objects.create(title="A Normal Review", year=1958)
    mark_watched(maker, reviewed, rating="4", content="<p>Solid.</p>")

    body = _home("viewer")
    entries = unpaged(viewer)
    rows = [
        _row_for(body, e.status_id)
        for e in entries
        if e.status_id is not None and not e.is_list_face
    ]
    assert rows, (
        "no non-list row in the feed — the negative assertion below would be "
        "vacuous, so this fixture has to carry one"
    )
    assert all("list-strip" not in row for row in rows)
    # And the positive control on the same render: the list row DID draw a
    # strip, or the two assertions above are comparing nothing to nothing.
    list_rows = [_row_for(body, e.status_id) for e in entries if e.is_list_face]
    assert list_rows and all("list-strip" in row for row in list_rows)


# --- THE TRAP: film-less is not list-face -----------------------------------


@pytest.mark.django_db
def test_is_list_face_comes_from_the_status_type_not_from_a_missing_film(
    maker, carol, viewer
):
    # The model-level half of the distinction, with both kinds side by side
    # and both film-less — so nothing here can pass by reading "no film".
    viewer.follows.add(maker)
    viewer.follows.add(carol)
    listed = create_list(maker, title="A Real List", films=_films(2, prefix="Real"))
    note = Status.objects.create(
        user=carol,
        film=None,
        status_type=None,
        content="<p>A mirrored note with no film.</p>",
        local=False,
        remote_url="https://remote.example/note/filmless-feed",
    )
    assert listed.status.film_id is None and note.film_id is None, (
        "both rows must be film-less for this test to prove anything"
    )

    assert _entry_for(viewer, listed.status_id).is_list_face is True
    assert _entry_for(viewer, listed.status_id).replyable is True
    assert _entry_for(viewer, note.id).is_list_face is False
    assert _entry_for(viewer, note.id).replyable is False
    assert _entry_for(viewer, note.id).list_strip is None


@pytest.mark.django_db
def test_a_list_row_gets_the_reply_control(maker, viewer, admin):
    viewer.follows.add(maker)
    film_list = create_list(maker, title="Reply To Me", films=_films(2, prefix="Rep"))
    row = _row_for(_home("viewer"), film_list.status_id)
    assert 'class="reply-btn"' in row
    assert f'href="/status/{film_list.status_id}/?reply=1"' in row


@pytest.mark.django_db
def test_the_reply_link_on_a_list_row_actually_opens_the_composer(maker, viewer, admin):
    # The icon is a door; this proves the door opens. The row points at
    # /status/<id>/?reply=1, which 302s to the list page (L10), and the
    # list page opens its composer off that same param. A redirect that
    # rewrote only the path landed the reader on the list with the composer
    # shut — found in the browser, not in a unit test: the reply ROUTE
    # accepted the post the whole time, it was the hop that dropped the
    # asking. So the chain is asserted end to end, through the redirect.
    viewer.follows.add(maker)
    film_list = create_list(
        maker, title="Open The Composer", films=_films(2, prefix="OC")
    )
    href = f"/status/{film_list.status_id}/?reply=1"
    assert f'href="{href}"' in _row_for(_home("viewer"), film_list.status_id)

    hop = _login("viewer").get(href)
    assert hop.status_code == 302
    assert hop["Location"] == f"/list/{film_list.id}/?reply=1", (
        "the redirect dropped the query string, so the reply icon opens a "
        "page whose composer is closed"
    )

    landed = _login("viewer").get(hop["Location"])
    assert landed.status_code == 200
    assert "reply-form" in landed.content.decode()


@pytest.mark.django_db
def test_a_plain_visit_to_a_lists_status_url_still_redirects_cleanly(
    maker, viewer, admin
):
    # The other side of preserving the query: with nothing to preserve, the
    # redirect must not grow a bare "?" or change what increment 2 pinned.
    viewer.follows.add(maker)
    film_list = create_list(maker, title="No Query Here", films=_films(1, prefix="NQ"))
    hop = _login("viewer").get(f"/status/{film_list.status_id}/")
    assert hop.status_code == 302
    assert hop["Location"] == f"/list/{film_list.id}/"


@pytest.mark.django_db
def test_a_reply_to_a_list_face_through_the_real_route_lands(maker, viewer, admin):
    # Not just that the icon is there — that pressing it works. The offer and
    # the route have to be proven as a pair; the icon alone proves nothing.
    viewer.follows.add(maker)
    film_list = create_list(maker, title="Answer Me", films=_films(2, prefix="Ans"))
    face = film_list.status
    assert Status.objects.filter(reply_parent=face).count() == 0

    response = _login("viewer").post(
        f"/status/{face.id}/reply/", {"content": "Great list."}
    )
    assert response.status_code == 200
    replies = Status.objects.filter(reply_parent=face)
    assert replies.count() == 1
    assert replies[0].user_id == viewer.id
    # L13: the reply carries no status_type, because a list has no film to inherit.
    assert replies[0].status_type is None


@pytest.mark.django_db
def test_a_filmless_mirrored_note_row_gets_no_reply_control(carol, viewer, admin):
    # The direction that regresses silently: widen the gate on "no film" and
    # this row gains an icon that 400s. It is live today (statuses 60 and 31
    # on the owner's instance), so this is not a hypothetical shape.
    viewer.follows.add(carol)
    note = Status.objects.create(
        user=carol,
        film=None,
        status_type=None,
        content="<p>A mirrored note with no film.</p>",
        local=False,
        remote_url="https://remote.example/note/no-reply-icon",
    )
    row = _row_for(_home("viewer"), note.id)
    assert "A mirrored note with no film." in row  # the row rendered…
    assert 'class="reply-btn"' not in row  # …and offers nothing it cannot do
    assert "created a new list" not in row


@pytest.mark.django_db
def test_the_route_still_refuses_a_reply_to_a_filmless_mirrored_note(
    carol, viewer, admin
):
    viewer.follows.add(carol)
    note = Status.objects.create(
        user=carol,
        film=None,
        status_type=None,
        content="<p>A mirrored note with no film.</p>",
        local=False,
        remote_url="https://remote.example/note/refused-here",
    )
    response = _login("viewer").post(f"/status/{note.id}/reply/", {"content": "hi"})
    assert response.status_code == 400
    assert Status.objects.filter(reply_parent=note).count() == 0


@pytest.mark.django_db
def test_a_film_anchored_row_still_gets_its_reply_control(maker, viewer, admin):
    # The gate widening must not have closed the ordinary case on its way
    # past. A review row keeps the icon it has had since 2026-10-05.
    viewer.follows.add(maker)
    film = Film.objects.create(title="A Normal Review", year=1958)
    status = mark_watched(maker, film, rating="4", content="<p>Solid.</p>")
    row = _row_for(_home("viewer"), status.id)
    assert 'class="reply-btn"' in row
    assert "list-strip" not in row


# --- Paging and the shared partial ------------------------------------------


@pytest.mark.django_db
def test_a_list_row_pages_off_the_right_row(maker, viewer, admin):
    # The cursor keys on source_id, which for a status row IS the status.
    # Walking the pages must reproduce the whole feed exactly, list rows
    # included — no gap, no duplicate, no reordering.
    viewer.follows.add(maker)
    create_list(maker, title="Paged List One", films=_films(3, prefix="P1"))
    create_list(maker, title="Paged List Two", films=_films(7, prefix="P2"))
    assert all_pages(viewer, limit=5) == unpaged(viewer)


@pytest.mark.django_db
def test_the_fragment_renders_the_list_row_identical_to_the_home_page(
    maker, viewer, admin
):
    # _feed_row.html is shared by / and /feed/page/, so the list row cannot
    # be shaped on one and forgotten on the other.
    viewer.follows.add(maker)
    film_list = create_list(
        maker,
        title="Same Row Both Routes",
        films=_films(LIST_FEED_POSTER_CAP + 1, prefix="Frag"),
    )
    home = _home("viewer")
    resp = _login("viewer").get("/feed/page/")
    assert resp.status_code == 200
    fragment = resp.content.decode()

    def norm(html):
        return "\n".join(line.strip() for line in html.split("\n") if line.strip())

    assert norm(_row_for(home, film_list.status_id)) == norm(
        _row_for(fragment, film_list.status_id)
    )


# --- The query cost ---------------------------------------------------------


def _list_query_count(who):
    """``(list rows, queries against the lists tables)`` for one feed build."""
    with CaptureQueriesContext(connection) as ctx:
        entries = unpaged(who)
    return (
        len([e for e in entries if e.is_list_face]),
        len(
            [
                q
                for q in ctx.captured_queries
                if "lists_filmlist" in q["sql"] or "lists_listitem" in q["sql"]
            ]
        ),
    )


@pytest.mark.django_db
def test_the_poster_strip_is_batched_not_one_query_per_list_row(maker, viewer, admin):
    # The brief's standing warning: the home feed already spends ~1,378
    # queries on identical core_shelf lookups. A per-row poster lookup here
    # would add a second N+1 on the busiest page in the app, so the cost is
    # asserted rather than assumed.
    viewer.follows.add(maker)

    def grow_to(total):
        made = FilmList.objects.filter(user=maker).count()
        for i in range(made, total):
            create_list(maker, title=f"Batched {i}", films=_films(3, prefix=f"B{i}"))

    grow_to(1)
    rows_small, queries_small = _list_query_count(viewer)
    grow_to(5)
    rows_large, queries_large = _list_query_count(viewer)

    assert (rows_small, rows_large) == (1, 5)
    assert queries_small > 0, "the strip lookups never ran — the test is vacuous"
    assert queries_small == queries_large, (
        f"the strip costs {queries_small} queries for 1 list row and "
        f"{queries_large} for 5 — that is an N+1"
    )


@pytest.mark.django_db
def test_no_list_query_runs_when_the_feed_holds_no_list(viewer, admin):
    # The guard's other half: a feed with no list row must not pay for the
    # strip at all.
    rows, queries = _list_query_count(viewer)
    assert rows == 0
    assert queries == 0


# --- The counts the row already carried -------------------------------------


@pytest.mark.django_db
def test_a_list_row_carries_its_like_and_reply_counts(maker, viewer, admin):
    # Verified rather than assumed: the like and reply lookups key on
    # status_id, so a list face gets correct tallies with no change.
    viewer.follows.add(maker)
    third = member(localname="third", password=PASSWORD)
    film_list = create_list(maker, title="Counted", films=_films(2, prefix="Cnt"))
    face = film_list.status
    Like.objects.create(user=viewer, status=face)
    Status.objects.create(
        user=third,
        film=None,
        status_type=None,
        content="<p>Nice one.</p>",
        reply_parent=face,
    )
    entry = _entry_for(maker, face.id)
    assert entry.like_count == 1
    assert entry.reply_count == 1
    assert entry.is_list_face is True
    # ``liked_by_viewer`` is about WHOSE feed this is: maker authored the
    # list, viewer liked it, so maker's row must not claim the like as own.
    assert entry.liked_by_viewer is False
    assert _entry_for(viewer, face.id).liked_by_viewer is True
