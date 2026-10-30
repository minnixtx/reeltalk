"""V1: the audience filter, walked surface by surface (``VISIBILITY_PLAN.md`` §4).

**The shape of this file is the point.** The risk of the feature is not the
column, it is one read path out of nineteen forgetting the filter and a member's
private review appearing on a stranger's screen. So the central test does not
check one surface well, it checks every surface that can render a post for every
class of viewer. A surface added later that skips ``audience_q`` fails here
rather than shipping a leak.

The viewer classes, and why each is its own case:

* **anonymous** - the floor: public only, and the class a surface most easily
  gets wrong by forgetting a ``request.user`` check;
* **stranger** - a signed-in member who follows the author nowhere. Proves a
  login is not itself an audience;
* **follower** - entitled to the followers rows and to nothing else;
* **blocked by the author** - follows the author, but the author blocked them,
  so ``feed_member_ids`` drops the author from their feed entirely (R54).

Plus the author, who must see their own rows everywhere: a filter that hid a
member's private review from *them* would pass every other test in this file.

The second group covers what the plan's answered decisions actually promise: the
tallies (D2), the review slot and the audience fixed at writing (D3, D6), the
whole act being private including its shelf row (D5), mentions that notify
nobody (D7), the 404 rather than 403 on both machine-facing doors (D9), and an
inbound mirror marked with what the peer's own ``to``/``cc`` said.
"""

import pytest
from django.test import Client
from django.urls import reverse

from reeltalk.activitypub.statuses import visibility_from_audience
from reeltalk.core.import_export import export_film_csv
from reeltalk.core.models import (
    Film,
    Like,
    ShelfFilm,
    Status,
    Visibility,
    add_reply,
    live_reviews,
    mark_watched,
    may_read,
    readable_statuses,
    trending_films,
)
from reeltalk.mentions.notify import record_mentions
from reeltalk.notifications.models import Notification
from reeltalk.tests.members import member, site_admin

PASSWORD = "s3cretpass"

PUBLIC_MARK = "PUBLIC-REVIEW-MARKER"
FOLLOWERS_MARK = "FOLLOWERS-REVIEW-MARKER"
PRIVATE_MARK = "PRIVATE-REVIEW-MARKER"


@pytest.fixture(autouse=True)
def admin(db):
    """``/`` redirects to ``/setup/`` until a superuser exists, after which every
    render assertion here would pass vacuously (the trap N6 documented)."""
    return site_admin(localname="admin", password=PASSWORD)


@pytest.fixture
def author(db):
    """The member whose posts are under test."""
    return member(localname="author", password=PASSWORD)


@pytest.fixture
def follower(db):
    return member(localname="follower", password=PASSWORD)


@pytest.fixture
def stranger(db):
    """A signed-in member who follows nobody."""
    return member(localname="stranger", password=PASSWORD)


@pytest.fixture
def films(db):
    """One film per audience under test. D5's one-review-per-user-per-film is a
    database constraint, so a fixture that wrote three reviews on one film would
    fail on the second row for a reason unrelated to the rule under test."""
    return [
        Film.objects.create(title="Dune", year=2021, genres=["Sci-Fi"]),
        Film.objects.create(title="Arrival", year=2016, genres=["Sci-Fi"]),
        Film.objects.create(title="Annihilation", year=2018, genres=["Sci-Fi"]),
    ]


@pytest.fixture
def posts(db, author, films):
    """The three audiences, one per film, all by the same author."""
    return {
        "public": _review(author, films[0], PUBLIC_MARK, Visibility.PUBLIC),
        "followers": _review(author, films[1], FOLLOWERS_MARK, Visibility.FOLLOWERS),
        "private": _review(author, films[2], PRIVATE_MARK, Visibility.PRIVATE),
    }


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password=PASSWORD), (
        f"{localname} did not sign in - the page below would render anonymously "
        "and every assertion on it would pass vacuously"
    )
    return client


def _review(user, film, marker, visibility) -> Status:
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        content=f"<p>{marker}</p>",
        rating=4,
        visibility=visibility,
    )


