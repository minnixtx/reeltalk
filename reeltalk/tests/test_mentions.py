"""The mention parser and ``StatusMention`` storage (mentions increment 1).

Nothing calls this code yet — increments 2, 3 and 4 wire it into rendering,
the outbound wire and the notification. What is pinned here is that the parser
matches what it should and refuses what it should refuse, and that the storage
holds.

The matrix follows notifications increment 1's shape: **every negative case
carries its own positive control inside the same test.** A test that only
asserts "this is not a mention" is satisfied just as well by a parser whose
regex matches nothing at all, and that is the failure mode that ships
silently — the feature reads as merely unused. Each negative below is
therefore paired with a string differing only in the guarded property,
asserted *to* match.
"""

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory

from reeltalk.core.models import Film, Status
from reeltalk.mentions.models import StatusMention
from reeltalk.mentions.parser import mentions_from_tags, mentions_from_text

User = get_user_model()

# The host this test instance pretends to be, so a same-host actor URL can be
# recognised as ours by ``resolve_known_actor`` the way it is in production.
# "testserver" is Django's own test-runner host and is already allowed.
HOST = "testserver"

MIRROR_HANDLE = "minnix@upallnight.minnix.dev"
PORTED_HANDLE = "bob@nightshift.minnix.dev:3030"


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return User.objects.create_user(localname="bob", password="s3cretpass")


@pytest.fixture
def minnix(db):
    """A local account that is also the first label of a real-looking email.

    Without the negative lookbehind this is the account a typed email
    address would falsely mention.
    """
    return User.objects.create_user(localname="minnix", password="s3cretpass")


@pytest.fixture
def reel_fan(db):
    """A hyphenated localname — legal under R12, unwritable by Mastodon."""
    return User.objects.create_user(localname="reel-fan", password="s3cretpass")


@pytest.fixture
def dotted(db):
    """A localname whose dot must survive the parse as part of the name."""
    return User.objects.create_user(localname="reel.fan", password="s3cretpass")


@pytest.fixture
def mirror(db):
    return User.objects.create_user(
        localname=MIRROR_HANDLE,
        password="s3cretpass",
        local=False,
        actor_url="https://upallnight.minnix.dev/users/minnix",
    )


@pytest.fixture
def ported_mirror(db):
    """A mirror whose netloc carries a non-default port."""
    return User.objects.create_user(
        localname=PORTED_HANDLE,
        password="s3cretpass",
        local=False,
        actor_url="http://nightshift.minnix.dev:3030/users/bob",
    )


