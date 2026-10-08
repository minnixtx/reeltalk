"""The outbound audience on our wire envelope (§2L increment 1, R143).

R143 measured a ReelTalk account mirroring into Mastodon with
``statuses_count: 18`` while ``/api/v1/accounts/<id>/statuses`` answered
``0``. The cause is not in our intent and not in our logs -- it is in the
peer's ``StatusParser#visibility``, which derives a received status's
visibility from ``to``/``cc`` and from nothing else. An activity carrying
neither falls through to ``direct``, so everything we ever sent arrived filed
as a direct message: counted on the account, returned by no endpoint,
invisible to anyone who was not already in on it.

These tests pin the fix against **the peer's decision table** rather than
against our own shape, because the table is what decides whether a third
party can see us. The table is transcribed below as a pure function and run
against the documents we actually build, so a future change that moves one of
our envelopes out of the ``public`` branch goes red here rather than turning
into a quiet re-run of the R143 investigation two increments from now.

The table also settles two questions that looked like judgment calls and are
not: putting the public collection in ``cc`` instead of ``to`` yields
``unlisted``, not ``public`` -- so the obvious-looking mapping is wrong --
and a ``Like`` with no audience at all is what the peer itself sends, so the
one builder that stays address-less is matching the house idiom rather than
missing it.
"""

from decimal import Decimal

import pytest
from django.test import RequestFactory

from reeltalk.activitypub.identity import PUBLIC_COLLECTION
from reeltalk.activitypub.objects import (
    create_activity,
    delete_activity,
    like_activity,
    note_document,
    status_audience,
    update_activity,
)
from reeltalk.core.models import Film, Status
from reeltalk.mentions.models import sync_status_mentions
from reeltalk.social.models import User
from reeltalk.tests.members import member

HOST = "testserver"
ZED_ACTOR = "https://remote.example/users/zed"
ZED_INBOX = "https://remote.example/users/zed/inbox"

FOLLOWERS_OF_ALICE = f"http://{HOST}/user/alice/followers/"


def _peer_visibility(to, cc, followers_collection):
    """Mastodon 4.7.3 ``StatusParser#visibility``, transcribed verbatim.

    Source: ``app/lib/activitypub/parser/status_parser.rb`` on the live peer,
    with its ``TagManager#public_collection?`` inlined. This is deliberately
    *their* rule and not ours. Asserting our documents against it means these
    tests fail when a peer would misfile our post, which is the failure that
    actually matters -- a test that only checked our own key names would stay
    green while the peer kept filing us as direct.
    """

    def public(value):
        # TagManager#public_collection? accepts the full IRI and both shorthands.
        return value == PUBLIC_COLLECTION or value in ("as:Public", "Public")

    if any(public(v) for v in to):
        return "public"
    if any(public(v) for v in cc):
        return "unlisted"
    if followers_collection in to:
        return "private"
    return "direct"


@pytest.fixture
def req(db):
    request = RequestFactory().get("/")
    request.META["HTTP_HOST"] = HOST
    return request


@pytest.fixture
def alice(db):
    return member(localname="alice", password="p")


@pytest.fixture
def carol(db):
    """A local member: addressable, but never a delivery target."""
    return member(localname="carol", password="p")


@pytest.fixture
def zed(db):
    user = User(
        localname="zed@remote.example",
        local=False,
        actor_url=ZED_ACTOR,
        inbox_url=ZED_INBOX,
    )
    user.save()
    return user


@pytest.fixture
def film(db):
    return Film.objects.create(title="Arrival", year=2016)


def _status(user, film, *, raw="Great.", **extra):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.COMMENT,
        content=raw,
        raw_content=raw,
        **extra,
    )


# --- the shape we now send --------------------------------------------------


def test_a_plain_note_is_addressed_to_the_public(req, alice, film):
    status = _status(alice, film)
    doc = note_document(status, req)
    assert doc["to"] == [PUBLIC_COLLECTION]


def test_cc_is_the_followers_collection_not_one_entry_per_follower(
    req, alice, carol, film
):
    """One URI for the collection, whatever the follower count.

    A per-follower expansion would make the envelope grow with the audience
    and would not match what the peer puts there -- it emits
    ``followers_uri_for(account)``, a single collection reference.
    """
    carol.follows.add(alice)
    doc = note_document(_status(alice, film), req)
    assert doc["cc"] == [FOLLOWERS_OF_ALICE]


def test_a_mentioned_remote_joins_cc_after_the_followers_collection(
    req, alice, zed, film
):
    status = _status(alice, film)
    sync_status_mentions(status, [zed])
    doc = note_document(status, req)
    assert doc["cc"] == [FOLLOWERS_OF_ALICE, ZED_ACTOR]


def test_a_mentioned_local_member_is_addressed_on_our_own_host(req, alice, carol, film):
    status = _status(alice, film)
    sync_status_mentions(status, [carol])
    doc = note_document(status, req)
    assert doc["cc"] == [FOLLOWERS_OF_ALICE, f"http://{HOST}/user/carol/"]


