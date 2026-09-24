"""The header badge (increment 4, R94/R96).

The badge is one number in one corner of the header, and the ways it can be
wrong are few but specific, so the tests are ordered by how each one would
bite:

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
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return User.objects.create_user(localname="bob", password="s3cretpass")


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
    assert 'class="btn"' in content


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


def test_the_badge_wears_the_header_links_look_and_adds_no_style():
    css = _stylesheet()
    # No CSS was written for the badge — it invents no visual language, so
    # the parked R73 pass owns the look and nothing here pre-empts it.
    assert "notifications-link" not in css
    # What it does inherit: the header link's own muted, ununderlined text.
    header = [
        body for tokens, body in _selector_blocks(css) if tokens == [".header-user a"]
    ]
    assert header, ".header-user a is not styled"
    assert "text-decoration: none" in "\n".join(header)
