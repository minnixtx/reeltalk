"""Authoring a list (§2K increment 3).

Six groups, ordered by how they bite:

* **The description pair and the edited stamp.** The service renders the
  markdown, so what the form sends must arrive as source and be rendered
  exactly once — a ``clean_description`` copied from ``FilmForm`` would render
  it twice. And the face's ``edited_date`` must follow real edits only: a
  no-op add, a removal of something absent, or an "up" on the first row must
  leave a list that was never edited reading as never edited.
* **The create route**, and the fact that it lands in the editor.
* **Owner-only, both halves (R85).** Every write route is exercised directly
  by a stranger rather than only checked for a hidden button, because "the
  control is not rendered" is not a permission.
* **The editor's writes** — reorder, remove, add — proven by re-rendering,
  with the ends treated as normal outcomes and an unknown direction refused
  at the boundary rather than raised out of the service.
* **The delete edge**: ``delete_review`` refuses a list's face outright, so
  the only way down is the list's own delete, which takes both halves.
* **The search box** marking what is already in the list, and the entry
  points that lead to the editor.

Absence assertions name the specific element rather than the page body, so a
"not present" check cannot pass by having read the wrong part of the page.
"""

import re

import pytest
import responses
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from reeltalk.core.models import Film, mark_watched
from reeltalk.lists.models import FilmList, ListItem
from reeltalk.lists.services import (
    MOVE_DOWN,
    MOVE_UP,
    add_films,
    create_list,
    move,
    remove_film,
    rename,
    set_description,
    soft_delete_list,
)
from reeltalk.tests.members import member

User = get_user_model()

TMDB_DETAILS_URL = "https://api.themoviedb.org/3/movie/12345"

# Every write route paired with a body that would succeed if the poster owned
# the list. Each is posted by a stranger, so the refusal has to be in the
# route: a template-only check would let this through.
WRITE_ROUTES = [
    "list-edit",
    "list-add-film",
    "list-remove-film",
    "list-move",
    "list-delete",
]


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
def alien(db):
    return Film.objects.create(title="Alien", year=1979)


@pytest.fixture
def blade(db):
    return Film.objects.create(title="Blade Runner", year=1982)


@pytest.fixture
def listing(alice, dune, alien, blade):
    return create_list(
        alice,
        title="Cold War Sci-Fi",
        description="Paranoia, in black and white.",
        films=[dune, alien, blade],
    )


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


def _body(response) -> str:
    assert response.status_code == 200
    return response.content.decode()


def _ranks(film_list) -> list[int]:
    return list(
        ListItem.objects.filter(film_list=film_list)
        .order_by("rank", "id")
        .values_list("rank", flat=True)
    )


def _items(film_list) -> list[ListItem]:
    return list(ListItem.objects.filter(film_list=film_list).order_by("rank", "id"))


def _printed_ordinals(body: str) -> list[str]:
    """The numbers the page actually shows, in order.

    Read out of the ``.rank-num`` spans specifically: the point is that these
    come from ``forloop.counter`` and not from ``rank``, so after a removal
    they must still read 1, 2 while the ranks underneath read 1, 3.
    """
    return re.findall(r'<span class="rank-num">(\d+)</span>', body)


def _titles_in_order(body: str) -> list[str]:
    """Each ranked row's film title, tags and the trailing ``(year)`` removed.

    The row wraps the year in ``<span class="muted">``, so stripping the
    parenthesised text alone would leave the empty span behind and every title
    would compare unequal. Tags first, then a trailing four-digit year — the
    year only, so a title that legitimately contains parentheses survives.
    """
    titles = []
    for raw in re.findall(r'<a class="rank-film"[^>]*>(.*?)</a>', body, re.S):
        text = re.sub(r"<[^>]+>", "", raw)
        text = re.sub(r"\s*\(\d{4}\)\s*$", "", text)
        titles.append(text.strip())
    return titles


def _stranger_body(route, film_list, film):
    """A body that would work for the owner of ``film_list``."""
    if route == "list-edit":
        return {"title": "Hijacked", "description": "not yours"}
    if route == "list-add-film":
        return {"film_id": film.pk}
    if route == "list-remove-film":
        return {"film_id": film.pk}
    if route == "list-move":
        return {"item_id": _items(film_list)[0].pk, "direction": MOVE_DOWN}
    return {}


