"""The read-only list surface (§2K increment 2, R137/R138).

Four groups, in the order they bite:

* **The URL shape.** ``/user/<localname>/lists/`` and not ``/lists/``
  (R138 decision 1), on the same ``re_path`` + ``PROFILE_LOCALNAME_RE``
  the films page uses — which is the only reason a mirror handle
  (``user@host``) resolves there at all.
* **The list page.** Ranked rows, the printed ordinal coming from the order
  rather than from ``rank``, the like control naming the **status** and not
  the list, no pager, and the three hide rules copied from the post page.
* **The redirect, the nav and the tab.** ``/status/<id>/`` → ``/list/<id>/``
  for a LIST face and for nothing else, with the AP arm still answering
  first; the nav item always there; the profile tab hidden when the member
  has made no lists (R138 #3).
* **L13, the reply.** Replying to a list writes an **untyped** reply, and
  — the half that actually matters — replying to an ordinary film comment
  still types it ``comment`` exactly as it was typed before this increment
  existed. A suite that covered only the list case would pass just as well if
  the film case had been broken on the way through.

Absence assertions go against the specific element rather than the page body
wherever the page also carries other members' markup, so a "not present"
check cannot pass vacuously by having read a different row.
"""

import json
import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from reeltalk.core.models import Film, Status
from reeltalk.lists.models import FilmList, ListSave
from reeltalk.lists.services import (
    create_list,
    remove_film,
    soft_delete_list,
)
from reeltalk.notifications.models import Notification
from reeltalk.tests.members import member, site_admin

User = get_user_model()


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return member(localname="bob", password="s3cretpass")


