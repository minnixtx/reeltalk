"""Saving a list (§2K increment 5: L2 / L6 / L12, R140).

Six groups, ordered by how quietly each one would fail.

* **The route's guards.** Login, POST-only, CSRF, and the three refusals
  copied from ``list_detail``'s hide rules — R85 says the route must refuse
  exactly what the page withholds, so a list nobody can open must nobody be
  able to save either.
* **Idempotency.** The unique constraint on ``(user, film_list)`` is a trap
  here, not a feature: a naive ``create()`` turns a double-click into a 500.
* **L6 silence.** The proof is the *ledger count*, not the absence of a
  display. A test that only checks "the Saved page shows no notification"
  passes happily if a producer exists and is merely not rendered, so every
  silence test here compares ``Notification.objects.count()`` across the
  action — and carries a control that proves the harness can see a
  notification when one really is produced.
* **The Save control on the list page**, including the two halves of the
  own-list rule agreeing: hidden on the page *and* refused by the route.
* **The notice and its dismissal (R140 1)**, including the trap this
  increment's title could walk straight into — a deleted card must not offer
  a link to a page that 404s — and the per-saver property.
* **L12 forward-compatibility**: a remote author's list saves through the
  same route, so nothing here may assume ``local=True``.

Absence assertions go against the specific element, never the page body,
wherever the page also carries other members' markup.
"""

import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from reeltalk.core.models import Film
from reeltalk.lists.models import FilmList, ListSave
from reeltalk.lists.services import create_list, soft_delete_list
from reeltalk.notifications.models import Notification
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


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


def _remote_user(localname: str) -> User:
    """A remote mirror account, exactly as federation creates it."""
    user = User(
        localname=localname,
        local=False,
        actor_url=f"https://remote.example/users/{localname.split('@')[0]}",
        inbox_url=f"https://remote.example/users/{localname.split('@')[0]}/inbox",
    )
    user.set_unusable_password()
    user.save()
    return user


def _saved_cards(body: str) -> list[str]:
    """Each Saved-tab card's own markup, so a "this card has no link" check
    cannot pass vacuously by having read a neighbouring card."""
    return re.findall(r'<li class="list-card.*?</li>', body, re.S)


def _cards_named(body: str, title: str) -> list[str]:
    """The cards whose markup names ``title``.

    Scoped to the card markup rather than the page because the page also
    carries the messages framework's output, and dismissing sets
    ``Dismissed "Bob Picks".`` — a body-wide ``not in`` check would read
    that flash message and report the card as still present. The messages
    render as bare ``<li>`` elements, so they never match this pattern."""
    return [c for c in _saved_cards(body) if title in c]


def _card_for(body: str, title: str) -> str:
    cards = _cards_named(body, title)
    assert len(cards) == 1, f"expected exactly one card named {title!r}"
    return cards[0]


# --- the route's guards ----------------------------------------------------


@pytest.mark.django_db
def test_saving_requires_login(bob, dune):
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    response = Client().post(f"/list/{theirs.pk}/save/")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/login/")
    assert not ListSave.objects.exists()


@pytest.mark.django_db
def test_saving_refuses_get(alice, bob, dune):
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    assert _login("alice").get(f"/list/{theirs.pk}/save/").status_code == 405


@pytest.mark.django_db
def test_saving_requires_csrf(alice, bob, dune):
    """CSRF is enforced by middleware, not by the view, so it has to be
    checked with a client that actually enforces it. The default test client
    silently disables the check, which would make this assertion pass with
    the protection removed entirely."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    strict = Client(enforce_csrf_checks=True)
    assert strict.login(username="alice", password="s3cretpass")
    assert strict.post(f"/list/{theirs.pk}/save/").status_code == 403
    assert not ListSave.objects.exists()


@pytest.mark.django_db
def test_a_deleted_list_cannot_be_saved(alice, bob, dune):
    """``/list/<id>/`` 404s on ``deleted=False``, so there is no page on
    which saving was ever offered. The route refuses the same way rather
    than letting a hand-made POST create a pointer at a list nobody can
    open."""
    doomed = create_list(bob, title="Doomed", films=[dune])
    soft_delete_list(doomed)
    assert _login("alice").post(f"/list/{doomed.pk}/save/").status_code == 404
    assert not ListSave.objects.exists()


@pytest.mark.django_db
def test_a_suspended_makers_list_refuses_the_save_route(alice, dune):
    maker = member(localname="maker", password="s3cretpass")
    theirs = create_list(maker, title="Suspender", films=[dune])
    maker.suspended_at = timezone.now()
    maker.save(update_fields=["suspended_at"])
    assert _login("alice").post(f"/list/{theirs.pk}/save/").status_code == 404
    assert not ListSave.objects.exists()


@pytest.mark.django_db
def test_a_blocked_makers_list_refuses_the_save_route(alice, dune):
    maker = member(localname="blockedguy", password="s3cretpass")
    theirs = create_list(maker, title="Blocked list", films=[dune])
    saver = _login("alice")
    User.objects.get(localname="alice").blocks.add(maker)
    assert saver.post(f"/list/{theirs.pk}/save/").status_code == 404
    assert not ListSave.objects.exists()


@pytest.mark.django_db
def test_you_cannot_save_your_own_list(alice, dune):
    """L2 frames a save as a pointer at *somebody else's* list. Nothing in
    the model forbids the self-pointer, so the rule is stated at the route
    and not only in the template — R85 is symmetric, and a control hidden
    from the owner's page is decoration unless the route refuses too."""
    mine = create_list(alice, title="My own", films=[dune])
    response = _login("alice").post(f"/list/{mine.pk}/save/")
    assert response.status_code == 400
    assert not ListSave.objects.filter(user__localname="alice").exists()


