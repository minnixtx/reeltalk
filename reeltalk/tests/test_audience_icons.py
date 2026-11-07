"""The audience markers are drawings (owner pass, 2026-11-07).

The word tags — "Private", "Followers" — became icons, and Public got one of its
own, so every post now states its audience instead of only the narrow ones. Four
ways that could rot, one test each:

* **Two audiences drawing the same art.** A member who cannot tell the padlock
  from the globe has lost the distinction the words carried, and nothing but the
  path data keeps the three apart. Asserted per page, one audience at a time, so
  a wrong branch shows as the wrong drawing rather than a count that still adds
  up.
* **Public quietly staying unmarked.** It was the one value every call site
  filtered out for three increments. A surface that drops its filter and forgets
  the icon, or the reverse, is the whole bug class here.
* **The drawing copied.** One partial feeds four surfaces. A second copy is a
  second answer, and the one that drifts is the one nobody looks at.
* **The icon arriving silent.** An icon with no name is a shape a sighted member
  decodes and a screen reader never hears. The word is still there — as the
  tooltip and the accessible name — and that is what the last two tests hold:
  the name on the markup, and no rule around the drawing, which the owner took
  off deliberately.

Proof is rendered pages through real client logins, as in the other visibility
modules: an anonymous render of these surfaces shows none of this markup at all,
so it would pass with the partial deleted.
"""

import re
from pathlib import Path

import pytest
from django.template import loader
from django.test import Client
from django.urls import reverse

from reeltalk.core.models import FeedEntry, Film, Status, Visibility
from reeltalk.tests.members import member, site_admin

PASSWORD = "s3cretpass"
REPO_ROOT = Path(__file__).resolve().parents[2]
CSS_PATH = REPO_ROOT / "reeltalk" / "social" / "static" / "css" / "reeltalk.css"
PARTIAL = REPO_ROOT / "templates" / "_audience_tag.html"

# One datum per drawing, the part of each that no other drawing carries. These
# are the owner's artwork (``~/design-elements/reeltalk-{private,followers,
# public}.svg``) as it sits in the partial; the file-copy test below is what
# keeps them honest to it.
ART = {
    "private": 'd="M6.5 10V7.5a5.5 5.5 0 0 1 11 0V10"',
    "followers": 'd="M2.75 20v-1.25a6.25 4.75 0 0 1 12.5 0V20"',
    "public": '<ellipse cx="12" cy="12" rx="4" ry="9" />',
}
WORDS = {"private": "Private", "followers": "Followers", "public": "Public"}


def marker(word: str) -> str:
    """The open tag of a rendered marker whose name is ``word``."""
    return f'<span class="audience" role="img" title="{word}" aria-label="{word}">'


