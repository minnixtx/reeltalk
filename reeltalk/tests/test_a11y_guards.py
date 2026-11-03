"""V4b/N7: the guards the QA sweep left behind.

The sweep measured the finished site in a browser rather than eyeballing it, and
three of what it found were colour or width rules that no diff would have shown.
These pins keep those three from quietly coming back.

The contrast pair is computed from the shipped palette, not from a screenshot:
WCAG relative luminance over the custom properties in ``:root``, so a future
palette edit fails a number instead of shipping an unreadable page. The star
track is held to the 3:1 bar for graphical objects (SC 1.4.11) rather than the
4.5:1 text bar because it is the graphic that says what a rating is out of, not
text; everything on the text side is held to 4.5:1.

The last two guards are pairing tests. The bug they prevent is the one the sweep
found twice: a template whose form class and a stylesheet whose textarea rule
drift apart, leaving a browser-default white plate on a near-black page. Each
test reads both sides so neither can be removed alone.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CSS_PATH = REPO_ROOT / "reeltalk" / "social" / "static" / "css" / "reeltalk.css"
PROFILE_TEMPLATE = (
    REPO_ROOT / "reeltalk" / "social" / "templates" / "social" / "profile_edit.html"
)
REPORT_TEMPLATE = (
    REPO_ROOT
    / "reeltalk"
    / "moderation"
    / "templates"
    / "moderation"
    / "_report_form.html"
)

CSS = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)


def _root_var(name: str) -> str:
    """A colour declared in ``:root``, as a ``#rrggbb`` string."""
    root = CSS[CSS.index(":root") : CSS.index("}", CSS.index(":root"))]
    match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", root)
    assert match, f"--{name} is not a six-digit colour in :root"
    return match.group(1)


def _ratio(foreground: str, background: str) -> float:
    """WCAG 1.4.3 contrast ratio between two ``#rrggbb`` colours."""

    def luminance(hex_colour: str) -> float:
        channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [
            c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            for c in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = (
        max(map(luminance, (foreground, background))),
        min(map(luminance, (foreground, background))),
    )
    return (high + 0.05) / (low + 0.05)


def _decl(selector: str, prop: str) -> str:
    """The first ``prop`` declared by the exact ``selector`` rule, anywhere in
    the sheet. Same shape as the CSS pins in ``test_page_measure.py``."""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        selectors = [re.sub(r"\s+", " ", s.strip()) for s in match.group(1).split(",")]
        if selector not in selectors:
            continue
        for part in match.group(2).split(";"):
            name, _, value = part.partition(":")
            if name.strip() == prop:
                return value.strip()
    raise AssertionError(f"no {prop!r} in the {selector!r} rule")


def test_the_star_track_clears_the_graphic_bar():
    """The unfilled layer of a star row is the graphic that says "out of five".
    It shipped as ``--line`` at 2.17:1 on the page, which is below the 3:1 bar
    for a graphical object; it now carries its own token."""
    ratio = _ratio(_root_var("star-empty"), _root_var("black"))
    assert ratio >= 3.0, f"--star-empty is {ratio:.2f}:1 on the page, needs 3:1"
    assert _decl(".stars-bg", "color") == "var(--star-empty)"


def test_the_text_colours_hold_aa_on_the_page():
    """Every colour this theme paints words in, against the page behind them.
    Measured during the sweep; the ones that failed were decorative only."""
    page = _root_var("black")
    for name in ("cream", "cream-muted", "white", "link", "gold", "error"):
        ratio = _ratio(_root_var(name), page)
        assert ratio >= 4.5, f"--{name} is {ratio:.2f}:1 on the page, needs 4.5:1"


def test_the_focus_ring_colour_is_visible_on_the_page():
    """A focus indicator is a graphical object (SC 1.4.11), so the amber the
    fields and the film actions use has to read against the page."""
    ratio = _ratio(_root_var("accent"), _root_var("black"))
    assert ratio >= 3.0, f"--accent is {ratio:.2f}:1 on the page, needs 3:1"


def test_the_site_wears_the_house_focus_ring():
    """V4b/N7 filed note 1, closed 2026-11-03 on the owner's approval.

    Focus rings used to be the browser's decision, surface by surface - fine
    on the build we measured, invisible-by-choice on another. One global rule
    now paints every link, button and disclosure summary with the house ring
    (2px, offset 3px, in ``--cream-muted`` - the deeper cream the owner
    picked over --cream, which read white on the dark), so a new surface is
    covered the day it is written rather than waiting to be enumerated. If
    this test fails, the keyboard path is back to the browser's default,
    which is the whole thing this pin prevents. Form fields deliberately
    ride their own border rule instead and are not in the list.
    """
    for selector in (
        "a:focus-visible",
        "button:focus-visible",
        "summary:focus-visible",
    ):
        assert _decl(selector, "outline") == "2px solid var(--cream-muted)"
        assert _decl(selector, "outline-offset") == "3px"
    # The ring colour has to read on the page: SC 1.4.11 wants 3:1, and this
    # pairs the rule above to the token it paints.
    ratio = _ratio(_root_var("cream-muted"), _root_var("black"))
    assert ratio >= 3.0, f"--cream-muted is {ratio:.2f}:1 on the page, needs 3:1"


def test_the_settings_form_and_its_field_rule_cannot_drift_apart():
    """The bio textarea shipped browser-default: a white plate at its intrinsic
    20-column width, which overflowed a 375px and a 320px viewport. The class on
    the form is what the stylesheet hangs on, so both sides are checked here."""
    assert 'class="profile-form"' in PROFILE_TEMPLATE.read_text(), (
        "profile_edit.html lost the class its textarea rule is keyed to"
    )
    assert _decl(".profile-form textarea", "width") == "100%"
    assert _decl(".profile-form textarea", "background") == "var(--field)"


def test_the_report_form_field_is_a_field_not_a_browser_default():
    """Same class of defect on the report box, which had no rule at all."""
    assert 'class="report-form"' in REPORT_TEMPLATE.read_text(), (
        "_report_form.html lost the class its textarea rule is keyed to"
    )
    assert _decl(".report-form textarea", "background") == "var(--field)"


def test_the_profile_head_wraps_instead_of_pushing_offscreen():
    """At 320px the avatar, the handle and the Follow control do not share a
    row, and the control sat 33px past the window edge."""
    assert _decl(".profile-head", "flex-wrap") == "wrap"
