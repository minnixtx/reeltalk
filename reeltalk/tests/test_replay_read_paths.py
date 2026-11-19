"""Replay P1: the gate and the counters (``REPLAY_PLAN.md`` §4 P1).

**What this file can test today.** A replay has no row on any page yet - the
control lands in P2, the Following row in P3, Local and All in P4, the wire in
P5. What P1 builds is the three things every one of those will read from: the
``Replay`` table, the single gate ``may_replay``, and the two batched counters.
That is what is asserted here and nothing else - P3 and P4 extend this same file
with their feed matrices rather than opening new ones, because the viewer
classes below are the same classes a feed row has to answer for.

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
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from reeltalk.core.models import (
    Film,
    Replay,
    Status,
    Visibility,
    may_replay,
    replay_counts,
    replayed_ids,
    toggle_replay,
)
from reeltalk.tests.members import member

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
    it is the entire "off" transition and there is nothing to update in place."""
    target = posts["public"]
    assert toggle_replay(stranger, target) is True
    assert Replay.objects.filter(user=stranger, status=target).count() == 1
    assert toggle_replay(stranger, target) is False
    assert Replay.objects.filter(user=stranger, status=target).count() == 0


@pytest.mark.django_db
def test_unreplaying_then_replaying_mints_a_new_origin_id(db, posts, stranger):
    """Delete-then-create is what keeps a re-replay from looking like a
    redelivery of the first one, so the second row must be a different object
    with a different identity."""
    target = posts["public"]
    toggle_replay(stranger, target)
    first = Replay.objects.get(user=stranger, status=target).origin_id
    toggle_replay(stranger, target)
    toggle_replay(stranger, target)
    second = Replay.objects.get(user=stranger, status=target).origin_id
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
