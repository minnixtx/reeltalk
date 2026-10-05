"""Endless scroll on the home feed (§2I increment 3, R132).

This file covers what a browser cannot tell you faster: that there is exactly one
copy of the row markup, that the fragment route returns what the cursor names,
and that the fragment and the full page agree byte-for-byte. The behaviour — rows
appearing on scroll, a Like working on a row that did not exist at page load — is
proven in a real browser; the scripts and their results are recorded in PROGRESS.md
§2I.

The load-bearing constraint is R132 decision 5: **one template, one row.** The fragment
route and the home page include the same ``_feed_row.html``, so the
``.review-open`` overlay, its ``status_id`` gate and its ``aria-label`` cannot
become two things. Two tests guard that from different directions — a
source-level one that fails the moment a second copy of the markup appears
anywhere, and a rendered one that fails when two copies stop agreeing. A
hand-copied duplicate that happens to match today would pass the second and fail
the first, which is why both are here.

Also guarded: the fragment is a fragment (no document wrapper to splice into a
``<ul>``), the end marker is a ``<li>`` for the same reason, the scroll pushes no
URLs, and the no-JS "Older" link is only ever hidden when the browser can
actually run the scroll.
"""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.test import Client
from django.utils import timezone

import reeltalk.social.views as social_views
from reeltalk.core.models import FEED_PAGE_SIZE, feed_entries
from reeltalk.tests.feed import spread_shelve, unpaged
from reeltalk.tests.members import member, site_admin

PAGE = FEED_PAGE_SIZE
TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
LIKES_JS = Path(__file__).resolve().parents[1] / "core" / "static" / "js" / "likes.js"
FEED_JS = Path(__file__).resolve().parents[1] / "core" / "static" / "js" / "feed.js"


@pytest.fixture
def admin(db):
    # R12: / redirects to /setup/ until a superuser exists.
    return site_admin(localname="admin", password="s3cretpass")


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


# --- Helpers ---------------------------------------------------------------


def _norm(html: str) -> str:
    """Compare modulo indentation and blank lines.

    Required because Django's ``include`` does not reindent: the same partial
    picked up at two different call-site depths comes back with different leading
    whitespace on every line. Same normalisation the increment-1 footer
    comparison needed, for the same reason.
    """
    return "\n".join(line.strip() for line in html.split("\n") if line.strip())


def _member_client() -> Client:
    client = Client()
    assert client.login(username="alice", password="s3cretpass"), (
        "alice did not sign in — the page below would render anonymously, "
        "and every assertion on it would pass vacuously"
    )
    return client


def _feed_region(body: str) -> str:
    """The ``<ul class="review-list">`` inner HTML, scoped so the rail's film
    links cannot be mistaken for feed rows.

    The opening tag is skipped to its closing ``>`` rather than to the end of
    the class attribute, because the element also carries an ``id`` — leaving it
    in the region would put ``id="feed-list">`` inside what every comparison
    below treats as the rows.
    """
    opener = '<ul class="review-list"'
    start = body.index(">", body.index(opener)) + 1
    end = body.index("</ul>", start)
    return body[start:end]


def _rows(html: str):
    return re.findall(r'<li class="review"', html)


def _fragment(client, cursor=None) -> str:
    url = "/feed/page/" + (f"?c={cursor}" if cursor else "")
    resp = client.get(url)
    assert resp.status_code == 200, f"{url} did not render"
    return resp.content.decode()


# --- One row, one copy ------------------------------------------------------


def test_no_second_copy_of_the_row_markup_exists():
    """The structural half of decision 5.

    The rendered-equality test below catches two copies that have drifted apart.
    This catches the copy itself — a hand-duplicated row that matches perfectly
    today still fails here, because it is a second thing someone now has to keep
    in step.
    """
    for name in ("home.html", "_feed_page.html"):
        src = (TEMPLATES / name).read_text()
        assert '<li class="review"' not in src, (
            f"{name} carries its own copy of the feed row instead of including "
            "the shared partial"
        )
        assert '{% include "_feed_row.html" %}' in src, (
            f"{name} no longer includes the shared row partial"
        )


