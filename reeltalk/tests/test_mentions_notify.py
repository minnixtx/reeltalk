"""The mention kind, its three producers and the page verb (mentions increment 4).

Increments 1–3 built a parser, a renderer and a wire that nobody could be
notified about. This is the last piece: ``Kind.MENTION``, ``record_mentions``,
the three producers that call it, and the one line of page copy that turns a
ledger row into something a member can read.

The shapes every guard here is tested against:

* **The template trap is real and it is tested first-class.** Before this
  increment ``index.html`` ended its dispatch in ``{% else %}followed you``,
  so a fourth kind with no branch of its own rendered every mention as a
  follow — silently, with no failing test, because nothing asserted the wrong
  verb was wrong. The mention wording is pinned against a real mention row,
  the follow branch is now explicit, and an unknown kind renders no verb at
  all so the next fourth-kind mistake is visible rather than confident.
* **M-c suppresses a row, not a mention.** A reply that also @mentions the
  person being replied to is one notification. The ``StatusMention`` row and
  the outbound ``tag`` still land — only the ledger row is withheld — so the
  suppression test asserts both halves in one call.
* **M-e is a guard, not a dedup accident.** It is per ``(recipient, status)``,
  it is read from the database rather than from memory, and it is *not* the
  standing "the ``_mirror_status`` update branch never notifies" rule. That
  rule is about the reply kind and stays where it is; the mention kind has its
  own mechanism, which is why the inbound update branch can notify without
  loosening anything.
* **Nothing is pre-filtered.** ``notify()`` owns self / remote / blocked (R97).
  A producer that helpfully pre-checked them would be a second source of truth
  about who is owed, so one test asserts every recipient reaches ``notify()``
  untouched and a companion asserts the guards still stop the rows *there*.
* **Every absence carries a control that could actually have failed**, per the
  increment-2 shape — and per increment 3's concrete lesson, the guards are
  asserted against their own function as well as through the end-to-end path,
  because a negative that a downstream decline would also satisfy is not a
  test of the guard.

The federated half is driven through the real signed-inbox route, never by
calling the handler by hand, and counts by ``kind`` throughout: establishing
the remote mirror sends a ``Follow`` at alice, which is itself one of the
six notifications-increment-2 producers and must neither mask a result nor
stand in for one.
"""

import json

import pytest
import responses
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from reeltalk.activitypub import crypto, signatures
from reeltalk.activitypub.objects import mention_tag, note_document
from reeltalk.core.models import Film, Status
from reeltalk.mentions.models import StatusMention, sync_status_mentions
from reeltalk.mentions.notify import record_mentions
from reeltalk.notifications.context_processors import unread_notifications
from reeltalk.notifications.models import (
    Notification,
    mark_all_read,
    notify,
)

User = get_user_model()

REMOTE_ACTOR = "https://remote.example/user/carol/"
REMOTE_INBOX = REMOTE_ACTOR.rstrip("/") + "/inbox"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"
ALICE_ACTOR = "http://testserver/user/alice/"
UNKNOWN_ACTOR = "https://never-mentioned-before.example/users/nobody"


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    """A local member who writes the posts under test — the mention *actor*."""
    return User.objects.create_user(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def their_post(db, bob, dune):
    return Status.objects.create(
        user=bob,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>A review of Dune.</p>",
        raw_content="A review of Dune.",
    )


@pytest.fixture
def alice_post(db, alice, dune):
    """A post whose author *is* the person a reply would be answering.

    Separate from ``their_post`` because M-c turns on exactly this: the
    suppressed member is ``reply_parent.user``, which is not the same person
    as the mention actor. A test that reuses bob's own post as the parent
    never exercises the guard at all — it only proves that mentioning an
    unrelated member writes a row, which is the control, not the case.
    """
    return Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW,
        rating="5",
        content="<p>Alice's review of Dune.</p>",
        raw_content="Alice's review of Dune.",
    )


@pytest.fixture
def req(db):
    """A request carrying the test-server Host, so our own actor URLs resolve."""
    request = RequestFactory().get("/")
    request.META["HTTP_HOST"] = "testserver"
    return request


@pytest.fixture
def member(client, alice):
    client.force_login(alice)
    return client


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


