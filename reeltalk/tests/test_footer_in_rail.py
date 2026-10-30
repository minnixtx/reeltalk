"""The footer moves into the right rail (§2I increment 1, R131).

Two placements, one body. ``base.html`` draws the wide three-zone band under
every page unless a page suppresses the ``site_footer`` block; ``home.html``
suppresses it and draws the same partial at the foot of ``.rail`` with the
``site-footer--rail`` modifier. The shared partial is the anti-drift
guarantee: the test asserting both placements render identical inner markup
fails the moment anyone edits one copy of the footer text into something the
other is not.

The rail follows normal document flow and scrolls with the feed. Its footer
remains reachable through the page scroll, without a separate scrollbar.

Two standing constraints are asserted here so they cannot be drifted past by
accident later: the home stays 2-pane (R64) and nothing in this increment is a
control (R82). The behaviour these rules actually produce — a rail that scrolls
with the feed, a footer reachable through the document scroll,
exactly one footer per document — is proven in a real browser, because a
string test cannot observe a stationary column.
"""

import re
from pathlib import Path

import pytest
from django.test import Client

from reeltalk.core.models import TRENDING_LIMIT
from reeltalk.tests.members import member, site_admin

CSS_PATH = (
    Path(__file__).resolve().parents[1] / "social" / "static" / "css" / "reeltalk.css"
)

# Comments stripped once, up front: they sit between a selector and its brace,
# and they carry quoted CSS of their own (``layout-single-column { … }``), which
# would break brace matching if left in.
CSS = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)


@pytest.fixture
def admin(db):
    # R12: / redirects to /setup/ until a superuser exists, so without this
    # the home page never renders and every absence assertion proves nothing.
    return site_admin(localname="admin", password="s3cretpass")


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


# --- Render helpers ---------------------------------------------------------


def _get(client, path) -> str:
    response = client.get(path)
    assert response.status_code == 200, f"{path} did not render"
    return response.content.decode()


def _member_client(alice) -> Client:
    client = Client()
    assert client.login(username="alice", password="s3cretpass")
    return client


