"""Status broadcast + shelf events (M4 increment 6).

Inbound: Create/Update/Delete handlers mirror a remote user's reviews, film
objects, and shelve events — attributed to the verified sender, idempotent by
origin id (the object's home URL as received / the ShelfFilm row per
sender+film+shelf), with D7 matching so the same movie stays one row.
Outbound: local review create/update/delete and watchlist add/remove are
delivered as signed Create/Update/Delete activities to each remote follower's
home inbox; a dead follower drops its send without failing the request.
Also covers the /status/<id>/ Note fetch endpoint (R41's deferred route).
"""

import json
from decimal import Decimal

import pytest
import requests
import responses
from django.test import RequestFactory

from reeltalk.activitypub import crypto, signatures
from reeltalk.activitypub.broadcast import (
    broadcast_shelf_event,
    broadcast_status_create,
    broadcast_status_delete,
    broadcast_status_update,
)
from reeltalk.activitypub.inbox import process_inbound_activity
from reeltalk.activitypub.models import DeliveredActivity
from reeltalk.activitypub.statuses import RemoteObjectError
from reeltalk.core.models import Film, Shelf, ShelfFilm, Status
from reeltalk.social.models import User

REMOTE_ACTOR = "https://remote.example/user/carol/"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"
REMOTE_INBOX = REMOTE_ACTOR + "inbox"  # inbox_for() fallback: no trailing slash (R43)
REMOTE_FILM = "https://remote.example/film/7/"
ALICE_ACTOR = "http://testserver/user/alice/"


@pytest.fixture()
def remote_keypair():
    return crypto.generate_keypair()


@pytest.fixture()
def person_doc(remote_keypair):
    _private_pem, public_pem = remote_keypair
    return {
        "@context": ["https://www.w3.org/ns/activitystreams"],
        "id": REMOTE_ACTOR,
        "type": "Person",
        "preferredUsername": "carol",
        "name": "Carol Remote",
        "inbox": REMOTE_INBOX,
        "outbox": REMOTE_ACTOR + "outbox/",
        "publicKey": {
            "id": REMOTE_KEY_ID,
            "owner": REMOTE_ACTOR,
            "publicKeyPem": public_pem,
        },
    }


def _request_with_host():
    """A bare request carrying the test-server Host header."""
    request = RequestFactory().get("/")
    request.META["HTTP_HOST"] = "testserver"
    return request


def _signed_post(path: str, body: bytes, private_pem: str, key_id=REMOTE_KEY_ID):
    """RFC 9421 signature headers for a POST to ``http://testserver{path}``."""
    return signatures.sign_request(
        "POST", f"http://testserver{path}", private_pem, key_id=key_id, body=body
    )


def _post_inbox(client, path: str, body: bytes, headers: dict):
    meta = {}
    for name, value in headers.items():
        meta[f"HTTP_{name.upper().replace('-', '_')}"] = value
    meta["HTTP_HOST"] = "testserver"
    return client.post(
        path, data=body, content_type="application/activity+json", **meta
    )


def _carol() -> User:
    """Carol's mirror directly (no first-contact fetch needed)."""
    return User(localname="carol@remote.example", local=False, actor_url=REMOTE_ACTOR)


def _film_doc(url: str, *, name="Arrival", year=2016, tmdb_id=None) -> dict:
    doc = {
        "@context": ["https://www.w3.org/ns/activitystreams"],
        "id": url,
        "type": "Film",
        "name": name,
        "url": url,
    }
    if year is not None:
        doc["year"] = year
    if tmdb_id is not None:
        doc["tmdbId"] = tmdb_id
    return doc


def _note(id_url: str, *, content="", rating=None, film=None) -> dict:
    note = {"id": id_url, "type": "Note"}
    if content:
        note["content"] = content
    if rating is not None:
        note["rating"] = rating
    if film:
        note["film"] = film
    return note


def _activity(id_url: str, type_: str, obj) -> dict:
    return {"id": id_url, "type": type_, "actor": REMOTE_ACTOR, "object": obj}