# --- The signed-inbox plumbing (same shape as test_notification_producers) --


def _signed_post(path: str, body: bytes, private_pem: str, key_id=REMOTE_KEY_ID):
    return signatures.sign_request(
        "POST", f"http://testserver{path}", private_pem, key_id=key_id, body=body
    )


def _deliver(client, private_pem, activity):
    body = json.dumps(activity).encode()
    meta = {}
    for name, value in _signed_post("/inbox/", body, private_pem).items():
        meta[f"HTTP_{name.upper().replace('-', '_')}"] = value
    meta["HTTP_HOST"] = "testserver"
    return client.post(
        "/inbox/", data=body, content_type="application/activity+json", **meta
    )


def _establish_carol(client, private_pem, person_doc):
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
        "publishedTime": "2026-09-25T10:00:00Z",
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


def _tag(user):
    """A wire Mention for a user we already know, in Mastodon's three fields."""
    href = user.actor_url or f"http://testserver/user/{user.localname}/"
    return {"type": "Mention", "href": href, "name": f"@{user.localname}"}


def rows(recipient, kind, status=None):
    query = Notification.objects.filter(recipient=recipient, kind=kind)
    return query.filter(status=status) if status is not None else query


# --- record_mentions: the two suppressions ----------------------------------


def test_record_mentions_files_one_row_per_mentioned_member(db, alice, their_post):
    made = record_mentions(their_post, [alice])
    assert len(made) == 1
    row = made[0]
    assert row.recipient_id == alice.pk
    # The actor is the post's author, not the caller: the person who wrote the
    # post is the person who mentioned you.
    assert row.actor_id == their_post.user_id
    assert row.kind == "mention"
    assert row.status_id == their_post.pk


def test_an_empty_mention_set_writes_nothing(db, alice, their_post):
    assert record_mentions(their_post, []) == []
    assert Notification.objects.count() == 0
    # Control: the identical call with a member in it writes, so the empty
    # result above is the empty set and not a function that never writes.
    assert len(record_mentions(their_post, [alice])) == 1


def test_the_author_being_replied_to_is_suppressed(db, alice, bob, alice_post):
    # M-c. A reply carries reply_parent, and the reply producer already told
    # that author about the reply. Mentioning them in the text must not add a
    # second row for the same turn.
    reply = Status.objects.create(
        user=bob,
        film=alice_post.film,
        status_type=Status.Type.COMMENT,
        content="<p>@alice you're right.</p>",
        reply_parent=alice_post,
    )
    assert record_mentions(reply, [alice]) == []
    assert rows(alice, "mention", reply).count() == 0
    # Control: the same status shape with no parent, naming a member who is
    # not its author, does write. So what stopped alice is the parent
    # relationship and not something about her or about this status.
    assert len(record_mentions(alice_post, [bob])) == 1


def test_only_the_person_being_replied_to_is_suppressed(db, alice, bob, alice_post):
    carol = User.objects.create_user(localname="carol", password="s3cretpass")
    reply = Status.objects.create(
        user=bob,
        film=alice_post.film,
        status_type=Status.Type.COMMENT,
        content="<p>@alice @carol both right.</p>",
        reply_parent=alice_post,
    )
    made = record_mentions(reply, [alice, carol])
    assert [row.recipient_id for row in made] == [carol.pk]
    assert rows(alice, "mention", reply).count() == 0
    assert rows(carol, "mention", reply).count() == 1


def test_a_suppressed_mention_still_has_its_row_and_its_tag(
    db, alice, bob, alice_post, req
):
    # The other half of M-c, and the half that is easy to get wrong by
    # suppressing too much. The mention still renders and still goes on the
    # wire — only the ledger row is withheld. If this test ever fails because
    # the StatusMention row went too, the fix is not to delete less: it is to
    # notice that the ledger and the wire are different questions.
    #
    # Both calls are made here because the producer makes both. ``sync_status_mentions``
    # owns the rows the renderer and the wire read; ``record_mentions`` owns the
    # ledger. Neither writes the other's table, which is precisely why one can
    # be suppressed without the other disappearing.
    reply = Status.objects.create(
        user=bob,
        film=alice_post.film,
        status_type=Status.Type.COMMENT,
        content="<p>@alice you're right.</p>",
        reply_parent=alice_post,
    )
    mentioned = [alice]
    sync_status_mentions(reply, mentioned)
    record_mentions(reply, mentioned)
    assert StatusMention.objects.filter(status=reply, user=alice).exists()
    assert note_document(reply, req)["tag"] == [mention_tag(alice, req)]
    assert rows(alice, "mention", reply).count() == 0