def test_the_fragment_template_body_is_the_home_page_list_body():
    """The two templates render their lists from literally the same two lines."""
    home = (TEMPLATES / "home.html").read_text()
    opener = '<ul class="review-list"'
    start = home.index(">", home.index(opener)) + 1
    home_body = home[start : home.index("</ul>", start)]

    frag = (TEMPLATES / "_feed_page.html").read_text()
    frag_body = frag[frag.index("{% endcomment %}") + len("{% endcomment %}") :]

    assert _norm(home_body) == _norm(frag_body)


def test_likes_js_binds_one_delegated_listener_not_one_per_button():
    """The trap the first appended rows would have sprung: ``init()`` runs once
    at ``DOMContentLoaded``, so a listener bound per ``.applaud-btn`` there
    leaves every applaud button on a row appended later dead. Delegation is the
    fix, and this pins it at the source."""
    src = LIKES_JS.read_text()
    assert 'querySelectorAll(".applaud-btn")' not in src, (
        "likes.js binds a listener per button again — applaud buttons on rows "
        "the endless scroll appends after page load would be dead"
    )
    assert 'document.addEventListener("click"' in src
    assert 'closest(".applaud-btn")' in src


def test_the_scroll_js_pushes_no_urls():
    """R132 decision 6: the URL stays ``/`` while scrolling. No pushState, no
    replaceState, no hash writes."""
    src = FEED_JS.read_text()
    for forbidden in ("pushState", "replaceState", "location.hash"):
        assert forbidden not in src, (
            f"feed.js touches {forbidden}; scrolling must push nothing"
        )


def test_the_scroll_js_stops_at_the_dom_ceiling_and_reveals_the_link():
    """R132 decision 4, pinned in CI.

    The behaviour itself — growing to 300 rows and then handing control back to
    the link — is proven in a real browser against a 320-entry feed; this only
    guards the two lines that make it happen, because a browser run cannot be
    part of the gate. Removing the ceiling block was tried against that proof and
    turned it red: the feed grew to all 320 rows with ``#feed-more`` still
    hidden, which is the silent stop this decision exists to prevent.
    """
    src = FEED_JS.read_text()
    assert "var CEILING = 300;" in src
    assert 'list.querySelectorAll("li.review").length >= CEILING' in src
    # The reveal has to be the ``stop(true)`` branch. ``stop(false)`` would
    # disconnect the observer and leave the reader with neither way forward.
    ceiling_branch = src.split(">= CEILING", 1)[1].split("}", 2)[0]
    assert "stop(true)" in ceiling_branch, (
        "the ceiling must un-hide the Older link, not just stop growing"
    )


# --- The fragment route ----------------------------------------------------


def test_the_fragment_is_a_fragment_not_a_page(alice, admin):
    spread_shelve(alice, PAGE + 5, timezone.now() - timedelta(hours=3))
    html = _member_client().get("/feed/page/").content.decode()
    assert len(_rows(html)) == PAGE
    for wrapper in ("<html", "<body", "<ul", "<nav", "site-footer"):
        assert wrapper not in html, (
            f"the fragment carries {wrapper!r}; it is spliced into the page's "
            "existing <ul>, so anything around these rows is invalid markup"
        )


def test_the_fragment_route_requires_a_member(db):
    resp = Client().get("/feed/page/")
    assert resp.status_code == 302
    assert "/login/" in resp.url


def test_the_fragment_returns_the_page_the_cursor_names(alice, admin):
    spread_shelve(alice, PAGE * 2 + 5, timezone.now() - timedelta(hours=3))
    client = _member_client()
    _, cursor = feed_entries(alice, limit=PAGE)
    expected, _ = feed_entries(alice, limit=PAGE, cursor=cursor)
    html = _fragment(client, cursor)
    assert re.findall(r'href="/film/(\d+)/"', html) == [
        str(e.film.pk) for e in expected
    ]


