"""Lists federation outbound (§2K increment 6, L1 / L11 / L13, R142).

Increment 6 is not purely additive. A list's post face was **already on the
wire** before this increment and arrived empty: the outbox filter carries no
``status_type`` exclusion, so every ``LIST`` status serialized as a
``Create(Note)`` with no ``content``, no title and nothing on it identifying
it as a list. These tests are written against that fact, not around it.

Seven groups, in the order they bite:

* **The body exists.** A list face's ``Note`` now carries human-readable
  ``content`` and the ``reeltalk:list`` extension, and the ``@context``
  declares the prefix -- without the declaration a strict JSON-LD processor
  drops the term rather than passing it through.
* **The widening touched nothing else.** An ordinary review's ``Note`` keeps
  its two-entry context and gains no extension key. Every "list" assertion
  here has a non-list twin, because a branch added to a shared serializer is
  exactly where an unrelated document type quietly changes shape.
* **The shape is importable, not merely displayable.** Every field a remote
  ``FilmList`` mirror needs is present and maps onto the existing inbound
  helpers, so increment 7 does not have to invent a mapping at the last
  minute.
* **Change detection.** A broadcast means the list changed. Re-adding a film
  that was already there, removing one that was not, moving a row at either
  end, and submitting the edit form untouched all send **nothing** -- the
  discipline ``_stamp_edited`` already follows, and the opposite of the
  unconditional shape ``mark_watched_view`` carries.
* **L6 silence.** Saving a list produces zero deliveries, with a
  non-vacuity control that proves the delivery path works at all.
* **The tombstone is composed from the surviving row.** ``Status.delete()``
  wipes ``content``; the ``FilmList`` does not, so a delete still names the
  list it deleted.
* **The repair command.** The Create-id collision the brief predicts is
  real, and ``rebroadcast_lists`` is the thing that answers it.

Absence assertions name the specific key rather than the whole document, and
delivery assertions read what ``responses`` captured rather than what the
builder would have produced.
"""

import json
from io import StringIO

import pytest
import responses
from django.core.management import call_command
from django.test import Client, RequestFactory

from reeltalk.activitypub.broadcast import broadcast_list_update
from reeltalk.activitypub.objects import (
    LIST_CONTENT_FILM_LIMIT,
    REELTALK_NS,
    create_activity,
    delete_activity,
    note_document,
    update_activity,
)
from reeltalk.activitypub.statuses import _remote_id_from_url
from reeltalk.core.models import Film, Status
from reeltalk.lists.models import FilmList, ListItem
from reeltalk.lists.services import (
    MOVE_DOWN,
    MOVE_UP,
    add_films,
    create_list,
    soft_delete_list,
)
from reeltalk.mentions.models import sync_status_mentions
from reeltalk.notifications.models import Notification
from reeltalk.social.models import User
from reeltalk.tests.members import member

REMOTE_ACTOR = "https://remote.example/user/carol/"
REMOTE_INBOX = REMOTE_ACTOR.rstrip("/") + "/inbox"
OTHER_REMOTE_ACTOR = "https://remote.example/user/dave/"
OTHER_REMOTE_INBOX = OTHER_REMOTE_ACTOR.rstrip("/") + "/inbox"


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return member(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021, tmdb_id=438631)


@pytest.fixture
def alien(db):
    return Film.objects.create(title="Alien", year=1979, imdb_id="tt0078748")


@pytest.fixture
def blade(db):
    return Film.objects.create(title="Blade Runner")


def _request():
    request = RequestFactory().get("/")
    request.META["HTTP_HOST"] = "testserver"
    return request


def _login(localname):
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


def _remote(localname, actor_url):
    """A remote mirror account, as federation creates it."""
    user = User(
        localname=localname,
        local=False,
        actor_url=actor_url,
        inbox_url=actor_url.rstrip("/") + "/inbox",
    )
    user.set_unusable_password()
    user.save()
    return user


def _carol():
    return _remote("carol@remote.example", REMOTE_ACTOR)


