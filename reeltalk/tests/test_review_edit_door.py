"""The review edit door (author edit/delete arc, increment I3, first half).

I3 adds no editor. The film page already carries a mark-watched modal that opens
prefilled with the author's own text and stars, so this increment only moves the
handle to the post page: an Edit link that lands on the film page with
``?edit-review=1``, and the film page renders that modal already open when the
param arrives and the member has a review here (owner ruling 1).

Three things can rot here, one test family each:

* **The link and the door disagree** (R85). ``may_edit_review`` is the single
  home of who may — author, local, live, a review type, and a film to land on —
  asked by the post page before it renders the link. Every row shape is pinned
  against it, and the two doors are pinned disjoint: a reply carries the inline
  editor I2 built, never this link.
* **The param opens a blank form.** The modal opens only when the member has a
  review of their own on this film, so an "edit" promise never lands on the
  empty mark-watched form — bob arriving with the param on alice's film gets a
  shut modal, and so does alice on a film she never finished.
* **The open modal is not the prefilled one.** Opening is the same modal, not a
  second one: the rating and the raw markdown still ride in.

Absence is asserted on the set of hrefs the page's Edit links carry
(``_edit_links``), never on a substring of the body, for the reason I1 filed in
its own file: a page-wide "not in body" check passes vacuously the moment the
markup changes shape.
"""

import re

import pytest
from django.test import Client

from reeltalk.core.models import Film, Status, add_reply, may_edit_review
from reeltalk.lists.services import create_list, rename
from reeltalk.social.models import User
from reeltalk.tests.members import member

# Every row shape the gate answers, and whether alice (the reader in every case
# below) may edit it through the modal door. The list face is here as a shape
# even though its own page is the list editor's: "the UI never points this link
# at it" is not a guard. The not-local row is the one the browser cannot show —
# no real page pairs "this is your row" with "this is not local" — pinned
# because the clause is load-bearing on the predicate.
SHAPES = {
    "your own review": True,
    "your own rating-only row": True,
    "another member's review": False,
    "your own reply": False,
    "a list's face": False,
    "a mirror of another instance's review": False,
    "your own review that is not local": False,
    "your own deleted review": False,
}

# The shapes with a page a browser can actually stand on. The deleted row's page
# 404s before any context is built, so its refusal needs no render twin — the
# same division I1 drew for its own tombstone clause.
RENDERABLE = {
    shape: expected
    for shape, expected in SHAPES.items()
    if shape != "your own deleted review"
}


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return member(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


def _body(client: Client, url: str) -> str:
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.content.decode()


def _review(user, film, **extra):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="4.5",
        content="<p>Spice must be reviewed.</p>",
        raw_content="Spice must be reviewed.",
        **extra,
    )


def _rating_only(user, film):
    return Status.objects.create(
        user=user, film=film, status_type=Status.Type.REVIEW_RATING, rating="4.00"
    )


def _shape_row(shape, alice, bob, dune):
    """The row, and the page on which this reader is ever offered it."""
    if shape == "your own review":
        row = _review(alice, dune)
        return row, f"/status/{row.pk}/"
    if shape == "your own rating-only row":
        row = _rating_only(alice, dune)
        return row, f"/status/{row.pk}/"
    if shape == "another member's review":
        row = _review(bob, dune)
        return row, f"/status/{row.pk}/"
    if shape == "your own reply":
        post = _review(bob, dune)
        row = add_reply(alice, post, content="<p>Agreed.</p>", raw_content="Agreed.")
        return row, f"/status/{post.pk}/"
    if shape == "a list's face":
        listing = create_list(alice, title="Cold War Sci-Fi", films=[dune])
        return listing.status, f"/list/{listing.pk}/"
    if shape == "a mirror of another instance's review":
        carol = User(
            localname="carol@remote.example",
            local=False,
            actor_url="https://remote.example/users/carol",
            inbox_url="https://remote.example/users/carol/inbox",
        )
        carol.set_unusable_password()
        carol.save()
        row = _review(
            carol,
            dune,
            local=False,
            remote_url="https://remote.example/status/carol-1",
        )
        return row, f"/status/{row.pk}/"
    if shape == "your own review that is not local":
        row = _review(
            alice,
            dune,
            local=False,
            remote_url="https://remote.example/status/mine-not-mine",
        )
        return row, f"/status/{row.pk}/"
    row = _review(alice, dune)
    row.delete()
    return row, None


def _edit_links(body: str) -> set[str]:
    """Every href this render offers as the review Edit link."""
    return set(re.findall(r'class="review-edit-link" href="([^"]+)"', body))


def _modal_attrs(body: str) -> str:
    """The attribute string of the film page's modal div. Raises if absent."""
    match = re.search(r'<div class="modal" id="mark-watched-modal"([^>]*)>', body)
    assert match is not None, "the film page shipped no modal at all"
    return match.group(1)


