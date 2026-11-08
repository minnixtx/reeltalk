"""W1: the list audience, walked surface by surface (the W-plan's §5).

**The shape of this file is the point**, and it is borrowed whole from
``test_visibility_read_paths.py``: the risk of the feature is not the value on the
face, it is one read path out of twelve forgetting the filter and a member's
private list reaching a stranger. So the central test does not check one surface
well, it walks every surface that can show a list for every class of viewer, and
the plan's §2 table is the inventory it walks. A surface added later that skips
``audience_q``/``may_read`` fails here rather than shipping a leak.

The viewer classes, same four the V-arc settled and for the same reasons:

* **anonymous** - the floor: public only, and the class a surface most easily
  gets wrong by forgetting a ``request.user`` check;
* **stranger** - a signed-in member who follows the author nowhere. Proves a
  login is not itself an audience;
* **follower** - entitled to the followers rows and to nothing else;
* **moderator** - the queue is the only place a narrow audience is widened, and
  a list page is not the queue (the profile's rule, applied to lists).

Plus the author, who must see their own rows everywhere (D12): a filter that hid
a member's private list from *them* would pass every other test in this file.

The surfaces are the plan's §2 rows 1 and 3-6 plus the two machine-facing doors:
the list page (both arms), the author's lists page, the profile's Lists tab (the
``list_count`` row), the feed, the Save door and control, and the Saved tab. Row 9
(the reply's link back to its list) is proven unreachable-by-construction at the
reply door rather than changed. The outbound rows (10, 11) are pinned by the
delivery spy at the bottom - the same seam the V-arc moved to, because the gate
lives in ``broadcast._announce``, not in whichever view called.

Row 12 (the inbound mirror files the audience its peer addressed) is pinned in
``test_lists_inbound.py``, where the signed-inbox harness lives.
"""

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from reeltalk.activitypub.identity import PUBLIC_COLLECTION
from reeltalk.core.models import Visibility
from reeltalk.lists.models import FilmList, ListSave
from reeltalk.lists.services import create_list
from reeltalk.tests.members import member, moderator, site_admin

PASSWORD = "s3cretpass"

PUBLIC_MARK = "PUBLIC-LIST"
FOLLOWERS_MARK = "FOLLOWERS-LIST"
PRIVATE_MARK = "PRIVATE-LIST"


@pytest.fixture(autouse=True)
def admin(db):
    """``/`` redirects to ``/setup/`` until a superuser exists, after which every
    render assertion here would pass vacuously (the trap N6 documented)."""
    return site_admin(localname="admin", password=PASSWORD)


@pytest.fixture
def author(db):
    """The member whose lists are under test."""
    return member(localname="author", password=PASSWORD)


@pytest.fixture
def follower(db):
    return member(localname="follower", password=PASSWORD)


@pytest.fixture
def stranger(db):
    """A signed-in member who follows nobody."""
    return member(localname="stranger", password=PASSWORD)


@pytest.fixture
def lists(db, author):
    """The three audiences, one list each, all by the same author."""
    return {
        "public": create_list(author, title=PUBLIC_MARK, visibility=Visibility.PUBLIC),
        "followers": create_list(
            author, title=FOLLOWERS_MARK, visibility=Visibility.FOLLOWERS
        ),
        "private": create_list(
            author, title=PRIVATE_MARK, visibility=Visibility.PRIVATE
        ),
    }


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password=PASSWORD), (
        f"{localname} did not sign in - the page below would render anonymously "
        "and every assertion on it would pass vacuously"
    )
    return client


def audience_marker(word: str) -> str:
    """The open tag of the rendered audience marker for ``word``.

    The tag, not the bare word - the same trap the V-arc documented: "Private"
    appears in other markup (the composer's radio label, the create form's), so
    a substring test would pass on a page that drew no marker at all.
    """
    return f'<span class="audience" role="img" title="{word}" aria-label="{word}">'


def _surfaces(lists) -> list[tuple[str, str]]:
    """Every URL that can show one of the author's lists (§2 rows 1, 3, 4, 7).

    The inventory is the test: a new surface belongs in this list, and adding it
    here is what makes the walk cover it. The list-page entry points at the
    **private** list's own URL, which is the one every non-author viewer must
    not open; the other two URLs show all three side by side.
    """
    private = lists["private"]
    return [
        ("list page", f"/list/{private.pk}/"),
        ("author lists page", f"/user/{private.user.localname}/lists/"),
        ("home following", "/"),
        ("feed page following", "/feed/page/"),
    ]


# --- The walk ----------------------------------------------------------------