@pytest.fixture
def film(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def post(db, alice, film):
    return Status.objects.create(
        user=alice,
        film=film,
        status_type=Status.Type.REVIEW,
        content="a review of Dune",
    )


def _request(host=HOST):
    return RequestFactory().get("/", HTTP_HOST=host)


def _handles(text):
    """The localnames ``mentions_from_text`` resolved, in the order found."""
    return [user.localname for user in mentions_from_text(text)]


def _tagged(tag_value):
    return [user.localname for user in mentions_from_tags(tag_value, _request())]


# --------------------------------------------------------------------------
# The text path: what a member typed.
# --------------------------------------------------------------------------


def test_a_bare_local_handle_is_a_mention(alice):
    assert _handles("loved it, @alice") == ["alice"]
    # Control: an unknown handle in the identical position resolves to
    # nothing, so the line above matched because alice exists.
    assert _handles("loved it, @someoneunknown") == []


def test_an_email_address_is_not_a_mention(alice, bob, minnix):
    # The '@' in an email is preceded by a word character, which the negative
    # lookbehind rejects. Two shapes are pinned because the greedy handle makes
    # only the second one detectable: on 'minnix@minnix.dev' the handle
    # swallows '.dev' and resolution fails whether or not the lookbehind is
    # there, whereas 'press@alice' ends exactly on a real account and would
    # falsely mention her without it.
    assert _handles("my email is minnix@minnix.dev") == []
    assert _handles("cc press@alice on the invite") == []
    # Control: the blocked token kept, with a resolvable handle that must come
    # through. Without the lookbehind this returns ['alice', 'bob'], not
    # ['bob'] -- the difference is the guard doing its job.
    assert _handles("cc press@alice, then @bob") == ["bob"]


def test_a_url_with_credentials_does_not_mention_its_userinfo(alice, bob):
    # A URL's userinfo is not a mention. Naming a real account as the
    # userinfo is what makes this case carry the lookbehind rather than lean
    # on resolution: without the guard the parser would capture 'alice' out of
    # 'https://alice@example.com/reel' and mention her for a credentials URL.
    assert _handles("the mirror lives at https://alice@example.com/reel") == []
    assert _handles("the mirror lives at https://alice@example.com/reel, cc @bob") == [
        "bob"
    ]


def test_a_path_segment_that_looks_like_a_handle_is_not_a_mention(alice):
    # '/' is in the lookbehind set for the same reason '=' is: a URL path
    # ending in /@alice is a route, not an address.
    assert _handles("see https://example.com/@alice for more") == []
    assert _handles("see https://example.com/ and @alice for more") == ["alice"]


def test_a_trailing_period_is_not_swallowed_into_the_lookup(alice):
    # If the period were swallowed the lookup key would be 'alice.', which no
    # account has, and the result would be empty rather than alice.
    assert _handles("great work, @alice.") == ["alice"]
    assert _handles("@alice.") == ["alice"]
    assert not User.objects.filter(localname="alice.").exists()


def test_a_trailing_comma_is_not_swallowed(alice, bob, reel_fan):
    assert _handles("@alice, @bob and @reel-fan") == ["alice", "bob", "reel-fan"]


def test_hyphenated_localnames_match(reel_fan):
    assert _handles("@reel-fan nailed it") == ["reel-fan"]
    assert _handles("@reel-unknown nailed it") == []


def test_a_dotted_localname_keeps_its_inner_dot(dotted):
    assert _handles("@reel.fan was right") == ["reel.fan"]
    # ...and still drops the sentence's own final period.
    assert _handles("@reel.fan. was right") == ["reel.fan"]


def test_local_handles_match_case_insensitively(alice):
    # R40: case variants are one identity and signup rejects insensitive
    # duplicates, so all three spellings are alice.
    assert _handles("@ALICE, @Alice and @aLiCe") == ["alice"]
    assert _handles("@NOBODYHERE") == []


def test_a_remote_handle_resolves_to_the_mirror_verbatim(mirror):
    assert _handles(f"@{MIRROR_HANDLE} showed up") == [MIRROR_HANDLE]
    assert _handles("@nobody@nowhere.example showed up") == []


def test_a_remote_handle_is_matched_verbatim_not_case_insensitively(mirror):
    # The asymmetry against the local case is deliberate and is copied from
    # the route (_resolve_profile_user applies no iexact to mirrors). A
    # parser more forgiving than the route would resolve a handle whose
    # profile link then 404s.
    assert _handles("@Minnix@upallnight.minnix.dev") == []
    # Control: the stored casing resolves.
    assert _handles(f"@{MIRROR_HANDLE}") == [MIRROR_HANDLE]


def test_a_remote_handle_with_a_port_in_the_netloc_resolves(ported_mirror):
    assert _handles(f"@{PORTED_HANDLE} posted") == [PORTED_HANDLE]
    assert _handles("@bob@nightshift.minnix.dev:9999 posted") == []


def test_a_code_span_is_not_a_mention(alice):
    assert _handles("type `@alice` to trigger it") == []
    # Control: the mention outside the code span still resolves.
    assert _handles("type `@nope` to trigger @alice") == ["alice"]


def test_a_fenced_block_is_not_a_mention(alice):
    # Both fence styles are pinned because only the tilde form makes the
    # fence guard load-bearing on its own: a backtick-fenced block is already
    # swallowed by the inline-code pass, so testing only ``` ``` ``` would
    # leave _FENCED_CODE_RE looking redundant while ~~~ fences went unmasked.
    for fence in ("```", "~~~"):
        fenced = f"intro\n\n{fence}\n@alice inside a fenced block\n{fence}\n\noutro"
        assert _handles(fenced) == [], fence
        # Control: the same fence, with a mention outside it.
        assert _handles(fenced + " @alice outside it") == ["alice"], fence


def test_a_mention_inside_a_link_label_is_not_a_mention(alice):
    # mistune renders a link's label inline, so a mention inside one is the
    # nested-anchor problem increment 2 has to render around. Refusing it at
    # the parser means the anchor never gets made.
    assert _handles("[look at @alice](https://allowed.tld/x)") == []
    assert _handles("![cover @alice](https://allowed.tld/x.png)") == []
    # Control: the same link, mention outside the label.
    assert _handles("[look at this](https://allowed.tld/x) - @alice agrees") == [
        "alice"
    ]


def test_an_unknown_handle_resolves_to_nothing(alice):
    assert _handles("@nobodyhere at all") == []
    # Control: the identical shape with a known handle.
    assert _handles("@alice at all") == ["alice"]


def test_order_follows_the_text_and_repeats_collapse(alice, bob):
    assert _handles("@bob then @alice then @bob again") == ["bob", "alice"]


def test_empty_input_yields_nothing():
    assert mentions_from_text("") == []
    assert mentions_from_text(None) == []


# --------------------------------------------------------------------------
# The wire path: what a peer put in note["tag"].
# --------------------------------------------------------------------------


def test_a_single_mention_dict_resolves(mirror):
    tag = {"type": "Mention", "href": mirror.actor_url, "name": f"@{MIRROR_HANDLE}"}
    assert mentions_from_tags(tag, _request()) == [mirror]


def test_a_list_of_tags_resolves_only_the_mentions_in_order(alice, mirror):
    tags = [
        {"type": "Hashtag", "href": "https://example.com/tag/dune", "name": "#dune"},
        {"type": "Mention", "href": mirror.actor_url, "name": f"@{MIRROR_HANDLE}"},
    ]
    assert mentions_from_tags(tags, _request()) == [mirror]
    # Control: a mention of a local member in the same list resolves too, so
    # the hashtag above was skipped rather than the list ignored.
    local_tag = {"type": "Mention", "href": f"https://{HOST}/user/alice/"}
    assert mentions_from_tags([tags[0], local_tag], _request()) == [alice]


def test_tag_type_may_be_a_list(mirror):
    # ActivityPub's `type` is multi-valued by spec; Mastodon reads the same
    # shape with equals_or_includes?.
    tag = {"type": ["Mention"], "href": mirror.actor_url}
    assert mentions_from_tags(tag, _request()) == [mirror]
    assert mentions_from_tags({"type": ["Hashtag"]}, _request()) == []


def test_tag_href_may_be_an_embedded_object(mirror):
    tag = {"type": "Mention", "href": {"id": mirror.actor_url}}
    assert mentions_from_tags(tag, _request()) == [mirror]


def test_an_unknown_href_is_dropped_and_never_fetched(mirror, monkeypatch):
    def _no_fetch(url, *args, **kwargs):
        raise AssertionError(f"the parser must never fetch, tried: {url}")

    monkeypatch.setattr("reeltalk.activitypub.mirrors.fetch_person_document", _no_fetch)
    unknown = {
        "type": "Mention",
        "href": "https://stranger.example/users/nobody",
        "name": "@nobody@stranger.example",
    }
    assert mentions_from_tags(unknown, _request()) == []
    # Control: with the same stub in place, a href we already hold still
    # resolves — so the empty result above is the resolver declining, not
    # the patch disabling resolution.
    assert mentions_from_tags(
        {"type": "Mention", "href": mirror.actor_url}, _request()
    ) == [mirror]


def test_a_local_member_is_resolved_from_a_same_host_actor_url(alice):
    # A local user's actor_url is empty — only mirrors fill it — so this
    # only works by recognising the URL as ours and reading the R40 path.
    assert _tagged({"type": "Mention", "href": f"https://{HOST}/user/alice/"}) == [
        "alice"
    ]
    # Local matching stays case-insensitive through the path too.
    assert _tagged({"type": "Mention", "href": f"https://{HOST}/user/ALICE/"}) == [
        "alice"
    ]


def test_a_foreign_host_wearing_our_actor_path_is_not_our_local_user(alice):
    # The netloc check is what stops https://evil.example/user/alice/ from
    # being read as a mention of our alice.
    assert (
        _tagged({"type": "Mention", "href": "https://evil.example/user/alice/"}) == []
    )
    # Control: the same path on our own host resolves.
    assert _tagged({"type": "Mention", "href": f"https://{HOST}/user/alice/"}) == [
        "alice"
    ]


def test_non_mention_tag_types_are_skipped(mirror):
    tags = [
        {"type": "Hashtag", "href": "https://example.com/tag/dune"},
        {"type": "Emoji", "href": "https://example.com/e.png"},
        {"type": "Link", "href": mirror.actor_url},
    ]
    assert mentions_from_tags(tags, _request()) == []
    # Control: changing only the type to Mention makes the last one resolve,
    # so the three above were skipped for their type and not their href.
    assert (
        mentions_from_tags(
            {"type": "Link", "href": mirror.actor_url, "also": "Mention"}, _request()
        )
        == []
    )
    assert mentions_from_tags(
        {"type": "Mention", "href": mirror.actor_url}, _request()
    ) == [mirror]


def test_missing_or_empty_tag_shapes_yield_nothing():
    assert mentions_from_tags(None, _request()) == []
    assert mentions_from_tags("", _request()) == []
    assert mentions_from_tags([], _request()) == []
    assert mentions_from_tags({}, _request()) == []
    assert mentions_from_tags({"type": "Mention"}, _request()) == []
    assert mentions_from_tags({"type": "Mention", "href": ""}, _request()) == []
    assert mentions_from_tags(["a bare string"], _request()) == []
    assert (
        mentions_from_tags({"type": 7, "href": "https://x.example/"}, _request()) == []
    )


# --------------------------------------------------------------------------
# The storage.
# --------------------------------------------------------------------------


def test_the_unique_pair_is_enforced_by_the_database(post, alice, bob):
    StatusMention.objects.create(status=post, user=alice)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            StatusMention.objects.create(status=post, user=alice)
    # Control: the same status with a different member is fine, so what
    # stopped the write above was the pair and not the status alone.
    assert StatusMention.objects.create(status=post, user=bob).pk
    assert StatusMention.objects.filter(status=post).count() == 2


def test_the_unique_constraint_is_a_unique_index_in_the_database(post, alice):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s",
            ["mentions_statusmention"],
        )
        defs = [row[0] for row in cursor.fetchall()]
    unique = [d for d in defs if "status_mention_unique" in d]
    assert unique, f"the unique index is missing from: {defs}"
    assert "UNIQUE" in unique[0], unique[0]
    # Pinned to the column order, and written the way Postgres says it:
    # pg_indexes.indexdef renders the column names unquoted.
    assert unique[0].endswith("(status_id, user_id)"), unique[0]


