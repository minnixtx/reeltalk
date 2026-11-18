"""The two red submit controls on Profile / Settings need room above them.

The owner's ask, read off a screenshot with arrows on both gaps (2026-11-17).
Measured on the rendered page before the change, "Save changes" sat 6.8px under
the last field of the form and "Invite" sat 0px under its pitch line: the shared
cinema-button rule zeroes ``margin-top``, which is correct for the film-action
rows it was written for and wrong for a submit that stands alone under the thing
it submits.

Source guards, like the other CSS pins in this suite -- the same shape as
``test_page_measure.py``. What they protect is the pairing: the scoped rule only
works while the family rule still zeroes the margin and the template still
carries the classes the scope is keyed to, so all three are asserted together and
none of them can be dropped alone.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CSS_PATH = REPO_ROOT / "reeltalk" / "social" / "static" / "css" / "reeltalk.css"
PROFILE_TEMPLATE = (
    REPO_ROOT / "reeltalk" / "social" / "templates" / "social" / "profile_edit.html"
)

# Comments stripped once, up front: they sit between a selector and its brace and
# carry quoted CSS of their own, which would break brace matching if left in.
CSS = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)
TEMPLATE = PROFILE_TEMPLATE.read_text()

SPACED = (
    ".profile-form .btn.cinema-button--primary",
    ".invite-form .btn.cinema-button--primary",
)


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


def test_both_red_submit_controls_get_a_top_margin():
    assert _decl(SPACED[0], "margin-top") == "1rem"
    assert _decl(SPACED[1], "margin-top") == "1rem"


def test_each_scoped_selector_is_written_once():
    """Each selector is keyed once in the whole sheet, outside every media block:
    a second copy under a breakpoint would move one control and not the other."""
    flat = re.sub(r"\s+", " ", CSS)
    for selector in SPACED:
        assert flat.count(selector) == 1, f"{selector} is keyed more than once"


def test_the_cinema_family_still_zeroes_the_margin():
    """Why the scoped rule has to exist at all. Drop this and the film-action
    rows -- buttons that sit in a row, not under a field -- pick up the gap."""
    assert _decl(".btn.cinema-button", "margin-top") == "0"


def test_the_template_still_carries_the_classes_the_rule_is_keyed_to():
    """The scope is three classes across two forms and two buttons. Rename any of
    them in the template and the rule stops matching in silence."""
    assert 'class="profile-form"' in TEMPLATE
    assert 'class="invite-form"' in TEMPLATE
    assert TEMPLATE.count('class="btn cinema-button cinema-button--primary"') == 2
