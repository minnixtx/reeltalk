"""The outbound mention wire and the outbound audience (mentions increment 3).

Two questions get pinned here, and they must not be collapsed into one:

* **What a mention looks like on the wire** — ``note_document``'s ``tag``
  array, in exactly the three fields Mastodon's ``MentionSerializer`` reads,
  with the actor URI in ``href`` because that is the half Mastodon resolves
  on and ``name`` is decoration.
* **Who a status is delivered to** — the author's remote followers *plus*
  the remote users the status mentions, deduped by pk.

Local members sit between those two questions with a different answer on
each side: a local mention **is** in the tag array and **is not** in the
delivery set, because a local member has no inbox and reads the local rows
their own queries already show them. Every test that asserts the local half
therefore carries the remote half in the same call. That is the mentions
increment 2 lesson applied — a negative assertion that passes because some
*downstream* stage declined is not a test of the guard at all, so the local
exclusion is asserted directly against ``_status_targets`` as well as
end-to-end.

Nothing here notifies. There is no ``Kind.MENTION`` and no ``notify()``
call: recording a mention is invisible to a user, and the notification stays
increment 4's per R90.
"""

import json

import pytest
import requests
import responses
from django.test import RequestFactory

from reeltalk.activitypub.broadcast import (
    _status_targets,
    broadcast_reply,
    broadcast_status_create,
    broadcast_status_update,
)
from reeltalk.activitypub.objects import mention_tag, note_document
from reeltalk.core.models import Film, Status
from reeltalk.mentions.models import sync_status_mentions
from reeltalk.social.models import User

HOST = "testserver"
ZED_ACTOR = "https://remote.example/users/zed"
ZED_INBOX = "https://remote.example/users/zed/inbox"
YAN_ACTOR = "https://other.example/users/yan"
YAN_INBOX = "https://other.example/users/yan/inbox"
GLENN_ACTOR = "https://third.example/users/glenn"
GLENN_INBOX = "https://third.example/users/glenn/inbox"


@pytest.fixture
def req(db):
    """A request carrying the test-server Host, so absolute URIs are ours."""
    request = RequestFactory().get("/")
    request.META["HTTP_HOST"] = HOST
    return request


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password="p")


@pytest.fixture
def carol(db):
    """A local member: mentionable, taggable, never a delivery target."""
    return User.objects.create_user(localname="carol", password="p")


def _mirror(localname, actor_url, inbox_url):
    user = User(
        localname=localname,
        local=False,
        actor_url=actor_url,
        inbox_url=inbox_url,
    )
    user.save()
    return user


@pytest.fixture
def zed(db):
    return _mirror("zed@remote.example", ZED_ACTOR, ZED_INBOX)


@pytest.fixture
def yan(db):
    return _mirror("yan@other.example", YAN_ACTOR, YAN_INBOX)


@pytest.fixture
def glenn(db):
    """A third remote on a third host, so three audiences stay distinguishable."""
    return _mirror("glenn@third.example", GLENN_ACTOR, GLENN_INBOX)


@pytest.fixture
def film(db):
    return Film.objects.create(title="Arrival", year=2016)


def _status(user, film, *, raw="Great.", **extra):
    """A ``comment`` status — a type with no rating and no D5 uniqueness, so a
    test can create several for one user on one film."""
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.COMMENT,
        content=raw,
        raw_content=raw,
        **extra,
    )


# --- The tag shape ------------------------------------------------------------


def test_a_tag_carries_exactly_the_three_fields_mastodon_reads(req, zed):
    tag = mention_tag(zed, req)
    # Mastodon's MentionSerializer is ``attributes :type, :href, :name``.
    # A fourth field would be ours alone and no peer owes it anything, so
    # this asserts the set and not merely that three are present.
    assert set(tag) == {"type", "href", "name"}
    assert tag["type"] == "Mention"


def test_a_local_mention_is_a_bare_handle_on_our_own_actor_uri(req, carol):
    tag = mention_tag(carol, req)
    assert tag["name"] == "@carol"
    assert tag["href"] == f"http://{HOST}/user/carol/"


def test_a_mirror_is_named_by_its_full_acct_at_its_home_actor_uri(req, zed):
    tag = mention_tag(zed, req)
    # The acct form arrives for free: a mirror's stored localname already
    # carries the home domain.
    assert tag["name"] == "@zed@remote.example"
    # The href is the actor URL we received, never a /user/… path we minted
    # here. Mastodon resolves a mention on href and never on name, so this
    # is the field that has to be honest.
    assert tag["href"] == ZED_ACTOR
    assert HOST not in tag["href"]


