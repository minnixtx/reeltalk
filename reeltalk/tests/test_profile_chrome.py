"""Shared profile chrome (new-chrome pass).

The Films and Lists pages used to wear a bare header - avatar and name only -
while the profile carried the counts, the follow controls, and the tab strip.
All three surfaces now include the same header partial and build it from one
view helper (``_profile_head_context``), so the header cannot answer
differently per page. Pinned here:

* the Reviews / Films strip on the profile and films pages with the correct
  active tab, and - the point of the tests below - that one reader gets one
  strip for one member, so it cannot change between the tabs it spans;
* the Lists-tab rule where that tab still lives: another member's pages only,
  and there hidden at zero lists (the viewer's own menu already carries their
  lists, so on your own pages the tab is a duplicate and never appears);
* that the lists page wears the shared strip on somebody else's account - with
  its own tab lit - and nothing but the My Lists / Saved Lists row on your own;
* the header block byte-identical to the profile's for every viewer class -
  anonymous, self, visitor, follower - which is the whole promise of sharing
  one partial and one context builder;
* the controls each viewer is owed on the two pages that previously showed
  none: Edit on one's own, Follow on a stranger's, nothing when signed out.

The shelf tabs (All films / Watchlist / Watched) and the self-only My Lists /
Saved Lists row are pinned in their own files; the shelf row sits below the
strip on the films page, and the lists row is the only tab bar the lists page
wears.
"""

import re

import pytest
from django.test import Client

from reeltalk.lists.services import create_list
from reeltalk.tests.members import member


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return member(localname="bob", password="s3cretpass")


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


def _body(response) -> str:
    assert response.status_code == 200
    return response.content.decode()


def _strip(body: str) -> str:
    """The shared Reviews / Films nav, and nothing else.

    It is the first ``.tabs`` nav on the profile and films pages; the films
    page's own shelf row (All films / Watchlist / Watched) sits below it, so
    first-match reads the strip on both pages this helper is used on.
    """
    return re.search(r'<nav class="tabs">.*?</nav>', body, re.S).group(0)


def _strips(body: str) -> list[str]:
    """Every ``.tabs`` nav on the page, in document order.

    Used where the count is the claim - a page that is meant to wear exactly
    one tab bar fails here on a second one appearing, which a first-match
    read would sail straight past.
    """
    return re.findall(r'<nav class="tabs">.*?</nav>', body, re.S)


def _labels(nav: str) -> list[str]:
    """The tab labels of one nav, in order.

    Comparing labels rather than markup is what lets the profile and films
    pages be compared at all: the same strip on both differs in which tab
    carries ``class="active"``, and that difference is not the thing under
    test - which tabs are offered is.
    """
    return re.findall(r">([^<]+)</a>", nav)


def _shared_labels(response) -> list[str]:
    """The shared strip's labels on a page, or ``[]`` where it is not worn.

    Found by content rather than position: on your own lists page the only
    ``.tabs`` nav is the My Lists / Saved Lists row, so "the first nav" is not
    "the shared strip" on every page these tests visit. An empty answer is a
    real answer here - it is the shape your own lists page is meant to have.
    """
    for nav in _strips(_body(response)):
        if ">Reviews<" in nav:
            return _labels(nav)
    return []


def _header(body: str) -> str:
    """The ``profile-head`` block with the CSRF nonce normalised.

    Two renderings of the same form carry different nonces, so a raw
    byte-compare of two pages would fail on the token alone. Everything
    else in the block is the comparison - counts, buttons, form actions.
    """
    block = re.search(r'<header class="profile-head">.*?</header>', body, re.S)
    return re.sub(r'name="csrfmiddlewaretoken" value="[^"]*"', "", block.group(0))


# --- the strip -----------------------------------------------------------------


@pytest.mark.django_db
def test_films_page_strip_carries_films_active(client, alice):
    body = _body(client.get("/user/alice/films/"))
    strip = _strip(body)
    assert '<a href="/user/alice/films/" class="active">Films</a>' in strip
    assert '<a href="/user/alice/">Reviews</a>' in strip


@pytest.mark.django_db
def test_the_strip_is_the_same_on_every_page_of_another_members_account(alice, bob):
    """The strip may not change between the tabs it spans (owner report,
    2026-11-06, twice over).

    Going to My Films and then clicking Reviews used to bring a Lists tab back
    that the films page had not shown, and clicking Lists used to drop the
    whole strip. Keyed on the viewer rather than the page, one reader gets one
    strip for one member: the same tabs in the same order on the profile, the
    films page and the lists page, with only the lit one moving.
    """
    create_list(alice, title="Alice Picks")
    create_list(bob, title="Bob Picks")
    readers = (
        ("anonymous", Client()),
        ("alice", _login("alice")),
        ("bob", _login("bob")),
    )
    for reader_name, reader in readers:
        for maker in (alice, bob):
            if reader_name == maker.localname:
                continue  # one's own pages are the next test's business
            pages = [
                _shared_labels(reader.get(f"/user/{maker.localname}/")),
                _shared_labels(reader.get(f"/user/{maker.localname}/films/")),
                _shared_labels(reader.get(f"/user/{maker.localname}/lists/")),
            ]
            assert pages[0] == pages[1] == pages[2], (reader_name, maker.localname)


