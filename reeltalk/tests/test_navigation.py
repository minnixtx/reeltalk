"""The shared navigation structure (N2, reshaped by N4).

One registry (``reeltalk/social/navigation.py``) defines the member
destinations once and ``templates/_navigation.html`` renders the desktop menu
from it, so the left menu shipped today and the phone bar and More sheet
(N3) read the same destinations. What is pinned here is the contract the
extraction must not break:

* the registry's shape per viewer: a signed-out visitor gets no menu at all
  (N1 decision 8 -- the wordmark is the way home and the corner carries Log
  in / Sign up), and a member gets the seven rows the approved design lists,
  in its order, with Log out last;
* active state is exact path equality -- the lists page lights Lists alone
  and a list's own page lights nothing (R138's rule, now in Python);
* the rendered menu marks the active item with ``aria-current="page"`` and
  marks nothing else;
* Log out is a POST form carrying CSRF, never a GET link;
* the menu is one labeled landmark, rendered once, from the shared partial,
  and every row carries the mockup's icon;
* the group rules come from ``NavItem.group``, not from markup that would
  have to be edited in step with the list.

Import, Export and Moderate are absent from the menu by design (they live
inside Profile / Settings), so their absence is pinned too: a future edit
that re-lists them here would silently undo the approved grouping.

The corner (search box, badge, Moderate, avatar) is untouched by N4 and
pinned by ``test_search.py``, ``test_notifications_badge.py`` and
``test_moderator_gate.py``; it is not duplicated here. The phone surfaces are
pinned by ``test_mobile_nav.py``.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.staticfiles import finders
from django.test import Client, RequestFactory

from reeltalk.social.navigation import nav_items, sidebar_nav
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
    """The desktop menu's own ``.site-nav`` landmark, and nothing else.

    Scoped for the same reason ``test_lists_views._tabs`` scopes the page's
    tab bar: a menu contract read off the whole body could be satisfied by
    other markup and say nothing about the menu.
    """
    match = re.search(r'<nav class="site-nav".*?</nav>', body, re.S)
    assert match, "no .site-nav menu rendered"
    return match.group(0)


def _request(path: str, user=None):
    request = RequestFactory().get(path)
    request.user = user if user is not None else AnonymousUser()
    return request


# --- the registry (the single source) ---------------------------------------


@pytest.mark.django_db
def test_the_registry_gives_a_signed_out_visitor_no_menu():
    assert sidebar_nav(_request("/")) == []


@pytest.mark.django_db
def test_the_registry_gives_a_member_every_row_in_the_approved_order(alice):
    items = sidebar_nav(_request("/find/", alice))
    assert [(i.key, i.label) for i in items] == [
        ("feed", "Feed"),
        ("films", "My Films"),
        ("lists", "Lists"),
        ("find", "Find People"),
        ("notifications", "Notifications"),
        ("settings", "Profile / Settings"),
        ("logout", "Log out"),
    ]
    assert [i.url for i in items] == [
        "/",
        "/user/alice/films/",
        "/user/alice/lists/",
        "/find/",
        "/notifications/",
        "/preferences/profile/",
        "/logout/",
    ]
    # Exactly one item is lit, and it is the page being viewed.
    assert [i.key for i in items if i.current] == ["find"]
    # Log out acts, so it is flagged as the form control, never a link.
    assert items[-1].kind == "form"


@pytest.mark.django_db
def test_the_rows_are_grouped_for_the_menu_dividers(alice):
    """The dividers are data, not template markup: ``group`` names the block
    each row sits in and the partial rules wherever the group changes."""
    assert [i.group for i in sidebar_nav(_request("/find/", alice))] == [
        "feed",
        "feed",
        "feed",
        "discover",
        "discover",
        "account",
        "session",
    ]


@pytest.mark.django_db
def test_import_export_and_moderate_ride_in_the_menu_by_design(alice):
    """The approved design nests these inside Profile / Settings."""
    keys = [i.key for i in sidebar_nav(_request("/find/", alice))]
    assert "import" not in keys
    assert "export" not in keys
    assert "moderate" not in keys


@pytest.mark.django_db
def test_active_state_is_exact_path_equality(alice):
    """A list's own page lights no menu item (R138): equality, not prefix."""
    assert [i.key for i in sidebar_nav(_request("/list/1/", alice)) if i.current] == []
    items = sidebar_nav(_request("/user/alice/lists/", alice))
    assert [i.key for i in items if i.current] == ["lists"]


def test_the_processor_is_registered():
    processors = settings.TEMPLATES[0]["OPTIONS"]["context_processors"]
    assert "reeltalk.social.navigation.nav_items" in processors


@pytest.mark.django_db
def test_the_processor_answers_all_three_surfaces_at_once(alice):
    """One processor feeds the desktop menu, the phone bar and the sheet, so
    a surface can never drift from the others by reading a second source."""
    context = nav_items(_request("/find/", alice))
    assert [i.key for i in context["sidebar_items"]][0] == "feed"
    assert [i.key for i in context["bottom_items"]] == [
        "feed",
        "search",
        "films",
        "lists",
    ]
    assert [i.key for i in context["more_items"]][-1] == "logout"


