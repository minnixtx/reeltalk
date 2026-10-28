"""N5: the home feed's scope tabs (plan §7 contract, §8 increment).

The strip is the front half of the feed contract. Three rules hold it down:

**URL-only state (N1 Q6).** ``/?scope=following|local|all`` is the whole
state — no session write, no stored preference — so reload, Back and a
shared URL all agree on the tab. ``?scope=following`` must be *identical*
to the bare ``/``, not merely similar: the default scope is a default, not
a second code path.

**Honest tabs only (plan §7).** ``feed_entries`` is the only scope backend
that exists, so Following is the only tab rendered. Local and All arrive
with N6 and appear in the strip when they appear in the registry — a stub
tab that goes nowhere is exactly the fake the plan forbids.

**Fallback, never 404.** A deep link to a not-yet-live (or nonsense) scope
serves the default feed with the default tab marked, because a shared
future-scope URL should degrade to the feed that exists. The cursor is
untouched by all of this: ``?scope=`` rides beside ``?c=`` without
disturbing it.
"""

import re
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from reeltalk.core.models import FEED_PAGE_SIZE, feed_entries
from reeltalk.tests.feed import spread_shelve
from reeltalk.tests.members import member, site_admin

# The sidebar's Log out form carries a {% csrf_token %} and the head carries
# the matching <meta> the like buttons' JS reads, both masked with a fresh
# random salt on every request. Two renders of the same page therefore differ
# in exactly those attribute values; equality tests strip them out first so
# "identical" means the page, not the per-request nonce.
_CSRF_NONCE = re.compile(
    r'<input type="hidden" name="csrfmiddlewaretoken" value="[^"]*"'
    r'|name="csrf-token" content="[^"]*"'
)


def _page(client: Client, url: str) -> str:
    """Rendered body of ``url`` with the per-request CSRF nonce normalised."""
    return _CSRF_NONCE.sub("", client.get(url).content.decode())


@pytest.fixture
def admin(db):
    # R12: / redirects to /setup/ until a superuser exists, so without this
    # every render assertion below proves nothing about the feed.
    return site_admin(localname="admin", password="s3cretpass")


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


def _member_client() -> Client:
    client = Client()
    assert client.login(username="alice", password="s3cretpass"), (
        "alice did not sign in — the page below would render anonymously, "
        "and every assertion on it would pass vacuously"
    )
    return client


def _strip(body: str) -> str:
    """The scope nav alone, so assertions cannot be satisfied by some other
    element on the page (the sidebar carries its own aria-current)."""
    start = body.index('<nav class="feed-scopes"')
    return body[start : body.index("</nav>", start) + len("</nav>")]


# --- The strip itself ------------------------------------------------------


def test_bare_home_renders_the_strip_with_following_active(alice, admin):
    body = _member_client().get("/").content.decode()
    strip = _strip(body)
    assert 'aria-label="Feed scope"' in strip
    assert 'href="/?scope=following"' in strip
    assert 'aria-current="page"' in strip, "no tab is marked as the served one"


def test_the_active_mark_is_on_following_and_nowhere_else_in_the_strip(alice, admin):
    strip = _strip(_member_client().get("/").content.decode())
    assert strip.count('aria-current="page"') == 1
    following_tag = strip[strip.index("<a") : strip.index(">", strip.index("<a"))]
    assert 'aria-current="page"' in following_tag


def test_only_live_scopes_render_as_tabs(alice, admin):
    """Local and All have no backend until N6, so no tab ships for them —
    the plan forbids shipping planned tabs early."""
    strip = _strip(_member_client().get("/").content.decode())
    assert ">Following<" in strip
    assert "Local" not in strip
    assert "All" not in strip


def test_anonymous_home_gets_no_strip(alice, admin):
    """Anonymous visitors see the landing copy where the feed is; a scope
    strip over no feed would mark a tab for a feed they cannot read."""
    body = Client().get("/").content.decode()
    assert "feed-scopes" not in body


# --- The URL contract ------------------------------------------------------


def test_scope_following_is_the_same_page_as_bare_home(alice, admin):
    """The acceptance line: the Following tab is identical to today's feed.
    A default that renders through a second path drifts; this pins that
    there is no second path."""
    client = _member_client()
    assert _page(client, "/?scope=following") == _page(client, "/")


@pytest.mark.parametrize("scope", ["local", "all", "bogus", "following;drop"])
def test_a_scope_the_app_does_not_serve_falls_back_not_404s(alice, admin, scope):
    """Deep links to N6's future scopes (and anything else) serve the
    default feed with the default tab marked."""
    client = _member_client()
    response = client.get(f"/?scope={scope}")
    assert response.status_code == 200
    assert 'aria-current="page"' in _strip(response.content.decode())
    assert _page(client, "/") == _page(client, f"/?scope={scope}")


def test_the_cursor_rides_beside_the_scope_untouched(alice, admin):
    """Scroll + cursor unaffected (the acceptance line): page 2 asked for
    under a falling-back scope is page 2, row for row — the scope resolve
    must not disturb ``?c=`` handling."""
    spread_shelve(alice, FEED_PAGE_SIZE * 2 + 5, timezone.now() - timedelta(hours=3))
    client = _member_client()
    _, cursor = feed_entries(alice, limit=FEED_PAGE_SIZE)
    plain = _page(client, f"/?c={cursor}")
    scoped = _page(client, f"/?scope=local&c={cursor}")
    assert scoped == plain, "?scope changed what ?c= served"
