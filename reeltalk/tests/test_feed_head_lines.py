"""The feed row's head is two lines (owner pass, 2026-11-13).

The head used to be a single wrapping flex line: name, stars, date, audience
marker and the two counters, side by side until it ran out of room. A short local
name fitted and read as one row of facts. A long remote handle like
``phil@files.shownotes.ooguy.com`` did not, and everything after it dropped to a
line of its own — so the same post was laid out two different ways depending on
how many characters its author's handle happened to carry. Worst on a phone, where
half the line is gone before the stars arrive.

The owner's ruling fixes the shape rather than the symptom: the author's name owns
the first line, the post's metadata owns the second, on desktop and mobile alike.
That is a grouping fact, so it lives in the markup — a ``.review-meta`` wrapper
that the stylesheet forces onto its own flex line — not in a ``<br>`` or a
breakpoint guess.

The same pass then took the two counters off that line. They made it five items
wide, which a phone column cannot hold — the reply counter wrapped onto a line of
its own, so the row read differently again at every width — and they are controls,
not facts, so they never belonged among the facts. They now close the card on the
right, after the review prose, which is the split Mastodon draws between what a
post says and what you can do with it. One wrapper at every width, because a
control cannot live in two places and a second copy would be a second click target
to keep honest.

Two halves are pinned. The rendered row: what stays above the wrapper, what sits
inside it, and where the controls ended up. The stylesheet: that the wrapper is
really the forced break, since a wrapper nobody styled would draw one long line
again while the markup still looked exactly right. The controls' escape from the
row's click overlay is pinned in test_feed_click_target.py, which owns that rule.
"""

import re
from pathlib import Path

import pytest
from django.test import Client

from reeltalk.core.models import Film, Status, mark_watched
from reeltalk.lists.services import create_list
from reeltalk.tests.members import member, site_admin

PASSWORD = "s3cretpass"
CSS_PATH = (
    Path(__file__).resolve().parents[1] / "social" / "static" / "css" / "reeltalk.css"
)


@pytest.fixture
def alice(db):
    return member(localname="alice", password=PASSWORD)


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def admin(db):
    # R12: / redirects to /setup/ until a superuser exists, so the feed never
    # renders and an absence assertion against that body proves nothing.
    return site_admin(localname="admin", password=PASSWORD)


def _row(who, status_id) -> str:
    """One home-feed row's own markup, so a check cannot borrow a neighbour's."""
    client = Client()
    assert client.login(username=who, password=PASSWORD)
    body = client.get("/").content.decode()
    marker = f'<li class="review" data-status="{status_id}"'
    assert marker in body, f"the home feed holds no row for status {status_id}"
    start = body.index(marker)
    return body[start : body.index("</li>", start)]


def _meta(row) -> str:
    """The metadata line's own markup.

    The wrapper holds no nested ``div``, so the first closing tag after it is its
    own. Asserting the opening tag is present is what stops a missing wrapper from
    reading as an empty line rather than as the failure it is.
    """
    assert '<div class="review-meta">' in row, "the row has no metadata line at all"
    start = row.index('<div class="review-meta">') + len('<div class="review-meta">')
    return row[start : row.index("</div>", start)]


def _actions(row) -> str:
    """The action row's own markup, empty when the row carries no controls.

    Same shape as ``_meta``: the wrapper holds spans, no nested ``div``, so the
    first closing tag after it is its own.
    """
    open_tag = '<div class="row-actions">'
    if open_tag not in row:
        return ""
    start = row.index(open_tag) + len(open_tag)
    return row[start : row.index("</div>", start)]


def _css_rules() -> list[tuple[list[str], str]]:
    """(selectors, declarations) for every rule in the shipped stylesheet.

    Comments go first because they sit between a rule's selector and its brace,
    and whitespace is collapsed so the assertions key on the declarations rather
    than on the formatting.
    """
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)
    css = re.sub(r"\s+", " ", css)
    out = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = [s.strip() for s in match.group(1).split(",") if s.strip()]
        out.append((selectors, match.group(2).strip()))
    return out