def logged_in(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def page_of(client, url_name: str, *args) -> str:
    """A rendered page, as text."""
    return client.get(reverse(url_name, args=args)).content.decode()


@pytest.fixture(autouse=True)
def admin(db):
    """``/`` redirects to ``/setup/`` until a superuser exists, and then every
    feed assertion below would pass on a page with no rows in it at all."""
    return site_admin(localname="admin", password=PASSWORD)


@pytest.fixture
def author(db):
    return member(localname="author", password=PASSWORD)


@pytest.fixture
def mod(db):
    return member(localname="mod", password=PASSWORD, is_moderator=True)


@pytest.fixture
def films(db):
    return [
        Film.objects.create(title="Dune", year=2021),
        Film.objects.create(title="Arrival", year=2016),
        Film.objects.create(title="Annihilation", year=2018),
    ]


@pytest.fixture
def posts(db, author, films):
    """One review per audience, each on its own film.

    Keyed on the plain values, not the choice members: an enum member hashes as
    its own name, so a lookup by ``"private"`` would miss.
    """
    return {
        choice.value: Status.objects.create(
            user=author,
            film=film,
            status_type=Status.Type.REVIEW,
            content=f"<p>note on the {choice.value} film</p>",
            raw_content=f"note on the {choice.value} film",
            rating=4,
            visibility=choice.value,
        )
        for choice, film in zip(Visibility, films, strict=True)
    }


# --- one audience, one drawing ----------------------------------------------


@pytest.mark.django_db
def test_each_audience_draws_its_own_drawing(db, author, posts):
    """The post page holds exactly one marker, so it shows which art each
    audience gets — with the other two drawings absent from the same page."""
    for value, status in posts.items():
        page = page_of(logged_in(author), "status", status.id)
        assert marker(WORDS[value]) in page, f"{value} lost its marker"
        assert ART[value] in page, f"{value} drew the wrong art"
        for other, datum in ART.items():
            if other != value:
                assert datum not in page, f"{value} picked up the {other} drawing"


@pytest.mark.django_db
def test_the_word_is_still_on_the_page_as_the_name(db, author, posts):
    """The drawing is not silent: the audience word is the marker's accessible
    name and its tooltip, so nobody has to learn three shapes to read a row."""
    for value, status in posts.items():
        page = page_of(logged_in(author), "status", status.id)
        assert marker(WORDS[value]) in page


# --- every audience is drawn, public included --------------------------------


@pytest.mark.django_db
def test_a_public_post_is_marked_on_every_member_surface(db, author, posts):
    """The change itself: public used to be the value that drew nothing.

    Three surfaces, because the filter that hid it lived in three templates and
    a fix that landed in one of them would leave the feed and the film page
    disagreeing about whether a public post has an audience.
    """
    public = posts["public"]
    member_view = logged_in(author)
    assert marker("Public") in page_of(member_view, "index")
    assert marker("Public") in page_of(member_view, "status", public.id)
    assert marker("Public") in page_of(member_view, "film", public.film_id)


@pytest.mark.django_db
def test_the_moderator_queue_marks_every_reported_post(db, author, mod, posts):
    """The queue marks what it shows, and it shows reported posts whatever
    their audience — so every card now carries a drawing, not only the narrow
    ones. The written sentence stays gated on a narrow audience: it is the card
    saying "this was not published to the world", which a public post is not.

    Two of the three cards are filed here. A private post cannot be reported
    locally at all — the door admits readers only, and refuses self-reports —
    so its card arrives over a verified remote Flag, which is exactly how
    ``test_visibility_moderation.py`` files and marks it.
    """
    mod.follows.add(author)
    for value in ("public", "followers"):
        resp = logged_in(mod).post(
            reverse("report-status", args=[posts[value].id]),
            {"category": "spam", "comment": "triage me"},
        )
        assert resp.status_code == 302, f"the {value} report did not file"
    page = page_of(logged_in(mod), "moderation")
    assert marker("Public") in page
    assert marker("Followers") in page
    assert "marked Public" not in page


@pytest.mark.django_db
def test_an_audience_we_do_not_have_draws_nothing(db):
    """An unrecognised value is no marker, not a guessed one — the rule the word
    tag already kept, now on the drawing's key."""
    assert 'class="audience"' not in loader.render_to_string(
        "_audience_tag.html", {"visibility": "direct", "label": "Direct"}
    )
    entry = FeedEntry(
        kind="status",
        user=None,
        film=None,
        date=None,
        source_id=1,
        visibility="direct",
    )
    assert entry.audience_label == ""


def test_the_drawing_lives_in_one_partial():
    """Four surfaces draw the marker and exactly one file holds the art.

    A copy is a second answer, and the copy that drifts is the one nobody has
    open — so this names the holder rather than counting them.
    """
    holders = [
        path
        for path in REPO_ROOT.rglob("*.html")
        if ".qwen" not in path.parts and ART["private"] in path.read_text()
    ]
    assert holders == [PARTIAL]
    for name in ("private", "followers", "public"):
        assert ART[name] in PARTIAL.read_text(), f"the {name} drawing left the partial"


def test_the_marker_is_a_bare_drawing():
    """The owner took the bordered label off, so nothing may quietly put a rule
    back: the marker sits in the row's metadata line as a drawing, in the same
    muted tone as the date, with no chrome of its own."""
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(), flags=re.S)
    start = css.index(".audience {")
    block = css[start : css.index("}", start)]
    for gone in ("border", "padding", "text-transform", "background"):
        assert gone not in block, f".audience grew {gone} again"
    assert "var(--muted)" in block, ".audience no longer takes the muted tone"
    start = css.index(".audience svg {")
    svg = css[start : css.index("}", start)]
    assert "width" in svg and "height" in svg, "the drawing has no size of its own"


def test_the_partial_is_what_the_surfaces_include():
    """Each surface draws the marker through the partial, so none of them can
    reintroduce a word tag or an icon of its own."""
    surfaces = [
        REPO_ROOT / "templates/_feed_row.html",
        REPO_ROOT / "reeltalk/core/templates/core/status/detail.html",
        REPO_ROOT / "reeltalk/core/templates/core/film/detail.html",
        REPO_ROOT / "reeltalk/moderation/templates/moderation/index.html",
    ]
    for path in surfaces:
        src = path.read_text()
        assert '{% include "_audience_tag.html"' in src, f"{path.name} draws its own"
        assert 'class="audience"' not in src, f"{path.name} carries a second marker"