def test_repeated_calls_for_one_status_notify_once(db, alice, their_post):
    # M-e. N edits that keep mentioning the same person produce one row, which
    # is what makes it safe to call this from a path that runs on every save.
    assert len(record_mentions(their_post, [alice])) == 1
    record_mentions(their_post, [alice])
    record_mentions(their_post, [alice])
    assert rows(alice, "mention", their_post).count() == 1
    # Control: the guard is per recipient, not a one-shot per status — a
    # member nobody has been told about yet still gets told on the third call.
    carol = User.objects.create_user(localname="carol", password="s3cretpass")
    assert len(record_mentions(their_post, [alice, carol])) == 1
    assert rows(carol, "mention", their_post).count() == 1
    assert rows(alice, "mention", their_post).count() == 1


def test_the_idempotency_guard_is_per_status_not_per_recipient(
    db, alice, bob, dune, their_post
):
    second = Status.objects.create(
        user=bob,
        film=dune,
        status_type=Status.Type.COMMENT,
        content="<p>Another one, @alice.</p>",
    )
    record_mentions(their_post, [alice])
    record_mentions(second, [alice])
    assert rows(alice, "mention", their_post).count() == 1
    assert rows(alice, "mention", second).count() == 1


def test_the_guard_is_read_from_the_database_not_from_memory(
    db, alice, their_post, req
):
    # A second "request" passes freshly-loaded objects, so nothing about the
    # in-memory instance could reveal that the first call already wrote. The
    # guard has to go to the table, and this is what proves it does.
    record_mentions(their_post, [alice])
    reloaded = Status.objects.get(pk=their_post.pk)
    again = User.objects.get(pk=alice.pk)
    assert record_mentions(reloaded, [again]) == []
    assert rows(alice, "mention", their_post).count() == 1


# --- Nothing is pre-filtered (R97) ------------------------------------------


def test_record_mentions_hands_every_recipient_to_notify_untouched(
    monkeypatch, db, alice, their_post
):
    # The structural half of R97: this producer must not decide who may be
    # told. Every recipient reaches notify() with the kind and the actor it was
    # given, including the three notify() exists to refuse. Asserted with a spy
    # rather than by behaviour, because behaviour alone cannot tell "notify
    # refused" apart from "the producer filtered first" — which is the exact
    # second source of truth this rule forbids.
    calls = []

    def spy(recipient, actor, kind, status=None):
        calls.append((recipient.pk, actor.pk, kind, status.pk))
        return None

    monkeypatch.setattr("reeltalk.mentions.notify.notify", spy)
    remote = User.objects.create_user(
        localname="someone@elsewhere.example", password="s3cretpass", local=False
    )
    alice.blocks.add(their_post.user)

    record_mentions(their_post, [alice, remote])

    assert calls == [
        (alice.pk, their_post.user_id, Notification.Kind.MENTION, their_post.pk),
        (remote.pk, their_post.user_id, Notification.Kind.MENTION, their_post.pk),
    ]


def test_notify_still_decides_who_may_be_told(db, alice, their_post):
    # ...and the guards still stop the rows, with a control in the same call
    # so an empty ledger is the guard rather than a producer that never fired.
    remote = User.objects.create_user(
        localname="someone@elsewhere.example", password="s3cretpass", local=False
    )
    blocked = User.objects.create_user(localname="blocked", password="s3cretpass")
    blocked.blocks.add(their_post.user)
    bystander = User.objects.create_user(localname="bystander", password="s3cretpass")

    made = record_mentions(their_post, [their_post.user, remote, blocked, bystander])

    assert [row.recipient_id for row in made] == [bystander.pk]
    assert rows(their_post.user, "mention", their_post).count() == 0
    assert rows(remote, "mention", their_post).count() == 0
    assert rows(blocked, "mention", their_post).count() == 0
    assert rows(bystander, "mention", their_post).count() == 1


