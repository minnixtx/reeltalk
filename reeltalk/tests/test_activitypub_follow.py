"""Follow / Undo(Follow) handling, both directions (M4 increment 5).

Inbound: a remote user following/unfollowing one of ours records the follow
M2M from the *verified sender* — the followers/following collections and the
feed query read that row. Outbound: a local user following/unfollowing a
remote user records the M2M (feeding the existing feed query) and delivers a
signed Follow / Undo(Follow) to the followed user's home inbox. Also covers
the Person-collection growth for remote mirrors and the mirror's stored inbox
URL (R42 create-only).
"""

import json
from urllib.parse import urlparse

import pytest
import responses
from django.test import RequestFactory

from reeltalk.activitypub import crypto, signatures
from reeltalk.activitypub.follow import (
    _follow_activity,
    follow_user,
    unfollow_user,
)
from reeltalk.activitypub.mirrors import mirror_user_from_person
from reeltalk.core.models import Status
from reeltalk.social.models import User

REMOTE_ACTOR = "https://remote.example/user/carol/"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"
REMOTE_INBOX = REMOTE_ACTOR + "inbox/"
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


def _make_mirror() -> User:
    return User(localname="carol@remote.example", local=False, actor_url=REMOTE_ACTOR)


# --- Inbound (a remote user follows / unfollows one of ours) -----------------


@responses.activate
@pytest.mark.django_db
def test_inbound_follow_records_m2m_from_sender(client, remote_keypair, person_doc):
    alice = User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    private_pem, _public_pem = remote_keypair
    activity = {
        "id": "https://remote.example/activity/f1",
        "type": "Follow",
        "actor": REMOTE_ACTOR,
        "object": ALICE_ACTOR,
    }
    body = json.dumps(activity).encode()
    response = _post_inbox(
        client,
        "/user/alice/inbox/",
        body,
        _signed_post("/user/alice/inbox/", body, private_pem),
    )
    assert response.status_code == 202
    mirror = User.objects.get(local=False)
    # Recorded from the verified sender (the mirror): the mirror follows alice,
    # so alice's followers include the mirror.
    assert alice in mirror.follows.all()
    assert mirror in alice.followers.all()


@responses.activate
@pytest.mark.django_db
def test_inbound_follow_uses_sender_not_declared_actor(
    client, remote_keypair, person_doc
):
    # The activity's self-declared actor is ignored: the follow is attributed
    # to the signature's verified sender, not a forged ``actor`` field.
    alice = User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    private_pem, _public_pem = remote_keypair
    activity = {
        "id": "https://remote.example/activity/f2",
        "type": "Follow",
        "actor": "https://attacker.example/user/mallory/",  # forged — ignored
        "object": ALICE_ACTOR,
    }
    body = json.dumps(activity).encode()
    response = _post_inbox(
        client,
        "/user/alice/inbox/",
        body,
        _signed_post("/user/alice/inbox/", body, private_pem),
    )
    assert response.status_code == 202
    mirror = User.objects.get(local=False)
    assert alice in mirror.follows.all()
    # No mirror was created for the forged actor.
    forged = "https://attacker.example/user/mallory/"
    assert User.objects.filter(actor_url=forged).count() == 0


@responses.activate
@pytest.mark.django_db
def test_inbound_undo_follow_removes_m2m(client, remote_keypair, person_doc):
    alice = User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    private_pem, _public_pem = remote_keypair

    follow = {
        "id": "https://remote.example/activity/f3a",
        "type": "Follow",
        "actor": REMOTE_ACTOR,
        "object": ALICE_ACTOR,
    }
    body = json.dumps(follow).encode()
    _post_inbox(
        client,
        "/user/alice/inbox/",
        body,
        _signed_post("/user/alice/inbox/", body, private_pem),
    )
    mirror = User.objects.get(local=False)
    assert alice in mirror.follows.all()

    undo = {
        "id": "https://remote.example/activity/f3b",
        "type": "Undo",
        "actor": REMOTE_ACTOR,
        "object": follow,  # the original Follow activity
    }
    body = json.dumps(undo).encode()
    response = _post_inbox(
        client,
        "/user/alice/inbox/",
        body,
        _signed_post("/user/alice/inbox/", body, private_pem),
    )
    assert response.status_code == 202
    mirror.refresh_from_db()
    assert alice not in mirror.follows.all()