@pytest.mark.parametrize("who", ["anonymous", "stranger", "follower", "moderator"])
@pytest.mark.django_db
def test_a_private_list_is_absent_from_every_surface_for_every_other_viewer(
    db, author, follower, stranger, lists, who
):
    """The one test the whole increment is built on (plan §5, W1 "Tests")."""
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

    for label, path in _surfaces(lists):
        response = client.get(path)
        assert response.status_code in (200, 302, 404), (
            f"{label}: {response.status_code}"
        )
        body = response.content.decode()
        assert PRIVATE_MARK not in body, f"private list reached {label} as {who}"
        if who != "follower":
            assert FOLLOWERS_MARK not in body, (
                f"followers-only list reached {label} as {who}"
            )
    # The public list is the control on all of that: if it never rendered
    # either, the walk would be asserting nothing.
    assert PUBLIC_MARK in client.get("/user/author/lists/").content.decode()


@pytest.mark.django_db
def test_the_author_still_sees_their_own_private_list_on_every_surface(
    db, author, lists
):
    """D12: the audience decides who *else* sees it, never whether the author does."""
    client = _login("author")
    for label, path in _surfaces(lists):
        body = client.get(path).content.decode()
        assert PRIVATE_MARK in body, f"author's own private list missing from {label}"


@pytest.mark.django_db
def test_a_followers_list_reaches_a_follower_and_nobody_else(
    db, author, follower, stranger, lists
):
    follower.follows.add(author)
    path = f"/list/{lists['followers'].pk}/"
    assert _login("follower").get(path).status_code == 200
    assert _login("stranger").get(path).status_code == 404
    assert Client().get(path).status_code == 404
    # And on the feed, where the audience rule is the follow set itself.
    assert FOLLOWERS_MARK in _login("follower").get("/").content.decode()
    assert FOLLOWERS_MARK not in _login("stranger").get("/").content.decode()


@pytest.mark.django_db
def test_the_list_page_404s_on_both_arms_for_a_list_you_may_not_read(
    db, author, stranger, lists
):
    """404, not 403: a 403 confirms the list exists. The AP arm is the face's own
    status route - it already ran ``may_read`` because a list face *is* a
    ``Status`` (§2 row 2), and this pins that it stays true for a list."""
    face_id = lists["private"].status_id
    for client in (Client(), _login("stranger")):
        assert (
            client.get(
                f"/status/{face_id}/", HTTP_ACCEPT="application/activity+json"
            ).status_code
            == 404
        )
    owner = _login("author")
    # The human arm of a list face redirects to the list page (pre-existing
    # behaviour) - and it only gets to redirect for a reader ``may_read``
    # admits, which is what the stranger's 404 above proves.
    assert owner.get(f"/status/{face_id}/", follow=True).status_code == 200
    assert (
        owner.get(
            f"/status/{face_id}/", HTTP_ACCEPT="application/activity+json"
        ).status_code
        == 200
    )


@pytest.mark.django_db
def test_a_reply_to_a_list_face_the_reader_cannot_read_is_refused(db, stranger, lists):
    """§2 row 9, proven rather than changed: the reply door runs ``may_read`` on
    the face, so no reply can ever carry a link back to a list its author could
    not open. The audience is immutable, so nothing widens that later either."""
    response = _login("stranger").post(
        reverse("status-reply", args=[lists["private"].status_id]),
        {"content": "hello"},
    )
    assert response.status_code == 404


# --- The tab strip (the ``list_count`` leak) ---------------------------------


@pytest.mark.django_db
def test_the_lists_tab_does_not_light_for_a_reader_who_cannot_open_any_list(
    db, stranger
):
    """§2 row 3: the count lights a tab, so a count that counts lists the reader
    cannot open points at a page that answers "No lists yet." A tab to nothing is
    worse than no tab (R138 decision 3), and this is that rule with an audience on
    top of it."""
    lonely = member(localname="lonely", password=PASSWORD)
    create_list(lonely, title="Only-Mine Shelf", visibility=Visibility.PRIVATE)
    for client in (Client(), _login("stranger")):
        body = client.get("/user/lonely/").content.decode()
        assert 'href="/user/lonely/lists/"' not in body
        # And the page itself, reached by URL anyway, shows the empty state.
        assert (
            "Only-Mine Shelf"
            not in Client().get("/user/lonely/lists/").content.decode()
        )
    # A follower of the author does get the row - there is a list behind it for
    # them. Adding a followers-only list is what lights it; the private one
    # above never does, for anybody but its author.
    create_list(lonely, title="Ours-Only Shelf", visibility=Visibility.FOLLOWERS)
    follower2 = member(localname="follower2", password=PASSWORD)
    follower2.follows.add(lonely)
    assert (
        'href="/user/lonely/lists/"'
        in _login("follower2").get("/user/lonely/").content.decode()
    )
    assert (
        'href="/user/lonely/lists/"'
        not in _login("stranger").get("/user/lonely/").content.decode()
    )


# --- The Save door and its control (D4) --------------------------------------


