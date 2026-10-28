"""N6: the Local and All scopes (plan §7 contract, §8 increment).

Three rules organise this file.

**The scope definitions the owner settled (2026-10-28).** Home (Following) is
every user you follow, remote or local, plus yourself. Local is public posts
authored *here*, whether you follow the author or not. All is Local plus the
public posts of the remote instances this instance federates with, plus the
posts of users you follow wherever they live. Local and All are **posts only**:
shelf events stay a Home thing, because Home is the scope whose membership rule
is what produces them.

**The §7 non-negotiables.** Not deleted; authors not suspended and not banned;
domain-blocked servers excluded; per-viewer blocks applied; top-level only;
pagination safe against posts that land mid-scroll; and no unfiltered "every
stored status" path. Each of those gets its own test here on purpose — the
failure mode is one missing clause in a shared filter, and a single combined
test would pass while one of them was wide open.

**Following must not have changed.** The last test in the membership section
asserts that against ``feed_entries`` directly, because §7 says so and because
every other test in this file could pass with a broken Home feed.
"""

import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from reeltalk.core.models import (
    FEED_PAGE_SIZE,
    Film,
    Status,
    encode_feed_cursor,
    public_feed,
)
from reeltalk.lists.services import create_list
from reeltalk.moderation.models import block_domain
from reeltalk.tests.feed import spread_shelve, unpaged
from reeltalk.tests.members import member, site_admin

User = get_user_model()
PASSWORD = "s3cretpass"
PAGE = FEED_PAGE_SIZE


@pytest.fixture(autouse=True)
def admin(db):
    """R12: ``/`` redirects to ``/setup/`` until a superuser exists, and then
    every render assertion passes vacuously. Autouse because half this file
    renders the page as a stranger, who has no other reason to bring one."""
    return site_admin(localname="admin", password="s3cretpass")