@responses.activate
@pytest.mark.django_db
def test_inbound_undo_of_other_type_is_ignored(client, remote_keypair, person_doc):
    # An Undo whose object is not a Follow must not touch the follow state.
    alice = User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    private_pem, _public_pem = remote_keypair

    follow = {
        "id": "https://remote.example/activity/f4a",
        "type": "Follow",
        "actor": REMOTE_ACTOR,
        "object": ALICE_ACTOR,
    }
    body = json.dumps(follow).encode()
    _post_inbox(
        client,
        "/user/alice/inbox/",
        body,
        _signed_post("/user/alice/inbox/", body, private_pem),
    )
    mirror = User.objects.get(local=False)

    undo = {
        "id": "https://remote.example/activity/f4b",
        "type": "Undo",
        "actor": REMOTE_ACTOR,
        "object": {"id": "https://remote.example/object/9", "type": "Create"},
    }
    body = json.dumps(undo).encode()
    response = _post_inbox(
        client,
        "/user/alice/inbox/",
        body,
        _signed_post("/user/alice/inbox/", body, private_pem),
    )
    assert response.status_code == 202
    mirror.refresh_from_db()
    assert alice in mirror.follows.all()  # unchanged


@responses.activate
@pytest.mark.django_db
def test_inbound_follow_unresolvable_object_ignored(client, remote_keypair, person_doc):
    # Object names a local user that does not exist: accepted + deduped, no
    # follow recorded, and no fetch of the unknown actor as a side effect.
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    private_pem, _public_pem = remote_keypair
    activity = {
        "id": "https://remote.example/activity/f5",
        "type": "Follow",
        "actor": REMOTE_ACTOR,
        "object": "http://testserver/user/nobody/",
    }
    body = json.dumps(activity).encode()
    response = _post_inbox(
        client,
        "/inbox/",
        body,
        _signed_post("/inbox/", body, private_pem),
    )
    assert response.status_code == 202
    # The sender was mirrored on first contact, but nothing follows anyone.
    mirror = User.objects.get(local=False)
    assert not mirror.follows.exists()


# --- Outbound (a local user follows / unfollows a remote user) ---------------


@responses.activate
@pytest.mark.django_db
def test_follow_user_records_and_delivers(person_doc):
    alice = User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    responses.add(responses.POST, REMOTE_INBOX)
    request = _request_with_host()

    mirror = follow_user(request, alice, REMOTE_ACTOR)

    assert mirror.local is False and mirror.actor_url == REMOTE_ACTOR
    # Recorded: alice follows the mirror -> in her following (and feed set).
    assert mirror in alice.follows.all()
    # Delivered: one Person-doc fetch + one signed POST to the remote inbox.
    assert [c.request.method for c in responses.calls] == ["GET", "POST"]
    post = responses.calls[1].request
    assert post.url == REMOTE_INBOX
    sent = json.loads(post.body)
    assert sent["type"] == "Follow"
    assert sent["actor"] == ALICE_ACTOR
    assert sent["object"] == REMOTE_ACTOR
    # Signed with alice's key (RFC 9421 headers, her keyid).
    assert "sig1=(" in post.headers["Signature-Input"]
    assert f'keyid="{ALICE_ACTOR}#main-key"' in post.headers["Signature-Input"]
    assert post.headers["Content-Type"] == "application/activity+json"


@responses.activate
@pytest.mark.django_db
def test_follow_user_delivery_passes_own_verification(person_doc):
    # A follow we send must pass our own inbound signature check — the
    # ReelTalk-to-ReelTalk interop guarantee for follows.
    alice = User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    responses.add(responses.POST, REMOTE_INBOX)
    follow_user(_request_with_host(), alice, REMOTE_ACTOR)

    post = responses.calls[1].request
    parsed = urlparse(REMOTE_INBOX)
    rf_request = RequestFactory().post(
        parsed.path, data=post.body, content_type="application/activity+json"
    )
    # Reconstruct the request as our inbox would see it (same host/scheme the
    # sender signed against).
    rf_request.META["HTTP_HOST"] = parsed.netloc
    rf_request.META["HTTP_X_FORWARDED_PROTO"] = parsed.scheme
    for header in ("Signature-Input", "Signature", "Content-Digest"):
        meta_key = f"HTTP_{header.upper().replace('-', '_')}"
        rf_request.META[meta_key] = post.headers[header]
    assert signatures.verify_request(rf_request, alice.public_key) is True


