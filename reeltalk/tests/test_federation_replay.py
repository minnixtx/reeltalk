"""Federation of the replay (boost) — replay arc P5, both directions.

Outbound is the ``Announce`` a member's replay becomes; inbound is a peer's
boost of one of our posts arriving as a ``Replay`` row. The contracts, in the
order they bite:

* **The Announce is the replier's news, not the author's.** It is addressed to
  the replier's followers and delivered to exactly those inboxes — the one
  broadcast on the replay route whose audience is not the post's author,
  which is why ``broadcast_replay`` cannot reuse ``broadcast_like`` or
  ``_announce``. A local original rides out too (owner ruling, P5): a
  follower on the peer network sees the member boost another member.
* **The ids are keyed on the replay row, because the peer matches on them.**
  The live peer's ``undo_announce`` finds the boost with
  ``Status.find_by(uri: <inner Announce id>, account: <sender>)``, so the
  ``Undo`` must carry the same id the ``Announce`` did — a fresh uuid per
  call (the ``like_activity`` idiom) could never be matched. A re-replay is a
  new row with a new ``origin_id``, so the sequence still never collides.
* **Inbound keeps ``handle_like``'s two rules verbatim** — the row belongs to
  the verified sender, never ``activity["actor"]``, and an unknown target is
  dropped without a fetch — plus the peer's own ``announceable?``: a replay
  of a status whose audience does not go out is refused unless the booster
  owns it.
* **The author's notification rides on row creation**, once, exactly as the
  Like pair's does.

Outbound assertions use ``responses`` to capture what actually left the
process; inbound activities go through the real inbox route with a real
signature so sender resolution and dedup are exercised, not bypassed.
"""

import json
import logging
from urllib.parse import urlparse

import pytest
import requests
import responses
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory

from reeltalk.activitypub import crypto, signatures
from reeltalk.activitypub.broadcast import broadcast_replay
from reeltalk.activitypub.objects import announce_activity
from reeltalk.core.models import Film, Replay, Status, Visibility, replay_counts
from reeltalk.notifications.models import Notification
from reeltalk.tests.members import member

User = get_user_model()

REMOTE_ACTOR = "https://remote.example/user/carol/"
REMOTE_INBOX = REMOTE_ACTOR.rstrip("/") + "/inbox"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"
FOLLOWER_ACTOR = "https://remote.example/user/dave/"
FOLLOWER_INBOX = FOLLOWER_ACTOR.rstrip("/") + "/inbox"
ALICE_ACTOR = "http://testserver/user/alice/"
ALICE_FOLLOWERS = "http://testserver/user/alice/followers/"
BOB_ACTOR = "http://testserver/user/bob/"
PUBLIC = "https://www.w3.org/ns/activitystreams#Public"
MIRROR_NOTE_URL = "https://remote.example/status/carol-review-1"
INBOX_LOGGER = "reeltalk.activitypub.inbox"


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return member(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


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


def _carol():
    return _remote_user("carol@remote.example", REMOTE_ACTOR)


def _dave(alice):
    """A remote follower of alice — the audience of alice's replays."""
    dave = _remote_user("dave@remote.example", FOLLOWER_ACTOR)
    dave.follows.add(alice)
    return dave


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


def _review(user, film, content="<p>Spice, replayed.</p>", visibility=None):
    status = Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="4.5",
        content=content,
        raw_content="Spice, replayed.",
    )
    if visibility is not None:
        status.visibility = visibility
        status.save(update_fields=["visibility"])
    return status


def _request() -> RequestFactory:
    """A bare request carrying the test-server Host header."""
    request = RequestFactory().get("/")
    request.META["HTTP_HOST"] = "testserver"
    return request


def _login(localname):
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


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


def _deliver(client, private_pem, activity):
    """Send one signed activity through the real inbox route."""
    body = json.dumps(activity).encode()
    return _post_inbox(client, body, _signed_post("/inbox/", body, private_pem))


def _establish_carol(client, private_pem, person_doc):
    """Create the remote mirror through the real first-contact path."""
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/establish-p5",
            "type": "Follow",
            "actor": REMOTE_ACTOR,
            "object": ALICE_ACTOR,
        },
    )
    return User.objects.get(local=False, actor_url=REMOTE_ACTOR)


