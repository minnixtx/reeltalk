"""Federation, outbound (feed interactions increment 5).

The interactions increments 3 and 4 stored locally now reach other
instances, and the gate that kept them home comes out. Five contracts, in
the order they bite:

* **A ``Like`` names the target's *home* URL.** ``note_reference`` is the
  rule ``film_url`` already applies to films (R42): a mirror's identity is
  the URL its own instance gave it. ``note_url`` builds a URL on our host
  from ``origin_id or pk``, which for a mirror claims our identity for
  someone else's object — and that is exactly what ``inReplyTo`` would have
  emitted once a local reply was allowed to have a mirror parent.
* **Activity ids carry a uuid fragment.** A member can like, unlike and
  re-like; each must arrive. An id derived from the (liker, target) pair
  would make the re-like identical to the first and get it dropped as a
  redelivery.
* **Recipient sets differ per interaction, and getting one wrong is
  invisible until it isn't.** A Like goes to the post's author alone — it
  is addressed to them, not broadcast. A reply goes to the post's author
  **and** the replier's followers, because the author may not follow the
  replier and a reply to a stranger would otherwise never arrive.
* **``Undo(Like)`` inbound is keyed on the verified sender**, never
  ``activity["actor"]``, the same posture every other handler takes, and
  resolves the target only among rows we already have — no fetch.
* **The gate opens in four places at once** (R85): ``FeedEntry.interactive``,
  the like lookup, the reply lookup, and the post page's control gate. The
  tests below pin all four, plus the one place that must *not* open — the
  AP arm of ``status_detail``, which still 404s a mirror because we never
  mint identity for another instance's object.

Outbound assertions use ``responses`` to capture what actually left the
process, not what the builder would have produced: a serializer test proves
the shape, a delivery test proves it was sent, signed, and to whom.
"""

import json
from urllib.parse import urlparse

import pytest
import requests
import responses
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory

from reeltalk.activitypub import crypto, signatures
from reeltalk.activitypub.objects import like_activity, note_document, note_reference
from reeltalk.core.models import Film, Like, Status, feed_entries

User = get_user_model()

REMOTE_ACTOR = "https://remote.example/user/carol/"
REMOTE_INBOX = REMOTE_ACTOR.rstrip("/") + "/inbox"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"
FOLLOWER_ACTOR = "https://remote.example/user/dave/"
FOLLOWER_INBOX = FOLLOWER_ACTOR.rstrip("/") + "/inbox"
ALICE_ACTOR = "http://testserver/user/alice/"
MIRROR_NOTE_URL = "https://remote.example/status/carol-review-1"


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return User.objects.create_user(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def admin(db):
    # R12: / redirects to /setup/ until a superuser exists.
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


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
        "publicKey": {
            "id": REMOTE_KEY_ID,
            "owner": REMOTE_ACTOR,
            "publicKeyPem": public_pem,
        },
    }


def _remote_user(localname: str, actor_url: str) -> User:
    """A remote mirror account, as federation creates it (no local password)."""
    user = User(
        localname=localname,
        local=False,
        actor_url=actor_url,
        inbox_url=actor_url.rstrip("/") + "/inbox",
    )
    user.set_unusable_password()
    user.save()
    return user


def _mirror_review(film, author, remote_url=MIRROR_NOTE_URL):
    return Status.objects.create(
        user=author,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Their review of Dune.</p>",
        local=False,
        remote_url=remote_url,
    )


def _review(user, film, content="<p>Spice must be liked.</p>"):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="4.5",
        content=content,
        raw_content="Spice must be liked.",
    )


def _carol():
    return _remote_user("carol@remote.example", REMOTE_ACTOR)


def _request() -> RequestFactory:
    """A bare request carrying the test-server Host header."""
    request = RequestFactory().get("/")
    request.META["HTTP_HOST"] = "testserver"
    return request


def _login(localname):
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


def _home(client) -> str:
    """The rendered home feed, with the 200 asserted before the body is used."""
    response = client.get("/")
    assert response.status_code == 200
    return response.content.decode()


def _signed_post(path: str, body: bytes, private_pem: str, key_id=REMOTE_KEY_ID):
    """RFC 9421 signature headers for a POST to ``http://testserver{path}``."""
    return signatures.sign_request(
        "POST", f"http://testserver{path}", private_pem, key_id=key_id, body=body
    )


