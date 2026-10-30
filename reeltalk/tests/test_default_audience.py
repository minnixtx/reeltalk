"""V3: the account default audience (``VISIBILITY_PLAN.md`` §5, D4, D6, D8).

The default is a **preselection, not an enforcement**, and the shape of this
file is that distinction. Three groups:

* the setting itself - it lives on Profile / Settings, saves, refuses a value
  the select does not offer, and defaults public so every account that already
  exists keeps behaving exactly as it did;
* the composer preselect - the finish modal checks the member's default, and
  the member can still change it per review, which is what separates V3 from
  a mode that would force an audience;
* the boundaries the default must not cross - an existing review's audience is
  fixed at writing (D6), so changing the default rewrites nothing, and a reply
  takes its parent's audience whatever the replier's default says.

The composer offers no prose about the audiences: the owner's V3 browser
review cut the Private note altogether ("everyone already knows what private
means"), so a test would only pin a shape that was deliberately removed. The
full help copy, if it ever comes, is V4's.
"""

import re

import pytest
from django.test import Client
from django.urls import reverse

from reeltalk.core.models import (
    Film,
    ShelfFilm,
    Status,
    Visibility,
    add_reply,
    mark_watched,
)
from reeltalk.social.models import User
from reeltalk.tests.members import member, site_admin

PASSWORD = "s3cretpass"

SETTINGS_HELPER = (
    "Sets the audience preselected when you write a review. You can still "
    "change it on each review. This does not change reviews you already posted."
)


@pytest.fixture(autouse=True)
def admin(db):
    """/setup/ gates every page until a superuser exists - the trap N6 and V1
    both documented: without this, render assertions pass on a redirect."""
    return site_admin(localname="admin", password=PASSWORD)


@pytest.fixture
def film(db):
    return Film.objects.create(title="Dune", year=2021, genres=["Sci-Fi"])


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password=PASSWORD), (
        f"{localname} did not sign in - the page below would render "
        "anonymously and every assertion on it would pass vacuously"
    )
    return client


def _checked_audience(html: str) -> str:
    """The value of the one checked audience radio. Exactly one must be
    checked: zero means the preselect logic failed, two means the three
    ``{% if %}`` conditions stopped being mutually exclusive."""
    hits = re.findall(r'name="visibility" value="(\w+)"\s*checked', html)
    assert len(hits) == 1, f"expected exactly one checked radio, got {hits}"
    return hits[0]


# --- the setting ---------------------------------------------------------------


@pytest.mark.django_db
def test_a_new_member_defaults_to_public():
    """The migration back-fills public and the column default is public, so
    the personal-log mode is opt-in and no existing account moves (V3 rule:
    a back-fill of anything else rewrites what every account means)."""
    user = member(localname="fresh", password=PASSWORD)
    assert user.default_visibility == Visibility.PUBLIC


@pytest.mark.django_db
def test_settings_page_offers_the_select_with_the_current_value():
    user = member(localname="alice", password=PASSWORD, default_visibility="private")
    body = _login("alice").get("/preferences/profile/").content.decode()
    assert "Default audience for new reviews" in body
    assert SETTINGS_HELPER in body
    # The member's own value is the selected option, not the first one.
    assert '<option value="private" selected>' in body
    assert user.default_visibility == "private"


@pytest.mark.django_db
def test_settings_save_changes_the_default():
    member(localname="alice", password=PASSWORD)
    client = _login("alice")
    response = client.post(
        "/preferences/profile/",
        {"display_name": "", "summary": "", "default_visibility": "private"},
    )
    assert response.status_code == 302
    user = User.objects.get(localname="alice")
    assert user.default_visibility == "private"


@pytest.mark.django_db
def test_settings_refuse_an_audience_the_select_does_not_offer():
    """The ChoiceField is the boundary: a hand-built POST cannot store a value
    no widget on this instance can produce."""
    member(localname="alice", password=PASSWORD)
    client = _login("alice")
    response = client.post(
        "/preferences/profile/",
        {"display_name": "", "summary": "", "default_visibility": "direct"},
    )
    assert response.status_code == 200  # re-rendered with the field error
    user = User.objects.get(localname="alice")
    assert user.default_visibility == Visibility.PUBLIC


# --- the composer preselect ------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("stored", ["public", "followers", "private"])
def test_the_finish_modal_preselects_the_account_default(db, film, stored):
    member(localname=f"user-{stored}", password=PASSWORD, default_visibility=stored)
    body = _login(f"user-{stored}").get(f"/film/{film.id}/").content.decode()
    assert _checked_audience(body) == stored


