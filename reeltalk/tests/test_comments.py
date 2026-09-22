"""Comments, local only (feed interactions increment 4, R83 decision 2).

The first writer ``Status.reply_parent`` ever had. Five contracts, roughly
in the order they bite:

* **A reply is a ``Status`` threaded by ``reply_parent``, not a comment
  table**, and it **inherits the parent's film**. ``Status.film`` is
  nullable, so a reply that left it unset would drop out of every
  film-anchored surface — the film's set, its author's "all films" tab,
  the export — while looking perfectly fine on its own page.
* **Replies leave the feed** (decision 2). ``feed_for`` excludes any
  status with a parent, which is what makes "comments don't multiply feed
  rows" true in the query rather than a hope about the template, and keeps
  pagination out of this increment.
* **The route refuses what the page withholds** (R85). The composer is
  hidden on a remote mirror and ``/status/<id>/reply/`` 404s on one too,
  so a hand-made request cannot write a reply this instance cannot
  deliver.
* **The thread is flat, labelled and bounded.** Depth lives in the data so
  ``inReplyTo`` stays honest; the page renders one list, the walk caps at
  ``REPLY_THREAD_MAX_DEPTH``, and tombstones are walked through rather
  than rendered so deleting a mid-reply does not bury what was said under
  it.
* **A reply whose parent is a tombstone says so**, instead of reading as
  addressed to nothing.

Absence assertions on the home page go through ``_home``, which asserts
200 first — without a superuser ``/`` answers an empty 302 to the setup
wizard (R12) and nothing asserted below it proves anything. Row-level
absences are asserted inside the row's own ``<li>`` via its
``data-status`` marker, because the page also carries every other
member's row and a page-wide "not in body" check passes vacuously.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from reeltalk.core.models import (
    REPLY_THREAD_MAX_DEPTH,
    Film,
    Status,
    add_reply,
    conversation,
    feed_entries,
    mark_watched,
    reply_counts,
    shelve_to_watchlist,
)

User = get_user_model()


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


def _remote_user(localname: str = "carol@remote.example") -> User:
    """A remote mirror account, as federation creates it (no local password)."""
    user = User(
        localname=localname,
        local=False,
        actor_url=f"https://remote.example/users/{localname.split('@')[0]}",
        inbox_url=f"https://remote.example/users/{localname.split('@')[0]}/inbox",
    )
    user.set_unusable_password()
    user.save()
    return user


def _review(user, film, content="<p>Spice must be reviewed.</p>", rating="4.5"):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=rating,
        content=content,
        raw_content="Spice must be reviewed.",
    )


def _reply(author, parent, text="<p>Agreed.</p>", raw="Agreed."):
    return add_reply(author, parent, content=text, raw_content=raw)


def _mirror(film, localname="carol@remote.example"):
    carol = _remote_user(localname)
    mirror = Status.objects.create(
        user=carol,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Their review of Dune.</p>",
        local=False,
        remote_url=f"https://remote.example/status/{localname}",
    )
    return carol, mirror


def _login(localname):
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


def _home(client) -> str:
    """The rendered home feed, with the render itself asserted first."""
    response = client.get("/")
    assert response.status_code == 200
    return response.content.decode()


def _row(body, status_id) -> str:
    """One feed row's own markup, so an absence check cannot borrow another row."""
    marker = f'<li class="review" data-status="{status_id}">'
    assert marker in body, "the target row did not render at all"
    start = body.index(marker)
    return body[start : body.index("</li>", start)]


# --- The writer --------------------------------------------------------------


@pytest.mark.django_db
def test_a_reply_is_a_comment_status_threaded_by_reply_parent(alice, bob, dune):
    parent = _review(alice, dune)
    reply = _reply(bob, parent)
    assert reply.status_type == Status.Type.COMMENT
    assert reply.reply_parent_id == parent.pk
    assert parent.replies.count() == 1


@pytest.mark.django_db
def test_a_reply_inherits_the_parents_film(alice, bob, dune):
    # The bug this exists to prevent: Status.film is nullable, so a reply
    # that did not set it would land with no film and disappear from every
    # film-anchored surface while its own page looked fine.
    parent = _review(alice, dune)
    reply = _reply(bob, parent)
    assert reply.film_id == dune.id


@pytest.mark.django_db
def test_an_inherited_film_keeps_the_reply_on_the_film_anchored_surfaces(
    alice, bob, dune
):
    parent = _review(alice, dune)
    _reply(bob, parent)
    # The replier has no shelf for Dune — the reply is the only thing
    # tying them to it, and it only ties them to it because the film was
    # inherited rather than left null.
    assert dune in list(bob.all_films())


