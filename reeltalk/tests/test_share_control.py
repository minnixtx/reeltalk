"""The share control (``SHARE_PLAN.md`` S2): the icon, its gates, and its script.

S1 settled what a page is allowed to hand out; this is the surface that hands it
out, and the failures it guards are all of one kind — a control that is there and
does nothing, or is there and should not be.

**What is asserted here.**

* **It renders where a stranger can open the link** — on the post page and the
  list page, for a member and for a signed-out reader (D5), and it is the last
  control in the row.
* **It does not render where they cannot.** The case a template guess gets wrong
  is the author's own private post, which renders 200 for the author: "the page
  rendered" is not the predicate. The mirror case below is the same lesson from
  the other side — a public mirror of a note whose stored home URL is not an http
  address is reachable and still gets no button, because the gate lives in the
  address and the template reads only the address.
* **The un-gate came with its clauses.** The row used to sit inside one
  ``user.is_authenticated`` wrapper that was the only thing keeping applaud and
  reply from rendering for a stranger, so the wrapper is now
  ``user.is_authenticated or share_url`` and each of those two controls carries
  its own auth clause. The owner's ruling is that a logged-out reader sees
  **exactly one** icon in that row and it is the share button — not two, not
  zero — so the row is counted, not merely checked for the share button.
* **The script can reach the button.** CSP is ``script-src 'self'`` with no
  ``unsafe-inline``, so an inline handler is blocked and the button sits inert
  with no error the page can see: the template is pinned to carry no inline
  handler, to load ``share.js`` on the same condition it renders the button, and
  to hand the address over as an attribute the script actually reads.
* **The ladder's one rule that pytest can still reach.** ``navigator.share``
  rejects when the reader closes the OS sheet, and copying then would rewrite
  their clipboard because someone pressed Escape. No JS harness exists in this
  repo, so that is pinned by reading the shipped script's share branch: it must
  not reach the copy path. The rest of the ladder is proven by clicking it in a
  real browser and is recorded in the increment's handoff.
* **The notice floats.** The owner's ruling of 2026-11-23, after seeing the first
  version: what a press says is one fixed pill the script creates, not a line of
  text beside the icon. The inline version widened the control group inside a
  right-aligned bar and slid the icon 59px out from under a second press, so no
  template prints a notice and the script owns the page's one live region — which
  is also why the announcement is pinned against the script's source.
* **The feed row carries it too**, which the owner asked for on 2026-11-23 and
  which overrode the item ``SHARE_PLAN.md`` had deferred. The home feed, its
  endless-scroll fragment and the profile all include this one partial, so the
  same gates are pinned on each: a member's row ends replay, applaud, reply,
  share; a stranger's row holds exactly one control; the author's own private and
  followers-only rows keep their controls and lose only the share button; a list
  row shares ``/list/`` and a mirror its own home address. The two pages ship the
  script differently on purpose and both reasons are written down — the profile
  ships it only when a row on that page can use it, the home feed ships it always
  because the scroll appends rows the first page cannot see.
* **Minting the address costs nothing.** ``_attach_share_addresses`` reads only
  what the row already carries, so the feed's query count does not move when the
  feed grows — the same invariance shape ``test_feed_query_count.py`` argues for.

**Mutation checks**, each run and each seen red, then reverted: gate the share
span on ``user.is_authenticated`` and the anonymous render and the one-control
count both go red; drop the ``user.is_authenticated`` clause from the applaud
span and the count goes red; move the share span before reply and the order pin
goes red; print ``status.remote_url`` instead of the gated address and the mirror
pin goes red; write ``onclick`` into the template and the CSP pin goes red.
The feed half, same treatment: gate the row's share span on a member and the
one-control count goes red; drop the clause the wrapper used to carry from the
row's applaud span and the same count goes red; stop running
``_attach_share_addresses`` in ``public_feed`` and five row tests go red; stop
recording which list a face belongs to and the list-row pin goes red; ship the
script unconditionally on the profile and its own pin goes red; and mint the
address before the reachability gate, which takes four red across the post page,
the list page, the row and the profile.

The address itself — the three shapes, the scheme gate, the canonical origin — is
``test_share.py``'s. Nothing here re-tests it; what is tested is that the page
puts in the button exactly what the model gave it.
"""

