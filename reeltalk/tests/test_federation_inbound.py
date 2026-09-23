"""Federation, inbound (increment 6).

What arrives at our inbox from the other side, in the order the four
contracts bite:

* **A ``Like`` is keyed on the verified sender, never ``activity["actor"]``.**
  A Like carries no content, so forging one costs the attacker nothing and
  leaves the instance choosing which identity to believe. The rule was
  already load-bearing in the direction that *deletes* (increment 5's
  ``Undo(Like)``); this is the direction that *creates*, and the tests name
  a real local user as the declared actor so a handler that trusted the
  wire would delete-or-create on the wrong account and fail loudly.
* **An unknown Like target is dropped, not fetched** (owner decision). The
  cost of fetching is a Note, its Film, and that film's poster — driven off
  the lowest-value signal on the wire — and it hands any peer our outbound
  request budget for whatever id they care to name. Mastodon's own handler
  drops the same way.
* **``inReplyTo`` is read on ingest**, so remote conversations arrive as
  threads instead of a pile of top-level notes, and a reply inherits its
  parent's film anchor the way a locally composed reply does.
* **``Accept`` and ``Reject`` are handled.** Accept changes nothing because
  we hold no pending state — but it is registered so the outcome is
  recorded rather than swallowed. ``Reject`` undoes the local follow,
  taking the followed side from the verified sender rather than the inner
  ``object``, which is the same split Mastodon's own reject handler makes.

Every activity here goes through the real inbox route with a real
signature, so the sender resolution and the dedup layer are exercised
rather than bypassed.
"""

import json

import pytest
import responses
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from reeltalk.activitypub import crypto, signatures
from reeltalk.activitypub.inbox import HANDLERS, process_inbound_activity
from reeltalk.core.models import (
    REPLY_THREAD_MAX_DEPTH,
    Film,
    Like,
    Status,
    conversation,
    like_counts,
)

User = get_user_model()

REMOTE_ACTOR = "https://remote.example/user/carol/"
REMOTE_INBOX = REMOTE_ACTOR.rstrip("/") + "/inbox"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"
OTHER_ACTOR = "https://other.example/user/zed/"
ALICE_ACTOR = "http://testserver/user/alice/"
BOB_ACTOR = "http://testserver/user/bob/"
INBOX_LOGGER = "reeltalk.activitypub.inbox"


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


def _review(user, film, content="<p>Spice, reviewed.</p>"):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="4",
        content=content,
        raw_content="Spice, reviewed.",
    )


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

    A Follow from carol to alice arrives, is verified against carol's
    Person document, and makes carol a mirror here. The Accept we send back
    is part of the path (R88), so it is registered in ``responses``.
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


def _note(url, content, **extra):
    doc = {
        "id": url,
        "type": "Note",
        "attributedTo": REMOTE_ACTOR,
        "content": content,
        "publishedTime": "2026-09-23T10:00:00Z",
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


# --- The registry -------------------------------------------------------------


def test_like_accept_and_reject_are_now_registered_handlers():
    # Before increment 6 an inbound Like was the same "ignored" as a typo,
    # and the answer to a Follow we sent was discarded as noise.
    assert set(HANDLERS) == {
        "Follow",
        "Undo",
        "Create",
        "Update",
        "Delete",
        "Like",
        "Accept",
        "Reject",
    }


# --- Inbound Like -------------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_inbound_like_records_a_like_for_the_verified_sender(
    client, remote_keypair, person_doc
):
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, Film.objects.create(title="Dune"))

    response = _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/like-1",
            "type": "Like",
            "actor": REMOTE_ACTOR,
            "object": f"http://testserver/status/{status.pk}/",
        },
    )

    assert response.status_code == 202
    assert Like.objects.filter(user=carol, status=status).count() == 1
    assert like_counts([status.pk]) == {status.pk: 1}