def _modal_open(client: Client, url: str) -> bool:
    return "hidden" not in _modal_attrs(_body(client, url))


# --- the predicate -----------------------------------------------------------


@pytest.mark.parametrize("shape,expected", SHAPES.items())
@pytest.mark.django_db
def test_may_edit_review_answers_every_shape(shape, expected, alice, bob, dune):
    row, _ = _shape_row(shape, alice, bob, dune)
    assert may_edit_review(alice, row) is expected, shape


@pytest.mark.django_db
def test_anonymous_reader_gets_no_door(alice, dune):
    from django.contrib.auth.models import AnonymousUser

    row = _review(alice, dune)
    assert may_edit_review(AnonymousUser(), row) is False


# --- the post page's link ----------------------------------------------------


@pytest.mark.parametrize("shape,expected", RENDERABLE.items())
@pytest.mark.django_db
def test_the_link_renders_exactly_where_the_door_exists(
    shape, expected, alice, bob, dune
):
    row, page = _shape_row(shape, alice, bob, dune)
    links = _edit_links(_body(_login("alice"), page))
    if expected:
        # The one honest href: the film's own page, with the param that opens
        # the modal. A link to anything else is a different door.
        assert links == {f"/film/{row.film_id}/?edit-review=1"}, shape
    else:
        assert links == set(), shape


@pytest.mark.django_db
def test_a_stranger_gets_no_link(alice, dune):
    row = _review(alice, dune)
    assert _edit_links(_body(Client(), f"/status/{row.pk}/")) == set()


@pytest.mark.django_db
def test_the_two_doors_never_both_open_on_one_row(alice, bob, dune):
    # A reply's door is the inline editor I2 built, a review's is this link, and
    # the set where both are offered on one page must stay empty: two editors
    # for one field is the second writer ruling 1 exists to prevent.
    post = _review(bob, dune)
    add_reply(alice, post, content="<p>Agreed.</p>", raw_content="Agreed.")
    body = _body(_login("alice"), f"/status/{post.pk}/")
    assert 'class="reply-edit-link"' in body
    assert _edit_links(body) == set()


@pytest.mark.django_db
def test_the_link_sits_beside_the_delete(alice, dune):
    # Ruling 1's placement: the Edit reads before the Delete in the action row,
    # matching how reply rows order the pair. Page order is the row's order.
    row = _review(alice, dune)
    body = _body(_login("alice"), f"/status/{row.pk}/")
    edit_at = body.index('class="review-edit-link"')
    delete_at = body.index('class="inline status-delete"')
    assert 0 < edit_at < delete_at


# --- the film page's open modal -----------------------------------------------


@pytest.mark.django_db
def test_the_param_opens_the_modal_only_for_its_own_review(alice, bob, dune):
    _review(alice, dune)
    # Alice, with the param: open.
    assert _modal_open(_login("alice"), f"/film/{dune.pk}/?edit-review=1")
    # Alice, without it: the page arrives as it always did, modal shut.
    assert not _modal_open(_login("alice"), f"/film/{dune.pk}/")
    # Bob, with the param: shut. The param alone is not enough, because opening
    # here would offer him the blank mark-watched form under an edit promise.
    assert not _modal_open(_login("bob"), f"/film/{dune.pk}/?edit-review=1")


@pytest.mark.django_db
def test_the_param_on_a_film_you_never_finished_keeps_the_modal_shut(alice, dune):
    # Alice is authenticated, so the modal renders for her — but she has no
    # review here, so the param finds nothing to edit and the modal stays shut.
    assert not _modal_open(_login("alice"), f"/film/{dune.pk}/?edit-review=1")


@pytest.mark.django_db
def test_a_stranger_gets_no_modal_at_all(alice, dune):
    # The modal is member chrome; the param cannot mint it for a stranger.
    body = _body(Client(), f"/film/{dune.pk}/?edit-review=1")
    assert 'id="mark-watched-modal"' not in body


@pytest.mark.django_db
def test_an_open_modal_is_still_the_prefilled_one(alice, dune):
    _review(alice, dune)
    body = _body(_login("alice"), f"/film/{dune.pk}/?edit-review=1")
    assert "hidden" not in _modal_attrs(body)
    # The same modal, not a second one: rating and raw markdown still ride in.
    assert 'data-value="4.5"' in body
    assert ">Spice must be reviewed.</textarea>" in body


@pytest.mark.django_db
def test_a_list_rename_still_owns_its_own_door(alice, dune):
    # The face's refusal is not the increment breaking the list's editor: the
    # rename stamps the face (lists increment 6) and may_edit_review still says
    # no, because the list editor is the one writer of both halves.
    listing = create_list(alice, title="Cold War Sci-Fi", films=[dune])
    rename(listing, "Cold War Cinema")
    listing.refresh_from_db()
    assert listing.status.edited_date is not None
    assert may_edit_review(alice, listing.status) is False