# --- The page verb ----------------------------------------------------------


def test_a_mention_renders_the_mention_verb(member, alice, their_post):
    notify(alice, their_post.user, Notification.Kind.MENTION, their_post)
    content = member.get("/notifications/").content.decode()
    assert "mentioned you in" in content
    assert f'href="/status/{their_post.pk}/"' in content


def test_a_mention_is_not_rendered_as_a_follow(member, alice, bob, their_post):
    # The trap, pinned with a count rather than a substring: "followed you" in
    # the page is satisfied by the follow row alone, so without a second kind
    # present the assertion could never notice the catch-all firing.
    notify(alice, bob, Notification.Kind.FOLLOW)
    notify(alice, their_post.user, Notification.Kind.MENTION, their_post)
    content = member.get("/notifications/").content.decode()
    assert content.count('<li class="review">') == 2
    assert content.count("followed you") == 1
    assert content.count("mentioned you in") == 1


def test_a_mention_on_a_soft_deleted_post_drops_the_deep_link(
    member, alice, their_post
):
    # Status.delete() is soft (R17), so the FK survives and /status/<id>/ 404s
    # on deleted=False. Same edge notifications increment 3 found for like and
    # reply; the mention branch has to make the same test.
    notify(alice, their_post.user, Notification.Kind.MENTION, their_post)
    their_post.delete()
    content = member.get("/notifications/").content.decode()
    assert "mentioned you in a post that is no longer here" in content
    assert f'href="/status/{their_post.pk}/"' not in content


def test_a_mention_with_no_status_row_still_renders(member, alice, their_post):
    # The SET_NULL edge: a hard delete keeps the record of the mention.
    note = notify(alice, their_post.user, Notification.Kind.MENTION, their_post)
    Status.objects.filter(pk=their_post.pk).delete()
    note.refresh_from_db()
    assert note.status is None
    content = member.get("/notifications/").content.decode()
    assert "mentioned you in a post that is no longer here" in content


def test_an_unknown_kind_renders_no_verb_at_all(member, alice, bob, their_post):
    # The reason the follow branch had to stop being {% else %}. An unlisted
    # kind now renders the row with no verb — visibly broken rather than
    # confidently wrong. Under the old template this asserted nothing, because
    # every unknown kind happily reported "followed you".
    Notification.objects.create(
        recipient=alice, actor=bob, kind="bogus", status=their_post
    )
    content = member.get("/notifications/").content.decode()
    assert content.count('<li class="review">') == 1
    assert "followed you" not in content
    assert "mentioned you" not in content
    assert "liked" not in content
    assert "replied" not in content
    # The row itself is intact: it is the verb that is missing, not the row.
    assert bob.localname in content


# --- The badge needs no change (R93's kind-agnostic contract) ---------------


def test_a_mention_lights_the_badge_with_no_badge_change(db, alice, their_post, req):
    # Verified rather than assumed: unread_for filters on (recipient, created)
    # and names no kind at all, so a mention is unread exactly like a like. If
    # this ever needs a badge change, the contract R93 bought is what broke.
    req.user = alice
    assert unread_notifications(req)["unread_notifications"] == 0
    record_mentions(their_post, [alice])
    assert unread_notifications(req)["unread_notifications"] == 1
    # Control: the row is unread because it is newer than the timestamp, not
    # because the count is stuck at one.
    mark_all_read(alice)
    assert unread_notifications(req)["unread_notifications"] == 0


# --- Producer 1: the local reply route --------------------------------------


@responses.activate
def test_a_reply_mentioning_the_parent_author_notifies_once(
    db, client, alice, bob, alice_post
):
    client.force_login(bob)
    response = client.post(
        f"/status/{alice_post.pk}/reply/", {"content": "@alice you're right."}
    )
    assert response.status_code == 200
    reply = Status.objects.get(user=bob, reply_parent=alice_post)
    # One notification for the whole turn, and it is the reply — not a reply
    # plus a mention, and not a mention that replaced the reply.
    assert Notification.objects.filter(recipient=alice).count() == 1
    assert rows(alice, "reply", reply).count() == 1
    assert rows(alice, "mention", reply).count() == 0
    # And the mention itself is intact on the row the wire reads.
    assert StatusMention.objects.filter(status=reply, user=alice).exists()