@responses.activate
@pytest.mark.django_db
def test_inbound_like_is_keyed_on_the_verified_sender_not_the_declared_actor(
    client, remote_keypair, person_doc
):
    # Asserted in both directions because either alone passes vacuously.
    # Carol signed; bob is named as the ``actor`` throughout the activity
    # and is a real, resolvable local user. If the handler ever trusted the
    # wire over the signature the like would land on bob and not on carol,
    # and both assertions below would fail. Checking only that bob gets
    # nothing would pass under a handler that created nothing at all.
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, Film.objects.create(title="Dune"))

    response = _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/like-2",
            "type": "Like",
            "actor": BOB_ACTOR,
            "object": f"http://testserver/status/{status.pk}/",
        },
    )

    assert response.status_code == 202
    assert Like.objects.filter(user=carol, status=status).count() == 1
    assert Like.objects.filter(user=bob, status=status).count() == 0


@responses.activate
@pytest.mark.django_db
def test_a_redelivered_inbound_like_is_a_duplicate_not_a_second_like(
    client, remote_keypair, person_doc
):
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, Film.objects.create(title="Dune"))
    activity = {
        "id": "https://remote.example/activity/like-3",
        "type": "Like",
        "actor": REMOTE_ACTOR,
        "object": f"http://testserver/status/{status.pk}/",
    }

    _deliver(client, private_pem, activity)
    _deliver(client, private_pem, activity)

    assert Like.objects.filter(user=carol, status=status).count() == 1


@responses.activate
@pytest.mark.django_db
def test_a_second_like_activity_for_the_same_pair_leaves_one_row(
    client, remote_keypair, person_doc
):
    # A different activity id for the same (sender, status): the binary
    # like rule (R83 decision 4) is the unique pair, so the second one
    # converges on the existing row rather than double-counting.
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, Film.objects.create(title="Dune"))
    for n in (1, 2):
        _deliver(
            client,
            private_pem,
            {
                "id": f"https://remote.example/activity/like-distinct-{n}",
                "type": "Like",
                "actor": REMOTE_ACTOR,
                "object": f"http://testserver/status/{status.pk}/",
            },
        )

    assert Like.objects.filter(user=carol, status=status).count() == 1
    assert like_counts([status.pk]) == {status.pk: 1}


@responses.activate
@pytest.mark.django_db
def test_inbound_like_for_a_note_we_do_not_have_is_dropped_without_fetching(
    client, remote_keypair, person_doc
):
    # The owner decision, tested as a *lack* of outbound work: only the
    # Person fetch from establishing the mirror may leave this process.
    # ``responses`` raises on an unregistered URL, so a fetching handler
    # would fail here even without the explicit assertion.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    before = len(responses.calls)

    response = _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/like-unknown",
            "type": "Like",
            "actor": REMOTE_ACTOR,
            "object": "http://testserver/status/999999/",
        },
    )

    assert response.status_code == 202
    assert Like.objects.count() == 0
    assert len(responses.calls) == before, "the Like handler must issue no fetch"


@responses.activate
@pytest.mark.django_db
def test_inbound_like_for_a_deleted_note_is_ignored(client, remote_keypair, person_doc):
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    status = _review(alice, Film.objects.create(title="Dune"))
    status.delete()

    _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/like-tombstone",
            "type": "Like",
            "actor": REMOTE_ACTOR,
            "object": f"http://testserver/status/{status.pk}/",
        },
    )

    assert Like.objects.filter(user=carol, status=status).count() == 0


@responses.activate
@pytest.mark.django_db
def test_inbound_like_on_a_mirrored_post_records_against_the_mirror(
    client, remote_keypair, person_doc
):
    # A remote like of a remote post: both rows are mirrors here, and the
    # Like still resolves by the target's stored home URL.
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    film = Film.objects.create(title="Dune")
    alice = User.objects.create_user(localname="alice", password="p")
    mirror = Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="3",
        content="<p>Their review.</p>",
        local=False,
        remote_url="https://remote.example/status/carol-note-9",
    )

    _deliver(
        client,
        private_pem,
        {
            "id": "https://remote.example/activity/like-mirror",
            "type": "Like",
            "actor": REMOTE_ACTOR,
            "object": "https://remote.example/status/carol-note-9",
        },
    )

    assert Like.objects.filter(user=carol, status=mirror).count() == 1


