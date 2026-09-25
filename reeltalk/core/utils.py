"""Shared helpers for the core (film) app."""

import re
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

# The mention path adds <span> for the mention wrapper and nothing else, so
# no other content gains a tag.
_MENTION_TAGS = [*_ALLOWED_TAGS, "span"]

# The only site-relative href the mention path admits. A mention's own link
# carries no netloc and ``LinkDomain.is_allowed("")`` is False, so without
# this arm the mention's anchor is stripped and the mention renders as inert
# text. Shape-checked rather than resolved-checked: the renderer only emits
# this for a user it actually resolved, and a hand-typed one still lands on
# this instance's own profile route instead of pointing off-site, so
# ``LinkDomain`` stays the gate for everything that really leaves here.
#
# The character class is the profile route's own namespace, not R12's local
# charset: a mirror's localname is ``<preferredUsername>@<netloc>`` and the
# netloc carries a ``:port`` when non-default, so both belong here or a
# mirror's profile link gets stripped. ``/`` is deliberately absent, which
# is what keeps ``/user/../admin/`` out of the admitted set.
_SITE_USER_HREF_RE = re.compile(r"^/user/[A-Za-z0-9][A-Za-z0-9._:@-]*/$")

# The only values the mention span's two attributes may carry. Exact values,
# so admitting them on the span cannot widen into a general class/data-*
# allowance.
_MENTION_CLASS = "mention"
_MENTION_DATA_LINKS = frozenset({"true", "false"})


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


def _allowed_mention_href(tag: str, attr: str, value: str):
    """``<a>`` filter for the mention path: the allowlist plus our own profiles.

    Same gate as ``_allowed_href`` with one extra arm in front of it for the
    ``/user/<localname>/`` shape the mention renderer emits. Everything
    else, including a relative href that is not a profile path, falls
    through to the ordinary deny-by-default rule.
    """
    if attr != "href":
        return None
    if _SITE_USER_HREF_RE.match(value):
        return value
    return _allowed_href(tag, attr, value)


def _allowed_mention_attrs(tag: str, attr: str, value: str):
    """The mention span's ``class`` and ``data-link``, by exact value.

    bleach dispatches attribute filters per tag, so this one only ever sees
    the ``span``. A member's own ``<a>`` keeps ``_allowed_mention_href``,
    which returns ``None`` for both of these attributes -- that pairing is
    the whole point: the mention span keeps ``class`` and ``data-link``
    while an ordinary anchor in the same document loses both.
    """
    if attr == "class" and value == _MENTION_CLASS:
        return value
    if attr == "data-link" and value in _MENTION_DATA_LINKS:
        return value
    return None


def sanitize_html(html: str, *, mentions: bool = False) -> str:
    """Sanitize already-rendered HTML with the user-content allowlist.

    For content that arrives as HTML rather than markdown — a remote Person
    document's ``summary`` (M5 profile refresh). Same tags/attributes/
    protocols as ``render_markdown``, so stored user content always passes
    through one safety gate.

    ``mentions=True`` widens the gate by exactly three things: the ``span``
    tag, the mention span's two attributes, and the site-relative profile
    href. It is opt-in per caller and **off by default**, because the other
    consumer of this function is remote HTML from another instance's Person
    document. Widening *that* path would let a remote bio's
    ``href="/user/x"`` start pointing at our host instead of theirs.
    """
    if not html:
        return ""
    if mentions:
        tags = _MENTION_TAGS
        attributes = {"a": _allowed_mention_href, "span": _allowed_mention_attrs}
    else:
        tags = _ALLOWED_TAGS
        attributes = {"a": _allowed_href}
    return bleach.clean(
        html,
        tags=tags,
        # Per-tag callable: bleach's dict form supports a filter per tag, not
        # per attribute — this one gates the only attribute <a> carries.
        attributes=attributes,
        protocols=_ALLOWED_PROTOCOLS,
    )


def render_markdown(text: str, *, mentions: bool = False) -> str:
    """Render markdown to sanitized HTML, stored at write time (§3.2).

    Empty input yields an empty string so optional fields stay blank rather
    than ``<p></p>``. Outbound links are kept only for the instance's
    allowed link domains (see ``_allowed_href``).

    ``mentions`` decides whether a ``@handle`` in the text becomes a link
    to that member's profile. **Off by default, and that default is load
    bearing.** M-a scopes mentions to status content only, and this
    function has five production callers spanning statuses, film
    descriptions, TMDB overviews and member bios. A flag that defaulted
    *on* would hand mention behavior to the three surfaces M-a declined
    outright -- including third-party TMDB text, which nobody in this
    instance chose to have mention anybody. Only the two status write sites
    pass ``mentions=True``.
    """
    if not text:
        return ""
    if mentions:
        # Imported inside the function, not at module scope. The renderer
        # needs the parser, the parser needs activitypub.mirrors, and
        # mirrors imports this module -- a top-level import closes that
        # cycle and breaks whichever of the two loads second.
        from reeltalk.mentions.renderer import render_mentions_html

        html = render_mentions_html(text)
    else:
        html = mistune.html(text)
    # mistune appends a trailing newline; strip so stored HTML stays tidy.
    return sanitize_html(html.strip(), mentions=mentions)
