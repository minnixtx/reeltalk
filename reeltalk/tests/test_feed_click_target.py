"""The feed row as a click target (parked ask, restated by the owner 2026-09-28).

Two halves, settled with the owner before any code was written:

* **The whole row opens its post**, via a real anchor stretched over it
  (``.review-open``) rather than a JS handler on the ``<li>``. A list item is
  not focusable, so a scripted row would be unreachable by Tab and would lose
  middle-click-open-in-a-new-tab. The anchor carries no content, so nothing
  in the row is nested inside it — which is also what keeps the markup legal,
  since review prose is rendered markdown and can hold links of its own.
* **The author name is its own link**, to the profile. It already was one on
  the post page, the reply partial and notifications; the bare ``<span>`` on
  the feed, the genre subfeed and the film page is why no profile was
  reachable from the feed at all.

The overlay is gated on ``status_id`` (R84): a row with no post behind it has
nothing to open, and that includes the R37 bulk aggregate, which has no single
post for structural reasons no policy change can fix. The row's own date used to
be a second link to the same page; the owner made it static text on 2026-10-04,
so the overlay is now the only way a row opens.
"""

import re
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from reeltalk.core.models import (
    Film,
    Shelf,
    ShelfFilm,
    Status,
    mark_watched,
    shelve_to_watchlist,
)
from reeltalk.tests.members import member, site_admin

User = get_user_model()

CSS_PATH = (
    Path(__file__).resolve().parents[1] / "social" / "static" / "css" / "reeltalk.css"
)


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return member(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def admin(db):
    # R12: / redirects to /setup/ until a superuser exists, so the feed
    # never renders and an absence assertion against that body proves nothing.
    return site_admin(localname="admin", password="s3cretpass")


def _remote_user(localname: str = "carol@remote.example") -> User:
    """A remote mirror account, as federation creates it (no local password)."""
    user = User(
        localname=localname,
        local=False,
        actor_url=f"https://remote.example/users/{localname.split('@')[0]}",
        inbox_url=f"https://remote.example/users/{localname.split('@')[0]}/inbox",
    )
    user.set_unusable_password()
    user.save()
    return user


def _login(localname):
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


def _home(client) -> str:
    response = client.get("/")
    assert response.status_code == 200
    return response.content.decode()


def _row(body, status_id) -> str:
    """One feed row's own markup, so a check cannot borrow another row.

    The marker stops before the tag's closing ``>`` on purpose. Increment 3
    (§2I) added ``data-cursor`` to the row element, and pinning the closing
    bracket here made this helper break on an attribute that has nothing to do
    with what it is checking. The status value is still matched exactly: the
    closing quote of ``data-status="N"`` is in the marker, so row 39 cannot
    satisfy a lookup for row 392.
    """
    marker = f'<li class="review" data-status="{status_id}"'
    assert marker in body, "the target row did not render at all"
    start = body.index(marker)
    return body[start : body.index("</li>", start)]


def _css_rules() -> list[tuple[list[str], str]]:
    """(selectors, declarations) for every rule in the shipped stylesheet.

    Comments are stripped first because they sit between a rule's selector and
    its brace, and whitespace is collapsed so the assertions key on the
    declarations rather than on formatting.
    """
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)
    css = re.sub(r"\s+", " ", css)
    rules = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = [s.strip() for s in match.group(1).split(",") if s.strip()]
        rules.append((selectors, match.group(2).strip()))
    return rules


def _rule_for(selector: str) -> str | None:
    for selectors, declarations in _css_rules():
        if selector in selectors:
            return declarations
    return None


# --- The overlay -------------------------------------------------------------


@pytest.mark.django_db
def test_a_row_with_a_post_stretches_a_link_over_it(alice, dune, admin):
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    row = _row(_home(_login("alice")), review.pk)
    assert f'<a class="review-open" href="/status/{review.pk}/"' in row


@pytest.mark.django_db
def test_the_overlay_anchor_holds_no_content_of_its_own(alice, dune, admin):
    # The empty anchor is the whole reason no control inside the row ends up
    # nested in it. If a label were ever put *inside* the anchor instead of
    # behind aria-label, this is the assertion that catches the nesting.
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    row = _row(_home(_login("alice")), review.pk)
    assert 'aria-label="Open alice\'s post"></a>' in row


@pytest.mark.django_db
def test_the_overlay_is_named_for_its_author(alice, dune, admin):
    # With no text inside the anchor, aria-label is the only accessible name
    # it has. A screen reader tabbing the feed must not meet a nameless link.
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    row = _row(_home(_login("alice")), review.pk)
    assert 'aria-label="Open alice\'s post"' in row


@pytest.mark.django_db
def test_the_overlay_closes_before_the_like_button(alice, dune, admin):
    # Order plus emptiness is what makes the Like button a sibling that paints
    # above the overlay rather than a child the overlay would swallow.
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    row = _row(_home(_login("alice")), review.pk)
    overlay_end = row.index('class="review-open"')
    overlay_end = row.index("</a>", overlay_end)
    assert row.index('class="like-btn"') > overlay_end


