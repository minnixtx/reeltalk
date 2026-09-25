"""The mention-aware markdown renderer (§2C increment 2).

Turns a resolved ``@handle`` in status prose into a link to that member's
profile. Two properties this class exists to hold, neither of which the
parser's guarantees hand over for free:

* **No nested anchors.** mistune renders a link's label by recursing over
  its children *before* it ever calls ``link()``, so overriding ``text()``
  alone would drop a mention ``<a>`` inside the member's own ``<a>`` for
  ``[look at @alice](https://allowed.tld/x)``. The guard therefore sits on
  ``render_token``, where the token type is still visible, and covers
  ``image`` alt text for the same reason. The parser refuses to *report* a
  mention inside a link label; this class independently refuses to *emit*
  one, which is the property that actually reaches stored HTML.
* **An unresolved handle is not an error.** It renders as the text the
  member typed, inside the mention span with ``data-link="false"`` and no
  anchor -- plain to read, still marked as having parsed as a mention.

Code spans and fenced blocks need no guard here: mistune tokenises them as
``codespan`` / ``block_code``, whose renderer methods take the raw string
and never recurse back through ``text()``.
"""

from django.template.loader import render_to_string
from mistune import HTMLRenderer, Markdown
from mistune import escape as escape_text

from reeltalk.mentions.parser import MENTION_RE, resolve_typed_handle

# Token types whose rendered children become an anchor's label or an image's
# alt text rather than prose. A mention landing inside either is plain text.
_LABEL_TOKENS = frozenset({"link", "image"})


def mention_html(handle: str) -> str:
    """One handle rendered: a profile link when it resolves, plain when not.

    The href is built from the stored ``localname``, never from the casing
    that was typed -- ``@ALICE`` must point at ``/user/alice/``, the path
    the profile route actually serves, or the link 404s. The visible text
    stays what the member wrote, so nobody's prose gets rewritten.
    """
    user = resolve_typed_handle(handle)
    return render_to_string(
        "mentions/mention.html",
        {"user": user, "linked": user is not None, "text": f"@{handle}"},
    ).strip()


class MentionRenderer(HTMLRenderer):
    """HTMLRenderer that links resolved mentions in prose.

    A fresh instance per document. ``_label_depth`` is per-document state;
    sharing one renderer across requests would leak a count from one
    document into another and suppress mentions at random.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._label_depth = 0

    def render_token(self, token, state):
        if token.get("type") in _LABEL_TOKENS:
            self._label_depth += 1
            try:
                return super().render_token(token, state)
            finally:
                self._label_depth -= 1
        return super().render_token(token, state)

    def text(self, text):
        if self._label_depth:
            return super().text(text)
        return _substitute_mentions(text)


def _substitute_mentions(text: str) -> str:
    """Replace every mention in a text token, leaving the rest escaped."""
    parts = []
    last = 0
    for match in MENTION_RE.finditer(text):
        parts.append(escape_text(text[last : match.start()]))
        parts.append(mention_html(match.group(1)))
        last = match.end()
    parts.append(escape_text(text[last:]))
    return "".join(parts)


def render_mentions_html(markdown_text: str) -> str:
    """Render markdown to HTML with mentions linked. **Not sanitized.**

    The caller still has to put this through ``sanitize_html(...,
    mentions=True)`` -- this only decides what a mention looks like, and the
    allowlist that lets it survive is the other half of the pair.
    """
    render = Markdown(renderer=MentionRenderer())
    return render(markdown_text)