@pytest.mark.django_db
def test_replying_on_a_film_you_reviewed_does_not_collide_with_your_review(
    alice, bob, dune
):
    # D5's unique_review_per_user_per_film is a partial index on
    # review/review_rating only, so a comment by the same user on the same
    # film is not a second review.
    _review(alice, dune)
    other = _review(bob, dune)
    mine = _reply(alice, other)
    assert mine.film_id == dune.id
    assert Status.objects.filter(user=alice, film=dune).count() == 2


@pytest.mark.django_db
def test_a_reply_mints_its_own_origin_id_like_any_local_status(alice, bob, dune):
    # Day-one identity (R41): the reply is a local status, so Status.save
    # gives it an origin_id increment 5 can build a Create(Note) from.
    parent = _review(alice, dune)
    reply = _reply(bob, parent)
    assert reply.origin_id == reply.pk
    assert reply.origin_id != parent.origin_id


@pytest.mark.django_db
def test_a_reply_to_a_reply_keeps_its_own_parent_not_the_root(alice, bob, dune):
    # Wire-faithful: inReplyTo in increment 5 is read from this, so a
    # grandchild must point at the turn it answered, not at the root.
    root = _review(alice, dune)
    first = _reply(bob, root)
    second = _reply(alice, first)
    assert second.reply_parent_id == first.pk


# --- The walk ----------------------------------------------------------------


@pytest.mark.django_db
def test_conversation_returns_replies_in_conversation_order_with_their_parents(
    alice, bob, dune
):
    root = _review(alice, dune)
    first = _reply(bob, root, "<p>First.</p>")
    second = _reply(alice, root, "<p>Second.</p>")
    grandchild = _reply(bob, first, "<p>Reply to first.</p>")
    pairs = conversation(root)
    assert [p[0].pk for p in pairs] == [first.pk, second.pk, grandchild.pk]
    assert [p[1].pk for p in pairs] == [root.pk, root.pk, first.pk]


@pytest.mark.django_db
def test_conversation_walks_through_a_tombstone_and_keeps_its_live_children(
    alice, bob, dune
):
    # Deleting one reply must not bury everything said underneath it.
    root = _review(alice, dune)
    middle = _reply(bob, root, "<p>Deleting this.</p>")
    child = _reply(alice, middle, "<p>Still here.</p>")
    middle.delete()
    pairs = conversation(root)
    assert [p[0].pk for p in pairs] == [child.pk]
    # …and the walk knows its parent is gone, which is what the page needs
    # to label the row honestly instead of naming a deleted reply.
    assert pairs[0][1].pk == middle.pk
    assert pairs[0][1].deleted is True


@pytest.mark.django_db
def test_conversation_caps_at_max_depth(alice, bob, dune):
    root = _review(alice, dune)
    node = root
    for _ in range(REPLY_THREAD_MAX_DEPTH + 3):
        node = _reply(bob, node)
    assert len(conversation(root)) == REPLY_THREAD_MAX_DEPTH
    # The cap is a bound on the work, not on the data: the deeper replies
    # still exist and are reachable from their own parent.
    assert Status.objects.filter(reply_parent__isnull=False).count() == (
        REPLY_THREAD_MAX_DEPTH + 3
    )


@pytest.mark.django_db
def test_conversation_costs_one_query_per_level_not_per_reply(alice, bob, dune):
    # A wide thread: 4 replies per node, 3 deep, so 84 replies in all. The
    # walk costs one query per level regardless of how many rows sit at
    # each level — a per-reply implementation would cost 84 here.
    root = _review(alice, dune)
    level = [root]
    for _ in range(3):
        nxt = []
        for node in level:
            for _ in range(4):
                nxt.append(_reply(bob, node))
        level = nxt
    with CaptureQueriesContext(connection) as ctx:
        pairs = conversation(root)
    assert len(pairs) == 84
    # Three levels walked, plus the one query that discovers the thread has
    # ended. That terminating probe is the whole cost of not knowing where
    # the bottom is; it is not paid per row.
    assert len(ctx.captured_queries) == 4
    # Non-vacuity: a probe that captured nothing would match any budget.
    assert len(ctx.captured_queries) > 0


@pytest.mark.django_db
def test_reply_counts_are_grouped_and_ignore_tombstones(alice, bob, dune):
    one = _review(alice, dune)
    two = _review(bob, dune)
    _reply(bob, one)
    _reply(alice, one)
    gone = _reply(bob, two)
    gone.delete()
    assert reply_counts([one.pk, two.pk]) == {one.pk: 2}
    assert reply_counts([]) == {}