@pytest.mark.django_db
def test_a_mention_link_in_the_prose_is_not_nested_in_the_overlay(
    alice, bob, dune, admin
):
    # The nested-anchor landmine: review prose is rendered markdown and can
    # carry its own links. They must stay outside the overlay's element, or
    # the page is invalid HTML and the browser re-breaks the nesting.
    parent = Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.COMMENT,
        content='<p>agree with <a href="/user/bob/">@bob</a></p>',
    )
    row = _row(_login("alice").get("/").content.decode(), parent.pk)
    assert 'href="/user/bob/"' in row  # the mention really rendered…
    overlay = row[row.index('class="review-open"') :]
    overlay = overlay[: overlay.index("</a>")]
    assert "/user/bob/" not in overlay  # …and really sits outside the overlay


@pytest.mark.django_db
def test_each_row_overlay_points_at_its_own_post(alice, dune, admin):
    # Two rows in one feed. Bob is not followed here, so his post is not a
    # row at all — the second row has to be Alice's own, on a second film,
    # because the unique review-per-film rule caps her at one on Dune.
    mark_watched(alice, dune, rating="4.5", content="<p>Mine.</p>")
    mine = Status.objects.get(user=alice, film=dune)
    thing = Film.objects.create(title="The Thing", year=1982)
    theirs = Status.objects.create(
        user=alice,
        film=thing,
        status_type=Status.Type.COMMENT,
        content="<p>Also mine.</p>",
    )
    body = _home(_login("alice"))
    assert f'href="/status/{mine.pk}/"' in _row(body, mine.pk)
    assert f'href="/status/{theirs.pk}/"' in _row(body, theirs.pk)
    # Neither row carries the other's overlay.
    assert f'href="/status/{theirs.pk}/"' not in _row(body, mine.pk)
    assert f'href="/status/{mine.pk}/"' not in _row(body, theirs.pk)


@pytest.mark.django_db
def test_a_bare_shelf_row_gets_no_overlay(alice, dune, admin):
    shelve_to_watchlist(alice, dune)
    body = _home(_login("alice"))
    row = _row(body, "none")
    assert "to their Watchlist" in row  # the row really rendered…
    assert "review-open" not in row  # …and really carries nothing to open


@pytest.mark.django_db
def test_the_bulk_aggregate_row_gets_no_overlay(alice, admin):
    # No single film, therefore no single post: structural, not policy.
    shelf = Shelf.objects.get(user=alice, identifier=Shelf.TO_READ)
    for i in range(3):
        ShelfFilm.objects.create(
            shelf=shelf,
            film=Film.objects.create(title=f"Bulk {i}", year=2000 + i),
            user=alice,
        )
    body = _home(_login("alice"))
    row = _row(body, "none")
    assert "2 other films" in row
    assert "review-open" not in row


@pytest.mark.django_db
def test_a_remote_mirror_row_gets_its_overlay(alice, dune, admin):
    # R84 again: the overlay opens a page, so it follows status_id and not
    # interactivity. A mirror's page exists here — that is what decision 3
    # bought — so the mirror's row opens it.
    carol = _remote_user()
    alice.follows.add(carol)
    mirror = Status.objects.create(
        user=carol,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Their review.</p>",
        local=False,
        remote_url="https://remote.example/status/77",
    )
    row = _row(_home(_login("alice")), mirror.pk)
    assert f'href="/status/{mirror.pk}/"' in row
    assert 'aria-label="Open carol@remote.example\'s post"' in row


@pytest.mark.django_db
def test_a_rating_only_row_still_gets_an_overlay(alice, dune, admin):
    # The reason the target is the row and not the prose: a rating-only
    # review has no body div at all, so a body-only target would leave a row
    # with a real post behind it nothing to click.
    Status.objects.create(
        user=alice, film=dune, status_type=Status.Type.REVIEW_RATING, rating="4"
    )
    review = Status.objects.get(user=alice, film=dune)
    row = _row(_home(_login("alice")), review.pk)
    assert 'class="review-body"' not in row  # no prose to hang a target on…
    assert f'class="review-open" href="/status/{review.pk}/"' in row  # …still opens


# --- The author name ---------------------------------------------------------


@pytest.mark.django_db
def test_the_feed_author_name_links_to_the_profile(alice, dune, admin):
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    row = _row(_home(_login("alice")), review.pk)
    assert (
        f'<a class="review-author" href="{reverse("user-profile", args=["alice"])}">'
        "alice</a>" in row
    )


@pytest.mark.django_db
def test_the_author_link_goes_to_the_profile_not_the_post(alice, dune, admin):
    # Two links in one row that must not be confused: the name opens the
    # person, the row opens the post.
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    row = _row(_home(_login("alice")), review.pk)
    author = row[row.index('class="review-author"') :]
    author = author[: author.index("</a>")]
    assert "/status/" not in author
    assert "/user/alice/" in author


