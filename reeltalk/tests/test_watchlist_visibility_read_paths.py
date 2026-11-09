"""W2: the Watchlist audience, walked surface by surface (the W-plan's §5, W2).

The shape is W1's, borrowed whole, and for the same reason: the risk of this
feature is not the value on the row - it is one read path out of several
forgetting the filter, and a member's private Watchlist add reaching a stranger.
So the central test does not check one surface well, it walks every surface that
can show a Watchlist add for every class of viewer.

What is new here compared to W1 is **where the audience comes from**. A list
carries its audience on its own post face and nothing writes it afterwards. A
Watchlist add carries the member's setting at the moment of the add, on the
shelf row, and saving the setting re-files the adds that already exist - both
directions (D7). So this file has three groups W1 never needed: the setting
itself, the stamp at each of the doors that writes a row (the film page, the
search one-click, the file import), and the restamp on save, including the half
that says a **Watched** row is never touched (D6: a finished film keeps the
audience chosen at the finish).

The viewer classes are the four the V-arc settled and the author:

* **anonymous** - the floor, and the surface a page most easily gets wrong by
  forgetting a ``request.user`` check;
* **stranger** - a signed-in member who follows the author nowhere, proving a
  login is not itself an audience;
* **follower** - entitled to the followers rows and to nothing else;
* **moderator** - the queue is the only place a narrow audience is widened, and
  a feed is not the queue.

Plus the author, who must see their own rows everywhere: a filter that hid a
member's private add from *them* would pass every other test in this file.

The read surfaces were already filtered by the V-arc - the feed's shelf half and
the Films tabs both run ``audience_q`` - which is exactly why they are walked
here rather than assumed: nothing before this increment could *put* a non-public
value on a Watchlist row, so the filter had never been exercised on one. The
outbound rows are pinned at the delivery seam (``broadcast._deliver_signed``),
because the gate lives in ``broadcast.broadcast_shelf_event``, not in whichever
view called it. The inbound mirror row is pinned at the inbox handler.
"""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from reeltalk.activitypub import broadcast as broadcast_module
from reeltalk.activitypub.identity import PUBLIC_COLLECTION
from reeltalk.activitypub.inbox import process_inbound_activity
from reeltalk.core.import_export import import_row
from reeltalk.core.models import (
    Film,
    Shelf,
    ShelfFilm,
    Status,
    Visibility,
    mark_watched,
)
from reeltalk.social.models import User
from reeltalk.tests.members import member, moderator, site_admin

PASSWORD = "s3cretpass"

PUBLIC_MARK = "PUBLIC-ADD"
FOLLOWERS_MARK = "FOLLOWERS-ADD"
PRIVATE_MARK = "PRIVATE-ADD"

SETTINGS_URL = "/preferences/profile/"


@pytest.fixture(autouse=True)
def admin(db):
    """``/`` redirects to ``/setup/`` until a superuser exists, after which every
    render assertion here would pass vacuously (the trap N6 documented)."""
    return site_admin(localname="admin", password=PASSWORD)


@pytest.fixture
def author(db):
    """The member whose Watchlist is under test."""
    return member(localname="author", password=PASSWORD)


@pytest.fixture
def follower(db):
    return member(localname="follower", password=PASSWORD)


@pytest.fixture
def stranger(db):
    """A signed-in member who follows nobody."""
    return member(localname="stranger", password=PASSWORD)


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password=PASSWORD), (
        f"{localname} did not sign in - the page below would render anonymously "
        "and every assertion on it would pass vacuously"
    )
    return client


def save_settings(client, *, watchlist: str, reviews: str = "public"):
    """Post the settings form the way the browser posts it.

    Both selects ride every POST because both are required fields - a form that
    is refused for a missing field returns 200 with a field error, and a helper
    that ignored the status would then stamp every row below with the default
    and assert on a value nothing wrote.
    """
    response = client.post(
        SETTINGS_URL,
        {
            "display_name": "",
            "summary": "",
            "default_visibility": reviews,
            "watchlist_visibility": watchlist,
        },
    )
    assert response.status_code == 302, "the settings form refused the POST"
    return response