# --- Inbound Create(Note): status mirrors ------------------------------------


@responses.activate
@pytest.mark.django_db
def test_inbound_create_note_mirrors_review_d7_matched_film():
    carol = _carol()
    carol.save()
    local_blade = Film.objects.create(title="Blade Runner", year=1982, tmdb_id=1316)
    responses.add(
        responses.GET,
        REMOTE_FILM,
        json=_film_doc(REMOTE_FILM, name="Blade Runner", year=1982, tmdb_id=1316),
    )
    request = _request_with_host()
    activity = _activity(
        "https://remote.example/activity/s1",
        "Create",
        _note(
            "https://remote.example/status/42/",
            content="A masterpiece.",
            rating=4.5,
            film=REMOTE_FILM,
        ),
    )
    assert process_inbound_activity(activity, carol, request) == "handled"

    status = Status.objects.get(local=False)
    assert status.user_id == carol.pk  # attributed to the verified sender
    assert status.remote_url == "https://remote.example/status/42/"
    assert status.remote_id == 42
    assert status.status_type == Status.Type.REVIEW
    assert status.rating == Decimal("4.5")
    assert status.content == "A masterpiece."
    # D7: the referenced film matched our existing row — no duplicate mirror.
    assert status.film_id == local_blade.pk
    assert Film.objects.count() == 1
    # A review implies Watched: the mirror's read shelf row exists for the feed.
    row = ShelfFilm.objects.get(user=carol, film=local_blade)
    assert row.shelf.identifier == Shelf.READ


@responses.activate
@pytest.mark.django_db
def test_inbound_create_note_fetches_and_mirrors_unknown_film():
    carol = _carol()
    carol.save()
    responses.add(responses.GET, REMOTE_FILM, json=_film_doc(REMOTE_FILM))
    request = _request_with_host()
    activity = _activity(
        "https://remote.example/activity/s2",
        "Create",
        _note("https://remote.example/status/43/", rating=5.0, film=REMOTE_FILM),
    )
    process_inbound_activity(activity, carol, request)

    film = Film.objects.get(remote_url=REMOTE_FILM)
    assert film.title == "Arrival" and film.year == 2016
    assert film.remote_id == 7  # parsed from /film/7/
    assert film.origin_id is None  # a mirror claims no local origin
    status = Status.objects.get(local=False)
    assert status.film_id == film.pk
    assert status.status_type == Status.Type.REVIEW_RATING


@responses.activate
@pytest.mark.django_db
def test_inbound_create_comment_uses_local_film_no_fetch():
    carol = _carol()
    carol.save()
    film = Film.objects.create(title="Arrival", year=2016)
    request = _request_with_host()
    activity = _activity(
        "https://remote.example/activity/s3",
        "Create",
        _note(
            "https://remote.example/status/44/",
            content="Loved the ending.",
            film=f"http://testserver/film/{film.pk}/",
        ),
    )
    process_inbound_activity(activity, carol, request)

    status = Status.objects.get(local=False)
    assert status.status_type == Status.Type.COMMENT
    assert status.film_id == film.pk
    # A comment carries no shelf row (only reviews imply Watched).
    assert ShelfFilm.objects.filter(user=carol).count() == 0
    # A film URL on this instance resolves locally — no fetch of it.
    assert len(responses.calls) == 0


@pytest.mark.django_db
def test_inbound_create_note_uses_sender_not_attributed_to():
    carol = _carol()
    carol.save()
    film = Film.objects.create(title="Arrival", year=2016)
    request = _request_with_host()
    note = _note(
        "https://remote.example/status/45/",
        content="Note.",
        film=f"http://testserver/film/{film.pk}/",
    )
    note["attributedTo"] = "https://attacker.example/user/mallory/"  # forged
    activity = _activity("https://remote.example/activity/s6", "Create", note)
    process_inbound_activity(activity, carol, request)

    status = Status.objects.get(local=False)
    assert status.user_id == carol.pk
    # No mirror was created for the forged attributedTo.
    assert (
        User.objects.filter(actor_url="https://attacker.example/user/mallory/").count()
        == 0
    )


