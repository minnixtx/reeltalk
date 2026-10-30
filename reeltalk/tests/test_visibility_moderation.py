"""V4: moderation around non-public posts, the written promises, the import default.

Three groups, in the order their failures would bite:

* **The queue around non-public posts.** The owner's V4 decision keeps today's
  policy and makes it deliberate: a report reveals the reported post to
  moderators whatever audience it was written for - a moderator who cannot
  read the text cannot judge it. What V4 adds is that the card says so: the
  audience marker travels onto the card, a line states the rule beside the
  text, and the "view post" link is only drawn when clicking it opens the
  page (the post page itself 404s on an audience the moderator may not read,
  and on a tombstone).
* **The help page.** The composer and the settings select carry no audience
  prose (cut at the V3 review), so /help/ is where the two honest sentences
  live: private does not hide anything from the operator, and a reported post
  is shown to moderators. The page is public - the promises bind visitors'
  expectations too - and the footer links to it.
* **The import default.** An imported rating carries the account's default
  audience (V4 decision), not a hardcoded public.

Plus the one accepted limit pinned rather than fixed: the inbox accepts a
verified sender's Like whatever the sender could have read - the signature
proves *who*, not *what they could see*, and a like adds a count and leaks no
text.

Session-proof rules as in the earlier visibility modules: real verified
members via ``member()``, ``force_login`` for each moderator position, and
assertions on rendered text a vacuous anonymous render cannot pass by accident
(the queue itself is moderator-only, so an anonymous GET is a 302, not a card).
"""

import pytest
from django.test import Client, RequestFactory
from django.urls import reverse

from reeltalk.activitypub.flags import handle_flag
from reeltalk.activitypub.interactions import handle_like
from reeltalk.core.import_export import import_film_csv
from reeltalk.core.models import Film, Like, ShelfFilm, Status, Visibility
from reeltalk.moderation.models import Report
from reeltalk.social.models import User
from reeltalk.tests.members import member

PASSWORD = "s3cretpass"
PUBLIC_MARK = "V4-PUBLIC-MARKER"
FOLLOWERS_MARK = "V4-FOLLOWERS-MARKER"
PRIVATE_MARK = "V4-PRIVATE-MARKER"


def logged_in(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _review(user, film, marker, visibility) -> Status:
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        content=f"<p>{marker}</p>",
        raw_content=marker,
        rating=4,
        visibility=visibility,
    )


@pytest.fixture
def author(db):
    return member(localname="author", password=PASSWORD)


@pytest.fixture
def follower(db):
    return member(localname="follower", password=PASSWORD)


@pytest.fixture
def mod(db):
    return member(localname="mod", password=PASSWORD, is_moderator=True)


@pytest.fixture
def films(db):
    return [
        Film.objects.create(title="Dune", year=2021),
        Film.objects.create(title="Arrival", year=2016),
        Film.objects.create(title="Annihilation", year=2018),
    ]


@pytest.fixture
def posts(db, author, films):
    return {
        "public": _review(author, films[0], PUBLIC_MARK, Visibility.PUBLIC),
        "followers": _review(author, films[1], FOLLOWERS_MARK, Visibility.FOLLOWERS),
        "private": _review(author, films[2], PRIVATE_MARK, Visibility.PRIVATE),
    }


def report_as(client, status, comment="not ok"):
    resp = client.post(
        reverse("report-status", args=[status.id]),
        {"category": "spam", "comment": comment},
    )
    assert resp.status_code == 302
    return Report.objects.get(target_status=status)


# --- The report door and the queue ------------------------------------------


@pytest.mark.django_db
def test_a_follower_can_report_a_followers_only_post(db, author, follower, posts):
    """D8's mirror: the door that refused a stranger (V1) admits a follower.

    ``may_read`` is the same answer for reporting and for reading, so a
    followers-only post is reportable by exactly the people who can read it.
    """
    follower.follows.add(author)
    report_as(logged_in(follower), posts["followers"])


@pytest.mark.django_db
def test_the_queue_marks_a_reported_followers_only_post_and_shows_its_text(
    db, author, follower, mod, posts
):
    """The V4 policy stated on the card: shown, marked, and said out loud.

    The text is inline (a moderator decides from it), the audience tag says
    what the author chose, and the line beside it states the rule. The
    "view post" link is absent - this moderator does not follow the author,
    and the post page would 404 them.
    """
    follower.follows.add(author)
    report_as(logged_in(follower), posts["followers"])
    page = logged_in(mod).get(reverse("moderation")).content.decode()
    assert FOLLOWERS_MARK in page
    assert '<span class="audience">Followers</span>' in page
    assert "marked Followers" in page
    assert "report-target-link" not in page


