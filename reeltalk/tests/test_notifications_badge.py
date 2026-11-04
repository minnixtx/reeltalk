"""The unread badge: the phone corner bell and the desktop sidebar count.

The badge is one number, and the ways it can be wrong are few but specific,
so the tests are ordered by how each one would bite:

* **It shows the viewer's unread count and nothing else.** Not the whole
  ledger, not another member's ledger, not a number that survives
  mark-all-read. Each of those is a different bug and gets its own test.
* **It is absent when it has nothing to say.** "Hidden at zero" is a
  property about absence, so it is asserted with a count of the badge's
  own marker rather than by a string that some other element might also
  produce.
* **It is an ``<a>`` carrying no ``.btn`` class** (R82/R96). This is the
  one constraint that a template edit can violate silently, because the
  markup looks harmless either way and only the stylesheet knows the
  difference — which is why the element choice is pinned against the
  applied stylesheet rather than asserted on its own.
* **The desktop/phone split is CSS, not markup** (owner ruling
  2026-11-04): the corner bell is the phone surface, hidden from 1024px
  up, where the sidebar's Notifications row carries the count on its own
  bell instead. The server renders both at every width because it does
  not know the viewport; the stylesheet picks.
* **It costs exactly one query** on a rendered authenticated page, and
  zero where the guard stops it or where no template renders at all.

The probe at the end reads the real ``reeltalk.css`` rather than a copy of
its rules, matched on the whitenoise manifest *stem* so it finds the file
whether it is the plain source name or the hashed one collectstatic writes.
"""

import re
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from reeltalk.core.models import Film, Status
from reeltalk.notifications.models import Notification, mark_all_read, notify
from reeltalk.tests.members import member as create_member

User = get_user_model()

BADGE_MARKER = 'class="notifications-link"'
NOTIFICATIONS_URL = reverse("notifications")
# ``/`` is the obvious page for a header test and the wrong one: the
# first-run wizard (R12) redirects it to ``/setup/`` until a superuser
# exists, so a badge assertion there passes or fails on the redirect rather
# than on anything rendered. These two render for a member and for a
# stranger without any gate of their own.
MEMBER_PAGE = reverse("find-user")
ANONYMOUS_PAGE = reverse("about")