def test_a_non_final_fragment_carries_no_end_marker(alice, admin):
    spread_shelve(alice, PAGE * 2 + 5, timezone.now() - timedelta(hours=3))
    _, cursor = feed_entries(alice, limit=PAGE)
    assert 'class="feed-end"' not in _fragment(_member_client(), cursor)


def test_the_final_fragment_carries_the_end_marker(alice, admin):
    spread_shelve(alice, PAGE + 3, timezone.now() - timedelta(hours=3))
    _, cursor = feed_entries(alice, limit=PAGE)
    html = _fragment(_member_client(), cursor)
    # A <li>, because it is appended inside the feed's <ul>.
    assert '<li class="feed-end">' in html


def test_every_row_carries_its_own_cursor_and_it_round_trips(alice, admin):
    """The scroll reads "where to next" off the last row it appended, so a row
    whose cursor did not round-trip would send the browser somewhere wrong."""
    spread_shelve(alice, 3, timezone.now() - timedelta(hours=3))
    html = _fragment(_member_client())
    cursors = re.findall(r'<li class="review"[^>]*data-cursor="([^"]+)"', html)
    assert len(cursors) == 3
    assert cursors == [e.cursor for e in unpaged(alice)]


# --- The two agree ---------------------------------------------------------


def test_the_fragment_concatenation_equals_the_full_page_list(
    alice, admin, monkeypatch
):
    """The anti-drift test decision 5 exists for.

    Every fragment page concatenated is identical — modulo the indentation
    ``include`` does not adjust — to the whole feed rendered inside the home
    page's own ``<ul>``. If the fragment route grew its own row markup, this is
    the test that says so.
    """
    spread_shelve(alice, PAGE * 2 + 5, timezone.now() - timedelta(hours=3))
    client = _member_client()

    fragments = []
    cursor = None
    while True:
        fragments.append(_fragment(client, cursor))
        _, cursor = feed_entries(alice, limit=PAGE, cursor=cursor)
        if cursor is None:
            break
    assert len(fragments) == 3, "fixture expects three fragment pages"

    # The full page at a page size that swallows the whole feed.
    monkeypatch.setattr(social_views, "FEED_PAGE_SIZE", 10_000)
    full = client.get("/").content.decode()

    assert _norm("".join(fragments)) == _norm(_feed_region(full))


# --- The page the browser gets first ---------------------------------------


def test_the_home_page_renders_the_sentinel_with_the_next_cursor(alice, admin):
    spread_shelve(alice, PAGE + 5, timezone.now() - timedelta(hours=3))
    _, cursor = feed_entries(alice, limit=PAGE)
    body = _member_client().get("/").content.decode()
    assert 'id="feed-list"' in body
    assert 'id="feed-older"' in body
    assert f'data-next="{cursor}"' in body


def test_a_single_page_feed_offers_the_scroll_nothing(alice, admin):
    """No next page means an empty sentinel cursor, so the scroll never starts,
    there is no Older link to hide, and the end marker is what the reader sees."""
    spread_shelve(alice, 3, timezone.now() - timedelta(hours=3))
    body = _member_client().get("/").content.decode()
    assert 'data-next=""' in body
    assert 'class="feed-end"' in body
    assert 'id="feed-older"' not in body


def test_the_end_marker_is_inside_the_list_on_the_full_page(alice, admin):
    """It has to be a list item, not a paragraph after the ``</ul>`` — the same
    constraint that makes it a ``<li>`` in the appended fragment."""
    spread_shelve(alice, 2, timezone.now() - timedelta(hours=3))
    body = _member_client().get("/").content.decode()
    assert 'class="feed-end"' in _feed_region(body)


def test_the_scroll_scripts_are_only_sent_to_members(admin):
    """Anonymous visitors get the landing CTA, not a scroll wired to a route
    they cannot reach."""
    body = Client().get("/").content.decode()
    assert "feed.js" not in body
    assert "likes.js" not in body


def test_an_empty_feed_renders_no_list_and_no_scroll_state(alice, admin):
    body = _member_client().get("/").content.decode()
    assert "Nothing here yet" in body
    assert 'id="feed-sentinel"' not in body