def _sent(index=0) -> dict:
    """The JSON body of the *index*th outbound request captured by ``responses``."""
    return json.loads(responses.calls[index].request.body)


def _urls() -> list[str]:
    return [call.request.url for call in responses.calls]


# --- The wire shape of an Announce (builder units) ----------------------------


def test_announce_targets_the_originals_home_url_for_a_mirror():
    # The R42 rule again: a mirror is referenced by the URL its own instance
    # gave it, never by one minted here.
    request = _request()
    mirror = Status(
        user=User(localname="carol@remote.example", local=False),
        local=False,
        remote_url=MIRROR_NOTE_URL,
    )
    activity = announce_activity(
        User(localname="alice"), mirror, request, 42, undo=False
    )
    assert activity["object"] == MIRROR_NOTE_URL
    assert "testserver" not in activity["object"]


def test_announce_ids_are_keyed_on_the_replay_row_and_the_undo_repeats_them():
    # The peer's undo_announce matches the boost by the Announce's own id, so
    # the Undo's inner Announce must carry the id the Announce went out under.
    # That is what the Replay row's origin_id was minted for on day one (R41).
    request = _request()
    target = Status(user=User(localname="bob"), local=True)
    target.pk = 77
    target.origin_id = 77
    announce = announce_activity(
        User(localname="alice"), target, request, 42, undo=False
    )
    undo = announce_activity(User(localname="alice"), target, request, 42, undo=True)
    assert announce["id"] == f"{ALICE_ACTOR}#announce-42"
    assert undo["type"] == "Undo"
    assert undo["id"] == f"{ALICE_ACTOR}#undo-announce-42"
    assert undo["object"]["id"] == announce["id"]
    assert undo["object"]["type"] == "Announce"
    assert undo["object"]["object"] == "http://testserver/status/77/"


def test_announce_audience_is_the_repliers_followers_plus_public_for_public():
    # The pair is the replier's followers collection, not the original
    # author's — status_audience cannot answer this row — and the public
    # collection rides in ``to`` so the peer files the boost as public
    # instead of falling through to direct (R145's mapping, one type later).
    request = _request()
    replier = User(localname="alice")
    public_target = Status(
        user=User(localname="bob"), local=True, visibility=Visibility.PUBLIC
    )
    followers_target = Status(
        user=User(localname="bob"), local=True, visibility=Visibility.FOLLOWERS
    )
    public = announce_activity(replier, public_target, request, 1, undo=False)
    narrow = announce_activity(replier, followers_target, request, 1, undo=False)
    assert public["to"] == [PUBLIC]
    assert public["cc"] == [ALICE_FOLLOWERS]
    # The narrower original narrows the Announce with it (D2: a replay
    # inherits the original's audience), and the Undo keeps the same pair so
    # it can never be addressed wider than the act it undoes.
    assert narrow["to"] == [ALICE_FOLLOWERS]
    assert narrow["cc"] == []
    undo = announce_activity(replier, followers_target, request, 1, undo=True)
    assert undo["to"] == narrow["to"]
    assert undo["cc"] == narrow["cc"]


# --- Outbound: delivered, addressed, signed ------------------------------------


@responses.activate
@pytest.mark.django_db
def test_replaying_a_remote_post_announces_to_the_repliers_followers(alice, dune):
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    _dave(alice)
    responses.add(responses.POST, FOLLOWER_INBOX)

    body = json.loads(_login("alice").post(f"/status/{mirror.pk}/replay/").content)
    assert body == {"replayed": True, "count": 1}

    # One send, to the replier's remote follower — and *not* to the original's
    # author: a replay is the replier's news, which is the whole reason
    # broadcast_replay exists beside broadcast_like instead of reusing it.
    assert _urls() == [FOLLOWER_INBOX]
    sent = _sent()
    assert sent["type"] == "Announce"
    assert sent["actor"] == ALICE_ACTOR
    assert sent["to"] == [PUBLIC]
    assert sent["cc"] == [ALICE_FOLLOWERS]
    assert sent["object"] == MIRROR_NOTE_URL
    post = responses.calls[0].request
    assert f'keyid="{ALICE_ACTOR}#main-key"' in post.headers["Signature-Input"]


