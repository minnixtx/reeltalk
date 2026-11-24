"""The tags that turn a pasted link into a card (share arc S3).

A link pasted into WhatsApp, Discord, Slack, X or Mastodon is fetched by a bot
that reads nothing but these tags: it runs no JavaScript, so whatever is not in
the HTML head might as well not be on the page. This module builds that head for
the two pages the arc covers - a post and a list - and nothing else. Film pages,
about and help wait their turn (D8).

Two rules hold the whole thing together.

**The address is the gate.** Every payload is built from ``share_url``, and
``share_url`` is empty for anything a stranger could not open, so an unreachable
post carries no tags rather than a card that promises a page which 404s. That is
R85 again - the page withholds exactly what the route refuses - and it is why
nothing here takes a predicate: there is one source for the address, and a page
either has it or it has nothing to render.

**Nothing here is markup.** A description is a bot's one line of prose, so it is
flattened to text before it is handed over: the stored body is already sanitized,
but a card attribute has no business carrying a tag or an attribute quote at all,
and stripping is not the same as trusting.
"""

from html import unescape

from django.utils.html import strip_tags

from reeltalk.activitypub.identity import absolute_uri

# Bots cut the description off around 200 to 300 characters of their own. Cutting
# it here as well keeps the ellipsis in the sentence rather than mid-word on
# whoever is rendering it.
DESCRIPTION_MAX = 300
TITLE_MAX = 120


def _flat(value: str, limit: int) -> str:
    """Sanitized HTML (or plain text) as one capped line of plain text."""
    # strip_tags leaves the escaped entities behind, so unescape after it rather
    # than before: the same order the federation path uses to count a note's
    # visible length. Collapsing whitespace is what turns a block of paragraphs
    # into the single line an attribute wants.
    text = unescape(strip_tags(value or ""))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def _payload(url: str, title: str, description: str, image: str = "") -> dict:
    """The one shape both pages hand the template, or ``{}`` for no address."""
    if not url:
        return {}
    # Local import: social imports core, so a module-level one would close the
    # circle. The instance's own name is admin-managed and is the card's footer.
    #
    # This costs one query per card, measured: the post page goes from 6 to 7. It
    # is the same one-row read the request already makes for the signup flag, and
    # folding the two together is the ruled-out option rather than the untried
    # one - ``test_signup_open_processor_exposes_only_the_boolean`` pins that
    # processor to a single bit precisely so a template cannot render what no view
    # approved. A card is a view decision, so it pays its own way.
    from reeltalk.social.models import SiteSettings

    return {
        "url": url,
        "title": _flat(title, TITLE_MAX),
        "description": _flat(description, DESCRIPTION_MAX),
        "image": image,
        "site_name": SiteSettings.get_instance().name or "ReelTalk",
    }


def _poster(film) -> str:
    """Absolute poster address, or "" when there is no poster at all.

    Absolute because a bot resolves it from wherever it stands, and a relative
    ``/media/...`` resolves against the bot's own host. Absent rather than
    placeholder because a card with a drawn box and no art inside is worse than a
    card with no picture, and the placeholder is a UI affordance, not content.
    """
    poster = getattr(film, "poster", None) if film else None
    return absolute_uri(poster.url) if poster else ""


def status_preview(status) -> dict:
    """The card for a post page, or ``{}`` when the post cannot be shared."""
    film = status.film
    author = status.user.localname
    body = _flat(status.content, DESCRIPTION_MAX)
    if film:
        # The page's own title voice ("minnix on Take Shelter"), because a card
        # that reads differently from the page it points at reads as an ad.
        title = f"{author} on {film.title}"
        label = film.title
        if film.year:
            title = f"{title} ({film.year})"
            label = f"{label} ({film.year})"
        description = f"{label} - {body}" if body else label
    else:
        title = f"{author}'s post"
        description = body or f"A post by {author} on ReelTalk"
    return _payload(status.share_url, title, description, _poster(film))


def list_preview(film_list, item_count: int) -> dict:
    """The card for a list page, or ``{}`` when the list cannot be shared."""
    films = "film" if item_count == 1 else "films"
    description = f"{film_list.title} - {item_count} {films}"
    if film_list.description:
        description = f"{description}. {_flat(film_list.description, DESCRIPTION_MAX)}"
    return _payload(film_list.status.share_url, film_list.title, description, image="")
