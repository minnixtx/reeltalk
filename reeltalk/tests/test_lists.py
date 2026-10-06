"""The list object and its write path (§2K increment 1, R137).

No view, no template, no wire — everything here is the model and the six
service functions. Four things get proven, in the order they bite:

* **The face.** Every list owns exactly one ``Status`` of type ``LIST`` with no
  film (L9), and that shape is legal only because the anchoring rule was
  narrowed for it.
* **The film rule kept its teeth.** The narrowing exempts ``LIST`` by name and
  nothing else. A film-less ``comment``, ``review`` or ``review_rating`` must
  still be refused — a test that only shows the exemption works proves nothing
  about the three types that were never meant to pass (L13).
* **Ranks.** Appends are dense and ordered; the swap is adjacent and stays
  inside its own list; the gap a removal leaves is documented, not accidental.
* **The merge.** ``ListItem`` is re-pointed by ``Film.merge_into``. Without
  that block a merge silently shortens every list that held the absorbed film.

The last test is a warning pinned to the suite rather than a feature: the moment
``Status.Type.LIST`` exists, a list row is already in every follower's
timeline, because ``Status.feed_for`` has never filtered on type. Increment 1
ships with no UI so nobody can make one; that is the reason for the ordering,
and the test is here so the ordering cannot be "optimised" away.
"""

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from reeltalk.core.models import Film, Status
from reeltalk.lists.models import FilmList, ListItem, ListSave
from reeltalk.lists.services import (
    MOVE_DOWN,
    MOVE_UP,
    add_films,
    create_list,
    move,
    remove_film,
    rename,
    soft_delete_list,
)
from reeltalk.tests.members import member

User = get_user_model()


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


# --- the object and its face -------------------------------------------------


@pytest.mark.django_db
def test_create_list_makes_a_list_owned_by_its_maker(alice):
    film_list = create_list(alice, title="Best Sci-Fi of the 50s")
    assert film_list.pk is not None
    assert film_list.user == alice
    assert film_list.deleted is False
    assert film_list.local is True
    # Day-one AP identity (R41), so increment 6 needs no re-shaping.
    assert film_list.origin_id == film_list.pk


@pytest.mark.django_db
def test_create_list_makes_the_post_face(alice):
    """L9: the list's social half is one ordinary Status row."""
    film_list = create_list(alice, title="Horror Films You Must See")
    face = film_list.status
    assert face.user == alice
    assert face.status_type == Status.Type.LIST
    # A list is not about one film, so the face has no film to carry.
    assert face.film_id is None
    # The face is a local status and mints its own origin identity.
    assert face.local is True
    assert face.origin_id == face.pk
    # One face per list, and the reverse accessor names it.
    assert face.film_list == film_list


@pytest.mark.django_db
def test_create_list_renders_the_description_and_keeps_the_source(alice):
    """The pair Film carries (R18): HTML for rendering, markdown for editing."""
    film_list = create_list(
        alice,
        title="Jack Palance's Top 5",
        description="Cold-blooded and **always** the villain.",
    )
    assert film_list.raw_description == "Cold-blooded and **always** the villain."
    assert (
        film_list.description
        == "<p>Cold-blooded and <strong>always</strong> the villain.</p>"
    )


@pytest.mark.django_db
def test_create_list_with_no_description_leaves_both_halves_blank(alice):
    """Empty stays empty rather than gaining an empty <p> element."""
    film_list = create_list(alice, title="Empty")
    assert film_list.description == ""
    assert film_list.raw_description == ""


@pytest.mark.django_db
def test_two_lists_may_share_a_title(alice, bob):
    """Duplicate titles are allowed, per-user or across users (open question 8).

    Nothing in the shape depends on a title being unique, so this pins the
    absence of a constraint that a later reader might assume is there.
    """
    first = create_list(alice, title="Favorites")
    second = create_list(bob, title="Favorites")
    assert first.pk != second.pk


# --- the film rule: the exemption and its teeth ------------------------------


@pytest.mark.django_db
def test_a_list_status_needs_no_film(alice):
    """The one exemption: LIST is the type the anchoring rule does not bind."""
    status = Status.objects.create(user=alice, status_type=Status.Type.LIST)
    assert status.film_id is None


