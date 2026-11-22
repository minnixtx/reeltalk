"""The replay read paths: the gate, the counters and the feed's replay rows
(``REPLAY_PLAN.md`` §4 P1, P3 and P4).

**What is asserted here.** P1 built the three things every surface reads from -
the ``Replay`` table, the single gate ``may_replay`` and the two batched
counters. P3 added the first place a replay becomes a row a reader can see: the
Following feed's replay row, with its eligibility clauses, its dedupe and its two
dates. P4 extended it with the public scopes' matrix: Local and All carry replay
rows too, and their eligibility is inherited through one subquery rather than
re-implemented. P5 (the wire) will extend this same file with its own matrices
rather than opening new ones, because the viewer classes below are the same
classes every one of those surfaces has to answer for.

The shape is copied from ``test_visibility_read_paths.py`` on purpose. The risk
of this feature is not the table; it is someone's review rendering on a screen
its author never addressed, wearing another member's name. So the central test
does not check the gate once, it answers the gate for every viewer class against
every audience, and the answers are written out as a table you can read rather
than scattered through assertions.

The ruled answers that table encodes (``REPLAY_PLAN.md`` §1):

* **public originals only** (D2). A followers-only or private original is
  replayable by nobody - not by the author's own followers, not by the author.
  This is the case the whole design is about: a replay inherits the original's
  audience, so a replay of a non-public post would be a second door for it.
  Mutate the visibility clause out of ``may_replay`` and the two right-hand
  columns of the table go red, which is the mutation check §5 asks for.
* **never your own** (D2b). The author answers False on all three - the one
  place this feature does not follow Mastodon.
* **no block clause**, deliberately. ``may_read`` has none either: blocks live
  in the scopes (``public_status_scope``, ``feed_for``), and a gate that
  invents its own block rule is a second place for the block model to drift.
  The ``blocker`` row is True on the public column because that is the ruled
  answer, not because nobody thought to check.
* **a suspended or banned author** is False everywhere: the original renders
  nowhere, so a replay of it could only ever render nowhere too.
* **suspension of the viewer is not the gate's business.** A suspended account
  cannot authenticate at all (``user_can_authenticate`` reads the derived
  ``is_active``), so the gate never meets one as an authenticated viewer. Where
  suspension bites a replay is the tally, pinned below the way
  ``test_suspension_read_sites.py`` pins it for a like.

The ``world`` fixture is autouse: every test here runs against the same five
members and the same two replay rows, so the tallies below are stated as
absolute numbers and not as deltas against whatever a test happened to set up.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from reeltalk.core.models import (
    KIND_RANK_REPLAY,
    KIND_RANK_STATUS,
    Film,
    Replay,
    Status,
    Visibility,
    _replay_rows,
    feed_entries,
    may_replay,
    public_feed,
    replay_counts,
    replayed_ids,
    toggle_replay,
)
from reeltalk.tests.members import member, site_admin

PASSWORD = "s3cretpass"
User = get_user_model()


# --- the world ---------------------------------------------------------------


@pytest.fixture
def author(db):
    """The member whose posts are under test."""
    return member(localname="author", password=PASSWORD)


@pytest.fixture
def follower(db):
    """Entitled to the author's followers rows - and to replaying none of them."""
    return member(localname="follower", password=PASSWORD)


@pytest.fixture
def stranger(db):
    """A signed-in member who follows the author nowhere."""
    return member(localname="stranger", password=PASSWORD)


@pytest.fixture
def replier(db):
    """A member who has already replayed the public original."""
    return member(localname="replier", password=PASSWORD)


@pytest.fixture
def blocker(db):
    """A member who has blocked the author."""
    return member(localname="blocker", password=PASSWORD)


@pytest.fixture
def suspended_replier(db):
    """A member who replayed the public original and was then suspended."""
    return member(localname="suspender", password=PASSWORD)


@pytest.fixture
def films(db):
    """One film per audience under test. D5's one-review-per-user-per-film rule
    is a database constraint, so three reviews on one film would fail on the
    second row for a reason unrelated to the rule under test."""
    return [
        Film.objects.create(title="Dune", year=2021, genres=["Sci-Fi"]),
        Film.objects.create(title="Arrival", year=2016, genres=["Sci-Fi"]),
        Film.objects.create(title="Annihilation", year=2018, genres=["Sci-Fi"]),
    ]


@pytest.fixture
def posts(db, author, films):
    """The three audiences, one per film, all by the same author."""
    return {
        "public": _review(author, films[0], Visibility.PUBLIC),
        "followers": _review(author, films[1], Visibility.FOLLOWERS),
        "private": _review(author, films[2], Visibility.PRIVATE),
    }