@responses.activate
def test_a_reply_mentioning_a_third_member_notifies_them(db, client, alice, bob):
    carol = User.objects.create_user(localname="carol", password="s3cretpass")
    own = Status.objects.create(
        user=alice,
        film=Film.objects.create(title="Arrival", year=2016),
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Arrival.</p>",
    )
    client.force_login(bob)
    client.post(f"/status/{own.pk}/reply/", {"content": "@carol look at this."})
    assert rows(carol, "mention").count() == 1
    assert rows(carol, "mention").get().actor_id == bob.pk
    # Carol is not the parent, so nothing about her was suppressed; and the
    # parent's own reply row is untouched by her presence.
    assert rows(alice, "reply", Status.objects.get(user=bob)).count() == 1


@responses.activate
def test_a_reply_mentioning_nobody_writes_no_mention_rows(db, client, alice, bob):
    own = Status.objects.create(
        user=alice,
        film=Film.objects.create(title="Blade Runner", year=1982),
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Blade Runner.</p>",
    )
    client.force_login(bob)
    client.post(f"/status/{own.pk}/reply/", {"content": "No handles here."})
    assert StatusMention.objects.count() == 0
    assert rows(bob, "mention").count() == 0
    # Control: the reply itself still landed and still notified.
    assert rows(alice, "reply", Status.objects.get(user=bob)).count() == 1


# --- Producer 2: the finish flow (create AND edit, per D5) ------------------


@responses.activate
def test_the_finish_flow_notifies_a_newly_mentioned_member(
    db, client, alice, bob, dune
):
    client.force_login(bob)
    client.post(
        f"/film/{dune.pk}/watched/",
        {"rating": "4", "content": "Watched it with @alice."},
    )
    review = Status.objects.get(user=bob, film=dune)
    assert rows(alice, "mention", review).count() == 1
    assert rows(alice, "mention", review).get().actor_id == bob.pk


@responses.activate
def test_re_finishing_a_review_does_not_notify_the_same_member_twice(
    db, client, alice, bob, dune
):
    # D5 updates the review in place, so this is M-e on the real edit path
    # rather than a synthetic second call.
    client.force_login(bob)
    client.post(
        f"/film/{dune.pk}/watched/",
        {"rating": "4", "content": "Watched it with @alice."},
    )
    first = Status.objects.get(user=bob, film=dune)
    client.post(
        f"/film/{dune.pk}/watched/",
        {"rating": "5", "content": "Still watched it with @alice."},
    )
    second = Status.objects.get(user=bob, film=dune)
    assert first.pk == second.pk
    assert rows(alice, "mention", second).count() == 1


@responses.activate
def test_editing_a_review_to_add_a_mention_notifies_the_new_member(
    db, client, alice, bob, dune
):
    carol = User.objects.create_user(localname="carol", password="s3cretpass")
    client.force_login(bob)
    client.post(
        f"/film/{dune.pk}/watched/",
        {"rating": "4", "content": "Watched it with @alice."},
    )
    client.post(
        f"/film/{dune.pk}/watched/",
        {"rating": "4", "content": "Watched it with @alice and @carol."},
    )
    review = Status.objects.get(user=bob, film=dune)
    assert rows(carol, "mention", review).count() == 1
    assert rows(alice, "mention", review).count() == 1


@responses.activate
def test_dropping_a_mention_on_an_edit_does_not_unnotify_anybody(
    db, client, alice, bob, dune
):
    # sync_status_mentions deletes the row for a mention dropped on an edit.
    # The notification lives in its own table and stays — otherwise an edit
    # could reach into someone's ledger and take a past event back.
    client.force_login(bob)
    client.post(
        f"/film/{dune.pk}/watched/",
        {"rating": "4", "content": "Watched it with @alice."},
    )
    review = Status.objects.get(user=bob, film=dune)
    client.post(
        f"/film/{dune.pk}/watched/",
        {"rating": "4", "content": "Watched it alone."},
    )
    assert StatusMention.objects.filter(status=review).count() == 0
    assert rows(alice, "mention", review).count() == 1