@pytest.mark.django_db
def test_an_unrecognised_stored_value_falls_back_to_public(db, film):
    """Same fallback as ``_posted_visibility``: a value no form of this
    instance offered lands on public on the server, so the drawn form must
    agree rather than check nothing and let the browser decide."""
    user = member(localname="odd", password=PASSWORD)
    User.objects.filter(pk=user.pk).update(default_visibility="direct")
    body = _login("odd").get(f"/film/{film.id}/").content.decode()
    assert _checked_audience(body) == "public"


@pytest.mark.django_db
def test_the_default_lands_the_review_private_end_to_end(db, film):
    """Walked as the browser walks it: set the default on the settings page,
    open the film page (private checked), submit what the checked form
    submits. The shelf row carries the same audience (D5)."""
    user = member(localname="log", password=PASSWORD)
    client = _login("log")
    client.post(
        "/preferences/profile/",
        {"display_name": "", "summary": "", "default_visibility": "private"},
    )
    body = client.get(f"/film/{film.id}/").content.decode()
    assert _checked_audience(body) == "private"
    client.post(
        reverse("film-mark-watched", args=[film.id]),
        {"rating": "4", "content": "a private note", "visibility": "private"},
    )
    status = Status.objects.get(user=user, film=film)
    assert status.visibility == Visibility.PRIVATE
    assert ShelfFilm.objects.get(user=user, film=film).visibility == Visibility.PRIVATE


@pytest.mark.django_db
def test_an_explicit_choice_beats_the_account_default(db, film):
    """The line between V3 and a mode: a private-by-default member who picks
    Public on one review gets a public review. The default preselects and
    never overrides."""
    user = member(localname="mixer", password=PASSWORD, default_visibility="private")
    client = _login("mixer")
    client.post(
        reverse("film-mark-watched", args=[film.id]),
        {"rating": "4", "content": "public anyway", "visibility": "public"},
    )
    assert Status.objects.get(user=user, film=film).visibility == Visibility.PUBLIC


# --- what the default must not touch --------------------------------------------


@pytest.mark.django_db
def test_changing_the_default_leaves_existing_reviews_alone(db, film):
    """D6: the audience is fixed at writing. Flipping the default to private
    after the fact rewrites no row - not the review, and not a re-finish of
    it, which changes text and rating only."""
    user = member(localname="alice", password=PASSWORD)
    client = _login("alice")
    mark_watched(user, film, rating=4, content="<p>first</p>", raw_content="first")
    status = Status.objects.get(user=user, film=film)
    assert status.visibility == Visibility.PUBLIC

    client.post(
        "/preferences/profile/",
        {"display_name": "", "summary": "", "default_visibility": "private"},
    )
    status.refresh_from_db()
    assert status.visibility == Visibility.PUBLIC

    # The edit modal posts no visibility field at all (D6 shows no control);
    # the missing field must not fall through to the account default either.
    client.post(
        reverse("film-mark-watched", args=[film.id]),
        {"rating": "5", "content": "edited"},
    )
    status.refresh_from_db()
    assert status.visibility == Visibility.PUBLIC
    assert status.rating == 5


@pytest.mark.django_db
def test_a_reply_takes_its_parent_s_audience_not_any_default(db):
    """Contract from plan §3, pinned against V3 specifically: the account
    default says nothing about replies, in either direction."""
    author = member(localname="author", password=PASSWORD, default_visibility="public")
    parent_film = Film.objects.create(title="Arrival", year=2016, genres=["Sci-Fi"])
    parent = mark_watched(
        author,
        parent_film,
        rating=4,
        content="<p>p</p>",
        raw_content="p",
        visibility=Visibility.PRIVATE,
    )
    reply = add_reply(author, parent, content="<p>r</p>", raw_content="r")
    assert reply.visibility == Visibility.PRIVATE

    # And the mirror case: a private-by-default account replying under a
    # public review does not make the reply private.
    logger = member(localname="logger", password=PASSWORD, default_visibility="private")
    public_film = Film.objects.create(title="Heat", year=1995, genres=["Crime"])
    public_parent = mark_watched(
        logger, public_film, rating=5, content="<p>q</p>", raw_content="q"
    )
    reply2 = add_reply(logger, public_parent, content="<p>r2</p>", raw_content="r2")
    assert reply2.visibility == Visibility.PUBLIC