import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext, override_settings
from django.urls import reverse

from reeltalk.core.models import (
    FEED_PAGE_SIZE,
    Film,
    Status,
    Visibility,
    mark_watched,
    public_feed,
)
from reeltalk.lists.services import create_list
from reeltalk.tests.members import member, site_admin

PASSWORD = "s3cretpass"
CANON = "https://reeltalk.example"
User = get_user_model()

REPO_ROOT = Path(__file__).resolve().parents[2]
CSS_PATH = REPO_ROOT / "reeltalk" / "social" / "static" / "css" / "reeltalk.css"
IMAGES = REPO_ROOT / "reeltalk" / "social" / "static" / "images"
SHARE_JS = REPO_ROOT / "reeltalk" / "social" / "static" / "js" / "share.js"
POST_TEMPLATE = (
    REPO_ROOT / "reeltalk" / "core" / "templates" / "core" / "status" / "detail.html"
)
LIST_TEMPLATE = REPO_ROOT / "reeltalk" / "lists" / "templates" / "lists" / "detail.html"
FEED_TEMPLATE = REPO_ROOT / "templates" / "_feed_row.html"

CREAM = "#F5E5C5"


# --- the world ---------------------------------------------------------------


@pytest.fixture
def alice(db):
    return member(localname="alice", password=PASSWORD)


@pytest.fixture
def bob(db):
    """A second member, so "someone else's post" is a real reader here."""
    return member(localname="bob", password=PASSWORD)


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def review(alice, dune):
    return mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")


def _page(who=None, url=None) -> str:
    client = Client()
    if who:
        assert client.login(username=who, password=PASSWORD)
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.content.decode()


def _post_page(who=None, status=None) -> str:
    return _page(who, reverse("status", args=[status.pk]))


def _row(body) -> str:
    """The action row's own markup. It holds spans only, so a search for its
    closing tag cannot land past it."""
    assert '<div class="row-actions">' in body, "the page carries no action row"
    start = body.index('<div class="row-actions">') + len('<div class="row-actions">')
    return body[start : body.index("</div>", start)]


def _controls(block) -> list[str]:
    """The row's controls, in document order."""
    return re.findall(r'<span class="(replay|applaud|reply|share)">', block)


def _share_span(body) -> str:
    start = body.index('<span class="share">') + len('<span class="share">')
    return body[start : body.index("</span>", start)]


def _top_level_rules() -> list[tuple[str, str]]:
    """(selector list, declarations) for every rule outside an ``@media`` block.

    Comments go because one can sit between a selector and its brace, and at-rules
    go because their bodies are nested braces this parser would split wrongly —
    the share control's rules are all top level.
    """
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)
    css = re.sub(r"@[^{}]*\{(?:[^{}]|\{[^{}]*\})*\}", "", css)
    css = re.sub(r"\s+", " ", css)
    return [
        (m.group(1).strip(), m.group(2).strip())
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
    ]


def _decl(selector: str) -> str:
    for selectors, declarations in _top_level_rules():
        if selectors == selector:
            return declarations
    raise AssertionError(f"no top-level rule for {selector}")


# --- the post page -----------------------------------------------------------


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_member_gets_the_control_carrying_the_posts_address(alice, bob, review):
    body = _post_page(bob, review)
    assert f'data-share-url="{CANON}/status/{review.pk}/"' in body


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_signed_out_visitor_gets_the_control_too(alice, review):
    """D5: a signed-out reader is the reader this feature is for."""
    body = _post_page(None, review)
    assert 'class="share-btn"' in body
    assert f'data-share-url="{CANON}/status/{review.pk}/"' in body


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_address_in_the_button_is_the_canonical_one(alice, dune, review):
    """The page must not hand out the host it was asked on (R108).

    A moderator reading the post on the LAN address is the whole reason this is
    pinned at the page and not only on the model: the button is what gets copied.
    """
    client = Client(SERVER_NAME="localhost")
    body = client.get(reverse("status", args=[review.pk])).content.decode()
    assert f'data-share-url="{CANON}/status/{review.pk}/"' in body
    assert "localhost" not in _share_span(body)


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_share_control_is_the_last_in_the_row(alice, bob, review):
    """The owner's row order: replay, applaud, reply, share (D-Look)."""
    assert _controls(_row(_post_page(bob, review))) == [
        "replay",
        "applaud",
        "reply",
        "share",
    ]