@pytest.mark.django_db
def test_you_cannot_unsave_your_own_list_either(alice, dune):
    """The pair refuses uniformly, so there is one rule to remember rather
    than one per half."""
    mine = create_list(alice, title="My own", films=[dune])
    assert _login("alice").post(f"/list/{mine.pk}/unsave/").status_code == 400


# --- idempotency ----------------------------------------------------------


@pytest.mark.django_db
def test_a_double_save_is_one_row_and_both_calls_succeed(alice, bob, dune):
    """The unique constraint is the trap: ``ListSave.objects.create()``
    twice raises ``IntegrityError`` and a double-click becomes a 500.
    ``get_or_create`` makes the second call a genuine no-op that still
    answers 200 with the same state."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    client = _login("alice")
    first = client.post(f"/list/{theirs.pk}/save/")
    second = client.post(f"/list/{theirs.pk}/save/")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {"saved": True}
    assert second.json() == {"saved": True}
    assert ListSave.objects.filter(user__localname="alice").count() == 1


@pytest.mark.django_db
def test_a_double_unsave_does_not_error(alice, bob, dune):
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    client = _login("alice")
    client.post(f"/list/{theirs.pk}/save/")
    assert client.post(f"/list/{theirs.pk}/unsave/").status_code == 200
    second = client.post(f"/list/{theirs.pk}/unsave/")
    assert second.status_code == 200
    assert second.json() == {"saved": False}
    assert not ListSave.objects.filter(user__localname="alice").exists()


@pytest.mark.django_db
def test_unsaving_a_list_you_never_saved_is_a_no_op(alice, bob, dune):
    """Not an error and not a 404: the delete matched nothing, which is a
    legitimate outcome of pressing the button."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    response = _login("alice").post(f"/list/{theirs.pk}/unsave/")
    assert response.status_code == 200
    assert response.json() == {"saved": False}


@pytest.mark.django_db
def test_two_members_saving_one_list_make_two_rows(alice, bob, dune):
    """The constraint is per-(saver, list), not per-list — one member's save
    must not block another's, and the control must not go missing for the
    second saver."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    third = member(localname="carol", password="s3cretpass")
    _login("alice").post(f"/list/{theirs.pk}/save/")
    _login("carol").post(f"/list/{theirs.pk}/save/")
    assert ListSave.objects.filter(film_list=theirs).count() == 2
    assert third.saved_lists.count() == 1


# --- L6: silence, proven by the ledger ------------------------------------


@pytest.mark.django_db
def test_saving_produces_no_notification_at_all(alice, bob, dune):
    """L6 is absolute. The proof is the count being **identical before and
    after**, not the Saved page displaying nothing — a producer that exists
    and is merely not rendered would pass a display check and fail this."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    client = _login("alice")
    before = Notification.objects.count()
    client.post(f"/list/{theirs.pk}/save/")
    after_save = Notification.objects.count()
    client.post(f"/list/{theirs.pk}/unsave/")
    after_unsave = Notification.objects.count()
    assert before == after_save == after_unsave