def _element_spans(html: str, tag: str) -> list[dict]:
    """Every ``<tag>`` element as ``{attrs, inner, start, end}``, nesting-aware.

    Deliberately not one regex across the whole element: the footer holds a
    ``<nav>`` and an ``<svg>``, and the rail holds the footer, so containment
    is the thing under test and a flat match cannot show which element is
    inside which.
    """
    open_re = re.compile(rf"<{tag}((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.I)
    pair_re = re.compile(rf"</?{tag}\b[^>]*>", re.I)
    spans = []
    for match in open_re.finditer(html):
        depth = 1
        end = None
        for candidate in pair_re.finditer(html, match.end()):
            if candidate.group(0).lower().startswith("</"):
                depth -= 1
                if depth == 0:
                    end = candidate.start()
                    break
            else:
                depth += 1
        assert end is not None, f"unbalanced <{tag}> in the rendered page"
        spans.append(
            {
                "attrs": match.group(1),
                "inner": html[match.end() : end],
                "start": match.start(),
                "end": end,
            }
        )
    return spans


# --- CSS helpers ----------------------------------------------------------


def _rules_in(css: str) -> list[tuple[list[str], str]]:
    """(selectors, declarations) for every rule in a CSS blob.

    Whitespace is collapsed so assertions key on the declarations and not on
    formatting.
    """
    flat = re.sub(r"\s+", " ", css)
    rules = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", flat):
        selectors = [s.strip() for s in match.group(1).split(",") if s.strip()]
        rules.append((selectors, match.group(2).strip()))
    return rules


def _rules_for(selector: str, css: str = CSS) -> list[str]:
    """Every rule block that lists ``selector`` exactly.

    Returned as a list rather than one string so a duplicated or moved rule
    shows up as a count instead of silently picking one.
    """
    return [decls for selectors, decls in _rules_in(css) if selector in selectors]


def _rule_for(selector: str) -> str:
    matches = _rules_for(selector)
    assert matches, f"no {selector!r} rule in the shipped stylesheet"
    return matches[0]


def _decl_map(declarations: str) -> dict[str, str]:
    """``"a: 1; b: 2"`` → ``{"a": "1", "b": "2"}``.

    Exact values rather than substring checks: ``grid-template-columns: 1fr`` is
    a substring of ``grid-template-columns: 1fr auto 1fr``, so a substring
    assertion would pass on the wide three-zone rule it means to exclude.
    """
    out = {}
    for part in declarations.split(";"):
        if not part.strip():
            continue
        prop, _, value = part.partition(":")
        out[prop.strip()] = value.strip()
    return out


def _media_block(max_width: str) -> str:
    """The source inside ``@media (max-width: <max_width>) { … }``."""
    marker = f"@media (max-width: {max_width}) {{"
    assert marker in CSS, f"no {marker!r} block in the shipped stylesheet"
    start = CSS.index(marker) + len(marker)
    depth = 1
    pos = start
    while depth:
        char = CSS[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        pos += 1
    return CSS[start : pos - 1]


# --- The two placements ---------------------------------------------------


@pytest.mark.django_db
def test_home_renders_exactly_one_footer(admin, alice):
    body = _get(_member_client(alice), "/")
    assert len(_element_spans(body, "footer")) == 1


@pytest.mark.django_db
def test_the_home_footer_sits_inside_the_rail(admin, alice):
    body = _get(_member_client(alice), "/")
    rails = _element_spans(body, "aside")
    footers = _element_spans(body, "footer")
    assert len(rails) == 1, "the home page is expected to have exactly one rail"
    assert len(footers) == 1
    rail = rails[0]
    assert rail["start"] < footers[0]["start"]
    assert footers[0]["end"] <= rail["end"]


@pytest.mark.django_db
def test_the_footer_is_the_last_thing_in_the_rail(admin, alice):
    """The ask was the *bottom* of the rail, not merely somewhere in it."""
    body = _get(_member_client(alice), "/")
    rail = _element_spans(body, "aside")[0]
    footer = _element_spans(body, "footer")[0]
    # Containment first: without this guard the slice below runs past the end
    # of the rail's inner markup, comes back empty, and passes vacuously for a
    # footer that is not in the rail at all. Caught by mutation, not by reading.
    assert rail["start"] < footer["start"] < footer["end"] < rail["end"], (
        "the footer is not inside the rail, so 'last thing in it' cannot hold"
    )
    after = rail["inner"][footer["end"] - rail["start"] :]
    assert after.strip() == "", "something still renders below the footer in the rail"


@pytest.mark.django_db
def test_the_rail_footer_carries_the_modifier(admin, alice):
    body = _get(_member_client(alice), "/")
    attrs = _element_spans(body, "footer")[0]["attrs"]
    # Exact, not substring: `"site-footer" in attrs` would be satisfied by the
    # modifier alone and prove nothing about the base class being present.
    assert attrs.strip() == 'class="site-footer site-footer--rail"'


@pytest.mark.django_db
def test_the_rail_footer_renders_for_anonymous_visitors_too(admin):
    """The rail shows to everyone (R81), so the footer must not have landed
    inside the authenticated branch of the template."""
    body = _get(Client(), "/")
    footers = _element_spans(body, "footer")
    assert len(footers) == 1
    assert "site-footer--rail" in footers[0]["attrs"]


@pytest.mark.django_db
def test_a_non_home_page_keeps_the_wide_band():
    body = _get(Client(), "/about/")
    footers = _element_spans(body, "footer")
    assert len(footers) == 1
    assert footers[0]["attrs"].strip() == 'class="site-footer"'
    # Not in a rail: only the home page renders an aside at all.
    assert not _element_spans(body, "aside")


@pytest.mark.django_db
def test_both_placements_render_the_same_footer_body(admin, alice):
    """One partial, two wrappers — the reason base and home cannot drift.

    Compared line-by-line with trailing whitespace stripped and the block's
    leading/trailing blank lines dropped, because Django's ``include`` does not
    reindent: the blank line the partial's comment block leaves behind picks up
    whatever indentation the call site has, and each call site indents its
    closing tag differently. Every byte of markup and copy in between still has
    to match — any real drift in the footer's text, links, or structure fails
    this. The wrapper is excluded on purpose: it is the one thing allowed to
    differ, because the wrapper is what carries the class.
    """

    def normalise(markup: str) -> str:
        lines = [line.rstrip() for line in markup.splitlines()]
        return "\n".join(lines).strip()

    home = _element_spans(_get(_member_client(alice), "/"), "footer")[0]["inner"]
    about = _element_spans(_get(Client(), "/about/"), "footer")[0]["inner"]
    assert normalise(home) == normalise(about)
    # Non-vacuity: the body really is the footer, not two empty strings.
    assert "footer-brand" in home and "footer-signoff" in home


# --- The rail shares the document scroll ---------------------------------


@pytest.mark.django_db
def test_the_rail_scrolls_with_the_document():
    decls = _decl_map(_rule_for(".rail"))
    assert decls["position"] == "static"
    assert decls["max-height"] == "none"
    assert decls["overflow"] == "visible"
    assert "scrollbar-color" not in decls
    assert ".rail::-webkit-scrollbar" not in CSS


@pytest.mark.django_db
def test_below_the_stack_point_the_rail_returns_to_document_flow():
    """The narrow-layout mapping §2I records for Mastodon. Below 64rem the rail
    falls under the feed; a viewport-capped sticky scroller there would hide
    the footer behind its own scrollbar."""
    block = _media_block("64rem")
    rail_rules = [d for selectors, d in _rules_in(block) if ".rail" in selectors]
    assert len(rail_rules) == 1, "expected exactly one .rail rule in the 64rem block"
    decls = _decl_map(rail_rules[0])
    assert decls["position"] == "static"
    assert decls["max-height"] == "none"
    assert decls["overflow"] == "visible"
    assert decls["overscroll-behavior"] == "auto"


# --- The footer restacked for the 20rem column ----------------------------


@pytest.mark.django_db
def test_the_rail_footer_stacks_one_zone_per_row():
    decls = _decl_map(_rule_for(".site-footer--rail .container"))
    assert decls["display"] == "flex"
    assert decls["flex-direction"] == "column"
    assert decls["align-items"] == "center"
    # The 72rem band width belongs to the wide footer only; inside a 20rem
    # column it would just be dead space.
    assert decls["max-width"] == "none"


@pytest.mark.django_db
def test_the_rail_footer_sits_closer_with_a_lighter_seam():
    """The approved prototype thins the top border inside the rail: the footer
    follows the genres panel there, so the wide band's 3px seam reads as a
    second band boundary stacked on the first."""
    decls = _decl_map(_rule_for(".site-footer--rail"))
    assert decls["margin-top"] == "1.25rem"
    assert decls["border-top-width"] == "2px"
    wide = _decl_map(_rule_for(".site-footer"))
    assert wide["border-top"] == "3px solid var(--black-2)"


@pytest.mark.django_db
def test_the_wide_footer_band_rule_is_untouched():
    """Outside the rail this is a move, not a redesign."""
    decls = _decl_map(_rule_for(".site-footer .container"))
    assert decls["grid-template-columns"] == "1fr auto 1fr"
    assert decls["max-width"] == "72rem"


@pytest.mark.django_db
def test_the_rail_brand_block_centres_on_the_tagline():
    """The exact alignment settled in §2I: one auto column sized by the
    tagline, centred, with the wordmark at that column's left edge — so
    REELTALK's left edge meets the beginning of the phrase instead of the
    pair shrink-wrapping into a box with dead space after the tagline."""
    brand = _decl_map(_rule_for(".rail .footer-brand"))
    assert brand["display"] == "grid"
    assert brand["justify-content"] == "center"

    wordmark = _decl_map(_rule_for(".rail .footer-wordmark"))
    tagline = _decl_map(_rule_for(".rail .footer-tagline"))
    assert wordmark["grid-row"] == "1"
    assert tagline["grid-row"] == "2"
    assert wordmark["grid-column"] == tagline["grid-column"] == "1", (
        "both lines must share one column for the left edges to meet"
    )
    assert wordmark["justify-self"] == "start"
    assert tagline["justify-self"] == "center"


# --- Standing constraints -------------------------------------------------


@pytest.mark.django_db
def test_the_home_is_still_two_pane():
    """R64: no third column. The rail getting its own scroll is not an excuse
    for the layout to grow.

    20rem, not the 22rem this asserted until N8: the owner picked a 62rem home
    band with the menu inside it, and the rail is what paid for the feed's 600px
    column. The ticket art scales with the track, so the stubs step down ~9%."""
    decls = _decl_map(_rule_for(".home-grid"))
    assert decls["grid-template-columns"] == "minmax(0, 1fr) 20rem"


@pytest.mark.django_db
def test_trending_stays_at_eight_films():
    """R131: trimming to 5 was prototyped, measured and rejected — it saves
    166px against a footer that costs 170px, so the rail ends up taller
    either way and the trim buys nothing."""
    assert TRENDING_LIMIT == 8


@pytest.mark.django_db
def test_nothing_in_the_rail_footer_is_a_lit_control(admin, alice):
    """R82: no new lit-red control. The footer's only interactive parts are
    plain anchors, and nothing here adds a ``button``/``.btn`` to the rail."""
    body = _get(_member_client(alice), "/")
    rail = _element_spans(body, "aside")[0]
    footer = _element_spans(body, "footer")[0]
    assert "<button" not in rail["inner"]
    assert 'class="btn' not in rail["inner"]
    # 4, not 3: visibility V4 added the Help link (the audiences page is
    # where the operator-disk and moderation promises are written down).
    # The count is a drift-detector, not a rule about the number itself -
    # the rule is the two lines above, and the Help link honors it: plain
    # anchor, no ``.btn``.
    assert footer["inner"].count("<a ") == 4