# --- the description pair and the edited stamp ------------------------------


def test_set_description_renders_once_and_keeps_the_source(alice):
    listing = create_list(alice, title="T", description="plain words")
    set_description(listing, "**paranoia**, in black and white")
    listing.refresh_from_db()
    # The source is stored verbatim, which is what the edit form pre-fills.
    assert listing.raw_description == "**paranoia**, in black and white"
    assert "<strong>paranoia</strong>" in listing.description
    # The double-render signature: markup rendered, then rendered again,
    # arrives escaped. Its absence is the proof.
    assert "&lt;strong&gt;" not in listing.description
    assert "&lt;p&gt;" not in listing.description


def test_set_description_can_be_cleared_back_to_blank(alice):
    listing = create_list(alice, title="T", description="something here")
    set_description(listing, "")
    listing.refresh_from_db()
    assert listing.raw_description == ""
    assert listing.description == ""


def test_creating_a_list_leaves_the_face_unedited_even_with_films(alice, dune):
    """A list that was *made* is not a list that was *edited*.

    ``create_list`` fills its films through the un-stamped append for exactly
    this reason: a face stamped at creation would go out on the wire
    (increment 6) reporting an ``editedTime`` for an edit nobody made.
    """
    listing = create_list(alice, title="T", description="d", films=[dune])
    assert listing.status.edited_date is None


@pytest.mark.parametrize(
    "op",
    ["rename", "set_description", "add_films", "remove_film", "move"],
)
def test_every_real_edit_stamps_the_face(alice, dune, alien, op):
    listing = create_list(alice, title="T", description="d", films=[dune])
    assert listing.status.edited_date is None
    if op == "rename":
        rename(listing, "Renamed")
    elif op == "set_description":
        set_description(listing, "New prose")
    elif op == "add_films":
        add_films(listing, [alien])
    elif op == "remove_film":
        remove_film(listing, dune)
    else:
        add_films(listing, [alien])
        move(_items(listing)[-1], MOVE_UP)
    listing.refresh_from_db()
    assert listing.status.edited_date is not None, f"{op} did not stamp the face"


def test_an_edit_that_changes_nothing_leaves_the_face_unedited(alice, dune, alien):
    """The other half of the stamp rule, and the half a lazy implementation
    misses. Pressing a button that does nothing must not make a list look
    edited: adding what is already there, removing what is not, and moving at
    the end all return without touching ``edited_date``."""
    listing = create_list(alice, title="T", description="d", films=[dune])
    add_films(listing, [dune])
    remove_film(listing, alien)
    move(_items(listing)[0], MOVE_UP)
    listing.refresh_from_db()
    assert listing.status.edited_date is None


# --- the create route -------------------------------------------------------


def test_creating_through_the_route_lands_in_the_editor(alice):
    client = _login("alice")
    response = client.post(
        reverse("list-create"), {"title": "New List", "description": " prose "}
    )
    made = FilmList.objects.get(user=alice)
    assert response.status_code == 302
    assert response.headers["Location"] == f"/list/{made.pk}/edit/"


def test_the_route_stores_the_markdown_source_not_rendered_html(alice):
    _login("alice").post(
        reverse("list-create"),
        {"title": "T", "description": "**lit** up"},
    )
    made = FilmList.objects.get(user=alice)
    assert made.raw_description == "**lit** up"
    assert "<strong>lit</strong>" in made.description
    assert "&lt;strong&gt;" not in made.description


def test_a_list_needs_a_name(alice):
    before = FilmList.objects.filter(user=alice).count()
    response = _login("alice").post(reverse("list-create"), {"title": ""})
    assert response.status_code == 200  # re-rendered, with the error shown
    assert FilmList.objects.filter(user=alice).count() == before


def test_anonymous_cannot_reach_the_create_page():
    response = Client().get(reverse("list-create"))
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login/")


# --- owner-only, both halves (R85) -----------------------------------------