def _css_rule(selector: str) -> str:
    """Declarations of the rule whose selector list is exactly ``selector``."""
    for selectors, declarations in _css_rules():
        if selectors == [selector]:
            return declarations
    raise AssertionError(f"no rule for {selector} in the shipped stylesheet")


def _css_rule_under_media(condition: str, selector: str) -> str:
    """Declarations of ``selector`` inside the ``@media`` block naming ``condition``.

    The flat parser cannot tell a scoped rule from a global one, and this row's
    desktop compensation is only correct inside its breakpoint, so the scope is
    part of what gets pinned. One nesting level: the sheet has no nested media
    block.
    """
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)
    css = re.sub(r"\s+", " ", css)
    for block in re.finditer(r"@media([^{]+)\{((?:[^{}]|\{[^{}]*\})*)\}", css):
        if condition not in block.group(1):
            continue
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", block.group(2)):
            selectors = [s.strip() for s in rule.group(1).split(",") if s.strip()]
            if selectors == [selector]:
                return rule.group(2).strip()
    raise AssertionError(f"no rule for {selector} under @media {condition}")


def _row_gaps_named(selector: str) -> list[str]:
    """Every ``row-gap`` any rule naming ``selector`` sets."""
    gaps = []
    for selectors, declarations in _css_rules():
        if selector not in selectors:
            continue
        gaps += re.findall(r"row-gap:\s*([^;]+)", declarations)
    assert gaps, f"nothing sets a row-gap through {selector}"
    return gaps


# --- The rendered row --------------------------------------------------------


@pytest.mark.django_db
def test_the_author_name_sits_above_the_metadata_line(alice, dune, admin):
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    row = _row("alice", review.pk)
    assert 'class="review-author"' in row
    assert row.index('class="review-author"') < row.index('<div class="review-meta">')
    assert 'class="review-author"' not in _meta(row)


@pytest.mark.django_db
def test_the_stars_date_and_marker_share_the_metadata_line(alice, dune, admin):
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    meta = _meta(_row("alice", review.pk))
    for part in ('class="stars"', 'class="post-date"', 'class="audience"'):
        assert part in meta, f"the metadata line lost {part}"


@pytest.mark.django_db
def test_the_controls_close_the_card_after_the_prose(alice, dune, admin):
    # The two counters used to ride the end of the metadata line, which made it
    # five items wide: on a phone column the reply counter dropped onto a line of
    # its own, so the row read differently at every width. They are controls, not
    # facts, so they now close the card after the review prose instead.
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    row = _row("alice", review.pk)
    meta = _meta(row)
    for control in ("applaud-btn", "reply-btn"):
        assert f'class="{control}"' not in meta, f"{control} is back on the facts line"
    actions = _actions(row)
    assert 'class="applaud-btn"' in actions and 'class="reply-btn"' in actions
    assert row.index('class="review-body"') < row.index('<div class="row-actions">')


def test_the_action_row_spans_the_card_and_hugs_the_right_edge():
    # The row is a grid whose first column is the poster. Left to itself the
    # appended div shrink-wraps into that column and draws flush left, which is
    # the opposite of the ask, so the span is load-bearing rather than tidy.
    rule = _css_rule(".feed .row-actions")
    assert "display: flex" in rule
    assert "grid-column: 1 / -1" in rule
    assert "justify-content: flex-end" in rule


@pytest.mark.django_db
def test_the_list_verb_stays_on_the_name_line(alice, dune, admin):
    # "alice created a new list" is one sentence (R139 line 1). Dropping the verb
    # onto the metadata line would leave the name reading as a subject with no
    # predicate and the line as a fragment with stars bolted on.
    film_list = create_list(alice, title="Noir", films=[dune])
    row = _row("alice", film_list.status_id)
    assert 'class="feed-verb"' in row
    assert row.index('class="feed-verb"') < row.index('<div class="review-meta">')
    assert "feed-verb" not in _meta(row)