def _surfaces(posts) -> list[tuple[str, str]]:
    """Every URL that can render one of the author's posts.

    The inventory is the test: a new surface belongs in this list, and adding it
    here is what makes the walk cover it. Pairs of (label, path) so a failure
    names the surface instead of an index. The film and status paths point at the
    **private** post's own, which is the one every viewer must not see.
    """
    private = posts["private"]
    return [
        ("home following", "/"),
        ("home local", "/?scope=local"),
        ("home all", "/?scope=all"),
        ("feed page following", "/feed/page/"),
        ("feed page local", "/feed/page/?scope=local"),
        ("film page", f"/film/{private.film_id}/"),
        ("status page", f"/status/{private.id}/"),
        ("profile", f"/user/{private.user.localname}/"),
        ("films all", f"/user/{private.user.localname}/films/"),
        ("genre", "/genre/sci-fi/"),
        ("notifications", "/notifications/"),
        ("outbox", f"/user/{private.user.localname}/outbox/"),
    ]


# The subset of the inventory that draws a post's body. The others are in the
# walk for the leak direction (a private body must never reach them) but cannot
# carry one in the positive direction: the profile draws shelves and counts, the
# notifications page draws events, the outbox draws wire documents, and a public
# scope is public by definition - the author's own private post is not supposed to
# appear there, which is its own test below.
POST_BODY_SURFACES = {
    "home following",
    "feed page following",
    "film page",
    "status page",
    "genre",
}


# --- The walk ----------------------------------------------------------------


@pytest.mark.parametrize(
    "who", ["anonymous", "stranger", "follower", "blocked_by_author"]
)
@pytest.mark.django_db
def test_a_private_post_is_absent_from_every_surface_for_every_other_viewer(
    db, author, follower, stranger, posts, who
):
    """The one test the whole increment is built on (plan §4)."""
    follower.follows.add(author)
    if who == "anonymous":
        client = Client()
    elif who == "stranger":
        client = _login("stranger")
    elif who == "follower":
        client = _login("follower")
    else:
        # A member the author blocked. They follow nobody, so they read as a
        # stranger here - which is the point: a block hides the blocked user's
        # content from the blocker, it does not wall the blocker's posts off.
        blocked = member(localname="blocked", password=PASSWORD)
        author.blocks.add(blocked)
        client = _login("blocked")

    for label, path in _surfaces(posts):
        response = client.get(path)
        assert response.status_code in (200, 302, 404), (
            f"{label}: {response.status_code}"
        )
        assert PRIVATE_MARK not in response.content.decode(), (
            f"private leaked onto {label}"
        )
        if who != "follower":
            assert FOLLOWERS_MARK not in response.content.decode(), (
                f"followers-only review reached {label} as a {who}"
            )
    # The public post is the control on all of that: if it never rendered
    # either, the walk would be asserting nothing. It is checked on the **Local**
    # scope rather than the default feed, because a stranger's Following feed
    # correctly holds nobody's posts - they follow nobody - and a control that
    # cannot appear is not a control.
    assert PUBLIC_MARK in client.get("/?scope=local").content.decode()


@pytest.mark.django_db
def test_the_author_still_sees_their_own_private_post_on_every_post_surface(
    db, author, posts
):
    client = _login("author")
    for label, path in _surfaces(posts):
        if label not in POST_BODY_SURFACES:
            continue
        response = client.get(path)
        assert PRIVATE_MARK in response.content.decode(), (
            f"author's own private review missing from {label}"
        )


@pytest.mark.django_db
def test_a_public_scope_stays_public_even_in_its_authors_view(db, author, posts):
    """Local and All are public scopes by definition (N6's contract, plan §3's
    surface table): the author's own private review belongs to Following, not to
    the instance's public feed - and their followers rows are not public either.
    """
    for path in ("/?scope=local", "/?scope=all", "/feed/page/?scope=local"):
        body = _login("author").get(path).content.decode()
        assert PUBLIC_MARK in body, path
        assert PRIVATE_MARK not in body, path
        assert FOLLOWERS_MARK not in body, path


@pytest.mark.django_db
def test_a_followers_post_reaches_a_follower_and_nobody_else(
    db, author, follower, stranger, posts
):
    follower.follows.add(author)
    path = f"/status/{posts['followers'].id}/"
    assert _login("follower").get(path).status_code == 200
    assert _login("stranger").get(path).status_code == 404
    assert Client().get(path).status_code == 404
    # And on the feed, where the audience rule is the follow set itself.
    assert FOLLOWERS_MARK in _login("follower").get("/").content.decode()
    assert FOLLOWERS_MARK not in _login("stranger").get("/").content.decode()