@pytest.fixture
def alice(db):
    return create_member(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return create_member(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def post(db, alice, dune):
    return Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW,
        content="a review of Dune",
    )


@pytest.fixture
def member(client, alice):
    client.force_login(alice)
    return client


def _header_user(content):
    """The ``.header-user`` corner, as rendered.

    Bounded on purpose: the badge's absence assertions are only meaningful
    against a region whose whole class inventory is known, so a stray
    ``btn`` elsewhere on the page cannot make them pass or fail.
    """
    start = content.index('<div class="header-user">')
    return content[start : content.index("</div>", start)]


def _sidebar(content):
    """The desktop menu's ``.site-nav`` landmark, as rendered."""
    match = re.search(r'<nav class="site-nav".*?</nav>', content, re.S)
    assert match, "no .site-nav menu rendered"
    return match.group(0)


# --- What the number says ----------------------------------------------------


def test_a_member_with_unread_notifications_sees_the_badge(member, alice, bob):
    notify(alice, bob, Notification.Kind.FOLLOW)
    content = member.get(MEMBER_PAGE).content.decode()
    assert content.count(BADGE_MARKER) == 1
    assert f'href="{NOTIFICATIONS_URL}"' in content
    assert "1 unread" in content


def test_the_badge_is_hidden_at_zero(member):
    # A fresh member has an empty ledger, and the corner must hold nothing
    # but the handle. Asserted as a count of the badge's own marker: a
    # substring could be produced by anything else in the header.
    content = member.get(MEMBER_PAGE).content.decode()
    assert content.count(BADGE_MARKER) == 0
    # Control: the handle is still there, so the corner renders and the
    # badge is what is missing.
    assert 'class="user-name"' in content


def test_the_badge_is_hidden_for_anonymous(db):
    content = Client().get(ANONYMOUS_PAGE).content.decode()
    assert content.count(BADGE_MARKER) == 0
    # Control: the corner itself rendered, with the anonymous controls in
    # it, so the badge is what is absent rather than the header.
    assert "Log in" in content


def test_the_badge_counts_unread_not_the_whole_ledger(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    notify(alice, bob, Notification.Kind.LIKE, post)
    mark_all_read(alice)
    for _ in range(3):
        notify(alice, bob, Notification.Kind.REPLY, post)
    assert Notification.objects.filter(recipient=alice).count() == 5
    content = member.get(MEMBER_PAGE).content.decode()
    assert "3 unread" in content
    # The opposite assertion is the one that makes this a test of the
    # unread half rather than of "a number is drawn".
    assert "5 unread" not in content


def test_the_badge_counts_only_the_viewers_own_ledger(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    for _ in range(4):
        notify(bob, alice, Notification.Kind.LIKE, post)
    assert Notification.unread_for(bob).count() == 4
    content = member.get(MEMBER_PAGE).content.decode()
    assert "1 unread" in content
    assert "4 unread" not in content


def test_marking_read_hides_the_badge(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    assert BADGE_MARKER in member.get(MEMBER_PAGE).content.decode()
    member.post("/notifications/read/")
    content = member.get(MEMBER_PAGE).content.decode()
    # The count is a timestamp comparison, so the badge goes away with it
    # rather than being dismissed per row (R93).
    assert content.count(BADGE_MARKER) == 0
    # Control: a new event brings it straight back.
    notify(alice, bob, Notification.Kind.REPLY, post)
    assert BADGE_MARKER in member.get(MEMBER_PAGE).content.decode()


def test_the_badge_shows_on_every_member_surface_not_just_one(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    paths = [
        MEMBER_PAGE,
        NOTIFICATIONS_URL,
        reverse("status", args=[post.id]),
    ]
    for path in paths:
        assert BADGE_MARKER in member.get(path).content.decode(), path


# --- The desktop count: the sidebar's Notifications bell -----------------------
# Owner ruling 2026-11-04 moved the desktop number off the header corner and
# onto the sidebar row. The markup renders at every width (the stylesheet
# hides the corner bell from 1024px up, pinned in the probe below), so these
# read the sidebar landmark the same way the corner tests read the corner.


def test_the_sidebar_bell_carries_the_unread_count(member, alice, bob):
    notify(alice, bob, Notification.Kind.FOLLOW)
    nav = _sidebar(member.get(MEMBER_PAGE).content.decode())
    assert '<span class="nav-count" aria-hidden="true">1</span>' in nav
    # The visible bubble is decoration; the clipped text names the row,
    # the same split the corner bell uses.
    assert '<span class="visually-hidden">1 unread</span>' in nav
    # The count rides the Notifications row, not just the landmark.
    row = re.search(r'<a href="/notifications/".*?</a>', nav, re.S)
    assert row and "nav-count" in row.group(0)


def test_the_sidebar_bell_says_nothing_at_zero(member):
    nav = _sidebar(member.get(MEMBER_PAGE).content.decode())
    assert "nav-count" not in nav
    # Control: the row itself is there, so the count is what is absent.
    assert ">Notifications<" in nav


def test_marking_read_clears_the_sidebar_count(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    assert "nav-count" in _sidebar(member.get(MEMBER_PAGE).content.decode())
    member.post("/notifications/read/")
    assert "nav-count" not in _sidebar(member.get(MEMBER_PAGE).content.decode())


# --- The element choice (R82 / R96) -----------------------------------------


def test_the_badge_is_an_anchor_not_a_button(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    region = _header_user(member.get(MEMBER_PAGE).content.decode())
    assert '<a class="notifications-link"' in region
    # A bare <button> here would arrive pre-lit crimson in Bebas caps, so
    # the corner must contain no button at all.
    assert "<button" not in region


def test_the_badge_carries_no_btn_class(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    region = _header_user(member.get(MEMBER_PAGE).content.decode())
    # "btn" appears nowhere in the corner's class inventory. The region is
    # bounded so this cannot be satisfied by a page that has no controls.
    assert "btn" not in region
    # Control on the target rather than the badge: the control the badge
    # points at really is a .btn, so this negative is specific to the
    # badge and not to a site that never uses the class.
    content = member.get(NOTIFICATIONS_URL).content.decode()
    control = re.search(
        r'<button[^>]*class="([^"]*)"[^>]*>Mark all read</button>', content
    )
    assert control is not None
    assert "btn" in control.group(1).split()


# --- What it costs ----------------------------------------------------------


LEDGER_TABLE = "notifications_notification"


def _ledger_queries(ctx):
    return [q["sql"] for q in ctx.captured_queries if LEDGER_TABLE in q["sql"]]


def test_the_badge_costs_exactly_one_count_query(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    with CaptureQueriesContext(connection) as ctx:
        member.get(reverse("find-user"))
    queries = _ledger_queries(ctx)
    assert len(queries) == 1
    assert "COUNT(" in queries[0].upper()
    # The count is scoped to the viewer, which is what lets it ride the
    # composite (recipient_id, created) index the cost claim rests on.
    assert '"recipient_id" =' in queries[0]


def test_an_anonymous_render_runs_no_notification_query(db):
    # The processor still runs for an anonymous render — it is registered
    # globally — and must answer 0 without touching the table.
    with CaptureQueriesContext(connection) as ctx:
        Client().get(ANONYMOUS_PAGE)
    assert _ledger_queries(ctx) == []


def test_a_json_response_runs_no_notification_query(member, alice, post):
    # Context processors run for a template render only. Liking one's own
    # post answers JsonResponse and the producer is silent (self-guard),
    # so the only thing that could touch the ledger table here is a
    # processor that ran when it should not have.
    with CaptureQueriesContext(connection) as ctx:
        member.post(f"/status/{post.id}/like/")
    assert _ledger_queries(ctx) == []


# --- The probe: the applied stylesheet (R82 / R96) --------------------------

COMMENTS_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
HASH_RE = re.compile(r"[0-9a-f]{8,40}")


def _stem(filename):
    """The whitenoise manifest stem: the name minus the content hash.

    ``collectstatic`` under manifest storage writes ``reeltalk.<hash>.css``
    and rewrites ``{% static %}`` to that name, so a probe that hard-codes
    either spelling finds nothing half the time. Matching on the stem finds
    the file in both forms.
    """
    base, _, ext = filename.rpartition(".")
    head, _, maybe_hash = base.rpartition(".")
    if HASH_RE.fullmatch(maybe_hash):
        return f"{head}.{ext}"
    return filename


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("reeltalk.css", "reeltalk.css"),
        ("reeltalk.8f3a2c1b.css", "reeltalk.css"),
        ("reeltalk.2c1b8f3a9d7e6f51.css", "reeltalk.css"),
        ("reeltalkfoo.css", "reeltalkfoo.css"),
        ("search.abc12345.js", "search.js"),
    ],
)
def test_the_stem_matcher_finds_the_stylesheet_in_either_form(filename, expected):
    assert _stem(filename) == expected


def _stylesheet():
    path = finders.find("css/reeltalk.css")
    assert path, "the applied stylesheet is not findable"
    # The probe locates the file by stem, so it reads the same rules whether
    # whitenoise is serving the source name or the hashed one.
    assert _stem(Path(path).name) == "reeltalk.css"
    # Comments are stripped first: they sit between rules and would
    # otherwise be swallowed into the next rule's selector list.
    return COMMENTS_RE.sub("", Path(path).read_text())


def _selector_blocks(css):
    """``(selector tokens, body)`` for every flat rule in the file.

    The tokens are the comma-separated selector parts, whitespace stripped,
    so a rule can be matched on what it actually selects rather than on a
    substring of its header — ``.film-actions .btn`` must not read as
    ``.btn``.
    """
    return [
        ([t.strip() for t in selector.split(",")], body)
        for selector, body in RULE_RE.findall(css)
    ]


def _base_control_blocks(css):
    """Rule blocks that name the bare control, not a descendant or a state.

    ``.film-actions .btn`` restyles a control in one panel and
    ``.btn:hover`` is a state; neither is the base look a stray element
    inherits. Only a selector token that is exactly ``button`` or ``.btn``
    applies to an element that merely *is* one.
    """
    return [
        body
        for tokens, body in _selector_blocks(css)
        if "button" in tokens or ".btn" in tokens
    ]


def test_the_applied_stylesheet_lights_the_bare_control_in_three_blocks():
    css = _stylesheet()
    blocks = _base_control_blocks(css)
    # R96's claim, read off the file rather than trusted: exactly three
    # rule blocks make a bare <button> or .btn the lit crimson control.
    assert len(blocks) == 3
    lit = "\n".join(blocks)
    assert "var(--blood-red)" in lit
    assert "var(--neon-mottle)" in lit
    assert "var(--neon-rim)" in lit
    assert "box-shadow" in lit
    assert "var(--display)" in lit
    assert "text-transform: uppercase" in lit


def _media_block(css: str, query: str) -> str:
    """The body of every @media block whose header contains ``query``."""
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


def _flat_rules(css: str) -> str:
    """CSS with layout whitespace collapsed, descendant spaces kept.

    Stripping *all* whitespace (the habit elsewhere in this file) would
    glue a descendant selector like ``.header-user .notifications-link``
    into a compound one, so these assertions normalize around the
    structural characters instead.
    """
    css = re.sub(r"\s+", " ", css)
    return re.sub(r"\s*([{};:])\s*", r"\1", css)


def test_the_stylesheet_makes_the_corner_bell_the_phone_surface():
    # Owner ruling 2026-11-04 replaced R96's "no CSS was written for the
    # badge" ban: the corner bell now has exactly one job in the sheet --
    # hide from 1024px up, where the sidebar carries the count, and come
    # back below the line, where the sidebar is gone.
    css = _stylesheet()
    flat = _flat_rules(css)
    # The hide is two classes deep on purpose: a bare .notifications-link
    # would lose the display fight to .header-user > a.
    assert ".header-user .notifications-link{display:none;}" in flat
    mobile = _flat_rules(_media_block(css, "max-width:1023px"))
    assert ".header-user .notifications-link{display:inline-flex;}" in mobile
    # And the sidebar count is styled where the sidebar itself exists.
    desktop = _flat_rules(_media_block(css, "min-width:1024px"))
    assert ".site-nav .nav-icon{position:relative;" in desktop
    assert ".site-nav .nav-count{" in desktop
    assert "var(--red)" in desktop


def test_the_badge_still_wears_the_header_links_look():
    css = _stylesheet()
    # What it inherits on the phone: the header link's own muted,
    # ununderlined text. No other CSS names the badge's look.
    header = [
        body for tokens, body in _selector_blocks(css) if tokens == [".header-user a"]
    ]
    assert header, ".header-user a is not styled"
    assert "text-decoration: none" in "\n".join(header)