@responses.activate
@pytest.mark.django_db
def test_replaying_a_local_post_announces_to_the_repliers_remote_followers(
    alice, bob, dune
):
    # Owner ruling, P5: the Announce goes to the replier's remote followers
    # no matter where the original lives, so a Mastodon follower of a member
    # sees that member boost another member's review. The object is our own
    # status URL — the original's wire URL, which is fetchable by the peer.
    status = _review(bob, dune)
    _dave(alice)
    responses.add(responses.POST, FOLLOWER_INBOX)

    _login("alice").post(f"/status/{status.pk}/replay/")

    assert _urls() == [FOLLOWER_INBOX]
    sent = _sent()
    assert sent["type"] == "Announce"
    assert sent["object"] == f"http://testserver/status/{status.origin_id}/"


@responses.activate
@pytest.mark.django_db
def test_a_replay_with_no_remote_followers_sends_nothing(alice, bob, dune):
    # Local followers read the Replay row their feed already renders — the
    # delivery layer skips them as everywhere else, so a replay that involves
    # nobody remote leaves the process not at all.
    status = _review(bob, dune)
    local_follower = member(localname="erin", password="s3cretpass")
    local_follower.follows.add(alice)
    responses.add(responses.POST, "https://remote.example/")

    assert _login("alice").post(f"/status/{status.pk}/replay/").status_code == 200
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_unreplaying_delivers_an_undo_naming_the_announce_it_undoes(alice, dune):
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    _dave(alice)
    responses.add(responses.POST, FOLLOWER_INBOX)

    _login("alice").post(f"/status/{mirror.pk}/replay/")
    assert _login("alice").post(f"/status/{mirror.pk}/replay/").status_code == 200

    assert len(responses.calls) == 2
    announce = _sent(0)
    undo = _sent(1)
    assert undo["type"] == "Undo"
    assert undo["object"]["type"] == "Announce"
    # The pin the live peer forces: its undo_announce finds the boost by the
    # Announce id, so the inner id must be the one that went out first — and
    # the Undo's own id stays distinct so neither dedups the other.
    assert undo["object"]["id"] == announce["id"]
    assert undo["object"]["object"] == MIRROR_NOTE_URL
    assert undo["id"] != announce["id"]


@responses.activate
@pytest.mark.django_db
def test_a_re_replay_announces_under_a_new_id(alice, dune):
    # Replay -> un-replay -> replay is a normal sequence and each must arrive.
    # The new id comes from the new row (delete-then-create mints a fresh
    # origin_id), not from a uuid: the Undo still has to name the old one.
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    _dave(alice)
    responses.add(responses.POST, FOLLOWER_INBOX)

    client = _login("alice")
    client.post(f"/status/{mirror.pk}/replay/")
    client.post(f"/status/{mirror.pk}/replay/")
    client.post(f"/status/{mirror.pk}/replay/")

    first, _undo, third = _sent(0), _sent(1), _sent(2)
    assert first["type"] == "Announce"
    assert third["type"] == "Announce"
    assert first["id"] != third["id"]


@responses.activate
@pytest.mark.django_db
def test_a_private_original_never_reaches_the_wire(alice, bob, dune):
    # The route cannot reach this case (may_replay admits public originals
    # only), so the gate is called directly: defense in depth, the same shape
    # _announce carries for private statuses.
    status = _review(bob, dune, visibility=Visibility.PRIVATE)
    _dave(alice)
    responses.add(responses.POST, "https://remote.example/")

    failures = broadcast_replay(_request(), status, alice, replayed=True, origin_id=7)

    assert failures == []
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_a_followers_only_original_announces_to_the_followers_collection_alone(
    alice, bob, dune
):
    # Also unreachable from the route (D2 admits public only) and pinned at
    # the broadcast so the audience shape exists even where the gate is
    # bypassed: the replier's followers, and no public collection.
    status = _review(bob, dune, visibility=Visibility.FOLLOWERS)
    _dave(alice)
    responses.add(responses.POST, FOLLOWER_INBOX)

    broadcast_replay(_request(), status, alice, replayed=True, origin_id=7)

    assert _urls() == [FOLLOWER_INBOX]
    sent = _sent()
    assert sent["to"] == [ALICE_FOLLOWERS]
    assert sent["cc"] == []