@pytest.mark.django_db
def test_the_author_s_own_private_post_carries_no_control(alice, dune):
    """The case a template guess gets wrong: this page renders 200 for her.

    "The page rendered, so the control may render" is the wrong predicate, and
    the right one is in the address, which is empty here.
    """
    private = Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW,
        content="<p>Not for strangers.</p>",
        visibility=Visibility.PRIVATE,
    )
    body = _post_page(alice, private)
    assert "share-btn" not in body
    assert "share.js" not in body


@pytest.mark.django_db
def test_a_reachable_mirror_with_no_usable_home_url_carries_no_control(db):
    """Reachable is not enough, and that is the gate's placement proven.

    The mirror is public by a live author, so ``is_reachable_offsite`` is True and
    the page renders for anyone. Its stored home URL is not an http address, so
    there is nothing to hand out and no button — the scheme gate lives in the
    address, so a template that only asked "is it reachable" would ship a button
    that copies a peer-supplied string.
    """
    peer = User(
        localname="carol@peer.example",
        local=False,
        actor_url="https://peer.example/users/carol",
        inbox_url="https://peer.example/users/carol/inbox",
    )
    peer.set_unusable_password()
    peer.save()
    mirror = Status.objects.create(
        user=peer,
        local=False,
        remote_url="javascript:alert(1)",
        content="<p>A mirrored note.</p>",
    )
    body = _page(None, reverse("status", args=[mirror.pk]))
    assert "share-btn" not in body
    assert "javascript" not in body


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_signed_out_reader_gets_exactly_one_control_in_the_row(alice, review):
    """The owner's ruling, counted rather than eyeballed: one icon, the share one.

    The row used to be inside a single auth wrapper, and applaud and reply carried
    no clause of their own. Un-gating the wrapper without giving them one would
    leave this reader two buttons whose routes bounce to ``/login/``, which is
    why the count is asserted and not merely the presence of the share button.
    """
    row = _row(_post_page(None, review))
    assert _controls(row) == ["share"]
    for control in ("applaud-btn", "reply-btn", "replay-btn"):
        assert control not in row


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_member_still_gets_the_other_controls_after_the_un_gate(alice, bob, review):
    """Non-vacuity for the row above: the clauses hide nothing from a member."""
    assert _controls(_row(_post_page(bob, review))) != ["share"]


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_control_is_named_share_and_carries_no_count(alice, review):
    """The glyph promises an action the browser may deliver differently (D7), so
    the name says Share and not Copy link. And a share has no tally (D1), so
    unlike its three neighbours there is no number beside it."""
    span = _share_span(_post_page(None, review))
    assert 'aria-label="Share"' in span and 'title="Share"' in span
    assert "count" not in span


def test_no_template_prints_the_notice():
    """The owner's ruling of 2026-11-23, pinned where it could come back.

    The notice used to be a ``<span class="share-flash">`` beside the button on
    every surface. It widened the control group inside a right-aligned bar, so the
    icon slid 59px while the word was up and a second press landed on the row's own
    link. A floating pill belongs to no layout and cannot move anything, so the
    inline element is gone from all three templates and stays gone.
    """
    for path in (POST_TEMPLATE, LIST_TEMPLATE, FEED_TEMPLATE):
        assert "share-flash" not in path.read_text(), f"{path.name} still prints one"