def _dave():
    return _remote("dave@remote.example", OTHER_REMOTE_ACTOR)


def _following(remote, target):
    """``remote`` follows ``target``, so ``target.followers`` holds ``remote``."""
    remote.follows.add(target)
    return remote


def _sent(index=0) -> dict:
    return json.loads(responses.calls[index].request.body)


def _urls():
    return [call.request.url for call in responses.calls]


def _films(count, prefix="Film"):
    return [
        Film.objects.create(title=f"{prefix} {i}", year=1980 + i) for i in range(count)
    ]


# --- the body exists -------------------------------------------------------


@pytest.mark.django_db
def test_a_list_face_note_now_carries_a_body(alice, dune):
    """The visible half of the bug: before increment 6 this Note had no
    ``content`` key at all, so a follower saw "minnix posted a Note" with
    nothing on it saying it was a list or what the list was called."""
    film_list = create_list(alice, title="Best Sci-Fi of the 50s", films=[dune])
    doc = note_document(film_list.status, _request())
    assert "content" in doc
    assert "Best Sci-Fi of the 50s" in doc["content"]
    assert "Dune" in doc["content"]


@pytest.mark.django_db
def test_a_list_note_declares_the_reeltalk_prefix_in_its_context(alice, dune):
    """A bare ``reeltalk:list`` key works with lenient peers and vanishes
    with strict ones. The prefix has to be declared, the same way ``toot``
    is on the Person document."""
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    doc = note_document(film_list.status, _request())
    assert {"reeltalk": REELTALK_NS} in doc["@context"]
    # The two standard entries are still there -- the extension widens the
    # context, it does not replace it.
    assert "https://www.w3.org/ns/activitystreams" in doc["@context"]


@pytest.mark.django_db
def test_the_note_id_stays_the_face_url_not_the_list_url(alice, dune):
    """L10 makes ``/list/<id>/`` canonical for humans, but the AP arm
    deliberately answers at ``/status/<id>/`` and increment 2 pinned that.
    The list's own identity goes inside the extension, never into the
    Note's ``id``."""
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    doc = note_document(film_list.status, _request())
    assert doc["id"] == f"http://testserver/status/{film_list.status_id}/"
    assert "/list/" not in doc["id"]


@pytest.mark.django_db
def test_the_list_url_rides_inside_the_extension(alice, dune):
    film_list = create_list(alice, title="Sci-Fi", films=[dune])
    doc = note_document(film_list.status, _request())
    assert doc["reeltalk:list"]["url"] == f"http://testserver/list/{film_list.pk}/"


# --- the widening touched nothing else -------------------------------------


@pytest.mark.django_db
def test_an_ordinary_review_note_gains_no_extension(alice, dune):
    """The control for every assertion above. A branch added to the shared
    ``note_document`` is exactly where a review would quietly pick up a
    list key or a widened context."""
    review = Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="4.5",
        content="<p>Spice, but make it ranked.</p>",
    )
    doc = note_document(review, _request())
    assert "reeltalk:list" not in doc
    assert doc["@context"] == [
        "https://www.w3.org/ns/activitystreams",
        "https://w3id.org/security/v1",
    ]


@pytest.mark.django_db
def test_a_rating_only_note_gains_no_extension(alice, dune):
    review = Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW_RATING,
        rating="3",
    )
    doc = note_document(review, _request())
    assert "reeltalk:list" not in doc
    assert {"reeltalk": REELTALK_NS} not in doc["@context"]


@pytest.mark.django_db
def test_a_list_face_with_no_backing_row_degrades_rather_than_500ing(alice):
    """A ``LIST`` status with no ``FilmList`` is reachable -- ``test_lists``
    creates one directly to exercise the film-anchoring carve-out -- and the
    outbox serializes every status of a user with no type filter. One
    malformed row must fall back to the plain Note, not take the whole
    outbox page down."""
    face = Status.objects.create(user=alice, status_type=Status.Type.LIST)
    doc = note_document(face, _request())
    assert doc["type"] == "Note"
    assert "reeltalk:list" not in doc
    assert "content" not in doc

    # And the outbox itself still answers, with the malformed row in it.
    response = Client().get(f"/user/{alice.localname}/outbox/?page=1")
    assert response.status_code == 200