def test_one_note_tags_both_a_local_member_and_a_mirror(req, alice, carol, zed, film):
    status = _status(alice, film)
    sync_status_mentions(status, [carol, zed])
    tags = note_document(status, req)["tag"]
    assert [t["name"] for t in tags] == ["@carol", "@zed@remote.example"]
    # Both href rules in one document: ours built from the localname, theirs
    # the URL we received. Asserted together because each is only wrong in
    # the presence of the other — a test with one mention cannot tell which
    # branch produced the URL.
    assert tags[0]["href"] == f"http://{HOST}/user/carol/"
    assert tags[1]["href"] == ZED_ACTOR


def test_a_status_that_mentions_nobody_carries_no_tag_key_at_all(
    req, alice, carol, film
):
    quiet = _status(alice, film)
    loud = _status(alice, film)
    sync_status_mentions(loud, [carol])
    assert "tag" not in note_document(quiet, req)
    # The control, through the same serializer in the same test: with one
    # mention the key is present. Without it the first assert would pass on
    # a serializer that never emits a tag at all.
    assert "tag" in note_document(loud, req)


# --- The delivery audience ----------------------------------------------------


def test_the_delivery_set_excludes_a_local_mention_and_includes_the_remote_one(
    alice, carol, zed, film
):
    # Asserted against the target helper directly rather than only through a
    # delivery count: ``_deliver_signed`` *also* skips local users, so a
    # delivery-level test would stay green with the filter here deleted. The
    # guard has to be tested where it lives.
    status = _status(alice, film)
    sync_status_mentions(status, [carol, zed])
    assert [user.pk for user in _status_targets(status)] == [zed.pk]


@responses.activate
def test_a_mentioned_remote_with_no_followers_anywhere_still_gets_the_create(
    alice, zed, film, req
):
    # Alice has no followers at all, so the only reason this POST exists is
    # the mention.
    status = _status(alice, film)
    sync_status_mentions(status, [zed])
    responses.add(responses.POST, ZED_INBOX)
    broadcast_status_create(req, status)
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == ZED_INBOX
    sent = json.loads(responses.calls[0].request.body)
    assert sent["type"] == "Create"
    assert sent["object"]["tag"][0]["name"] == "@zed@remote.example"


@responses.activate
def test_a_status_with_no_followers_and_no_mentions_sends_nothing(alice, film, req):
    # The control for the test above: with no mention and no follower, the
    # audience machinery sends zero, so the single send above is the mention
    # and not the broadcast machinery always posting somewhere.
    status = _status(alice, film)
    broadcast_status_create(req, status)
    assert len(responses.calls) == 0


@responses.activate
def test_a_follower_who_is_also_mentioned_is_posted_to_exactly_once(
    alice, zed, yan, film, req
):
    zed.follows.add(alice)
    yan.follows.add(alice)
    status = _status(alice, film)
    # zed is in both sets: a follower of the author and a mentioned remote.
    sync_status_mentions(status, [yan, zed])
    responses.add(responses.POST, ZED_INBOX)
    responses.add(responses.POST, YAN_INBOX)
    broadcast_status_create(req, status)
    urls = [call.request.url for call in responses.calls]
    assert urls.count(ZED_INBOX) == 1
    # yan is a follower who is *not* mentioned and still gets his one send,
    # so two calls is a dedupe that ran, not one that never had to.
    assert sorted(urls) == sorted([ZED_INBOX, YAN_INBOX])


@responses.activate
def test_a_local_mention_adds_no_send_while_a_remote_one_does(
    alice, carol, zed, film, req
):
    status = _status(alice, film)
    sync_status_mentions(status, [carol, zed])
    responses.add(responses.POST, ZED_INBOX)
    broadcast_status_create(req, status)
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == ZED_INBOX
    # And carol is not absent from the feature — she is on the wire. The
    # mention was recorded and tagged; she simply is not someone to POST to.
    sent = json.loads(responses.calls[0].request.body)
    assert [t["name"] for t in sent["object"]["tag"]] == [
        "@carol",
        "@zed@remote.example",
    ]