def test_the_notice_is_a_live_region_the_script_creates():
    """One press, no dialog (D2), so the page is the only thing that can say
    whether it copied or the operating system took the link instead.

    The pill is built by the script rather than printed in four templates, so the
    announcement cannot drift between the feed row and the post page — and the pin
    has to be on the script for the same reason. ``role="status"`` is the implicit
    polite live region, and it is created at load and kept laid out at rest, because
    a region a browser never laid out is a region nothing announces.
    """
    script = SHARE_JS.read_text()
    assert 'setAttribute("role", "status")' in script
    assert "document.body.appendChild(toast)" in script
    assert "Link copied" in script and "Press Ctrl+C" in script
    decl = _decl(".share-toast")
    assert "opacity: 0" in decl
    assert "display: none" not in decl and "visibility" not in decl
    assert "opacity: 1" in _decl(".share-toast.is-up")


def test_the_phone_notice_clears_the_bottom_bar():
    """The desktop notice hangs off the bottom of the viewport; on a phone that is
    where the navigation bar lives, so the phone lift uses the bar's own
    measurement rather than a second literal that can drift.

    The order is the rule, not an implementation detail. Both selectors are a bare
    ``.share-toast``, so the sheet's order decides which ``bottom`` wins, and the
    lift written into the phone block above the share group silently lost to the
    base rule: measured on a 390px viewport the pill sat 31px inside the bar with
    the literal present and the test green.
    """
    css = CSS_PATH.read_text()
    assert (
        "calc(var(--bottom-nav-row) + 1px + 20px + env(safe-area-inset-bottom))" in css
    )
    base = css.index("bottom: calc(20px + env(safe-area-inset-bottom))")
    lift = css.index("calc(var(--bottom-nav-row) + 1px + 20px")
    assert lift > base, "the phone lift must follow the base rule, or it never applies"


# --- the script --------------------------------------------------------------


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_no_inline_handler_and_the_script_arrives_for_every_reader(alice, review):
    """``script-src 'self'`` with no ``unsafe-inline`` makes an inline handler a
    dead button, and a script behind the auth gate would do the same to the
    anonymous reader, who is the control's main audience."""
    for body in (_post_page(None, review), _post_page(alice, review)):
        assert "onclick" not in body
        assert "js/share.js" in body


def test_neither_template_writes_an_inline_handler():
    for path in (POST_TEMPLATE, LIST_TEMPLATE):
        text = path.read_text()
        assert not re.search(r"\son[a-z]+\s*=", text), f"{path.name} carries a handler"
        assert "<script>" not in text, f"{path.name} carries inline script"


def test_the_script_reads_the_attribute_the_templates_write():
    """The address crosses the seam by name, so the two halves are pinned together."""
    script = SHARE_JS.read_text()
    assert 'getAttribute("data-share-url")' in script
    for path in (POST_TEMPLATE, LIST_TEMPLATE):
        assert 'data-share-url="{{ share_url }}"' in path.read_text()


def test_a_cancelled_share_sheet_does_not_copy():
    """The one rung rule pytest can still reach without a JS harness.

    ``navigator.share`` rejects when the reader closes the sheet without choosing
    anything. Falling through to the clipboard then rewrites what they had copied
    because someone pressed Escape, so the copy rungs are reached only when the
    sheet is absent — and the prototype that had ``.catch(copy)`` is how the rule
    was found.
    """
    branch = re.search(
        r"if \(navigator\.share\) \{(.*?)return;", SHARE_JS.read_text(), re.S
    )
    assert branch, "share.js no longer branches on navigator.share"
    assert "copy(" not in branch.group(1)