@pytest.mark.django_db
def test_a_remote_authors_feed_link_points_at_their_mirror_profile(alice, dune, admin):
    carol = _remote_user()
    alice.follows.add(carol)
    mirror = Status.objects.create(
        user=carol,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Their review.</p>",
        local=False,
        remote_url="https://remote.example/status/78",
    )
    row = _row(_home(_login("alice")), mirror.pk)
    assert 'href="/user/carol@remote.example/"' in row


@pytest.mark.django_db
def test_the_genre_subfeed_author_name_links_to_the_profile(db):
    film = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    author = member(localname="alice", password="s3cretpass")
    Status.objects.create(
        user=author,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Classic.</p>",
    )
    body = Client().get("/genre/horror/").content.decode()
    assert 'class="review-author"' in body  # the row really rendered…
    assert '<a class="review-author" href="/user/alice/">alice</a>' in body


@pytest.mark.django_db
def test_the_film_page_author_name_links_to_the_profile(db):
    film = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    author = member(localname="alice", password="s3cretpass")
    Status.objects.create(
        user=author,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Classic.</p>",
    )
    body = Client().get(reverse("film", args=[film.pk])).content.decode()
    assert "Reviews (1)" in body  # the list really rendered…
    assert '<a class="review-author" href="/user/alice/">alice</a>' in body


# --- The stylesheet invariants the overlay depends on ------------------------


@pytest.mark.django_db
def test_the_row_is_the_containing_block_for_its_overlay():
    declarations = _rule_for(".review")
    assert declarations is not None, "no .review rule in the shipped stylesheet"
    assert "position: relative" in declarations


@pytest.mark.django_db
def test_the_overlay_stretches_over_the_whole_row():
    declarations = _rule_for(".review-open")
    assert declarations is not None
    assert "position: absolute" in declarations
    assert "inset: 0" in declarations
    assert "cursor: pointer" in declarations


@pytest.mark.django_db
def test_every_interactive_part_of_a_row_is_raised_above_the_overlay():
    # One rule, four selectors. If any of the four goes missing, whatever it
    # covered stops receiving its own clicks and the row navigates instead —
    # the failure mode the owner's whole ask turns on.
    raise_selectors = [
        ".review-head a",
        ".review-head button",
        ".feed-item a",
        ".review-body a",
    ]
    rules = [decls for selectors, decls in _css_rules() if selectors == raise_selectors]
    assert len(rules) == 1, f"expected exactly one raise rule, found {len(rules)}"
    assert "position: relative" in rules[0]
    assert "z-index: 1" in rules[0]


@pytest.mark.django_db
def test_nothing_underlines_the_prose_when_the_row_is_hovered():
    # The owner took the hover underline away on 2026-10-04: post text must not
    # light up as followable text merely because the row opens. The overlay keeps
    # ``cursor: pointer`` (pinned above), so the row still reads as clickable.
    #
    # This is written as an absence check over *any* hover/focus rule that names
    # the prose, not just the one selector that used to exist — a re-introduced
    # underline under a different selector is the same regression. Against the
    # stylesheet as it stood before this change it fails on
    # ``.review-open:hover ~ .review-body``, which is what makes it real.
    offenders = [
        selectors
        for selectors, declarations in _css_rules()
        if any(
            "review-body" in s and (":hover" in s or ":focus" in s) for s in selectors
        )
        and re.search(r"text-decoration:\s*underline", declarations)
    ]
    assert offenders == [], f"a hover/focus rule underlines the prose: {offenders}"


@pytest.mark.django_db
def test_post_prose_is_white_not_cream():
    # Owner pass, 2026-10-04. Asserted against the token rather than a literal so
    # the palette stays the single place the colour is decided.
    declarations = _rule_for(".review-body")
    assert declarations is not None, "no .review-body rule in the shipped stylesheet"
    assert "color: var(--white)" in declarations
    assert "var(--cream)" not in declarations


@pytest.mark.django_db
def test_the_row_date_is_static_text_not_a_link(alice, dune, admin):
    # The row already opens through .review-open, so the linked date was a second
    # way to the same page — and it rendered as red underlined text. Owner pass,
    # 2026-10-04.
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    row = _row(_home(_login("alice")), review.pk)
    assert '<span class="post-date">' in row
    date_start = row.index('<span class="post-date">')
    date_span = row[date_start : row.index("</span>", date_start)]
    assert "<a " not in date_span, f"the date is still a link: {date_span}"


@pytest.mark.django_db
def test_the_date_style_is_white_not_the_muted_cream():
    # The class carries the colour; the template only names it. Asserted on the
    # token so the palette stays the one place white is decided.
    declarations = _rule_for(".post-date")
    assert declarations is not None, "no .post-date rule in the shipped stylesheet"
    assert "color: var(--white)" in declarations
    assert "var(--cream)" not in declarations
