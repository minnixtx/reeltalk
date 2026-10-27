"""The shared navigation structure (N2).

One registry (``reeltalk/social/navigation.py``) defines the nav row once and
``templates/_navigation.html`` renders it, so the header shipped today and the
sidebar/bottom bar/More of N3/N4 read the same destinations. What is pinned
here is the contract the extraction must not break:

* the registry's shape per viewer: anonymous gets Home alone; a member gets
  the six link destinations plus the Log out control, in the order the old
  hand-written row used;
* active state is exact path equality -- the lists page lights My Lists
  alone and a list's own page lights nothing (R138's rule, now in Python);
* the rendered row marks the active item with ``aria-current="page"`` and
  marks nothing else;
* Log out is a POST form carrying CSRF, never a GET link;
* the row is one labeled landmark, rendered once, from the shared partial.

The corner (search box, badge, Moderate, avatar) is untouched by N2 and
pinned by ``test_search.py``, ``test_notifications_badge.py`` and
``test_moderator_gate.py``; it is not duplicated here.
"""

import re

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory

from reeltalk.social.navigation import primary_nav
from reeltalk.tests.members import member

PASSWORD = "s3cretpass"


@pytest.fixture
def alice(db):
    return member(localname="alice", password=PASSWORD)


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password=PASSWORD)
    return client


def _site_nav(body: str) -> str:
    """The header's own ``.site-nav`` row, and nothing else.

    Scoped for the same reason ``test_lists_views._tabs`` scopes the page's
    tab bar: a header contract read off the whole body could be satisfied by
    other markup and say nothing about the header.
    """
    match = re.search(r'<nav class="site-nav".*?</nav>', body, re.S)
    assert match, "no .site-nav row rendered"
    return match.group(0)


def _request(path: str, user=None):
    request = RequestFactory().get(path)
    request.user = user if user is not None else AnonymousUser()
    return request


# --- the registry (the single source) ---------------------------------------


@pytest.mark.django_db
def test_the_registry_gives_anonymous_only_home():
    (item,) = primary_nav(_request("/"))
    assert (item.key, item.label, item.url, item.current) == ("home", "Home", "/", True)


@pytest.mark.django_db
def test_the_registry_gives_a_member_every_destination_in_order(alice):
    items = primary_nav(_request("/find/", alice))
    assert [(i.key, i.label) for i in items] == [
        ("home", "Home"),
        ("films", "My Films"),
        ("lists", "My Lists"),
        ("find", "Find People"),
        ("import", "Import"),
        ("export", "Export"),
        ("logout", "Log out"),
    ]
    assert [i.url for i in items] == [
        "/",
        "/user/alice/films/",
        "/user/alice/lists/",
        "/find/",
        "/preferences/import/",
        "/preferences/export/",
        "/logout/",
    ]
    # Exactly one item is lit, and it is the page being viewed.
    assert [i.key for i in items if i.current] == ["find"]
    # Log out acts, so it is flagged as the form control, never a link.
    assert items[-1].kind == "form"


@pytest.mark.django_db
def test_active_state_is_exact_path_equality(alice):
    """A list's own page lights no nav item (R138): equality, not prefix."""
    assert [i.key for i in primary_nav(_request("/list/1/", alice)) if i.current] == []
    items = primary_nav(_request("/user/alice/lists/", alice))
    assert [i.key for i in items if i.current] == ["lists"]


def test_the_processor_is_registered():
    processors = settings.TEMPLATES[0]["OPTIONS"]["context_processors"]
    assert "reeltalk.social.navigation.nav_items" in processors


def test_base_html_defers_the_row_to_the_shared_partial():
    """The single-source guard: base.html includes the partial and keeps no
    nav item of its own, so a future edit cannot fork the row back into it."""
    base = (settings.BASE_DIR / "templates" / "base.html").read_text()
    assert '{% include "_navigation.html" %}' in base
    assert ">My Lists<" not in base
    assert ">Find People<" not in base


# --- the rendered row --------------------------------------------------------


@pytest.mark.django_db
def test_the_member_row_renders_every_destination(alice):
    nav = _site_nav(_login("alice").get("/find/").content.decode())
    for href in (
        'href="/"',
        'href="/user/alice/films/"',
        'href="/user/alice/lists/"',
        'href="/find/"',
        'href="/preferences/import/"',
        'href="/preferences/export/"',
    ):
        assert href in nav
    assert ">Log out</button>" in nav


@pytest.mark.django_db
def test_the_active_item_is_marked_aria_current_and_nothing_else_is(alice):
    nav = _site_nav(_login("alice").get("/find/").content.decode())
    lit = '<a href="/find/" class="active" aria-current="page">Find People</a>'
    assert lit in nav
    assert nav.count('aria-current="page"') == 1


@pytest.mark.django_db
def test_a_page_outside_the_row_lights_nothing(alice):
    nav = _site_nav(_login("alice").get("/about/").content.decode())
    assert "aria-current" not in nav
    assert 'class="active"' not in nav


@pytest.mark.django_db
def test_the_anonymous_row_carries_home_only(db):
    nav = _site_nav(Client().get("/about/").content.decode())
    assert '<a href="/">Home</a>' in nav
    assert "<form" not in nav
    assert "My Films" not in nav


@pytest.mark.django_db
def test_log_out_is_a_post_form_with_csrf_never_a_get_link(alice):
    body = _login("alice").get("/find/").content.decode()
    nav = _site_nav(body)
    assert '<form method="post" action="/logout/"' in nav
    assert 'name="csrfmiddlewaretoken"' in nav
    # No GET path to a POST-only route, anywhere on the page.
    assert 'href="/logout/"' not in body


@pytest.mark.django_db
def test_the_row_is_one_labeled_landmark_rendered_once(alice):
    body = _login("alice").get("/find/").content.decode()
    assert body.count('<nav class="site-nav" aria-label="Main">') == 1
