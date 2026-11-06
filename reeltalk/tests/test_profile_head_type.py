"""The profile head's type voice (owner pass, 2026-11-05).

The owner's complaint was that the handle and the follower count on the profile
head "do not really match with the rest of the site", and the measurement agreed:
everywhere else the site names a *person* it speaks the body face at weight 600 —
the feed row's author line (Archivo 600 / 15.2px), the header's own handle
(``.header-user a``), the tab strip directly beneath the head (Archivo 600 /
14.08px) — while the head's handle and counts were the only person-shaped text
wearing Oswald, the house face for *impersonal* metadata (counts, ranks, genre
pills, table headers).

So the two moved out of the ``var(--ui)`` group and onto the tab strip's voice:
face, weight and size together. Weight travels with the face for the reason the
feed scope tabs recorded on 2026-11-03 — at 400 the body face alone still reads
as a third voice rather than as the site's own.

The head is one partial (``social/_profile_head.html``) shared by the profile,
the member's films page and the lists page, so all three move on this one rule.
"""

import re
from pathlib import Path

CSS_PATH = (
    Path(__file__).resolve().parents[1] / "social" / "static" / "css" / "reeltalk.css"
)

# The head's two text lines. They are typed by one shared rule, so either name
# stands in for the pair; that they travel together is its own assertion below.
HEAD = ".handle"
COUNTS = ".profile-counts"
TAB = ".tabs a"


def _css_rules() -> list[tuple[list[str], str]]:
    """(selectors, declarations) for every rule in the shipped stylesheet.

    Comments go first because they sit between a selector and its brace. Runs of
    whitespace collapse to a single space rather than vanishing: a descendant
    selector like ``.tabs a`` has to keep its space or it turns into a compound
    one and the assertion silently tests a selector nobody wrote.
    """
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)
    css = re.sub(r"\s+", " ", css)
    rules = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = [s.strip() for s in match.group(1).split(",") if s.strip()]
        rules.append((selectors, match.group(2).strip()))
    return rules


def _rules_naming(selector: str) -> list[tuple[list[str], str]]:
    """Every rule whose selector list names ``selector`` exactly, in file order."""
    return [
        (selectors, declarations)
        for selectors, declarations in _css_rules()
        if selector in selectors
    ]


def _declared(selector: str, prop: str) -> list[str]:
    return [
        declarations
        for _selectors, declarations in _rules_naming(selector)
        if f"{prop}:" in declarations
    ]


def _value(selector: str, prop: str) -> str:
    """The value a property ends up with for a selector.

    Last declaration in file order wins, which is what the browser does here:
    every rule in play has the same specificity, so the cascade is decided by
    order alone. ``font-size`` really is declared twice for ``.handle`` — the
    profile section's 0.9rem and the roles block's 0.88rem — and the second is
    the one that renders.
    """
    found = [
        re.search(rf"{prop}:\s*([^;]+)", declarations).group(1).strip()
        for declarations in _declared(selector, prop)
    ]
    assert found, f"{selector} never declares {prop}"
    return found[-1]


def test_the_profile_head_wears_the_body_face():
    assert _value(HEAD, "font-family") == "var(--body)"
    assert _value(COUNTS, "font-family") == "var(--body)"


def test_the_profile_head_is_no_longer_metadata_oswald():
    # The negative that makes the change real: both selectors used to sit in the
    # group that claims ``var(--ui)``, and nothing but this assertion stops a
    # future edit from sliding them back for the sake of tidiness.
    offenders = [
        selectors
        for selectors, declarations in _css_rules()
        if "var(--ui)" in declarations and any(s in (HEAD, COUNTS) for s in selectors)
    ]
    assert offenders == [], f"the head is back on the metadata face: {offenders}"


def test_the_profile_head_matches_the_tab_strip_it_sits_over():
    # The owner's ask was continuity with the strip immediately below, so the two
    # are pinned to each other rather than to numbers each could satisfy alone.
    for prop in ("font-family", "font-weight", "font-size"):
        assert _value(HEAD, prop) == _value(TAB, prop), prop


def test_both_head_lines_are_typed_by_the_same_rule():
    # Two rules that happen to agree today would drift apart tomorrow. One rule
    # naming both selectors is what keeps the handle and its counts one block.
    shared = [
        selectors
        for selectors, declarations in _rules_naming(HEAD)
        if COUNTS in selectors and "font-family" in declarations
    ]
    assert shared == [[HEAD, COUNTS]], shared


def test_the_face_is_decided_in_one_place_per_selector():
    # The size and colour rules for the head live up in the profile section; the
    # face belongs to the roles block at the foot of the file, which is placed
    # last on purpose so it wins over the ``font: inherit`` shorthands. Two rules
    # naming a face for one selector is how a face silently stops applying.
    for selector in (HEAD, COUNTS):
        assert len(_declared(selector, "font-family")) == 1, selector


def test_the_roles_rule_is_the_last_word_for_the_head():
    # Order is what the browser resolves with: the profile section still sets
    # 0.9rem and the roles block sets 0.88rem, so the roles block has to come
    # after the head's own rules for any of this to render.
    last = _rules_naming(HEAD)[-1][1]
    assert "font-family: var(--body)" in last, last
    assert "font-size: 0.88rem" in last, last