# --- the shape is importable -----------------------------------------------


@pytest.mark.django_db
def test_extension_items_are_in_rank_order_not_creation_order(
    alice, dune, alien, blade
):
    """Rank is the ranking. Items appended in a different order than the
    ranks came out must still serialize in rank order, or a remote rebuilds
    the list wrong and the whole point of the extension is lost."""
    film_list = create_list(alice, title="Ranked", films=[dune, alien, blade])
    # Reverse the stored ranks so creation order and rank order disagree.
    for item in film_list.items.all():
        item.rank = 10 - item.rank
        item.save()
    doc = note_document(film_list.status, _request())
    titles = [i["name"] for i in doc["reeltalk:list"]["orderedItems"]]
    assert titles == ["Blade Runner", "Alien", "Dune"]
    ranks = [i["reeltalk:rank"] for i in doc["reeltalk:list"]["orderedItems"]]
    assert ranks == sorted(ranks)


@pytest.mark.django_db
def test_an_item_carries_the_same_film_identity_terms_a_review_uses(alice, dune, alien):
    """Film identity is ``film`` / ``tmdbId`` / ``imdbId`` -- the exact
    terms ``film_document`` and a review's ``Note`` already publish. One
    resolution path in the importer instead of two that can drift."""
    film_list = create_list(alice, title="Two Films", films=[dune, alien])
    items = note_document(film_list.status, _request())["reeltalk:list"]["orderedItems"]
    by_title = {i["name"]: i for i in items}
    assert by_title["Dune"]["tmdbId"] == 438631
    assert by_title["Alien"]["imdbId"] == "tt0078748"
    assert by_title["Dune"]["film"] == f"http://testserver/film/{dune.pk}/"


@pytest.mark.django_db
def test_a_film_without_identifiers_omits_them_rather_than_nulling(alice, blade):
    """``Blade Runner`` here has no tmdb and no imdb. Omitting matches
    ``film_document``; emitting ``None`` would be a third thing a peer has
    to special-case."""
    film_list = create_list(alice, title="One Bare Film", films=[blade])
    items = note_document(film_list.status, _request())["reeltalk:list"]["orderedItems"]
    assert "tmdbId" not in items[0]
    assert "imdbId" not in items[0]
    assert "year" not in items[0]
    assert items[0]["name"] == "Blade Runner"


@pytest.mark.django_db
def test_the_summary_is_the_rendered_description(alice, dune):
    film_list = create_list(alice, title="With Text", description="The *best* films.")
    ext = note_document(film_list.status, _request())["reeltalk:list"]
    assert "<em>best</em>" in ext["summary"]


@pytest.mark.django_db
def test_no_summary_key_when_the_list_has_no_description(alice, dune):
    film_list = create_list(alice, title="Terse")
    ext = note_document(film_list.status, _request())["reeltalk:list"]
    assert "summary" not in ext


@pytest.mark.django_db
def test_the_extension_maps_onto_a_mirror_row_without_invention(alice, dune, alien):
    """The increment 7 contract, asserted now rather than discovered then.

    Every field a remote ``FilmList`` needs comes off the extension using
    the helpers the inbound path already has -- ``_remote_id_from_url`` on
    the list URL gives the same integer a status mirror derives, and the
    film identity keys are the ones ``_film_for_reference`` already
    resolves. A shape that is merely displayable is not importable; this is
    the test that says which one we shipped."""
    film_list = create_list(
        alice, title="Importable", description="Proof of concept.", films=[dune, alien]
    )
    ext = note_document(film_list.status, _request())["reeltalk:list"]

    mirror = FilmList(
        user=alice,
        local=False,
        remote_url=ext["url"],
        remote_id=_remote_id_from_url(ext["url"]),
        title=ext["name"],
        description=ext.get("summary", ""),
    )
    assert mirror.remote_url == f"http://testserver/list/{film_list.pk}/"
    assert mirror.remote_id == film_list.pk
    assert mirror.title == "Importable"
    assert "Proof of concept." in mirror.description

    rows = [
        ListItem(
            film_list=mirror,
            rank=item["reeltalk:rank"],
            film_id=None,  # resolved by film URL / tmdbId through the existing path
        )
        for item in ext["orderedItems"]
    ]
    assert [r.rank for r in rows] == [1, 2]
    assert all(item["film"].startswith("http") for item in ext["orderedItems"])


