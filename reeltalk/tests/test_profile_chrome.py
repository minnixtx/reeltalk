"""Shared profile chrome (new-chrome pass).

The Films and Lists pages used to wear a bare header - avatar and name only -
while the profile carried the counts, the follow controls, and the Reviews /
Films / Lists strip. All three surfaces now include the same two partials and
build them from one view helper (``_profile_head_context``), so the header
cannot answer differently per page. Pinned here:

* the strip on each page with the correct active tab, and the Lists-tab rule
  (hidden at zero lists on the profile and films pages, never on the lists
  page itself, where hiding it would hide the tab you are standing on);
* the header block byte-identical to the profile's for every viewer class -
  anonymous, self, visitor, follower - which is the whole promise of sharing
  one partial and one context builder;
* the controls each viewer is owed on the two pages that previously showed
  none: Edit on one's own, Follow on a stranger's, nothing when signed out.

The shelf tabs (All films / Watchlist / Watched) and the self-only My Lists /
Saved Lists row are pinned in their own files; they sit below the strip now,
unchanged.
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
    """The shared Reviews / Films / Lists nav, and nothing else.

    It is the first ``.tabs`` nav on all three pages; any page-specific tab
    row (the films shelves, the self-only lists split) sits below it, so
    first-match reads the strip on every page these tests visit.
    """
    return re.search(r'<nav class="tabs">.*?</nav>', body, re.S).group(0)


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
def test_lists_tab_is_hidden_at_zero_lists_but_never_on_the_lists_page(client, alice):
    """R138 decision 3 on the films page, and its one exception.

    A tab that leads nowhere stays hidden on a page that is not it; on the
    lists page itself the strip always shows the active tab, because a
    strip that hides the tab you are standing on says you are nowhere.
    """
    films_strip = _strip(_body(client.get("/user/alice/films/")))
    assert "Lists" not in films_strip
    lists_strip = _strip(_body(client.get("/user/alice/lists/")))
    assert '<a href="/user/alice/lists/" class="active">Lists</a>' in lists_strip

    create_list(alice, title="Picks")
    assert '<a href="/user/alice/lists/">Lists</a>' in _strip(
        _body(client.get("/user/alice/films/"))
    )


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
