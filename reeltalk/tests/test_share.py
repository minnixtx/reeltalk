"""The share address and the reachability gate (``SHARE_PLAN.md`` S1).

S1 is Python only: no template, no asset, no script. What it settles is the two
answers every later surface has to ask, and each one exists because the wrong
answer is a control that hands out a link the recipient cannot open.

**What is asserted here.**

* ``Status.is_reachable_offsite`` — could a stranger with no account here open
  this post. The matrix below is the whole ruling (D4): public, not deleted,
  author neither suspended nor banned, and **the same answer for every reader**,
  an anonymous one included, because the property takes no viewer. That shape is
  the decision and not a convenience: a gate written ``may_share(user, status)``
  invites someone to "fix" the anonymous case later, which is how a control only
  half the audience can use gets shipped. The property pin below is what makes
  the absence of a viewer structural rather than a convention.
* The one place this differs from replay, pinned beside replay's own answer
  because the two rules are otherwise identical and a reader has to be able to
  see the seam: your **own** public post is shareable and not replayable.
* ``Status.share_url`` — the three shapes (local post, mirror, list face), each
  empty whenever the gate is closed, so no caller can render a control from the
  URL alone and skip it.

**The new one.** A mirror's share URL is its ``remote_url``, which is the inbound
Note's ``id`` — a peer-supplied string that has never been printed to a browser
before this feature and is stored after a check that it is a non-empty string and
nothing more. So the scheme rule is tested as an attack surface, not as a
formality: a peer that sent ``javascript:`` would otherwise be putting that
string in front of a browser for the first time through our own button.

**Mutation checks** (``SHARE_PLAN.md`` S1 "Done when"), each run and each seen
red, then reverted: widen the visibility clause to admit followers-only and the
matrix goes red in two rows; drop the scheme check and the mirror's non-http
parametrisation goes red; drop the ``netloc`` half and ``http:/no-host`` goes
red; drop the ban clause and the banned-author row goes red; return the URL
before the gate and every unreachable row goes red.
"""

from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.test.utils import override_settings
from django.urls import resolve

from reeltalk.core.models import Film, Status, Visibility, may_replay
from reeltalk.lists.services import create_list, soft_delete_list
from reeltalk.tests.members import member

PASSWORD = "s3cretpass"
User = get_user_model()

# A canonical origin that is deliberately nothing like the host Django's test
# client presents (``http://testserver``, which the suite's other URL asserts
# lean on). If ``share_url`` ever took the origin from the request again, every
# assertion below would name a host the request never sent.
CANON = "https://reeltalk.example"


# --- the world ---------------------------------------------------------------


@pytest.fixture
def author(db):
    """The member whose posts are under test."""
    return member(localname="author", password=PASSWORD)


@pytest.fixture
def films(db):
    """One film per state under test.

    D5's one-review-per-user-per-film rule is a database constraint, so two
    reviews by one author on one film would fail on the second row for a reason
    that has nothing to do with the rule under test.
    """
    titles = [
        ("Dune", 2021),
        ("Arrival", 2016),
        ("Annihilation", 2018),
        ("Stalker", 1979),
        ("Under the Skin", 2013),
    ]
    return [Film.objects.create(title=t, year=y, genres=["Sci-Fi"]) for t, y in titles]


def _review(user, film, visibility) -> Status:
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        content=f"<p>A {visibility} review.</p>",
        rating=4,
        visibility=visibility,
    )


def _remote_user(localname: str = "carol@peer.example") -> User:
    """A remote mirror account, as federation creates it (no local password)."""
    user = User(
        localname=localname,
        local=False,
        actor_url=f"https://peer.example/users/{localname.split('@')[0]}",
        inbox_url=f"https://peer.example/users/{localname.split('@')[0]}/inbox",
    )
    user.set_unusable_password()
    user.save()
    return user


def _mirror(user=None, **kwargs) -> Status:
    """A mirrored Note — the shape ``_mirror_status`` writes for a standalone post."""
    kwargs.setdefault("local", False)
    kwargs.setdefault("remote_url", "https://peer.example/users/carol/statuses/9001")
    kwargs.setdefault("status_type", None)
    kwargs.setdefault("content", "<p>A mirrored note.</p>")
    return Status.objects.create(user=user or _remote_user(), **kwargs)


@pytest.fixture
def states(db, author, films):
    """One post per state the gate is asked about, keyed by the state's name.

    The suspended and banned authors are separate members rather than the
    fixture author flipped over: those are facts about a person, and flipping
    them mid-fixture would make every other row in the table depend on the order
    the tests ran in.
    """
    suspended = member(localname="suspender", password=PASSWORD)
    suspended.suspend(reason="spam ring")
    suspended.refresh_from_db()
    banned = member(localname="banned", password=PASSWORD)
    banned.ban(reason="repeat offender")
    banned.refresh_from_db()

    tombstone = _review(author, films[3], Visibility.PUBLIC)
    tombstone.delete()

    return {
        "public": _review(author, films[0], Visibility.PUBLIC),
        "followers-only": _review(author, films[1], Visibility.FOLLOWERS),
        "private": _review(author, films[2], Visibility.PRIVATE),
        "soft-deleted": tombstone,
        "suspended author": _review(suspended, films[4], Visibility.PUBLIC),
        "banned author": _review(banned, films[0], Visibility.PUBLIC),
    }