@pytest.mark.django_db
def test_a_viewer_who_blocked_the_author_loses_the_followers_post(
    db, author, follower, posts
):
    """Audience follows the same one-directional block rule as the feed does.

    ``feed_member_ids`` subtracts the *viewer's* blocks (R54), so a member who
    blocks an author stops being entitled to that author's followers rows - and
    the audience predicate reads membership, so it moves with it. The reverse is
    deliberately not asserted: a block in this product hides the blocked
    user's content from the blocker (R56), it is not a wall around the blocker's
    own posts, and an audience rule that quietly became one would be a second,
    undocumented block semantic.
    """
    follower.follows.add(author)
    assert (
        _login("follower").get(f"/status/{posts['followers'].id}/").status_code == 200
    )
    follower.blocks.add(author)
    client = _login("follower")
    assert client.get(f"/status/{posts['followers'].id}/").status_code == 404
    assert FOLLOWERS_MARK not in client.get("/").content.decode()


# --- The two machine-facing doors (D9) ---------------------------------------


@pytest.mark.django_db
def test_the_post_page_404s_on_both_arms_for_a_post_you_may_not_read(
    db, stranger, posts
):
    """404, not 403: a 403 confirms the post exists. The AP arm matters as much
    as the human one - it is how a peer fetches the Note by its id."""
    for client in (Client(), _login("stranger")):
        for accept in (None, "application/activity+json"):
            headers = {"HTTP_ACCEPT": accept} if accept else {}
            assert client.get(
                f"/status/{posts['private'].id}/", **headers
            ).status_code == (404), f"leaked on the arm with accept={accept}"


@pytest.mark.django_db
def test_the_author_still_opens_their_own_private_post_on_both_arms(db, author, posts):
    owner = _login("author")
    assert owner.get(f"/status/{posts['private'].id}/").status_code == 200
    assert (
        owner.get(
            f"/status/{posts['private'].id}/", HTTP_ACCEPT="application/activity+json"
        ).status_code
        == 200
    )


@pytest.mark.django_db
def test_the_outbox_lists_only_public_posts(db, author, posts):
    """The outbox is an unauthenticated endpoint, so it is a read door like any
    other, and its count has to count what it lists. It is an OrderedCollection,
    so the assertion walks to the first page: the collection alone would pass on
    a count while still naming rows it should not."""
    client = Client()
    collection = client.get(f"/user/{author.localname}/outbox/").json()
    assert collection["totalItems"] == 1
    page = str(client.get(collection["first"]).json())
    assert PUBLIC_MARK in page
    assert PRIVATE_MARK not in page
    assert FOLLOWERS_MARK not in page


# --- The tallies (D2) --------------------------------------------------------


@pytest.mark.django_db
def test_a_private_review_moves_no_public_number(db, author, stranger, posts):
    """A count and the list under it must agree for the reader in front of them."""
    assert live_reviews(None).count() == 1
    assert live_reviews(stranger).count() == 1
    assert live_reviews(follower_of_author(author, stranger)).count() == 2
    assert live_reviews(author).count() == 3
    # Trending: a stranger sees one reviewed film, the author sees all three.
    assert {row.film.title for row in trending_films(viewer=None)} == {"Dune"}
    assert len(trending_films(viewer=author)) == 3


def follower_of_author(author, user):
    user.follows.add(author)
    return user


@pytest.mark.django_db
def test_a_genre_page_shows_only_what_its_viewer_may_read(db, author, posts):
    anonymous = Client().get("/genre/sci-fi/").content.decode()
    assert PUBLIC_MARK in anonymous
    assert PRIVATE_MARK not in anonymous
    assert FOLLOWERS_MARK not in anonymous
    assert PRIVATE_MARK in _login("author").get("/genre/sci-fi/").content.decode()


@pytest.mark.django_db
def test_a_genre_with_only_private_reviews_404s_for_a_stranger(db, author, films):
    """Otherwise a pill would lead to a page that answers 200 and shows nothing."""
    _review(author, films[0], PRIVATE_MARK, Visibility.PRIVATE)
    assert Client().get("/genre/sci-fi/").status_code == 404
    assert _login("author").get("/genre/sci-fi/").status_code == 200


# --- The write paths ---------------------------------------------------------


@pytest.mark.django_db
def test_a_private_finish_writes_a_private_review_and_a_private_shelf_row(
    db, author, films
):
    """D5: finishing a film privately is one act with two rows."""
    status = mark_watched(
        author, films[0], rating=4, content="Mine", visibility="private"
    )
    assert status.visibility == Visibility.PRIVATE
    row = ShelfFilm.objects.get(
        shelf__user=author, shelf__identifier="read", film=films[0]
    )
    assert row.visibility == Visibility.PRIVATE