# --- the human-readable prose ----------------------------------------------


@pytest.mark.django_db
def test_the_body_writes_films_in_rank_order_with_years(alice, dune, alien):
    film_list = create_list(alice, title="Pair", films=[dune, alien])
    content = note_document(film_list.status, _request())["content"]
    assert "<ol>" in content
    assert content.index("Dune (2021)") < content.index("Alien (1979)")


@pytest.mark.django_db
def test_a_title_is_escaped_and_a_description_is_not(alice, dune):
    """Two halves, both wrong if you get them backwards. The title is plain
    text and must not be able to inject markup. The description is already
    sanitized HTML rendered at write time, and escaping it again would show
    a member their own tags."""
    film_list = create_list(
        alice,
        title="A & B <b>tag</b>",
        description="Real *emphasis* here.",
        films=[dune],
    )
    content = note_document(film_list.status, _request())["content"]
    assert "&amp;" in content
    assert "&lt;b&gt;tag&lt;/b&gt;" in content
    assert "<b>tag</b>" not in content
    # The description's own markup survives -- not double-escaped.
    assert "<em>emphasis</em>" in content
    assert "&lt;em&gt;" not in content


@pytest.mark.django_db
def test_a_long_list_caps_the_prose_but_not_the_extension(alice):
    """The cap protects the Mastodon post from arriving truncated with no
    way back; the extension carries every film regardless, so the cap
    costs a ReelTalk peer nothing it needed."""
    many = _films(LIST_CONTENT_FILM_LIMIT + 5, prefix="Long")
    film_list = create_list(alice, title="Very Long", films=many)
    doc = note_document(film_list.status, _request())
    assert doc["content"].count("<li>") == LIST_CONTENT_FILM_LIMIT
    assert f"Showing the first {LIST_CONTENT_FILM_LIMIT} of" in doc["content"]
    assert len(doc["reeltalk:list"]["orderedItems"]) == LIST_CONTENT_FILM_LIMIT + 5


@pytest.mark.django_db
def test_an_empty_list_still_reads_as_a_post(alice):
    """A list made through the create form always starts empty -- films come
    from the editor afterwards -- so the very first ``Create`` a peer sees
    for a list is this one. It must read as deliberate, not broken."""
    film_list = create_list(alice, title="Still Deciding")
    content = note_document(film_list.status, _request())["content"]
    assert "Still Deciding" in content
    assert "No films in this list yet" in content
    assert "<ol>" not in content


# --- activities and the tombstone ------------------------------------------


@pytest.mark.django_db
def test_create_update_and_delete_each_carry_the_list_body(alice, dune):
    film_list = create_list(alice, title="All Three", films=[dune])
    request = _request()
    for activity in (
        create_activity(film_list.status, alice, request),
        update_activity(film_list.status, alice, request),
        delete_activity(film_list.status, alice, request),
    ):
        assert activity["object"]["type"] == "Note"
        assert "All Three" in activity["object"]["content"]
        assert activity["object"]["reeltalk:list"]["name"] == "All Three"