# --- the gate ----------------------------------------------------------------

# Every state, and whether a stranger could open it. One column only, and that
# is the point: the answer does not depend on who is asking, so there is no
# viewer axis to draw (compare the replay gate's table, which has one).
REACHABLE = {
    "public": True,
    "followers-only": False,
    "private": False,
    "soft-deleted": False,
    "suspended author": False,
    "banned author": False,
}


@pytest.mark.parametrize("state", sorted(REACHABLE))
@override_settings(CANONICAL_ORIGIN=CANON)
def test_only_a_public_live_post_by_a_live_author_is_reachable(db, states, state):
    """The gate, and the address that follows it.

    Both halves are asserted from the same row on purpose: the rule of this
    feature is that the page withholds exactly what the gate refuses, and an
    address that outlives the gate is the one way it could not.
    """
    post = states[state]
    expected = REACHABLE[state]
    assert post.is_reachable_offsite is expected
    if expected:
        assert post.share_url == f"{CANON}/status/{post.pk}/"
    else:
        assert post.share_url == ""


def test_the_gate_takes_no_viewer_so_the_anonymous_case_cannot_be_fixed_in(db):
    """The shape is the ruling (D4), so the shape gets pinned.

    ``may_replay(user, status)`` takes a viewer because a replay is an act by a
    member. This must not, because an anonymous reader on a public post is a
    first-class sharer — and a function that accepts a user is a function someone
    will eventually "correct" into requiring one. A property cannot be called
    with a viewer, so the mistake cannot be made quietly.
    """
    assert isinstance(Status.__dict__["is_reachable_offsite"], property)


def test_your_own_public_post_is_shareable_where_replay_withholds_it(db, author, films):
    """The one seam between this gate and ``may_replay``, pinned from both sides.

    Replay refuses your own post (D2b); share must allow it, because forwarding
    your own review is the most likely thing a member does with this. Asserting
    only the share half would leave the two rules free to drift back together.
    """
    own = _review(author, films[0], Visibility.PUBLIC)
    assert may_replay(author, own) is False
    assert own.is_reachable_offsite is True


def test_unsuspending_an_author_reopens_the_gate(db, states):
    """Suspension is reversible and the gate reads a column, so the answer has to
    come back exactly rather than stay closed."""
    post = states["suspended author"]
    assert post.is_reachable_offsite is False
    post.user.unsuspend()
    post.user.refresh_from_db()
    assert post.is_reachable_offsite is True


# --- the address: a local post -------------------------------------------------


@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_local_post_shares_the_canonical_origin_plus_its_status_path(db, states):
    """Minted off ``CANONICAL_ORIGIN``, never off the request (R108)."""
    post = states["public"]
    assert post.share_url == f"{CANON}/status/{post.pk}/"


@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_address_does_not_move_when_the_request_arrives_on_another_host(db, states):
    """A moderator on the LAN address must not copy out a LAN link (R108).

    The request here arrives on a perfectly legitimate second host — the shape of
    ``http://192.168.1.138:3030``, which is how this instance is actually reached
    on a home network — and the minted address still names the canonical origin.
    The helper never sees a request, which is what makes that true, so this is a
    regression pin on the absence: put ``request.build_absolute_uri()`` back in
    and the second read names ``localhost``.
    """
    post = states["public"]
    before = post.share_url
    response = Client(SERVER_NAME="localhost").get(f"/status/{post.pk}/")
    assert response.status_code == 200
    assert post.share_url == before == f"{CANON}/status/{post.pk}/"


@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_shared_address_resolves_to_the_post_page(db, states):
    """The string is an address on this site, not a shape that looks like one.

    The equality tests above compare against a hand-written path, so a typo in
    the helper would agree with a typo in the test. Resolving the path cannot.
    """
    path = urlparse(states["public"].share_url).path
    assert resolve(path).url_name == "status"


@override_settings(CANONICAL_ORIGIN=CANON)
def test_the_shared_address_carries_no_query_parameter(db, states):
    """It stays byte-identical to the post's identity — no ``?ref=`` (D1)."""
    assert parse_qs(urlparse(states["public"].share_url).query) == {}


# --- the address: a mirror -----------------------------------------------------


def test_a_mirror_shares_its_home_instance_url_not_our_copy(db):
    """D3: the post's identity lives on its home instance.

    Our copy of it goes dead the day the mirror is dropped, and the home URL is
    what Mastodon's own copy-link hands out.
    """
    home = "https://upallnight.minnix.dev/users/minnix/statuses/117663708140180311"
    mirror = _mirror(remote_url=home)
    assert mirror.share_url == home