def _post_inbox(client, body: bytes, headers: dict):
    meta = {}
    for name, value in headers.items():
        meta[f"HTTP_{name.upper().replace('-', '_')}"] = value
    meta["HTTP_HOST"] = "testserver"
    return client.post(
        "/inbox/", data=body, content_type="application/activity+json", **meta
    )


def _sent(index=0) -> dict:
    """The JSON body of the *index*th outbound request captured by ``responses``."""
    return json.loads(responses.calls[index].request.body)


def _urls() -> list[str]:
    return [call.request.url for call in responses.calls]


# --- The wire shape of a Like -------------------------------------------------


def test_like_activity_targets_the_targets_home_url():
    # The R42 rule, unit-tested on the builder: a mirror is referenced by
    # the URL its own instance gave it, never by one minted here.
    request = _request()
    mirror = Status(
        user=User(localname="carol@remote.example", local=False),
        local=False,
        remote_url=MIRROR_NOTE_URL,
    )
    activity = like_activity(User(localname="alice"), mirror, request, undo=False)
    assert activity["object"] == MIRROR_NOTE_URL
    assert "testserver" not in activity["object"]


def test_like_activity_targets_our_own_url_for_a_local_post():
    request = _request()
    local = Status(user=User(localname="bob"), local=True)
    local.pk = 77
    local.origin_id = 77
    activity = like_activity(User(localname="alice"), local, request, undo=False)
    assert activity["object"] == "http://testserver/status/77/"


def test_like_activity_ids_are_unique_per_event():
    # Like -> unlike -> re-like is a normal thing a member does. An id
    # derived from (liker, target) would make the re-like identical to the
    # first like and the receiver would drop it as a redelivery.
    request = _request()
    liker = User(localname="alice")
    target = Status(user=User(localname="bob"), local=True)
    ids = set()
    for _ in range(50):
        ids.add(like_activity(liker, target, request, undo=False)["id"])
        ids.add(like_activity(liker, target, request, undo=True)["id"])
    assert len(ids) == 100


def test_undo_wraps_a_like_naming_the_same_target():
    # The shape handle_undo dispatches on, and the shape Mastodon's
    # Undo(Like) uses: the undone activity inline, its object the target.
    request = _request()
    undo = like_activity(
        User(localname="alice"),
        Status(user=User(localname="bob"), local=False, remote_url=MIRROR_NOTE_URL),
        request,
        undo=True,
    )
    assert undo["type"] == "Undo"
    assert undo["object"]["type"] == "Like"
    assert undo["object"]["object"] == MIRROR_NOTE_URL
    assert undo["object"]["actor"] == ALICE_ACTOR


def test_note_reference_is_the_home_url_for_a_mirror_and_ours_for_local():
    request = _request()
    mirror = Status(user=User(localname="c"), local=False, remote_url=MIRROR_NOTE_URL)
    local = Status(user=User(localname="b"), local=True)
    local.pk = 12
    local.origin_id = 12
    assert note_reference(request, mirror) == MIRROR_NOTE_URL
    assert note_reference(request, local) == "http://testserver/status/12/"


@pytest.mark.django_db
def test_a_local_reply_to_a_mirror_serialises_the_parents_home_url():
    # The bug the gate change would have introduced: with the reply route no
    # longer forcing local=True, a local reply can have a mirror parent —
    # and note_url() would have pointed inReplyTo at our own host for an
    # object another instance owns.
    carol = _carol()
    film = Film.objects.create(title="Dune", year=2021)
    mirror = _mirror_review(film, carol)
    reply = Status.objects.create(
        user=User.objects.create_user(localname="alice", password="p"),
        film=film,
        status_type=Status.Type.COMMENT,
        content="<p>Agreed.</p>",
        reply_parent=mirror,
    )
    doc = note_document(reply, _request())
    assert doc["inReplyTo"] == MIRROR_NOTE_URL
    # The reply's own id stays ours — it *is* our object.
    assert doc["id"].startswith("http://testserver/status/")


# --- Outbound Like: delivered, addressed, signed ------------------------------