@pytest.mark.django_db
def test_the_tombstone_still_names_the_list_after_the_face_is_wiped(alice, dune):
    """``Status.delete()`` clears ``content`` and ``raw_content``. Because
    the body is composed from the ``FilmList`` rather than read off the
    face, the tombstone still says which list went away -- and no separate
    delete serializer was needed to get that."""
    film_list = create_list(alice, title="Doomed List", films=[dune])
    face = film_list.status
    soft_delete_list(film_list)
    face.refresh_from_db()
    assert face.content == ""  # the wipe really happened

    doc = delete_activity(face, alice, _request())["object"]
    assert "Doomed List" in doc["content"]
    assert doc["reeltalk:list"]["name"] == "Doomed List"


@pytest.mark.django_db
def test_repeated_updates_share_the_object_id_not_the_activity_id(alice, dune):
    """Idempotence per L11: re-sending must not create a second list on a
    peer, which needs the object id stable; and each edit must actually
    arrive, which needs the activity id unique. Both halves, pinned."""
    film_list = create_list(alice, title="Stable Object", films=[dune])
    request = _request()
    first = update_activity(film_list.status, alice, request)
    second = update_activity(film_list.status, alice, request)
    assert first["object"]["id"] == second["object"]["id"]
    assert first["id"] != second["id"]


# --- the broadcast wiring and its change gate ------------------------------


@responses.activate
@pytest.mark.django_db
def test_creating_a_list_delivers_a_create_to_remote_followers(alice, dune):
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _login("alice").post("/lists/new/", {"title": "Fresh List", "description": ""})
    assert _urls() == [REMOTE_INBOX]
    sent = _sent()
    assert sent["type"] == "Create"
    assert "Fresh List" in sent["object"]["content"]
    assert sent["object"]["reeltalk:list"]["name"] == "Fresh List"


@responses.activate
@pytest.mark.django_db
def test_renaming_delivers_an_update(alice, dune):
    film_list = create_list(alice, title="Old Name", films=[dune])
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _login("alice").post(
        f"/list/{film_list.pk}/edit/", {"title": "New Name", "description": ""}
    )
    assert _sent()["type"] == "Update"
    assert "New Name" in _sent()["object"]["content"]


@responses.activate
@pytest.mark.django_db
def test_adding_a_film_delivers_an_update(alice, dune, alien):
    film_list = create_list(alice, title="Growing", films=[dune])
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _login("alice").post(f"/list/{film_list.pk}/add-film/", {"film_id": alien.pk})
    sent = _sent()
    assert sent["type"] == "Update"
    assert "Alien" in sent["object"]["content"]


@responses.activate
@pytest.mark.django_db
def test_re_adding_a_film_already_in_the_list_delivers_nothing(alice, dune):
    """``add_films`` skips what is already present and returns no rows, so
    the view's gate closes and the network hears nothing about a list that
    did not change. A double-click is not an edit event."""
    film_list = create_list(alice, title="Already Has It", films=[dune])
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _login("alice").post(f"/list/{film_list.pk}/add-film/", {"film_id": dune.pk})
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_removing_a_film_delivers_an_update(alice, dune, alien):
    film_list = create_list(alice, title="Shrinking", films=[dune, alien])
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _login("alice").post(f"/list/{film_list.pk}/remove-film/", {"film_id": alien.pk})
    assert _sent()["type"] == "Update"
    assert "Alien" not in _sent()["object"]["content"]


@responses.activate
@pytest.mark.django_db
def test_removing_a_film_that_is_not_in_the_list_delivers_nothing(alice, dune, alien):
    film_list = create_list(alice, title="Never Had It", films=[dune])
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _login("alice").post(f"/list/{film_list.pk}/remove-film/", {"film_id": alien.pk})
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_moving_a_row_delivers_an_update(alice, dune, alien):
    film_list = create_list(alice, title="Reorder Me", films=[dune, alien])
    item = film_list.items.order_by("rank").first()
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _login("alice").post(
        f"/list/{film_list.pk}/move/", {"item_id": item.pk, "direction": MOVE_DOWN}
    )
    sent = _sent()
    assert sent["type"] == "Update"
    ordered = [i["name"] for i in sent["object"]["reeltalk:list"]["orderedItems"]]
    assert ordered == ["Alien", "Dune"]