@pytest.mark.django_db
def test_reply_counts_runs_no_query_for_an_empty_id_list(db):
    with CaptureQueriesContext(connection) as ctx:
        assert reply_counts([]) == {}
    assert len(ctx.captured_queries) == 0


# --- The feed (decision 2) ---------------------------------------------------


@pytest.mark.django_db
def test_feed_for_excludes_replies(alice, bob, dune):
    alice.follows.add(bob)
    parent = _review(bob, dune)
    reply = _reply(alice, parent)
    assert parent in list(Status.feed_for(alice))
    assert reply not in list(Status.feed_for(alice))


@pytest.mark.django_db
def test_a_reply_adds_no_feed_row(alice, bob, dune, admin):
    # The whole of decision 2, measured on the entry list: reading a post
    # and replying to it never grows the feed.
    alice.follows.add(bob)
    parent = _review(bob, dune)
    before = len(feed_entries(alice))
    _reply(alice, parent)
    assert len(feed_entries(alice)) == before


@pytest.mark.django_db
def test_a_reply_does_not_appear_as_a_top_level_row_on_the_home_page(
    alice, bob, dune, admin
):
    alice.follows.add(bob)
    parent = _review(bob, dune)
    _reply(alice, parent, "<p>A reply that must not be a row.</p>")
    body = _home(_login("alice"))
    assert "Spice must be reviewed." in body  # the post's row really rendered…
    assert "A reply that must not be a row." not in body  # …without the reply


@pytest.mark.django_db
def test_feed_reply_counts_are_batched_not_paid_per_row(alice, bob, dune):
    # Six posts, each carrying its own replies. Batched, the feed costs the
    # same number of queries whether those replies number 21 or none.
    bob.follows.add(alice)
    posts = []
    for i in range(6):
        film = Film.objects.create(title=f"Film {i}", year=2000 + i)
        posts.append(_review(alice, film))
    for index, post in enumerate(posts):
        for _ in range(index + 1):
            _reply(bob, post)
    with CaptureQueriesContext(connection) as ctx:
        entries = feed_entries(bob)
    with_replies = len(ctx.captured_queries)
    assert sum(e.reply_count for e in entries) == 21  # the counts are real…
    assert with_replies > 0  # …and the probe can see queries at all
    Status.objects.filter(reply_parent__isnull=False).delete()
    with CaptureQueriesContext(connection) as ctx2:
        feed_entries(bob)
    assert len(ctx2.captured_queries) == with_replies


@pytest.mark.django_db
def test_a_folded_row_counts_the_replies_on_the_review_it_stands_for(alice, dune):
    # The fold again (R35/R83): the row reads as a shelf event, so the
    # number on it has to be the review's replies, not the shelf's.
    mark_watched(alice, dune, rating="4.5", content="<p>Desert planet.</p>")
    review = Status.objects.get(user=alice, film=dune)
    entry = next(e for e in feed_entries(alice) if e.status_id == review.pk)
    assert entry.kind == "watched"
    assert entry.reply_count == 0
    User.objects.create_user(localname="zoe", password="s3cretpass")
    zoe = User.objects.get(localname="zoe")
    _reply(zoe, review)
    entry = next(e for e in feed_entries(alice) if e.status_id == review.pk)
    assert entry.reply_count == 1


# --- The endpoint ------------------------------------------------------------


@pytest.mark.django_db
def test_reply_endpoint_writes_the_reply_and_answers_json(alice, bob, dune):
    parent = _review(alice, dune)
    response = _login("bob").post(
        f"/status/{parent.pk}/reply/", {"content": "Agreed, all the way."}
    )
    assert response.status_code == 200
    data = json.loads(response.content.decode())
    assert data["count"] == 1
    reply = Status.objects.get(reply_parent=parent)
    assert reply.user == bob
    assert reply.status_type == Status.Type.COMMENT
    assert reply.film_id == dune.id
    assert reply.raw_content == "Agreed, all the way."


@pytest.mark.django_db
def test_reply_endpoint_returns_the_row_rendered_by_the_pages_own_partial(
    alice, bob, dune
):
    # One source of truth for a reply row: the JSON carries the same
    # server-rendered markup the thread loop renders, so the row comments.js
    # appends cannot drift from the template.
    parent = _review(alice, dune)
    data = json.loads(
        _login("bob")
        .post(f"/status/{parent.pk}/reply/", {"content": "Agreed."})
        .content.decode()
    )
    assert "review reply" in data["html"]
    assert "Agreed." in data["html"]
    page = _login("bob").get(f"/status/{parent.pk}/").content.decode()
    assert data["html"].strip() in page


