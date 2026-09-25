"""The mention parser: two input shapes, one ordered list of resolved users.

Both entry points answer the same question — which members does this content
address? — and differ only in where the content came from.
``mentions_from_text`` reads markdown a member typed; ``mentions_from_tags``
reads the ``tag`` array a peer put on the wire. One output shape means every
caller treats the two paths alike.

Resolution never creates, never fetches, never webfingers (M-b). It answers
from rows already in this database: a handle with no ``@`` is a local account,
a handle with one is a mirror we already hold. An unresolvable handle stays
plain text — the same end state Mastodon reaches by resolving and failing,
arrived at here by not looking, which keeps an unbounded external round-trip
out of the middle of someone's post.
"""

import re

from django.contrib.auth import get_user_model

from reeltalk.activitypub.identity import reference_url
from reeltalk.activitypub.mirrors import resolve_known_actor

User = get_user_model()

# R12's localname charset is [a-zA-Z0-9._-] starting with a letter or digit.
# Signup permits a *trailing* '.', '_' or '-' too (social.forms.LOCALNAME_RE),
# so copying that charset straight into the parser would swallow a sentence's
# final period and look up 'alice.' for '@alice.'. The handle is therefore
# required to end on a letter or digit. The cost is taken deliberately: an
# account whose name ends in punctuation ('alice_') cannot be @-mentioned.
# A period ending a sentence is everywhere; a localname ending in one is not.
_HANDLE = r"[a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?"

# The domain half of a remote handle. A mirror's localname is
# <preferredUsername>@<netloc>, and the netloc carries the port when it is
# non-default (M4 increment 4), so ':' is admissible between host and port.
# The port is digits, so the no-trailing-punctuation rule still closes the
# whole handle rather than stopping short of a port.
_DOMAIN = r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?(?::[0-9]{1,5})?"

# The '@' may not be preceded by '=', a word character, or '/'.
#
# That lookbehind is the whole reason an email address inside a review is not
# a mention: in 'minnix@example.com' the '@' is preceded by a word character,
# so nothing downstream has to recognise the shape of an email address at all.
# '/' is in the set for the same reason applied to URLs — 'https://host/@alice'
# mentions nobody. Mastodon's MENTION_RE excludes the same two characters
# (app/models/account.rb), which is what makes this shape a parity requirement
# rather than a local invention.
MENTION_RE = re.compile(rf"(?<![=/\w])@({_HANDLE}(?:@{_DOMAIN})?)")

# Regions of markdown where a typed '@' is code or link syntax rather than an
# address. Masked before scanning because the regex alone cannot tell a handle
# in prose from a handle inside a code span.
#
# Order matters: fenced blocks are blanked first so a backtick inside one is
# already gone before the inline-code pass looks for backticks.
_FENCED_CODE_RE = re.compile(
    r"^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1",
    re.DOTALL | re.MULTILINE,
)
_CODE_SPAN_RE = re.compile(r"`+.+?`+", re.DOTALL)
# A link or image with its destination, so neither the label nor the URL is
# scanned. The label is the half that matters: mistune renders a link's label
# inline, so a mention inside one would otherwise come back in increment 2 as
# an <a> nested inside an <a>.
_LINK_RE = re.compile(r"!?\[[^\]]*\]\([^)]*\)")


def _blanked(match: "re.Match[str]") -> str:
    """Replace a matched span with same-length filler.

    Length-preserving so the text around the hole keeps its shape — a filler
    that shrank the string could pull two unrelated characters together
    across it. The filler character does not occur in real markdown.
    """
    return "\x00" * (match.end() - match.start())


def _hide_non_mention_regions(text: str) -> str:
    hidden = _FENCED_CODE_RE.sub(_blanked, text)
    hidden = _CODE_SPAN_RE.sub(_blanked, hidden)
    return _LINK_RE.sub(_blanked, hidden)