@pytest.mark.django_db
def test_a_private_finish_leaves_no_feed_row_for_anybody_else(
    db, author, follower, films
):
    follower.follows.add(author)
    mark_watched(author, films[0], rating=4, content="Mine", visibility="private")
    assert "Dune" not in _login("follower").get("/").content.decode()
    # The author still sees their own watched row.
    assert "Dune" in _login("author").get("/").content.decode()


@pytest.mark.django_db
def test_a_private_finish_produces_no_delivery_of_any_kind(
    db, author, films, deliveries
):
    """No ``Create``, and no watchlist-removal ``ShelfEvent`` either (D5).

    Re-pinned in V2, deliberately. This test used to spy on the view's
    ``broadcast_status_*`` calls, which was the right seam when the audience
    check lived in the view. The gate moved into ``broadcast._announce`` so the
    reply and delete routes are covered by it too, and a view-level spy now sees
    the same call for all three audiences - it would pass with the gate deleted.
    The spy sits on the delivery seam instead, which is what the promise is about.
    """
    client = _login("author")
    # Public finish first, so the film is on Watched and the next finish has a
    # Watchlist removal it would otherwise announce.
    client.post(reverse("film-shelve", args=[films[0].id]))
    client.post(reverse("film-mark-watched", args=[films[0].id]), {"rating": "4"})
    assert deliveries  # the public finish announced something
    deliveries.clear()
    client.post(
        reverse("film-mark-watched", args=[films[1].id]),
        {"rating": "4", "content": "Private", "visibility": "private"},
    )
    assert deliveries == []
    # A public finish still broadcasts, so the empty list above is the audience
    # and not a spy that never fires.
    client.post(reverse("film-shelve", args=[films[2].id]))
    deliveries.clear()
    client.post(reverse("film-mark-watched", args=[films[2].id]), {"rating": "4"})
    assert deliveries


@pytest.mark.django_db
def test_the_audience_is_fixed_at_writing(db, author, films):
    """D6: an edit changes text and rating, never the audience."""
    first = mark_watched(
        author, films[0], rating=4, content="Private", visibility="private"
    )
    again = mark_watched(author, films[0], rating=5, content="Still private")
    assert again.pk == first.pk
    assert again.visibility == Visibility.PRIVATE


@pytest.mark.django_db
def test_an_unknown_posted_audience_falls_back_to_public(db, author, films):
    response = _login("author").post(
        reverse("film-mark-watched", args=[films[0].id]),
        {"rating": "4", "content": "Typo", "visibility": "followres"},
    )
    assert response.status_code == 302
    assert (
        Status.objects.get(user=author, film=films[0]).visibility == Visibility.PUBLIC
    )


@pytest.mark.django_db
def test_a_reply_inherits_its_parent_audience(db, author, films):
    parent = _review(author, films[0], PRIVATE_MARK, Visibility.PRIVATE)
    reply = add_reply(author, parent, content="To myself", raw_content="To myself")
    assert reply.visibility == Visibility.PRIVATE


@pytest.mark.django_db
def test_the_like_and_reply_routes_refuse_a_post_the_viewer_cannot_read(
    db, stranger, posts
):
    """The route refuses exactly what the page withholds (R85's rule)."""
    client = _login("stranger")
    for url in (
        reverse("status-like", args=[posts["private"].id]),
        reverse("status-reply", args=[posts["private"].id]),
    ):
        assert client.post(url, {"content": "hello"}).status_code == 404


@pytest.mark.django_db
def test_a_follower_can_like_and_reply_to_a_followers_only_post(
    db, author, follower, posts
):
    """The acceptance half of the refusal above (visibility V4).

    A follower may read a followers-only post, so both write doors must stand
    open to them - the like lands and the reply inherits the parent's audience
    (the thread can never widen past its root). Before V4 this direction was
    only implied by ``may_read`` and never actually clicked.
    """
    follower.follows.add(author)
    client = _login("follower")
    target = posts["followers"]
    assert client.post(reverse("status-like", args=[target.id])).status_code == 200
    assert Like.objects.filter(user=follower, status=target).exists()
    assert (
        client.post(
            reverse("status-reply", args=[target.id]), {"content": "Agreed"}
        ).status_code
        == 200
    )
    reply = Status.objects.get(reply_parent=target, user=follower)
    assert reply.visibility == Visibility.FOLLOWERS


@pytest.mark.django_db
def test_the_report_route_refuses_a_post_the_reporter_cannot_read(db, stranger, posts):
    response = _login("stranger").post(
        reverse("report-status", args=[posts["private"].id]),
        {"category": "spam", "comment": "cannot see it"},
    )
    assert response.status_code == 404