def test_a_mirror_mention_uses_its_published_actor_url_never_a_same_host_mint(
    req, alice, zed, film
):
    """The trap this function exists to avoid.

    ``absolute_uri(actor_path(mirror.localname))`` is the tempting one-liner
    and it produces ``http://testserver/user/zed@remote.example/`` -- a URI
    on our host about somebody who does not live here. A peer resolves an
    audience entry rather than treating it as a label, so a wrong host here
    is worse than the entry being absent: it points at something that is not
    who the array claims.
    """
    status = _status(alice, film)
    sync_status_mentions(status, [zed])
    cc = note_document(status, req)["cc"]
    assert ZED_ACTOR in cc
    assert not any(
        "remote.example" in uri and not uri.startswith("https://remote.example")
        for uri in cc
    )
    assert f"http://{HOST}/user/zed@remote.example/" not in cc


def test_cc_holds_no_duplicate_when_a_member_is_in_both_sets(
    req, alice, carol, zed, film
):
    """Addressed once even when a member follows the author *and* is mentioned.

    The dedup that matters is across the two sources of a ``cc`` entry --
    the followers collection and the mention list -- so the setup puts carol
    in both rather than duplicating her within one list.
    """
    carol.follows.add(alice)
    status = _status(alice, film)
    sync_status_mentions(status, [carol, zed])
    cc = note_document(status, req)["cc"]
    assert cc == [FOLLOWERS_OF_ALICE, f"http://{HOST}/user/carol/", ZED_ACTOR]
    assert len(cc) == len(set(cc))


# --- the activities that wrap it -------------------------------------------


def test_create_activity_carries_the_same_audience_as_its_note(req, alice, zed, film):
    status = _status(alice, film)
    sync_status_mentions(status, [zed])
    activity = create_activity(status, alice, req)
    assert activity["to"] == activity["object"]["to"] == [PUBLIC_COLLECTION]
    assert activity["cc"] == activity["object"]["cc"]


def test_update_activity_recomputes_the_audience(req, alice, zed, film):
    """An edit that newly mentions a remote changes who the post is addressed to.

    Taken from the row's current mention state rather than cached, the same
    way the delivery list is recomputed, so the envelope and the recipients
    cannot drift apart across an edit.
    """
    status = _status(alice, film)
    assert status_audience(status)[1] == [FOLLOWERS_OF_ALICE]

    sync_status_mentions(status, [zed])
    after = status_audience(status)
    assert after[1] == [FOLLOWERS_OF_ALICE, ZED_ACTOR]

    activity = update_activity(status, alice, req)
    assert activity["cc"] == [FOLLOWERS_OF_ALICE, ZED_ACTOR]


def test_delete_activity_is_public_and_carries_no_cc(req, alice, zed, film):
    """The deliberate asymmetry, matching the peer's ``DeleteNoteSerializer``.

    A tombstone is not aimed at the author's followers -- everyone who could
    see the post needs to learn it is gone, which is what a public audience
    says and what a follower collection does not.
    """
    status = _status(alice, film)
    sync_status_mentions(status, [zed])
    activity = delete_activity(status, alice, req)
    assert activity["to"] == [PUBLIC_COLLECTION]
    assert "cc" not in activity


# --- what stays address-less, on purpose -----------------------------------


def test_like_activity_carries_no_audience_in_either_direction(req, alice, carol, film):
    """Pinned so a future "address everything" pass cannot quietly widen this.

    The peer's own ``LikeSerializer`` emits ``id``/``type``/``actor``/
    ``object`` and nothing else, and ``broadcast_like`` delivers to the
    post's author alone because a like is not timeline content. Giving it a
    public audience would put our favourites into public timelines on the
    peer and contradict the decision that shaped the delivery.
    """
    target = _status(alice, film)
    for undo in (False, True):
        activity = like_activity(carol, target, req, undo=undo)
        inner = activity if not undo else activity["object"]
        assert "to" not in inner, f"undo={undo}"
        assert "cc" not in inner, f"undo={undo}"


def test_flag_activity_carries_no_audience(db):
    """A report must never be public-addressed.

    Checked rather than assumed because a ``Flag`` is the one outbound
    activity whose recipients are moderation targets: a public audience on
    it would broadcast who was reported to whom.
    """
    from reeltalk.activitypub.identity import flag_activity

    representative = member(localname="rep", password="p")
    activity = flag_activity(
        representative=representative,
        report_id=7,
        comment="harassment",
        object_uris=[ZED_ACTOR],
    )
    assert "to" not in activity
    assert "cc" not in activity


# --- the contract, judged by the receiver's own rule -----------------------


def test_every_status_we_publish_classifies_as_public_on_the_peer(
    req, alice, zed, film
):
    """The one assertion that would have caught R143 on the day it shipped.

    Not "we added the key" but "the receiver files this as public", run
    through the peer's transcribed rule against documents we really built.
    """
    for kind, builder in (
        ("note", lambda s: note_document(s, req)),
        ("create", lambda s: create_activity(s, alice, req)),
        ("update", lambda s: update_activity(s, alice, req)),
    ):
        status = _status(alice, film, raw=f"{kind} body")
        sync_status_mentions(status, [zed])
        doc = builder(status)
        assert _peer_visibility(doc["to"], doc["cc"], FOLLOWERS_OF_ALICE) == "public", (
            kind
        )