def add_to_watchlist(client, title: str, *, audience: str) -> Film:
    """Set the account setting, then add a film through the film-page door.

    The two doors in one helper on purpose: the audience on the row is supposed
    to *be* the setting at the moment of the add, so a fixture that set the row
    by hand would test the read filter against a state the instance can never
    actually be in.
    """
    save_settings(client, watchlist=audience)
    film = Film.objects.create(title=title, year=2024)
    response = client.post(reverse("film-shelve", args=[film.id]))
    assert response.status_code == 302
    return film


@pytest.fixture
def adds(db, author):
    """Three Watchlist adds by one author, one per audience, through the doors.

    **This state is not reachable through the doors alone, and that is worth
    saying out loud.** D7 re-files every ``to-read`` row whenever the setting is
    saved, so a member's own Watchlist is uniformly whatever the setting says
    right now - three rows with three audiences only exists here because the
    rows are re-stamped by hand afterwards. The walk is kept anyway, and kept
    against the doors that made the rows, for two reasons: the read filter must
    hold for every audience even though today's writes cannot mix them (the
    mirror path in this file's last section files a peer's row under the
    audience *the peer* chose, and that is a second writer this instance does
    not control), and a filter that has never been exercised on a value is a
    filter that can rot.
    """
    client = _login("author")
    made = {
        "public": add_to_watchlist(client, PUBLIC_MARK, audience="public"),
        "followers": add_to_watchlist(client, FOLLOWERS_MARK, audience="followers"),
        "private": add_to_watchlist(client, PRIVATE_MARK, audience="private"),
    }
    for audience, film in made.items():
        ShelfFilm.objects.filter(user=author, film=film).update(visibility=audience)
    # R37 folds consecutive same-shelf adds by one user inside a five-minute
    # window into a single "added X and N other films" row, and the fold keeps
    # only the newest film - so three adds made in one second would show one
    # title on the feed and the walk would be asserting on a row that never
    # rendered. Spacing them past the window gives each add its own entry.
    now = timezone.now()
    for offset, film in enumerate(made.values()):
        ShelfFilm.objects.filter(user=author, film=film).update(
            shelved_date=now - timedelta(minutes=20 * (offset + 1))
        )
    return made


def _surfaces(author, adds) -> list[tuple[str, str]]:
    """Every URL that can show one of the author's Watchlist adds.

    The inventory is the test: a new surface belongs in this list, and adding it
    here is what makes the walk cover it.
    """
    name = author.localname
    return [
        ("home feed", "/"),
        ("feed page", "/feed/page/"),
        ("films watchlist tab", f"/user/{name}/films/?tab=watchlist"),
        ("films all tab", f"/user/{name}/films/"),
    ]


# Who each audience reaches, as one table rather than three near-identical
# tests. The author is in every row: the audience decides who *else* sees an
# add, never whether the member who made it does.
ENTITLED = {
    "public": {"anonymous", "stranger", "follower", "moderator", "author"},
    "followers": {"follower", "author"},
    "private": {"author"},
}

MARKS = {"public": PUBLIC_MARK, "followers": FOLLOWERS_MARK, "private": PRIVATE_MARK}

# The two surfaces whose rows come from the follow graph rather than from a
# public page. The feed is where two rules meet and only one of them is the
# audience: the home feed's default scope is *following*, and the public scopes
# carry no shelf rows at all (the V-arc's rule, pinned in
# ``test_feed_scopes_public.py``), so a Watchlist add reaches a follower and the
# author there whatever its audience says. The Films tabs are where the audience
# alone decides, because they are pages anybody may open.
FOLLOW_GRAPH_SURFACES = {"home feed", "feed page"}


def _expects(label: str, who: str, audience: str) -> bool:
    """Whether ``who`` should find the add on ``label``."""
    if label in FOLLOW_GRAPH_SURFACES and who not in ("author", "follower"):
        return False
    return who in ENTITLED[audience]


# --- The walk ----------------------------------------------------------------