@pytest.mark.django_db
def test_inbound_create_note_redelivery_is_duplicate():
    carol = _carol()
    carol.save()
    film = Film.objects.create(title="Arrival", year=2016)
    request = _request_with_host()
    activity = _activity(
        "https://remote.example/activity/s7",
        "Create",
        _note(
            "https://remote.example/status/46/",
            content="Note.",
            film=f"http://testserver/film/{film.pk}/",
        ),
    )
    assert process_inbound_activity(activity, carol, request) == "handled"
    assert process_inbound_activity(activity, carol, request) == "duplicate"
    assert Status.objects.filter(local=False).count() == 1
    assert DeliveredActivity.objects.count() == 1


@pytest.mark.django_db
def test_inbound_same_object_second_activity_no_double_row():
    # A different activity id referencing the same Note (e.g. a re-sent Create
    # after an id change upstream) still converges on one mirror row — the
    # origin id, not just the activity wire id, is the dedup key.
    carol = _carol()
    carol.save()
    film = Film.objects.create(title="Arrival", year=2016)
    request = _request_with_host()
    note = _note(
        "https://remote.example/status/47/",
        content="Note.",
        film=f"http://testserver/film/{film.pk}/",
    )
    for activity_id in (
        "https://remote.example/activity/s8a",
        "https://remote.example/activity/s8b",
    ):
        assert (
            process_inbound_activity(
                _activity(activity_id, "Create", note), carol, request
            )
            == "handled"
        )
    assert Status.objects.filter(local=False).count() == 1


@pytest.mark.django_db
def test_inbound_note_without_film_ref_is_standalone():
    carol = _carol()
    carol.save()
    request = _request_with_host()
    activity = _activity(
        "https://remote.example/activity/s9",
        "Create",
        _note("https://remote.example/status/48/", content="A stray thought."),
    )
    process_inbound_activity(activity, carol, request)

    status = Status.objects.get(local=False)
    assert status.status_type is None  # standalone note
    assert status.film_id is None
    assert ShelfFilm.objects.filter(user=carol).count() == 0


@pytest.mark.django_db
def test_inbound_empty_note_mirrors_nothing():
    carol = _carol()
    carol.save()
    request = _request_with_host()
    activity = _activity(
        "https://remote.example/activity/s10",
        "Create",
        _note("https://remote.example/status/49/"),
    )
    assert process_inbound_activity(activity, carol, request) == "handled"
    assert Status.objects.filter(local=False).count() == 0


# --- Inbound Update / Delete(Note) -------------------------------------------


@pytest.mark.django_db
def test_inbound_update_note_applies_fields():
    carol = _carol()
    carol.save()
    film = Film.objects.create(title="Arrival", year=2016)
    request = _request_with_host()
    film_ref = f"http://testserver/film/{film.pk}/"
    create = _activity(
        "https://remote.example/activity/s11a",
        "Create",
        _note(
            "https://remote.example/status/50/",
            content="First draft.",
            rating=4.0,
            film=film_ref,
        ),
    )
    process_inbound_activity(create, carol, request)

    update = _activity(
        "https://remote.example/activity/s11b",
        "Update",
        _note(
            "https://remote.example/status/50/",
            content="Edited.",
            rating=4.5,
            film=film_ref,
        ),
    )
    process_inbound_activity(update, carol, request)

    status = Status.objects.get(local=False)
    assert status.content == "Edited."
    assert status.rating == Decimal("4.5")
    assert Status.objects.filter(local=False).count() == 1


