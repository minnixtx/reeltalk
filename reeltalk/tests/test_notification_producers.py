"""The six producers that write a notification (notifications increment 2, R92).

Increment 1 built the door; this increment walks through it from every side
an event can arrive on. Likes and replies each have **two** producers — a
local route and an inbox handler — and R92 forbids wiring one and leaving
the other dark, because the dark half announces nothing: the ledger reads
fine for whatever traffic does arrive, and the missing half surfaces only as
someone who never hears that a reply landed.

The three shapes every test here is built around:

* **Every absence carries a control that could actually have failed.** "No
  row was written" is also what a producer that never writes at all looks
  like, so each absence is paired either with the same call that *must*
  write, or with proof that the handler ran past every drop on its way to
  the notify (R79).
* **The real entry point.** Each producer is driven through its HTTP route
  or a signed inbox delivery, never by calling the helper directly. A guard
  that only works when the helper is called by hand is not a guard.
* **Redelivery is pinned, not trusted.** What keeps a retried ``Like`` from
  doubling the ledger is the inbox running the dedup row and the handler in
  one transaction — a property this increment newly depends on and
  increment 1 had no producer to test it with.

Where a producer and ``notify()`` could each have stopped something, the
tests record which one did. The local follow route refuses a self-follow;
the like and reply routes refuse nothing. No local route checks blocks at
all — blocks are read-side (M5 increment 3) — so the write-time guard in
``notify()`` is the only thing between a blocked member's action and
someone's ledger, which is why the block cases go through the routes rather
than being left to the model test.

Note that establishing the remote mirror sends a ``Follow`` at alice, which
is itself one of the six producers. The federated tests therefore count by
``kind`` so the establishing follow's own row can neither mask a result nor
stand in for one.
"""

import json

import pytest
import responses
from django.contrib.auth import get_user_model

from reeltalk.activitypub import crypto, signatures
from reeltalk.core.models import Film, Like, Status
from reeltalk.notifications.models import Notification

User = get_user_model()

REMOTE_ACTOR = "https://remote.example/user/carol/"
REMOTE_INBOX = REMOTE_ACTOR.rstrip("/") + "/inbox"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"
ALICE_ACTOR = "http://testserver/user/alice/"


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
def post(db, alice, dune):
    return Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>A review of Dune.</p>",
        raw_content="A review of Dune.",
    )


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


# --- The signed-inbox plumbing (same shape as test_federation_inbound) ------


def _signed_post(path: str, body: bytes, private_pem: str, key_id=REMOTE_KEY_ID):
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
    """Create the remote mirror through the real first-contact path.

    The Follow that does it is a producer in its own right, so callers get a
    mirror *and* one follow notification at alice.
    """
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/establish-1",
            "type": "Follow",
            "actor": REMOTE_ACTOR,
            "object": ALICE_ACTOR,
        },
    )
    return User.objects.get(local=False, actor_url=REMOTE_ACTOR)


def _like_activity(status, activity_id):
    return {
        "id": activity_id,
        "type": "Like",
        "actor": REMOTE_ACTOR,
        "object": f"http://testserver/status/{status.pk}/",
    }


def _follow_activity(activity_id):
    return {
        "id": activity_id,
        "type": "Follow",
        "actor": REMOTE_ACTOR,
        "object": ALICE_ACTOR,
    }


def _note(url, content, **extra):
    doc = {
        "id": url,
        "type": "Note",
        "attributedTo": REMOTE_ACTOR,
        "content": content,
        "publishedTime": "2026-09-24T10:00:00Z",
    }
    doc.update(extra)
    return doc


def _create(note):
    return {
        "id": f"{REMOTE_ACTOR}#create-{note['id']}",
        "type": "Create",
        "actor": REMOTE_ACTOR,
        "object": note,
    }


def _update(note):
    return {
        "id": f"{REMOTE_ACTOR}#update-{note['id']}",
        "type": "Update",
        "actor": REMOTE_ACTOR,
        "object": note,
    }


def rows(recipient, kind, status=None):
    query = Notification.objects.filter(recipient=recipient, kind=kind)
    return query.filter(status=status) if status is not None else query


def _review(user, film, content):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="3",
        content=content,
        raw_content=content,
    )


# --- Producer 1: the local like route ---------------------------------------


def test_liking_someone_elses_post_notifies_the_author(client, alice, bob, post):
    client.login(username="bob", password="s3cretpass")

    response = client.post(f"/status/{post.pk}/like/")

    assert response.status_code == 200
    note = rows(alice, "like", post).get()
    assert note.actor_id == bob.pk