@pytest.mark.parametrize("audience", ["public", "followers", "private"])
@pytest.mark.parametrize("who", ["anonymous", "stranger", "follower", "moderator"])
@pytest.mark.django_db
def test_an_add_reaches_every_surface_for_exactly_the_viewers_its_audience_allows(
    db, author, follower, stranger, adds, who, audience
):
    """The one test the whole increment is built on (plan §5, W2 "Tests").

    Both directions are asserted on every surface: the entitled viewer sees the
    row, and everybody else does not. A test that only checked the absence would
    pass on a page that renders no adds at all, which is the way a read-path
    suite goes green while leaking.
    """
    follower.follows.add(author)
    if who == "anonymous":
        client = Client()
    elif who == "stranger":
        client = _login("stranger")
    elif who == "follower":
        client = _login("follower")
    else:
        moderator(localname="mod", password=PASSWORD)
        client = _login("mod")

    mark = MARKS[audience]
    for label, path in _surfaces(author, adds):
        expect = _expects(label, who, audience)
        response = client.get(path)
        assert response.status_code in (200, 302, 404), (
            f"{label}: {response.status_code}"
        )
        body = response.content.decode()
        assert (mark in body) == expect, (
            f"{mark} on {label} as {who}: expected {expect}"
        )


@pytest.mark.parametrize("audience", ["public", "followers", "private"])
@pytest.mark.django_db
def test_the_author_still_sees_their_own_add_whatever_its_audience(
    db, author, adds, audience
):
    """The author's half of the table, stated on its own because it is the half
    whose loss would otherwise hide inside the walk above."""
    client = _login("author")
    mark = MARKS[audience]
    for label, path in _surfaces(author, adds):
        body = client.get(path).content.decode()
        assert mark in body, f"author's own {mark} missing from {label}"


@pytest.mark.django_db
def test_a_followers_add_reaches_a_follower_and_nobody_else(
    db, author, follower, stranger, adds
):
    """The followers row in the table above, walked one more time through the
    unfollow door: the entitlement is the follow, so taking it back takes the
    row away again from a page that showed it a moment ago."""
    follower.follows.add(author)
    assert FOLLOWERS_MARK in _login("follower").get("/").content.decode()
    assert (
        FOLLOWERS_MARK
        in _login("follower")
        .get(f"/user/{author.localname}/films/?tab=watchlist")
        .content.decode()
    )
    follower.follows.remove(author)
    assert FOLLOWERS_MARK not in _login("follower").get("/").content.decode()
    assert (
        FOLLOWERS_MARK
        not in _login("follower")
        .get(f"/user/{author.localname}/films/?tab=watchlist")
        .content.decode()
    )
    # And a member who never followed at all never saw it in the first place.
    assert FOLLOWERS_MARK not in _login("stranger").get("/").content.decode()
    assert FOLLOWERS_MARK not in Client().get("/").content.decode()


# --- The setting --------------------------------------------------------------


@pytest.mark.django_db
def test_a_new_member_defaults_to_public():
    """Public by default and back-filled public on purpose - anything else would
    rewrite what every existing account's Watchlist already means."""
    user = member(localname="fresh", password=PASSWORD)
    assert user.watchlist_visibility == Visibility.PUBLIC


@pytest.mark.django_db
def test_the_settings_page_offers_the_watchlist_select_with_the_current_value(db):
    member(localname="quiet", password=PASSWORD, watchlist_visibility="private")
    body = _login("quiet").get(SETTINGS_URL).content.decode()
    assert "Watchlist visibility" in body
    # No help text under the select - owner ruling 2026-11-09; the promises the
    # setting makes are stated once on /help/.
    # The member's own value is the selected option, not the first one.
    assert '<option value="private" selected>' in body
    # And every control that was already on this page still renders. W1's
    # browser pass found a create page that drew only its new field, because a
    # template loop called something a BoundField does not have and Django
    # renders that as *no markup at all* rather than an error. A render test
    # that looks only for the new control is the test that misses it.
    assert 'name="display_name"' in body
    assert 'name="summary"' in body
    assert 'name="default_visibility"' in body
    assert 'name="watchlist_visibility"' in body


@pytest.mark.django_db
def test_saving_the_settings_changes_the_setting(db):
    member(localname="quiet", password=PASSWORD)
    save_settings(_login("quiet"), watchlist="private")
    assert User.objects.get(localname="quiet").watchlist_visibility == "private"


@pytest.mark.django_db
def test_the_settings_refuse_an_audience_the_select_does_not_offer(db):
    """The ChoiceField is the boundary: a hand-built POST cannot store a value no
    widget on this instance can produce."""
    member(localname="quiet", password=PASSWORD)
    response = _login("quiet").post(
        SETTINGS_URL,
        {
            "display_name": "",
            "summary": "",
            "default_visibility": "public",
            "watchlist_visibility": "direct",
        },
    )
    assert response.status_code == 200  # re-rendered with the field error
    assert User.objects.get(localname="quiet").watchlist_visibility == Visibility.PUBLIC