# --- the list page -----------------------------------------------------------


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_list_page_carries_the_control_with_the_list_address(alice, dune):
    """D8: list faces are in. The address is the canonical ``/list/`` shape, not
    the face's ``/status/`` path — the page a stranger lands on is the list."""
    film_list = create_list(
        alice, title="Noir", films=[dune], visibility=Visibility.PUBLIC
    )
    body = _page(None, reverse("list-detail", args=[film_list.pk]))
    span = _share_span(body)
    assert f'data-share-url="{CANON}/list/{film_list.pk}/"' in span
    assert f"/status/{film_list.status.pk}/" not in span


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_list_control_sits_after_the_reply_control(alice, dune, bob):
    """Same order as the post page's row, as far as this page carries them: it has
    no replay control, and Save and Edit are a different weight of control."""
    film_list = create_list(
        alice, title="Noir", films=[dune], visibility=Visibility.PUBLIC
    )
    body = _page(bob, reverse("list-detail", args=[film_list.pk]))
    start = body.index('class="review-head"')
    head = body[start : body.index("</header>", start)]
    assert _controls(head) == ["applaud", "reply", "share"]


@pytest.mark.django_db
def test_the_makers_own_private_list_carries_no_control(alice, dune):
    film_list = create_list(
        alice, title="Private Shelf", films=[dune], visibility=Visibility.PRIVATE
    )
    body = _page(alice, reverse("list-detail", args=[film_list.pk]))
    assert "share-btn" not in body
    assert "share.js" not in body


# --- the feed row -----------------------------------------------------------


@pytest.fixture
def admin(db):
    """R12: ``/`` redirects to ``/setup/`` until a superuser exists, and then
    every feed assertion below would pass against a redirect page. Required
    explicitly rather than autouse so the post-page tests above stay untouched."""
    return site_admin(localname="admin", password=PASSWORD)


def _feed(who=None, query="") -> str:
    client = Client()
    if who:
        assert client.login(username=who, password=PASSWORD), (
            f"{who} did not sign in — the row below would render anonymously and "
            "every assertion on it would pass vacuously"
        )
    resp = client.get("/" + query)
    assert resp.status_code == 200, "the home feed did not render (R12: /setup/)"
    return resp.content.decode()


def _post(user, film, marker, **kwargs) -> Status:
    """A plain public post about ``film``, for a feed row.

    A comment, not a review: ``mark_watched`` also writes a shelf row, which
    arrives in the feed as its own entry and would make "the row for this post"
    ambiguous. A film is attached because a row's reply control requires one.
    """
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.COMMENT,
        content=f"<p>{marker}</p>",
        **kwargs,
    )


def _remote_author(localname="carol@peer.example") -> User:
    user = User(
        localname=localname,
        local=False,
        actor_url="https://peer.example/users/carol",
        inbox_url="https://peer.example/users/carol/inbox",
    )
    user.set_unusable_password()
    user.save()
    return user


def _feed_block(body, status_id) -> str:
    """One rendered feed row, found by the status behind it."""
    marker = f'<li class="review" data-status="{status_id}"'
    assert marker in body, f"no feed row for status {status_id} — wrong scope?"
    start = body.index(marker)
    return body[start : body.index("</li>", start)]