@pytest.mark.django_db
def test_a_typeless_note_still_needs_no_film(alice):
    """The pre-existing exemption is untouched: null type = standalone note."""
    status = Status.objects.create(user=alice, content="Just a note.")
    assert status.status_type is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status_type",
    [
        Status.Type.COMMENT,
        Status.Type.REVIEW,
        Status.Type.REVIEW_RATING,
    ],
    ids=["comment", "review", "review_rating"],
)
def test_a_film_less_typed_status_still_raises(alice, status_type):
    """The narrowing must not have loosened the three types it never touched.

    Each carries a rating so ``REVIEW_RATING`` gets past its own check and
    actually reaches the anchoring rule — otherwise the rating-only case would
    pass for the wrong reason and the test would prove nothing about it.
    """
    with pytest.raises(ValueError, match="must be anchored to a film"):
        Status.objects.create(
            user=alice, status_type=status_type, content="text", rating="4.00"
        )


@pytest.mark.django_db
def test_the_three_exempted_shapes_are_the_only_ones(alice):
    """Exactly two shapes are film-less-legal: a null type, and LIST.

    Written as a sweep over every declared type so a fourth type added later
    cannot slip in unanchored without this failing.
    """
    legal = set()
    illegal = set()
    for choice in Status.Type:
        try:
            with transaction.atomic():
                Status.objects.create(
                    user=alice,
                    status_type=choice,
                    content="text",
                    rating="4.00",
                )
        except ValueError:
            illegal.add(choice)
        else:
            legal.add(choice)
    assert legal == {Status.Type.LIST}
    assert illegal == {
        Status.Type.COMMENT,
        Status.Type.REVIEW,
        Status.Type.REVIEW_RATING,
    }


# --- ranks -----------------------------------------------------------------


@pytest.mark.django_db
def test_add_films_appends_in_order_with_dense_ranks(alice, dune, alien, blade):
    film_list = create_list(alice, title="Sci-Fi")
    items = add_films(film_list, [dune, alien, blade])
    assert [item.rank for item in items] == [1, 2, 3]
    assert [item.film for item in items] == [dune, alien, blade]
    # The model's own ordering agrees with the ranks, not with insertion id.
    assert list(film_list.items.all()) == list(items)


@pytest.mark.django_db
def test_add_films_continues_from_the_highest_rank(alice, dune, alien, blade):
    """A second call appends after the first rather than restarting at 1."""
    film_list = create_list(alice, title="Sci-Fi", films=[dune, alien])
    more = add_films(film_list, [blade])
    assert [more[0].rank] == [3]
    assert [item.rank for item in film_list.items.all()] == [1, 2, 3]


@pytest.mark.django_db
def test_add_films_skips_a_film_already_in_the_list(alice, dune, alien):
    """Idempotent: the door can be pressed twice without an error (R19 shape)."""
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    assert add_films(film_list, [dune]) == []
    assert add_films(film_list, [dune, alien]) == [
        ListItem.objects.get(film_list=film_list, film=alien)
    ]
    assert film_list.items.count() == 2


@pytest.mark.django_db
def test_one_film_appears_once_per_list_at_the_database(alice, dune, alien):
    """The constraint itself, not just the service that avoids tripping it.

    The service skipping is a courtesy; this is the guarantee. Bypassing
    ``add_films`` and writing the row directly must still be refused.
    """
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ListItem.objects.create(film_list=film_list, film=dune, rank=99)


@pytest.mark.django_db
def test_the_same_film_may_sit_in_two_different_lists(alice, bob, dune):
    """Uniqueness is per list. Two members ranking the same film is normal."""
    mine = create_list(alice, title="Mine", films=[dune])
    theirs = create_list(bob, title="Theirs", films=[dune])
    assert mine.items.count() == 1
    assert theirs.items.count() == 1


@pytest.mark.django_db
def test_remove_film_reports_whether_it_was_there(alice, dune, alien):
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    assert remove_film(film_list, dune) is True
    assert remove_film(film_list, dune) is False
    # A film that was never in the list is also a clean False.
    assert remove_film(film_list, alien) is False