@pytest.mark.django_db
def test_a_forwarded_remote_report_on_a_private_post_lands_marked(
    db, author, mod, posts
):
    """The remote ``Flag`` path can name any local post, private included.

    A peer's report is filed whatever the audience (the report is the event,
    not a read permission), and the card carries the same marker and rule
    line as a local one. This is the policy the owner kept: a report reveals
    the post to moderators; the marker is what tells the moderator they are
    reading something the author never published.
    """
    private = posts["private"]
    sender = User.objects.create(
        localname="reporter@peer.example",
        local=False,
        actor_url="https://peer.example/users/reporter",
        inbox_url="https://peer.example/users/reporter/inbox",
    )
    handle_flag(
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://peer.example/flags/1",
            "type": "Flag",
            "actor": sender.actor_url,
            "content": "reported from over there",
            "object": [f"https://testserver/status/{private.pk}/"],
        },
        sender,
        RequestFactory().post("/"),
    )
    assert Report.objects.get(target_status=private).target_user_id == author.pk
    page = logged_in(mod).get(reverse("moderation")).content.decode()
    assert PRIVATE_MARK in page
    assert '<span class="audience">Private</span>' in page
    assert "marked Private" in page
    assert "report-target-link" not in page


@pytest.mark.django_db
def test_the_view_post_link_is_offered_for_a_public_report(db, author, mod, posts):
    """The public card keeps its link: clicking it opens the page."""
    report_as(logged_in(mod), posts["public"])
    page = logged_in(mod).get(reverse("moderation")).content.decode()
    assert "report-target-link" in page
    assert '<span class="audience">' not in page  # a public post carries no marker


@pytest.mark.django_db
def test_the_view_post_link_is_offered_to_a_moderator_who_can_read_the_post(
    db, author, mod, posts
):
    """The link gate is ``may_read`` itself, not "public": a moderator who
    follows the author opens their followers-only post like anyone entitled."""
    mod.follows.add(author)
    report_as(logged_in(mod), posts["followers"])
    page = logged_in(mod).get(reverse("moderation")).content.decode()
    assert "report-target-link" in page


@pytest.mark.django_db
def test_the_view_post_link_is_dropped_for_a_tombstone(db, author, mod, posts):
    """The card says the post is gone and stops offering a link that 404s."""
    report_as(logged_in(mod), posts["public"])
    posts["public"].delete()
    page = logged_in(mod).get(reverse("moderation")).content.decode()
    assert "since been deleted" in page
    assert "report-target-link" not in page


# --- The help page -----------------------------------------------------------


@pytest.mark.django_db
def test_the_help_page_states_both_promises():
    """Anonymous-readable, and it says the two things V4 promised to write down."""
    resp = Client().get("/help/")
    assert resp.status_code == 200
    page = resp.content.decode()
    # D8's operator half.
    assert "admin tools can read everything" in page
    # The moderation half the owner kept as decision 1 of this increment.
    assert "shown to moderators" in page


@pytest.mark.django_db
def test_the_footer_links_to_the_help_page():
    """A promise nobody can navigate to is not written down."""
    page = Client().get("/welcome/").content.decode()
    assert 'href="/help/"' in page


# --- The import default ------------------------------------------------------


def _rated_row(name="Heat"):
    return {"Name": name, "Type": "movie", "Your Rating": "8"}


@pytest.mark.django_db
def test_an_import_follows_the_account_default_audience(db):
    """V4 decision: an imported rating is a new review, so it follows the
    account's default - the setting reads "default audience for new reviews"
    and the import was the one write path that ignored it."""
    user = member(localname="private_importer", password=PASSWORD)
    user.default_visibility = Visibility.PRIVATE
    user.save()
    import_film_csv(user, [_rated_row()])
    status = Status.objects.get(user=user, status_type=Status.Type.REVIEW_RATING)
    assert status.visibility == Visibility.PRIVATE
    shelf_row = ShelfFilm.objects.get(user=user, film=status.film)
    assert shelf_row.visibility == Visibility.PRIVATE


@pytest.mark.django_db
def test_an_import_stays_public_for_a_default_public_account(db):
    """The default moved with the setting; the setting's own default did not move."""
    user = member(localname="public_importer", password=PASSWORD)
    import_film_csv(user, [_rated_row()])
    status = Status.objects.get(user=user, status_type=Status.Type.REVIEW_RATING)
    assert status.visibility == Visibility.PUBLIC


# --- The accepted limit, pinned ----------------------------------------------


@pytest.mark.django_db
def test_a_verified_remote_like_on_a_followers_only_post_is_counted(db, author, posts):
    """The inbox proves *who* sent a Like, not what the sender could read.

    Pinned as an accepted limit, not a feature: a signed Like from any known
    remote actor counts even if that actor does not follow the author - they
    could not have seen the post, but the Like carries no text, leaks nothing,
    and refusing it would mean this instance deciding what a peer's users saw.
    Mastodon's inbox posture is the same: accept what arrives, drop unknown
    targets.
    """
    sender = User.objects.create(
        localname="liker@far.example",
        local=False,
        actor_url="https://far.example/users/liker",
        inbox_url="https://far.example/users/liker/inbox",
    )
    target = posts["followers"]
    handle_like(
        {"object": f"https://testserver/status/{target.pk}/"},
        sender,
        RequestFactory().post("/"),
    )
    assert Like.objects.filter(user=sender, status=target).exists()