def _bar(block) -> str:
    """The row's action bar, or the empty string when it renders none at all."""
    open_tag = '<div class="row-actions">'
    if open_tag not in block:
        return ""
    start = block.index(open_tag) + len(open_tag)
    return block[start : block.index("</div>", start)]


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_member_feed_row_carries_the_share_control_last(alice, bob, dune, admin):
    """The owner's ask of 2026-11-23: share from the row, not only from the post.

    Local scope rather than the default Following one so the row exists without
    a follow being part of what this test has to set up.
    """
    post = _post(bob, dune, "local-row")
    body = _feed(alice, "?scope=local")
    bar = _bar(_feed_block(body, post.pk))
    assert _controls(bar) == ["replay", "applaud", "reply", "share"]
    assert f'data-share-url="{CANON}/status/{post.pk}/"' in bar


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_signed_out_feed_row_holds_the_share_button_and_nothing_else(
    alice, bob, dune, admin
):
    """The owner's ruling, asked of the row: one icon, and it is the share one.

    Same accidental-guard story as the post page — the row partial wrapped both
    controls in one auth clause, so un-gating the wrapper for the share control
    meant handing that clause to each of them in the same change.
    """
    post = _post(bob, dune, "stranger-row")
    bar = _bar(_feed_block(_feed(None, "?scope=local"), post.pk))
    assert _controls(bar) == ["share"]


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_an_unshareable_row_keeps_its_controls_and_drops_only_share(alice, dune, admin):
    """The author's own private and followers-only rows: she reads them, a
    stranger could not, so the bar stays and the share button does not appear.

    Asserted on her own rows on purpose. A private row a reader cannot see is not
    in the feed at all, so the only rows that can prove the gate is the address
    and not the page are the ones that render for the person who wrote them.
    """
    rows = {
        name: _post(alice, dune, name, visibility=vis)
        for name, vis in (
            ("own-private", Visibility.PRIVATE),
            ("own-followers", Visibility.FOLLOWERS),
        )
    }
    # Her own feed, not Local: Local is the public-posts scope, and the only
    # reader who may open these two is the person who wrote them.
    body = _feed(alice)
    for name, post in rows.items():
        bar = _bar(_feed_block(body, post.pk))
        assert "share" not in _controls(bar), f"{name} grew a share button"
        assert "applaud-btn" in bar, f"{name} lost the controls it should keep"


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_list_row_shares_the_list_page_not_its_face(alice, dune, admin):
    """The list id arrives from the strip pass's own batch (``list_id``), which is
    what lets a list row mint ``/list/`` at the feed's fixed query count. A row
    whose list is gone has no id and so no address — minted from the face's
    ``/status/`` path it would hand out a page that redirects."""
    film_list = create_list(
        alice, title="Noir", films=[dune], visibility=Visibility.PUBLIC
    )
    body = _feed(alice, "?scope=local")
    span = _share_span(_bar(_feed_block(body, film_list.status_id)))
    assert f'data-share-url="{CANON}/list/{film_list.pk}/"' in span
    assert f"/status/{film_list.status_id}/" not in span


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_mirror_row_shares_its_home_address(alice, dune, admin):
    """D6: the mirror's row shares the peer's address, not our copy of it."""
    mirror = Status.objects.create(
        user=_remote_author(),
        film=dune,
        local=False,
        remote_url="https://peer.example/notes/7",
        status_type=Status.Type.COMMENT,
        content="<p>Mirrored.</p>",
    )
    body = _feed(alice, "?scope=all")
    bar = _bar(_feed_block(body, mirror.pk))
    assert 'data-share-url="https://peer.example/notes/7"' in bar


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_mirror_row_with_no_usable_home_url_carries_no_control(alice, dune, admin):
    """The scheme gate at the row: the string is peer-supplied and the button
    would copy it verbatim, so a non-http note id buys no button."""
    mirror = Status.objects.create(
        user=_remote_author(),
        film=dune,
        local=False,
        remote_url="javascript:alert(1)",
        status_type=Status.Type.COMMENT,
        content="<p>Mirrored badly.</p>",
    )
    bar = _bar(_feed_block(_feed(alice, "?scope=all"), mirror.pk))
    assert "share" not in _controls(bar)
    assert "javascript" not in bar
    assert "applaud-btn" in bar


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_fragment_route_carries_the_control(alice, bob, dune, admin):
    """The endless scroll splices this markup into the open page, so a control
    that exists on page 1 and not in the fragment is a button that stops working
    after the first scroll."""
    post = _post(bob, dune, "fragment-row")
    body = _feed(alice, "feed/page/?scope=local")
    assert f'data-share-url="{CANON}/status/{post.pk}/"' in _bar(
        _feed_block(body, post.pk)
    )


@pytest.mark.django_db
def test_the_home_feed_ships_the_script_to_a_signed_out_reader(db, admin):
    """The one page that ships it unconditionally: rows arrive later by scroll,
    and the first page cannot know whether the next cursor holds a control."""
    assert "js/share.js" in _feed(None)


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_profile_row_carries_the_share_control(alice, bob, review):
    """The profile draws the same partial (D6), so it inherits the control — and
    has to ship the script for it, which the page's own likes.js rule already
    states: the script follows the control, not the page."""
    body = _page(bob, reverse("user-profile", args=["alice"]))
    bar = _bar(_feed_block(body, review.pk))
    assert f'data-share-url="{CANON}/status/{review.pk}/"' in bar
    assert "js/share.js" in body


