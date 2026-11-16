"""The post page carries its own poster (owner pass, 2026-11-16).

A post on its own URL used to draw the feed row's thumbnail: 40x60 pixels of
art above the review, on a page with a whole viewport to itself. The owner's
reference for this pass is a mockup in which the poster is roughly a third of
the page and the review sits beside it, and the ask was exactly that — the art
grows because the post is no longer one row in a list.

Two things had to give way for that. The page is a 40rem reading column, and a
poster big enough to matter inside it pushes the prose to 444px and breaks the
film title onto two lines, so the post page opts into the 62rem band the feed
already uses. And the film title becomes the page's heading: it was a paragraph
beside the thumbnail, which is where a row wants it and not where a page does.

The layout is one mechanism, not two. The poster leaves the flow and the whole
text column is padded past it, so the title, byline, prose, report form and
action row stay the article's own children in the order they read — there is no
wrapper whose contents could quietly stop matching that order. Below the phone
cut the same poster floats beside the byline instead, because a two-column split
at 390px leaves the prose about 250px, which is too narrow to read a review in.

Both halves are pinned, as every other layout pass here: the rendered page, and
the stylesheet that makes the markup mean what it looks like it means. The
drift guard that matters most is that the poster's width and the text column's
indent are the same custom property in two places — if one moves alone, the
poster lands on top of the prose it is supposed to sit beside.
"""

import re
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse

from reeltalk.core.models import Film, Status, mark_watched
from reeltalk.tests.members import member

PASSWORD = "s3cretpass"
CSS_PATH = (
    Path(__file__).resolve().parents[1] / "social" / "static" / "css" / "reeltalk.css"
)
DESKTOP = "(min-width: 600px)"
PHONE = "(max-width: 599px)"


@pytest.fixture
def alice(db):
    return member(localname="alice", password=PASSWORD)


@pytest.fixture
def dune(db):
    film = Film.objects.create(title="Dune", year=2021)
    # A path, not bytes: the page renders the URL the field holds, and the
    # branch that matters is whether the field is filled at all.
    film.poster = "posters/dune.jpg"
    film.save(update_fields=["poster"])
    return film


@pytest.fixture
def review(alice, dune):
    return mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")


def _page(who=None, status=None) -> str:
    client = Client()
    if who:
        assert client.login(username=who, password=PASSWORD)
    resp = client.get(reverse("status", args=[status.pk]))
    assert resp.status_code == 200
    return resp.content.decode()


def _article(body) -> str:
    start = body.index('<article class="status-post')
    return body[start : body.index("</article>", start)]


def _block(body, open_tag) -> str:
    """One element's own markup, given it holds no nested ``div``."""
    assert open_tag in body, f"the page has no {open_tag} at all"
    start = body.index(open_tag) + len(open_tag)
    return body[start : body.index("</div>", start)]


def _css_rules() -> list[tuple[list[str], str]]:
    """(selectors, declarations) for every rule inside one ``@media`` block.

    Comments go first because they sit between a selector and its brace, and
    whitespace is collapsed so assertions key on declarations, not formatting.
    """
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)
    css = re.sub(r"\s+", " ", css)
    out = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = [s.strip() for s in match.group(1).split(",") if s.strip()]
        out.append((selectors, match.group(2).strip()))
    return out


def _media_blocks(condition: str) -> list[tuple[list[str], str]]:
    """Every rule that lives inside an ``@media`` naming ``condition`` exactly."""
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)
    css = re.sub(r"\s+", " ", css)
    out = []
    for block in re.finditer(r"@media([^{]+)\{((?:[^{}]|\{[^{}]*\})*)\}", css):
        if block.group(1).strip() != condition:
            continue
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", block.group(2)):
            selectors = [s.strip() for s in rule.group(1).split(",") if s.strip()]
            out.append((selectors, rule.group(2).strip()))
    assert out, f"no @media {condition} block in the shipped stylesheet"
    return out


def _decl(rule_selector: str, rules) -> str:
    """Declarations of the one rule whose selector list is exactly ``rule_selector``."""
    for selectors, declarations in rules:
        if selectors == [rule_selector]:
            return declarations
    raise AssertionError(f"no rule for {rule_selector}")


def _decl_one_of(rule_selectors: list[str], rules) -> str:
    for selectors, declarations in rules:
        if selectors == rule_selectors:
            return declarations
    raise AssertionError(f"no rule for {rule_selectors}")


# --- The rendered page -------------------------------------------------------


@pytest.mark.django_db
def test_the_page_widens_itself_for_the_poster(review):
    assert '<main class="container status-page">' in _page("alice", review)


@pytest.mark.django_db
def test_the_film_title_leads_the_page_and_links_to_the_film(alice, dune, review):
    article = _article(_page("alice", review))
    assert '<h1 class="post-film">' in article
    head = article.index('class="review-head"')
    assert article.index('<h1 class="post-film">') < head
    assert f'href="{reverse("film", args=[dune.pk])}"' in article
    assert "Dune" in article
    assert '<span class="muted">(2021)</span>' in article


@pytest.mark.django_db
def test_the_poster_is_the_pages_own_not_the_row_thumbnail(review):
    poster = _block(_article(_page("alice", review)), '<div class="post-poster">')
    assert 'class="poster" src="' in poster
    assert 'class="thumb"' not in poster