@pytest.mark.django_db
def test_reply_content_is_rendered_as_markdown_not_stored_raw(alice, bob, dune):
    # The same write-time gate a review gets: with no allowed link domains
    # the href is stripped and the anchor text stays.
    parent = _review(alice, dune)
    _login("bob").post(
        f"/status/{parent.pk}/reply/",
        {"content": "Like it, see [here](https://elsewhere.example/x)."},
    )
    reply = Status.objects.get(reply_parent=parent)
    assert "href" not in reply.content
    "see here" in reply.content
    assert "elsewhere.example" not in reply.content


@pytest.mark.django_db
def test_reply_endpoint_rejects_blank_content(alice, dune):
    parent = _review(alice, dune)
    for blank in ("", "   ", "\n\n"):
        response = _login("alice").post(
            f"/status/{parent.pk}/reply/", {"content": blank}
        )
        assert response.status_code == 400
    assert Status.objects.filter(reply_parent=parent).count() == 0


@pytest.mark.django_db
def test_reply_endpoint_requires_login(dune):
    parent = _review(User.objects.create_user(localname="zed", password="s3c"), dune)
    response = Client().post(f"/status/{parent.pk}/reply/", {"content": "hi"})
    assert response.status_code == 302
    assert "/login/" in response["Location"]


@pytest.mark.django_db
def test_reply_endpoint_refuses_a_remote_mirror(alice, dune):
    # R85: the page hides the composer from a mirror, so the route has to
    # refuse one too — otherwise a hand-made request writes a reply this
    # instance has no way to deliver.
    carol, mirror = _mirror(dune)
    response = _login("alice").post(f"/status/{mirror.pk}/reply/", {"content": "hi"})
    assert response.status_code == 404
    assert Status.objects.filter(reply_parent=mirror).count() == 0


@pytest.mark.django_db
def test_reply_endpoint_refuses_a_deleted_post(alice, dune):
    parent = _review(alice, dune)
    parent.delete()
    response = _login("alice").post(f"/status/{parent.pk}/reply/", {"content": "hi"})
    assert response.status_code == 404


@pytest.mark.django_db
def test_reply_endpoint_requires_a_csrf_token(alice, dune):
    parent = _review(alice, dune)
    client = Client(enforce_csrf_checks=True)
    assert client.login(username="alice", password="s3cretpass")
    response = client.post(f"/status/{parent.pk}/reply/", {"content": "hi"})
    assert response.status_code == 403


@pytest.mark.django_db
def test_reply_endpoint_is_post_only(alice, dune):
    parent = _review(alice, dune)
    assert _login("alice").get(f"/status/{parent.pk}/reply/").status_code == 405


# --- The composer ------------------------------------------------------------


@pytest.mark.django_db
def test_the_post_page_offers_a_composer_to_a_member_on_a_local_post(alice, dune):
    parent = _review(alice, dune)
    body = _login("alice").get(f"/status/{parent.pk}/").content.decode()
    assert (
        f'class="reply-form" method="post" action="/status/{parent.pk}/reply/"' in body
    )
    assert "Reply to alice" in body


@pytest.mark.django_db
def test_the_post_page_offers_an_anonymous_reader_no_composer(alice, dune):
    parent = _review(alice, dune)
    body = Client().get(f"/status/{parent.pk}/").content.decode()
    assert "Spice must be reviewed." in body  # the page rendered…
    assert "reply-form" not in body  # …with no composer


@pytest.mark.django_db
def test_the_post_page_offers_no_composer_on_a_mirror(alice, dune):
    carol, mirror = _mirror(dune)
    body = _login("alice").get(f"/status/{mirror.pk}/").content.decode()
    assert "Their review of Dune." in body  # the mirror page really rendered…
    assert "reply-form" not in body  # …and really carries no composer


# --- The thread on the page --------------------------------------------------


@pytest.mark.django_db
def test_the_thread_counts_what_it_renders(alice, bob, dune):
    parent = _review(alice, dune)
    _reply(bob, parent)
    body = _login("alice").get(f"/status/{parent.pk}/").content.decode()
    assert "Replies (1)" in body
    assert "No replies yet." not in body


@pytest.mark.django_db
def test_a_direct_reply_carries_no_replying_to_line(alice, bob, dune):
    parent = _review(alice, dune)
    _reply(bob, parent)
    body = _login("alice").get(f"/status/{parent.pk}/").content.decode()
    assert "replying to" not in body


