"""The notifications page and the mark-all-read control (increment 3).

The ledger and the unread contract are pinned in ``test_notifications.py``;
what is pinned here is the surface built on top of them, in the order each
one would be a bug:

* **The page is the member's own and nobody else's.** One recipient filter,
  proved by a second user's row being absent rather than by the count
  looking right.
* **Each kind deep-links where the brief says it does** — like and reply to
  the status, follow to the actor's profile — and the reply links to the
  *reply*, not to the post it answered. Each case carries the opposite
  assertion so a wrong target cannot pass by also being present.
* **Both SET_NULL edges drop the deep link and keep the row.** A deleted
  status must not take the record of the like with it, and neither must a
  deleted actor.
* **Opening the page writes nothing.** The unread count survives a GET.
  This is the property that makes the mark-read timestamp safe at all: were
  the page to mark read on render, a reload would silently eat the state.
* **Mark-all-read is a POST with CSRF**, affects only the caller, and is
  one UPDATE on the user row — no write to the notification table, which
  is what R93 bought.

M3 (owner rulings D6 and D7) adds two sections at the end: which rows render
as unseen, and the copy that names the film each row is about.
"""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from reeltalk.core.models import Film, Status
from reeltalk.notifications.models import (
    Notification,
    mark_all_read,
    notify,
)
from reeltalk.notifications.views import (
    NOTIFICATION_EXCERPT_CHARS,
    NOTIFICATIONS_PAGE_SIZE,
)
from reeltalk.tests.members import member as create_member

User = get_user_model()