@pytest.fixture(autouse=True)
def world(db, posts, author, follower, replier, blocker, suspended_replier):
    """Apply each viewer class's relation to the author, and write the replays.

    Autouse so the numbers asserted below are absolute. The standing state after
    this fixture is: the public original carries **one** counted replay
    (``replier``) plus one from a suspended member that the tally drops, and the
    other two originals carry none.
    """
    follower.follows.add(author)
    blocker.blocks.add(author)
    toggle_replay(replier, posts["public"])
    toggle_replay(suspended_replier, posts["public"])
    suspended_replier.suspend(reason="spam ring")
    suspended_replier.refresh_from_db()
    return posts


def _review(user, film, visibility) -> Status:
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        content=f"<p>A {visibility} review.</p>",
        rating=4,
        visibility=visibility,
    )


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


# --- The gate ----------------------------------------------------------------

# Every viewer class P1 has an opinion about, and the answer for each of the
# three audiences. Writing it as one table is the point: the answers get settled
# here rather than discovered in a view in P2.
AUDIENCES = ["public", "followers", "private"]
EXPECTED = {
    "anonymous": {"public": False, "followers": False, "private": False},
    "stranger": {"public": True, "followers": False, "private": False},
    "follower": {"public": True, "followers": False, "private": False},
    "author": {"public": False, "followers": False, "private": False},
    "replier": {"public": True, "followers": False, "private": False},
    "blocker": {"public": True, "followers": False, "private": False},
    "suspended_replier": {"public": True, "followers": False, "private": False},
}


@pytest.mark.parametrize("who", sorted(EXPECTED))
@pytest.mark.parametrize("audience", AUDIENCES)
@pytest.mark.django_db
def test_the_gate_answers_every_viewer_class_against_every_audience(
    db,
    posts,
    who,
    audience,
    author,
    follower,
    stranger,
    replier,
    blocker,
    suspended_replier,
):
    """The one test this increment exists for.

    ``replier`` staying True is its own pin and not an accident of the fixture: a
    member who has already replayed a post must keep the gate open, or the
    control could never take the replay back.
    """
    viewer = {
        "anonymous": AnonymousUser(),
        "stranger": stranger,
        "follower": follower,
        "author": author,
        "replier": replier,
        "blocker": blocker,
        "suspended_replier": suspended_replier,
    }[who]
    assert may_replay(viewer, posts[audience]) is EXPECTED[who][audience]


@pytest.mark.django_db
def test_a_suspended_author_makes_their_post_not_replayable(
    db, author, posts, stranger
):
    """The post renders nowhere, so a replay of it would be an interaction on a
    hidden row - the same reason the like route 404s (R85)."""
    assert may_replay(stranger, posts["public"]) is True
    author.suspend(reason="spam ring")
    author.refresh_from_db()
    assert may_replay(stranger, posts["public"]) is False


@pytest.mark.django_db
def test_a_banned_author_makes_their_post_not_replayable(db, author, posts, stranger):
    """A ban sets ``banned_at`` and deliberately leaves ``suspended_at`` alone,
    so the gate needs both terms and neither one covers the other."""
    author.ban(reason="repeat offender")
    author.refresh_from_db()
    assert author.banned_at is not None
    assert author.suspended_at is None
    assert may_replay(stranger, posts["public"]) is False


@pytest.mark.django_db
def test_unsuspending_the_author_reopens_the_gate(db, author, posts, stranger):
    """Suspension is reversible and the gate reads a column rather than deleting
    anything, so the answer has to come back exactly."""
    author.suspend(reason="spam ring")
    author.refresh_from_db()
    assert may_replay(stranger, posts["public"]) is False
    author.unsuspend()
    author.refresh_from_db()
    assert may_replay(stranger, posts["public"]) is True


# --- The table ---------------------------------------------------------------


@pytest.mark.django_db
def test_a_replay_mints_its_own_origin_id_on_create(db, posts, replier):
    """R41/R42 day-one identity, the same discipline ``Like`` carries: a local
    replay's origin is its own pk, so P5 can build an ``Announce`` without the
    table being re-shaped for it."""
    replay = Replay.objects.get(user=replier, status=posts["public"])
    assert replay.origin_id == replay.pk


@pytest.mark.django_db
def test_a_remote_replay_is_not_minted_an_origin_id(db, posts):
    """The gate is the actor's locality, not the row's existence: a replay that
    arrived from another instance carries its home's identity, not one minted
    here."""
    carol = _remote_user()
    Replay.objects.create(user=carol, status=posts["public"])
    assert Replay.objects.get(user=carol).origin_id is None