def test_liking_your_own_post_notifies_nobody(client, alice, bob, post):
    # ``like_status`` carries no self-guard of its own, so this call really
    # does reach the producer. Only notify() stands between a member and a
    # badge for their own like.
    client.login(username="alice", password="s3cretpass")

    response = client.post(f"/status/{post.pk}/like/")

    assert response.status_code == 200
    assert json.loads(response.content)["liked"] is True
    assert Notification.objects.count() == 0
    # Control: the same route with a different viewer writes.
    client.login(username="bob", password="s3cretpass")
    assert client.post(f"/status/{post.pk}/like/").status_code == 200
    assert Notification.objects.count() == 1


def test_taking_a_like_back_does_not_notify(client, alice, bob, post):
    client.login(username="bob", password="s3cretpass")
    client.post(f"/status/{post.pk}/like/")
    assert rows(alice, "like", post).count() == 1

    response = client.post(f"/status/{post.pk}/like/")

    assert json.loads(response.content)["liked"] is False
    # The toggle off is the absence of a like, not a second event to file.
    assert rows(alice, "like", post).count() == 1
    assert Notification.objects.count() == 1


def test_a_blocked_member_liking_your_post_notifies_nobody(client, alice, bob, dune):
    # No local route checks blocks — they are read-side — so the write-time
    # guard is doing all the work here, and it is the only thing that could.
    blocked_from = _review(alice, dune, "<p>Blocked from this.</p>")
    alice.blocks.add(bob)
    client.login(username="bob", password="s3cretpass")

    response = client.post(f"/status/{blocked_from.pk}/like/")

    assert response.status_code == 200
    # The like itself still records. A block does not erase what happened, it
    # stops this one reaching her.
    assert Like.objects.filter(user=bob, status=blocked_from).exists()
    assert Notification.objects.count() == 0
    # Control: block lifted, same actor, same route. It needs its own post —
    # a second call against the one above would be the toggle *off*, which
    # notifies nothing either way and would prove nothing. Its own film as
    # well: D5 lets alice review a given film only once.
    alice.blocks.remove(bob)
    other_film = Film.objects.create(title="Dune Part Two", year=2024)
    allowed = _review(alice, other_film, "<p>Not blocked from this.</p>")
    assert client.post(f"/status/{allowed.pk}/like/").status_code == 200
    assert Notification.objects.count() == 1


# --- Producer 2: the local reply route --------------------------------------


def test_replying_to_a_post_notifies_its_author(client, alice, bob, post):
    client.login(username="bob", password="s3cretpass")

    response = client.post(f"/status/{post.pk}/reply/", {"content": "Agreed."})

    assert response.status_code == 200
    note = rows(alice, "reply").get()
    assert note.actor_id == bob.pk
    # The row links the reply, not the post answered: the page deep-links to
    # the reply's own post page.
    assert note.status_id != post.pk
    assert note.status.reply_parent_id == post.pk


def test_replying_to_your_own_post_notifies_nobody(client, alice, bob, post):
    client.login(username="alice", password="s3cretpass")

    response = client.post(
        f"/status/{post.pk}/reply/", {"content": "Adding to myself."}
    )

    assert response.status_code == 200
    assert Notification.objects.count() == 0
    # Control: same route, same parent, someone else replies.
    client.login(username="bob", password="s3cretpass")
    assert (
        client.post(f"/status/{post.pk}/reply/", {"content": "A stranger."})
    ).status_code == 200
    assert Notification.objects.count() == 1


# --- Producer 3: the local follow route -------------------------------------


def test_following_a_member_notifies_them(client, alice, bob):
    client.login(username="bob", password="s3cretpass")

    response = client.post("/user/alice/follow/")

    assert response.status_code == 302
    note = Notification.objects.get(recipient=alice, kind="follow")
    assert note.actor_id == bob.pk
    assert note.status is None


def test_unfollowing_does_not_notify(client, alice, bob):
    client.login(username="bob", password="s3cretpass")
    client.post("/user/alice/follow/")
    assert Notification.objects.count() == 1

    assert client.post("/user/alice/unfollow/").status_code == 302

    assert not bob.follows.filter(pk=alice.pk).exists()
    assert Notification.objects.count() == 1