@pytest.mark.django_db
def test_remove_film_leaves_the_rank_gap_open(alice, dune, alien, blade):
    """Ranks order the rows; they are not the number a page prints.

    Pinned because a later "tidy up the gaps" change would look like an
    improvement and is not needed — and because a removal must not renumber
    every row below it, which is what closing the gap would cost.
    """
    film_list = create_list(alice, title="Sci-Fi", films=[dune, alien, blade])
    remove_film(film_list, alien)
    assert [item.rank for item in film_list.items.all()] == [1, 3]


# --- reorder ---------------------------------------------------------------


@pytest.mark.django_db
def test_move_up_swaps_with_the_item_above(alice, dune, alien, blade):
    film_list = create_list(alice, title="Sci-Fi", films=[dune, alien, blade])
    second = film_list.items.get(film=alien)
    assert move(second, MOVE_UP) is True
    assert [(item.film.title, item.rank) for item in film_list.items.all()] == [
        ("Alien", 1),
        ("Dune", 2),
        ("Blade Runner", 3),
    ]


@pytest.mark.django_db
def test_move_down_swaps_with_the_item_below(alice, dune, alien, blade):
    film_list = create_list(alice, title="Sci-Fi", films=[dune, alien, blade])
    first = film_list.items.get(film=dune)
    assert move(first, MOVE_DOWN) is True
    assert [(item.film.title, item.rank) for item in film_list.items.all()] == [
        ("Alien", 1),
        ("Dune", 2),
        ("Blade Runner", 3),
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("direction", [MOVE_UP, MOVE_DOWN], ids=["up", "down"])
def test_move_at_the_end_does_not_move(alice, dune, alien, direction):
    """Pressing up on the first row is a normal no-op, not an error."""
    film_list = create_list(alice, title="Sci-Fi", films=[dune, alien])
    first = film_list.items.get(film=dune)
    last = film_list.items.get(film=alien)
    edge = first if direction == MOVE_UP else last
    assert move(edge, direction) is False
    assert [(item.film.title, item.rank) for item in film_list.items.all()] == [
        ("Dune", 1),
        ("Alien", 2),
    ]


@pytest.mark.django_db
def test_move_down_never_crosses_into_another_list(alice, bob, dune, alien, blade):
    """The neighbour is found inside the item's own list, always.

    The other member's list is given rows at ranks 2 and 3 while ours sits
    alone at rank 1. A query that dropped the ``film_list`` filter would find
    one of those and move our row into somebody else's ordering; ours must
    report nothing below it and leave both lists exactly as they were.
    """
    mine = create_list(alice, title="Mine", films=[dune])
    theirs = create_list(bob, title="Theirs")
    ListItem.objects.create(film_list=theirs, film=alien, rank=2)
    ListItem.objects.create(film_list=theirs, film=blade, rank=3)
    item = mine.items.get(film=dune)
    assert move(item, MOVE_DOWN) is False
    item.refresh_from_db()
    assert item.rank == 1
    assert [(i.film.title, i.rank) for i in theirs.items.order_by("rank")] == [
        ("Alien", 2),
        ("Blade Runner", 3),
    ]


@pytest.mark.django_db
def test_move_up_never_crosses_into_another_list(alice, bob, dune, alien, blade):
    """The same trap from the other end, with the other list ranked below."""
    theirs = create_list(bob, title="Theirs", films=[alien, blade])
    mine = create_list(alice, title="Mine")
    ListItem.objects.create(film_list=mine, film=dune, rank=5)
    item = mine.items.get(film=dune)
    assert move(item, MOVE_UP) is False
    item.refresh_from_db()
    assert item.rank == 5
    assert [(i.film.title, i.rank) for i in theirs.items.order_by("rank")] == [
        ("Alien", 1),
        ("Blade Runner", 2),
    ]


@pytest.mark.django_db
def test_move_targets_the_neighbour_not_the_ordinal(alice, dune, alien, blade):
    """Across a gap the swap is with the next row by rank, not rank - 1.

    With ranks 1 / 3 / 5, moving the last one up must land it beside rank 3,
    not at a rank 4 that nothing occupies.
    """
    film_list = create_list(alice, title="Sci-Fi", films=[dune, alien, blade])
    remove_film(film_list, dune)
    last = film_list.items.get(film=blade)
    assert last.rank == 3
    assert move(last, MOVE_UP) is True
    assert [(item.film.title, item.rank) for item in film_list.items.all()] == [
        ("Blade Runner", 2),
        ("Alien", 3),
    ]


@pytest.mark.django_db
def test_move_with_an_unknown_direction_raises(alice, dune):
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    item = film_list.items.get()
    with pytest.raises(ValueError, match="Unknown move direction"):
        move(item, "sideways")


@pytest.mark.django_db
def test_a_single_item_list_cannot_move(alice, dune):
    film_list = create_list(alice, title="Solo", films=[dune])
    item = film_list.items.get()
    assert move(item, MOVE_UP) is False
    assert move(item, MOVE_DOWN) is False
    assert item.rank == 1


# --- rename ----------------------------------------------------------------


@pytest.mark.django_db
def test_rename_changes_the_title(alice):
    film_list = create_list(alice, title="Working title")
    rename(film_list, "Best Sci-Fi Films of the 50s")
    film_list.refresh_from_db()
    assert film_list.title == "Best Sci-Fi Films of the 50s"


# --- delete and cascade ----------------------------------------------------


@pytest.mark.django_db
def test_soft_delete_takes_down_the_list_and_its_face_together(alice):
    """Both halves, or the feed keeps a post that leads nowhere (R85 shape)."""
    film_list = create_list(alice, title="Doomed")
    face = film_list.status
    soft_delete_list(film_list)
    film_list.refresh_from_db()
    face.refresh_from_db()
    assert film_list.deleted is True
    assert film_list.deleted_date is not None
    assert face.deleted is True
    assert face.deleted_date is not None


@pytest.mark.django_db
def test_film_list_delete_is_soft(alice):
    """A bare ``.delete()`` cannot wipe the row, only mark it."""
    film_list = create_list(alice, title="Doomed")
    film_list.delete()
    assert FilmList.objects.filter(pk=film_list.pk).exists()
    assert FilmList.objects.get(pk=film_list.pk).deleted is True


@pytest.mark.django_db
def test_soft_delete_leaves_items_and_saves_in_place(alice, bob, dune):
    """The soft path hides rather than removes (see ``soft_delete_list``).

    Pinned so increment 5 cannot assume a ``ListSave`` row means a saveable
    list: it must filter on the list's own ``deleted`` flag.
    """
    film_list = create_list(alice, title="Doomed", films=[dune])
    ListSave.objects.create(user=bob, film_list=film_list)
    soft_delete_list(film_list)
    assert ListItem.objects.filter(film_list=film_list).count() == 1
    assert ListSave.objects.filter(film_list=film_list).count() == 1


@pytest.mark.django_db
def test_deleting_a_deleted_list_twice_keeps_the_first_deletion(alice):
    """The guard is idempotence, not decoration: the first deletion wins."""
    film_list = create_list(alice, title="Doomed")
    soft_delete_list(film_list)
    first_deleted_date = film_list.deleted_date
    face_deleted_date = film_list.status.deleted_date
    soft_delete_list(film_list)
    film_list.refresh_from_db()
    assert film_list.deleted_date == first_deleted_date
    assert film_list.status.deleted_date == face_deleted_date


@pytest.mark.django_db
def test_a_hard_delete_cascades_items_and_saves(alice, bob, dune):
    """L2 at the schema level: a pointer at nothing is not worth keeping.

    A queryset delete bypasses the soft-delete override, which is the only
    way to reach the CASCADE the model declares.
    """
    film_list = create_list(alice, title="Doomed", films=[dune])
    ListSave.objects.create(user=bob, film_list=film_list)
    FilmList.objects.filter(pk=film_list.pk).delete()
    assert ListItem.objects.filter(film_list=film_list).count() == 0
    assert ListSave.objects.filter(film_list=film_list).count() == 0


@pytest.mark.django_db
def test_saving_the_same_list_twice_is_one_row(alice, bob, dune):
    film_list = create_list(alice, title="Good list", films=[dune])
    ListSave.objects.create(user=bob, film_list=film_list)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ListSave.objects.create(user=bob, film_list=film_list)


@pytest.mark.django_db
def test_deleting_a_member_cascades_their_saves_but_not_their_lists(alice, bob, dune):
    """The two sides of the save pointer, in one test.

    ``ListSave.user`` is CASCADE — a member's saved-list rows go with them.
    ``FilmList.user`` is PROTECT — a member cannot be deleted out from under a
    list other people are pointing at, so the maker's account is not a
    cascade path for somebody else's save.
    """
    film_list = create_list(alice, title="Kept", films=[dune])
    ListSave.objects.create(user=bob, film_list=film_list)
    bob.delete()
    assert ListSave.objects.filter(film_list=film_list).count() == 0
    assert FilmList.objects.filter(pk=film_list.pk).exists()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            User.objects.filter(pk=alice.pk).delete()


# --- the film merge (§2K finding 1) ----------------------------------------


@pytest.mark.django_db
def test_merge_repoints_list_items_to_the_canonical(alice, dune, alien, blade):
    """Without ``_repoint_list_items`` this test fails by vanishing rows.

    The absorbed film is deleted, so a list item left pointing at it is gone
    with it — no error raised, just a shorter list. That is why this is here.
    """
    film_list = create_list(alice, title="Sci-Fi", films=[blade, alien])
    manual = Film.objects.create(title="Dune (manual)", year=2021)
    add_films(film_list, [manual])
    # Captured before the merge: a hard delete clears the pk off the Python
    # object, so it cannot be used in a filter afterwards.
    manual_id = manual.pk
    manual.merge_into(dune)
    assert [item.film.title for item in film_list.items.order_by("rank")] == [
        "Blade Runner",
        "Alien",
        "Dune",
    ]
    assert ListItem.objects.filter(film=manual_id).count() == 0


@pytest.mark.django_db
def test_merge_keeps_each_repointed_rank(alice, dune, alien, blade):
    """Re-pointing must not reshuffle: the member ordered this list."""
    film_list = create_list(alice, title="Sci-Fi", films=[alien, blade])
    manual = Film.objects.create(title="Dune (manual)", year=2021)
    add_films(film_list, [manual])
    manual.merge_into(dune)
    assert [(item.film.title, item.rank) for item in film_list.items.all()] == [
        ("Alien", 1),
        ("Blade Runner", 2),
        ("Dune", 3),
    ]


@pytest.mark.django_db
def test_merge_dedups_when_both_films_are_already_in_the_list(alice, dune, blade):
    """The canonical is already listed: the absorbed row drops out.

    Re-pointing blindly would hit ``one_row_per_film_per_list`` and abort the
    whole merge. Same resolution as a duplicate on a shelf.
    """
    film_list = create_list(alice, title="Sci-Fi", films=[dune, blade])
    manual = Film.objects.create(title="Dune (manual)", year=2021)
    add_films(film_list, [manual])
    manual.merge_into(dune)
    assert [(item.film.title, item.rank) for item in film_list.items.all()] == [
        ("Dune", 1),
        ("Blade Runner", 2),
    ]


@pytest.mark.django_db
def test_merge_repoints_items_across_two_lists(alice, bob, dune, blade):
    """Every list holding the absorbed film is re-pointed, whoever made it."""
    mine = create_list(alice, title="Mine", films=[blade])
    theirs = create_list(bob, title="Theirs", films=[blade])
    manual = Film.objects.create(title="Dune (manual)", year=2021)
    add_films(mine, [manual])
    add_films(theirs, [manual])
    manual.merge_into(dune)
    assert [item.film.title for item in mine.items.order_by("rank")] == [
        "Blade Runner",
        "Dune",
    ]
    assert [item.film.title for item in theirs.items.order_by("rank")] == [
        "Blade Runner",
        "Dune",
    ]


# --- the warning this increment ships with ---------------------------------


@pytest.mark.django_db
def test_a_list_row_is_already_in_the_follower_timeline(alice, bob):
    """``feed_for`` has never filtered on type, so LIST rows arrive for free.

    This is not a feature being claimed — it is the reason increment 1 ships
    with no UI and the increments must not be reordered (§2K). A list made
    today would already render in a follower's home feed as a film-less row;
    increment 4 is what makes that row look like a list. Pinning it here so
    the constraint is visible in the suite rather than only in the plan.
    """
    film_list = create_list(alice, title="Horror Films You Must See")
    bob.follows.add(alice)
    feed = list(Status.feed_for(bob))
    assert film_list.status in feed
    assert film_list.status.film_id is None