@pytest.mark.django_db
def test_inbound_update_before_create_creates_mirror():
    # Out-of-order delivery: an Update that arrives before its Create still
    # produces the mirror (same mapping either way).
    carol = _carol()
    carol.save()
    film = Film.objects.create(title="Arrival", year=2016)
    request = _request_with_host()
    update = _activity(
        "https://remote.example/activity/s12",
        "Update",
        _note(
            "https://remote.example/status/51/",
            content="Late create.",
            rating=3.5,
            film=f"http://testserver/film/{film.pk}/",
        ),
    )
    process_inbound_activity(update, carol, request)
    assert Status.objects.filter(local=False).count() == 1


@pytest.mark.django_db
def test_inbound_delete_note_soft_deletes_and_is_idempotent():
    carol = _carol()
    carol.save()
    film = Film.objects.create(title="Arrival", year=2016)
    request = _request_with_host()
    note = _note(
        "https://remote.example/status/52/",
        content="Gone soon.",
        rating=4.0,
        film=f"http://testserver/film/{film.pk}/",
    )
    process_inbound_activity(
        _activity("https://remote.example/activity/s13a", "Create", note),
        carol,
        request,
    )

    for activity_id in (
        "https://remote.example/activity/s13b",
        "https://remote.example/activity/s13c",
    ):
        process_inbound_activity(_activity(activity_id, "Delete", note), carol, request)

    status = Status.objects.get(local=False)
    assert status.deleted is True  # tombstone, identity intact
    assert status.content == ""
    assert status.remote_url == "https://remote.example/status/52/"
    assert Status.objects.filter(local=False).count() == 1


# --- Inbound Create(Film) / Delete(Film) -------------------------------------


@pytest.mark.django_db
def test_inbound_create_film_mirrors_and_is_idempotent():
    carol = _carol()
    carol.save()
    request = _request_with_host()
    doc = _film_doc(REMOTE_FILM, name="Arrival", year=2016)
    for activity_id in (
        "https://remote.example/activity/s14a",
        "https://remote.example/activity/s14b",
    ):
        process_inbound_activity(_activity(activity_id, "Create", doc), carol, request)

    film = Film.objects.get(remote_url=REMOTE_FILM)
    assert film.title == "Arrival" and film.year == 2016
    assert film.remote_id == 7
    assert film.origin_id is None
    assert Film.objects.count() == 1


@pytest.mark.django_db
def test_inbound_create_film_d7_match_creates_no_duplicate():
    carol = _carol()
    carol.save()
    local_blade = Film.objects.create(title="Blade Runner", year=1982, tmdb_id=1316)
    request = _request_with_host()
    doc = _film_doc(
        "https://remote.example/film/9/", name="Blade Runner", year=1982, tmdb_id=1316
    )
    process_inbound_activity(
        _activity("https://remote.example/activity/s15", "Create", doc), carol, request
    )

    # The D7 match wins: no new row, and the local row keeps its identity.
    assert Film.objects.count() == 1
    assert local_blade.remote_url == ""


@pytest.mark.django_db
def test_inbound_delete_film_ignored_gracefully():
    carol = _carol()
    carol.save()
    request = _request_with_host()
    doc = _film_doc(REMOTE_FILM)
    process_inbound_activity(
        _activity("https://remote.example/activity/s16a", "Create", doc), carol, request
    )
    process_inbound_activity(
        _activity("https://remote.example/activity/s16b", "Delete", doc), carol, request
    )
    # v0.1: film rows are PROTECTed while referenced — deletion is ignored.
    assert Film.objects.filter(remote_url=REMOTE_FILM).count() == 1


# --- Inbound ShelfEvent -------------------------------------------------------


