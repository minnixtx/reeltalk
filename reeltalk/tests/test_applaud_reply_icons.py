"""The applaud and reply artwork (owner pass, 2026-11-15).

The clapperboard and the old speech bubble were replaced with a pair of clapping
hands and a bubble with film sprockets in it, and the ink rule changed with the
art: red now means the READER did the thing, not that anyone did. Five ways that
swap could rot, one test each:

* **An artwork that is not there.** A CSS ``background-image`` that points at a
  missing file fails silently — the control renders as an empty box, the click
  still works, and nothing anywhere reports it. The only honest proof is that
  every path the stylesheet names resolves to a file on disk.
* **A derived drawing that drifted.** The red reply bubble was generated from
  the cream one by swapping the fill, because a CSS background-image cannot take
  a colour from the stylesheet. If someone edits one of the pair, the control
  stops being one bubble in two colours and becomes two bubbles.
* **A red that is no longer the site's red.** Both red drawings are the link red
  by owner ruling, so a red control and a red link read as one colour. The guard
  reads that value out of the stylesheet instead of copying a hex in here, where
  it would go stale the day ``--link`` moves.
* **A drawing that will not render.** Malformed XML in an SVG background is as
  silent as a missing file.
* **The two controls drawn at different scales.** They sit side by side in the
  row's action bar, and the old set had to be registered by hand because its
  three PNGs were drawn at different sizes. The two drawings are on different
  canvases (the hands on 32 units, the bubble on 24), so what keeps them the
  same size on screen is that each canvas is square — ``center / contain`` maps
  a square canvas onto the shared 26px box without squashing an axis.

The rendered-page half of the ink rule — which control wears which state, and
for whom — lives in ``test_likes.py`` and ``test_comments.py``, next to the
views that decide it. This module holds only the artwork and the stylesheet.
"""

import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CSS_PATH = REPO_ROOT / "reeltalk" / "social" / "static" / "css" / "reeltalk.css"
IMAGES = REPO_ROOT / "reeltalk" / "social" / "static" / "images"

# The four drawings this pass shipped, in the order the controls read: cream
# hands, red hands, cream bubble, red bubble.
DRAWINGS = (
    "applaud-icon-idle.svg",
    "applaud-icon-mine.svg",
    "reply-icon-idle.svg",
    "reply-icon-mine.svg",
)

# What the swap left behind. ManifestStaticFilesStorage hashes and serves every
# file in the static tree whether or not a rule points at it, so a retired
# drawing that stays in the folder is dead weight on every deploy — the reason
# the old set never shipped an unused solid-red bubble.
RETIRED = (
    "applaud-icon-idle.png",
    "applaud-icon-others.png",
    "applaud-icon-mine.png",
    "reply-icon-idle.png",
    "reply-icon-others.png",
)


def _css() -> str:
    """The stylesheet with its comments stripped, so a name that survives only
    in prose cannot satisfy an assertion about the rules."""
    return re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)


CREAM = "#F5E5C5"


def _link_red() -> str:
    """The sheet's own ``--link``, upper-cased the way the drawings spell it.

    Read out of the stylesheet rather than copied in here. The owner put the red
    art on the link red, so the sheet is the source of truth and a literal in
    this file would only go stale the day someone moves it.
    """
    match = re.search(r"--link:\s*(#[0-9a-fA-F]{6})\s*;", _css())
    assert match, "--link is no longer a plain six-digit hex in the sheet"
    return match.group(1).upper()


def _hexes(name: str) -> set:
    """Every colour a drawing spells out for itself."""
    return set(re.findall(r"#[0-9a-fA-F]{6}", (IMAGES / name).read_text()))


def test_the_four_drawings_are_on_disk():
    for name in DRAWINGS:
        assert (IMAGES / name).is_file(), f"{name} never landed in the static tree"


@pytest.mark.parametrize("name", DRAWINGS)
def test_each_drawing_is_parsable_svg(name):
    """A drawing the browser cannot parse draws nothing, and says so nowhere.

    Asserting the root element too: a file that parses as XML but is not an
    ``<svg>`` is a blank control all the same.
    """
    root = ElementTree.fromstring((IMAGES / name).read_text())
    assert root.tag.endswith("svg"), f"{name} is not an SVG document"