@pytest.fixture
def alice(db):
    return create_member(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return create_member(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def post(db, alice, dune):
    return Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW,
        content="a review of Dune",
    )


@pytest.fixture
def member(client, alice):
    client.force_login(alice)
    return client


# A ledger row wears a second class when it is unseen (M3, D6), so a count of
# "how many rows are on this page" matches the class prefix rather than the
# seen spelling. Pins that mean the seen spelling specifically say so inline.
ROW_OPEN = '<li class="review'


def _spread(*notes, base=None):
    """Give rows distinct ``created`` values an ordering test can rely on.

    Two ``timezone.now()`` calls can land in the same microsecond, and a
    reverse-chronological assertion that depends on the clock's luck is not
    an assertion.
    """
    base = base or notes[0].created
    for offset, note in enumerate(notes):
        Notification.objects.filter(pk=note.pk).update(
            created=base + timedelta(minutes=offset)
        )
    return notes


# --- Who may see the page ----------------------------------------------------


def test_anonymous_is_sent_to_login(db):
    response = Client().get("/notifications/")
    assert response.status_code == 302
    assert response.url == "/login/?next=/notifications/"


def test_a_member_gets_their_own_ledger_newest_first(member, alice, bob, post):
    older = notify(alice, bob, Notification.Kind.FOLLOW)
    newer = notify(alice, bob, Notification.Kind.LIKE, post)
    _spread(older, newer)
    # A second recipient's row sits in the same table, addressed to bob
    # about alice.
    notify(bob, alice, Notification.Kind.LIKE, post)
    content = member.get("/notifications/").content.decode()
    # The row count is what proves the recipient filter. Every string
    # assertion below still holds with the filter removed, because bob's row
    # renders the same verbs — so without this one the test would pass on an
    # unscoped ``Notification.objects.all()``. (Found by mutation: dropping
    # the filter left the original assertions all green.)
    assert content.count(ROW_OPEN) == 2
    assert "followed you" in content
    assert "applauded" in content
    assert content.index("followed you") > content.index("applauded")


def test_the_page_renders_every_kind_it_holds(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.FOLLOW)
    notify(alice, bob, Notification.Kind.LIKE, post)
    notify(alice, bob, Notification.Kind.REPLY, post)
    content = member.get("/notifications/").content.decode()
    assert "followed you" in content
    assert "applauded" in content
    assert "replied to your post" in content


# --- Deep links --------------------------------------------------------------


def test_a_like_deep_links_to_the_post_that_was_liked(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    content = member.get("/notifications/").content.decode()
    assert f'href="/status/{post.id}/"' in content


def test_a_reply_links_to_the_reply_and_not_the_post_answered(
    member, alice, bob, dune, post
):
    reply = Status.objects.create(
        user=bob,
        film=dune,
        status_type=Status.Type.COMMENT,
        content="a reply",
        reply_parent=post,
    )
    notify(alice, bob, Notification.Kind.REPLY, reply)
    content = member.get("/notifications/").content.decode()
    # The reply is what arrived, so the reply is what the row opens.
    assert f'href="/status/{reply.id}/"' in content
    # Control on the target rather than the presence: the answered post's
    # URL must not be the link.
    assert f'href="/status/{post.id}/"' not in content


def test_a_follow_deep_links_to_the_actors_profile(member, alice, bob):
    notify(alice, bob, Notification.Kind.FOLLOW)
    content = member.get("/notifications/").content.decode()
    assert 'href="/user/bob/"' in content
    # A follow has no post behind it, so no status link belongs on the row.
    assert "/status/" not in content


def test_a_status_that_was_hard_deleted_keeps_the_row_and_loses_the_link(
    member, alice, bob, post
):
    notify(alice, bob, Notification.Kind.LIKE, post)
    status_url = f'href="/status/{post.id}/"'
    assert status_url in member.get("/notifications/").content.decode()
    # The control above is what makes the next assertion mean SET_NULL
    # rather than "the page never draws links".
    Status.objects.filter(pk=post.pk).delete()
    content = member.get("/notifications/").content.decode()
    assert status_url not in content
    assert "applauded a post that is no longer here" in content


def test_a_soft_deleted_post_loses_the_link_too(member, alice, bob, post):
    # SET_NULL only fires on a hard delete, so a soft-deleted post is the
    # other half of this edge: the row is still there, the FK still points
    # at it, and /status/<id>/ 404s on ``deleted=False``. Without the
    # ``deleted`` check the page would hand the user a link that dead-ends.
    notify(alice, bob, Notification.Kind.LIKE, post)
    status_url = f'href="/status/{post.id}/"'
    assert status_url in member.get("/notifications/").content.decode()
    post.delete()
    content = member.get("/notifications/").content.decode()
    assert status_url not in content
    assert "applauded a post that is no longer here" in content
    # The row survives — the like happened whatever became of the post.
    assert content.count(ROW_OPEN) == 1


def test_a_soft_deleted_reply_loses_its_link_too(member, alice, bob, dune, post):
    reply = Status.objects.create(
        user=bob,
        film=dune,
        status_type=Status.Type.COMMENT,
        content="a reply",
        reply_parent=post,
    )
    notify(alice, bob, Notification.Kind.REPLY, reply)
    reply_url = f'href="/status/{reply.id}/"'
    assert reply_url in member.get("/notifications/").content.decode()
    reply.delete()
    content = member.get("/notifications/").content.decode()
    assert reply_url not in content
    assert "replied to a post that is no longer here" in content


def test_a_deleted_actor_keeps_the_row_and_loses_the_profile_link(
    member, alice, bob, post
):
    notify(alice, bob, Notification.Kind.LIKE, post)
    assert 'href="/user/bob/"' in member.get("/notifications/").content.decode()
    bob.delete()
    content = member.get("/notifications/").content.decode()
    assert 'href="/user/bob/"' not in content
    # The event is still on the ledger; only the name is gone.
    assert "Someone" in content
    assert "applauded" in content
    assert Notification.objects.filter(recipient=alice, actor__isnull=True).count() == 1


def test_a_deleted_actor_on_a_follow_leaves_the_row_with_no_link_at_all(
    member, alice, bob
):
    # The follow's only deep link was the actor, so this row ends up with
    # nothing clickable and still reads as an event.
    notify(alice, bob, Notification.Kind.FOLLOW)
    bob.delete()
    content = member.get("/notifications/").content.decode()
    # Asserted as the two fragments the row is built from rather than one
    # contiguous string: the actor span and the verb are separate elements,
    # so the collapsed-whitespace sentence only exists once a browser draws it.
    assert "Someone" in content
    assert "followed you" in content
    assert "/user/bob/" not in content


# --- Unread count and the mark-all-read control ------------------------------


def test_the_page_states_the_unread_count(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    notify(alice, bob, Notification.Kind.REPLY, post)
    content = member.get("/notifications/").content.decode()
    assert "2 unread notifications" in content


def test_opening_the_page_marks_nothing_read(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    before = User.objects.get(pk=alice.pk).notifications_last_read
    member.get("/notifications/")
    assert Notification.unread_for(User.objects.get(pk=alice.pk)).count() == 1
    assert User.objects.get(pk=alice.pk).notifications_last_read == before


def test_a_read_ledger_shows_no_mark_all_read_control(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    mark_all_read(alice)
    content = member.get("/notifications/").content.decode()
    assert "Mark all read" not in content
    assert "Nothing new since" in content
    # Control: an unread row brings the control back.
    notify(alice, bob, Notification.Kind.REPLY, post)
    assert "Mark all read" in member.get("/notifications/").content.decode()


def test_mark_all_read_clears_the_unread_state(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    notify(alice, bob, Notification.Kind.REPLY, post)
    assert Notification.unread_for(alice).count() == 2
    response = member.post("/notifications/read/")
    assert response.status_code == 302
    assert response.url == "/notifications/"
    # Read through a fresh instance on purpose. The view marks read on
    # ``request.user``, which is a different object from this test's
    # ``alice``; asserting against the stale one would test nothing about
    # what the POST wrote, and asserting through it would test the
    # in-memory refresh increment 1 already pins.
    alice.refresh_from_db()
    assert Notification.unread_for(alice).count() == 0
    content = member.get("/notifications/").content.decode()
    assert "Mark all read" not in content
    # The rows are still on the page — read is a timestamp, not a delete.
    assert "applauded" in content


def test_mark_all_read_touches_only_the_callers_user_row(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    notify(bob, alice, Notification.Kind.LIKE, post)
    with CaptureQueriesContext(connection) as captured:
        member.post("/notifications/read/")
    updates = [
        q["sql"]
        for q in captured.captured_queries
        if q["sql"].strip().upper().startswith("UPDATE")
    ]
    assert len(updates) == 1
    assert updates[0].startswith('UPDATE "social_user"')
    # R93: the cost of marking read must not scale with the unread count,
    # so the ledger table is not written at all.
    assert "notifications_notification" not in updates[0]
    # And it was the caller who was read, not the other party.
    alice.refresh_from_db()
    assert Notification.unread_for(alice).count() == 0
    assert Notification.unread_for(bob).count() == 1


def test_mark_all_read_is_post_only(alice):
    client = Client()
    client.force_login(alice)
    assert client.get("/notifications/read/").status_code == 405


def test_mark_all_read_requires_a_csrf_token(alice):
    client = Client(enforce_csrf_checks=True)
    assert client.login(username="alice", password="s3cretpass")
    assert client.post("/notifications/read/").status_code == 403


def test_anonymous_cannot_mark_read(db, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    response = Client().post("/notifications/read/")
    assert response.status_code == 302
    assert "/login/" in response.url
    assert Notification.unread_for(alice).count() == 1


def test_the_mark_all_read_control_is_a_button_in_a_post_form(member, alice, bob, post):
    # The badge in increment 4 must be an <a> because it only navigates;
    # this one really acts, so a <button> inside a CSRF POST form is the
    # correct shape and a link would be the wrong one.
    carol = create_member(localname="carol", password="s3cretpass")
    notify(alice, bob, Notification.Kind.LIKE, post)
    notify(alice, carol, Notification.Kind.LIKE, post)
    content = member.get("/notifications/").content.decode()
    start = content.index("Mark all read")
    form = content[max(0, start - 400) : start]
    assert 'method="post"' in form
    assert "<button" in form
    assert "csrfmiddlewaretoken" in form


# --- Pagination ------------------------------------------------------------


def test_the_page_paginates_at_the_named_size(alice):
    other = create_member(localname="other", password="s3cretpass")
    film = Film.objects.create(title="Dune", year=2021)
    status = Status.objects.create(
        user=alice, film=film, status_type=Status.Type.REVIEW, content="r"
    )
    for _ in range(NOTIFICATIONS_PAGE_SIZE + 1):
        notify(alice, other, Notification.Kind.LIKE, status)
    client = Client()
    client.force_login(alice)
    first = client.get("/notifications/").content.decode()
    assert first.count("applauded <a") == NOTIFICATIONS_PAGE_SIZE
    assert "Page 1 of 2" in first
    second = client.get("/notifications/?page=2").content.decode()
    assert second.count("applauded <a") == 1
    assert "Page 2 of 2" in second


def test_a_non_numeric_page_falls_back_to_the_first_page(alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    client = Client()
    client.force_login(alice)
    assert client.get("/notifications/?page=notanumber").status_code == 200


def test_an_empty_page_number_404s(alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    client = Client()
    client.force_login(alice)
    assert client.get("/notifications/?page=99").status_code == 404


# --- Empty state -------------------------------------------------------------


def test_a_member_with_nothing_sees_no_empty_state_copy(member):
    content = member.get("/notifications/").content.decode()
    assert "No notifications yet" not in content
    assert "Mark all read" not in content
    assert '<ul class="review-list">' not in content


def test_the_page_url_resolves_by_name():
    assert reverse("notifications") == "/notifications/"
    assert reverse("notifications-mark-read") == "/notifications/read/"


# --- Unseen display (M3, owner ruling D6) ------------------------------------


def _set_marker(user, when):
    """Move the member's mark-read marker without touching the ledger."""
    User.objects.filter(pk=user.pk).update(notifications_last_read=when)
    user.refresh_from_db()
    return user


def _move(note, when):
    """Give one row an exact ``created`` (the marker boundary needs precision)."""
    Notification.objects.filter(pk=note.pk).update(created=when)
    return note


def test_only_rows_after_the_mark_read_marker_render_unseen(member, alice, bob, post):
    marker = timezone.now()
    _set_marker(alice, marker)
    _move(notify(alice, bob, Notification.Kind.FOLLOW), marker - timedelta(minutes=5))
    _move(
        notify(alice, bob, Notification.Kind.LIKE, post),
        marker + timedelta(minutes=5),
    )
    content = member.get("/notifications/").content.decode()
    assert content.count('class="review review--unseen"') == 1
    # Control on the other half: the seen row keeps the plain class, so the
    # flag cannot be a class the template wears on every row.
    assert content.count('<li class="review">') == 1


def test_a_row_exactly_at_the_marker_is_not_unseen(member, alice, bob, post):
    # The boundary the bell already counts with: unread is ``created >``
    # marker, not ``>=``. A page that lit the row the marker itself covers
    # would disagree with the count beside the mark-all-read button.
    marker = timezone.now()
    _set_marker(alice, marker)
    _move(notify(alice, bob, Notification.Kind.LIKE, post), marker)
    content = member.get("/notifications/").content.decode()
    assert "review--unseen" not in content
    assert content.count('<li class="review">') == 1


def test_mark_all_read_flips_the_unseen_rows(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    notify(alice, bob, Notification.Kind.REPLY, post)
    assert (
        member.get("/notifications/")
        .content.decode()
        .count('class="review review--unseen"')
        == 2
    )
    member.post("/notifications/read/")
    content = member.get("/notifications/").content.decode()
    assert "review--unseen" not in content
    # Read is a timestamp, not a delete: the rows are all still listed.
    assert content.count('<li class="review">') == 2


def test_the_unseen_rows_are_the_rows_the_badge_counts(member, alice, bob, post):
    # One definition, three readers (R93): the amber rule and the bell must
    # never disagree about what a member has not seen yet.
    marker = timezone.now() - timedelta(hours=1)
    _set_marker(alice, marker)
    for hours in (-3, -1, 2, 5):
        _move(
            notify(alice, bob, Notification.Kind.LIKE, post),
            marker + timedelta(hours=hours),
        )
    content = member.get("/notifications/").content.decode()
    unread = Notification.unread_for(alice).count()
    assert unread == 2
    assert content.count('class="review review--unseen"') == unread


def test_the_stylesheet_lit_the_unseen_row():
    """The unseen treatment is in the applied sheet, not only in the markup.

    The design itself is reviewed in a browser (D6); this pin only keeps the
    three declarations from being read as dead CSS and tidied away. Whitespace
    is normalised around the structural characters, never stripped whole — that
    would glue a descendant selector into a compound one.
    """
    css = Path(finders.find("css/reeltalk.css")).read_text()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    flat = re.sub(r"\s+", " ", css)
    flat = re.sub(r"\s*([{};:])\s*", r"\1", flat)
    # The rule is on every ledger row, transparent until lit, so an unseen row
    # cannot shove its own text sideways.
    assert ".notifications-feed .review{border-left:3px solid transparent;" in flat
    assert (
        ".notifications-feed .review--unseen{border-left-color:var(--amber);}" in flat
    )
    assert (
        ".notifications-feed .review--unseen .review-head{color:var(--white);}" in flat
    )


# --- Copy that names the film (M3, owner ruling D7) --------------------------


def test_a_like_names_the_film_the_post_is_about(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    content = member.get("/notifications/").content.decode()
    assert 'about <span class="notif-film">Dune (2021)</span>' in content
    # The link stays on the post: the film is context, not a second door to
    # somewhere else.
    assert f'applauded <a href="/status/{post.id}/">your post</a>' in content
    assert f'href="/film/{post.film_id}/"' not in content


def test_a_film_with_no_year_is_named_without_parentheses(member, alice, bob):
    film = Film.objects.create(title="Nosferatu", year=None)
    status = Status.objects.create(
        user=alice, film=film, status_type=Status.Type.REVIEW, content="c"
    )
    notify(alice, bob, Notification.Kind.LIKE, status)
    content = member.get("/notifications/").content.decode()
    assert '<span class="notif-film">Nosferatu</span>' in content


def test_a_film_less_post_keeps_the_film_free_wording(member, alice, bob):
    # The fallback D7 keeps: a status with no film has nothing to name, so the
    # row reads exactly as it did before the copy changed.
    status = Status.objects.create(
        user=alice, status_type=None, content="<p>A note</p>", raw_content="A note"
    )
    notify(alice, bob, Notification.Kind.LIKE, status)
    content = member.get("/notifications/").content.decode()
    assert "notif-film" not in content
    assert f'applauded <a href="/status/{status.id}/">your post</a>' in content


def test_a_deleted_status_loses_the_film_label_with_its_link(member, alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    assert "notif-film" in member.get("/notifications/").content.decode()
    post.delete()
    content = member.get("/notifications/").content.decode()
    assert "notif-film" not in content
    assert "applauded a post that is no longer here" in content


def test_a_reply_names_the_film_and_shows_what_arrived(member, alice, bob, dune, post):
    reply = Status.objects.create(
        user=bob,
        film=dune,
        status_type=Status.Type.COMMENT,
        content="<p>The second act loses me.</p>",
        raw_content="The second act loses me.",
        reply_parent=post,
    )
    notify(alice, bob, Notification.Kind.REPLY, reply)
    content = member.get("/notifications/").content.decode()
    assert 'about <span class="notif-film">Dune (2021)</span>' in content
    # The reply's own words are the link, and they still open the reply.
    assert f'href="/status/{reply.id}/">The second act loses me.</a>' in content


def test_a_reply_with_no_text_of_its_own_keeps_the_plain_label(
    member, alice, bob, dune, post
):
    reply = Status.objects.create(
        user=bob,
        film=dune,
        status_type=Status.Type.COMMENT,
        content="",
        raw_content="",
        reply_parent=post,
    )
    notify(alice, bob, Notification.Kind.REPLY, reply)
    content = member.get("/notifications/").content.decode()
    assert f'href="/status/{reply.id}/">their reply</a>' in content


def test_a_mention_names_the_film_and_shows_what_arrived(member, alice, bob, dune):
    their_post = Status.objects.create(
        user=bob,
        film=dune,
        status_type=Status.Type.COMMENT,
        content="<p>You have to see this one.</p>",
        raw_content="You have to see this one.",
    )
    notify(alice, bob, Notification.Kind.MENTION, their_post)
    content = member.get("/notifications/").content.decode()
    assert "mentioned you in their post" in content
    assert 'about <span class="notif-film">Dune (2021)</span>' in content
    assert f'href="/status/{their_post.id}/">You have to see this one.</a>' in content


def test_the_excerpt_sits_on_one_line_whatever_the_source_wore(
    member, alice, bob, dune, post
):
    reply = Status.objects.create(
        user=bob,
        film=dune,
        status_type=Status.Type.COMMENT,
        content="<p>Line one</p><p>Line two</p>",
        raw_content="Line one\n\nLine two",
        reply_parent=post,
    )
    notify(alice, bob, Notification.Kind.REPLY, reply)
    content = member.get("/notifications/").content.decode()
    assert f'href="/status/{reply.id}/">Line one Line two</a>' in content


def test_the_excerpt_is_cut_at_the_cap_on_a_word_boundary(
    member, alice, bob, dune, post
):
    words = [f"word{n:02d}" for n in range(40)]
    reply = Status.objects.create(
        user=bob,
        film=dune,
        status_type=Status.Type.COMMENT,
        content="",
        raw_content=" ".join(words),
        reply_parent=post,
    )
    notify(alice, bob, Notification.Kind.REPLY, reply)
    content = member.get("/notifications/").content.decode()
    excerpt = re.search(r'href="/status/\d+/">([^<]+)</a>', content).group(1)
    assert excerpt.endswith("...")
    assert len(excerpt) <= NOTIFICATION_EXCERPT_CHARS + len("...")
    # The cut lands between words: the last whole word inside the cap is in,
    # the first one past it is out.
    assert "word19" in excerpt
    assert "word20" not in excerpt