# --- The stamp at every door ---------------------------------------------------


@pytest.mark.parametrize("value", ["public", "followers", "private"])
@pytest.mark.django_db
def test_a_watchlist_add_stamps_the_setting_on_the_row(db, value):
    member(localname="stamper", password=PASSWORD)
    client = _login("stamper")
    film = add_to_watchlist(client, f"Stamped {value}", audience=value)
    row = ShelfFilm.objects.get(user__localname="stamper", film=film)
    assert row.visibility == value


@pytest.mark.django_db
def test_an_add_stays_public_when_the_setting_was_never_touched(db):
    """The default is the whole story for an account that never opened the
    settings page - no existing member's Watchlist moves by accident."""
    member(localname="untouched", password=PASSWORD)
    film = Film.objects.create(title="Untouched", year=2024)
    _login("untouched").post(reverse("film-shelve", args=[film.id]))
    row = ShelfFilm.objects.get(user__localname="untouched", film=film)
    assert row.visibility == Visibility.PUBLIC


@pytest.mark.django_db
def test_a_re_shelve_after_a_removal_is_a_new_act(db):
    """Unshelve and add the film again: the new row takes the setting of the
    moment it was made, not the audience of the row that was deleted.

    The removal comes first on purpose. If the setting were changed while the row
    still existed, D7's restamp would have re-filed it and the assertion would
    pass on the restamp rather than on the add.
    """
    user = member(localname="stamper", password=PASSWORD)
    client = _login("stamper")
    film = add_to_watchlist(client, "Twice", audience="public")
    client.post(reverse("film-unshelve", args=[film.id]))
    save_settings(client, watchlist="private")
    client.post(reverse("film-shelve", args=[film.id]))
    row = ShelfFilm.objects.get(user=user, film=film)
    assert row.visibility == Visibility.PRIVATE


@pytest.mark.django_db
def test_a_second_add_of_a_film_already_on_the_shelf_writes_no_audience(db):
    """``already`` is a no-op, audience included: a row's audience belongs to the
    act that created it. The row is set aside by hand to a value the member's
    setting never said, so a writer on the add path has something to get wrong
    and a test to catch it."""
    user = member(localname="stamper", password=PASSWORD)
    client = _login("stamper")
    film = add_to_watchlist(client, "Twice", audience="public")
    ShelfFilm.objects.filter(user=user, film=film).update(
        visibility=Visibility.FOLLOWERS
    )
    client.post(reverse("film-shelve", args=[film.id]))
    row = ShelfFilm.objects.get(user=user, film=film)
    assert row.visibility == Visibility.FOLLOWERS


@pytest.mark.django_db
def test_an_import_honours_the_setting(db):
    """D9: an unrated import row is a Watchlist add, so it takes the Watchlist
    setting - not the review default, which says nothing about this shelf."""
    user = member(
        localname="importer", password=PASSWORD, watchlist_visibility="private"
    )
    watchlist = Shelf.objects.get(user=user, identifier=Shelf.TO_READ)
    watched = Shelf.objects.get(user=user, identifier=Shelf.READ)
    row = {"TMDb ID": "42", "Name": "Imported Film", "Release Date": "1982-04-01"}
    import_row(user, row, watchlist, watched)
    shelf_row = ShelfFilm.objects.get(user=user, shelf=watchlist)
    assert shelf_row.visibility == Visibility.PRIVATE


@pytest.mark.django_db
def test_a_rated_import_uses_the_review_default_not_the_watchlist_setting(db):
    """D6's boundary in the other direction: a rated import is a finish, and a
    finish takes the review default like any other review."""
    user = member(
        localname="importer",
        password=PASSWORD,
        watchlist_visibility="public",
        default_visibility="private",
    )
    watchlist = Shelf.objects.get(user=user, identifier=Shelf.TO_READ)
    watched = Shelf.objects.get(user=user, identifier=Shelf.READ)
    row = {
        "TMDb ID": "43",
        "Name": "Rated Import",
        "Release Date": "1982-04-01",
        "Your Rating": "8",
    }
    import_row(user, row, watchlist, watched)
    assert ShelfFilm.objects.get(user=user, shelf=watched).visibility == "private"
    assert Status.objects.get(user=user).visibility == Visibility.PRIVATE