@pytest.mark.django_db
def test_inbound_shelf_event_adds_and_removes_row():
    carol = _carol()
    carol.save()
    film = Film.objects.create(title="Arrival", year=2016)
    request = _request_with_host()
    event = {
        "id": f"{REMOTE_ACTOR}#shelve-to-read-{film.pk}",
        "type": "ShelfEvent",
        "film": f"http://testserver/film/{film.pk}/",
        "shelf": Shelf.TO_READ,
    }
    process_inbound_activity(
        _activity("https://remote.example/activity/s17a", "Create", event),
        carol,
        request,
    )

    row = ShelfFilm.objects.get(user=carol, film=film)
    assert row.shelf.identifier == Shelf.TO_READ
    # The mirror's shelf was created from federation (R15).
    assert row.shelf.user_id == carol.pk

    # A second event with a different activity id converges on the same row.
    process_inbound_activity(
        _activity("https://remote.example/activity/s17b", "Create", event),
        carol,
        request,
    )
    assert ShelfFilm.objects.filter(user=carol, film=film).count() == 1

    # The Delete removes it; redelivering the Delete is a no-op.
    for activity_id in (
        "https://remote.example/activity/s17c",
        "https://remote.example/activity/s17d",
    ):
        process_inbound_activity(
            _activity(activity_id, "Delete", event), carol, request
        )
    assert ShelfFilm.objects.filter(user=carol, film=film).count() == 0


@pytest.mark.django_db
def test_inbound_shelf_event_unknown_identifier_ignored():
    carol = _carol()
    carol.save()
    film = Film.objects.create(title="Arrival", year=2016)
    request = _request_with_host()
    event = {
        "id": f"{REMOTE_ACTOR}#shelve-something-{film.pk}",
        "type": "ShelfEvent",
        "film": f"http://testserver/film/{film.pk}/",
        "shelf": "in-progress",  # not a D1 shelf
    }
    process_inbound_activity(
        _activity("https://remote.example/activity/s18", "Create", event),
        carol,
        request,
    )
    assert ShelfFilm.objects.filter(user=carol).count() == 0


@pytest.mark.django_db
def test_inbound_unknown_object_type_ignored_gracefully():
    carol = _carol()
    carol.save()
    request = _request_with_host()
    activity = _activity(
        "https://remote.example/activity/s19",
        "Create",
        {"id": "https://remote.example/book/3", "type": "Book"},
    )
    assert process_inbound_activity(activity, carol, request) == "handled"
    assert Film.objects.count() == 0
    assert Status.objects.filter(local=False).count() == 0


@responses.activate
@pytest.mark.django_db
def test_inbound_review_with_unresolvable_film_rolls_back():
    # A review must be anchored: an unreachable film reference raises, and the
    # whole activity (dedup row included) rolls back so a transient failure
    # stays retryable instead of silently dropping the review.
    carol = _carol()
    carol.save()
    responses.add(responses.GET, REMOTE_FILM, status=500)
    request = _request_with_host()
    activity = _activity(
        "https://remote.example/activity/s20",
        "Create",
        _note(
            "https://remote.example/status/53/",
            content="Review.",
            rating=4.0,
            film=REMOTE_FILM,
        ),
    )
    with pytest.raises(RemoteObjectError):
        process_inbound_activity(activity, carol, request)
    assert Status.objects.filter(local=False).count() == 0
    assert DeliveredActivity.objects.count() == 0


# --- Inbound end-to-end (signed delivery through the pipeline) ----------------


@responses.activate
@pytest.mark.django_db
def test_inbox_create_note_end_to_end(client, remote_keypair, person_doc):
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    film = Film.objects.create(title="Arrival", year=2016)
    private_pem, _public_pem = remote_keypair
    activity = _activity(
        "https://remote.example/activity/s21",
        "Create",
        _note(
            "https://remote.example/status/54/",
            content="End to end.",
            rating=3.5,
            film=f"http://testserver/film/{film.pk}/",
        ),
    )
    body = json.dumps(activity).encode()
    response = _post_inbox(
        client, "/inbox/", body, _signed_post("/inbox/", body, private_pem)
    )
    assert response.status_code == 202

    status = Status.objects.get(local=False)
    assert status.user.localname == "carol@remote.example"
    assert status.film_id == film.pk
    assert DeliveredActivity.objects.count() == 1


# --- Outbound broadcast -------------------------------------------------------


def _alice_with_followers():
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    carol = _carol()
    carol.save()
    bob.follows.add(alice)  # local follower — never delivered to
    carol.follows.add(alice)  # remote mirror — delivered to her home inbox
    return alice, bob, carol