def test_following_yourself_notifies_nobody(client, alice):
    client.login(username="alice", password="s3cretpass")

    assert client.post("/user/alice/follow/").status_code == 302
    assert Notification.objects.count() == 0
    # Control: the route writes for a non-self follow.
    User.objects.create_user(localname="bob", password="s3cretpass")
    assert client.post("/user/bob/follow/").status_code == 302
    assert Notification.objects.count() == 1


def test_a_blocked_actor_following_you_notifies_nobody(client, alice, bob):
    # The local follow route refuses only a self-follow, so the M2M really
    # is written and only notify() stops the ledger row.
    alice.blocks.add(bob)
    client.login(username="bob", password="s3cretpass")

    assert client.post("/user/alice/follow/").status_code == 302
    assert bob.follows.filter(pk=alice.pk).exists()
    assert Notification.objects.count() == 0
    # Control: block lifted, same actor, same route.
    alice.blocks.remove(bob)
    assert client.post("/user/alice/follow/").status_code == 302
    assert Notification.objects.count() == 1


# --- Producer 4: the inbound Like -------------------------------------------


@responses.activate
def test_a_remote_like_of_our_post_notifies_the_author(
    client, remote_keypair, person_doc, alice, post
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)

    response = _deliver(
        client,
        private_pem,
        _like_activity(post, "https://remote.example/activity/notify-like-1"),
    )

    assert response.status_code == 202
    note = rows(alice, "like", post).get()
    assert note.actor_id == carol.pk


@responses.activate
def test_a_redelivered_inbound_like_writes_one_notification_not_two(
    client, remote_keypair, person_doc, alice, post
):
    # The contract R92 leans on. What prevents the double is not the Like's
    # own unique pair — it is the inbox running the dedup row and the
    # handler in one transaction, so the second delivery is answered
    # "duplicate" before handle_like runs at all.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    activity = _like_activity(post, "https://remote.example/activity/like-redelivered")

    _deliver(client, private_pem, activity)
    # Non-vacuity asserted *before* the retry: without this, the count below
    # would be satisfied just as well by a producer that never wrote.
    assert rows(alice, "like", post).count() == 1

    _deliver(client, private_pem, activity)

    assert rows(alice, "like", post).count() == 1
    assert Like.objects.filter(status=post).count() == 1


@responses.activate
def test_a_second_like_activity_for_a_pair_we_hold_does_not_notify_again(
    client, remote_keypair, person_doc, alice, post
):
    # A different activity id for the same (sender, status), so the dedup
    # row does not stop it. The gate is handle_like's own: the event is the
    # like arriving, not another envelope describing a like we already hold.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)

    _deliver(
        client,
        private_pem,
        _like_activity(post, "https://remote.example/activity/like-first"),
    )
    assert rows(alice, "like", post).count() == 1

    _deliver(
        client,
        private_pem,
        _like_activity(post, "https://remote.example/activity/like-second"),
    )

    assert rows(alice, "like", post).count() == 1


@responses.activate
def test_a_remote_like_of_a_mirrored_post_notifies_nobody(
    db, client, remote_keypair, person_doc
):
    # status.user is a remote mirror: no account here can read the row, and
    # nothing will ever clear it. The Like row being written is the control —
    # it proves the handler ran past both drops and reached the notify, so
    # the empty ledger is the guard and not an early return.
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    them = User.objects.create_user(
        localname="someone@elsewhere.example", password="s3cretpass", local=False
    )
    mirror = Status.objects.create(
        user=them,
        film=Film.objects.create(title="Dune"),
        status_type=Status.Type.REVIEW,
        rating="3",
        content="<p>Their review.</p>",
        local=False,
        remote_url="https://remote.example/status/mirrored-target",
    )

    _deliver(
        client,
        private_pem,
        _like_activity(mirror, "https://remote.example/activity/like-mirror"),
    )

    assert Like.objects.filter(user=carol, status=mirror).count() == 1
    assert Notification.objects.filter(recipient=them).count() == 0


@responses.activate
def test_a_remote_like_from_someone_the_author_blocked_notifies_nobody(
    client, remote_keypair, person_doc, alice, post
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    alice.blocks.add(carol)

    _deliver(
        client,
        private_pem,
        _like_activity(post, "https://remote.example/activity/like-blocked"),
    )

    # Again: the like landed, the notification did not.
    assert Like.objects.filter(user=carol, status=post).count() == 1
    assert rows(alice, "like", post).count() == 0


# --- Producer 5: the inbound reply mirror -----------------------------------


@responses.activate
def test_a_remote_reply_to_our_post_notifies_its_author(
    client, remote_keypair, person_doc, alice, post
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)

    response = _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/notify-reply-1",
                "<p>Agreed, and the sandworms.</p>",
                inReplyTo=f"http://testserver/status/{post.pk}/",
            )
        ),
    )

    assert response.status_code == 202
    note = rows(alice, "reply").get()
    assert note.actor_id == carol.pk
    assert note.status.reply_parent_id == post.pk