@pytest.mark.django_db
def test_a_finish_keeps_the_audience_chosen_at_the_finish(db, film):
    """D6: the setting governs the Watchlist shelf only. A private-Watchlist
    member who finishes a film publicly gets a public Watched row."""
    user = member(
        localname="finisher", password=PASSWORD, watchlist_visibility="private"
    )
    mark_watched(user, film, rating=4, content="<p>public</p>", raw_content="public")
    row = ShelfFilm.objects.get(user=user, shelf__identifier=Shelf.READ, film=film)
    assert row.visibility == Visibility.PUBLIC


@pytest.fixture
def film(db):
    return Film.objects.create(title="Dune", year=2021, genres=["Sci-Fi"])


# --- The restamp (D7) ---------------------------------------------------------


@pytest.mark.django_db
def test_flipping_to_private_hides_last_month_s_adds_from_a_follower(
    db, author, follower
):
    """D7, walked as the owner will walk it: adds made a month ago, the setting
    flipped to Private, and the follower's feed and Films tab lose them while
    the author's keep them."""
    follower.follows.add(author)
    client = _login("author")
    old = Film.objects.create(title="Last-Month Film", year=2024)
    client.post(reverse("film-shelve", args=[old.id]))
    ShelfFilm.objects.filter(user=author, film=old).update(
        shelved_date=timezone.now() - timedelta(days=30)
    )
    assert (
        "Last-Month Film"
        in _login("follower").get(f"/user/{author.localname}/films/").content.decode()
    )

    save_settings(client, watchlist="private")

    for path in (
        "/",
        f"/user/{author.localname}/films/",
        f"/user/{author.localname}/films/?tab=watchlist",
    ):
        assert "Last-Month Film" not in _login("follower").get(path).content.decode()
        assert "Last-Month Film" in client.get(path).content.decode()
    assert ShelfFilm.objects.get(user=author, film=old).visibility == "private"


@pytest.mark.django_db
def test_flipping_back_re_exposes_the_same_adds(db, author, follower):
    """Both directions, from the same rows - a restamp that only narrowed would
    be a one-way door the member cannot walk back through."""
    follower.follows.add(author)
    client = _login("author")
    old = Film.objects.create(title="Re-Exposed Film", year=2024)
    client.post(reverse("film-shelve", args=[old.id]))
    save_settings(client, watchlist="private")
    assert "Re-Exposed Film" not in _login("follower").get("/").content.decode()
    save_settings(client, watchlist="public")
    assert "Re-Exposed Film" in _login("follower").get("/").content.decode()
    assert ShelfFilm.objects.get(user=author, film=old).visibility == "public"


@pytest.mark.django_db
def test_the_restamp_leaves_finished_films_alone(db, author, follower):
    """D6 again, at the door that could break it: the settings save must restamp
    ``to-read`` rows and nothing else, or a member's Watchlist setting would
    quietly rewrite the audience of every film they ever reviewed."""
    client = _login("author")
    watched_film = Film.objects.create(title="Finished Film", year=2020)
    mark_watched(
        author,
        watched_film,
        rating=5,
        content="<p>seen</p>",
        raw_content="seen",
        visibility=Visibility.PUBLIC,
    )
    wanted = Film.objects.create(title="Wanted Film", year=2024)
    client.post(reverse("film-shelve", args=[wanted.id]))

    save_settings(client, watchlist="private")

    assert (
        ShelfFilm.objects.get(
            user=author, shelf__identifier=Shelf.READ, film=watched_film
        ).visibility
        == Visibility.PUBLIC
    )
    assert (
        ShelfFilm.objects.get(
            user=author, shelf__identifier=Shelf.TO_READ, film=wanted
        ).visibility
        == Visibility.PRIVATE
    )
    assert Status.objects.get(user=author, film=watched_film).visibility == "public"


