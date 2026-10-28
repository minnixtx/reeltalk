"""N5: the home feed's scope tabs (plan §7 contract, §8 increment).

The strip is the front half of the feed contract. Three rules hold it down:

**URL-only state (N1 Q6).** ``/?scope=following|local|all`` is the whole
state — no session write, no stored preference — so reload, Back and a
shared URL all agree on the tab. ``?scope=following`` must be *identical*
to the bare ``/``, not merely similar: the default scope is a default, not
a second code path.

**Honest tabs only (plan §7).** A tab ships only for a scope with a real
backend, so every tab shown goes somewhere. N5 had one backend and therefore
one tab; N6 added the Local and All queries and the tabs appeared with them
without the template's loop changing, which is the registry doing its job.
``test_feed_scopes_public.py`` holds the row-level rules for those two scopes;
this file is about the strip and the URL contract.

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


def test_every_live_scope_renders_as_a_tab(alice, admin):
    """RE-PINNED at N6, deliberately (the N2 ``test_lists_views`` re-pin is the
    precedent). N5 asserted Local and All were ABSENT — no backend, no tab. N6
    shipped both queries, so all three now render; leaving the old assertion
    would pin a state the plan explicitly scheduled to change."""
    strip = _strip(_member_client().get("/").content.decode())
    assert ">Following<" in strip
    assert ">Local<" in strip
    assert ">All<" in strip
    assert strip.count('href="/?scope=') == 3


def test_anonymous_home_gets_the_local_tab_only(alice, admin):
    """RE-PINNED at N6. N5 asserted no strip at all, because a stranger had no
    feed to scope. A stranger now reads Local (owner decision 2026-10-28), so
    the strip ships with the one tab they may read — and still nothing that
    would bounce them to a login page."""
    body = Client().get("/").content.decode()
    strip = _strip(body)
    assert ">Local<" in strip and 'aria-current="page"' in strip
    assert "Following" not in strip and ">All<" not in strip


# --- The URL contract ------------------------------------------------------


def test_scope_following_is_the_same_page_as_bare_home(alice, admin):
    """The acceptance line: the Following tab is identical to today's feed.
    A default that renders through a second path drifts; this pins that
    there is no second path."""
    client = _member_client()
    assert _page(client, "/?scope=following") == _page(client, "/")


@pytest.mark.parametrize("scope", ["bogus", "following;drop", "FOLLOWING"])
def test_a_scope_the_app_does_not_serve_falls_back_not_404s(alice, admin, scope):
    """A scope with no backend (or a nonsense one) serves the default feed with
    the default tab marked.

    N5 parametrized this over ``local`` and ``all`` too; those moved to
    ``test_feed_scopes_public.py`` at N6, where they are live scopes with their
    own rows rather than falling back. What is pinned here is the fallback for
    a value the registry has never heard of."""
    client = _member_client()
    response = client.get(f"/?scope={scope}")
    assert response.status_code == 200
    assert 'aria-current="page"' in _strip(response.content.decode())
    assert _page(client, "/") == _page(client, f"/?scope={scope}")


def test_the_cursor_rides_beside_the_scope_untouched(alice, admin):
    """Scroll + cursor unaffected (the acceptance line): page 2 asked for
    under a falling-back scope is page 2, row for row — the scope resolve
    must not disturb ``?c=`` handling.

    The scope is a nonsense value on purpose. Since N6, ``local`` and ``all``
    mean something, so they would serve a different feed and prove nothing
    about the cursor; the scope that falls back isolates ``?c=`` alone."""
    spread_shelve(alice, FEED_PAGE_SIZE * 2 + 5, timezone.now() - timedelta(hours=3))
    client = _member_client()
    _, cursor = feed_entries(alice, limit=FEED_PAGE_SIZE)
    plain = _page(client, f"/?c={cursor}")
    scoped = _page(client, f"/?scope=nonsense&c={cursor}")
    assert scoped == plain, "?scope changed what ?c= served"