@responses.activate
def test_an_update_to_a_remote_reply_does_not_notify_again(
    client, remote_keypair, person_doc, alice, post
):
    # The trap _mirror_status carries: one function serves both create and
    # update of a mirror. An edit to a reply nobody asked about is not a
    # fresh "replied to you", so the notify sits on the create branch only.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    note_doc = _note(
        "https://remote.example/status/notify-reply-edited",
        "<p>First draft.</p>",
        inReplyTo=f"http://testserver/status/{post.pk}/",
    )

    _deliver(client, private_pem, _create(note_doc))
    assert rows(alice, "reply").count() == 1

    edited = dict(
        note_doc, content="<p>An edited reply.</p>", editedTime="2026-09-24T11:00:00Z"
    )
    _deliver(client, private_pem, _update(edited))

    # The mirror really did update, so the count holding at one is the
    # branch being right rather than a delivery that changed nothing.
    mirror = Status.objects.get(
        local=False, remote_url="https://remote.example/status/notify-reply-edited"
    )
    assert mirror.content == "<p>An edited reply.</p>"
    assert rows(alice, "reply").count() == 1


@responses.activate
def test_a_top_level_remote_note_notifies_nobody(
    client, remote_keypair, person_doc, alice
):
    # No parent means no one the note answered. Without the ``parent is not
    # None`` test every mirrored note would notify whoever it happened to
    # share a film with.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)

    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/notify-top-level",
                "<p>A standalone note, threaded to nothing.</p>",
            )
        ),
    )

    assert Status.objects.filter(
        local=False, remote_url="https://remote.example/status/notify-top-level"
    ).exists()
    assert Notification.objects.filter(kind="reply").count() == 0
    # Control: the same note shape with a parent does notify.
    dune = Film.objects.create(title="Dune")
    theirs = _review(alice, dune, "<p>A post to reply to.</p>")
    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/notify-threaded",
                "<p>Threaded after all.</p>",
                inReplyTo=f"http://testserver/status/{theirs.pk}/",
            )
        ),
    )
    assert Notification.objects.filter(kind="reply").count() == 1


# --- Producer 6: the inbound Follow -----------------------------------------


@responses.activate
def test_a_remote_follow_of_our_member_notifies_them(
    client, remote_keypair, person_doc, alice
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)

    note = rows(alice, "follow").get()
    assert note.actor_id == carol.pk
    assert note.status is None
    # The accepted path is what wrote it: the relationship really was made.
    assert carol.follows.filter(pk=alice.pk).exists()


@responses.activate
def test_a_blocked_remote_sender_gets_a_reject_and_no_notification(
    client, remote_keypair, person_doc, alice
):
    # The blocked branch returns early with a Reject. Were the notify placed
    # above that return, a refused follow would still land in the ledger.
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    assert rows(alice, "follow").count() == 1
    alice.blocks.add(carol)

    _deliver(
        client,
        private_pem,
        _follow_activity("https://remote.example/activity/follow-blocked"),
    )

    assert rows(alice, "follow").count() == 1
    # Control: block lifted, the same route and sender write again.
    alice.blocks.remove(carol)
    _deliver(
        client,
        private_pem,
        _follow_activity("https://remote.example/activity/follow-allowed"),
    )
    assert rows(alice, "follow").count() == 2


# --- The pair property ------------------------------------------------------


@responses.activate
def test_a_like_and_a_reply_from_the_same_remote_are_two_rows_not_one(
    client, remote_keypair, person_doc, alice, post
):
    # R90 names three events, and one person producing two of them files
    # both. A producer that upserted on (recipient, actor) instead of on the
    # event would collapse these into one.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)

    _deliver(
        client,
        private_pem,
        _like_activity(post, "https://remote.example/activity/pair-like"),
    )
    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/pair-reply",
                "<p>And a reply as well.</p>",
                inReplyTo=f"http://testserver/status/{post.pk}/",
            )
        ),
    )

    assert rows(alice, "like").count() == 1
    assert rows(alice, "reply").count() == 1