@responses.activate
@pytest.mark.django_db
def test_an_unreachable_follower_does_not_fail_the_replay_request(alice, dune):
    # The local write is the member's; a dead remote is allowed to lag.
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    _dave(alice)
    responses.add(
        responses.POST, FOLLOWER_INBOX, body=requests.ConnectionError("unreachable")
    )

    response = _login("alice").post(f"/status/{mirror.pk}/replay/")
    assert response.status_code == 200
    assert json.loads(response.content) == {"replayed": True, "count": 1}
    assert Replay.objects.filter(user=alice, status=mirror).exists()


@responses.activate
@pytest.mark.django_db
def test_announce_delivery_passes_our_own_signature_verification(alice, dune):
    # The ReelTalk-to-ReelTalk interop guarantee, same pin as the Like pair:
    # what we send is something our own inbox would accept.
    carol = _carol()
    mirror = _mirror_review(dune, carol)
    _dave(alice)
    responses.add(responses.POST, FOLLOWER_INBOX)
    _login("alice").post(f"/status/{mirror.pk}/replay/")

    post = responses.calls[0].request
    parsed = urlparse(FOLLOWER_INBOX)
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


# --- Inbound: a peer's boost of our post ---------------------------------------


@responses.activate
@pytest.mark.django_db
def test_inbound_announce_writes_a_replay_row_for_the_verified_sender(
    client, remote_keypair, person_doc, alice, dune
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, dune)

    response = _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/announce-1",
            "type": "Announce",
            "actor": REMOTE_ACTOR,
            "object": f"http://testserver/status/{status.pk}/",
        },
    )

    assert response.status_code == 202
    assert Replay.objects.filter(user=carol, status=status).count() == 1
    assert replay_counts([status.pk]) == {status.pk: 1}


@responses.activate
@pytest.mark.django_db
def test_inbound_announce_is_keyed_on_the_verified_sender_not_the_declared_actor(
    client, remote_keypair, person_doc, alice, bob, dune
):
    # Asserted in both directions because either alone passes vacuously.
    # Carol signed; bob is named as the ``actor`` throughout and is a real,
    # resolvable local user — if the handler ever trusted the wire over the
    # signature the replay would land on bob and not on carol, and both
    # assertions below would fail. This is the mutation-check target the plan
    # names: forge the actor on purpose and this goes red.
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, dune)

    response = _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/announce-forged",
            "type": "Announce",
            "actor": BOB_ACTOR,
            "object": f"http://testserver/status/{status.pk}/",
        },
    )

    assert response.status_code == 202
    assert Replay.objects.filter(user=carol, status=status).count() == 1
    assert Replay.objects.filter(user=bob, status=status).count() == 0


@responses.activate
@pytest.mark.django_db
def test_a_redelivered_inbound_announce_is_a_duplicate_not_a_second_replay(
    client, remote_keypair, person_doc, alice, dune
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, dune)
    activity = {
        "id": "https://remote.example/activity/announce-2",
        "type": "Announce",
        "actor": REMOTE_ACTOR,
        "object": f"http://testserver/status/{status.pk}/",
    }

    _deliver(client, private_pem, activity)
    _deliver(client, private_pem, activity)

    assert Replay.objects.filter(user=carol, status=status).count() == 1


@responses.activate
@pytest.mark.django_db
def test_a_second_announce_activity_for_the_same_pair_leaves_one_row(
    client, remote_keypair, person_doc, alice, dune
):
    # A different activity id for the same (sender, status): the unique pair
    # is the redelivery key, exactly like Like.
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, dune)
    for n in (1, 2):
        _deliver(
            client,
            private_pem,
            {
                "id": f"https://remote.example/activity/announce-distinct-{n}",
                "type": "Announce",
                "actor": REMOTE_ACTOR,
                "object": f"http://testserver/status/{status.pk}/",
            },
        )

    assert Replay.objects.filter(user=carol, status=status).count() == 1
    assert replay_counts([status.pk]) == {status.pk: 1}