@pytest.mark.django_db
def test_a_save_that_changes_nothing_restamps_nothing(db, author):
    """The restamp runs when the value changes. A profile save that leaves the
    audience alone must not spend the write, and must not move a row a member
    set some other way."""
    client = _login("author")
    wanted = Film.objects.create(title="Left Alone", year=2024)
    client.post(reverse("film-shelve", args=[wanted.id]))
    ShelfFilm.objects.filter(user=author, film=wanted).update(
        visibility=Visibility.FOLLOWERS
    )
    save_settings(client, watchlist="public")
    assert (
        ShelfFilm.objects.get(user=author, film=wanted).visibility
        == Visibility.FOLLOWERS
    )


@pytest.mark.django_db
def test_the_restamp_and_the_user_save_are_one_transaction(db, author, monkeypatch):
    """D7's "in the same transaction as the user save", proven by breaking the
    restamp: if the rows could not be re-filed, the setting did not change
    either. Half a save here would leave the member's page saying one thing and
    their rows another."""
    from reeltalk.social import views as social_views

    def explode(*args, **kwargs):
        raise ValidationError("the restamp failed")

    monkeypatch.setattr(social_views, "restamp_watchlist_audience", explode)
    client = _login("author")
    with pytest.raises(ValidationError):
        client.post(
            SETTINGS_URL,
            {
                "display_name": "",
                "summary": "",
                "default_visibility": "public",
                "watchlist_visibility": "private",
            },
        )
    assert (
        User.objects.get(localname="author").watchlist_visibility == Visibility.PUBLIC
    )


# --- The wire -----------------------------------------------------------------


@pytest.fixture
def deliveries(monkeypatch):
    """Every delivery the broadcast layer actually attempts.

    Patched a level below the views, exactly as W1's is: the gate lives in
    ``broadcast``, so a spy on the view's ``broadcast_shelf_event`` call would
    see the same call for all three audiences and prove nothing at all.
    """
    calls: list[dict] = []

    def fake(request, actor, activity, recipients):
        calls.append(activity)
        return []

    monkeypatch.setattr(broadcast_module, "_deliver_signed", fake)
    return calls


def _shelf_events(deliveries: list[dict]) -> list[dict]:
    """Only the ShelfEvent activities out of everything the spy recorded.

    A finish broadcasts its review and the Watchlist removal it caused as two
    separate activities, so a test about the removal has to name it rather than
    count the deliveries.
    """
    return [a for a in deliveries if a["object"].get("type") == "ShelfEvent"]


@pytest.mark.django_db
def test_a_private_add_sends_nothing_on_add_or_removal(db, author, deliveries):
    """A private add sends nothing, and neither does taking it back off again -
    the removal names the same film to the same people, so a gate on the add
    alone would leak the fact through the door on the way out."""
    client = _login("author")
    film = add_to_watchlist(client, "Secret Add", audience="private")
    assert deliveries == []
    client.post(reverse("film-unshelve", args=[film.id]))
    assert deliveries == []
    # The control: the same two doors on a public add both send.
    open_film = add_to_watchlist(client, "Open Add", audience="public")
    assert [a["type"] for a in deliveries] == ["Create"]
    client.post(reverse("film-unshelve", args=[open_film.id]))
    assert [a["type"] for a in deliveries] == ["Create", "Delete"]


@pytest.mark.django_db
def test_a_followers_add_is_addressed_to_the_followers_collection(
    db, author, deliveries
):
    """The difference between the two narrow audiences is on the wire, not in a
    flag: the followers collection in ``to``, an empty ``cc``, no public
    collection anywhere - which is what makes a peer file the event as its own
    narrow post rather than a public one."""
    add_to_watchlist(_login("author"), "Ours Add", audience="followers")
    assert [a["type"] for a in deliveries] == ["Create"]
    activity = deliveries[0]
    assert PUBLIC_COLLECTION not in activity["to"] + activity["cc"]
    assert activity["cc"] == []
    assert activity["to"] == ["http://testserver/user/author/followers/"]


@pytest.mark.django_db
def test_a_public_add_is_addressed_to_the_public_collection(db, author, deliveries):
    """The shape a peer's parser files as public: the public collection in
    ``to``, the followers collection in ``cc``. An address-less event is what we
    used to send, and a peer files that as a direct message."""
    add_to_watchlist(_login("author"), "Open Add", audience="public")
    activity = deliveries[0]
    assert activity["to"] == [PUBLIC_COLLECTION]
    assert activity["cc"] == ["http://testserver/user/author/followers/"]