@pytest.mark.django_db
def test_the_unique_constraint_blocks_a_second_replay_of_the_same_pair(
    db, posts, replier
):
    """One replay per member per post is enforced at the database, not in a
    view - and it is the same constraint that makes a redelivered remote
    ``Announce`` land on the existing row instead of double-counting."""
    with pytest.raises(IntegrityError):
        Replay.objects.create(user=replier, status=posts["public"])


@pytest.mark.django_db
def test_two_members_can_replay_the_same_post(db, posts, replier, stranger):
    toggle_replay(stranger, posts["public"])
    holders = {row.user_id for row in Replay.objects.filter(status=posts["public"])}
    assert {replier.pk, stranger.pk}.issubset(holders)


@pytest.mark.django_db
def test_replays_order_newest_first(db, posts, replier, suspended_replier, stranger):
    """P3 pages a feed off the replay's own date (D11), so the default ordering
    is load-bearing rather than cosmetic. The three rows are aged apart rather
    than two of them sharing a timestamp, because a tie would let the secondary
    ordering decide and this test would stop naming which one it pins."""
    now = timezone.now()
    Replay.objects.filter(user=replier).update(published_date=now - timedelta(days=3))
    Replay.objects.filter(user=suspended_replier).update(
        published_date=now - timedelta(days=2)
    )
    toggle_replay(stranger, posts["public"])
    assert [row.user_id for row in Replay.objects.all()] == [
        stranger.pk,
        suspended_replier.pk,
        replier.pk,
    ]


@pytest.mark.django_db
def test_a_replay_outlives_its_original_being_soft_deleted(db, posts, replier):
    """D9, pinned so nobody "repairs" it. A soft-deleted original leaves its
    replay rows in place and nothing renders them; deleting them would be a
    second delete path with its own ordering bugs, for no visible gain."""
    target = posts["public"]
    target.delete()
    assert Replay.objects.filter(user=replier, status=target).exists()


# --- The toggle --------------------------------------------------------------


@pytest.mark.django_db
def test_toggle_replay_turns_it_on_then_off(db, posts, stranger):
    """The row's existence is the whole state, exactly as with a like: removing
    it is the entire "off" transition and there is nothing to update in place.

    The second half of the answer is the row's AP identity — the created row's
    on the way in, the deleted row's on the way out, because the outbound
    ``Announce`` builds its id from it and the off half has already deleted
    the row by the time the broadcast runs (P5)."""
    target = posts["public"]
    replayed, origin_id = toggle_replay(stranger, target)
    assert replayed is True
    row = Replay.objects.get(user=stranger, status=target)
    assert origin_id == row.origin_id == row.pk
    replayed, deleted_origin = toggle_replay(stranger, target)
    assert replayed is False
    assert deleted_origin == origin_id
    assert Replay.objects.filter(user=stranger, status=target).count() == 0


@pytest.mark.django_db
def test_unreplaying_then_replaying_mints_a_new_origin_id(db, posts, stranger):
    """Delete-then-create is what keeps a re-replay from looking like a
    redelivery of the first one, so the second row must be a different object
    with a different identity — and the toggle must hand each identity back at
    the moment its ``Announce`` or ``Undo`` is built."""
    target = posts["public"]
    _, first = toggle_replay(stranger, target)
    _, off_again = toggle_replay(stranger, target)
    assert off_again == first, "the Undo must name the Announce it undoes"
    _, second = toggle_replay(stranger, target)
    assert Replay.objects.filter(user=stranger, status=target).count() == 1
    assert first != second


# --- The counters ------------------------------------------------------------


@pytest.mark.django_db
def test_replay_counts_groups_the_tally_into_one_query(db, posts):
    """One ``GROUP BY`` answers a whole page. The zero case is the shape to
    know: a post with no replays gets no row at all rather than a zero, so
    callers read ``.get(pk, 0)``."""
    assert replay_counts([posts["public"].pk, posts["private"].pk]) == {
        posts["public"].pk: 1
    }
    assert replay_counts([]) == {}