# --- The stylesheet that makes it two lines ----------------------------------


def test_the_metadata_line_is_forced_onto_its_own_flex_line():
    # The head is a wrapping flex row, so a full-width basis on this wrapper is
    # what breaks the line. Without it the markup is correct and the page is
    # wrong, which is exactly the failure this file exists to catch.
    rule = _css_rule(".review-meta")
    assert "flex-basis: 100%" in rule
    assert "display: flex" in rule


def test_the_two_head_lines_have_no_gap_between_them():
    # The owner's second pass of 2026-11-13: two thirds of the old 8px was still
    # too loose. Measured at 1:1, the name's ink sat 14px above the stars while
    # the poster sits 9px below them, so the row still read as two stacked rows.
    # Zero is the floor a flex gap can take, and it is enough: the ink lands at
    # 9px against the poster's 9px. The wrapper keeps its own 5px for the one
    # place it wraps internally — a phone's metadata line — and the profile
    # review list keeps the 8px it took from its own base, because measured there
    # the two gaps are already equal at 20px each, so tightening that surface
    # would make it uneven in the other direction.
    assert set(_row_gaps_named(".feed .review-head")) == {"0"}
    assert _row_gaps_named(".feed .review-meta") == ["5px"]
    assert _row_gaps_named(".profile-review-list .review-head") == ["8px"]


def test_the_poster_is_as_close_as_the_metadata_line_is():
    # Same pass, second ask: the name-to-stars gap and the stars-to-poster gap
    # have to read as one distance. Measured, the head's 8px bottom margin put
    # the poster 13.3px under the stars against 5.7px name-to-stars; the star
    # drawing carries its own slack inside the metadata line box, so with this
    # margin at zero and the head's row-gap at zero both ink gaps land at 9px.
    # Scoped to rows carrying a poster.
    rule = _css_rule(".feed .review:has(> .feed-item) > .review-head")
    assert "margin-bottom: 0" in rule


def test_the_desktop_gap_buys_back_the_pixel_the_snap_costs():
    # The owner's follow-up of 2026-11-14: on desktop the poster still sat closer
    # to the stars than the stars sat to the name. Measured at 3x, every window up
    # to 1023px is even (8.33px above, 7.67px below) and every window from 1024px
    # is not (9.33 against 7.00), with the row's own metrics identical on both
    # sides of that breakpoint. The desktop block pays the 2px back. Both halves
    # are pinned because either alone is a bug: the compensation without the base
    # zero over-spaces every narrower window, and the base zero without the
    # compensation is the ask this closes.
    poster_head = ".feed .review:has(> .feed-item) > .review-head"
    assert "margin-bottom: 0" in _css_rule(poster_head)
    desktop = _css_rule_under_media("(min-width: 1024px)", poster_head)
    assert "margin-bottom: 2px" in desktop


def test_the_poster_is_dropped_onto_the_title_first_line():
    # Owner rule of 2026-11-14: the poster's top edge is level with the first
    # line of the title beside it, and it is the POSTER that moves down to meet
    # the type - the owner rejected lifting the type up. The two boxes already
    # shared a top, since .feed-item is display: contents and the poster and the
    # title are grid items of the row, so the 5px between them is the leading a
    # 16px line on a 23.2px line-height carries above the capitals: measured
    # 5.0px at 1440 and 360, 5.33 at a phone's 3x. That is the one number in this
    # row that agrees between machines, because the title is a loaded webfont
    # while the star glyph is whatever the device substitutes.
    want = ".feed .feed-item .thumb"
    rules = [d for selectors, d in _css_rules() if selectors == [want]]
    dropped = [d for d in rules if "margin-top: 5px" in d]
    assert dropped, "nothing drops the poster onto the title's first line"
    title = [d for s, d in _css_rules() if s == [".feed .feed-item .feed-film"]]
    assert not [d for d in title if "top: -5px" in d], "the type is being lifted"