@responses.activate
@pytest.mark.django_db
def test_liking_a_remote_post_delivers_a_signed_like_to_its_author(alice, dune):
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    responses.add(responses.POST, REMOTE_INBOX)

    body = json.loads(_login("alice").post(f"/status/{mirror.pk}/like/").content)
    assert body == {"liked": True, "count": 1}

    # One send, to the post's author — not to the liker's followers.
    assert _urls() == [REMOTE_INBOX]
    sent = _sent()
    assert sent["type"] == "Like"
    assert sent["actor"] == ALICE_ACTOR
    assert sent["object"] == MIRROR_NOTE_URL
    # Signed with the liker's key: the receiver attributes the like to the
    # verified sender, which is the only identity it can trust.
    post = responses.calls[0].request
    assert f'keyid="{ALICE_ACTOR}#main-key"' in post.headers["Signature-Input"]
    assert post.headers["Content-Type"] == "application/activity+json"


@responses.activate
@pytest.mark.django_db
def test_liking_a_local_post_sends_nothing(alice, bob, dune):
    # The author is local and reads the Like row their own page already
    # shows. A delivery here would be a POST telling them what they can see.
    status = _review(bob, dune)
    responses.add(responses.POST, "https://remote.example/")

    assert _login("alice").post(f"/status/{status.pk}/like/").status_code == 200
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_unliking_a_remote_post_delivers_undo_like(alice, dune):
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    responses.add(responses.POST, REMOTE_INBOX)

    _login("alice").post(f"/status/{mirror.pk}/like/")
    assert _login("alice").post(f"/status/{mirror.pk}/like/").status_code == 200

    assert len(responses.calls) == 2
    undo = _sent(1)
    assert undo["type"] == "Undo"
    assert undo["object"]["type"] == "Like"
    assert undo["object"]["object"] == MIRROR_NOTE_URL
    # A distinct event id from the Like it undoes, so neither dedups the other.
    assert undo["id"] != _sent(0)["id"]


@responses.activate
@pytest.mark.django_db
def test_like_delivery_passes_our_own_signature_verification(alice, dune):
    # The ReelTalk-to-ReelTalk interop guarantee: what we send is something
    # our own inbox would accept, not merely something we think is signed.
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    responses.add(responses.POST, REMOTE_INBOX)
    _login("alice").post(f"/status/{mirror.pk}/like/")

    post = responses.calls[0].request
    parsed = urlparse(REMOTE_INBOX)
    rf_request = RequestFactory().post(
        parsed.path,
        data=post.body,
        content_type="application/activity+json",
        secure=parsed.scheme == "https",
    )
    rf_request.META["HTTP_HOST"] = parsed.netloc
    for header in ("Signature-Input", "Signature", "Content-Digest"):
        meta_key = f"HTTP_{header.upper().replace('-', '_')}"
        rf_request.META[meta_key] = post.headers[header]
    alice.refresh_from_db()
    assert signatures.verify_request(rf_request, alice.public_key) is True


@responses.activate
@pytest.mark.django_db
def test_an_unreachable_author_does_not_fail_the_like_request(alice, dune):
    # The local write is the member's; a dead remote is allowed to lag.
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    responses.add(
        responses.POST, REMOTE_INBOX, body=requests.ConnectionError("unreachable")
    )

    response = _login("alice").post(f"/status/{mirror.pk}/like/")
    assert response.status_code == 200
    assert json.loads(response.content) == {"liked": True, "count": 1}
    assert Like.objects.filter(user=alice, status=mirror).exists()


# --- Inbound Undo(Like) ------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_inbound_undo_like_removes_the_senders_like(client, remote_keypair, person_doc):
    alice = User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    responses.add(responses.POST, REMOTE_INBOX, status=202)  # the Accept (R88)
    private_pem, _public_pem = remote_keypair

    # Establish the mirror through the real first-contact path, then the
    # like it is undoing. (The inbound *Like* handler is increment 6, so
    # the row is written directly — what is under test is the Undo.)
    follow = {
        "id": "https://remote.example/activity/l1a",
        "type": "Follow",
        "actor": REMOTE_ACTOR,
        "object": ALICE_ACTOR,
    }
    body = json.dumps(follow).encode()
    _post_inbox(client, body, _signed_post("/inbox/", body, private_pem))
    carol = User.objects.get(local=False)
    film = Film.objects.create(title="D")
    Like.objects.create(user=carol, status=_review(alice, film))

    undo = {
        "id": "https://remote.example/activity/l1b",
        "type": "Undo",
        "actor": REMOTE_ACTOR,
        "object": {
            "id": "https://remote.example/user/carol/#like-abc",
            "type": "Like",
            "actor": REMOTE_ACTOR,
            "object": f"http://testserver/status/{Like.objects.get().status_id}/",
        },
    }
    body = json.dumps(undo).encode()
    response = _post_inbox(client, body, _signed_post("/inbox/", body, private_pem))

    assert response.status_code == 202
    assert Like.objects.filter(user=carol).count() == 0