@pytest.mark.django_db
def test_replay_counts_is_one_query_whatever_the_page_size(db, author, replier):
    """The N+1 the plan names. ``feed_entries`` hands back a Python list, so a
    per-row count would be one query per row on every home page; the cost must
    not grow with the feed."""

    def count_queries(total: int) -> int:
        made = Film.objects.filter(title__startswith="Replay count ").count()
        films = list(Film.objects.filter(title__startswith="Replay count "))
        for i in range(made, total):
            films.append(Film.objects.create(title=f"Replay count {i}", year=2000 + i))
        # get_or_create, not create: D5 allows one review per member per film,
        # and the second measurement re-walks the films the first one already
        # made rather than starting from an empty library.
        made_up = [
            Status.objects.get_or_create(
                user=author,
                film=film,
                status_type=Status.Type.REVIEW,
                defaults={"content": "<p>Counted.</p>", "rating": "4"},
            )[0]
            for film in films
        ]
        for status in made_up:
            Replay.objects.get_or_create(user=replier, status=status)
        with CaptureQueriesContext(connection) as ctx:
            replay_counts([p.pk for p in made_up])
        return sum(1 for q in ctx.captured_queries if "core_replay" in q["sql"].lower())

    small = count_queries(2)
    large = count_queries(8)
    assert small > 0, "the batched replay query never ran - the test is vacuous"
    assert small == large


@pytest.mark.django_db
def test_replay_counts_exclude_a_suspended_replier_and_come_back_on_unsuspend(
    db, posts, suspended_replier
):
    """Read with ``.get`` rather than indexing: the grouped query returns no row
    at all for a zero tally, so a missing key and a zero would otherwise be
    indistinguishable here.

    Suspension is one server-wide fact, not a per-viewer one, so excluding it
    keeps every viewer looking at the same number - and it is symmetric,
    because nothing was deleted to hide it.
    """
    assert replay_counts([posts["public"].pk]) == {posts["public"].pk: 1}
    suspended_replier.unsuspend()
    suspended_replier.refresh_from_db()
    assert replay_counts([posts["public"].pk]) == {posts["public"].pk: 2}


@pytest.mark.django_db
def test_replay_counts_survive_a_block_between_replier_and_author(db, posts, blocker):
    """The tally is viewer-independent, following the like rule: a blocked
    member's replay still counts toward the number. Blocks live in the scopes,
    and a per-viewer block list leaking into a shared tally is the drift the
    gate deliberately refuses to own."""
    toggle_replay(blocker, posts["public"])
    assert replay_counts([posts["public"].pk]) == {posts["public"].pk: 2}


@pytest.mark.django_db
def test_replayed_ids_reports_the_viewers_own_pairs(db, posts, replier, stranger):
    """The control needs the viewer's own state so a reloaded page draws the
    button as it actually stands - a button labelled "Replay" that would actually
    take a replay back is the same class of lie as one that no-ops."""
    assert replayed_ids(replier, [p.pk for p in posts.values()]) == {posts["public"].pk}
    assert replayed_ids(stranger, [posts["public"].pk]) == set()


@pytest.mark.django_db
def test_replayed_ids_is_empty_for_an_anonymous_caller(db, posts, replier):
    """The helper guards its own boundary: an AnonymousUser has no replays, and
    must not turn into an unfiltered query over everyone's."""
    assert replayed_ids(AnonymousUser(), [posts["public"].pk]) == set()
    assert replayed_ids(replier, []) == set()


@pytest.mark.django_db
def test_replayed_ids_keeps_a_suspended_members_own_row(db, posts, suspended_replier):
    """The counter excludes suspended repliers from the tally; the did-I state
    carries no such clause, for the same reason ``liked_ids`` has none - it is
    one member asking about their own rows, and a second place to apply
    suspension is a second place to disagree with the first."""
    assert suspended_replier.suspended_at is not None
    assert replayed_ids(suspended_replier, [posts["public"].pk]) == {posts["public"].pk}


# --- The Following feed's replay row (replay arc P3) -------------------------

# Whether the row exists is a property of the READER's relations, not of the
# replay, so every case below moves one relation and one only:
#
#   follows the replier, not the author      -> the row (the feature's whole point)
#   follows neither party                    -> no row: the replier is not feed material
#   follows the replier AND the author       -> no row (D3): they already see the post
#   follows the replier, blocked the author  -> no row: the block clause, load-bearing
#   the replier is suspended                 -> no row (D9)
#
# The follow direction reads backwards the first time, so it is spelled out once
# here: a home feed is made of the accounts *you* follow, so "the replay shows in
# admirer's feed" is ``admirer.follows.add(replier)``, never the reverse.


@pytest.fixture
def admin(db):
    # R12: / redirects to /setup/ until a superuser exists, so a render
    # assertion against that body would prove nothing.
    return site_admin(localname="admin", password=PASSWORD)


@pytest.fixture
def admirer(db, replier):
    """A member who follows the replier and not the author."""
    user = member(localname="admirer", password=PASSWORD)
    user.follows.add(replier)
    return user


@pytest.fixture
def keeper(db):
    """A member with a post of their own, to sit between the replay's two dates."""
    return member(localname="keeper", password=PASSWORD)


def _feed(user):
    """The feed rows, without the cursor."""
    entries, _ = feed_entries(user)
    return entries