@responses.activate
@pytest.mark.django_db
def test_moving_at_the_end_delivers_nothing(alice, dune, alien):
    """``move`` returns False at the ends rather than raising, and a press
    that moved nothing must not fan out as an edit event."""
    film_list = create_list(alice, title="Already First", films=[dune, alien])
    first = film_list.items.order_by("rank").first()
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _login("alice").post(
        f"/list/{film_list.pk}/move/", {"item_id": first.pk, "direction": MOVE_UP}
    )
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_submitting_the_edit_form_unchanged_delivers_nothing(alice, dune):
    """Posting back the same title and description is not an edit. The
    activity id carries a uuid, so a peer cannot dedup a spurious Update
    -- the only guard is not sending one."""
    film_list = create_list(
        alice, title="Same On Return", description="Unchanged.", films=[dune]
    )
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _login("alice").post(
        f"/list/{film_list.pk}/edit/",
        {"title": film_list.title, "description": film_list.raw_description},
    )
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_deleting_a_list_delivers_a_delete(alice, dune):
    film_list = create_list(alice, title="Delete Me", films=[dune])
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _login("alice").post(f"/list/{film_list.pk}/delete/")
    sent = _sent()
    assert sent["type"] == "Delete"
    assert f"#delete-{film_list.status_id}" in sent["id"]
    assert "Delete Me" in sent["object"]["content"]


# --- L6: saving federates nothing -----------------------------------------


@responses.activate
@pytest.mark.django_db
def test_saving_and_unsaving_a_list_deliver_nothing(alice, bob, dune):
    """L6 is absolute. The proof is that nothing left the process, not that
    the page displays nothing -- a broadcast that fired and was merely not
    rendered would pass a display check and fail this one.

    Carol follows **bob**, the list's owner, not alice, who is only the
    saver. With the follower on the wrong account the audience would be
    empty and this would pass on an empty recipient set rather than on
    silence -- see the control below, which must send with the same rows.
    """
    theirs = create_list(bob, title="Bob Picks", films=[dune])
    _following(_carol(), bob)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    client = _login("alice")
    assert client.post(f"/list/{theirs.pk}/save/").status_code == 200
    assert client.post(f"/list/{theirs.pk}/unsave/").status_code == 200
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_the_l6_silence_control_a_real_edit_on_the_same_list_does_deliver(
    alice, bob, dune, alien
):
    """Non-vacuity for the test above. If the delivery path were broken, or
    the follower row absent, the silence test would pass having proven
    nothing. Here the same owner, the same follower and the same inbox do
    produce a send -- so the silence on save is silence, not a dead wire."""
    theirs = create_list(bob, title="Bob Picks Again", films=[dune])
    _following(_carol(), bob)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    client = _login("alice")
    client.post(f"/list/{theirs.pk}/save/")
    assert len(responses.calls) == 0

    # Same owner, same follower, same inbox: an actual edit does arrive.
    add_films(theirs, [alien])
    broadcast_list_update(_request(), theirs)
    assert len(responses.calls) == 1
    assert _sent()["type"] == "Update"
    assert "Alien" in _sent()["object"]["content"]


@pytest.mark.django_db
def test_saving_a_list_adds_no_notification_either(alice, bob, dune):
    """L6 covers the ledger as well as the wire, and increment 5 pinned it
    there. Re-asserted here because increment 6 added broadcast calls to
    this same view module and a stray ``notify`` would land on the same
    table."""
    theirs = create_list(bob, title="Silent List", films=[dune])
    client = _login("alice")
    before = Notification.objects.count()
    client.post(f"/list/{theirs.pk}/save/")
    client.post(f"/list/{theirs.pk}/unsave/")
    assert Notification.objects.count() == before