@pytest.mark.parametrize("route", WRITE_ROUTES)
def test_a_stranger_posting_to_a_write_route_is_refused(
    route, alice, bob, dune, alien, listing
):
    response = _login("bob").post(
        reverse(route, args=[listing.pk]),
        _stranger_body(route, listing, alien),
    )
    assert response.status_code == 404, f"{route} let a stranger through"
    listing.refresh_from_db()
    assert listing.title == "Cold War Sci-Fi"
    assert ListItem.objects.filter(film_list=listing).count() == 3
    assert listing.deleted is False


@pytest.mark.parametrize("route", WRITE_ROUTES)
def test_anonymous_posting_to_a_write_route_is_refused(route, listing, dune):
    response = Client().post(
        reverse(route, args=[listing.pk]),
        _stranger_body(route, listing, dune),
    )
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login/")


def test_the_edit_page_is_not_there_for_a_non_owner(listing, bob):
    response = _login("bob").get(reverse("list-edit", args=[listing.pk]))
    assert response.status_code == 404


def test_the_edit_link_shows_for_the_maker_and_not_for_a_stranger(listing, bob):
    own = _body(_login("alice").get(reverse("list-detail", args=[listing.pk])))
    assert 'class="list-edit-link"' in own

    theirs = _body(_login("bob").get(reverse("list-detail", args=[listing.pk])))
    assert 'class="list-edit-link"' not in theirs
    # And not because the page failed to render for the stranger: it did.
    assert "Cold War Sci-Fi" in theirs


def test_an_anonymous_reader_sees_no_edit_link(listing):
    body = _body(Client().get(reverse("list-detail", args=[listing.pk])))
    assert 'class="list-edit-link"' not in body


# --- the editor's writes ----------------------------------------------------


def test_the_editor_prefills_the_markdown_source_not_the_html(listing):
    body = _body(_login("alice").get(reverse("list-edit", args=[listing.pk])))
    assert "Paranoia, in black and white." in body
    # The rendered <p> wrapper must not be sitting in the textarea.
    assert "&lt;p&gt;" not in body


def test_renaming_through_the_editor_changes_the_title(listing):
    _login("alice").post(
        reverse("list-edit", args=[listing.pk]),
        {"title": "Renamed Cold War Sci-Fi", "description": "x"},
    )
    listing.refresh_from_db()
    assert listing.title == "Renamed Cold War Sci-Fi"
    assert listing.status.edited_date is not None


def test_reordering_through_the_route_changes_the_rendered_order(listing, blade):
    client = _login("alice")
    third = ListItem.objects.filter(film_list=listing, film=blade).first()
    client.post(
        reverse("list-move", args=[listing.pk]),
        {"item_id": third.pk, "direction": MOVE_UP},
    )
    body = _body(client.get(reverse("list-edit", args=[listing.pk])))
    assert _titles_in_order(body) == ["Dune", "Blade Runner", "Alien"]
    assert _ranks(listing) == [1, 2, 3]


@pytest.mark.parametrize(
    "direction,expected",
    [(MOVE_UP, "already first"), (MOVE_DOWN, "already last")],
)
def test_pressing_into_the_end_is_a_normal_outcome_not_an_error(
    listing, direction, expected
):
    """``move`` returns a bool at the ends rather than raising, and the view
    neither wraps that in try/except nor pre-checks it. The row already at
    the end is pushed against that end and the page says so."""
    client = _login("alice")
    items = _items(listing)
    edge = items[0] if direction == MOVE_UP else items[-1]
    response = client.post(
        reverse("list-move", args=[listing.pk]),
        {"item_id": edge.pk, "direction": direction},
    )
    assert response.status_code == 302
    body = _body(client.get(reverse("list-edit", args=[listing.pk])))
    assert expected in body
    assert _ranks(listing) == [1, 2, 3]


def test_an_unknown_direction_is_refused_at_the_boundary(listing):
    """``services.move`` raises on an unknown direction because that is a
    caller's typo. A hand-typed form field is not, so the route answers 400
    rather than letting the raise surface as a 500."""
    response = _login("alice").post(
        reverse("list-move", args=[listing.pk]),
        {"item_id": _items(listing)[0].pk, "direction": "sideways"},
    )
    assert response.status_code == 400
    assert _ranks(listing) == [1, 2, 3]