@responses.activate
def test_a_self_mention_on_the_finish_flow_writes_a_row_and_no_ledger_entry(
    db, client, bob, dune
):
    # notify()'s self guard, reached through the real route: the mention is
    # real (it renders, it goes on the wire) and the notification is not.
    client.force_login(bob)
    client.post(
        f"/film/{dune.pk}/watched/",
        {"rating": "4", "content": "Rewatching, as @bob always does."},
    )
    review = Status.objects.get(user=bob, film=dune)
    assert StatusMention.objects.filter(status=review, user=bob).exists()
    assert rows(bob, "mention", review).count() == 0


# --- Producer 3: the inbound mirror -----------------------------------------


@responses.activate
def test_an_inbound_note_mentioning_a_local_member_notifies_them(
    client, remote_keypair, person_doc, alice
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    response = _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/mention-in-1",
                "<p>Have you seen this? @alice</p>",
                tag=[_tag(alice)],
            )
        ),
    )
    assert response.status_code == 202
    mirror = Status.objects.get(
        local=False, remote_url="https://remote.example/status/mention-in-1"
    )
    note = rows(alice, "mention", mirror).get()
    # Attributed to the verified sender, not to the note's attributedTo.
    assert note.actor_id == carol.pk
    assert StatusMention.objects.filter(status=mirror, user=alice).exists()


@responses.activate
def test_an_inbound_self_mention_notifies_nobody(
    client, remote_keypair, person_doc, alice
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/mention-self-1",
                "<p>My own @carol thoughts.</p>",
                tag=[_tag(carol)],
            )
        ),
    )
    mirror = Status.objects.get(
        local=False, remote_url="https://remote.example/status/mention-self-1"
    )
    assert StatusMention.objects.filter(status=mirror, user=carol).exists()
    assert rows(carol, "mention", mirror).count() == 0
    # Control: the same delivery path with a local target does write a row, so
    # the empty ledger is notify()'s guard and not a producer that never ran.
    assert rows(alice, "follow").count() == 1


@responses.activate
def test_an_inbound_mention_of_a_remote_mirror_notifies_nobody(
    client, remote_keypair, person_doc, alice
):
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    them = User.objects.create_user(
        localname="someone@elsewhere.example",
        password="s3cretpass",
        local=False,
        actor_url="https://elsewhere.example/users/them",
    )
    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/mention-remote-1",
                "<p>Hey @them.</p>",
                tag=[_tag(them)],
            )
        ),
    )
    mirror = Status.objects.get(
        local=False, remote_url="https://remote.example/status/mention-remote-1"
    )
    assert StatusMention.objects.filter(status=mirror, user=them).exists()
    assert Notification.objects.filter(recipient=them).count() == 0
    # Control: alice's establishing follow is still on the ledger, so the
    # delivery reached the producer rather than being dropped upstream.
    assert rows(alice, "follow").count() == 1


@responses.activate
def test_an_inbound_mention_from_someone_the_member_blocked_notifies_nobody(
    client, remote_keypair, person_doc, alice
):
    private_pem, _public = remote_keypair
    carol = _establish_carol(client, private_pem, person_doc)
    alice.blocks.add(carol)
    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/mention-blocked-1",
                "<p>@alice again.</p>",
                tag=[_tag(alice)],
            )
        ),
    )
    mirror = Status.objects.get(
        local=False, remote_url="https://remote.example/status/mention-blocked-1"
    )
    # The note really mirrored, so the empty ledger is the write-time block
    # guard rather than a delivery that never reached this code.
    assert StatusMention.objects.filter(status=mirror, user=alice).exists()
    assert rows(alice, "mention", mirror).count() == 0