@pytest.fixture
def alice(db):
    """The viewer. Follows nobody by default — Local must not need follows."""
    return member(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    """A local member alice does not follow."""
    return member(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


def _member_client(localname="alice") -> Client:
    client = Client()
    assert client.login(username=localname, password=PASSWORD), (
        f"{localname} did not sign in — the page below would render anonymously "
        "and every assertion on it would pass vacuously"
    )
    return client


def _post(user, film, marker, *, status_type=Status.Type.COMMENT, **kwargs) -> Status:
    """A live post about ``film`` whose content names it in the rendered row.

    A comment by default rather than a review: D5's one-review-per-user-per-film
    is a database constraint, so a fixture that wrote reviews in a loop would
    fail on the second row for a reason that has nothing to do with the scope
    under test.
    """
    return Status.objects.create(
        user=user,
        film=film,
        status_type=status_type,
        content=f"<p>{marker}</p>",
        published_date=kwargs.pop("published_date", timezone.now()),
        **kwargs,
    )


def _remote_author(localname="carol@remote.example", host="remote.example") -> User:
    """A remote mirror account, as federation creates it (no local password)."""
    user = User(
        localname=localname,
        local=False,
        actor_url=f"https://{host}/users/{localname.split('@')[0]}",
        inbox_url=f"https://{host}/users/{localname.split('@')[0]}/inbox",
    )
    user.set_unusable_password()
    user.save()
    return user


def _mirror(author, film, marker, host="remote.example") -> Status:
    """A mirrored (remote) status — ``local=False``, carrying its home URL."""
    return Status.objects.create(
        user=author,
        film=film,
        status_type=Status.Type.REVIEW,
        content=f"<p>{marker}</p>",
        local=False,
        remote_url=f"https://{host}/notes/{marker}",
    )


def _local_ids(viewer=None, **kwargs) -> set[int]:
    entries, _ = public_feed(viewer, local_only=True, **kwargs)
    return {entry.status_id for entry in entries}


def _all_ids(viewer=None, **kwargs) -> set[int]:
    entries, _ = public_feed(viewer, local_only=False, **kwargs)
    return {entry.status_id for entry in entries}


def _feed_region(body: str) -> str:
    """The rendered feed list alone, so a check cannot borrow the rail."""
    start = body.index('<ul class="review-list"')
    return body[start : body.index("</ul>", start)]


def _row_ids(region: str) -> list[str]:
    """``data-status`` of every rendered row, in page order."""
    return re.findall(r'<li class="review" data-status="(\d+)"', region)


# --- The scope definitions ----------------------------------------------------


def test_local_shows_a_local_post_from_someone_you_do_not_follow(alice, bob, dune):
    """The whole point of Local: no follow required. The owner's definition."""
    post = _post(bob, dune, "unfollowed local review")
    assert post.pk in _local_ids(alice)


def test_local_excludes_a_remote_mirror(alice, dune):
    """Local is authored here. ``Status.local`` is the authoritative flag, not
    the author's account flag — a mirror is not a local post."""
    mirror = _mirror(_remote_author(), dune, "remote review")
    assert mirror.pk not in _local_ids(alice)
    assert mirror.pk in _all_ids(alice), "All is the scope that holds mirrors"


def test_all_holds_local_and_mirrored_posts_together(alice, bob, dune):
    local = _post(bob, dune, "local one")
    mirror = _mirror(_remote_author(), dune, "remote one")
    ids = _all_ids(alice)
    assert {local.pk, mirror.pk} <= ids


def test_a_followed_remote_author_is_in_all_and_not_in_local(alice, dune):
    """The follow set reaches across instances in All; locality still decides
    Local. Both halves matter: following must not smuggle a mirror into Local."""
    carol = _remote_author()
    alice.follows.add(carol)
    mirror = _mirror(carol, dune, "followed remote review")
    assert mirror.pk in _all_ids(alice)
    assert mirror.pk not in _local_ids(alice)


def test_local_and_all_hold_no_shelf_events(alice, dune):
    """Owner ruling 2026-10-28: Home is the users you follow, Local and All are
    posts. A watchlist event is not a post, so it appears in neither — not even
    alice's own, which Home does show."""
    spread_shelve(alice, 3, timezone.now())
    home_kinds = {entry.kind for entry in unpaged(alice)}
    assert "watchlist" in home_kinds, "Home must still carry the shelf events"
    assert _local_ids(alice) == set()
    assert _all_ids(alice) == set()


def test_following_is_unchanged_by_the_public_scopes(alice, bob, dune):
    """§7's hard line: the Following queryset did not move. An unfollowed local
    post is invisible at home and visible on Local — the two scopes differ in
    exactly the way the owner defined, and Home is still its own rule."""
    post = _post(bob, dune, "not followed")
    assert post.pk not in {entry.status_id for entry in unpaged(alice)}
    alice.follows.add(bob)
    assert post.pk in {entry.status_id for entry in unpaged(alice)}
    assert post.pk in _local_ids(alice)


def test_a_list_face_is_a_post_and_appears_in_local(alice, bob):
    """The knock-on the owner confirmed: a list's face is a top-level public
    post, so Local carries it. ``is_list_face`` is what the row's reply gate
    reads, so the row also has to be built the same way (R85)."""
    listed = create_list(bob, title="Films for a rainy Sunday")
    assert listed.status.pk in _local_ids(alice)


# --- The §7 non-negotiables ---------------------------------------------------


def test_a_reply_gets_no_row_in_either_public_scope(alice, bob, dune):
    """Top-level only (R83 decision 2, and what Mastodon's public timelines do).
    The reply still exists and still renders on the post page — it just is not
    a feed row."""
    parent = _post(bob, dune, "the post")
    reply = _post(bob, dune, "the reply", reply_parent=parent)
    assert reply.pk not in _local_ids(alice)
    assert reply.pk not in _all_ids(alice)
    assert parent.pk in _local_ids(alice), "the parent must still be there"


def test_a_soft_deleted_post_is_absent(alice, bob, dune):
    post = _post(bob, dune, "deleted review")
    post.delete()  # soft: the row stays as a tombstone (R18/§3.2)
    assert Status.objects.filter(pk=post.pk).exists(), "delete must be a soft one"
    assert post.pk not in _local_ids(alice)
    assert post.pk not in _all_ids(alice)


def test_a_suspended_authors_post_is_absent_from_both_scopes(alice, bob, dune):
    post = _post(bob, dune, "suspended author")
    bob.suspend(reason="spam ring")
    assert post.pk not in _local_ids(alice)
    assert post.pk not in _all_ids(alice)


def test_a_banned_authors_post_is_absent(alice, bob, dune):
    """Two clauses cover this and both are wanted: ban soft-deletes the posts,
    and the author-state clause is what catches an account that is gone without
    every row having been reached."""
    post = _post(bob, dune, "banned author")
    bob.ban(reason="bad actor")
    assert post.pk not in _local_ids(alice)
    assert post.pk not in _all_ids(alice)


def test_a_domain_blocked_servers_mirror_is_absent_from_all(alice, dune):
    """§7 asks this filter and asks it to be verified against ingest rather
    than assumed. Blocking a server suspends every mirror held from it (R105),
    so the account-state clause already excludes the content — no second
    domain-block join. Asserting presence *before* the block is what makes the
    absence afterwards mean something."""
    smuggled = _remote_author("mallory@bad.example", host="bad.example")
    mirror = _mirror(smuggled, dune, "from a blocked server", host="bad.example")
    assert mirror.pk in _all_ids(alice), "precondition: the mirror is held here"
    block_domain("bad.example", reason="spam")
    assert mirror.pk not in _all_ids(alice)


def test_a_blocked_author_is_gone_from_your_local_but_not_everyones(alice, bob, dune):
    """Per-viewer state must stay per-viewer: the block removes bob from alice's
    scopes and from nobody else's."""
    dave = member(localname="dave", password=PASSWORD)
    post = _post(bob, dune, "blocked author")
    alice.blocks.add(bob)
    assert post.pk not in _local_ids(alice)
    assert post.pk not in _all_ids(alice)
    assert post.pk in _local_ids(dave), "a block is one viewer's decision, not all"
    assert post.pk in _local_ids(None), "and an anonymous reader has no blocks"


# --- Anonymous behaviour (owner decision: strangers read Local only) ----------


def test_anonymous_home_serves_local_posts(alice, bob, dune):
    """The owner's ruling, and the reason the landing copy moved: a stranger
    gets the instance's public local posts, not an empty feed they cannot use."""
    post = _post(bob, dune, "stranger-readable")
    body = Client().get("/").content.decode()
    assert "stranger-readable" in _feed_region(body)
    assert _row_ids(_feed_region(body)) == [str(post.pk)]


def test_anonymous_sees_local_only_in_the_strip(alice, admin):
    """No tab that bounces a stranger to a login page: Following and All are
    absent, Local is present and marked."""
    body = Client().get("/").content.decode()
    start = body.index('<nav class="feed-scopes"')
    strip = body[start : body.index("</nav>", start)]
    assert ">Local<" in strip and 'aria-current="page"' in strip
    assert "Following" not in strip and ">All<" not in strip


@pytest.mark.parametrize("scope", ["following", "all", "bogus"])
def test_anonymous_asked_for_a_scope_they_cannot_read_gets_local(
    alice, bob, dune, scope
):
    """Degrades, never scolds — the same rule N5 set for a not-yet-live scope."""
    _post(bob, dune, "local fallback for strangers")
    response = Client().get(f"/?scope={scope}")
    assert response.status_code == 200
    assert "local fallback for strangers" in _feed_region(response.content.decode())


def test_anonymous_never_sees_a_remote_mirror(alice, dune):
    """The other half of the ruling: remote posts need an account."""
    _mirror(_remote_author(), dune, "remote post for a stranger")
    body = Client().get("/").content.decode()
    assert "remote post for a stranger" not in body


def test_anonymous_rows_carry_no_applaud_or_reply_control(alice, bob, dune):
    """Both routes behind those buttons are ``login_required``. A button whose
    POST bounces to a login page is the offer-without-the-route R85 forbids, so
    the page withholds what the route refuses."""
    _post(bob, dune, "a row a stranger can read")
    body = Client().get("/").content.decode()
    assert "a row a stranger can read" in _feed_region(body)
    assert "applaud-btn" not in body
    assert "reply-btn" not in body


def test_a_member_gets_the_controls_on_the_same_row(alice, bob, dune):
    """Non-vacuity for the test above: the markup is absent for the stranger,
    not gone from the partial."""
    _post(bob, dune, "a row a member can read")
    body = _member_client().get("/?scope=local").content.decode()
    assert "applaud-btn" in body and "reply-btn" in body


# --- Pagination (the cursor machinery, reused) --------------------------------


def _make_posts(bob, dune, count, *, newest_at):
    """``count`` posts, ``count`` the newest, a minute apart."""
    return [
        _post(
            bob,
            dune,
            f"paged post {i:03d}",
            published_date=newest_at - timedelta(minutes=i),
        )
        for i in range(count)
    ]


def test_local_pages_walk_the_scope_with_no_gap_and_no_duplicate(alice, bob, dune):
    """The identity that has to hold in every scope: paging to the end returns
    exactly the unpaged list, in order."""
    _make_posts(bob, dune, PAGE * 2 + 5, newest_at=timezone.now())
    whole, _ = public_feed(alice, local_only=True)
    seen: list[int] = []
    cursor = None
    for _ in range(100):
        entries, cursor = public_feed(alice, local_only=True, limit=PAGE, cursor=cursor)
        seen.extend(entry.status_id for entry in entries)
        if cursor is None:
            break
    else:
        pytest.fail("the public cursor never terminated")
    assert seen == [entry.status_id for entry in whole]


def test_a_post_landing_mid_scroll_does_not_shift_a_row_onto_the_next_page(
    alice, bob, dune
):
    """The reason the cursor is a key and not an offset: a fresh post between
    two page requests must not duplicate the last row of page 1 or drop one."""
    _make_posts(bob, dune, PAGE + 2, newest_at=timezone.now() - timedelta(hours=1))
    page1, cursor = public_feed(alice, local_only=True, limit=PAGE)
    assert cursor is not None
    _post(bob, dune, "landed between the two requests")  # newer than everything
    page2, _ = public_feed(alice, local_only=True, limit=PAGE, cursor=cursor)
    page2_ids = [entry.status_id for entry in page2]
    assert not {entry.status_id for entry in page1} & set(page2_ids), "row repeated"
    assert page2_ids, "the next page came back empty"


def test_an_unreadable_cursor_is_a_first_page_not_an_error(alice, bob, dune):
    _make_posts(bob, dune, 3, newest_at=timezone.now())
    entries, _ = public_feed(alice, local_only=True, limit=PAGE, cursor="not-a-cursor")
    assert len(entries) == 3


def test_a_cursor_from_a_shelf_row_still_names_a_correct_boundary(alice, bob, dune):
    """The cursor carries a kind rank, and a shelf row sorts *newer* than a
    same-timestamp status. Pasting a Home cursor onto a Local URL has to mean
    what the sort says it means, not silently drop the same-minute rows."""
    same_time = timezone.now() - timedelta(hours=2)
    post = _post(bob, dune, "same minute as a shelf row", published_date=same_time)
    spread_shelve(alice, 1, same_time)
    home_entries = unpaged(alice)
    shelf = next(entry for entry in home_entries if entry.kind == "watchlist")
    assert shelf.date == post.published_date, "the fixtures must tie on purpose"
    entries, _ = public_feed(
        alice, local_only=True, limit=PAGE, cursor=encode_feed_cursor(shelf)
    )
    assert post.pk in {entry.status_id for entry in entries}


# --- The fragment route and the Older link ------------------------------------


def test_the_fragment_serves_the_scope_it_is_asked_for(alice, bob, dune):
    """N5 left this route scope-blind on purpose (one scope made it invisible).
    With three scopes it is load-bearing: a scope-blind fragment would append
    Home rows under a Local tab."""
    _make_posts(bob, dune, PAGE + 2, newest_at=timezone.now() - timedelta(hours=1))
    _, cursor = public_feed(alice, local_only=True, limit=PAGE)
    expected, _ = public_feed(alice, local_only=True, limit=PAGE, cursor=cursor)
    html = _member_client().get(f"/feed/page/?scope=local&c={cursor}").content.decode()
    assert [int(pk) for pk in _row_ids(html)] == [e.status_id for e in expected]


def test_the_fragment_serves_local_to_anonymous(alice, bob, dune):
    """The route stopped being ``login_required`` because a stranger's scroll
    needs it exactly as a member's does. This is the test that says so — and it
    is a 200 of feed rows, not a login page spliced into the list."""
    _make_posts(bob, dune, PAGE * 2 + 2, newest_at=timezone.now() - timedelta(hours=1))
    _, cursor = public_feed(None, local_only=True, limit=PAGE)
    response = Client().get(f"/feed/page/?scope=local&c={cursor}")
    assert response.status_code == 200
    assert len(_row_ids(response.content.decode())) == PAGE


def test_the_older_link_carries_the_scope(alice, bob, dune):
    """A link that dropped the scope would send the reader back to the default
    feed one page down. It stays the plain ``/?c=`` when the scope already IS
    the visitor's default, which is why both directions are asserted — and
    whose default matters: a member's is Following, a stranger's is Local, so
    the same Local page pays for the parameter for one of them and not the
    other."""
    _make_posts(bob, dune, PAGE + 2, newest_at=timezone.now() - timedelta(hours=1))
    # alice follows nobody by default, and an empty Following feed has no Older
    # link to inspect — the no-parameter half needs a default scope that pages.
    alice.follows.add(bob)
    scoped = _member_client().get("/?scope=local").content.decode()
    assert 'href="/?scope=local&amp;c=' in scoped
    plain = _member_client().get("/").content.decode()
    assert 'href="/?c=' in plain, "the default scope must not pay for a parameter"


def test_a_strangers_older_link_needs_no_scope_parameter(alice, bob, dune):
    """The other side of the same rule: Local *is* a stranger's default, so
    their link stays the bare ``/?c=``. Asserting it from the anonymous side is
    what proves the template asks *this visitor's* default and not a constant."""
    _make_posts(bob, dune, PAGE + 2, newest_at=timezone.now() - timedelta(hours=1))
    body = Client().get("/").content.decode()
    assert 'href="/?c=' in body
    assert 'href="/?scope=local&amp;c=' not in body