@pytest.fixture
def root(db):
    """R12: ``/`` answers an empty 302 to the setup wizard until a
    superuser exists, so a test that reads the nav off the home page needs
    one present or it asserts against a redirect."""
    return site_admin(localname="root", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def alien(db):
    return Film.objects.create(title="Alien", year=1979)


@pytest.fixture
def blade(db):
    return Film.objects.create(title="Blade Runner", year=1982)


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


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


def _body(response) -> str:
    assert response.status_code == 200
    return response.content.decode()


def _rows(body: str) -> list[str]:
    """Each ranked row's own markup, so a count or an order check cannot
    read a neighbouring row and pass vacuously.

    Matched as a block rather than line-by-line because the row's film link
    sits on its own line inside the ``<li>``.
    """
    return re.findall(r'<li class="rank-row">.*?</li>', body, re.S)


def _tabs(body: str) -> str:
    """The page's own ``.tabs`` nav, and nothing else.

    Required rather than convenient: the header nav carries a "My Lists"
    item on every authenticated page, so an unscoped ``">My Lists</a>"
    not in body`` check would fail on the header while saying nothing about
    the tab row it means to test.
    """
    match = re.search(r'<nav class="tabs">.*?</nav>', body, re.S)
    return match.group(0) if match else ""


# --- the URL shape (R138 decision 1) ---------------------------------------


@pytest.mark.django_db
def test_the_lists_url_is_user_scoped(alice):
    """``/user/<localname>/lists/``, mirroring ``/user/<localname>/films/``."""
    assert reverse("user-lists", args=["alice"]) == "/user/alice/lists/"


@pytest.mark.django_db
def test_there_is_no_top_level_lists_page():
    """The rejected shape stays rejected: nothing answers ``/lists/``."""
    assert Client().get("/lists/").status_code == 404


@pytest.mark.django_db
def test_a_mirror_handle_resolves_on_the_lists_url():
    """The reason this is a ``re_path`` over ``PROFILE_LOCALNAME_RE``.

    That character class is what admits ``@`` and ``:``, so a mirror's full
    ``user@host`` localname matches the route. A plain ``path()`` would have
    matched only a single path segment and every remote member's lists page
    would 404 before the view ever ran — which is why this test asserts a
    200 rather than trusting that the pattern was copied correctly.
    """
    _remote_user("carol@remote.example")
    response = Client().get("/user/carol@remote.example/lists/")
    assert response.status_code == 200
    assert "No lists yet." in response.content.decode()


@pytest.mark.django_db
def test_an_unknown_member_has_no_lists_page():
    assert Client().get("/user/nobody/lists/").status_code == 404


# --- the list page ----------------------------------------------------------


@pytest.mark.django_db
def test_the_list_page_renders_its_head_and_ranked_rows(alice, dune, alien, blade):
    film_list = create_list(
        alice,
        title="Best Sci-Fi of the 50s",
        description="Cold-blooded and **always** the villain.",
        films=[dune, alien, blade],
    )
    body = _body(Client().get(f"/list/{film_list.pk}/"))
    assert "Best Sci-Fi of the 50s" in body
    assert "Cold-blooded and <strong>always</strong> the villain." in body
    assert "alice" in body
    rows = _rows(body)
    assert len(rows) == 3
    # The order on the page is the rank order, not title order or id order.
    assert "Dune" in rows[0]
    assert "Alien" in rows[1]
    assert "Blade Runner" in rows[2]
    # L8: the row is poster + title + year and nothing more — no rating,
    # no per-film note, no watch state.
    assert "stars" not in rows[0]
    assert "rating" not in rows[0]


@pytest.mark.django_db
def test_the_printed_position_is_the_order_not_the_rank(alice, dune, alien, blade):
    """``rank`` is an ordering key; the page numbers rows by position.

    ``remove_film`` leaves the gap it makes, so the middle film is gone and
    the ranks read 1 and 3. The page must print 1 and 2 — printing ``rank``
    would make the numbering visibly skip over a film that is not there.
    """
    film_list = create_list(alice, title="Sci-Fi", films=[dune, alien, blade])
    remove_film(film_list, alien)
    assert [item.rank for item in film_list.items.all()] == [1, 3]
    rows = _rows(_body(Client().get(f"/list/{film_list.pk}/")))
    assert len(rows) == 2
    assert '<span class="rank-num">1</span>' in rows[0]
    assert '<span class="rank-num">2</span>' in rows[1]
    assert '<span class="rank-num">3</span>' not in "".join(rows)


@pytest.mark.django_db
def test_the_like_control_names_the_status_not_the_list(alice, bob, dune):
    """L9/L10: what gets applauded is the post face, so the endpoint is the
    existing status one and a list needs no like route of its own."""
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    body = _body(_login("bob").get(f"/list/{film_list.pk}/"))
    assert f'data-url="/status/{film_list.status_id}/like/"' in body
    assert f"/list/{film_list.pk}/like/" not in body


@pytest.mark.django_db
def test_the_reply_control_posts_to_the_faces_reply_route(alice, bob, dune):
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    body = _body(_login("bob").get(f"/list/{film_list.pk}/?reply=1"))
    assert f'action="/status/{film_list.status_id}/reply/"' in body


@pytest.mark.django_db
def test_the_list_page_renders_its_thread(alice, bob, dune):
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    _login("bob").post(
        f"/status/{film_list.status_id}/reply/", {"content": "Great list."}
    )
    body = _body(Client().get(f"/list/{film_list.pk}/"))
    assert "Replies (1)" in body
    assert "Great list." in body
    # The reply row is the post page's own partial, not a list-specific copy.
    assert 'class="review reply-row"' in body


@pytest.mark.django_db
def test_the_reply_row_does_not_wear_the_reply_controls_class(alice, bob, dune):
    """The class collision that laid every reply's text beside its date.

    ``.reply`` is the reply *control's* class and it is ``inline-flex``, so
    while the thread row carried it too the row laid its head and its body
    side by side. Pinned here by the exact class string because no Python
    assertion can see the computed layout — the geometry was measured in a
    browser (head right edge 531 against body left edge 537 on three
    different pages), and this is what keeps it from coming back.
    """
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    _login("bob").post(
        f"/status/{film_list.status_id}/reply/", {"content": "Layout check."}
    )
    body = _body(Client().get(f"/list/{film_list.pk}/"))
    assert 'class="review reply-row"' in body
    assert 'class="review reply"' not in body


@pytest.mark.django_db
def test_an_anonymous_reader_gets_the_counts_and_no_controls(alice, bob, dune):
    """Public per L4; the offer needs an account, the number does not (R85)."""
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    _login("bob").post(f"/status/{film_list.status_id}/like/")
    body = _body(Client().get(f"/list/{film_list.pk}/"))
    assert "Applauded 1 time" in body
    assert "applaud-btn" not in body
    assert "reply-btn" not in body
    assert "reply-form" not in body


@pytest.mark.django_db
def test_a_member_gets_both_controls_on_the_list_page(alice, bob, dune):
    """The other half of the gate: a list's face has no film and neither
    control is withheld for it, which is the L13 change made visible."""
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    body = _body(_login("bob").get(f"/list/{film_list.pk}/"))
    assert "applaud-btn" in body
    assert "reply-btn" in body


@pytest.mark.django_db
def test_no_pager_on_the_list_page(alice):
    """R138 decision 2: the ranking is the content and a list reads top to
    bottom. Asserted over more rows than any page size on this site so a
    pager could not hide inside the count."""
    films = [
        Film.objects.create(title=f"Film {i:02d}", year=1970 + i) for i in range(60)
    ]
    film_list = create_list(alice, title="Sixty films", films=films)
    body = _body(Client().get(f"/list/{film_list.pk}/"))
    assert len(_rows(body)) == 60
    assert 'class="pager"' not in body
    assert "?page=" not in body


# --- the hide rules, copied from the post page ------------------------------


@pytest.mark.django_db
def test_a_soft_deleted_list_404s_on_its_page(alice):
    """``FilmList.delete()`` is soft, so the row is still in the table and
    only the ``deleted=False`` filter hides it."""
    film_list = create_list(alice, title="Doomed")
    soft_delete_list(film_list)
    assert FilmList.objects.filter(pk=film_list.pk).exists()
    assert Client().get(f"/list/{film_list.pk}/").status_code == 404


@pytest.mark.django_db
def test_a_suspended_authors_list_404s(alice, dune):
    """R102, and the same rule the post page applies to the same row."""
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    alice.suspended_at = timezone.now()
    alice.save(update_fields=["suspended_at"])
    assert Client().get(f"/list/{film_list.pk}/").status_code == 404


@pytest.mark.django_db
def test_a_blocked_authors_list_404s_rather_than_redirecting(alice, bob, dune):
    """A redirect here would lead the viewer somewhere naming the person they
    blocked, so the 404 has to win — which is why the redirect in
    ``status_detail`` sits below the block check rather than above it."""
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    bob.blocks.add(alice)
    assert _login("bob").get(f"/list/{film_list.pk}/").status_code == 404


# --- the redirect (L10) ----------------------------------------------------


@pytest.mark.django_db
def test_a_list_status_page_redirects_to_the_list(alice, dune):
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    response = Client().get(f"/status/{film_list.status_id}/")
    assert response.status_code == 302
    assert response.headers["location"] == f"/list/{film_list.pk}/"


@pytest.mark.django_db
def test_a_review_status_page_does_not_redirect(alice, dune):
    """The control case: the redirect is keyed on LIST and nothing else, so
    the ordinary post page keeps its own URL."""
    review = Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="4.5",
        content="<p>Spice must be reviewed.</p>",
    )
    response = Client().get(f"/status/{review.pk}/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_the_activitypub_arm_still_answers_for_a_list_status(alice, dune):
    """The redirect sits **below** the content-negotiation split on purpose.

    ``/status/<id>/`` is the list's wire identity: a peer that re-fetches
    the Note it already holds must find a document there, not get bounced to
    an HTML page it cannot parse. Redirecting above the AP arm would break
    increment 6 from the inside without failing anything else.
    """
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    response = Client().get(
        f"/status/{film_list.status_id}/",
        HTTP_ACCEPT="application/activity+json",
    )
    assert response.status_code == 200
    assert response["Content-Type"] == "application/activity+json"
    assert json.loads(response.content.decode())["type"] == "Note"


@pytest.mark.django_db
def test_a_deleted_list_does_not_redirect_from_its_status(alice):
    """Both halves are tombstones, so the status 404s rather than redirecting
    to a list page that also 404s — one answer, and it is the true one."""
    film_list = create_list(alice, title="Doomed")
    soft_delete_list(film_list)
    assert Client().get(f"/status/{film_list.status_id}/").status_code == 404


# --- the lists page --------------------------------------------------------


@pytest.mark.django_db
def test_the_lists_page_shows_only_lists_that_member_made(alice, bob, dune):
    """The default tab is what you made. A save row must not put somebody
    else's list on it — the saved ones live behind their own tab."""
    create_list(alice, title="Made by alice", films=[dune])
    theirs = create_list(bob, title="Made by bob", films=[dune])
    ListSave.objects.create(user=alice, film_list=theirs)
    body = _body(_login("alice").get("/user/alice/lists/"))
    assert "Made by alice" in body
    assert "Made by bob" not in body


@pytest.mark.django_db
def test_the_lists_page_excludes_a_soft_deleted_list(alice):
    create_list(alice, title="Still here")
    doomed = create_list(alice, title="Doomed")
    soft_delete_list(doomed)
    body = _body(_login("alice").get("/user/alice/lists/"))
    assert "Still here" in body
    assert "Doomed" not in body


@pytest.mark.django_db
def test_the_lists_page_card_reports_the_film_count(alice, dune, alien):
    film_list = create_list(alice, title="Two films", films=[dune, alien])
    body = _body(_login("alice").get("/user/alice/lists/"))
    card = next(line for line in body.split("\n") if "list-card-meta" in line)
    assert "2 films" in card
    assert str(film_list.pk) in body


# --- the two tabs on the lists page (owner decision, 2026-10-06) ----------


@pytest.mark.django_db
def test_your_own_lists_page_carries_both_tabs(alice):
    tabs = _tabs(_body(_login("alice").get("/user/alice/lists/")))
    assert ">My Lists</a>" in tabs
    assert ">Saved Lists</a>" in tabs
    assert '<a href="/user/alice/lists/" class="active">My Lists</a>' in tabs


@pytest.mark.django_db
def test_the_saved_tab_renders_its_empty_state(alice):
    """The save button is increment 5; the tab and its empty sentence are
    here now so the shape under review is the real page rather than a stub."""
    body = _body(_login("alice").get("/user/alice/lists/?tab=saved"))
    assert "You haven't saved any lists yet." in body
    assert '<a href="?tab=saved" class="active">Saved Lists</a>' in body


@pytest.mark.django_db
def test_the_saved_tab_lists_what_you_saved_and_names_the_maker(alice, bob, dune):
    """L2: a saved card names the **maker**, never the saver — the card is a
    pointer at somebody else's list and says whose it is."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    ListSave.objects.create(user=alice, film_list=theirs)
    body = _body(_login("alice").get("/user/alice/lists/?tab=saved"))
    assert "Bob Picks" in body
    assert "saved from bob" in body
    assert f'href="/list/{theirs.pk}/"' in body
    # And it is on the Saved tab only, not the made tab.
    made_body = _body(_login("alice").get("/user/alice/lists/"))
    assert "Bob Picks" not in made_body


@pytest.mark.django_db
def test_the_saved_tab_no_longer_drops_a_list_the_maker_deleted(alice, bob, dune):
    """R140 1 **reverses** the rule this test used to pin.

    Through increment 2 the Saved tab filtered ``film_list__deleted=False``,
    so a deleted list vanished from the saver's view with no explanation. The
    owner ruled against that: the list must still surface, carrying a notice
    the saver can dismiss. The old assertion is kept here inverted rather than
    deleted, because the reversal is the thing worth pinning — a future
    "cleanup" that reinstates the filter would look like the old correct
    behaviour and would silently undo an owner decision.

    The full notice/dismiss surface is in ``test_lists_saving.py``.
    """
    theirs = create_list(bob, title="Doomed list", films=[dune])
    ListSave.objects.create(user=alice, film_list=theirs)
    assert "Doomed list" in _body(_login("alice").get("/user/alice/lists/?tab=saved"))
    soft_delete_list(theirs)
    body = _body(_login("alice").get("/user/alice/lists/?tab=saved"))
    assert "Doomed list" in body
    assert "You haven't saved any lists yet." not in body


@pytest.mark.django_db
def test_somebody_elses_lists_page_has_no_tabs(alice, bob, dune):
    """No Saved tab on another member's page — and no lone "My Lists" tab
    over their lists either. One thing to show means no tab bar. Scoped to
    the ``.tabs`` nav because the header nav legitimately says "My Lists"
    on every page a member is signed in to."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    ListSave.objects.create(user=bob, film_list=theirs)
    body = _body(_login("alice").get("/user/bob/lists/"))
    assert _tabs(body) == ""
    assert "Bob Picks" in body


@pytest.mark.django_db
def test_the_saved_param_is_ignored_on_somebody_elses_page(alice, bob, dune):
    """Asking for another member's saves must not deliver them. The param
    falls back to the made-lists view rather than 404-ing, the same way
    ``user_films`` treats an unrecognised tab — the difference between a
    page that quietly shows you the safe thing and one that tells an attacker
    which params exist."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    ListSave.objects.create(user=bob, film_list=theirs)
    body = _body(_login("alice").get("/user/bob/lists/?tab=saved"))
    assert "You haven't saved any lists yet." not in body
    assert "Bob Picks" in body
    assert "saved from" not in body


@pytest.mark.django_db
def test_an_anonymous_visitor_gets_no_saved_tab(alice):
    """Not self, so no tabs — including no Saved tab that would be empty for
    a visitor who cannot have saved anything."""
    create_list(alice, title="Alice Picks")
    body = _body(Client().get("/user/alice/lists/"))
    assert _tabs(body) == ""
    assert "Alice Picks" in body


@pytest.mark.django_db
def test_a_banned_members_lists_page_is_gone(alice):
    create_list(alice, title="Anything")
    alice.banned_at = timezone.now()
    alice.save(update_fields=["banned_at"])
    assert Client().get("/user/alice/lists/").status_code == 410


@pytest.mark.django_db
def test_a_suspended_members_lists_page_redirects_to_the_profile(alice):
    """The profile is where R102 says a suspended account explains itself,
    so the tab inherits that answer instead of 404-ing a second one."""
    create_list(alice, title="Anything")
    alice.suspended_at = timezone.now()
    alice.save(update_fields=["suspended_at"])
    response = Client().get("/user/alice/lists/")
    assert response.status_code == 302
    assert response.headers["location"] == "/user/alice/"


# --- the nav item and the profile tab --------------------------------------


@pytest.mark.django_db
def test_the_nav_item_shows_even_with_no_lists(alice, root):
    """The owner's call on 2026-10-06: the nav is the way *in*, not a
    report of what is behind it, so R138's hide rule stays on the profile
    tab only."""
    body = _body(_login("alice").get("/"))
    assert 'href="/user/alice/lists/"' in body


@pytest.mark.django_db
def test_the_nav_item_is_active_only_on_the_lists_page(alice):
    made = create_list(alice, title="Sci-Fi")
    lists_page = _body(_login("alice").get("/user/alice/lists/"))
    assert '<a href="/user/alice/lists/" class="active">My Lists</a>' in lists_page
    # A list's own page is not the lists page, so it does not light the item.
    list_page = _body(_login("alice").get(f"/list/{made.pk}/"))
    assert '<a href="/user/alice/lists/" class="active">' not in list_page


@pytest.mark.django_db
def test_the_lists_tab_is_hidden_when_the_member_has_no_lists(alice):
    """R138 decision 3, asserted on the tab and not the nav."""
    body = _body(Client().get("/user/alice/"))
    assert 'href="/user/alice/lists/"' not in body


@pytest.mark.django_db
def test_the_lists_tab_appears_when_the_member_has_a_list(alice):
    create_list(alice, title="Sci-Fi")
    body = _body(Client().get("/user/alice/"))
    assert 'href="/user/alice/lists/"' in body


@pytest.mark.django_db
def test_a_soft_deleted_list_does_not_keep_the_tab_up(alice):
    """The count is of live lists, because a deleted ``FilmList`` is still a
    row in the table."""
    made = create_list(alice, title="Sci-Fi")
    soft_delete_list(made)
    body = _body(Client().get("/user/alice/"))
    assert 'href="/user/alice/lists/"' not in body


# --- L13: the reply -------------------------------------------------------


@pytest.mark.django_db
def test_replying_to_a_list_writes_an_untyped_reply(alice, bob, dune):
    """The write L13 exists to unblock. Before it, this POST raised ``"A
    comment status must be anchored to a film"`` because ``add_reply``
    typed its row ``comment`` and had no film to copy off the face."""
    film_list = create_list(alice, title="Best Sci-Fi of the 50s", films=[dune])
    response = _login("bob").post(
        f"/status/{film_list.status_id}/reply/", {"content": "Where is film 3?"}
    )
    assert response.status_code == 200
    reply = Status.objects.get(reply_parent=film_list.status)
    assert reply.user == bob
    assert reply.status_type is None
    assert reply.film_id is None
    assert reply.raw_content == "Where is film 3?"


@pytest.mark.django_db
def test_replying_to_a_film_comment_still_types_the_reply_comment(alice, bob, dune):
    """The control the brief asks for by name.

    Same route, same helper, one changed expression — and this is the case
    that must not have moved. The reply to an ordinary film comment is still
    a ``comment`` carrying the parent's film, exactly as it was before the
    lists feature existed at all.
    """
    review = Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="4.5",
        content="<p>Spice must be reviewed.</p>",
    )
    comment = Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.COMMENT,
        content="<p>A film comment.</p>",
    )
    response = _login("bob").post(
        f"/status/{comment.pk}/reply/", {"content": "Replying to a comment."}
    )
    assert response.status_code == 200
    reply = Status.objects.get(reply_parent=comment)
    assert reply.status_type == Status.Type.COMMENT
    assert reply.film_id == dune.pk
    # And the review's own direct reply, from the same rule, likewise.
    _login("bob").post(f"/status/{review.pk}/reply/", {"content": "To the review."})
    review_reply = Status.objects.get(reply_parent=review)
    assert review_reply.status_type == Status.Type.COMMENT
    assert review_reply.film_id == dune.pk


@pytest.mark.django_db
def test_a_reply_to_a_list_stays_off_every_film_surface(alice, bob, dune):
    """The drop-out L13 chose is the wanted one: a conversation about a list
    must not turn up on the page for one of the films in it."""
    film_list = create_list(alice, title="Best Sci-Fi of the 50s", films=[dune])
    _login("bob").post(
        f"/status/{film_list.status_id}/reply/", {"content": "About the list."}
    )
    reply = Status.objects.get(reply_parent=film_list.status)
    assert not Status.objects.filter(film=dune, pk=reply.pk).exists()
    assert reply.pk not in list(
        Status.objects.filter(film_id__isnull=False).values_list("pk", flat=True)
    )
    # It reads where it belongs: on the list page's thread.
    assert "About the list." in _body(Client().get(f"/list/{film_list.pk}/"))


@pytest.mark.django_db
def test_a_reply_to_a_list_notifies_the_list_maker(alice, bob, dune):
    """L13 changes the label, not the threading, so the existing reply
    producer — which keys on the parent's author and never consults the
    type — still fires."""
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    _login("bob").post(
        f"/status/{film_list.status_id}/reply/", {"content": "Notifying anyway."}
    )
    note = Notification.objects.get(recipient=alice, actor=bob, kind="reply")
    assert note.status.reply_parent_id == film_list.status_id


@pytest.mark.django_db
def test_the_reply_count_on_the_list_page_counts_the_live_thread(alice, bob, dune):
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    _login("bob").post(f"/status/{film_list.status_id}/reply/", {"content": "One."})
    body = _body(_login("alice").get(f"/list/{film_list.pk}/"))
    assert 'class="reply-count" aria-live="polite">1<' in body
    assert "Replies (1)" in body
    # The tombstone half: deleting the reply takes it out of the count.
    reply = Status.objects.get(reply_parent=film_list.status)
    reply.delete()
    body = _body(_login("alice").get(f"/list/{film_list.pk}/"))
    assert "Replies (0)" in body
    assert 'class="reply-count" aria-live="polite">0<' in body
