"""The card a pasted link turns into (share arc S3).

A bot that fetches a pasted link runs no JavaScript and reads nothing but the
head, so these tags are the whole of what WhatsApp, Discord, Slack, X and
Mastodon can say about a ReelTalk post. That makes the failures quiet: a card
that promises a page which 404s, a description that arrives as escaped markup, a
picture that resolves against the wrong host. None of them are visible on the
page itself, which is why every assertion here is about the rendered head rather
than about the model.

**What is asserted here.**

* **The card appears exactly where a stranger can open the link.** The gate is
  ``share_url`` and nothing else, so the author's own private post - which
  renders, for its author - carries no tags at all, and a public mirror whose
  stored home address is not a web address carries none either. A card for a page
  nobody can open is the one failure this feature has.
* **One address, twice printed.** ``og:url`` is the same string the share button
  copies, because two sources for the address is how a card and a button end up
  disagreeing.
* **The description is text.** The stored body is sanitized already, but a card
  attribute has no business carrying a tag or a quote, so the tags must arrive
  flattened: no angle bracket, no entity, capped.
* **The picture is absolute or absent.** A relative ``/media/...`` resolves
  against whoever is fetching it, and a placeholder box is a UI affordance rather
  than content, so a film with no poster gets no ``og:image`` and the Twitter
  card drops to ``summary``.
* **The instance's own name is the footer.** ``og:site_name`` follows
  ``SiteSettings.name``, which is admin-managed and already the name the app
  calls itself elsewhere.

The address itself - the three shapes and the scheme gate - is ``test_share.py``'s
and is not re-tested here; what is tested is what the page does with it.
"""

import re
from html import unescape
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.test.utils import override_settings
from django.urls import reverse

from reeltalk.core.models import Film, Status, Visibility, mark_watched
from reeltalk.core.preview import DESCRIPTION_MAX
from reeltalk.lists.services import create_list
from reeltalk.social.models import SiteSettings
from reeltalk.tests.members import member

PASSWORD = "s3cretpass"
CANON = "https://reeltalk.example"
User = get_user_model()

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_TEMPLATE = REPO_ROOT / "templates" / "_link_preview.html"


@pytest.fixture
def alice(db):
    return member(localname="alice", password=PASSWORD)


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def review(alice, dune):
    return mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")


def _page(who=None, url=None) -> str:
    client = Client()
    if who:
        assert client.login(username=who, password=PASSWORD)
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.content.decode()


def _post_page(who=None, status=None) -> str:
    return _page(who, reverse("status", args=[status.pk]))


def _raw_meta(body, key) -> str | None:
    """The attribute exactly as rendered, entities and all."""
    match = re.search(
        rf'<meta (?:property|name)="{re.escape(key)}" content="([^"]*)"', body
    )
    return match.group(1) if match else None


def _meta(body, key) -> str | None:
    """What a bot reads after it parses the attribute, or None if it is absent.

    Django escapes an apostrophe in an attribute to ``&#x27;``, which is correct
    and which no reader ever sees, so prose assertions compare the parsed text.
    The markup check below deliberately uses the raw form: that one is a claim
    about what is in the document.
    """
    value = _raw_meta(body, key)
    return unescape(value) if value is not None else None


def _card(body) -> dict:
    """Every preview tag the page carries, keyed by its property name."""
    keys = [
        "og:url",
        "og:site_name",
        "og:title",
        "og:description",
        "og:image",
        "twitter:card",
    ]
    return {key: _meta(body, key) for key in keys if _meta(body, key) is not None}


def _remote_author(localname="carol@peer.example") -> User:
    user = User(
        localname=localname,
        local=False,
        actor_url="https://peer.example/users/carol",
        inbox_url="https://peer.example/users/carol/inbox",
    )
    user.set_unusable_password()
    user.save()
    return user