def _replay_rows_of(user, status) -> list:
    """The replay rows in ``user``'s feed that stand for ``status``."""
    return [
        entry
        for entry in _feed(user)
        if entry.replayed_by is not None and entry.status_id == status.pk
    ]


def _eligibility(user):
    """The replay rows ``user`` is *eligible* for, with the D3 dedupe emptied out.

    ``shown=set()`` is the point and not an oversight. The dedupe is pinned by its
    own tests above, and leaving it in force here would hide the audience clause
    completely: every reader entitled to a non-public original already has that
    post in their own feed, so the dedupe drops the row for exactly the readers
    whose entitlement this is about.
    """
    return _replay_rows(user, user.feed_member_ids(), set())


def _row_in(body: str, status_id: int) -> str:
    """One row's own markup, so a check cannot borrow a neighbour's."""
    marker = f'<li class="review" data-status="{status_id}"'
    assert marker in body, f"the page holds no row for status {status_id}"
    start = body.index(marker)
    return body[start : body.index("</li>", start)]


def _row_markup(who: str, status_id: int) -> str:
    """One rendered home-feed row, by the status its row speaks for."""
    client = Client()
    assert client.login(username=who, password=PASSWORD)
    return _row_in(client.get("/").content.decode(), status_id)


@pytest.mark.django_db
def test_a_replay_row_shows_for_a_member_who_follows_the_replier(
    db, posts, author, replier, admirer, films
):
    """The row's data, all of it (``REPLAY_PLAN.md`` §4 P3).

    Everything the row *shows* is the original's - the name line, the film, the
    prose, the audience - and the three facts that make it a different row from the
    post itself are the replay's: who, where it sits in the order, and its own id
    as the cursor's row.
    """
    original = posts["public"]
    replay = Replay.objects.get(user=replier, status=original)
    rows = _replay_rows_of(admirer, original)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.user == author
    assert entry.replayed_by == replier
    assert entry.status_id == original.pk
    assert entry.source_id == replay.pk
    assert entry.film == films[0]
    assert entry.content == original.content
    assert entry.visibility == Visibility.PUBLIC
    assert entry.kind == "status"
    # The rank is what pages this row above a same-timestamp original, and it keys
    # off replayed_by and not off kind -- kind stays "status" on a replay row.
    assert entry.kind_rank == KIND_RANK_REPLAY
    # Its counters are the original's, and the row may offer the control because
    # the reader is not the author: replaying this row replays the original.
    assert entry.replay_count == 1
    assert entry.replayed_by_viewer is False
    assert entry.replayable is True


@pytest.mark.django_db
def test_no_replay_row_for_a_member_who_follows_neither_party(db, posts, stranger):
    """The replier is not in the reader's feed, so their replay is not either."""
    assert _replay_rows_of(stranger, posts["public"]) == []


@pytest.mark.django_db
def test_the_replay_row_is_suppressed_when_the_reader_already_sees_the_original(
    db, posts, author, replier
):
    """D3. Replaying a post does not resurface it for someone who already has it.

    The reader follows both parties, which is the only way to make the dedupe the
    thing under test: with the author outside their feed the row would be absent
    for an entirely different reason.
    """
    both = member(localname="both", password=PASSWORD)
    both.follows.add(author, replier)
    entries = [entry for entry in _feed(both) if entry.status_id == posts["public"].pk]
    # The post must be in the feed once, as itself, and not twice with the replay
    # row riding beside it.
    assert len(entries) == 1
    assert entries[0].replayed_by is None


@pytest.mark.django_db
def test_only_the_newest_replay_of_one_post_shows(db, posts, replier, stranger):
    """D3's second half, pinned from both directions.

    The ages are set explicitly and then swapped, so the answer cannot be an
    accident of which row was created first: the replay's own date decides.
    """
    reader = member(localname="reader", password=PASSWORD)
    reader.follows.add(replier, stranger)
    toggle_replay(stranger, posts["public"])
    now = timezone.now()

    def age(days: int) -> None:
        Replay.objects.filter(user=replier).update(
            published_date=now + timedelta(days=days)
        )

    age(-3)
    rows = _replay_rows_of(reader, posts["public"])
    assert len(rows) == 1, "two members replayed one post and the reader got two rows"
    assert rows[0].source_id == Replay.objects.get(user=stranger).pk

    age(3)  # the replier's replay is now the newer of the two
    rows = _replay_rows_of(reader, posts["public"])
    assert len(rows) == 1
    assert rows[0].source_id == Replay.objects.get(user=replier).pk