# --- Mentions (D7) -----------------------------------------------------------


@pytest.mark.django_db
def test_a_mention_inside_a_private_review_notifies_nobody(db, author, follower, films):
    status = _review(
        author, films[0], f"note @{follower.localname}", Visibility.PRIVATE
    )
    assert record_mentions(status, [follower]) == []
    assert not Notification.objects.filter(status=status).exists()


@pytest.mark.django_db
def test_a_mention_in_a_public_review_still_notifies(db, author, follower, films):
    status = _review(author, films[0], f"note @{follower.localname}", Visibility.PUBLIC)
    assert len(record_mentions(status, [follower])) == 1


# --- Inbound mirrors ---------------------------------------------------------


@pytest.mark.parametrize(
    "note,expected",
    [
        ({"to": ["https://www.w3.org/ns/activitystreams#Public"]}, Visibility.PUBLIC),
        (
            {"to": [], "cc": ["https://www.w3.org/ns/activitystreams#Public"]},
            Visibility.PUBLIC,
        ),
        ({"to": ["https://remote.example/users/bob/followers"]}, Visibility.FOLLOWERS),
        ({"to": ["https://mastodon.example/visibility"]}, Visibility.FOLLOWERS),
        ({"to": ["https://remote.example/users/alice"]}, Visibility.PRIVATE),
        # An absent audience is silence, not the narrowest case: every mirror
        # stored before V1 was public by construction, so a sparse peer that sends
        # neither array must not have its posts hidden for a rule it never opted
        # into. An array present but empty is the opposite: addressed to nobody.
        ({}, Visibility.PUBLIC),
        ({"to": [], "cc": []}, Visibility.PRIVATE),
        (
            {"to": [{"id": "https://www.w3.org/ns/activitystreams#Public"}]},
            Visibility.PUBLIC,
        ),
    ],
)
def test_a_mirrors_audience_comes_from_what_the_peer_addressed_it_to(note, expected):
    """Stored and marked, never dropped: a thread one of our members can read
    needs its parts, so the answer is a mark, not a deletion."""
    assert visibility_from_audience(note) == expected


# --- Export ------------------------------------------------------------------


@pytest.mark.django_db
def test_export_keeps_private_rows(db, author, films):
    """It is the member's own data (plan §3's agreed contract)."""
    mark_watched(author, films[0], rating=4.5, content="Mine", visibility="private")
    csv_text = export_film_csv(author)
    assert "Dune" in csv_text
    assert "9" in csv_text  # 4.5 stars on the Letterboxd 0-10 scale


# --- The predicate -----------------------------------------------------------


@pytest.mark.django_db
def test_may_read_and_the_queryset_never_disagree(
    db, author, follower, stranger, posts
):
    """Two answers to one question is how a leak comes back after the sweep that
    closed it, so they are asserted against each other rather than separately."""
    follower.follows.add(author)
    for status in posts.values():
        for viewer in (None, stranger, follower, author):
            in_queryset = readable_statuses(viewer).filter(pk=status.pk).exists()
            assert may_read(status, viewer) == in_queryset, (
                f"{status.visibility} for {viewer}"
            )


@pytest.mark.django_db
def test_the_marker_names_the_audience_only_to_those_entitled_to_see_it(
    db, author, follower, stranger, posts
):
    """D10's marker: the author sees which audience a post went to, and nobody
    else sees a marker for a post they cannot read at all.

    Asserted off the rendered tag, not a bare word: "Private" is also the
    composer's radio label, so a substring test would pass on a page that drew
    no marker at all.
    """
    private_marker = '<span class="audience">Private</span>'
    assert private_marker in _login("author").get("/").content.decode()
    assert private_marker not in _login("stranger").get("/").content.decode()
    follower.follows.add(author)
    assert '<span class="audience">Followers</span>' in (
        _login("follower").get("/").content.decode()
    )


# --- V2: the composer, and what actually leaves the instance -----------------


@pytest.fixture
def deliveries(monkeypatch):
    """Every delivery the broadcast layer actually attempts, as (type, targets).

    Patched a level below the views on purpose. The audience gate lives in
    ``broadcast._announce``, so a spy on the view's ``broadcast_status_*`` calls
    would see the same call for all three audiences and prove nothing at all.
    """
    from reeltalk.activitypub import broadcast

    calls: list[tuple[str, list[str]]] = []

    def fake(request, actor, activity, recipients):
        calls.append((activity["type"], [user.localname for user in recipients]))
        return []

    monkeypatch.setattr(broadcast, "_deliver_signed", fake)
    return calls