@pytest.mark.django_db
def test_a_reply_to_a_reply_is_labelled_with_who_it_answers(alice, bob, dune):
    parent = _review(alice, dune)
    first = _reply(bob, parent, "<p>First.</p>")
    _reply(alice, first, "<p>Answering bob.</p>")
    body = _login("alice").get(f"/status/{parent.pk}/").content.decode()
    assert "replying to bob" in body
    assert "Replies (2)" in body


@pytest.mark.django_db
def test_a_reply_to_a_blocked_users_reply_names_the_hidden_reply_not_the_user(
    alice, bob, dune
):
    # Bob's row is hidden from Alice. Naming him in the attribution line of
    # the reply that survives would leak the one thing blocking hides.
    parent = _review(alice, dune)
    first = _reply(bob, parent, "<p>Bob's turn.</p>")
    _reply(alice, first, "<p>Answering a hidden reply.</p>")
    alice.blocks.add(bob)
    body = _login("alice").get(f"/status/{parent.pk}/").content.decode()
    assert "Bob's turn." not in body
    assert "replying to a hidden reply" in body
    assert "replying to bob" not in body
    assert "Replies (1)" in body


@pytest.mark.django_db
def test_a_reply_under_a_deleted_reply_is_still_rendered(alice, bob, dune):
    root = _review(alice, dune)
    middle = _reply(bob, root, "<p>Gone.</p>")
    _reply(alice, middle, "<p>Underneath, still readable.</p>")
    middle.delete()
    body = _login("alice").get(f"/status/{root.pk}/").content.decode()
    assert "Underneath, still readable." in body
    assert "replying to a deleted reply" in body
    assert "Replies (1)" in body


@pytest.mark.django_db
def test_a_reply_to_a_deleted_post_says_its_parent_is_gone(alice, bob, dune):
    # The orphan rule. The deleted post 404s on its own URL, so this page
    # is the only place the reply can be read, and it must not read as
    # addressed to nothing.
    parent = _review(alice, dune)
    reply = _reply(bob, parent, "<p>Replying to a post about to go.</p>")
    parent.delete()
    assert Client().get(f"/status/{parent.pk}/").status_code == 404
    body = _login("bob").get(f"/status/{reply.pk}/").content.decode()
    assert "Replying to a post about to go." in body
    assert "This replies to a post that has been deleted." in body


@pytest.mark.django_db
def test_a_local_post_without_a_deleted_parent_shows_no_orphan_line(alice, dune):
    # The absence is scoped to a page that rendered, so this cannot pass
    # because the line was never reachable in the first place.
    parent = _review(alice, dune)
    body = _login("alice").get(f"/status/{parent.pk}/").content.decode()
    assert "Spice must be reviewed." in body
    assert "has been deleted" not in body


# --- The feed row's reply count ----------------------------------------------


@pytest.mark.django_db
def test_a_feed_row_shows_its_reply_count(alice, bob, dune, admin):
    alice.follows.add(bob)
    parent = _review(bob, dune)
    _reply(alice, parent)
    _reply(alice, parent)
    body = _home(_login("alice"))
    row = _row(body, parent.pk)
    assert "2 replies" in row


@pytest.mark.django_db
def test_a_mirror_row_shows_its_reply_count_and_no_control(alice, dune, admin):
    # R85 on the feed: the number is shown on a mirror, the offer is not.
    carol, mirror = _mirror(dune)
    alice.follows.add(carol)
    zoe = User.objects.create_user(localname="zoe", password="s3cretpass")
    _reply(zoe, mirror)
    own = _review(alice, Film.objects.create(title="Alice film", year=1999))
    body = _home(_login("alice"))
    mirror_row = _row(body, mirror.pk)
    assert "1 reply" in mirror_row
    assert "reply-form" not in mirror_row
    assert "like-btn" not in mirror_row
    # Non-vacuity: the same page carries a local row that really does have
    # the control, so the absence above is not just a control-free page.
    assert 'class="like-btn"' in _row(body, own.pk)


@pytest.mark.django_db
def test_a_row_with_no_replies_shows_no_count(alice, bob, dune, admin):
    alice.follows.add(bob)
    parent = _review(bob, dune)
    body = _home(_login("alice"))
    row = _row(body, parent.pk)
    assert "Spice must be reviewed." in row  # the row rendered…
    assert "reply" not in row  # …and carries no reply count


@pytest.mark.django_db
def test_a_bare_shelf_row_carries_no_reply_count(alice, dune, admin):
    shelve_to_watchlist(alice, dune)
    body = _home(_login("alice"))
    row = _row(body, "none")
    assert "to their Watchlist" in row  # the row rendered…
    assert "reply" not in row  # …and carries no reply count