def test_a_move_cannot_reach_into_another_members_list(listing, bob, dune):
    other = create_list(bob, title="Bob's", films=[dune])
    their_item = _items(other)[0]
    response = _login("alice").post(
        reverse("list-move", args=[listing.pk]),
        {"item_id": their_item.pk, "direction": MOVE_UP},
    )
    assert response.status_code == 404
    assert _ranks(other) == [1]


def test_removing_a_film_leaves_the_gap_and_the_page_still_numbers_from_one(
    listing, alien
):
    client = _login("alice")
    client.post(reverse("list-remove-film", args=[listing.pk]), {"film_id": alien.pk})
    # The ordering key keeps the hole; nothing renumbered to tidy it.
    assert _ranks(listing) == [1, 3]
    body = _body(client.get(reverse("list-edit", args=[listing.pk])))
    assert _printed_ordinals(body) == ["1", "2"]
    assert _titles_in_order(body) == ["Dune", "Blade Runner"]


def test_adding_a_local_film_puts_it_at_the_end(listing):
    extra = Film.objects.create(title="The Thing", year=1982)
    client = _login("alice")
    client.post(reverse("list-add-film", args=[listing.pk]), {"film_id": extra.pk})
    assert _ranks(listing) == [1, 2, 3, 4]
    body = _body(client.get(reverse("list-edit", args=[listing.pk])))
    assert _titles_in_order(body)[-1] == "The Thing"


def test_adding_a_film_that_is_already_in_the_list_is_a_no_op(listing, dune):
    client = _login("alice")
    response = client.post(
        reverse("list-add-film", args=[listing.pk]), {"film_id": dune.pk}
    )
    assert response.status_code == 302
    assert ListItem.objects.filter(film_list=listing).count() == 3
    body = _body(client.get(reverse("list-edit", args=[listing.pk])))
    assert "is already in this list" in body


def test_a_malformed_add_is_refused_not_crashed(listing):
    response = _login("alice").post(
        reverse("list-add-film", args=[listing.pk]), {"film_id": "not-a-number"}
    )
    assert response.status_code == 400


def test_adding_both_ids_at_once_is_refused(listing, dune):
    response = _login("alice").post(
        reverse("list-add-film", args=[listing.pk]),
        {"film_id": dune.pk, "tmdb_id": 12345},
    )
    assert response.status_code == 400


def test_a_malformed_move_id_is_refused_not_crashed(listing):
    response = _login("alice").post(
        reverse("list-move", args=[listing.pk]),
        {"item_id": "abc", "direction": MOVE_UP},
    )
    assert response.status_code == 400


@responses.activate
@override_settings(TMDB_API_KEY="***")
def test_adding_from_tmdb_runs_the_existing_find_or_create(listing):
    """The TMDB arm reuses ``create_or_match_film`` rather than inventing a
    second way for a search hit to become a local film."""
    responses.add(
        responses.GET,
        TMDB_DETAILS_URL,
        json={
            "id": 12345,
            "title": "The Day the Earth Stood Still",
            "release_date": "1951-09-28",
            "runtime": 92,
            "overview": "An alien lands.",
            "poster_path": None,
            "genres": [],
            "credits": {"crew": [], "cast": []},
        },
        status=200,
    )
    client = _login("alice")
    client.post(reverse("list-add-film", args=[listing.pk]), {"tmdb_id": 12345})
    film = Film.objects.get(tmdb_id=12345)
    assert ListItem.objects.filter(film_list=listing, film=film).exists()
    assert _ranks(listing) == [1, 2, 3, 4]


# --- the search box ---------------------------------------------------------
#
# Every test here pins the TMDB key explicitly, in both directions. This
# instance has a real key configured, so a test that left the setting alone
# would silently spend real TMDB quota, depend on the network, and assert
# against whatever TMDB happens to return today rather than against the code
# under test. The local-path tests say "no key"; the one TMDB-path test says
# "key plus a mocked response". Neither leaves the source of the rows to
# chance.

LOCAL_ONLY = override_settings(TMDB_API_KEY="")