@pytest.mark.django_db
def test_your_own_pages_carry_no_lists_tab(client, alice):
    """The sidebar and the phone bar each carry a Lists row that points at the
    viewer's own lists, so on your own pages the tab only duplicates a row
    already on screen. It stays off whatever you have made, and your own lists
    page wears no shared strip at all - the My Lists / Saved Lists row is what
    says where you are there.
    """
    login = _login("alice")
    create_list(alice, title="Picks")
    for path in ("/user/alice/", "/user/alice/films/"):
        assert "Lists" not in _strip(_body(login.get(path)))
    assert _shared_labels(login.get("/user/alice/lists/")) == []


@pytest.mark.django_db
def test_another_members_pages_carry_the_lists_tab_above_zero_lists(client, alice, bob):
    """On somebody else's pages the tab is the only way onto their lists - no
    menu row reaches another member's - so it is on all three of them, and it
    still follows R138 decision 3: hidden while the member has made none,
    because a tab that leads nowhere is worse than one that appears when there
    is something behind it.
    """
    login = _login("alice")
    for path in ("/user/bob/", "/user/bob/films/"):
        assert "Lists" not in _strip(_body(login.get(path)))

    create_list(bob, title="Picks")
    for path in ("/user/bob/", "/user/bob/films/", "/user/bob/lists/"):
        assert 'href="/user/bob/lists/"' in _strip(_body(login.get(path)))


@pytest.mark.django_db
def test_the_lists_page_lights_the_tab_you_are_standing_on(client, alice, bob):
    """The lists page is not a dead end (owner report, 2026-11-06).

    With the My Lists / Saved Lists row self-only, a visitor on somebody
    else's lists page had no navigation on it at all and could leave only with
    the browser's back button. The shared strip goes there, and the tab it
    lights is the one you are standing on - lit even at zero lists, because a
    strip that hides the tab you are on says you are nowhere.
    """
    strip = _strip(_body(_login("alice").get("/user/bob/lists/")))
    assert '<a href="/user/bob/lists/" class="active">Lists</a>' in strip
    assert '<a href="/user/bob/">Reviews</a>' in strip
    assert '<a href="/user/bob/films/">Films</a>' in strip


@pytest.mark.django_db
def test_your_own_lists_page_wears_only_its_own_tab_row(client, alice):
    """On your own lists page the shared strip stays out (owner decision,
    2026-11-06): the page's one tab row is the My Lists / Saved Lists split,
    and the menu already reaches every other page of your own."""
    rows = _strips(_body(_login("alice").get("/user/alice/lists/")))
    assert len(rows) == 1
    assert "Reviews" not in rows[0]
    assert "Saved Lists" in rows[0]


# --- the header is one answer for three pages ----------------------------------

OTHER_PAGES = ("/user/alice/films/", "/user/alice/lists/")


@pytest.mark.django_db
def test_the_header_matches_the_profile_for_every_viewer(alice, bob):
    """Anonymous, self, plain visitor, follower: on the films and lists pages
    the header block equals the profile's, nonce aside. That equality is the
    reason the partial and ``_profile_head_context`` exist - three pages
    cannot drift on who sees what when they render one block."""
    bob.follows.add(alice)  # follower state is set before every read below
    for who, login in (
        ("anonymous", Client()),
        ("self", _login("alice")),
        ("visitor", _login("bob")),
    ):
        profile_head = _header(_body(login.get("/user/alice/")))
        for path in OTHER_PAGES:
            assert _header(_body(login.get(path))) == profile_head, (who, path)


# --- the controls each viewer is owed ------------------------------------------


@pytest.mark.django_db
def test_self_sees_edit_profile_and_no_follow(alice):
    login = _login("alice")
    for path in OTHER_PAGES:
        body = _body(login.get(path))
        assert "Edit profile" in body
        assert ">Follow</button>" not in body


@pytest.mark.django_db
def test_visitor_sees_follow_and_no_edit(alice, bob):
    login = _login("bob")
    for path in OTHER_PAGES:
        body = _body(login.get(path))
        assert ">Follow</button>" in body
        assert "Edit profile" not in body


@pytest.mark.django_db
def test_a_follower_sees_following_on_the_films_page(alice, bob):
    """The follow state is shared, not just the markup: a member you already
    follow must not get a second Follow button on the films page."""
    bob.follows.add(alice)
    body = _body(_login("bob").get("/user/alice/films/"))
    assert ">Following</button>" in body
    assert ">Follow</button>" not in body


@pytest.mark.django_db
def test_anonymous_sees_neither_edit_nor_follow(client, alice):
    for path in OTHER_PAGES:
        body = _body(client.get(path))
        assert "Edit profile" not in body
        assert ">Follow</button>" not in body


# --- the counts -----------------------------------------------------------------


@pytest.mark.django_db
def test_follower_counts_render_on_both_pages(client, alice, bob):
    bob.follows.add(alice)
    for path in OTHER_PAGES:
        body = _body(client.get(path))
        assert "1 Follower" in body
        assert "0 Following" in body