# --- audience -------------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_local_follower_gets_no_delivery(bob, dune):
    maker = member(localname="maker", password="s3cretpass")
    reader = bob
    reader.follows.add(maker)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    client = Client()
    assert client.login(username="maker", password="s3cretpass")
    client.post("/lists/new/", {"title": "Local Only", "description": ""})
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_the_tombstone_reaches_everyone_the_create_reached(alice, dune):
    """``broadcast_status_delete`` uses the followers-only helper, so a
    named remote never gets a tombstone. The list family uses
    ``_status_targets`` for all three activities instead, and this pins
    that by putting a mention row on the face by hand -- a list description
    does not create one today, so the equality is proven rather than
    assumed."""
    film_list = create_list(alice, title="Named And Framed", films=[dune])
    carol = _following(_carol(), alice)
    dave = _dave()
    sync_status_mentions(film_list.status, [dave])
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    responses.add(responses.POST, OTHER_REMOTE_INBOX, status=202)

    _login("alice").post(f"/list/{film_list.pk}/delete/")
    assert sorted(_urls()) == sorted([REMOTE_INBOX, OTHER_REMOTE_INBOX])
    assert carol.inbox_url == REMOTE_INBOX


# --- the repair command ----------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_rebroadcast_sends_one_update_per_live_list(alice, dune):
    """The Create-dedup answer. A re-sent ``Create`` would collide with the
    id peers already hold and be discarded; an ``Update`` has a fresh id per
    event and lands."""
    first = create_list(alice, title="First Repair", films=[dune])
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    out = StringIO()
    call_command("rebroadcast_lists", stdout=out)
    assert len(responses.calls) == 1
    assert _sent()["type"] == "Update"
    assert "First Repair" in _sent()["object"]["content"]
    assert "Re-sent 1 list(s)" in out.getvalue()
    assert first.deleted is False


@responses.activate
@pytest.mark.django_db
def test_rebroadcast_skips_deleted_and_remote_lists(alice, dune):
    face = Status.objects.create(user=alice, status_type=Status.Type.LIST)
    FilmList.objects.create(
        user=alice,
        title="Mirrored From Elsewhere",
        status=face,
        local=False,
        remote_url="https://remote.example/list/9/",
    )
    doomed = create_list(alice, title="Doomed", films=[dune])
    soft_delete_list(doomed)
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    out = StringIO()
    call_command("rebroadcast_lists", stdout=out)
    assert len(responses.calls) == 0
    assert "No live local lists" in out.getvalue()


@responses.activate
@pytest.mark.django_db
def test_rebroadcast_dry_run_sends_nothing(alice, dune):
    create_list(alice, title="Not Yet Sent", films=[dune])
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    out = StringIO()
    call_command("rebroadcast_lists", dry_run=True, stdout=out)
    assert len(responses.calls) == 0
    assert "Not Yet Sent" in out.getvalue()
    assert "1 remote follower(s)" in out.getvalue()


@responses.activate
@pytest.mark.django_db
def test_rebroadcast_can_target_one_list(alice, dune):
    keep = create_list(alice, title="Send This One", films=[dune])
    create_list(alice, title="Leave That One", films=[dune])
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    call_command("rebroadcast_lists", list_ids=[keep.pk])
    assert len(responses.calls) == 1
    assert "Send This One" in _sent()["object"]["content"]


@responses.activate
@pytest.mark.django_db
def test_rebroadcast_twice_repairs_the_same_object_not_a_second_one(alice, dune):
    """Idempotence of the repair, stated precisely: two runs produce two
    activity ids and one object id, so a peer applies the second to the
    list it already has rather than creating another."""
    create_list(alice, title="Twice Over", films=[dune])
    _following(_carol(), alice)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    call_command("rebroadcast_lists", stdout=StringIO())
    call_command("rebroadcast_lists", stdout=StringIO())
    assert len(responses.calls) == 2
    first, second = _sent(0), _sent(1)
    assert first["object"]["id"] == second["object"]["id"]
    assert first["id"] != second["id"]