@LOCAL_ONLY
def test_the_search_box_marks_a_film_that_is_already_in_the_list(listing):
    """Without the mark, a curation tool offers an "Add" button for a film
    that is already there and the button appears broken."""
    body = _body(
        _login("alice").get(reverse("list-edit", args=[listing.pk]) + "?q=Alien")
    )
    assert "In this list" in body
    assert "Add to list" not in body


@LOCAL_ONLY
def test_the_search_box_offers_a_film_that_is_not_in_the_list(listing):
    Film.objects.create(title="Metropolis", year=1927)
    body = _body(
        _login("alice").get(reverse("list-edit", args=[listing.pk]) + "?q=Metropolis")
    )
    assert "Metropolis" in body
    assert "Add to list" in body
    assert "In this list" not in body


@LOCAL_ONLY
def test_a_search_with_no_hits_says_so(listing):
    body = _body(
        _login("alice").get(
            reverse("list-edit", args=[listing.pk]) + "?q=zzzznomatchesatall"
        )
    )
    assert "Nothing found" in body


@responses.activate
@override_settings(TMDB_API_KEY="***")
def test_the_tmdb_path_marks_a_hit_that_matches_a_film_already_in_the_list(alice, dune):
    """The primary path, and the reason the mark is computed on ``tmdb_id``
    rather than on the local film id: a TMDB row has no local film id at all,
    because the hit does not become a film until someone adds it. So the list
    member's ``tmdb_id`` is the only thing the two sides can meet on."""
    sourced = Film.objects.create(title="The Thing", year=1982, tmdb_id=18341)
    listing = create_list(alice, title="Who Goes There", films=[dune, sourced])
    responses.add(
        responses.GET,
        "https://api.themoviedb.org/3/search/movie",
        json={
            "page": 1,
            "total_results": 2,
            "total_pages": 1,
            "results": [
                {
                    "id": 18341,
                    "title": "The Thing",
                    "release_date": "1982-06-25",
                },
                {
                    "id": 8382,
                    "title": "The Thing from Another World",
                    "release_date": "1951-01-01",
                },
            ],
        },
        status=200,
    )
    body = _body(
        _login("alice").get(reverse("list-edit", args=[listing.pk]) + "?q=The+Thing")
    )
    assert body.count("In this list") == 1
    assert body.count("Add to list") == 1


# --- the typeahead endpoint -------------------------------------------------


@LOCAL_ONLY
def test_the_typeahead_returns_local_rows_tagged_for_the_list(listing, alien):
    """Each row carries what the add route acts on, and whether the film is
    already in this list — the dropdown must not offer either decision to
    the member by accident."""
    extra = Film.objects.create(title="Alien 3", year=1992)
    response = _login("alice").get(
        reverse("list-suggest", args=[listing.pk]) + "?q=alien"
    )
    assert response.status_code == 200
    by_title = {r["title"]: r for r in response.json()["results"]}
    assert by_title["Alien"]["film_id"] == alien.pk
    assert by_title["Alien"]["in_list"] is True
    assert by_title["Alien"]["tmdb_id"] is None
    assert by_title["Alien 3"]["film_id"] == extra.pk
    assert by_title["Alien 3"]["in_list"] is False


@responses.activate
@override_settings(TMDB_API_KEY="***")
def test_the_typeahead_tags_a_tmdb_hit_already_in_the_list(alice, dune):
    """The primary path. A TMDB hit has no local film id, so the mark has to
    be made on ``tmdb_id`` — the only identifier the two sides share. A test
    that asserted only ``film_id`` here would pass while the mark was broken."""
    sourced = Film.objects.create(title="The Thing", year=1982, tmdb_id=18341)
    listing = create_list(alice, title="Who Goes There", films=[dune, sourced])
    responses.add(
        responses.GET,
        "https://api.themoviedb.org/3/search/movie",
        json={
            "page": 1,
            "total_results": 2,
            "total_pages": 1,
            "results": [
                {
                    "id": 18341,
                    "title": "The Thing",
                    "release_date": "1982-06-25",
                },
                {
                    "id": 8382,
                    "title": "The Thing from Another World",
                    "release_date": "1951-01-01",
                },
            ],
        },
        status=200,
    )
    rows = (
        _login("alice")
        .get(reverse("list-suggest", args=[listing.pk]) + "?q=the+thing")
        .json()["results"]
    )
    assert rows[0]["tmdb_id"] == 18341
    assert rows[0]["film_id"] is None
    assert rows[0]["in_list"] is True
    assert rows[1]["tmdb_id"] == 8382
    assert rows[1]["in_list"] is False