@pytest.mark.django_db
def test_a_blocked_original_author_takes_the_replay_row_away(
    db, posts, replier, author
):
    """The clause §5 names as one of the two places a replay can smuggle a post
    past a filter that already hides it.

    It is **not** redundant with feed membership: membership subtracts the blocked
    author from the *follow* set, and here the reader never followed the author in
    the first place - that is precisely what makes it a replay row. Without the
    clause the reader is handed a blocked member's post wrapped in a name they
    follow.
    """
    reader = member(localname="reader", password=PASSWORD)
    reader.follows.add(replier)
    assert len(_replay_rows_of(reader, posts["public"])) == 1
    reader.blocks.add(author)
    assert _replay_rows_of(reader, posts["public"]) == []


@pytest.mark.django_db
def test_a_suspended_repliers_row_shows_nowhere(db, posts, suspended_replier):
    """D9: the row stays in the table and renders nowhere, and unsuspending
    restores it because nothing was deleted to hide it."""
    reader = member(localname="reader", password=PASSWORD)
    reader.follows.add(suspended_replier)
    assert _replay_rows_of(reader, posts["public"]) == []
    suspended_replier.unsuspend()
    suspended_replier.refresh_from_db()
    assert len(_replay_rows_of(reader, posts["public"])) == 1


@pytest.mark.django_db
def test_a_replay_never_carries_an_audience_the_original_does_not(
    db, posts, author, replier, admirer
):
    """The clause §5 asks to be mutation-checked, and why it is pinned at the query.

    A replay inherits the original's audience, so a replay of a followers-only post
    must reach the author's followers and nobody else. Through ``feed_entries``
    that is invisible, and the reason is worth writing down because it reads like a
    gap: everyone entitled to a followers-only original already has that post in
    their own feed, so D3 drops the row for every one of them. The eligibility rule
    is therefore pinned where it is implemented, with the dedupe emptied by hand.

    It is written so that replacing ``audience_q`` with a literal
    ``visibility="public"`` filter goes red: that clause answers "no" for the
    entitled reader below, and that cell is the one which tells the two apart.
    """
    Replay.objects.create(user=replier, status=posts["followers"])
    entitled = member(localname="entitled", password=PASSWORD)
    entitled.follows.add(author, replier)
    # Both of the replier's replays are eligible here: the public one on its own
    # terms and the followers one because this reader follows its author. The
    # second is the cell a literal ``visibility="public"`` clause would drop.
    assert {entry.status_id for entry in _eligibility(entitled)} == {
        posts["public"].pk,
        posts["followers"].pk,
    }
    # A member who follows the replier and not the author is not entitled to the
    # followers row, and still gets the public original's replay.
    assert {entry.status_id for entry in _eligibility(admirer)} == {posts["public"].pk}


@pytest.mark.django_db
def test_a_replay_row_pages_by_the_replay_and_prints_the_original(
    db, posts, replier, admirer, keeper, films, admin
):
    """D11, pinned both ways on purpose: it prints the older date and pages as
    though it were new.

    The trap has no other symptom. If the template reads ``entry.date``, a review
    written a hundred days ago claims it was written today and nothing else in the
    app notices - so both halves are asserted on one row, and the paging half is a
    real cursor round trip rather than a read of ``sort_key``.
    """
    original = posts["public"]
    now = timezone.now()
    written = now - timedelta(days=100)
    Status.objects.filter(pk=original.pk).update(published_date=written)
    kept = Status.objects.create(
        user=keeper,
        film=films[1],
        status_type=Status.Type.REVIEW,
        content="<p>In between.</p>",
        rating=3,
        visibility=Visibility.PUBLIC,
        published_date=now - timedelta(days=50),
    )
    admirer.follows.add(keeper)

    entries = _feed(admirer)
    assert entries[0].replayed_by is not None, "the row is not at the replay's date"
    assert entries[0].display_date.date() == written.date()
    assert entries[0].date.date() == now.date()
    assert entries[-1].status_id == kept.pk

    # And the cursor it mints continues from where the replay sits, not from the
    # date printed on the row.
    page, cursor = feed_entries(admirer, limit=1)
    assert page[0].replayed_by is not None and cursor is not None
    older, _ = feed_entries(admirer, cursor=cursor)
    assert [entry.replayed_by for entry in older] == [None]

    # The printed line, on the rendered page.
    row = _row_markup("admirer", original.pk)
    assert written.strftime("%Y-%m-%d") in row
    assert now.strftime("%Y-%m-%d") not in row