@pytest.mark.django_db
def test_saving_adds_nothing_to_a_live_notification_baseline(alice, bob, dune):
    """The non-vacuity control for the silence tests above.

    A baseline of exactly one notification is put on the ledger through a
    real route first. If the ledger were simply empty and stayed empty, the
    silence tests would pass even with the whole notifications app broken.
    Here the baseline is present and the save still adds nothing to it."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    client = _login("alice")
    client.post(f"/status/{theirs.status_id}/like/")
    baseline = Notification.objects.count()
    assert baseline == 1, "the control itself failed: a like should have notified"
    assert Notification.objects.filter(recipient=bob).count() == 1

    client.post(f"/list/{theirs.pk}/save/")
    assert Notification.objects.count() == baseline
    assert Notification.objects.filter(recipient=bob).count() == 1


@pytest.mark.django_db
def test_there_is_no_save_kind_on_the_notification_enum():
    """L6 forecloses the kind itself, not just its producers — a ``SAVE``
    member sitting on the enum invites somebody to produce it later."""
    assert "save" not in {k.value for k in Notification.Kind}
    assert "saved_list" not in {k.value for k in Notification.Kind}


@pytest.mark.django_db
def test_saving_does_not_edit_the_lists_post_face(alice, bob, dune):
    """A save is one member's bookkeeping about somebody else's list. It
    must not stamp ``Status.edited_date``, which is what increment 6 reports
    on the wire as ``editedTime`` — saving would otherwise broadcast an
    edit that never happened."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    face = theirs.status
    before = face.edited_date
    _login("alice").post(f"/list/{theirs.pk}/save/")
    face.refresh_from_db()
    assert face.edited_date == before


# --- the Save control on the list page ------------------------------------


@pytest.mark.django_db
def test_a_non_owner_is_offered_the_save_control(alice, bob, dune):
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    body = _login("alice").get(f"/list/{theirs.pk}/").content.decode()
    assert 'class="btn list-save-btn"' in body
    assert f'data-save-url="/list/{theirs.pk}/save/"' in body
    assert f'data-unsave-url="/list/{theirs.pk}/unsave/"' in body
    assert ">Save<" in body


@pytest.mark.django_db
def test_the_control_renders_in_its_saved_state_when_already_saved(alice, bob, dune):
    """The state comes from the server, never from the browser, so a reload
    cannot show a save that is not there or hide one that is."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    client = _login("alice")
    client.post(f"/list/{theirs.pk}/save/")
    body = client.get(f"/list/{theirs.pk}/").content.decode()
    assert ">Saved<" in body
    assert 'data-state="saved"' in body
    assert 'aria-pressed="true"' in body


@pytest.mark.django_db
def test_the_maker_is_not_offered_a_save_control(alice, dune):
    mine = create_list(alice, title="My own", films=[dune])
    body = _login("alice").get(f"/list/{mine.pk}/").content.decode()
    assert "list-save-btn" not in body
    # And the maker still gets their own control, so the absence above is
    # about the save and not about the whole control row failing to render.
    assert 'class="list-edit-link"' in body


@pytest.mark.django_db
def test_an_anonymous_visitor_is_not_offered_a_save_control(bob, dune):
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    body = Client().get(f"/list/{theirs.pk}/").content.decode()
    assert "list-save-btn" not in body
    # The page is still public and still shows the applause tally.
    assert "list-name" in body


# --- the notice and its dismissal (R140 1) --------------------------------


@pytest.mark.django_db
def test_a_deleted_saved_list_shows_a_notice(alice, bob, dune):
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    _login("alice").post(f"/list/{theirs.pk}/save/")
    soft_delete_list(theirs)
    body = _login("alice").get("/user/alice/lists/?tab=saved").content.decode()
    card = _card_for(body, "Bob Picks")
    assert "list-deleted-notice" in card
    assert "bob deleted this list." in card
    assert "Dismiss" in card


@pytest.mark.django_db
def test_a_deleted_saved_card_offers_no_link_to_its_404ing_page(alice, bob, dune):
    """THE trap this increment ships with.

    ``/list/<id>/`` 404s on a deleted list, so the card's existing
    ``<a class="list-card-name">`` would put a live-looking link on a
    destination that refuses — R85's offer-with-no-route bug, third
    appearance. The title becomes a plain span, asserted here by the
    absence of the anchor inside *this* card rather than inside the page."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    _login("alice").post(f"/list/{theirs.pk}/save/")
    soft_delete_list(theirs)
    body = _login("alice").get("/user/alice/lists/?tab=saved").content.decode()
    card = _card_for(body, "Bob Picks")
    assert f'href="/list/{theirs.pk}/"' not in card
    assert '<span class="list-card-name">Bob Picks</span>' in card


