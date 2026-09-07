"""Shared helpers for the core (film) app."""

import bleach
import mistune

# Conservative allowlist for user-authored markdown (descriptions, reviews).
# Markdown is rendered to HTML at write time and stored; this trims anything
# beyond simple prose formatting (no scripts, styles, or iframes).
_ALLOWED_TAGS = [
    "p",
    "br",
    "em",
    "strong",
    "b",
    "i",
    "u",
    "s",
    "a",
    "ul",
    "ol",
    "li",
    "blockquote",
    "code",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
]
_ALLOWED_ATTRS = {"a": ["href"]}
_ALLOWED_PROTOCOLS = ["http", "https"]


def render_markdown(text: str) -> str:
    """Render markdown to sanitized HTML, stored at write time (§3.2).

    Empty input yields an empty string so optional fields stay blank rather
    than ``<p></p>``.
    """
    if not text:
        return ""
    # mistune appends a trailing newline; strip so stored HTML stays tidy.
    html = mistune.html(text).strip()
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
    )