@pytest.mark.django_db
def test_the_byline_is_the_heads_first_child_and_names_the_replier(
    db, posts, replier, admirer, admin
):
    """The rendered byline, and the row's own control pointing at the original."""
    original = posts["public"]
    row = _row_markup("admirer", original.pk)
    assert '<p class="replay-line replay-line--flush">' in row
    assert row.index('class="replay-line') < row.index('class="avatar')
    assert f'href="/user/{replier.localname}/"' in row
    assert "replayed</p>" in row
    # The row opens the original and its replay control replays the original:
    # there is no replay of a replay here.
    assert f'data-url="/status/{original.pk}/replay/"' in row


@pytest.mark.django_db
def test_the_scroll_fragment_carries_the_replay_row_and_its_cursor(
    db, posts, replier, admirer, admin
):
    """The endless scroll appends rows from ``/feed/page/``, so a replay row has
    to survive that trip: the same byline, a button with a route behind it, and the
    ``data-cursor`` the scroll reads its next request off.

    The JS half is already pinned in ``test_feed_endless_scroll.py`` — one
    delegated listener whose selector names ``.replay-btn`` matches any element in
    the document, appended or not — so what is pinned here is that the fragment
    hands that listener something to match and the scroll something to page by.
    """
    client = Client()
    assert client.login(username="admirer", password=PASSWORD)
    original = posts["public"]
    fragment = client.get("/feed/page/").content.decode()
    row = _row_in(fragment, original.pk)
    assert '<p class="replay-line replay-line--flush">' in row
    assert f'data-url="/status/{original.pk}/replay/"' in row
    assert 'data-cursor="' in row


# --- The public scopes' replay row (replay arc P4) --------------------------

# Local and All carry replay rows as a second SQL population, and their
# eligibility is **inherited** rather than re-implemented: population B is
# ``Replay.objects.filter(status__in=<population A>)``, so every clause that
# hides an original (deleted, suspension, ban, per-viewer blocks, audience,
# top-level) hides its replay by construction. §5 names that subquery, with P3's
# block exclusion, as the two places a replay can smuggle a post past a filter
# that already hides it — so every test below moves the ORIGINAL's state (or the
# viewer's relation to it) and asserts the two rows leave together. That pairing
# is the proof: a hand-copied clause list that drifts from population A passes a
# test about the replay alone and fails this one.
#
# The viewer is ``stranger`` almost everywhere — a member who follows neither
# party — because a public scope must not need a follow relation to carry the
# row, and the same absence then makes each state change the only variable.


def _public_keys(viewer, *, local_only: bool) -> set[tuple]:
    """Every row of one public scope, as ``(kind_rank, source_id)`` pairs — the
    identity that tells a replay row apart from the original it replays."""
    entries, _ = public_feed(viewer, local_only=local_only)
    return {(entry.kind_rank, entry.source_id) for entry in entries}


def _both_scopes(viewer) -> list[set[tuple]]:
    """The same scope read both ways: Local and All must agree on eligibility."""
    return [
        _public_keys(viewer, local_only=True),
        _public_keys(viewer, local_only=False),
    ]


def _status_key(status) -> tuple:
    return (KIND_RANK_STATUS, status.pk)


def _replay_key(replay) -> tuple:
    return (KIND_RANK_REPLAY, replay.pk)


@pytest.mark.django_db
def test_a_replay_row_shows_in_both_public_scopes(db, posts, author, replier, stranger):
    """The row, all of its data, in a scope with no follow relation anywhere.

    Everything shown is the original's and the three position facts are the
    replay's — the same contract the Following row pins, re-pinned here because
    population B builds its rows through the same ``_replay_entry`` and a public
    row that disagreed with the Following one would page and print differently
    per scope.
    """
    original = posts["public"]
    replay = Replay.objects.get(user=replier, status=original)
    for local_only in (True, False):
        entries, _ = public_feed(stranger, local_only=local_only)
        rows = [entry for entry in entries if entry.replayed_by is not None]
        assert len(rows) == 1, "the suspended replier's row must not render (D9)"
        entry = rows[0]
        assert entry.user == author
        assert entry.replayed_by == replier
        assert entry.status_id == original.pk
        assert entry.source_id == replay.pk
        assert entry.kind_rank == KIND_RANK_REPLAY
        # D11: the row sits at the replay's date and prints the original's.
        assert entry.date == replay.published_date
        assert entry.post_date == original.published_date
        assert entry.display_date == original.published_date