@responses.activate
@pytest.mark.django_db
def test_inbound_undo_like_uses_the_verified_sender_not_the_declared_actor(
    client, remote_keypair, person_doc
):
    # Asserted in both directions, because either one alone passes
    # vacuously. The sender's like must go — that proves the branch fires
    # and is keyed on the verified sender. Bob's must survive — and bob is
    # named as the ``actor`` throughout the activity, a real and resolvable
    # local user, so if the handler ever trusted the wire over the
    # signature it would delete the wrong person's like and this fails.
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    responses.add(responses.POST, REMOTE_INBOX, status=202)  # the Accept (R88)
    private_pem, _public_pem = remote_keypair
    status = _review(alice, Film.objects.create(title="D"))
    Like.objects.create(user=bob, status=status)

    follow = {
        "id": "https://remote.example/activity/l2a",
        "type": "Follow",
        "actor": REMOTE_ACTOR,
        "object": ALICE_ACTOR,
    }
    body = json.dumps(follow).encode()
    _post_inbox(client, body, _signed_post("/inbox/", body, private_pem))
    carol = User.objects.get(local=False)
    Like.objects.create(user=carol, status=status)

    bob_actor = "http://testserver/user/bob/"
    undo = {
        "id": "https://remote.example/activity/l2b",
        "type": "Undo",
        "actor": bob_actor,
        "object": {
            "id": f"{bob_actor}#like-xyz",
            "type": "Like",
            "actor": bob_actor,
            "object": f"http://testserver/status/{status.pk}/",
        },
    }
    body = json.dumps(undo).encode()
    response = _post_inbox(client, body, _signed_post("/inbox/", body, private_pem))

    assert response.status_code == 202
    assert Like.objects.filter(user=carol, status=status).count() == 0
    assert Like.objects.filter(user=bob, status=status).count() == 1


@responses.activate
@pytest.mark.django_db
def test_inbound_undo_like_leaves_another_users_like_alone(
    client, remote_keypair, person_doc
):
    alice = User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    responses.add(responses.POST, REMOTE_INBOX, status=202)  # the Accept (R88)
    private_pem, _public_pem = remote_keypair
    follow = {
        "id": "https://remote.example/activity/l3a",
        "type": "Follow",
        "actor": REMOTE_ACTOR,
        "object": ALICE_ACTOR,
    }
    body = json.dumps(follow).encode()
    _post_inbox(client, body, _signed_post("/inbox/", body, private_pem))
    carol = User.objects.get(local=False)
    status = _review(alice, Film.objects.create(title="D"))
    bob = User.objects.create_user(localname="bob", password="p")
    Like.objects.create(user=bob, status=status)
    Like.objects.create(user=carol, status=status)

    undo = {
        "id": "https://remote.example/activity/l3b",
        "type": "Undo",
        "actor": REMOTE_ACTOR,
        "object": {
            "id": "https://remote.example/user/carol/#like-1",
            "type": "Like",
            "actor": REMOTE_ACTOR,
            "object": f"http://testserver/status/{status.pk}/",
        },
    }
    body = json.dumps(undo).encode()
    _post_inbox(client, body, _signed_post("/inbox/", body, private_pem))

    assert Like.objects.filter(user=carol).count() == 0
    assert Like.objects.filter(user=bob, status=status).count() == 1


