"""The phone surfaces (navigation redesign N3).

The bottom bar and the More sheet render from the same registry as the
desktop row (``reeltalk/social/navigation.py``), so what is pinned here is
the contract the new surfaces must hold:

* the two new registry lists: shape, order, labels, URLs, the exact-equality
  active rule, and icons on every item;
* the rendered surfaces: one bottom-bar landmark for members and nothing
  signed out; the active item alone carries ``aria-current="page"``;
* Log out inside the sheet is still the POST+CSRF form, never a GET link;
* the sheet's Moderate row follows the moderator flag, and the corner's
  desk Moderate sits in its ``.corner-desk`` wrapper;
* the badge anchor now carries the bell parts the stylesheet swaps per
  breakpoint (the ``notifications-link``/``moderation-link`` CSS bans in
  ``test_notifications_badge.py`` and ``test_moderator_gate.py`` stay green
  because the CSS names the corner's parts, never those classes);
* the search page carries its own field for phones, and the profile page
  is the settings hub (Import, Export, Moderate for moderators) with the
  edit form untouched;
* the mobile treatment lives under the 1024px line in the stylesheet.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.staticfiles import finders
from django.test import Client, RequestFactory

from reeltalk.notifications.models import Notification, notify
from reeltalk.social.navigation import bottom_nav, more_nav
from reeltalk.tests.members import member, moderator

PASSWORD = "s3cretpass"


@pytest.fixture
def alice(db):
    return member(localname="alice", password=PASSWORD)


@pytest.fixture
def wendy(db):
    return moderator(localname="wendy", password=PASSWORD)


@pytest.fixture
def bob(db):
    return member(localname="bob", password=PASSWORD)


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password=PASSWORD)
    return client


def _request(path: str, user=None):
    request = RequestFactory().get(path)
    request.user = user if user is not None else AnonymousUser()
    return request


def _bottom_nav(body: str) -> str:
    """The bottom bar region, scoped like ``test_navigation._site_nav``."""
    match = re.search(r'<nav class="bottom-nav".*?</nav>', body, re.S)
    assert match, "no .bottom-nav bar rendered"
    return match.group(0)


def _more_sheet(body: str) -> str:
    match = re.search(r'<dialog id="more-sheet".*?</dialog>', body, re.S)
    assert match, "no More sheet rendered"
    return match.group(0)


def _settings_links(body: str) -> str:
    match = re.search(r'<nav class="settings-links".*?</nav>', body, re.S)
    assert match, "no settings links nav rendered"
    return match.group(0)


def _header_user(content: str) -> str:
    """The ``.header-user`` corner, bounded like the badge test bounds it."""
    start = content.index('<div class="header-user">')
    return content[start : content.index("</div>", start)]


# --- the registry (the same single source) ------------------------------------


@pytest.mark.django_db
def test_the_bottom_bar_gives_a_member_four_destinations_in_order(alice):
    items = bottom_nav(_request("/user/alice/films/", alice))
    assert [(i.key, i.label) for i in items] == [
        ("feed", "Feed"),
        ("search", "Search"),
        ("films", "My Films"),
        ("lists", "Lists"),
    ]
    assert [i.url for i in items] == [
        "/",
        "/search/",
        "/user/alice/films/",
        "/user/alice/lists/",
    ]
    assert [i.key for i in items if i.current] == ["films"]
    # Every bar item carries the approved mockup's icon path.
    assert all(i.icon for i in items)


@pytest.mark.django_db
def test_the_bottom_bar_is_empty_signed_out():
    """No member bar for strangers (N1 decision 8): the list itself is empty."""
    assert bottom_nav(_request("/")) == []


@pytest.mark.django_db
def test_the_more_sheet_lists_its_destinations_with_log_out_last(alice):
    items = more_nav(_request("/find/", alice))
    assert [(i.key, i.label) for i in items] == [
        ("find", "Find People"),
        ("notifications", "Notifications"),
        ("settings", "Profile / Settings"),
        ("logout", "Log out"),
    ]
    assert [i.url for i in items] == [
        "/find/",
        "/notifications/",
        "/preferences/profile/",
        "/logout/",
    ]
    assert [i.key for i in items if i.current] == ["find"]
    assert items[-1].kind == "form"
    assert all(i.icon for i in items)


@pytest.mark.django_db
def test_moderate_joins_the_sheet_only_for_a_moderator(alice, wendy):
    assert "moderate" not in [i.key for i in more_nav(_request("/find/", alice))]
    assert [i.key for i in more_nav(_request("/find/", wendy))] == [
        "find",
        "notifications",
        "settings",
        "moderate",
        "logout",
    ]


# --- the rendered surfaces -----------------------------------------------------


@pytest.mark.django_db
def test_the_bottom_bar_renders_once_for_a_member(alice):
    body = _login("alice").get("/find/").content.decode()
    assert body.count('<nav class="bottom-nav" aria-label="Primary">') == 1
    bar = _bottom_nav(body)
    assert 'aria-haspopup="dialog"' in bar
    assert 'aria-expanded="false"' in bar


@pytest.mark.django_db
def test_signed_out_pages_carry_neither_bar_nor_sheet(db):
    body = Client().get("/about/").content.decode()
    assert "bottom-nav" not in body
    assert "more-sheet" not in body


@pytest.mark.django_db
def test_the_bottom_bar_lights_only_the_current_destination(alice):
    bar = _bottom_nav(_login("alice").get("/user/alice/films/").content.decode())
    assert bar.count('aria-current="page"') == 1
    assert '<a href="/user/alice/films/" class="active" aria-current="page">' in bar
    # A page outside the bar lights nothing.
    assert "aria-current" not in _bottom_nav(
        _login("alice").get("/about/").content.decode()
    )


@pytest.mark.django_db
def test_the_sheet_renders_log_out_as_a_post_form_with_csrf(alice):
    body = _login("alice").get("/find/").content.decode()
    sheet = _more_sheet(body)
    assert "<hr>" in sheet
    assert '<form method="post" action="/logout/"' in sheet
    assert 'name="csrfmiddlewaretoken"' in sheet
    # No GET path to a POST-only route, anywhere on the page.
    assert 'href="/logout/"' not in body


@pytest.mark.django_db
def test_the_sheet_offers_moderate_only_to_a_moderator(alice, wendy):
    assert ">Moderate<" not in _more_sheet(
        _login("alice").get("/find/").content.decode()
    )
    assert ">Moderate<" in _more_sheet(_login("wendy").get("/find/").content.decode())


@pytest.mark.django_db
def test_the_sheet_counts_unread_on_the_notifications_row(alice, bob):
    notify(alice, bob, Notification.Kind.FOLLOW)
    sheet = _more_sheet(_login("alice").get("/find/").content.decode())
    assert "<small>1 unread</small>" in sheet
    # At zero the row says nothing -- same hide rule as the corner badge.
    plain = _more_sheet(_login("bob").get("/find/").content.decode())
    assert "<small>" not in plain


@pytest.mark.django_db
def test_the_corner_carries_the_bell_parts_and_the_desk_wrapper(alice, bob, wendy):
    notify(alice, bob, Notification.Kind.FOLLOW)
    corner = _header_user(_login("alice").get("/find/").content.decode())
    assert 'class="corner-icon"' in corner
    assert 'class="corner-count"' in corner
    assert '<span class="corner-label">1 unread</span>' in corner
    # The desk Moderate sits inside its wrapper, so the mobile CSS can hide
    # the wrapper without ever naming the guarded moderation-link class.
    desk = _header_user(_login("wendy").get("/find/").content.decode())
    assert '<span class="corner-desk"><a class="moderation-link"' in desk


@pytest.mark.django_db
def test_the_search_page_carries_a_mobile_field(alice):
    body = _login("alice").get("/search/").content.decode()
    assert 'class="global-search page-search"' in body
    assert 'name="q"' in body
    assert '<ul class="suggest-list" hidden></ul>' in body


@pytest.mark.django_db
def test_the_profile_page_is_the_settings_hub(alice):
    body = _login("alice").get("/preferences/profile/").content.decode()
    assert "<h1>Profile / Settings</h1>" in body
    assert 'href="/preferences/import/"' in body
    assert 'href="/preferences/export/"' in body
    # The edit form is preserved, not replaced by a settings invention.
    assert "Save changes" in body


@pytest.mark.django_db
def test_the_settings_hub_offers_moderate_only_to_moderators(alice, wendy):
    assert ">Moderate<" not in _settings_links(
        _login("alice").get("/preferences/profile/").content.decode()
    )
    assert ">Moderate<" in _settings_links(
        _login("wendy").get("/preferences/profile/").content.decode()
    )


# --- the wiring and the stylesheet ----------------------------------------------


def test_base_html_wires_the_mobile_surfaces():
    base = (settings.BASE_DIR / "templates" / "base.html").read_text()
    assert '{% include "_mobile_nav.html" %}' in base
    assert "js/nav_sheet.js" in base


def _media_block(css: str, query: str) -> str:
    """The body of every @media block whose header contains ``query``."""
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


def test_the_mobile_treatment_lives_below_the_1024px_line():
    """The phone chrome is scoped to the approved breakpoint, so the desktop
    row and header keep their appearance until N4 touches them."""
    css = Path(finders.find("css/reeltalk.css")).read_text()
    mobile = re.sub(r"\s+", "", _media_block(css, "max-width:1023px"))
    assert ".site-nav{display:none;}" in mobile
    assert ".global-search{display:none;}" in mobile
    assert ".bottom-nav{display:grid;" in mobile
    assert "env(safe-area-inset-bottom)" in mobile
    # And the phone parts are inert on desktop: hidden outside the block.
    flat = re.sub(r"\s+", "", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
    assert ".bottom-nav{display:none;}" in flat
    assert ".page-search{display:none;}" in flat