@pytest.mark.django_db
def test_a_finish_still_announces_its_watchlist_removal_only_when_public(
    db, author, film, deliveries
):
    """The owner's ruling of this session: this call site is the finish's, not
    the Watchlist setting's, and it stays exactly as the V-arc left it - a public
    finish announces the removal, a non-public one announces nothing."""
    client = _login("author")
    # Public finish: the film was on the Watchlist, so the finish takes it off
    # and the removal is announced - beside the review's own Create, which is
    # why the shelf events are picked out of the spy below.
    client.post(reverse("film-shelve", args=[film.id]))
    deliveries.clear()
    client.post(
        reverse("film-mark-watched", args=[film.id]),
        {"rating": "4", "content": "public finish", "visibility": "public"},
    )
    assert [a["type"] for a in deliveries] == ["Create", "Delete"]
    assert [a["type"] for a in _shelf_events(deliveries)] == ["Delete"]

    # Private finish, same shape one film later: nothing at all goes out - not
    # the review (the V-arc's gate) and not the removal.
    deliveries.clear()
    second = Film.objects.create(title="Second Film", year=2024)
    client.post(reverse("film-shelve", args=[second.id]))
    deliveries.clear()
    client.post(
        reverse("film-mark-watched", args=[second.id]),
        {"rating": "4", "content": "private finish", "visibility": "private"},
    )
    assert deliveries == []


# --- The inbound mirror -------------------------------------------------------


def _mirror(db, localname="carol"):
    return User(
        localname=f"{localname}@remote.example",
        local=False,
        actor_url=f"https://remote.example/user/{localname}/",
    )


def _wire_request():
    """A bare request carrying the test-server Host header (the wire harness's)."""
    request = RequestFactory().get("/")
    request.META["HTTP_HOST"] = "testserver"
    return request


def _inbound_shelf_event(film, *, to=None, cc=None, on_object=False):
    activity = {
        "@context": ["https://www.w3.org/ns/activitystreams"],
        "id": "https://remote.example/activity/shelf-1",
        "type": "Create",
        "actor": "https://remote.example/user/carol/",
        "object": {
            "id": "https://remote.example/user/carol/#shelve-to-read-1",
            "type": "ShelfEvent",
            "film": f"http://testserver/film/{film.pk}/",
            "shelf": Shelf.TO_READ,
        },
    }
    audience = {"to": to} if to is not None else {}
    if cc is not None:
        audience["cc"] = cc
    if on_object:
        activity["object"].update(audience)
    else:
        activity.update(audience)
    return activity


@pytest.mark.parametrize(
    "to,cc,expected",
    [
        (["https://remote.example/user/carol/followers"], [], "followers"),
        ([PUBLIC_COLLECTION], [], "public"),
        ([], [], "private"),
        (None, None, "public"),
    ],
)
@pytest.mark.django_db
def test_an_inbound_shelf_event_keeps_the_peer_s_audience(db, to, cc, expected):
    """D9's mirror half: we hold a copy of another instance's shelf event either
    way, so it is filed with the audience the peer addressed it to rather than
    the one our own default would have given it. An event that names no audience
    at all is the one case silence is not evidence of the narrowest answer -
    every mirror we ever stored was public by construction."""
    sender = _mirror(db)
    sender.save()
    film = Film.objects.create(title="Remote Add", year=2024)
    process_inbound_activity(
        _inbound_shelf_event(film, to=to, cc=cc), sender, _wire_request()
    )
    row = ShelfFilm.objects.get(user=sender, film=film)
    assert row.visibility == expected


@pytest.mark.django_db
def test_an_inbound_shelf_event_reads_the_object_arrays_too(db):
    """A peer that puts the audience on the object rather than the envelope is
    answered the same way - the arrays are the audience wherever they ride."""
    sender = _mirror(db)
    sender.save()
    film = Film.objects.create(title="Remote Add Two", year=2024)
    process_inbound_activity(
        _inbound_shelf_event(
            film, to=["https://remote.example/user/carol/followers"], on_object=True
        ),
        sender,
        _wire_request(),
    )
    assert (
        ShelfFilm.objects.get(user=sender, film=film).visibility == Visibility.FOLLOWERS
    )