@responses.activate
def test_a_dead_mentioned_instance_drops_its_send_without_failing_the_request(
    alice, zed, yan, film, req
):
    yan.follows.add(alice)
    status = _status(alice, film)
    sync_status_mentions(status, [zed])
    responses.add(responses.POST, YAN_INBOX)
    responses.add(
        responses.POST,
        ZED_INBOX,
        body=requests.exceptions.ConnectionError("refused"),
    )
    # Must not raise: a member's post is not down because the person they
    # mentioned lives on a server that is.
    broadcast_status_create(req, status)
    urls = {call.request.url for call in responses.calls}
    assert urls == {YAN_INBOX, ZED_INBOX}


@responses.activate
def test_broadcast_status_update_reaches_a_mentioned_remote_too(alice, zed, film, req):
    status = _status(alice, film)
    sync_status_mentions(status, [zed])
    responses.add(responses.POST, ZED_INBOX)
    broadcast_status_update(req, status)
    assert len(responses.calls) == 1
    sent = json.loads(responses.calls[0].request.body)
    assert sent["type"] == "Update"
    assert sent["object"]["tag"][0]["href"] == ZED_ACTOR


# --- The reply's three audiences ----------------------------------------------


def _reply_to(parent, user, raw):
    return Status.objects.create(
        user=user,
        film_id=parent.film_id,
        status_type=Status.Type.COMMENT,
        content=raw,
        raw_content=raw,
        reply_parent=parent,
    )


@responses.activate
def test_a_reply_reaches_a_mentioned_remote_and_the_parent_author(
    alice, zed, yan, film, req
):
    # yan authored the post being answered and follows nobody, so the only
    # reason he receives this is the threading. zed follows nobody either,
    # so the only reason he receives it is the mention.
    parent = _status(yan, film)
    reply = _reply_to(parent, alice, "asking @zed@remote.example")
    sync_status_mentions(reply, [zed])
    responses.add(responses.POST, ZED_INBOX)
    responses.add(responses.POST, YAN_INBOX)
    broadcast_reply(req, reply)
    urls = {call.request.url for call in responses.calls}
    assert urls == {ZED_INBOX, YAN_INBOX}


@responses.activate
def test_a_reply_dedupes_a_remote_who_is_both_the_parent_and_a_mention(
    alice, zed, film, req
):
    parent = _status(zed, film)
    reply = _reply_to(parent, alice, "as @zed@remote.example says")
    sync_status_mentions(reply, [zed])
    responses.add(responses.POST, ZED_INBOX)
    broadcast_reply(req, reply)
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == ZED_INBOX


@responses.activate
def test_a_reply_reaches_all_three_audiences_exactly_once(
    alice, zed, yan, glenn, film, req
):
    zed.follows.add(alice)  # the replier's remote follower
    parent = _status(yan, film)  # the parent author, follows nobody
    reply = _reply_to(parent, alice, "cc @glenn@third.example")
    sync_status_mentions(reply, [glenn])  # the mentioned third party
    responses.add(responses.POST, ZED_INBOX)
    responses.add(responses.POST, YAN_INBOX)
    responses.add(responses.POST, GLENN_INBOX)
    broadcast_reply(req, reply)
    urls = [call.request.url for call in responses.calls]
    assert sorted(urls) == sorted([ZED_INBOX, YAN_INBOX, GLENN_INBOX])


# --- Recording the rows -------------------------------------------------------


def test_sync_writes_one_row_per_user_in_the_order_given(alice, carol, zed, film):
    status = _status(alice, film)
    sync_status_mentions(status, [zed, carol])
    assert [m.user.localname for m in status.mentions.all()] == [
        "zed@remote.example",
        "carol",
    ]


def test_re_syncing_the_same_set_writes_no_second_row(alice, carol, zed, film):
    status = _status(alice, film)
    sync_status_mentions(status, [carol, zed])
    first = [m.pk for m in status.mentions.all()]
    # The D5 case: ``mark_watched`` updates this review in place, so this
    # runs a second time with the same people. An append would raise
    # ``status_mention_unique`` right here.
    sync_status_mentions(status, [carol, zed])
    assert [m.pk for m in status.mentions.all()] == first


def test_a_sync_that_adds_one_leaves_the_existing_row_alone(alice, carol, zed, film):
    status = _status(alice, film)
    sync_status_mentions(status, [carol])
    kept = status.mentions.get().pk
    sync_status_mentions(status, [carol, zed])
    rows = list(status.mentions.all())
    assert len(rows) == 2
    # The control inside the same call: carol's row is the same row, so the
    # second sync added rather than rebuilt everything.
    assert rows[0].pk == kept
    assert rows[1].user == zed