@pytest.mark.django_db
def test_a_film_with_no_art_still_holds_the_slot(alice):
    """The empty plate is the same slot at the same size, not a missing column.

    The indent is padding on the card, so a slot that vanished would leave a
    gutter the width of art that was never drawn.
    """
    bare = Film.objects.create(title="Nosferatu", year=1922)
    note = mark_watched(alice, bare, rating="3", content="<p>Silent.</p>")
    poster = _block(_article(_page("alice", note)), '<div class="post-poster">')
    assert 'class="poster poster-placeholder"' in poster
    assert 'class="thumb' not in poster


@pytest.mark.django_db
def test_the_byline_keeps_its_facts_and_loses_its_controls(alice, dune, review):
    head = _block(_article(_page("alice", review)), '<div class="review-head">')
    for fact in ('class="review-author"', 'class="stars"', 'class="post-date"'):
        assert fact in head, f"the byline lost {fact}"
    for control in ("applaud-btn", "reply-btn"):
        assert control not in head, f"the byline still carries the {control}"


@pytest.mark.django_db
def test_the_controls_close_the_page_after_the_prose(alice, dune, review):
    article = _article(_page("alice", review))
    actions = _block(article, '<div class="row-actions">')
    assert "applaud-btn" in actions and "reply-btn" in actions
    assert article.index('class="review-body"') < article.index(
        '<div class="row-actions">'
    )


@pytest.mark.django_db
def test_the_reply_control_still_opens_the_composer(review):
    actions = _block(_article(_page("alice", review)), '<div class="row-actions">')
    assert 'href="?reply=1"' in actions


@pytest.mark.django_db
def test_a_stranger_gets_the_count_and_no_controls(review):
    article = _article(_page(None, review))
    assert '<div class="row-actions">' not in article
    assert 'class="review-head"' in article


@pytest.mark.django_db
def test_a_post_with_no_film_holds_no_poster_column(db):
    """A mirrored note with no film behind it has no art to make room for.

    The column is padding, so leaving it on would be an empty gutter the width
    of a poster the page never drew.
    """
    drifter = member(localname="drifter", password=PASSWORD)
    note = Status.objects.create(user=drifter, content="<p>No film attached.</p>")
    article = _article(Client().get(reverse("status", args=[note.pk])).content.decode())
    assert "has-poster" not in article
    assert '<div class="post-poster">' not in article
    assert '<h1 class="post-film">' not in article


@pytest.mark.django_db
def test_the_back_link_goes_to_the_feed(review):
    body = _page("alice", review)
    assert f'<a class="post-back" href="{reverse("index")}">' in body


# --- The stylesheet ----------------------------------------------------------


def test_the_poster_width_and_the_text_indent_are_one_value():
    """The poster column and the prose indent cannot drift apart.

    The poster is out of the flow, so the only thing keeping it off the prose is
    that the indent is the same width plus the same gap. Written as two literals
    the page still renders — until someone changes one of them.
    """
    desktop = _media_blocks(DESKTOP)
    card = _decl(".status-post.has-poster", desktop)
    poster = _decl(".post-poster", desktop)
    thread = _decl_one_of([".status-replies", ".reply-form"], desktop)
    indent = re.search(r"padding-left:\s*calc\((.+?)\)\s*;", card)
    assert indent, "the card no longer indents its text column"
    assert indent.group(1).strip() == "var(--poster-w) + var(--poster-gap)"
    assert re.search(r"width:\s*var\(--poster-w\)", poster)
    assert "calc(var(--poster-w) + var(--poster-gap))" in thread


def test_the_page_column_is_the_bands_62rem_not_the_reading_40rem():
    desktop = _media_blocks(DESKTOP)
    assert "max-width: 62rem" in _decl("main.status-page", desktop)


def test_the_card_holds_room_for_an_art_taller_than_the_review():
    """A 2:3 poster has a known height, so the card can reserve it.

    Without this a one-line review would be shorter than the absolutely
    positioned art beside it, and the thread below would start under the poster.
    """
    card = _decl(".status-post.has-poster", _media_blocks(DESKTOP))
    assert "min-height: calc(var(--poster-w) * 1.5)" in card


def test_the_phone_floating_is_scoped_to_the_phone():
    """The float belongs below the cut and the absolute column above it.

    Both mechanisms pin the poster to the same slot in the markup, so a sheet
    that shipped both at once would take the poster out of the flow on a phone
    and leave the title sitting where the art should be.
    """
    phone = _decl(".post-poster", _media_blocks(PHONE))
    assert "float: left" in phone
    assert "position: absolute" not in phone
    assert "position: absolute" in _decl(".post-poster", _media_blocks(DESKTOP))


def test_the_title_takes_its_face_from_the_rule_the_film_page_uses():
    """Both titles are h1s wearing one shared rule, not two copies of it.

    The owner ruled the post page's title onto the film page's face and weight.
    Restating them on ``.post-film`` would match today and drift the moment
    either is edited, so what gets pinned is that neither page's title rule
    names a face: the only thing that can change either title is the shared
    display rule, which changes both.
    """
    display = _decl_one_of(["h1", "h2"], _css_rules())
    assert "font-family: var(--display)" in display
    for selector in (".post-film", ".film-meta h1"):
        declarations = _decl(selector, _css_rules())
        for prop in ("font-family", "font-weight", "letter-spacing", "text-transform"):
            assert f"{prop}:" not in declarations, (
                f"{selector} restates {prop}, so it can now drift from the other"
            )