@responses.activate
@pytest.mark.django_db
def test_inbound_undo_like_for_an_unknown_status_is_ignored_without_fetching(
    client, remote_keypair, person_doc
):
    # No fetch on the undo path: naming an object we do not have cannot pull
    # it in as a side effect of processing an activity.
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    private_pem, _public_pem = remote_keypair
    follow = {
        "id": "https://remote.example/activity/l4a",
        "type": "Follow",
        "actor": REMOTE_ACTOR,
        "object": ALICE_ACTOR,
    }
    body = json.dumps(follow).encode()
    _post_inbox(client, body, _signed_post("/inbox/", body, private_pem))

    undo = {
        "id": "https://remote.example/activity/l4b",
        "type": "Undo",
        "actor": REMOTE_ACTOR,
        "object": {
            "id": "https://remote.example/user/carol/#like-2",
            "type": "Like",
            "actor": REMOTE_ACTOR,
            "object": "http://testserver/status/999999/",
        },
    }
    body = json.dumps(undo).encode()
    response = _post_inbox(client, body, _signed_post("/inbox/", body, private_pem))

    assert response.status_code == 202
    assert Like.objects.count() == 0
    # Only the Person fetch from establishing the mirror — the undo fetched nothing.
    assert [call.request.method for call in responses.calls] == ["GET"]


# --- The threaded Create ------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_reply_is_broadcast_with_in_reply_to_pointing_at_the_parent(alice, bob, dune):
    dave = _remote_user("dave@remote.example", FOLLOWER_ACTOR)
    dave.follows.add(alice)
    status = _review(bob, dune)
    responses.add(responses.POST, FOLLOWER_INBOX)

    response = _login("alice").post(
        f"/status/{status.pk}/reply/", {"content": "Agreed."}
    )
    assert response.status_code == 200

    assert _urls() == [FOLLOWER_INBOX]
    sent = _sent()
    assert sent["type"] == "Create"
    parent_url = f"http://testserver/status/{status.origin_id}/"
    assert sent["object"]["inReplyTo"] == parent_url


@responses.activate
@pytest.mark.django_db
def test_a_reply_to_a_mirror_carries_the_parents_home_url_not_ours(alice, dune):
    # The case the gate change opens up, and the reason note_reference had to
    # exist. Pointing inReplyTo at our own mirror URL would make the thread
    # unresolvable on the instance that actually owns the turn.
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    responses.add(responses.POST, REMOTE_INBOX)

    response = _login("alice").post(f"/status/{mirror.pk}/reply/", {"content": "Yes."})
    assert response.status_code == 200

    sent = _sent()
    assert sent["object"]["inReplyTo"] == MIRROR_NOTE_URL
    assert "testserver" not in sent["object"]["inReplyTo"]
    # The reply itself is our object, so its id is ours.
    assert sent["object"]["id"].startswith("http://testserver/status/")


@responses.activate
@pytest.mark.django_db
def test_a_reply_reaches_the_author_who_does_not_follow_the_replier(alice, dune):
    # The reason the author is added explicitly rather than relying on the
    # follower fan-out: a reply to a stranger would otherwise vanish.
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    assert carol.follows.filter(pk=alice.pk).count() == 0
    responses.add(responses.POST, REMOTE_INBOX)

    _login("alice").post(f"/status/{mirror.pk}/reply/", {"content": "Yes."})

    assert _urls() == [REMOTE_INBOX]
    assert _sent()["object"]["inReplyTo"] == MIRROR_NOTE_URL


@responses.activate
@pytest.mark.django_db
def test_a_reply_reaches_the_repliers_remote_followers(alice, dune):
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    dave = _remote_user("dave@remote.example", FOLLOWER_ACTOR)
    dave.follows.add(alice)
    responses.add(responses.POST, REMOTE_INBOX)
    responses.add(responses.POST, FOLLOWER_INBOX)

    _login("alice").post(f"/status/{mirror.pk}/reply/", {"content": "Yes."})

    assert set(_urls()) == {REMOTE_INBOX, FOLLOWER_INBOX}


@responses.activate
@pytest.mark.django_db
def test_a_parent_who_already_follows_the_replier_is_posted_to_once(alice, dune):
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    carol.follows.add(alice)  # author and follower are the same person now
    responses.add(responses.POST, REMOTE_INBOX)

    _login("alice").post(f"/status/{mirror.pk}/reply/", {"content": "Yes."})

    assert _urls() == [REMOTE_INBOX]


@responses.activate
@pytest.mark.django_db
def test_a_reply_with_no_remote_anywhere_sends_nothing(alice, bob, dune):
    status = _review(bob, dune)
    responses.add(responses.POST, "https://remote.example/")

    response = _login("alice").post(f"/status/{status.pk}/reply/", {"content": "Yes."})
    assert response.status_code == 200
    assert len(responses.calls) == 0