@pytest.mark.django_db
def test_the_save_door_refuses_a_list_the_reader_cannot_read(
    db, author, follower, stranger, lists
):
    """D4: a reader who cannot read a list cannot save it. The route refuses
    exactly what the page withholds (R85), and the public list is the control
    proving the door still opens for a readable one."""
    client = _login("stranger")
    assert (
        client.post(reverse("list-save", args=[lists["private"].pk])).status_code == 404
    )
    assert (
        client.post(reverse("list-save", args=[lists["followers"].pk])).status_code
        == 404
    )
    assert (
        client.post(reverse("list-unsave", args=[lists["private"].pk])).status_code
        == 404
    )
    assert not ListSave.objects.filter(user=stranger, film_list=lists["private"])
    # The control: a readable list still saves.
    assert (
        client.post(reverse("list-save", args=[lists["public"].pk])).status_code == 200
    )
    assert ListSave.objects.filter(user=stranger, film_list=lists["public"]).exists()
    # And the entitled reader still saves the followers list.
    follower.follows.add(author)
    assert (
        _login("follower")
        .post(reverse("list-save", args=[lists["followers"].pk]))
        .status_code
        == 200
    )


@pytest.mark.django_db
def test_the_save_control_is_drawn_for_the_reader_the_door_would_open(
    db, author, follower, lists
):
    """The positive half of "the control is not drawn": a follower who may read
    the followers-only list sees the Save button on its page. The negative half
    is the 404 above - a page that does not render draws no control."""
    follower.follows.add(author)
    body = _login("follower").get(f"/list/{lists['followers'].pk}/").content.decode()
    assert "list-save-btn" in body


@pytest.mark.django_db
def test_the_saved_tab_drops_a_list_the_saver_lost_access_to(
    db, author, follower, lists
):
    """§2 row 5: the Saved tab is filtered too. A list's audience cannot change,
    but the *reader's* entitlement can - unfollowing the author ends the
    followers-row entitlement, and the saved card goes with it."""
    follower.follows.add(author)
    client = _login("follower")
    assert (
        client.post(reverse("list-save", args=[lists["followers"].pk])).status_code
        == 200
    )
    assert (
        FOLLOWERS_MARK in client.get("/user/follower/lists/?tab=saved").content.decode()
    )
    follower.follows.remove(author)
    assert (
        FOLLOWERS_MARK
        not in client.get("/user/follower/lists/?tab=saved").content.decode()
    )


# --- The marker on the list page ----------------------------------------------


@pytest.mark.django_db
def test_the_list_page_draws_the_audience_marker(db, author, follower, lists):
    """The list page renders the same partial every other surface renders - the
    owner's ruling is one drawing wherever the audience appears."""
    assert (
        audience_marker("Private")
        in _login("author").get(f"/list/{lists['private'].pk}/").content.decode()
    )
    follower.follows.add(author)
    assert (
        audience_marker("Followers")
        in _login("follower").get(f"/list/{lists['followers'].pk}/").content.decode()
    )
    assert (
        audience_marker("Public")
        in Client().get(f"/list/{lists['public'].pk}/").content.decode()
    )


# --- The create door and the immutability -------------------------------------


@pytest.mark.django_db
def test_the_create_form_offers_the_three_audiences(db, author):
    page = _login("author").get(reverse("list-create")).content.decode()
    for value in ("public", "followers", "private"):
        assert f'name="visibility" value="{value}"' in page, value
    # And the two fields that were already there still render. This is the
    # owner's browser find of 2026-11-08: the first version of this page drew
    # the remaining fields with ``{{ field.as_p }}`` inside a loop, and a
    # BoundField has no ``as_p`` - ``silent_variable_failure`` turns that into
    # *no markup at all* rather than an error, so the page showed the audience
    # radios and nothing else, and submitting did nothing visible. A render
    # test that only looks for the new control is exactly the test that misses
    # the old ones going missing.
    assert 'name="title"' in page
    assert 'name="description"' in page


@pytest.mark.parametrize("value", ["public", "followers", "private"])
@pytest.mark.django_db
def test_creating_a_list_stamps_the_chosen_audience_on_the_face(db, author, value):
    response = _login("author").post(
        reverse("list-create"),
        {"title": f"Stamped {value}", "description": "", "visibility": value},
    )
    assert response.status_code == 302
    film_list = FilmList.objects.get(title=f"Stamped {value}")
    assert film_list.status.visibility == value


@pytest.mark.django_db
def test_a_bogus_audience_is_refused_and_a_missing_one_is_public(db, author):
    """The form validates what it offers (a value it never rendered is not
    silently stored), and a hand-built post that names no audience gets the
    audience every list already had - the composer's fallback rule."""
    client = _login("author")
    response = client.post(
        reverse("list-create"),
        {"title": "Bogus Audience", "description": "", "visibility": "followres"},
    )
    assert response.status_code == 200
    assert not FilmList.objects.filter(title="Bogus Audience").exists()
    response = client.post(
        reverse("list-create"), {"title": "Silent Audience", "description": ""}
    )
    assert response.status_code == 302
    assert FilmList.objects.get(title="Silent Audience").status.visibility == "public"