@responses.activate
@pytest.mark.django_db
def test_unfollow_user_removes_and_delivers(person_doc):
    alice = User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    responses.add(responses.POST, REMOTE_INBOX)
    request = _request_with_host()

    follow_user(request, alice, REMOTE_ACTOR)  # establish the follow
    assert len(responses.calls) == 2
    mirror = User.objects.get(local=False)
    assert mirror in alice.follows.all()

    result = unfollow_user(request, alice, REMOTE_ACTOR)

    assert result.pk == mirror.pk
    # The mirror already existed: no new fetch, one Undo POST.
    assert [c.request.method for c in responses.calls] == ["GET", "POST", "POST"]
    sent = json.loads(responses.calls[2].request.body)
    assert sent["type"] == "Undo"
    assert sent["object"]["type"] == "Follow"
    assert sent["object"]["object"] == REMOTE_ACTOR
    alice.refresh_from_db()
    assert mirror not in alice.follows.all()


@responses.activate
@pytest.mark.django_db
def test_unfollow_user_noop_when_not_following(person_doc):
    alice = User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    # Not following: no mirror fetch (no side effect), no delivery.
    result = unfollow_user(_request_with_host(), alice, REMOTE_ACTOR)
    assert result is None
    assert len(responses.calls) == 0
    assert User.objects.filter(local=False).count() == 0


@pytest.mark.django_db
def test_follow_user_requires_local_follower():
    mirror = _make_mirror()
    mirror.save()
    with pytest.raises(ValueError):
        follow_user(_request_with_host(), mirror, "https://other.example/user/dave/")


# --- Collections: followers/following include remote mirrors -----------------


@pytest.mark.django_db
def test_followers_collection_includes_remote_mirror(client):
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    mirror = _make_mirror()
    mirror.save()
    # bob (local) and carol (remote) both follow alice.
    bob.follows.add(alice)
    mirror.follows.add(alice)

    collection = client.get("/user/alice/followers/").json()
    assert collection["totalItems"] == 2
    page = client.get("/user/alice/followers/?page=1").json()
    items = {item["id"]: item for item in page["items"]}
    # Local: the full Person document (actor path as id).
    local_item = items["http://testserver/user/bob/"]
    assert local_item["type"] == "Person" and local_item["preferredUsername"] == "bob"
    # Remote: a minimal Person document keyed on the home actor URL.
    remote_item = items[REMOTE_ACTOR]
    assert remote_item["type"] == "Person"
    # display_name is empty, so the name falls back to the mirror localname.
    assert remote_item["name"] == "carol@remote.example"


@pytest.mark.django_db
def test_following_collection_includes_remote_mirror(client):
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    mirror = _make_mirror()
    mirror.save()
    # alice follows bob (local) and carol (remote).
    alice.follows.add(bob)
    alice.follows.add(mirror)

    collection = client.get("/user/alice/following/").json()
    assert collection["totalItems"] == 2
    page = client.get("/user/alice/following/?page=1").json()
    ids = {item["id"] for item in page["items"]}
    assert ids == {"http://testserver/user/bob/", REMOTE_ACTOR}


# --- The follow state feeds the existing feed query --------------------------


@pytest.mark.django_db
def test_feed_includes_followed_remote_mirror_statuses():
    alice = User.objects.create_user(localname="alice", password="p")
    mirror = _make_mirror()
    mirror.save()
    # A mirrored status from the remote user (increment 6 creates these).
    status = Status(local=False, content="Remote review.")
    status.user = mirror
    status.save()

    # Before following: not in alice's feed.
    assert not Status.feed_for(alice).filter(pk=status.pk).exists()
    # After following: the follow M2M feeds the existing feed query.
    alice.follows.add(mirror)
    assert Status.feed_for(alice).filter(pk=status.pk).exists()


# --- Supporting details ------------------------------------------------------


@pytest.mark.django_db
def test_mirror_stores_inbox_url(person_doc):
    mirror = mirror_user_from_person(person_doc)
    assert mirror.inbox_url == REMOTE_INBOX


def test_follow_activity_ids_are_unique_per_event():
    # A follow -> unfollow -> re-follow must not collide on the activity id,
    # or the receiving instance would dedup the re-follow as a redelivery.
    # _follow_activity only reads the mirror's actor_url — no DB access needed.
    mirror = _make_mirror()
    ids = set()
    for _ in range(50):
        ids.add(_follow_activity(ALICE_ACTOR, mirror, undo=False)["id"])
        ids.add(_follow_activity(ALICE_ACTOR, mirror, undo=True)["id"])
    assert len(ids) == 100