# --- The gate, in all four places ---------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_the_like_endpoint_now_accepts_a_remote_post(alice, dune):
    # Was: 404. The route refuses what the page withholds, and the page no
    # longer withholds it, because the delivery now exists.
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    responses.add(responses.POST, REMOTE_INBOX)

    response = _login("alice").post(f"/status/{mirror.pk}/like/")
    assert response.status_code == 200
    assert json.loads(response.content) == {"liked": True, "count": 1}


@pytest.mark.django_db
def test_a_mirror_feed_row_now_carries_the_like_control(alice, dune, admin):
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    alice.follows.add(carol)
    entry = next(e for e in feed_entries(alice) if e.user == carol)
    assert entry.interactive is True
    body = _home(_login("alice"))
    assert "Their review of Dune." in body  # the row really rendered…
    assert f'data-url="/status/{mirror.pk}/like/"' in body  # …and carries the control


@pytest.mark.django_db
def test_a_mirror_post_page_now_carries_the_like_control(alice, dune):
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    body = _login("alice").get(f"/status/{mirror.pk}/").content.decode()
    assert "Their review of Dune." in body
    assert f'data-url="/status/{mirror.pk}/like/"' in body


@responses.activate
@pytest.mark.django_db
def test_the_reply_endpoint_now_accepts_a_remote_post(alice, dune):
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    responses.add(responses.POST, REMOTE_INBOX)

    response = _login("alice").post(f"/status/{mirror.pk}/reply/", {"content": "Yes."})
    assert response.status_code == 200
    assert Status.objects.filter(reply_parent=mirror).count() == 1


@pytest.mark.django_db
def test_a_mirror_post_page_now_offers_the_composer(alice, dune):
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    body = _login("alice").get(f"/status/{mirror.pk}/").content.decode()
    assert f'action="/status/{mirror.pk}/reply/"' in body


@pytest.mark.django_db
def test_a_post_with_no_film_offers_no_composer_and_the_route_refuses_one(alice, dune):
    # The one place the gate stays shut, and it stays shut on both sides.
    # A reply inherits its film; Status.save refuses a typed status with
    # none, so a mirrored note that arrived with no film reference cannot
    # be replied to at all. Offering the composer there would be R85's bug
    # with the signs reversed — an offer the route is obliged to refuse.
    carol = _carol()
    filmless = Status.objects.create(
        user=carol,
        film=None,
        status_type=None,
        content="<p>A note about nothing in particular.</p>",
        local=False,
        remote_url="https://remote.example/status/filmless",
    )
    body = _login("alice").get(f"/status/{filmless.pk}/").content.decode()
    assert "A note about nothing in particular." in body  # the page rendered…
    assert "reply-form" not in body  # …with no composer

    response = _login("alice").post(f"/status/{filmless.pk}/reply/", {"content": "hi"})
    assert response.status_code == 400
    assert Status.objects.filter(reply_parent=filmless).count() == 0


@pytest.mark.django_db
def test_the_like_control_keeps_its_script_where_there_is_no_composer(alice, dune):
    # The scripts used to live inside the composer's gate. Since the like
    # control renders on pages that carry no composer, that placement would
    # have left the button on this page inert — present, clickable, dead.
    carol = _carol()
    filmless = Status.objects.create(
        user=carol,
        film=None,
        status_type=None,
        content="<p>A note about nothing in particular.</p>",
        local=False,
        remote_url="https://remote.example/status/filmless2",
    )
    body = _login("alice").get(f"/status/{filmless.pk}/").content.decode()
    assert "like-btn" in body
    assert "js/likes.js" in body
    assert "reply-form" not in body


# --- The gate that must NOT open -----------------------------------------------


@pytest.mark.django_db
def test_the_ap_arm_still_refuses_a_mirror_after_the_gate_opened(alice, dune):
    # Opening the interaction gate says nothing about identity. We never mint
    # a wire document for another instance's object (R41/R42), so the AP
    # arm of status_detail keeps its 404 for a mirror while the HTML arm
    # still serves it.
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    ap = Client().get(f"/status/{mirror.pk}/", HTTP_ACCEPT="application/activity+json")
    assert ap.status_code == 404
    assert Client().get(f"/status/{mirror.pk}/").status_code == 200