@responses.activate
@pytest.mark.django_db
def test_inbound_announce_for_a_note_we_do_not_have_is_dropped_without_fetching(
    client, remote_keypair, person_doc, caplog
):
    # The no-fetch posture, tested as a *lack* of outbound work, and the drop
    # reason pinned in the log — an interaction that arrived and went nowhere
    # must be readable from the log, not rediscovered.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    before = len(responses.calls)

    with caplog.at_level(logging.INFO, logger=INBOX_LOGGER):
        response = _deliver(
            client,
            private_pem,
            {
                "id": "https://remote.example/activity/announce-unknown",
                "type": "Announce",
                "actor": REMOTE_ACTOR,
                "object": "http://testserver/status/999999/",
            },
        )

    assert response.status_code == 202
    assert Replay.objects.count() == 0
    assert len(responses.calls) == before, "the Announce handler must issue no fetch"
    assert "dropped, no such note here" in caplog.text


@responses.activate
@pytest.mark.django_db
def test_inbound_announce_of_a_private_original_is_refused(
    client, remote_keypair, person_doc, alice, dune, caplog
):
    # The peer's own announceable? refusal, kept verbatim: a replay of a
    # status whose audience does not go out is dropped unless the booster
    # owns it. This is the mutation-check target the plan names — delete the
    # refusal and this test goes red.
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, dune, visibility=Visibility.PRIVATE)

    with caplog.at_level(logging.INFO, logger=INBOX_LOGGER):
        _deliver(
            client,
            private_pem,
            {
                "id": "https://remote.example/activity/announce-private",
                "type": "Announce",
                "actor": REMOTE_ACTOR,
                "object": f"http://testserver/status/{status.pk}/",
            },
        )

    assert Replay.objects.filter(user=carol, status=status).count() == 0
    assert "the original's audience does not go out" in caplog.text


@responses.activate
@pytest.mark.django_db
def test_inbound_announce_of_a_followers_only_original_lands_a_row(
    client, remote_keypair, person_doc, alice, dune
):
    # announceable? refuses the audiences that do not go out; a followers-only
    # original does go out, so the row lands. It is not a widening: every
    # read path filters the *original* (the arc's first failure mode, closed
    # at the query), so this row renders only for readers the original
    # already admits.
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, dune, visibility=Visibility.FOLLOWERS)

    _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/announce-followers",
            "type": "Announce",
            "actor": REMOTE_ACTOR,
            "object": f"http://testserver/status/{status.pk}/",
        },
    )

    assert Replay.objects.filter(user=carol, status=status).count() == 1


@responses.activate
@pytest.mark.django_db
def test_inbound_announce_for_a_deleted_note_is_ignored(
    client, remote_keypair, person_doc, alice, dune
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, dune)
    status.delete()

    _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/announce-tombstone",
            "type": "Announce",
            "actor": REMOTE_ACTOR,
            "object": f"http://testserver/status/{status.pk}/",
        },
    )

    assert Replay.objects.filter(user=carol, status=status).count() == 0


@responses.activate
@pytest.mark.django_db
def test_inbound_undo_announce_removes_the_row(
    client, remote_keypair, person_doc, alice, dune
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, dune)
    note_url = f"http://testserver/status/{status.pk}/"
    _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/announce-undo-a",
            "type": "Announce",
            "actor": REMOTE_ACTOR,
            "object": note_url,
        },
    )
    assert Replay.objects.filter(user=carol, status=status).count() == 1

    response = _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/announce-undo-b",
            "type": "Undo",
            "actor": REMOTE_ACTOR,
            "object": {
                "id": "https://remote.example/activity/announce-undo-a",
                "type": "Announce",
                "actor": REMOTE_ACTOR,
                "object": note_url,
            },
        },
    )

    assert response.status_code == 202
    assert Replay.objects.filter(user=carol, status=status).count() == 0


@responses.activate
@pytest.mark.django_db
def test_inbound_announce_notifies_the_author_once(
    client, remote_keypair, person_doc, alice, dune
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, dune)
    activity = {
        "id": "https://remote.example/activity/announce-notify",
        "type": "Announce",
        "actor": REMOTE_ACTOR,
        "object": f"http://testserver/status/{status.pk}/",
    }

    _deliver(client, private_pem, activity)
    _deliver(client, private_pem, activity)

    notes = Notification.objects.filter(
        recipient=alice, actor=carol, kind=Notification.Kind.REPLAY
    )
    assert notes.count() == 1
    assert notes.get().status == status