@pytest.mark.django_db
def test_the_edit_page_offers_no_audience_control(db, author, lists):
    """D2: the audience is fixed at creation, so the edit page shows no control
    rather than one the view would ignore - the V1 rule for a post, applied."""
    page = _login("author").get(f"/list/{lists['public'].pk}/edit/").content.decode()
    assert 'name="visibility"' not in page


@pytest.mark.django_db
def test_the_edit_route_cannot_change_an_audience(db, author, lists):
    """No control *and no writer*: a hand-built POST naming a different audience
    changes the prose it names and nothing else. The immutability is the absence
    of a writer, not a hidden field."""
    private = lists["private"]
    response = _login("author").post(
        f"/list/{private.pk}/edit/",
        {"title": "Renamed Anyway", "description": "", "visibility": "public"},
    )
    assert response.status_code == 302
    private.refresh_from_db()
    assert private.title == "Renamed Anyway"
    assert private.status.visibility == Visibility.PRIVATE


# --- The wire (§2 rows 10 and 11) ---------------------------------------------


@pytest.fixture
def deliveries(monkeypatch):
    """Every delivery the broadcast layer actually attempts.

    Patched a level below the views on purpose, exactly as the V-arc's fixture
    is: the gate lives in ``broadcast._announce``, so a spy on the view's
    ``broadcast_list_*`` calls would see the same call for all three audiences
    and prove nothing at all.
    """
    from reeltalk.activitypub import broadcast

    calls: list[dict] = []

    def fake(request, actor, activity, recipients):
        calls.append(activity)
        return []

    monkeypatch.setattr(broadcast, "_deliver_signed", fake)
    return calls


@pytest.mark.django_db
def test_a_private_list_broadcasts_nothing_on_create_update_or_delete(
    db, author, deliveries
):
    """D5, the whole sentence: a private list sends nothing, at any of the three
    doors. The public control after it is what makes the empty lists mean the
    audience and not a spy that never fires."""
    client = _login("author")
    response = client.post(
        reverse("list-create"),
        {"title": "Secret Shelf", "description": "", "visibility": "private"},
    )
    assert response.status_code == 302
    assert deliveries == []
    film_list = FilmList.objects.get(title="Secret Shelf")
    client.post(
        reverse("list-edit", args=[film_list.pk]),
        {"title": "Secret Shelf Renamed", "description": ""},
    )
    assert deliveries == []
    client.post(reverse("list-delete", args=[film_list.pk]))
    assert deliveries == []
    # The control: the same three doors on a public list all send.
    client.post(reverse("list-create"), {"title": "Open Shelf", "description": ""})
    assert [a["type"] for a in deliveries] == ["Create"]
    open_list = FilmList.objects.get(title="Open Shelf")
    client.post(
        reverse("list-edit", args=[open_list.pk]),
        {"title": "Open Shelf Renamed", "description": ""},
    )
    assert [a["type"] for a in deliveries] == ["Create", "Update"]
    client.post(reverse("list-delete", args=[open_list.pk]))
    assert [a["type"] for a in deliveries] == ["Create", "Update", "Delete"]


@pytest.mark.django_db
def test_a_followers_list_is_addressed_to_the_followers_collection(
    db, author, deliveries
):
    """The difference between the two narrow audiences is on the wire, not in a
    flag: a followers-only list carries the followers collection in ``to`` and an
    empty ``cc`` - no public collection, no mention set - which is what makes a
    peer file it as its own narrow post rather than a public one."""
    _login("author").post(
        reverse("list-create"),
        {"title": "Ours Shelf", "description": "", "visibility": "followers"},
    )
    assert [a["type"] for a in deliveries] == ["Create"]
    activity = deliveries[0]
    assert PUBLIC_COLLECTION not in activity["to"] + activity["cc"]
    assert activity["cc"] == []
    assert len(activity["to"]) == 1
    assert activity["to"][0].endswith("/user/author/followers/")


@pytest.mark.django_db
def test_rebroadcast_lists_withholds_the_private_list(db, author, deliveries):
    """§2 row 11: the repair command must not re-publish what the routes now
    withhold. The public list is the control that the command still sends."""
    create_list(author, title="Rebroadcast Public", visibility=Visibility.PUBLIC)
    create_list(author, title="Rebroadcast Private", visibility=Visibility.PRIVATE)
    call_command("rebroadcast_lists")
    assert len(deliveries) == 1
    content = deliveries[0]["object"]["content"]
    assert "Rebroadcast Public" in content
    assert "Rebroadcast Private" not in content