def test_the_addressless_envelope_we_used_to_send_classifies_as_direct(
    req, alice, film
):
    """The diagnosis, kept as an executable record.

    This is the pre-change shape written out by hand. Under the peer's rule it
    is ``direct`` -- which is the whole of ``statuses_count: 18`` against a
    profile endpoint that answers ``0``. If someone "tidies" the rule above
    this stops proving anything, so it is asserted rather than described.
    """
    legacy = {"id": "https://example.org/note/1", "type": "Note"}
    assert (
        _peer_visibility(legacy.get("to", []), legacy.get("cc", []), FOLLOWERS_OF_ALICE)
        == "direct"
    )


def test_public_in_cc_alone_would_only_be_unlisted():
    """Why the mapping has to be ``to``: [Public], not ``cc``: [Public].

    The intuitive mapping -- mentions in ``to``, followers-and-public in
    ``cc`` -- reads plausibly and lands every post in ``unlisted``. Kept as
    a test because it is the mistake this increment came closest to making.
    """
    wrong = _peer_visibility(
        [], [PUBLIC_COLLECTION, FOLLOWERS_OF_ALICE], FOLLOWERS_OF_ALICE
    )
    assert wrong == "unlisted"


def test_status_audience_puts_public_in_to_not_cc(req, alice, film):
    to, cc = status_audience(_status(alice, film))
    assert to == [PUBLIC_COLLECTION]
    assert PUBLIC_COLLECTION not in cc


# --- the audience is not the delivery list ---------------------------------


def test_the_audience_and_the_delivery_list_agree_on_every_remote_member(
    req, alice, carol, zed, film
):
    """Same remote people, two different questions, one consistent answer.

    ``_status_targets`` answers "who gets a POST" and holds ``User`` rows;
    ``cc`` answers "who may see this" and holds URIs. They must name the same
    remote members or a mention would be in the document but never delivered
    -- or delivered to someone the document does not claim. Local members are
    where they legitimately differ: addressable in ``cc``, never a delivery
    target, because a local member has no inbox.
    """
    from reeltalk.activitypub.broadcast import _status_targets
    from reeltalk.activitypub.objects import actor_reference

    status = _status(alice, film)
    sync_status_mentions(status, [carol, zed])

    remote_targets = {
        actor_reference(u) for u in _status_targets(status) if not u.local
    }
    addressed = set(status_audience(status)[1]) - {FOLLOWERS_OF_ALICE}

    # Every remote member we POST to is addressed, and every remote member we
    # address is one we POST to. Neither set leads the other on the wire.
    assert remote_targets == {ZED_ACTOR}
    assert addressed & remote_targets == remote_targets

    # The only entries in ``cc`` with no matching delivery are local members.
    # That is not drift, it is the two questions being different: a local
    # member is a person who may see the post and has no inbox to be sent
    # it to, which is exactly why the audience could not simply be the
    # delivery list wearing a different hat.
    assert addressed - remote_targets == {f"http://{HOST}/user/carol/"}
    assert [u for u in _status_targets(status) if u.local] == []


def test_note_document_reads_the_mention_table_once(req, alice, zed, film):
    """The audience must not cost a second read of the mention table.

    The outbox serializes a page of these documents at a time, so a second
    read per row is a query per row for no reason. ``note_document`` loads
    the mentioned users once and serves both the ``cc`` array and the
    ``tag`` array off that one list -- which is also why ``create_activity``
    reads the pair off the finished Note instead of computing it again.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    status = _status(alice, film)
    sync_status_mentions(status, [zed])

    with CaptureQueriesContext(connection) as ctx:
        doc = note_document(status, req)

    mention_reads = [
        q["sql"] for q in ctx.captured_queries if "mentions_statusmention" in q["sql"]
    ]
    assert len(mention_reads) == 1, mention_reads
    # And both consumers of that one read are still correct.
    assert doc["cc"] == [FOLLOWERS_OF_ALICE, ZED_ACTOR]
    assert len(doc["tag"]) == 1


def test_create_activity_adds_no_mention_read_over_its_note(req, alice, zed, film):
    """Wrapping the Note must not re-read what the Note already read."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    status = _status(alice, film)
    sync_status_mentions(status, [zed])

    with CaptureQueriesContext(connection) as ctx:
        activity = create_activity(status, alice, req)

    mention_reads = [
        q["sql"] for q in ctx.captured_queries if "mentions_statusmention" in q["sql"]
    ]
    assert len(mention_reads) == 1, mention_reads
    assert activity["to"] == activity["object"]["to"]
    assert activity["cc"] == activity["object"]["cc"]


def test_a_rating_status_is_addressed_the_same_as_a_comment(req, alice, film):
    """Addressing follows the post, not the post's kind."""
    review = Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW,
        content="Excellent.",
        raw_content="Excellent.",
        rating=Decimal("4.5"),
    )
    assert status_audience(review) == status_audience(_status(alice, film))