@LOCAL_ONLY
def test_the_typeahead_needs_two_characters(listing):
    rows = (
        _login("alice")
        .get(reverse("list-suggest", args=[listing.pk]) + "?q=a")
        .json()["results"]
    )
    assert rows == []


def test_the_typeahead_is_not_open_to_a_stranger(listing, bob):
    response = _login("bob").get(
        reverse("list-suggest", args=[listing.pk]) + "?q=alien"
    )
    assert response.status_code == 404


def test_the_typeahead_answers_json_not_a_login_redirect_when_anonymous(listing):
    """A 302 to the login page would hand the XHR HTML where it expects a
    payload, so the anonymous answer is an explicit 401 — the same choice
    ``/search/suggest/`` makes for the same reason."""
    response = Client().get(reverse("list-suggest", args=[listing.pk]) + "?q=alien")
    assert response.status_code == 401
    assert response["Content-Type"].startswith("application/json")


@LOCAL_ONLY
def test_the_editor_page_renders_the_typeahead_markup(listing):
    """The script is wired to a data attribute rather than a hardcoded path,
    so if the URL is missing the dropdown silently never works. This pins the
    wiring that the JS depends on."""
    body = _body(_login("alice").get(reverse("list-edit", args=[listing.pk])))
    assert f'data-suggest-url="/list/{listing.pk}/suggest/"' in body
    assert 'id="list-add-pick-form"' in body
    assert 'name="tmdb_id"' in body
    assert "list_editor.js" in body


# --- the delete edge --------------------------------------------------------


def test_the_review_delete_route_refuses_a_lists_face(listing):
    """The hole this closes: ``delete_review`` looks up a ``Status``, and a
    list's face *is* a ``Status``, so the route used to admit it and take the
    face down while the ``FilmList`` stayed alive."""
    face = listing.status
    response = _login("alice").post(reverse("status-delete", args=[face.pk]), {})
    assert response.status_code == 404
    face.refresh_from_db()
    listing.refresh_from_db()
    # Nothing half-happened: the face is untouched, so the list is still whole.
    assert face.deleted is False
    assert listing.deleted is False


def test_the_review_delete_route_still_deletes_an_ordinary_review(alice, dune):
    """The guard must not take the real use case down with it."""
    status = mark_watched(alice, dune, rating=4, content="Good", raw_content="Good")
    _login("alice").post(reverse("status-delete", args=[status.pk]), {})
    status.refresh_from_db()
    assert status.deleted is True


def test_deleting_a_list_takes_its_face_down_together(listing):
    face = listing.status
    _login("alice").post(reverse("list-delete", args=[listing.pk]), {})
    listing.refresh_from_db()
    face.refresh_from_db()
    assert listing.deleted is True
    assert face.deleted is True


def test_a_stranger_cannot_delete_another_members_list(listing, bob):
    _login("bob").post(reverse("list-delete", args=[listing.pk]), {})
    listing.refresh_from_db()
    assert listing.deleted is False


def test_a_deleted_list_cannot_be_edited_back_to_life(listing):
    soft_delete_list(listing)
    response = _login("alice").post(
        reverse("list-edit", args=[listing.pk]),
        {"title": "Resurrected", "description": ""},
    )
    assert response.status_code == 404
    listing.refresh_from_db()
    assert listing.title == "Cold War Sci-Fi"


# --- the entry points -------------------------------------------------------


def test_the_new_list_button_shows_on_your_own_lists_page(alice, bob):
    own = _body(_login("alice").get(reverse("user-lists", args=["alice"])))
    assert 'href="/lists/new/"' in own

    theirs = _body(_login("alice").get(reverse("user-lists", args=["bob"])))
    assert 'href="/lists/new/"' not in theirs


def test_the_new_list_button_is_absent_on_the_saved_tab(alice):
    body = _body(
        _login("alice").get(reverse("user-lists", args=["alice"]) + "?tab=saved")
    )
    assert 'href="/lists/new/"' not in body