# --- Inbound threading --------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_remote_reply_gets_a_reply_parent_instead_of_landing_flat(
    client, remote_keypair, person_doc
):
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    parent = _review(alice, Film.objects.create(title="Dune"))
    parent_url = f"http://testserver/status/{parent.pk}/"

    response = _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/reply-1",
                "<p>Agreed, and the sandworms.</p>",
                inReplyTo=parent_url,
            )
        ),
    )

    assert response.status_code == 202
    child = Status.objects.get(
        local=False, remote_url="https://remote.example/status/reply-1"
    )
    assert child.reply_parent_id == parent.pk
    assert child.status_type == Status.Type.COMMENT


@responses.activate
@pytest.mark.django_db
def test_a_remote_reply_inherits_the_film_anchor_from_its_parent(
    client, remote_keypair, person_doc
):
    # A Mastodon Note carries no ``film`` field. Without the inheritance a
    # threaded remote comment would have no anchor and would drop off every
    # film-anchored surface while looking fine on its own post page.
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    dune = Film.objects.create(title="Dune")
    parent = _review(alice, dune)

    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/reply-anchor",
                "<p>Inheriting the anchor.</p>",
                inReplyTo=f"http://testserver/status/{parent.pk}/",
            )
        ),
    )

    child = Status.objects.get(remote_url="https://remote.example/status/reply-anchor")
    assert child.film_id == dune.pk


@responses.activate
@pytest.mark.django_db
def test_a_reply_to_a_mirror_threads_under_the_mirror(
    client, remote_keypair, person_doc
):
    # The parent is another instance's object; the child arrives pointing at
    # its home URL, which is exactly how we store the mirror.
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    alice = User.objects.create_user(localname="alice", password="p")
    mirror = Status.objects.create(
        user=alice,
        film=Film.objects.create(title="Dune"),
        status_type=Status.Type.REVIEW,
        rating="3",
        content="<p>Their review.</p>",
        local=False,
        remote_url="https://remote.example/status/parent-mirror",
    )

    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/child-of-mirror",
                "<p>A reply to their review.</p>",
                inReplyTo="https://remote.example/status/parent-mirror",
            )
        ),
    )

    child = Status.objects.get(
        remote_url="https://remote.example/status/child-of-mirror"
    )
    assert child.reply_parent_id == mirror.pk
    assert child.user == carol


@responses.activate
@pytest.mark.django_db
def test_a_remote_reply_whose_parent_we_do_not_have_lands_flat(
    client, remote_keypair, person_doc
):
    # Unresolved parent ≠ dropped note. The content is real; it mirrors with
    # no parent, which is what every remote note did before this field was
    # read at all.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)

    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/orphan-reply",
                "<p>A reply to something we never got.</p>",
                inReplyTo="https://elsewhere.example/status/unknown",
            )
        ),
    )

    orphan = Status.objects.get(remote_url="https://remote.example/status/orphan-reply")
    assert orphan.reply_parent_id is None
    assert orphan.content == "<p>A reply to something we never got.</p>"


@responses.activate
@pytest.mark.django_db
def test_in_reply_to_arriving_as_an_object_or_a_list_still_threads(
    client, remote_keypair, person_doc
):
    # The spec allows a link property as a bare IRI, an embedded object, or
    # an array. All three must thread.
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    parent = _review(alice, Film.objects.create(title="Dune"))
    parent_url = f"http://testserver/status/{parent.pk}/"

    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/reply-as-object",
                "<p>Object-shaped.</p>",
                inReplyTo={"id": parent_url},
            )
        ),
    )
    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/reply-as-list",
                "<p>List-shaped.</p>",
                inReplyTo=[parent_url],
            )
        ),
    )

    for url in ("reply-as-object", "reply-as-list"):
        child = Status.objects.get(remote_url=f"https://remote.example/status/{url}")
        assert child.reply_parent_id == parent.pk, url