@responses.activate
@pytest.mark.django_db
def test_broadcast_status_create_delivers_to_remote_followers_only():
    alice, _bob, _carol = _alice_with_followers()
    film = Film.objects.create(title="Arrival", year=2016)
    status = Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=Decimal("4.5"),
        content="Great.",
        raw_content="Great.",
    )
    responses.add(responses.POST, REMOTE_INBOX)
    broadcast_status_create(_request_with_host(), status)

    assert len(responses.calls) == 1  # bob (local) got nothing
    post = responses.calls[0].request
    assert post.url == REMOTE_INBOX
    sent = json.loads(post.body)
    assert sent["type"] == "Create"
    assert sent["actor"] == ALICE_ACTOR
    assert sent["object"]["id"] == f"http://testserver/status/{status.pk}/"
    assert sent["object"]["type"] == "Note"
    assert sent["object"]["rating"] == 4.5
    assert sent["object"]["film"] == f"http://testserver/film/{film.pk}/"
    # Signed with alice's key (RFC 9421 headers, her keyid).
    assert f'keyid="{ALICE_ACTOR}#main-key"' in post.headers["Signature-Input"]
    assert post.headers["Content-Type"] == "application/activity+json"


@responses.activate
@pytest.mark.django_db
def test_broadcast_status_update_unique_activity_ids_stable_object():
    alice, _bob, _carol = _alice_with_followers()
    film = Film.objects.create(title="Arrival", year=2016)
    status = Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW_RATING,
        rating=Decimal("4"),
    )
    responses.add(responses.POST, REMOTE_INBOX)
    broadcast_status_update(_request_with_host(), status)
    broadcast_status_update(_request_with_host(), status)

    first = json.loads(responses.calls[0].request.body)
    second = json.loads(responses.calls[1].request.body)
    assert first["type"] == "Update" and second["type"] == "Update"
    # Two edits must not collide on the activity id (the second would dedup
    # as a redelivery of the first), but the object identity is stable.
    assert first["id"] != second["id"]
    assert first["object"]["id"] == second["object"]["id"]


@responses.activate
@pytest.mark.django_db
def test_broadcast_status_delete_shape():
    alice, _bob, _carol = _alice_with_followers()
    film = Film.objects.create(title="Arrival", year=2016)
    status = Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW_RATING,
        rating=Decimal("4"),
    )
    status.delete()  # soft-delete — the tombstone keeps its identity
    responses.add(responses.POST, REMOTE_INBOX)
    broadcast_status_delete(_request_with_host(), status)
    first_id = json.loads(responses.calls[0].request.body)["id"]
    broadcast_status_delete(_request_with_host(), status)

    sent = json.loads(responses.calls[0].request.body)
    assert sent["type"] == "Delete"
    assert sent["object"]["id"] == f"http://testserver/status/{status.pk}/"
    # A stable id: a status is deleted at most once in v0.1.
    assert json.loads(responses.calls[1].request.body)["id"] == first_id


@responses.activate
@pytest.mark.django_db
def test_broadcast_shelf_event_shapes():
    alice, _bob, _carol = _alice_with_followers()
    film = Film.objects.create(title="Arrival", year=2016)
    request = _request_with_host()
    responses.add(responses.POST, REMOTE_INBOX)
    broadcast_shelf_event(request, alice, film, Shelf.TO_READ, added=True)
    first = json.loads(responses.calls[0].request.body)
    broadcast_shelf_event(request, alice, film, Shelf.TO_READ, added=False)
    second = json.loads(responses.calls[1].request.body)
    broadcast_shelf_event(request, alice, film, Shelf.TO_READ, added=True)
    third = json.loads(responses.calls[2].request.body)

    assert first["type"] == "Create" and second["type"] == "Delete"
    for sent in (first, second, third):
        obj = sent["object"]
        assert obj["type"] == "ShelfEvent"
        assert obj["shelf"] == Shelf.TO_READ
        assert obj["film"] == f"http://testserver/film/{film.pk}/"
        # The object id is stable per (user, film, shelf)…
    assert first["object"]["id"] == second["object"]["id"] == third["object"]["id"]
    # …while the activity ids are unique per event — an unshelve-then-re-shelve
    # must not dedup as a redelivery of the original.
    assert len({first["id"], second["id"], third["id"]}) == 3


