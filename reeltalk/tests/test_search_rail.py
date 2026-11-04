"""M4: the rail modules gain a second home on the Search page (owner ruling D8).

Three moves, one increment (the plan's silent-stranding failure mode: they ship
together or not at all):

* the three modules (Trending Films, Top Reviews, Popular Genres) live in one
  partial now, so the home rail and the Search page render the same body;
* a member's home rail carries the ``rail--member`` treatment the stylesheet
  hides below 1280px — the hide itself is CSS and is proven in browser QA at
  375/768/1100/1440, not unit-pinned (plan M4's test note); what is pinned
  here is that the markup carries the handle, and that the anonymous rail
  stays plain and keeps every width;
* the Search page (login-gated, R80) renders the modules under the search box
  in its landing state, with the same empty-state rules the rail has: Top
  Reviews absent until something qualifies, the two fallback lines on a quiet
  instance. With a query the results take the page (owner ruling at M4's
  review): the modules never sit between the box and what the reader asked
  for.

The More-sheet half of D8 (the four informative links) is pinned in
``test_mobile_nav.py``.
"""

import re
from decimal import Decimal

import pytest
from django.test import Client, override_settings

from reeltalk.core.models import Film, Like, Status
from reeltalk.tests.members import member, site_admin

PASSWORD = "s3cretpass"


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def admin(db):
    return site_admin(localname="admin", password=PASSWORD)


@pytest.fixture
def alice(db):
    return member(localname="alice", password=PASSWORD)


@pytest.fixture
def bob(db):
    return member(localname="bob", password=PASSWORD)


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password=PASSWORD)
    return client


def _review(user, film):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=Decimal("4.00"),
        content="<p>a solid watch</p>",
        raw_content="a solid watch",
    )


def _seed_instance(alice):
    """One reviewed film: Trending and Genres have rows, Top Reviews does not."""
    film = Film.objects.create(title="Alien", year=1979, genres=["Horror"])
    _review(alice, film)
    return film


def _search_rail(body: str) -> str:
    match = re.search(r'<aside class="search-rail".*?</aside>', body, re.S)
    assert match, "no .search-rail aside on the Search page"
    return match.group(0)


# --- the home rails (the markup half; the width is browser QA's) ------------


@pytest.mark.django_db
def test_member_home_marks_the_rail_with_the_member_treatment(client, admin, alice):
    """The member's rail wears the handle the stylesheet hides below 1280px.

    The hide itself is CSS and lives in browser QA; what a unit test can pin
    is that the server renders the handle at all -- a template regression
    that drops the class would strand the hide rule with nothing to match.
    The rail markup still renders (the browser, not the view, decides who
    sees it), so the modules are present in the body either way.
    """
    _seed_instance(alice)
    body = _login("alice").get("/").content.decode()
    assert '<aside class="rail rail--member" aria-label="On this instance">' in body
    assert 'class="visually-hidden">Trending Films' in body


@pytest.mark.django_db
def test_anonymous_home_keeps_the_plain_rail(client, admin, alice):
    """D8's other half: the stranger's rail is untouched at every width."""
    _seed_instance(alice)
    body = client.get("/").content.decode()
    assert '<aside class="rail" aria-label="On this instance">' in body
    assert "rail--member" not in body


# --- the Search page landing state --------------------------------------------


@pytest.mark.django_db
def test_search_landing_carries_the_three_modules(client, admin, alice):
    """The modules render from the shared partial, member-linked (R80 means
    every Search viewer is a member, so the member branch always answers)."""
    film = _seed_instance(alice)
    body = _login("alice").get("/search/").content.decode()
    rail = _search_rail(body)
    assert 'class="visually-hidden">Trending Films' in rail
    assert 'class="visually-hidden">Popular Genres' in rail
    assert "Alien" in rail
    assert f'href="/film/{film.id}/"' in rail
    assert 'href="/genre/horror/"' in rail
    # The landing hint and the manual-add link are untouched.
    assert "Use the search box" in body
    assert "Add a film manually" in body


@pytest.mark.django_db
def test_search_landing_and_home_rail_render_one_body(client, admin, alice):
    """The anti-drift proof of the extraction: the module bodies are identical.

    Same reader, same instance, two surfaces -- the partial cannot have grown
    a per-surface copy. The slice runs from the first ticket banner to the
    last panel section on each side: the home rail's footer and the Search
    aside's own tag are call-site markup, not the shared body.
    """
    _seed_instance(alice)
    member_client = _login("alice")
    home = member_client.get("/").content.decode()
    search = _search_rail(member_client.get("/search/").content.decode())

    def modules(markup: str) -> str:
        start = markup.index('<img class="rail-banner"')
        end = markup.rindex("</section>") + len("</section>")
        return markup[start:end].strip()

    assert modules(home) == modules(search)
    # Non-vacuity: the body really is the modules, not two empty strings.
    assert "Trending Films" in modules(home)


@pytest.mark.django_db
def test_search_landing_on_a_quiet_instance_falls_back(client, admin, alice):
    """The rail's empty-state rules ride along: both fallback lines, and no
    Top Reviews box at all (a box of zeros is noise)."""
    body = _login("alice").get("/search/").content.decode()
    rail = _search_rail(body)
    assert "No reviews in the last 30 days." in rail
    assert "Genres appear once films start getting reviews." in rail
    assert "top-reviews.png" not in rail


@pytest.mark.django_db
def test_search_top_reviews_stays_absent_until_something_qualifies(
    client, admin, alice, bob
):
    """A review nobody answered is not Top Reviews — on either surface."""
    film = Film.objects.create(title="Alien", year=1979)
    _review(alice, film)
    body = _login("bob").get("/search/").content.decode()
    assert "top-reviews.png" not in body
    assert 'class="visually-hidden">Top Reviews' not in body


@pytest.mark.django_db
def test_search_top_reviews_appears_once_a_review_is_received(
    client, admin, alice, bob
):
    film = Film.objects.create(title="Alien", year=1979)
    review = _review(alice, film)
    Like.objects.create(user=bob, status=review)

    rail = _search_rail(_login("bob").get("/search/").content.decode())
    assert "top-reviews.png" in rail
    assert 'class="visually-hidden">Top Reviews' in rail
    assert "alice" in rail
    assert "a solid watch" in rail


@override_settings(TMDB_API_KEY="")
@pytest.mark.django_db
def test_a_query_takes_the_page_and_the_modules_step_aside(client, admin, alice):
    """The owner's ruling at M4's review: landing-state only. The modules
    never sit between the box and the results, and they do not repeat under
    them either. (Empty key pins the local path: no TMDB call, no flake.)"""
    _seed_instance(alice)
    member_client = _login("alice")

    listed = member_client.get("/search/?q=alien").content.decode()
    assert "search-rail" not in listed
    assert "Alien" in listed  # the result itself is still there

    empty = member_client.get("/search/?q=zzzznope").content.decode()
    assert "search-rail" not in empty
    assert "No films found" in empty