@pytest.mark.django_db
def test_the_composer_offers_all_three_audiences(db, author, films):
    """V2 flips the third one on: the control and the stored value agree."""
    page = _login("author").get(f"/film/{films[0].id}/").content.decode()
    for value in ("public", "followers", "private"):
        assert f'name="visibility" value="{value}"' in page, value


@pytest.mark.django_db
def test_a_followers_finish_is_delivered_and_a_private_one_is_not(
    db, author, films, deliveries
):
    """The difference between the two narrow audiences is on the wire, not in a flag."""
    client = _login("author")
    client.post(
        reverse("film-mark-watched", args=[films[0].id]),
        {"rating": "4", "content": "Open"},
    )
    client.post(
        reverse("film-mark-watched", args=[films[1].id]),
        {"rating": "4", "content": "Followers", "visibility": "followers"},
    )
    client.post(
        reverse("film-mark-watched", args=[films[2].id]),
        {"rating": "4", "content": "Private", "visibility": "private"},
    )
    assert [kind for kind, _ in deliveries] == ["Create", "Create"]


@pytest.mark.django_db
def test_a_followers_finish_writes_a_followers_shelf_row_too(db, author, films):
    """D5's "the whole act" rule is audience-blind: it applies to the new value."""
    mark_watched(author, films[0], rating=4, content="Ours", visibility="followers")
    row = ShelfFilm.objects.get(
        shelf__user=author, shelf__identifier="read", film=films[0]
    )
    assert row.visibility == Visibility.FOLLOWERS


@pytest.mark.django_db
def test_deleting_a_private_review_delivers_nothing(db, author, films, deliveries):
    """The gap V1's finish-flow gate left behind, closed at the broadcast layer.

    ``delete_activity`` embeds the whole Note, so a private review deleted behind
    the old unconditional broadcast reached every remote follower carrying its
    full text - the one row of the three that had never been sent at all.
    """
    private = mark_watched(
        author, films[0], rating=4, content="Mine", visibility="private"
    )
    assert deliveries == []
    _login("author").post(reverse("status-delete", args=[private.id]))
    assert deliveries == []
    # A public review's delete still goes out, so the empty list above is the
    # audience and not a spy that never fires.
    public = mark_watched(author, films[1], rating=4, content="Ours")
    deliveries.clear()
    _login("author").post(reverse("status-delete", args=[public.id]))
    assert [kind for kind, _ in deliveries] == ["Delete"]


@pytest.mark.django_db
def test_a_reply_under_a_private_review_is_not_delivered(db, author, films, deliveries):
    """The other half of the same gap: a reply inherits, and so must the gate.

    Only the author can answer a private review, and they are the one reader who
    already has it, so there is nobody to send it to.
    """
    parent = mark_watched(
        author, films[0], rating=4, content="Mine", visibility="private"
    )
    _login("author").post(reverse("status-reply", args=[parent.id]), {"content": "hi"})
    assert deliveries == []


@pytest.mark.django_db
def test_a_non_public_finish_announces_no_watchlist_removal(
    db, author, films, monkeypatch
):
    """D5 stays true for the new value: the shelf has no audience shape yet.

    Both halves are asserted, because an empty list on its own is also what a
    finish that never touched a Watchlist produces - the same trap the delivery
    spy above had to step around.
    """
    from reeltalk.core import views as views_module

    shelf_events: list[bool] = []
    monkeypatch.setattr(
        views_module,
        "broadcast_shelf_event",
        lambda *a, added, **k: shelf_events.append(added),
    )
    client = _login("author")
    client.post(reverse("film-shelve", args=[films[0].id]))
    client.post(reverse("film-mark-watched", args=[films[0].id]), {"rating": "4"})
    # The add announced itself and so did the removal - the pair is what proves
    # the second half below is the audience and not a route that never fires.
    assert shelf_events == [True, False]
    shelf_events.clear()
    client.post(reverse("film-shelve", args=[films[1].id]))
    client.post(
        reverse("film-mark-watched", args=[films[1].id]),
        {"rating": "4", "content": "Followers", "visibility": "followers"},
    )
    # The Watchlist add still announced itself; the removal that the finish caused
    # did not. That is D5 for the new audience: the shelf has no shape of its own
    # on the wire, so a non-public finish leaves peers holding a stale row.
    assert shelf_events == [True]