@responses.activate
@pytest.mark.django_db
def test_a_remote_thread_renders_in_conversation_order(
    client, remote_keypair, person_doc
):
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    parent = _review(alice, Film.objects.create(title="Dune"))

    previous = f"http://testserver/status/{parent.pk}/"
    for n in range(3):
        url = f"https://remote.example/status/thread-{n}"
        _deliver(
            client,
            private_pem,
            _create(
                _note(
                    url,
                    f"<p>Turn {n}.</p>",
                    inReplyTo=previous,
                    publishedTime=f"2026-09-23T10:0{n}:00Z",
                )
            ),
        )
        previous = url

    pairs = conversation(parent)
    assert [child.content for child, _ in pairs] == [
        "<p>Turn 0.</p>",
        "<p>Turn 1.</p>",
        "<p>Turn 2.</p>",
    ]
    # Each pair carries the turn it answered, which is what lets the flat
    # render name who a reply is replying to.
    assert pairs[0][1].pk == parent.pk
    assert pairs[1][1].pk == pairs[0][0].pk


@responses.activate
@pytest.mark.django_db
def test_a_thread_deeper_than_the_walk_cap_is_truncated_not_fatal(
    client, remote_keypair, person_doc
):
    # Inbound threads can be deeper than anything we ever write: our own
    # composer only ever replies one level. REPLY_THREAD_MAX_DEPTH is the
    # only bound, and it must bound the work rather than break the page.
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    root = _review(alice, Film.objects.create(title="Dune"))

    previous = f"http://testserver/status/{root.pk}/"
    depth = REPLY_THREAD_MAX_DEPTH + 3
    for n in range(depth):
        url = f"https://remote.example/status/deep-{n}"
        _deliver(
            client,
            private_pem,
            _create(
                _note(
                    url,
                    f"<p>Deep {n}.</p>",
                    inReplyTo=previous,
                    publishedTime="2026-09-23T11:00:00Z",
                )
            ),
        )
        previous = url

    assert Status.objects.filter(local=False).count() == depth
    # Every row is stored; only the walk is capped.
    assert len(conversation(root)) == REPLY_THREAD_MAX_DEPTH


@responses.activate
@pytest.mark.django_db
def test_a_remote_reply_is_not_a_top_level_feed_entry(
    client, remote_keypair, person_doc
):
    # R83 decision 2 made real on the inbound side: the reply exists as a
    # thread object under its parent and does not multiply feed rows.
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    alice.follows.add(carol)
    parent = _review(alice, Film.objects.create(title="Dune"))

    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/feed-threaded",
                "<p>A threaded reply.</p>",
                inReplyTo=f"http://testserver/status/{parent.pk}/",
            )
        ),
    )

    threaded = Status.objects.get(
        remote_url="https://remote.example/status/feed-threaded"
    )
    assert threaded.reply_parent_id is not None
    assert threaded not in list(Status.feed_for(alice))


# --- Inbound Accept -----------------------------------------------------------


def _inner_follow(actor=ALICE_ACTOR, target=REMOTE_ACTOR):
    return {
        "id": "https://remote.example/activity/our-follow-1",
        "type": "Follow",
        "actor": actor,
        "object": target,
    }


def _answer(kind, inner):
    return {
        "id": f"{REMOTE_ACTOR}#{kind.lower()}-follow-1",
        "type": kind,
        "actor": REMOTE_ACTOR,
        "object": inner,
    }


@responses.activate
@pytest.mark.django_db
def test_inbound_accept_leaves_the_local_follow_in_place(
    client, remote_keypair, person_doc
):
    # We record the follow when the member clicks and deliver it in the
    # same request, so there is no pending state to promote. The Accept is
    # confirmation, and the relationship we already hold is the right one.
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    alice.follows.add(carol)

    response = _deliver(client, private_pem, _answer("Accept", _inner_follow()))

    assert response.status_code == 202
    assert alice.follows.filter(pk=carol.pk).exists()


@responses.activate
@pytest.mark.django_db
def test_inbound_accept_is_logged_as_accepted(
    client, remote_keypair, person_doc, caplog
):
    # Without a handler this is the same "ignored" as a misspelled type, so
    # the one piece of good news a peer sends would be discarded like noise.
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    alice.follows.add(carol)

    with caplog.at_level("INFO", logger=INBOX_LOGGER):
        _deliver(client, private_pem, _answer("Accept", _inner_follow()))

    assert "Accept" in caplog.text
    assert "accepted" in caplog.text.lower()
    assert "alice" in caplog.text and "carol" in caplog.text