@pytest.mark.django_db
def test_the_profile_ships_the_script_only_with_a_shareable_row(alice, dune):
    """The mirror image of R85, on the page that can afford it: a profile whose
    only review is private draws no control, so it ships no script either.

    The home feed breaks this rule on purpose and says why; the profile is
    re-rendered whole per page and has no excuse.
    """
    Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW,
        content="<p>Private.</p>",
        visibility=Visibility.PRIVATE,
    )
    body = _page(alice, reverse("user-profile", args=["alice"]))
    assert "share-btn" not in body
    assert "js/share.js" not in body


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_share_address_costs_no_queries_as_the_feed_grows(alice, dune):
    """The address is minted from what the row already carries.

    Invariance, not a ceiling, for the reason ``test_feed_query_count.py`` gives:
    a ceiling passes for any N loose enough to fit whatever the fixture happened
    to build. What breaks if someone reads a lazy FK per row is the count moving
    when the feed grows, so that is what is measured — the same queryset shape
    measured twice with twelve more rows in the middle.
    """

    def build():
        with CaptureQueriesContext(connection) as captured:
            entries, _ = public_feed(None, local_only=True, limit=FEED_PAGE_SIZE + 50)
        return [q["sql"] for q in captured.captured_queries], len(entries)

    for n in range(3):
        _post(alice, dune, f"small-{n}")
    small_sql, small_rows = build()
    for n in range(12):
        _post(alice, dune, f"large-{n}")
    large_sql, large_rows = build()

    assert large_rows > small_rows, (
        "the feed did not grow, so the count below proves nothing"
    )
    assert len(large_sql) == len(small_sql), (
        f"the feed's query count grew from {len(small_sql)} to {len(large_sql)} "
        f"when rows were added — the share address is being minted from a "
        f"per-row lookup again"
    )


# --- the stylesheet and the drawing ------------------------------------------


def test_the_button_takes_the_applaud_contract():
    """26px, no border, ``center / contain`` — the row's three icons share a box
    and a fourth that is drawn to a different size reads as a different class."""
    decl = _decl(".share-btn")
    assert "width: 26px" in decl and "height: 26px" in decl
    assert "border: 0" in decl
    assert 'url("../images/share-icon.svg") center / contain no-repeat' in decl


def test_the_hover_rule_outspecs_the_base_button_fill():
    """``button:hover`` paints a red plate at a specificity a bare class loses
    to, so the hover rule carries the element name and the fill reset."""
    decl = _decl("button.share-btn:hover")
    assert "transform: scale(1.08)" in decl
    assert "background-color: transparent" in decl


def test_the_notice_is_a_pinned_pill_not_a_line_of_text():
    """Cream pill on near-black ink, mono like every count in the row, and fixed
    out of every layout — the point of the ruling is that nothing on the page can
    move because this appeared. ``pointer-events: none`` so a notice never eats a
    click aimed at what is underneath it, and the z-index clears the finish-flow
    modal at 100 because a toast behind the dialog you pressed inside is no toast."""
    decl = _decl(".share-toast")
    assert "position: fixed" in decl
    assert "pointer-events: none" in decl
    assert "z-index: 200" in decl
    assert "border-radius: 999px" in decl
    assert "background: var(--cream)" in decl
    assert "ui-monospace" in decl


def test_the_drawing_is_shipped_square_and_in_the_site_cream():
    """One file, no idle/mine pair: a share has no state to show, so there is
    nothing for a second drawing to say (``SHARE_PLAN.md`` S2)."""
    text = (IMAGES / "share-icon.svg").read_text()
    assert ElementTree.fromstring(text).tag.endswith("svg")
    match = re.search(r'viewBox="0 0 (\d+) (\d+)"', text)
    assert match and match.group(1) == match.group(2), "not a square canvas"
    assert set(re.findall(r"#[0-9a-fA-F]{6}", text)) == {CREAM}