def test_a_mirror_shares_the_stored_string_unchanged(db):
    """Nothing is appended, normalised or re-encoded on the way out."""
    home = "https://peer.example/users/carol/statuses/9001?a=1&b=2#x"
    assert _mirror(remote_url=home).share_url == home


NOT_AN_HTTP_ADDRESS = [
    pytest.param("javascript:alert(1)", id="javascript"),
    pytest.param("JavaScript:alert(1)", id="uppercase-javascript"),
    pytest.param("data:text/html,<script>alert(1)</script>", id="data"),
    pytest.param("ftp://peer.example/x", id="ftp"),
    pytest.param("//peer.example/x", id="protocol-relative"),
    pytest.param("peer.example/x", id="bare-host"),
    pytest.param("not a url", id="prose"),
    pytest.param("", id="blank"),
    pytest.param("http:/no-host", id="scheme-without-a-host"),
    pytest.param("http:", id="scheme-only"),
]


@pytest.mark.parametrize("home_url", NOT_AN_HTTP_ADDRESS)
def test_a_mirror_whose_home_url_is_not_http_gets_no_address(db, home_url):
    """The scheme gate, and the reason it is here at all.

    ``Status.remote_url`` is the inbound Note's ``id``, stored after a check that
    it is a non-empty string: no scheme allowlist, no host check, and no template
    has ever printed it. This helper is the first place that string could reach a
    browser, so a peer that sent one of these gets no control rather than a
    suspect one. Validating the note id at ingest is the deeper fix and is
    deliberately out of this arc (``SHARE_PLAN.md``, Deferred).

    ``http:/no-host`` is the odd one out: a fine scheme on a string with no host,
    which is not an address either.
    """
    mirror = _mirror(remote_url=home_url)
    assert mirror.is_reachable_offsite is True
    assert mirror.share_url == ""


@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_mirror_of_a_post_the_world_cannot_read_gets_no_address(db):
    """A mirror carries its author's audience, and the gate is the same one."""
    mirror = _mirror(visibility=Visibility.FOLLOWERS)
    assert mirror.is_reachable_offsite is False
    assert mirror.share_url == ""


@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_mirror_that_is_a_list_face_shares_its_home_url(db):
    """The mirror rule wins over the ``/list/`` shape, and that is the ruling.

    A list's home address is whatever its own instance calls it; rewriting it into
    our ``/list/`` shape here would be minting identity for another instance's
    object, which R41/R42 refuse everywhere else.
    """
    home = "https://peer.example/status/42/"
    face = _mirror(status_type=Status.Type.LIST, remote_url=home)
    assert face.share_url == home


# --- the address: a list face --------------------------------------------------


@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_list_face_shares_the_list_page_not_the_status_path(db, author, films):
    """L10: a list's canonical human page is ``/list/<id>/``.

    ``/status/<id>/`` works too — it redirects — but the address a member hands
    out should be the one the site itself calls canonical, and a redirect is a
    promise the receiving app's bot may not follow.
    """
    film_list = create_list(
        author, title="Noir You Must See", films=films[:2], visibility=Visibility.PUBLIC
    )
    face = film_list.status
    assert face.share_url == f"{CANON}/list/{film_list.pk}/"
    assert resolve(urlparse(face.share_url).path).url_name == "list-detail"


@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_private_list_face_shares_nothing(db, author, films):
    """The list page 404s for a stranger, so there is nothing to hand out.

    This is the case a template guess gets wrong: the author's own page renders
    fine, so "does the page render" is not the question.
    """
    film_list = create_list(
        author, title="Private Shelf", films=films[:1], visibility=Visibility.PRIVATE
    )
    face = film_list.status
    assert face.is_reachable_offsite is False
    assert face.share_url == ""


@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_deleted_list_face_shares_nothing(db, author, films):
    """``soft_delete_list`` takes both halves down, so the face is gone too."""
    film_list = create_list(author, title="Doomed Shelf", films=films[:1])
    face = film_list.status
    soft_delete_list(film_list)
    face.refresh_from_db()
    assert face.share_url == ""


@override_settings(CANONICAL_ORIGIN=CANON)
def test_a_list_face_with_no_list_behind_it_shares_nothing(db, author):
    """The same inconsistency ``status_detail`` defends against.

    ``create_list`` always writes the pair and ``soft_delete_list`` always takes
    both down, so a live face with no live ``FilmList`` is not a state the normal
    path leaves behind — which is exactly why it gets a pin rather than an
    assumption. The alternative reading of that state is a link to a page that is
    not there, or an exception out of a template.
    """
    face = Status.objects.create(
        user=author, status_type=Status.Type.LIST, visibility=Visibility.PUBLIC
    )
    assert face.is_reachable_offsite is True
    assert face.share_url == ""
