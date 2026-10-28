"""N8: the page measure and the gutters beside it.

The owner's ask, read off two screenshots: pull the page toward the centre the
way Mastodon does, so there is empty space down both sides instead of content
spread across the whole window. Measured on the deployed stack before the change,
the 72rem home band left 16px between the rail's right edge and the window at a
1440 viewport, and the feed column was 721px against the reference's 600px.

Three numbers hold the design and each one is load-bearing on its own, so each
gets a guard: the 62rem band cap (the gutter), the 20rem rail (what pays for the
feed's column inside that band), and the four-track body grid that puts the menu
*inside* the centred band instead of glued to the left window edge. The fourth
guards the arithmetic between them: change one value in isolation and the feed
column is what silently moves.

These are source guards, like the other CSS pins in this suite. What the rules
actually render -- gutters visible at 1440 and 1869, no horizontal scroll at
320/375/768/1024/1440 -- was measured in a real browser on the deployed stack;
the numbers are in the plan's N8 entry.
"""

import re
from pathlib import Path

CSS_PATH = (
    Path(__file__).resolve().parents[1] / "social" / "static" / "css" / "reeltalk.css"
)

# Comments stripped once, up front: they sit between a selector and its brace and
# carry quoted CSS of their own, which would break brace matching if left in.
CSS = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)

ROOT_PX = 16
MENU_PX = 280
VIEWPORT = 1440


def _rem(value: str) -> float:
    match = re.fullmatch(r"([\d.]+)rem", value.strip())
    assert match, f"{value!r} is not a rem value"
    return float(match.group(1)) * ROOT_PX


def _decl(selector: str, prop: str) -> str:
    """The first ``prop`` declared by the exact rule whose selector is ``selector``.

    Rules are found without anchoring on the previous rule's closing brace: that
    anchor makes the scan consume it and skip every second rule in the file.
    """
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        selectors = [s.strip() for s in match.group(1).split(",")]
        if selector not in selectors:
            continue
        for part in match.group(2).split(";"):
            name, _, value = part.partition(":")
            if name.strip() == prop:
                return value.strip()
    raise AssertionError(f"no {prop!r} in the {selector!r} rule")


def _desktop_block() -> str:
    """Source of every ``@media`` block whose header mentions min-width:1024px."""
    bodies = []
    for match in re.finditer(r"@media\s*([^{]+)\{", CSS):
        if "min-width:1024px" not in re.sub(r"\s+", "", match.group(1)):
            continue
        depth, i = 1, match.end()
        while depth:
            depth += CSS[i] == "{"
            depth -= CSS[i] == "}"
            i += 1
        bodies.append(CSS[match.end() : i - 1])
    assert bodies, "no desktop (min-width: 1024px) block in the shipped stylesheet"
    return "".join(bodies)


def _desktop_decl(selector: str, prop: str) -> str:
    block = _desktop_block()
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", block):
        selectors = [re.sub(r"\s+", " ", s.strip()) for s in match.group(1).split(",")]
        if selector not in selectors:
            continue
        for part in match.group(2).split(";"):
            name, _, value = part.partition(":")
            if name.strip() == prop:
                return value.strip()
    raise AssertionError(f"no {prop!r} for {selector!r} in the desktop block")


def test_the_home_band_is_capped_at_62rem():
    """72rem was the marquee band's width, not a reading measure. At a 1440
    viewport it left 16px of gutter and a 721px feed column."""
    assert _decl(".container.wide-main", "max-width") == "62rem"


def test_the_gutters_stay_visible_at_a_1440_window():
    """The whole point of the increment, as arithmetic: the band must not eat the
    track beside the menu. (1440 - 280 - 992) / 2 = 84px per side."""
    band = _rem(_decl(".container.wide-main", "max-width"))
    gutter = (VIEWPORT - MENU_PX - band) / 2
    assert gutter >= 64, f"only {gutter:.0f}px of gutter at {VIEWPORT}px"


def test_the_rail_pays_for_the_feed_column_not_the_other_way_round():
    """62rem minus the container's padding, the column gap and a 20rem rail is a
    600px feed, the reference's column. Keep the old 22rem rail and the feed is
    what shrinks; grow the cap and the gutters go instead."""
    band = _rem(_decl(".container.wide-main", "max-width"))
    columns = _decl(".home-grid", "grid-template-columns").split()
    gap = _rem(_decl(".home-grid", "gap"))
    padding = 2 * _rem(_decl(".container", "padding").split()[1])
    rail = _rem(columns[-1])
    feed = band - padding - gap - rail
    assert rail == 320, f"rail is {rail:.0f}px, not the 320px that pays for the feed"
    assert 560 <= feed <= 640, f"feed column would be {feed:.0f}px"


def test_the_menu_sits_inside_the_centred_band():
    """Two auto tracks outside the menu and content tracks, so the free space
    lands on both sides of everything. A two-track grid (menu then content) can
    only ever collect it on the right."""
    assert _desktop_decl("body:has(.site-nav)", "grid-template-columns") == (
        "minmax(0, 1fr) 280px minmax(0, 62rem) minmax(0, 1fr)"
    )
    assert _desktop_decl(".site-nav", "grid-column") == "2"
    assert _desktop_decl("body:has(.site-nav) main", "grid-column") == "3"


def test_the_header_content_is_capped_to_the_band():
    """The header keeps its full-bleed paint, but its content has to stop at the
    band or the profile corner floats away from the column it belongs to while
    the wordmark drifts out past the menu."""
    assert _desktop_decl(".site-header .container", "max-width") == (
        "calc(280px + 62rem)"
    )
    assert _desktop_decl(".site-header .container", "margin") == "0 auto"


def test_the_band_exists_in_one_place_only():
    """N8 is a desktop measure change and the phone surfaces must stay out of its
    reach: below 1024px ``body`` is not a grid at all, the bottom bar and the
    More sheet live in the max-width:1023px block. Asserting the band
    declaration occurs exactly once in the stylesheet is what keeps a second
    copy from being added somewhere it can reach the narrow layout."""
    flat = re.sub(r"\s+", " ", CSS)
    assert flat.count("minmax(0, 1fr) 280px minmax(0, 62rem) minmax(0, 1fr)") == 1