def test_base_html_defers_the_row_to_the_shared_partial():
    """The single-source guard: base.html includes the partial and keeps no
    nav item of its own, so a future edit cannot fork the menu back into it."""
    base = (settings.BASE_DIR / "templates" / "base.html").read_text()
    assert '{% include "_navigation.html" %}' in base
    assert ">My Lists<" not in base
    assert ">Find People<" not in base


# --- the rendered menu --------------------------------------------------------


@pytest.mark.django_db
def test_the_member_menu_renders_every_row(alice):
    nav = _site_nav(_login("alice").get("/find/").content.decode())
    for href in (
        'href="/"',
        'href="/user/alice/films/"',
        'href="/user/alice/lists/"',
        'href="/find/"',
        'href="/notifications/"',
        'href="/preferences/profile/"',
    ):
        assert href in nav
    assert ">Log out<" in nav


@pytest.mark.django_db
def test_every_row_carries_its_icon(alice):
    """The menu is the icon-and-label surface; a row that lost its drawing
    would render as a text-only row and read as a different design."""
    nav = _site_nav(_login("alice").get("/find/").content.decode())
    assert nav.count("<svg") == 7


@pytest.mark.django_db
def test_the_active_item_is_marked_aria_current_and_nothing_else_is(alice):
    nav = _site_nav(_login("alice").get("/find/").content.decode())
    assert '<a href="/find/" class="active" aria-current="page">' in nav
    assert nav.count('aria-current="page"') == 1


@pytest.mark.django_db
def test_a_page_outside_the_menu_lights_nothing(alice):
    nav = _site_nav(_login("alice").get("/about/").content.decode())
    assert "aria-current" not in nav
    assert 'class="active"' not in nav


@pytest.mark.django_db
def test_the_group_rules_are_drawn_where_the_group_changes(alice):
    """Three group changes (feed -> discover -> account -> session), so three
    rules -- and none before the first row."""
    nav = _site_nav(_login("alice").get("/find/").content.decode())
    assert nav.count("<hr>") == 3


@pytest.mark.django_db
def test_a_signed_out_visitor_gets_no_menu_at_all(db):
    body = Client().get("/about/").content.decode()
    assert '<nav class="site-nav"' not in body
    # Control: the page rendered, so the menu is what is absent, not the page.
    assert "Log in" in body


@pytest.mark.django_db
def test_log_out_is_a_post_form_with_csrf_never_a_get_link(alice):
    body = _login("alice").get("/find/").content.decode()
    nav = _site_nav(body)
    assert '<form method="post" action="/logout/"' in nav
    assert 'name="csrfmiddlewaretoken"' in nav
    # No GET path to a POST-only route, anywhere on the page.
    assert 'href="/logout/"' not in body


@pytest.mark.django_db
def test_the_menu_is_one_labeled_landmark_rendered_once(alice):
    body = _login("alice").get("/find/").content.decode()
    assert body.count('<nav class="site-nav" aria-label="Main">') == 1


# --- the stylesheet -----------------------------------------------------------


def _media_block(css: str, query: str) -> str:
    """The body of every ``@media`` block whose header contains ``query``."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    bodies = []
    for match in re.finditer(r"@media\s*([^{]+)\{", css):
        if query not in re.sub(r"\s+", "", match.group(1)):
            continue
        depth, i = 1, match.end()
        while depth:
            depth += css[i] == "{"
            depth -= css[i] == "}"
            i += 1
        bodies.append(css[match.end() : i - 1])
    assert bodies, f"no @media block matching {query!r}"
    return "".join(bodies)


def test_the_page_column_does_not_size_itself_to_the_text_on_it():
    """The ``width`` ``main`` wears in the desktop block is load-bearing.

    ``.container`` centres itself with ``margin: 0 auto``, and auto margins on a
    grid item defeat the default stretch: the item shrink-wraps to its content's
    max-content instead. N4 made ``body`` a grid, so from then on the page column
    sized itself to the length of the text on it. Measured at 1440px the Local
    feed rendered 486px against Following's 721px for an identical layout, which
    is the owner-reported "the feed changes width between scopes". As a block
    child of a block body the same declaration merely centred, which is why it
    took the grid to create the condition and two feed scopes to make it visible.

    ``width: 100%`` restores the fill; ``max-width`` still caps the column and
    the auto margins still centre it once capped. Re-measured after the fix at
    1440, 1280 and 1024 with all three scopes agreeing at each. This guard is here
    so the declaration is not read as redundant and tidied away.
    """
    css = Path(finders.find("css/reeltalk.css")).read_text()
    rule = re.search(r"main\s*\{[^}]*\}", _media_block(css, "min-width:1024px"))
    assert rule, "the desktop block no longer styles main at all"
    assert "width: 100%" in rule.group(0), rule.group(0)