def test_a_sync_that_drops_a_mention_removes_its_row_and_its_tag(
    req, alice, carol, zed, film
):
    status = _status(alice, film)
    sync_status_mentions(status, [carol, zed])
    sync_status_mentions(status, [zed])
    assert [m.user.localname for m in status.mentions.all()] == ["zed@remote.example"]
    # The wire follows the rows: an edit that stopped addressing carol no
    # longer claims to.
    assert [t["name"] for t in note_document(status, req)["tag"]] == [
        "@zed@remote.example"
    ]


def test_syncing_an_empty_set_clears_every_row(alice, carol, film):
    status = _status(alice, film)
    sync_status_mentions(status, [carol])
    assert status.mentions.count() == 1
    sync_status_mentions(status, [])
    assert status.mentions.count() == 0


def test_a_surviving_row_keeps_its_first_position_so_tags_are_first_mentioned(
    req, alice, carol, zed, film
):
    status = _status(alice, film)
    sync_status_mentions(status, [carol, zed])
    # The edit re-orders the wording. Surviving rows are not re-sorted, so
    # the tag order is first-mentioned rather than whatever the latest
    # revision happens to read. Pinned because it is a real consequence of
    # not churning rows, not an accident of insertion.
    sync_status_mentions(status, [zed, carol])
    assert [t["name"] for t in note_document(status, req)["tag"]] == [
        "@carol",
        "@zed@remote.example",
    ]


# --- The two write sites ------------------------------------------------------


@responses.activate
def test_the_finish_flow_records_mentions_and_delivers_to_the_mentioned_remote(
    client, alice, zed, film
):
    client.force_login(alice)
    responses.add(responses.POST, ZED_INBOX)
    response = client.post(
        f"/film/{film.pk}/watched/",
        {"rating": "4", "content": "see also @zed@remote.example"},
    )
    assert response.status_code == 302
    status = Status.objects.get(user=alice, film=film)
    assert [m.user_id for m in status.mentions.all()] == [zed.pk]
    assert len(responses.calls) == 1
    sent = json.loads(responses.calls[0].request.body)
    assert sent["object"]["tag"][0]["href"] == ZED_ACTOR


@responses.activate
def test_the_finish_flow_records_a_local_mention_without_adding_a_send(
    client, alice, carol, zed, film
):
    client.force_login(alice)
    responses.add(responses.POST, ZED_INBOX)
    client.post(
        f"/film/{film.pk}/watched/",
        {"rating": "4", "content": "@carol and @zed@remote.example"},
    )
    status = Status.objects.get(user=alice, film=film)
    # carol IS recorded — the local half of the feature works —
    assert status.mentions.count() == 2
    # …and only the remote half is posted to.
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == ZED_INBOX


@responses.activate
def test_editing_through_the_finish_flow_stops_delivering_a_dropped_mention(
    client, alice, zed, yan, film
):
    client.force_login(alice)
    responses.add(responses.POST, ZED_INBOX)
    responses.add(responses.POST, YAN_INBOX)
    client.post(
        f"/film/{film.pk}/watched/", {"rating": "4", "content": "@zed@remote.example"}
    )
    client.post(
        f"/film/{film.pk}/watched/", {"rating": "5", "content": "@yan@other.example"}
    )
    status = Status.objects.get(user=alice, film=film)
    assert [m.user.localname for m in status.mentions.all()] == ["yan@other.example"]
    # zed got the Create and *not* the Update; yan got the Update. One
    # ordered list proves both halves at once.
    assert [call.request.url for call in responses.calls] == [ZED_INBOX, YAN_INBOX]


@responses.activate
def test_the_reply_route_records_mentions_and_delivers_to_the_mentioned_remote(
    client, alice, zed, yan, film
):
    parent = _status(yan, film)
    client.force_login(alice)
    responses.add(responses.POST, ZED_INBOX)
    responses.add(responses.POST, YAN_INBOX)
    response = client.post(
        f"/status/{parent.pk}/reply/", {"content": "asking @zed@remote.example"}
    )
    assert response.status_code == 200
    reply = Status.objects.get(reply_parent=parent)
    assert [m.user_id for m in reply.mentions.all()] == [zed.pk]
    urls = {call.request.url for call in responses.calls}
    assert urls == {ZED_INBOX, YAN_INBOX}