@responses.activate
@pytest.mark.django_db
def test_broadcast_drops_unreachable_follower():
    alice, _bob, carol = _alice_with_followers()
    dave = User(
        localname="dave@other.example",
        local=False,
        actor_url="https://other.example/user/dave/",
    )
    dave.save()
    dave.follows.add(alice)
    film = Film.objects.create(title="Arrival", year=2016)
    status = Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW_RATING,
        rating=Decimal("4"),
    )
    responses.add(responses.POST, REMOTE_INBOX)
    # A connection-level failure must be a requests exception for the
    # broadcast's ``except requests.RequestException`` to catch it.
    responses.add(
        responses.POST,
        "https://other.example/user/dave/inbox",
        body=requests.exceptions.ConnectionError("refused"),
    )

    # Must not raise — a dead follower must not fail the user's request.
    broadcast_status_create(_request_with_host(), status)

    assert len(responses.calls) == 2
    urls = {c.request.url for c in responses.calls}
    assert urls == {REMOTE_INBOX, "https://other.example/user/dave/inbox"}


@responses.activate
@pytest.mark.django_db
def test_broadcast_no_remote_followers_is_a_noop():
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    bob.follows.add(alice)  # local only
    film = Film.objects.create(title="Arrival", year=2016)
    status = Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW_RATING,
        rating=Decimal("4"),
    )
    broadcast_status_create(_request_with_host(), status)
    assert len(responses.calls) == 0


# --- View wiring --------------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_mark_watched_view_broadcasts_create_and_watchlist_removal(client):
    alice = User.objects.create_user(localname="alice", password="p")
    carol = _carol()
    carol.save()
    carol.follows.add(alice)
    film = Film.objects.create(title="Arrival", year=2016)
    watchlist = Shelf.objects.get(user=alice, identifier=Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=watchlist, film=film, user=alice)
    client.force_login(alice)

    responses.add(responses.POST, REMOTE_INBOX)
    response = client.post(
        f"/film/{film.pk}/watched/", {"rating": "4.5", "content": "Great."}
    )

    assert response.status_code == 302
    assert len(responses.calls) == 2
    created = json.loads(responses.calls[0].request.body)
    removed = json.loads(responses.calls[1].request.body)
    assert created["type"] == "Create" and created["object"]["type"] == "Note"
    assert created["object"]["rating"] == 4.5
    # mark_watched took the film off the Watchlist (D1) — that removal is
    # broadcast too, so the follower's mirrored watchlist row goes away.
    assert removed["type"] == "Delete" and removed["object"]["type"] == "ShelfEvent"
    assert removed["object"]["shelf"] == Shelf.TO_READ


@responses.activate
@pytest.mark.django_db
def test_mark_watched_view_broadcasts_update_on_refinish(client):
    alice = User.objects.create_user(localname="alice", password="p")
    carol = _carol()
    carol.save()
    carol.follows.add(alice)
    film = Film.objects.create(title="Arrival", year=2016)
    Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW_RATING,
        rating=Decimal("4"),
    )
    client.force_login(alice)

    responses.add(responses.POST, REMOTE_INBOX)
    response = client.post(f"/film/{film.pk}/watched/", {"rating": "5"})

    assert response.status_code == 302
    assert (
        len(responses.calls) == 1
    )  # an edit is one Update — no shelf event (wasn't shelved)
    sent = json.loads(responses.calls[0].request.body)
    assert sent["type"] == "Update"
    assert sent["object"]["rating"] == 5.0