def _unique_in_order(users):
    """De-duplicate by pk, keeping first occurrence.

    The same handle typed twice in one post is one mention. The database
    enforces the same thing on the row; doing it here keeps a caller from
    seeing a user twice before anything is written.
    """
    seen = set()
    ordered = []
    for user in users:
        if user.pk in seen:
            continue
        seen.add(user.pk)
        ordered.append(user)
    return ordered


def resolve_typed_handle(handle: str):
    """The user a handle typed into a post names, or ``None``.

    Public because the renderer resolves a mention with this same function:
    the set of handles that notify and the set that render as links must be
    one answer, not two that can drift apart.

    Mirrors ``social.views._resolve_profile_user``, which answers the same
    question for the profile route, so a typed handle resolves to the same
    account that handle's profile link would lead to. The two halves are not
    symmetric, and the asymmetry is the point:

    * **No ``@``** — a local account, matched case-insensitively. R40 treats
      case variants as one identity and signup rejects insensitive
      duplicates, so ``@ALICE`` and ``@alice`` are the same person.
    * **An ``@``** — a remote mirror we already hold, matched **verbatim**.
      ``_resolve_profile_user`` applies no ``iexact`` here, so
      ``@Minnix@upallnight.minnix.dev`` misses the mirror stored as
      ``minnix@upallnight.minnix.dev``. Reproducing that exactly is the
      requirement, not an oversight to smooth over: a parser more forgiving
      than the route would resolve a handle whose profile link then 404s.
    """
    if "@" not in handle:
        return User.objects.filter(local=True, localname__iexact=handle).first()
    return User.objects.filter(local=False, localname=handle).first()


def mentions_from_text(raw_markdown: str):
    """The users a piece of typed markdown mentions, in the order found.

    Takes raw markdown rather than rendered HTML because the mention has to be
    found before rendering decides what it looks like — and because once
    content has been through the sanitizer, the difference between a handle in
    prose and a handle in a code span has been erased.

    An empty list means the content addresses nobody we know. Nothing here
    rewrites the text in that case: an unresolvable handle is not an error,
    and a member's post must read the same whether or not the handle behind it
    resolves.
    """
    if not raw_markdown:
        return []
    hidden = _hide_non_mention_regions(raw_markdown)
    found = []
    for match in MENTION_RE.finditer(hidden):
        user = resolve_typed_handle(match.group(1))
        if user is not None:
            found.append(user)
    return _unique_in_order(found)


def mentions_from_tags(tag_value, request):
    """The users a wire ``tag`` array mentions, in the order found.

    ``request`` is not decoration. It is the only thing that says which actor
    URLs are *ours*: a local user's ``actor_url`` is empty — mirrors fill it
    and nothing else — so a local member can only be recognised from an actor
    URL by comparing that URL's netloc against this instance's own host.
    Resolve without the request and a local member becomes unresolvable from
    the wire, which — given ``notify()``'s standing "no remote recipient"
    guard — would leave the entire remote-to-local mention direction unable to
    notify anybody.

    Resolution goes through ``resolve_known_actor``, the same resolver that
    answers "which user does this actor URL name" for inbound Follow. One
    resolver, one answer. It never fetches (M-b), so an unknown href is
    dropped rather than chased and nothing a stranger posted can put an
    external round-trip into our request path.
    """
    found = []
    for entry in _as_list(tag_value):
        if not isinstance(entry, dict):
            continue
        if not _is_mention(entry.get("type")):
            continue
        href = reference_url(entry.get("href"))
        if not href:
            continue
        user = resolve_known_actor(href, request)
        if user is not None:
            found.append(user)
    return _unique_in_order(found)


def _as_list(value) -> list:
    """Normalise a wire property that may be single, a list, or absent."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _is_mention(type_value) -> bool:
    """Whether a tag's ``type`` names a Mention.

    Tolerant of a list because ActivityPub's ``type`` property is
    multi-valued by spec — Mastodon reads the same shape with
    ``equals_or_includes?`` (app/lib/activitypub/activity/create.rb), so a
    peer sending ``["Mention"]`` is well-formed and has to work here too.
    """
    names = type_value if isinstance(type_value, list) else [type_value]
    return any(name.lower() == "mention" for name in names if isinstance(name, str))
