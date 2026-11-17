"""The moderation queue's submit controls: red like the rest, with room above.

The owner's ask, read off a screenshot with arrows on the two tight spots
(2026-11-17). Measured on the rendered page before the change, the report card's
Dismiss / Delete post row sat 0px under its note field and "Block this server"
sat 0px under its own, and every one of them wore the outlined treatment while
the rest of the site's form submits are flat red with a cream label.

This reverses a pinned decision, so the reversal is what is asserted: two
earlier tests used to prove these controls were *not* the red control (they are
updated alongside this file). Severity ordering survives because it never came
from colour alone -- the heavier verbs live in collapsed disclosures, so delete
is one click and everything above it is two with a warning in between.

Source guards, same shape as the other CSS pins in this suite.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CSS_PATH = REPO_ROOT / "reeltalk" / "social" / "static" / "css" / "reeltalk.css"
TEMPLATE = (
    REPO_ROOT / "reeltalk" / "moderation" / "templates" / "moderation" / "index.html"
)

CSS = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)
TEMPLATE_SRC = TEMPLATE.read_text()


def _decl(selector: str, prop: str) -> str:
    """The first ``prop`` declared by the exact ``selector`` rule."""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        selectors = [re.sub(r"\s+", " ", s.strip()) for s in match.group(1).split(",")]
        if selector not in selectors:
            continue
        for part in match.group(2).split(";"):
            name, _, value = part.partition(":")
            if name.strip() == prop:
                return value.strip()
    raise AssertionError(f"no {prop!r} in the {selector!r} rule")


def _submit_buttons() -> list[str]:
    return re.findall(r"<button[^>]*type=\"submit\"[^>]*>", TEMPLATE_SRC)


def test_every_submit_control_on_the_page_takes_the_red_dressing():
    """Swept, not listed: a control added to the queue later has to be dressed
    the same way or this goes red."""
    buttons = _submit_buttons()
    assert len(buttons) >= 9, f"expected the whole queue's controls, found {len(buttons)}"
    undressed = [b for b in buttons if "cinema-button--primary" not in b]
    assert not undressed, f"controls left in the outlined treatment: {undressed}"


def test_the_page_gives_its_controls_room_above():
    assert _decl(".moderation-queue .btn.cinema-button", "margin-top") == "1rem"


def test_the_cinema_family_still_zeroes_the_margin():
    """Why the page rule is needed at all: without it the controls sit flush
    under their fields again."""
    assert _decl(".btn.cinema-button", "margin-top") == "0"


def test_the_two_buttons_in_the_row_are_kept_apart():
    """They are inline siblings, so with no margin the only space between them
    is the word space in the markup -- measured at 3.4px, which reads as one
    control with two labels."""
    assert _decl(".dismiss-form button + button", "margin-left") == "0.75rem"


def test_the_disclosures_get_room_under_the_button_row():
    assert _decl(".dismiss-form details", "margin-top") == "1rem"


def test_the_here_only_caption_stayed_cut():
    """The owner cut this line on 2026-11-17; it is asserted absent so it cannot
    creep back with the card's other prose."""
    assert "removes it here only" not in TEMPLATE_SRC