@responses.activate
@pytest.mark.django_db
def test_shelve_view_broadcasts_create(client):
    alice = User.objects.create_user(localname="alice", password="p")
    carol = _carol()
    carol.save()
    carol.follows.add(alice)
    film = Film.objects.create(title="Arrival", year=2016)
    client.force_login(alice)

    responses.add(responses.POST, REMOTE_INBOX)
    response = client.post(f"/film/{film.pk}/shelve/")

    assert response.status_code == 302
    sent = json.loads(responses.calls[0].request.body)
    assert len(responses.calls) == 1
    assert sent["type"] == "Create" and sent["object"]["type"] == "ShelfEvent"
    assert sent["object"]["shelf"] == Shelf.TO_READ


@responses.activate
@pytest.mark.django_db
def test_unshelve_view_broadcasts_delete(client):
    alice = User.objects.create_user(localname="alice", password="p")
    carol = _carol()
    carol.save()
    carol.follows.add(alice)
    film = Film.objects.create(title="Arrival", year=2016)
    watchlist = Shelf.objects.get(user=alice, identifier=Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=watchlist, film=film, user=alice)
    client.force_login(alice)

    responses.add(responses.POST, REMOTE_INBOX)
    response = client.post(f"/film/{film.pk}/unshelve/")

    assert response.status_code == 302
    sent = json.loads(responses.calls[0].request.body)
    assert len(responses.calls) == 1
    assert sent["type"] == "Delete" and sent["object"]["type"] == "ShelfEvent"


@responses.activate
@pytest.mark.django_db
def test_search_watchlist_broadcasts(client):
    alice = User.objects.create_user(localname="alice", password="p")
    carol = _carol()
    carol.save()
    carol.follows.add(alice)
    # A row with the tmdb_id already exists: create_or_match short-circuits
    # without an API call.
    Film.objects.create(title="Arrival", year=2016, tmdb_id=4935)
    client.force_login(alice)

    responses.add(responses.POST, REMOTE_INBOX)
    response = client.post("/search/watchlist/4935/")

    assert response.status_code == 200
    assert response.json() == {"status": "added"}
    sent = json.loads(responses.calls[0].request.body)
    assert len(responses.calls) == 1
    assert sent["type"] == "Create" and sent["object"]["type"] == "ShelfEvent"


# --- The /status/<id>/ fetch endpoint (R41's deferred route) ------------------


@pytest.mark.django_db
def test_status_detail_serves_note_document_to_ap_clients(client):
    alice = User.objects.create_user(localname="alice", password="p")
    film = Film.objects.create(title="Arrival", year=2016)
    status = Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW_RATING,
        rating=Decimal("4.5"),
    )
    response = client.get(
        f"/status/{status.pk}/", HTTP_ACCEPT="application/activity+json"
    )
    assert response.status_code == 200
    doc = response.json()
    assert doc["id"] == f"http://testserver/status/{status.pk}/"
    assert doc["type"] == "Note"
    assert doc["rating"] == 4.5


@pytest.mark.django_db
def test_status_detail_404_for_browsers_deleted_and_mirrors(client):
    alice = User.objects.create_user(localname="alice", password="p")
    film = Film.objects.create(title="Arrival", year=2016)
    status = Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW_RATING,
        rating=Decimal("4.5"),
    )
    ap = {"HTTP_ACCEPT": "application/activity+json"}
    # Browsers get no JSON (there is no human-facing status page in v0.1).
    assert client.get(f"/status/{status.pk}/").status_code == 404
    # A tombstone is not served…
    status.delete()
    assert client.get(f"/status/{status.pk}/", **ap).status_code == 404
    # …nor a remote mirror (its canonical id is its home instance's URL).
    mirror = _carol()
    mirror.save()
    mirrored = Status.objects.create(
        user=mirror,
        film=film,
        status_type=Status.Type.REVIEW_RATING,
        rating=Decimal("4"),
        local=False,
        remote_url="https://remote.example/status/60/",
    )
    assert client.get(f"/status/{mirrored.pk}/", **ap).status_code == 404