# --- the public post page ----------------------------------------------------


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_public_review_carries_the_card(alice, dune, review):
    body = _post_page(None, review)
    card = _card(body)
    assert card["og:url"] == f"{CANON}/status/{review.pk}/"
    assert card["og:title"] == "alice on Dune (2021)"
    # The film leads the description (the plan's rule): a reader of the card is
    # being told which film this is about before they are told what someone said.
    assert card["og:description"].startswith("Dune (2021) - ")
    assert "Desert planet." in card["og:description"]
    assert card["og:site_name"] == "ReelTalk"
    assert card["twitter:card"] == "summary"


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_card_points_at_the_same_address_the_button_copies(alice, review):
    """One source for the address. If these two ever differ, the card and the
    button are describing different posts."""
    body = _post_page(None, review)
    button = re.search(r'data-share-url="([^"]*)"', body).group(1)
    assert _meta(body, "og:url") == button


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_card_sits_in_the_head(alice, review):
    """A bot that stops at ``</head>`` must still find it."""
    body = _post_page(None, review)
    assert body.index('property="og:url"') < body.index("</head>")


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_posters_own_address_is_absolute(alice, dune, review):
    dune.poster = "posters/dune.jpg"
    dune.save()
    body = _post_page(None, review)
    # Compared against the field rather than a literal path: what is being pinned
    # is that the card made it absolute, not where MEDIA_URL happens to point.
    assert _meta(body, "og:image") == f"{CANON}{dune.poster.url}"
    assert _meta(body, "og:image").startswith("https://")
    assert _meta(body, "twitter:card") == "summary_large_image"


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_film_without_a_poster_carries_no_image(alice, dune, review):
    """Absent, not a placeholder path: a relative URL would resolve against the
    fetching bot's own host, and an empty box is not content."""
    body = _post_page(None, review)
    assert "og:image" not in _card(body)
    assert _meta(body, "twitter:card") == "summary"


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_description_arrives_as_text_not_markup(alice, dune):
    review = mark_watched(
        alice,
        dune,
        rating="4.0",
        content='<p>It has <a href="https://x.example">links</a> and <b>bold</b>.</p>',
    )
    raw = _raw_meta(_post_page(None, review), "og:description")
    assert "<" not in raw and ">" not in raw
    assert "links and bold" in _meta(_post_page(None, review), "og:description")


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_description_is_capped_on_one_line(alice, dune):
    review = mark_watched(
        alice,
        dune,
        rating="4.0",
        content="<p>" + ("word " * 400) + "</p>\n<p>second</p>",
    )
    description = _meta(_post_page(None, review), "og:description")
    assert len(description) <= DESCRIPTION_MAX
    assert description.endswith("\u2026")
    assert "\n" not in description


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_post_with_no_film_still_carries_a_card(alice):
    # A film-less post is untyped: every named type has to anchor to a film (L13
    # leaves exactly one exemption, and it is a list's face).
    note = Status.objects.create(user=alice, content="<p>A plain note.</p>")
    body = _post_page(None, note)
    card = _card(body)
    assert card["og:title"] == "alice's post"
    assert card["og:description"] == "A plain note."
    assert "og:image" not in card


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_site_name_is_the_instance_s_own(alice, review):
    """The card's footer is the admin's name for this instance, not the word the
    wordmark happens to draw."""
    site = SiteSettings.get_instance()
    site.name = "Watchtower"
    site.save()
    assert _meta(_post_page(None, review), "og:site_name") == "Watchtower"


# --- where a stranger cannot open the link -----------------------------------


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_authors_own_private_post_carries_no_card(alice, dune):
    """The page renders, for the one reader allowed to read it, and says nothing
    to a bot. "The page rendered" is not the predicate."""
    private = mark_watched(
        alice,
        dune,
        rating="3.0",
        content="<p>Just for me.</p>",
        visibility=Visibility.PRIVATE,
    )
    body = _post_page(alice, private)
    assert "og:" not in body.split("</head>")[0]
    assert "twitter:card" not in body


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_mirror_without_a_web_address_carries_no_card():
    peer = _remote_author()
    mirror = Status.objects.create(
        user=peer,
        local=False,
        remote_url="javascript:alert(1)",
        content="<p>A mirrored note.</p>",
    )
    body = _post_page(None, mirror)
    assert "og:" not in body.split("</head>")[0]


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_mirror_card_points_at_its_home_instance():
    """D3 again, from the card's side: the peer's address is the post's identity,
    so the card has to name it and not our copy of it."""
    peer = _remote_author("dave@peer.example")
    mirror = Status.objects.create(
        user=peer,
        local=False,
        remote_url="https://peer.example/posts/7",
        content="<p>From over there.</p>",
    )
    body = _post_page(None, mirror)
    assert _meta(body, "og:url") == "https://peer.example/posts/7"


# --- the list page -----------------------------------------------------------


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_list_card_names_the_list_and_counts_its_films(alice, dune):
    film_list = create_list(
        alice,
        title="Noir",
        films=[dune],
        visibility=Visibility.PUBLIC,
        description="Films with rain in them.",
    )
    body = _page(None, reverse("list-detail", args=[film_list.pk]))
    card = _card(body)
    assert card["og:url"] == f"{CANON}/list/{film_list.pk}/"
    assert card["og:title"] == "Noir"
    assert card["og:description"].startswith("Noir - 1 film.")
    assert "Films with rain in them." in card["og:description"]


@pytest.mark.django_db
@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_makers_own_private_list_carries_no_card(alice, dune):
    film_list = create_list(
        alice, title="Private Shelf", films=[dune], visibility=Visibility.PRIVATE
    )
    body = _page(alice, reverse("list-detail", args=[film_list.pk]))
    assert "og:" not in body.split("</head>")[0]


# --- the shape of the partial itself -----------------------------------------


def test_no_other_page_ships_the_card():
    """D8: the arc covers the post page and the list page, and nothing else. A
    site-wide default on ``base.html`` would make every page promise a preview of
    something it has never decided how to describe."""
    assert (
        "{% block head_meta %}{% endblock %}"
        in (REPO_ROOT / "templates" / "base.html").read_text()
    )
    assert PREVIEW_TEMPLATE.exists()
    pages = [
        REPO_ROOT / "reeltalk" / "core" / "templates" / "core" / "film" / "detail.html",
        REPO_ROOT / "reeltalk" / "social" / "templates" / "social" / "profile.html",
    ]
    for path in pages:
        assert "_link_preview.html" not in path.read_text(), path