def test_the_stylesheet_points_at_all_four():
    css = _css()
    for name in DRAWINGS:
        assert f'url("../images/{name}")' in css, f"the sheet no longer uses {name}"


def test_the_retired_drawings_are_gone_from_both():
    css = _css()
    for name in RETIRED:
        assert name not in css, f"the sheet still names the retired {name}"
        assert not (IMAGES / name).exists(), f"{name} is still shipped but unused"


def test_every_image_the_stylesheet_names_exists():
    """The general form of the guard, not just this swap's four files.

    A typo in a CSS ``url()`` is invisible: the page renders, the control is a
    blank box, and no test fails. Walking every image path in the sheet is what
    turns that into a red test.
    """
    named = re.findall(r'url\("\.\./images/([^"]+)"\)', _css())
    assert named, "the probe found no image rules at all"
    missing = [name for name in named if not (IMAGES / name).is_file()]
    assert not missing, f"the stylesheet names images that are not on disk: {missing}"


def test_the_red_bubble_is_the_cream_one_with_only_the_fill_swapped():
    """The derived drawing stays derived.

    ``reply-icon-mine.svg`` was generated from ``reply-icon-idle.svg`` by
    changing the fill and nothing else. Editing either one by hand turns the
    control into two different shapes wearing one slot, which is exactly the
    defect the retired PNG set carried.
    """
    cream = (IMAGES / "reply-icon-idle.svg").read_text()
    red = (IMAGES / "reply-icon-mine.svg").read_text()
    assert cream.replace(CREAM, "COLOR") == red.replace(_link_red(), "COLOR")


def test_the_two_applaud_drawings_differ_only_by_colour():
    """Same rule for the hands, which arrived as two files from the start. The
    revised drawing spells its colour twice — a root ``fill`` for the hands and
    a ``stroke`` on the sparks — so a swap has to land on both."""
    idle = (IMAGES / "applaud-icon-idle.svg").read_text()
    mine = (IMAGES / "applaud-icon-mine.svg").read_text()
    assert idle.replace(CREAM, "COLOR") == mine.replace(_link_red(), "COLOR")


def test_the_idle_pair_is_cream_and_the_mine_pair_is_the_link_red():
    """A red control and a red link are one red (owner ruling, 2026-11-15).

    The whole set of hexes is asserted, not merely that the right one appears: a
    drawing that picked up a second colour would render part of the control in
    it, and the state swap would stop being colour-only.
    """
    for name in ("applaud-icon-idle.svg", "reply-icon-idle.svg"):
        assert _hexes(name) == {CREAM}, f"{name} drifted off the cream"
    for name in ("applaud-icon-mine.svg", "reply-icon-mine.svg"):
        assert _hexes(name) == {_link_red()}, f"{name} drifted off --link"


def test_every_drawing_is_on_a_square_canvas():
    """The two controls share a 26px box but not a canvas — the hands are drawn
    on 32 units, the bubble on 24 — and ``center / contain`` maps each drawing's
    own canvas onto that box. A square canvas is what keeps the mapping from
    squashing one axis, which is the whole reason the pair stays matched at any
    zoom. Asserting squareness rather than one shared viewBox, because the
    viewBox is the owner's to change and the box is not."""
    for name in DRAWINGS:
        text = (IMAGES / name).read_text()
        match = re.search(r'viewBox="0 0 (\d+) (\d+)"', text)
        assert match, f"{name} declares no viewBox of its own"
        side = match.group(1)
        assert side == match.group(2), f"{name} is drawn on a non-square canvas"


def test_no_rule_left_behind_for_the_retired_state():
    """The ink rule is two states keyed on the reader, so the third selector is
    gone from both halves of the sheet: a rule that matches markup no template
    renders is a rule that will be "fixed" back into a state nobody chose."""
    css = _css()
    for selector in (".applaud-btn", ".reply-btn"):
        assert f'{selector}[data-state="others"]' not in css