@responses.activate
def test_an_inbound_edit_that_newly_mentions_a_member_notifies_them(
    client, remote_keypair, person_doc, alice
):
    # M-e on the federated path. The standing "the _mirror_status update
    # branch never notifies" rule is about the reply kind and is untouched —
    # this is the mention kind's own guard letting a genuinely new recipient
    # through, which is exactly what M-e decided.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    note_doc = _note(
        "https://remote.example/status/mention-edit-1",
        "<p>A note with no mentions.</p>",
    )
    _deliver(client, private_pem, _create(note_doc))
    mirror = Status.objects.get(
        local=False, remote_url="https://remote.example/status/mention-edit-1"
    )
    assert StatusMention.objects.filter(status=mirror).count() == 0
    assert rows(alice, "mention", mirror).count() == 0

    edited = dict(
        note_doc,
        content="<p>A note that now mentions @alice.</p>",
        tag=[_tag(alice)],
        editedTime="2026-09-25T11:00:00Z",
    )
    _deliver(client, private_pem, _update(edited))
    assert StatusMention.objects.filter(status=mirror, user=alice).exists()
    assert rows(alice, "mention", mirror).count() == 1


@responses.activate
def test_repeated_inbound_edits_are_bound_to_one_row(
    client, remote_keypair, person_doc, alice
):
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    note_doc = _note(
        "https://remote.example/status/mention-spam-1",
        "<p>@alice</p>",
        tag=[_tag(alice)],
    )
    for i in range(4):
        activity = _create(note_doc) if i == 0 else _update(note_doc)
        _deliver(client, private_pem, activity)
        note_doc = dict(note_doc, content=f"<p>@alice edit {i}</p>")
    mirror = Status.objects.get(
        local=False, remote_url="https://remote.example/status/mention-spam-1"
    )
    assert rows(alice, "mention", mirror).count() == 1


@responses.activate
def test_an_unknown_inbound_href_is_dropped_and_never_fetched(
    client, remote_keypair, person_doc, alice
):
    # M-b / R89: an unknown actor URL is dropped, not chased. The URL is
    # registered below precisely so a fetch would be visible rather than
    # raising — the assertion is that nothing was ever asked for it.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    responses.add(
        responses.GET,
        UNKNOWN_ACTOR,
        json={"@context": "https://www.w3.org/ns/activitystreams", "type": "Person"},
    )
    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/mention-unknown-1",
                "<p>Someone nobody knows.</p>",
                tag=[
                    {
                        "type": "Mention",
                        "href": UNKNOWN_ACTOR,
                        "name": "@nobody@never-mentioned-before.example",
                    }
                ],
            )
        ),
    )
    mirror = Status.objects.get(
        local=False, remote_url="https://remote.example/status/mention-unknown-1"
    )
    assert StatusMention.objects.filter(status=mirror).count() == 0
    assert Notification.objects.filter(kind="mention").count() == 0
    asked = [c.request.url for c in responses.calls]
    assert not any("never-mentioned-before.example" in url for url in asked)


@responses.activate
def test_an_inbound_reply_that_also_mentions_the_author_notifies_once(
    client, remote_keypair, person_doc, alice, alice_post
):
    # M-c on the federated path, and the case the trap note calls out by
    # name: on an inbound mirror the row's owner is the sender, so the
    # person being replied to is reply_parent.user. Both producers see the
    # same status and the same author, and the turn still yields one row.
    private_pem, _public = remote_keypair
    _establish_carol(client, private_pem, person_doc)
    _deliver(
        client,
        private_pem,
        _create(
            _note(
                "https://remote.example/status/mention-reply-1",
                "<p>@alice agreed.</p>",
                inReplyTo=f"http://testserver/status/{alice_post.pk}/",
                tag=[_tag(alice)],
            )
        ),
    )
    mirror = Status.objects.get(
        local=False, remote_url="https://remote.example/status/mention-reply-1"
    )
    assert mirror.reply_parent_id == alice_post.pk
    # Counted excluding the establishing follow, which is a different event
    # from a different producer and would otherwise sit in every total here.
    turn = Notification.objects.filter(recipient=alice).exclude(kind="follow")
    assert turn.count() == 1
    assert rows(alice, "reply", mirror).count() == 1
    assert rows(alice, "mention", mirror).count() == 0
    # The mention is still stored, so it still renders and still rides any
    # later document for this mirror.
    assert StatusMention.objects.filter(status=mirror, user=alice).exists()