@pytest.mark.django_db
def test_a_live_saved_card_still_links_and_the_link_resolves(alice, bob, dune):
    """The control case for the trap above: the no-link rule is keyed on
    ``deleted`` and nothing else, so a live card keeps its working link."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    _login("alice").post(f"/list/{theirs.pk}/save/")
    body = _login("alice").get("/user/alice/lists/?tab=saved").content.decode()
    card = _card_for(body, "Bob Picks")
    assert f'<a class="list-card-name" href="/list/{theirs.pk}/">' in card
    assert "list-deleted-notice" not in card
    assert Client().get(f"/list/{theirs.pk}/").status_code == 200


@pytest.mark.django_db
def test_dismissing_takes_the_deleted_card_off_the_saved_tab(alice, bob, dune):
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    client = _login("alice")
    client.post(f"/list/{theirs.pk}/save/")
    soft_delete_list(theirs)
    assert "Bob Picks" in client.get("/user/alice/lists/?tab=saved").content.decode()
    response = client.post(f"/list/{theirs.pk}/save/dismiss/")
    assert response.status_code == 302
    assert response.headers["location"] == "/user/alice/lists/?tab=saved"
    after = client.get("/user/alice/lists/?tab=saved").content.decode()
    assert _cards_named(after, "Bob Picks") == []


@pytest.mark.django_db
def test_dismissing_marks_the_save_row_rather_than_deleting_it(alice, bob, dune):
    """Dismiss retires the *notice*, not the save. The row survives with its
    marker, which is the only reason the marker can be per-saver at all —
    and if the list were ever restored, the pointer is still there."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    client = _login("alice")
    client.post(f"/list/{theirs.pk}/save/")
    soft_delete_list(theirs)
    client.post(f"/list/{theirs.pk}/save/dismiss/")
    save = ListSave.objects.get(user__localname="alice", film_list=theirs)
    assert save.notice_dismissed_at is not None
    assert FilmList.objects.filter(pk=theirs.pk, deleted=True).exists()


@pytest.mark.django_db
def test_a_second_dismiss_does_not_move_the_timestamp(alice, bob, dune):
    """Idempotent in the way that matters for a timestamp: a second press
    must not restamp, or the marker stops meaning *when it was seen*."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    client = _login("alice")
    client.post(f"/list/{theirs.pk}/save/")
    soft_delete_list(theirs)
    client.post(f"/list/{theirs.pk}/save/dismiss/")
    first = ListSave.objects.get(
        user__localname="alice", film_list=theirs
    ).notice_dismissed_at
    second = client.post(f"/list/{theirs.pk}/save/dismiss/")
    assert second.status_code == 302
    assert (
        ListSave.objects.get(
            user__localname="alice", film_list=theirs
        ).notice_dismissed_at
        == first
    )


@pytest.mark.django_db
def test_dismissing_is_per_saver(alice, bob, dune):
    """One member dismissing says nothing about any other member's notice.
    This is why the marker is on ``ListSave`` and could live nowhere else —
    a marker on the ``FilmList`` would let alice silence bob's notice too."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    carol = member(localname="carol", password="s3cretpass")
    for who in ("alice", "carol"):
        client = _login(who)
        client.post(f"/list/{theirs.pk}/save/")
    soft_delete_list(theirs)
    _login("alice").post(f"/list/{theirs.pk}/save/dismiss/")

    alice_after = _login("alice").get("/user/alice/lists/?tab=saved").content.decode()
    assert _cards_named(alice_after, "Bob Picks") == []
    carol_body = _login("carol").get("/user/carol/lists/?tab=saved").content.decode()
    assert "Bob Picks" in carol_body
    assert "list-deleted-notice" in _card_for(carol_body, "Bob Picks")
    assert (
        ListSave.objects.get(user=carol, film_list=theirs).notice_dismissed_at is None
    )


@pytest.mark.django_db
def test_dismissing_requires_login(bob, dune):
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    response = Client().post(f"/list/{theirs.pk}/save/dismiss/")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/login/")