@pytest.mark.django_db
def test_a_replay_of_a_soft_deleted_post_renders_in_no_public_scope(
    db, posts, replier, stranger
):
    """The eligibility-inheritance test this increment exists for.

    The replay row leaves every public scope exactly when the original does, and
    stays while it stays — the two move together because they are filtered by one
    rule, not two that happen to match today.
    """
    original = posts["public"]
    replay = Replay.objects.get(user=replier, status=original)
    for keys in _both_scopes(stranger):
        assert _status_key(original) in keys and _replay_key(replay) in keys
    original.delete()  # soft: the row stays as a tombstone
    assert Status.objects.filter(pk=original.pk).exists(), "delete must be a soft one"
    for keys in _both_scopes(stranger):
        assert _status_key(original) not in keys
        assert _replay_key(replay) not in keys, (
            "the replay outlived the filter that hid its original"
        )


@pytest.mark.django_db
def test_a_replay_of_a_suspended_authors_post_renders_in_no_public_scope(
    db, posts, author, replier, stranger
):
    original = posts["public"]
    replay = Replay.objects.get(user=replier, status=original)
    author.suspend(reason="spam ring")
    for keys in _both_scopes(stranger):
        assert _status_key(original) not in keys
        assert _replay_key(replay) not in keys
    author.unsuspend()
    for keys in _both_scopes(stranger):
        assert _replay_key(replay) in keys, "unsuspending restores exactly what it hid"


@pytest.mark.django_db
def test_a_replay_of_a_banned_authors_post_renders_in_no_public_scope(
    db, posts, author, replier, stranger
):
    """Ban is its own clause and its own test: it sets ``banned_at`` and leaves
    ``suspended_at`` alone, so an implementation that inherited only the
    suspension clause would pass the test above and fail here."""
    original = posts["public"]
    replay = Replay.objects.get(user=replier, status=original)
    author.ban(reason="repeat offender")
    for keys in _both_scopes(stranger):
        assert _status_key(original) not in keys
        assert _replay_key(replay) not in keys


@pytest.mark.django_db
def test_a_replay_of_a_blocked_authors_post_renders_in_no_public_scope(
    db, posts, replier, stranger, blocker
):
    """The per-viewer half of the subquery. Population A already hides the
    original from the blocker (the block clause is the viewer's own); the
    inheritance is what hides the replay — and the row must survive for a member
    who has not blocked anybody, because a block is one reader's decision.
    """
    original = posts["public"]
    replay = Replay.objects.get(user=replier, status=original)
    for keys in _both_scopes(blocker):
        assert _status_key(original) not in keys
        assert _replay_key(replay) not in keys
    for keys in _both_scopes(stranger):
        assert _replay_key(replay) in keys


@pytest.mark.django_db
def test_a_suspended_repliers_replay_renders_in_no_public_scope(
    db, posts, suspended_replier, stranger
):
    """D9 across the public scopes: the replier's own suspension is the one
    eligibility clause the subquery cannot inherit, so population B carries it
    directly — the same clause the tally and the Following synthesizer apply.
    The world fixture wrote this replay before the suspension, which is exactly
    the order that leaves a row behind if the clause is missing."""
    replay = Replay.objects.get(user=suspended_replier, status=posts["public"])
    for keys in _both_scopes(stranger):
        assert _replay_key(replay) not in keys
        assert _status_key(posts["public"]) in keys, (
            "the original stays — only the suspended replay row leaves"
        )
    suspended_replier.unsuspend()
    suspended_replier.refresh_from_db()
    assert _replay_key(replay) in _public_keys(stranger, local_only=True)


@pytest.mark.django_db
def test_a_replay_of_a_non_public_post_renders_in_no_public_scope(
    db, posts, replier, follower
):
    """The audience clause rides along with the subquery.

    ``may_replay`` refuses to write this row, but the row can exist: an author
    flipping a public post to followers-only after the replay, or an inbound
    pair from a federation peer. The viewer is ``follower`` — entitled to the
    original in Following and still owed its absence from a public timeline,
    which is the cell that separates the inherited ``visibility=public`` clause
    from no clause at all.
    """
    Replay.objects.create(user=replier, status=posts["followers"])
    replay = Replay.objects.get(user=replier, status=posts["followers"])
    for keys in _both_scopes(follower):
        assert _status_key(posts["followers"]) not in keys
        assert _replay_key(replay) not in keys


@pytest.mark.django_db
def test_a_replay_of_a_reply_renders_in_no_public_scope(
    db, posts, author, replier, stranger, films
):
    """Top-level rides along too: a turn in someone else's conversation is not a
    feed row, and replaying one does not promote it."""
    reply = Status.objects.create(
        user=author,
        film=films[0],
        status_type=Status.Type.COMMENT,
        content="<p>A turn in the thread.</p>",
        reply_parent=posts["public"],
    )
    replay = Replay.objects.create(user=replier, status=reply)
    for keys in _both_scopes(stranger):
        assert _status_key(reply) not in keys
        assert _replay_key(replay) not in keys
