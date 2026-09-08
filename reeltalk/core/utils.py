"""Shared helpers for the core (film) app."""

from urllib.parse import urlparse

import bleach
import mistune

from reeltalk.social.models import LinkDomain

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
_ALLOWED_PROTOCOLS = ["http", "https"]


def _allowed_href(tag: str, attr: str, value: str):
    """Bleach attribute filter for ``<a>`` (site settings, §3.2).

    The instance's link-domain allowlist is the safety gate for outbound
    links in user content: an href survives only when its host matches an
    allowed domain. With no domains allowed every external link is stripped
    (the anchor text stays); relative and non-URL hrefs are denied too.
    """
    if attr != "href" or not LinkDomain.is_allowed(urlparse(value).netloc):
        return None
    return value


def render_markdown(text: str) -> str:
    """Render markdown to sanitized HTML, stored at write time (§3.2).

    Empty input yields an empty string so optional fields stay blank rather
    than ``<p></p>``. Outbound links are kept only for the instance's
    allowed link domains (see ``_allowed_href``).
    """
    if not text:
        return ""
    # mistune appends a trailing newline; strip so stored HTML stays tidy.
    html = mistune.html(text).strip()
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        # Per-tag callable: bleach's dict form supports a filter per tag, not
        # per attribute — this one gates the only attribute <a> carries.
        attributes={"a": _allowed_href},
        protocols=_ALLOWED_PROTOCOLS,
    )