@pytest.mark.django_db
def test_dismissing_a_live_list_is_refused(alice, bob, dune):
    """There is no notice to dismiss on a live list. Refusing rather than
    quietly setting the marker keeps ``notice_dismissed_at`` meaning exactly
    one thing."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    client = _login("alice")
    client.post(f"/list/{theirs.pk}/save/")
    assert client.post(f"/list/{theirs.pk}/save/dismiss/").status_code == 404
    assert (
        ListSave.objects.get(
            user__localname="alice", film_list=theirs
        ).notice_dismissed_at
        is None
    )


@pytest.mark.django_db
def test_dismissing_a_list_you_never_saved_404s(alice, bob, dune):
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    soft_delete_list(theirs)
    assert _login("alice").post(f"/list/{theirs.pk}/save/dismiss/").status_code == 404


@pytest.mark.django_db
def test_dismissing_somebody_elses_save_does_not_touch_it(alice, bob, dune):
    """The lookup is scoped to the requesting member, so alice cannot set
    the marker on carol's pointer by posting the list id.

    The save has to be carol's rather than bob's: bob making the list and
    then saving it would be a self-save, which the route rightly refuses, and
    the test would be asserting against a row that was never created."""
    carol = member(localname="carol", password="s3cretpass")
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    _login("carol").post(f"/list/{theirs.pk}/save/")
    assert ListSave.objects.filter(user=carol, film_list=theirs).exists()
    soft_delete_list(theirs)
    assert _login("alice").post(f"/list/{theirs.pk}/save/dismiss/").status_code == 404
    assert (
        ListSave.objects.get(user=carol, film_list=theirs).notice_dismissed_at is None
    )


@pytest.mark.django_db
def test_the_empty_state_does_not_appear_beside_a_pending_notice(alice, bob, dune):
    """The empty state keys on the wrong thing if it means *no live lists*.
    A member with one undismissed notice must see the notice and not also be
    told they have saved nothing."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    client = _login("alice")
    client.post(f"/list/{theirs.pk}/save/")
    soft_delete_list(theirs)
    body = client.get("/user/alice/lists/?tab=saved").content.decode()
    assert "Bob Picks" in body
    assert "You haven't saved any lists yet." not in body


@pytest.mark.django_db
def test_the_empty_state_returns_once_the_notice_is_dismissed(alice, bob, dune):
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    client = _login("alice")
    client.post(f"/list/{theirs.pk}/save/")
    soft_delete_list(theirs)
    client.post(f"/list/{theirs.pk}/save/dismiss/")
    body = client.get("/user/alice/lists/?tab=saved").content.decode()
    assert "You haven't saved any lists yet." in body


# --- L12: a remote list saves through the same route -----------------------


@pytest.mark.django_db
def test_a_remote_members_list_is_savable(alice, dune):
    """L12, and the forward-compatibility gate for increment 7.

    Nothing on this path may assume ``local=True``: if the save route
    filtered on it, saving a mirror would be blocked from the inside out and
    increment 7 would inherit a dead path rather than an open one."""
    remote = _remote_user("carol@remote.example")
    theirs = create_list(remote, title="Remote Picks", films=[dune])
    response = _login("alice").post(f"/list/{theirs.pk}/save/")
    assert response.status_code == 200
    assert response.json() == {"saved": True}
    assert ListSave.objects.filter(user__localname="alice", film_list=theirs).exists()


@pytest.mark.django_db
def test_a_remote_lists_deleted_notice_reaches_the_saver_locally(alice, dune):
    """The notice works for a mirror with nothing remote involved: the
    deletion is local state on our mirror row, and the marker is on our own
    save row."""
    remote = _remote_user("carol@remote.example")
    theirs = create_list(remote, title="Remote Picks", films=[dune])
    client = _login("alice")
    client.post(f"/list/{theirs.pk}/save/")
    soft_delete_list(theirs)
    body = client.get("/user/alice/lists/?tab=saved").content.decode()
    assert "carol@remote.example deleted this list." in body
    client.post(f"/list/{theirs.pk}/save/dismiss/")
    after = client.get("/user/alice/lists/?tab=saved").content.decode()
    assert _cards_named(after, "Remote Picks") == []


# --- the reverse direction: saving must not disturb the made tab ----------


@pytest.mark.django_db
def test_saving_somebodys_list_does_not_put_it_on_the_made_tab(alice, bob, dune):
    """The Made tab is what you made. A save row must not leak onto it —
    re-pinned here because increment 5 touched this view's query."""
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    _login("alice").post(f"/list/{theirs.pk}/save/")
    body = _login("alice").get("/user/alice/lists/").content.decode()
    assert "Bob Picks" not in body


@pytest.mark.django_db
def test_the_saved_card_reports_the_lists_film_count(alice, bob, dune, alien):
    theirs = create_list(bob, title="Bob Picks", films=[dune, alien])
    _login("alice").post(f"/list/{theirs.pk}/save/")
    body = _login("alice").get("/user/alice/lists/?tab=saved").content.decode()
    card = _card_for(body, "Bob Picks")
    assert "2 films" in card
    assert "saved from bob" in card