def test_a_hard_delete_of_the_status_takes_its_mentions(post, alice):
    StatusMention.objects.create(status=post, user=alice)
    Status.objects.filter(pk=post.pk).delete()
    assert StatusMention.objects.count() == 0


def test_a_hard_delete_of_a_member_takes_their_mentions(post, alice, bob):
    StatusMention.objects.create(status=post, user=alice)
    StatusMention.objects.create(status=post, user=bob)
    # bob is mentioned but not the post's author, so Status.user's PROTECT
    # edge is out of the way and this exercises the mention CASCADE alone.
    User.objects.filter(pk=bob.pk).delete()
    assert [m.user for m in StatusMention.objects.filter(status=post)] == [alice]


def test_a_soft_deleted_status_keeps_its_mention_rows(post, alice):
    # R17: Status.delete() is soft, so the CASCADE does not fire. The row
    # outlives the deleted-looking post exactly as a notification's does --
    # the reason a reader must check `deleted` rather than trust the FK.
    StatusMention.objects.create(status=post, user=alice)
    post.delete()
    assert StatusMention.objects.filter(status=post).count() == 1


def test_a_status_holds_its_mentions_in_insertion_order(post, alice, reel_fan):
    StatusMention.objects.create(status=post, user=reel_fan)
    StatusMention.objects.create(status=post, user=alice)
    assert [m.user.localname for m in post.mentions.all()] == ["reel-fan", "alice"]


def test_a_mention_row_describes_itself(post, alice):
    row = StatusMention.objects.create(status=post, user=alice)
    assert str(row) == f"alice mentioned in status {post.pk}"