@responses.activate
@pytest.mark.django_db
def test_inbound_accept_for_a_follow_we_do_not_hold_changes_nothing(
    client, remote_keypair, person_doc
):
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    assert not alice.follows.filter(pk=carol.pk).exists()

    _deliver(client, private_pem, _answer("Accept", _inner_follow()))

    assert not alice.follows.filter(pk=carol.pk).exists()


# --- Inbound Reject -----------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_inbound_reject_removes_the_local_follow(client, remote_keypair, person_doc):
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    alice.follows.add(carol)

    response = _deliver(client, private_pem, _answer("Reject", _inner_follow()))

    assert response.status_code == 202
    assert not alice.follows.filter(pk=carol.pk).exists()
    assert carol.followers.filter(pk=alice.pk).count() == 0


@responses.activate
@pytest.mark.django_db
def test_inbound_reject_undoes_only_the_follow_it_answers(
    client, remote_keypair, person_doc
):
    # bob follows carol too and is not the one who asked. The inner actor
    # picks which local follow is undone; without that scoping one Reject
    # would clear everyone's follow of the sender.
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    alice.follows.add(carol)
    bob.follows.add(carol)

    _deliver(client, private_pem, _answer("Reject", _inner_follow(actor=ALICE_ACTOR)))

    assert not alice.follows.filter(pk=carol.pk).exists()
    assert bob.follows.filter(pk=carol.pk).exists()


@responses.activate
@pytest.mark.django_db
def test_inbound_reject_takes_the_followed_side_from_the_verified_sender(
    client, remote_keypair, person_doc
):
    # The inner ``object`` names somebody else entirely. The followed side
    # comes from the signature, so alice's follow of *carol* is the one
    # undone — a peer cannot point a refusal at a third party without
    # signing as that party.
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    alice.follows.add(carol)

    _deliver(
        client,
        private_pem,
        _answer("Reject", _inner_follow(target=OTHER_ACTOR)),
    )

    assert not alice.follows.filter(pk=carol.pk).exists()


@responses.activate
@pytest.mark.django_db
def test_inbound_reject_naming_a_non_local_requester_changes_nothing(
    client, remote_keypair, person_doc
):
    # The inner actor must resolve to a local user of this host. A Reject
    # naming a remote actor answers a follow we did not make, so the local
    # follow survives — the control that keeps the branch from firing wide.
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    alice.follows.add(carol)

    _deliver(client, private_pem, _answer("Reject", _inner_follow(actor=REMOTE_ACTOR)))

    assert alice.follows.filter(pk=carol.pk).exists()


@responses.activate
@pytest.mark.django_db
def test_inbound_reject_sends_nothing_back(client, remote_keypair, person_doc):
    # Their instance has just told us the relationship does not exist. A
    # follow-cancellation would be a message about a thing already settled.
    alice = User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    alice.follows.add(carol)
    before = len(responses.calls)

    _deliver(client, private_pem, _answer("Reject", _inner_follow()))

    assert len(responses.calls) == before


# --- The inbox logs what it decided -------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_the_inbox_logs_the_type_sender_and_outcome_of_every_activity(
    client, remote_keypair, person_doc, caplog
):
    # R88 read from the inside: delivery used to discard the response it got
    # back; the inbox used to discard the outcome it decided. An activity we
    # deliberately ignored looked exactly like one that never arrived.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)

    with caplog.at_level("INFO", logger=INBOX_LOGGER):
        _deliver(
            client,
            private_pem,
            {
                "id": "https://remote.example/activity/unknown-type",
                "type": "Question",
                "actor": REMOTE_ACTOR,
                "object": "https://remote.example/thing",
            },
        )

    assert "Question" in caplog.text
    assert "carol" in caplog.text
    assert "ignored" in caplog.text


def test_a_non_dict_activity_is_reported_as_ignored():
    request = RequestFactory().get("/")
    request.META["HTTP_HOST"] = "testserver"
    assert process_inbound_activity("not a document", None, request) == "ignored"
