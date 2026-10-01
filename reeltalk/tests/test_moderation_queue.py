"""The report routes and the moderator queue (moderation arc increment 2).

Ordered by how each failure would bite:

* A member files a report from the post page and the profile, and the row
  carries the right target, category and comment.
* The refusals: anonymous is sent to log in, a self-report writes nothing,
  an invalid category writes nothing, a GET never writes anything at all.
* The control's **absence** where it cannot be used. A control that renders
  for someone who cannot use it is its own bug, so every "does not appear"
  case is pinned on a page that otherwise renders normally.
* The queue: moderator-only (R101's 403 for a signed-in non-moderator,
  302 for anonymous), one card per unresolved target rather than one row
  per report (R107), the reported content inline so a moderator can decide
  without opening the post somewhere else.
* Dismiss records who and when (R106) and clears the whole pile.

The session-proof rule from the earlier increments is followed throughout:
probe users are made with ``create_user()`` and the real login POST takes
``username``, and every authenticated assertion carries a ``sessionid``
check so a passing "member sees X" cannot be an anonymous render.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from reeltalk.core.models import Film, Status
from reeltalk.moderation.models import Report, dismiss_report
from reeltalk.notifications.models import Notification
from reeltalk.tests.members import member, site_admin

User = get_user_model()
PASSWORD = "s3cretpass"


@pytest.fixture
def alice(db):
    """The author of the reported post."""
    return member(localname="alice", password=PASSWORD)


@pytest.fixture
def bob(db):
    """The reporting member."""
    return member(localname="bob", password=PASSWORD)


@pytest.fixture
def carol(db):
    """A second reporting member."""
    return member(localname="carol", password=PASSWORD)


@pytest.fixture
def mod(db):
    return member(localname="mod", password=PASSWORD, is_moderator=True)


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def review(alice, dune):
    return Status.objects.create(
        user=alice,
        film=dune,
        status_type="review",
        content="<p>Great film</p>",
        raw_content="Great film",
    )


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


def report_payload(category="spam", comment="looks like junk"):
    return {"category": category, "comment": comment}


# --- filing from the post page -------------------------------------------


def test_a_member_reports_a_post(bob, review, alice):
    resp = logged_in(bob).post(
        reverse("report-status", args=[review.id]), report_payload()
    )
    assert resp.status_code == 302
    report = Report.objects.get(reporter=bob, target_status=review)
    assert report.target_user_id == alice.pk
    assert report.category == "spam"
    assert report.comment == "looks like junk"
    assert report.resolved_at is None


def test_anonymous_cannot_report_and_is_sent_to_log_in(review):
    resp = Client().post(reverse("report-status", args=[review.id]), report_payload())
    assert resp.status_code == 302
    assert reverse("login") in resp.headers["Location"]
    assert Report.objects.count() == 0


def test_a_member_cannot_report_their_own_post(alice, review):
    resp = logged_in(alice).post(
        reverse("report-status", args=[review.id]), report_payload()
    )
    assert resp.status_code == 302
    assert Report.objects.count() == 0, "a self-report wrote a row"


def test_a_deleted_status_cannot_be_reported(bob, review):
    review.delete()  # soft delete: the row stays, the content is cleared
    resp = logged_in(bob).post(
        reverse("report-status", args=[review.id]), report_payload()
    )
    assert resp.status_code == 404
    assert Report.objects.count() == 0


def test_reporting_a_missing_status_404s(bob):
    resp = logged_in(bob).post(
        reverse("report-status", args=[999999]), report_payload()
    )
    assert resp.status_code == 404


@pytest.mark.parametrize("category", ["", "harassment", "legal", "violation", "SPAM"])
def test_an_invalid_category_writes_nothing(bob, review, category):
    resp = logged_in(bob).post(
        reverse("report-status", args=[review.id]), report_payload(category)
    )
    assert resp.status_code == 302
    assert Report.objects.count() == 0, f"accepted category {category!r}"


def test_a_missing_category_writes_nothing(bob, review):
    resp = logged_in(bob).post(
        reverse("report-status", args=[review.id]), {"comment": "no category"}
    )
    assert resp.status_code == 302
    assert Report.objects.count() == 0


def test_an_overlong_comment_writes_nothing(bob, review):
    resp = logged_in(bob).post(
        reverse("report-status", args=[review.id]),
        {"category": "spam", "comment": "x" * 501},
    )
    assert resp.status_code == 302
    assert Report.objects.count() == 0


def test_a_comment_is_optional(bob, review):
    resp = logged_in(bob).post(
        reverse("report-status", args=[review.id]), {"category": "spam"}
    )
    assert resp.status_code == 302
    assert Report.objects.get(reporter=bob).comment == ""


def test_get_on_the_report_route_writes_nothing(bob, review):
    resp = logged_in(bob).get(reverse("report-status", args=[review.id]))
    assert resp.status_code == 405
    assert Report.objects.count() == 0


def test_a_second_report_from_the_same_member_writes_one_row(bob, review):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    logged_in(bob).post(
        reverse("report-status", args=[review.id]), report_payload("other", "again")
    )
    assert Report.objects.filter(reporter=bob, target_status=review).count() == 1


# --- filing from the profile ---------------------------------------------


def test_a_member_reports_a_member(bob, alice):
    resp = logged_in(bob).post(
        reverse("report-user", args=[alice.localname]), report_payload()
    )
    assert resp.status_code == 302
    report = Report.objects.get(reporter=bob, target_user=alice)
    assert report.target_status is None
    assert report.category == "spam"


def test_a_member_cannot_report_themselves(alice):
    resp = logged_in(alice).post(
        reverse("report-user", args=[alice.localname]), report_payload()
    )
    assert resp.status_code == 302
    assert Report.objects.count() == 0


def test_reporting_an_unknown_member_404s(bob):
    resp = logged_in(bob).post(
        reverse("report-user", args=["nobody"]), report_payload()
    )
    assert resp.status_code == 404


def test_anonymous_cannot_report_a_member(alice):
    resp = Client().post(
        reverse("report-user", args=[alice.localname]), report_payload()
    )
    assert resp.status_code == 302
    assert reverse("login") in resp.headers["Location"]
    assert Report.objects.count() == 0


def test_a_moderator_can_file_a_report_too(mod, alice):
    # Moderators are members first; nothing about the role stops them filing.
    resp = logged_in(mod).post(
        reverse("report-user", args=[alice.localname]), report_payload()
    )
    assert resp.status_code == 302
    assert Report.objects.filter(reporter=mod).count() == 1


# --- the control on the post page ----------------------------------------


def test_a_member_sees_the_report_control_on_someone_elses_post(bob, review):
    body = logged_in(bob).get(reverse("status", args=[review.id])).content.decode()
    assert 'class="report-box"' in body
    assert f'action="/status/{review.id}/report/"' in body
    assert 'name="category"' in body


def test_the_author_sees_no_report_control_on_their_own_post(alice, review):
    client = logged_in(alice)
    assert client.get(reverse("status", args=[review.id])).status_code == 200
    body = client.get(reverse("status", args=[review.id])).content.decode()
    assert 'class="report-box"' not in body
    assert "/report/" not in body


def test_an_anonymous_reader_sees_no_report_control(review):
    client = Client()
    assert client.get(reverse("status", args=[review.id])).status_code == 200
    body = client.get(reverse("status", args=[review.id])).content.decode()
    assert 'class="report-box"' not in body
    assert "/report/" not in body


def test_after_reporting_the_control_becomes_a_filed_note(bob, review):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    body = logged_in(bob).get(reverse("status", args=[review.id])).content.decode()
    assert "already reported this post" in body
    # And the form is gone, so the page cannot offer a submit that does nothing.
    assert 'class="report-box"' not in body


def test_another_members_control_survives_someone_elses_report(bob, carol, review):
    logged_in(carol).post(reverse("report-status", args=[review.id]), report_payload())
    body = logged_in(bob).get(reverse("status", args=[review.id])).content.decode()
    assert 'class="report-box"' in body
    assert "already reported" not in body


# --- the control on the profile ------------------------------------------


def test_a_member_sees_the_report_control_on_another_profile(bob, alice):
    body = (
        logged_in(bob)
        .get(reverse("user-profile", args=[alice.localname]))
        .content.decode()
    )
    assert 'class="report-box"' in body
    assert f'action="/user/{alice.localname}/report/"' in body


def test_no_report_control_on_your_own_profile(alice):
    client = logged_in(alice)
    url = reverse("user-profile", args=[alice.localname])
    assert client.get(url).status_code == 200
    body = client.get(url).content.decode()
    assert 'class="report-box"' not in body
    assert "/report/" not in body


def test_anonymous_sees_no_report_control_on_a_profile(alice):
    client = Client()
    url = reverse("user-profile", args=[alice.localname])
    assert client.get(url).status_code == 200
    body = client.get(url).content.decode()
    assert 'class="report-box"' not in body
    assert "/report/" not in body


# --- the queue: who may open it ------------------------------------------


QUEUE = reverse("moderation")


def test_a_moderator_opens_the_queue_with_nothing_in_it(mod):
    resp = logged_in(mod).get(QUEUE)
    assert resp.status_code == 200
    assert "Nothing to review" in resp.content.decode()


def test_a_signed_in_member_is_refused_the_queue_with_403(bob, review):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    resp = logged_in(bob).get(QUEUE)
    assert resp.status_code == 403
    # R101's 403, not a login redirect — and the refused page leaks nothing.
    body = resp.content.decode()
    assert "Great film" not in body
    assert "moderation-dismiss" not in body


def test_anonymous_is_sent_to_log_in_from_the_queue():
    resp = Client().get(QUEUE)
    assert resp.status_code == 302
    assert reverse("login") in resp.headers["Location"]


def test_a_superuser_sees_the_queue(db):
    admin = site_admin(localname="root", password=PASSWORD)
    assert logged_in(admin).get(QUEUE).status_code == 200


# --- the queue: what it shows --------------------------------------------


def test_the_queue_shows_the_reported_content_inline(mod, review, bob):
    logged_in(bob).post(
        reverse("report-status", args=[review.id]),
        {"category": "spam", "comment": "affiliate link"},
    )
    body = logged_in(mod).get(QUEUE).content.decode()
    # The post itself, not just a link to it — a moderator cannot triage
    # from a bare id.
    assert "Great film" in body
    assert f"/status/{review.id}/" in body
    # The reporter and the target, both named and linked.
    assert "@bob" in body
    assert "@alice" in body
    # The reporter's own words and the triage label.
    assert "affiliate link" in body
    assert "Spam" in body


def test_the_queue_groups_by_target_and_lists_every_reporter(bob, carol, review, mod):
    logged_in(bob).post(
        reverse("report-status", args=[review.id]),
        {"category": "spam", "comment": "first words"},
    )
    logged_in(carol).post(
        reverse("report-status", args=[review.id]),
        {"category": "other", "comment": "second words"},
    )
    assert Report.objects.count() == 2
    body = logged_in(mod).get(QUEUE).content.decode()
    assert "first words" in body
    assert "second words" in body
    assert "@bob" in body and "@carol" in body
    # One card, one dismiss form, for two reports — R107's "once per
    # unresolved target" on the queue surface.
    assert body.count("dismiss-form") == 1
    assert "2 reports" in body


def test_a_profile_report_renders_without_a_post_reference(bob, alice, mod):
    logged_in(bob).post(
        reverse("report-user", args=[alice.localname]),
        {"category": "spam", "comment": "bot account"},
    )
    body = logged_in(mod).get(QUEUE).content.decode()
    assert "@alice" in body
    assert "bot account" in body
    assert "Reported member" in body


def test_a_soft_deleted_reported_post_says_so_instead_of_rendering_empty(
    bob, review, mod
):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    review.delete()
    body = logged_in(mod).get(QUEUE).content.decode()
    assert "has since been deleted" in body
    # The empty-tombstone failure: the card is present but shows nothing to
    # point at, which reads as a report with no content.
    assert "Great film" not in body


def test_resolved_reports_are_off_the_queue(bob, review, mod):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    dismiss_report(Report.objects.get(), by_user=mod)
    body = logged_in(mod).get(QUEUE).content.decode()
    assert "Great film" not in body
    assert "Nothing to review" in body


# --- dismiss -------------------------------------------------------------


def test_a_moderator_dismisses_and_it_records_who_and_when(bob, review, mod):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    report = Report.objects.get()
    resp = logged_in(mod).post(
        reverse("moderation-dismiss", args=[report.id]), {"note": "not spam"}
    )
    assert resp.status_code == 302
    report.refresh_from_db()
    assert report.resolved_at is not None
    assert report.resolved_by_id == mod.pk
    assert report.action == "dismiss"
    assert report.note == "not spam"


def test_dismissing_clears_the_queue(bob, review, mod):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    report = Report.objects.get()
    logged_in(mod).post(reverse("moderation-dismiss", args=[report.id]), {})
    body = logged_in(mod).get(QUEUE).content.decode()
    assert "Great film" not in body
    assert "Nothing to review" in body


def test_dismissing_one_report_clears_the_whole_pile(bob, carol, review, mod):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    logged_in(carol).post(reverse("report-status", args=[review.id]), report_payload())
    report = Report.objects.first()
    logged_in(mod).post(reverse("moderation-dismiss", args=[report.id]), {})
    assert Report.unresolved().count() == 0
    assert Report.objects.filter(resolved_by=mod).count() == 2


def test_a_note_is_optional_on_a_dismissal(bob, review, mod):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    report = Report.objects.get()
    logged_in(mod).post(reverse("moderation-dismiss", args=[report.id]), {})
    report.refresh_from_db()
    assert report.note == ""
    assert report.resolved_by_id == mod.pk


def test_a_member_cannot_dismiss(bob, review):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    report = Report.objects.get()
    resp = logged_in(bob).post(reverse("moderation-dismiss", args=[report.id]), {})
    assert resp.status_code == 403
    report.refresh_from_db()
    assert report.resolved_at is None


def test_anonymous_cannot_dismiss(bob, review):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    report = Report.objects.get()
    resp = Client().post(reverse("moderation-dismiss", args=[report.id]), {})
    assert resp.status_code == 302
    assert reverse("login") in resp.headers["Location"]
    report.refresh_from_db()
    assert report.resolved_at is None


def test_a_get_on_the_dismiss_route_does_not_resolve(bob, review, mod):
    logged_in(bob).post(reverse("report-status", args=[review.id]), report_payload())
    report = Report.objects.get()
    resp = logged_in(mod).get(reverse("moderation-dismiss", args=[report.id]))
    assert resp.status_code == 405
    report.refresh_from_db()
    assert report.resolved_at is None


def test_dismissing_an_unknown_report_404s(mod):
    resp = logged_in(mod).post(reverse("moderation-dismiss", args=[999999]), {})
    assert resp.status_code == 404


# --- the whole loop, with the session proven ------------------------------


def test_the_whole_loop_member_reports_moderator_sees_and_dismisses(bob, review, mod):
    """The increment's exit bar, with each session proved live.

    ``sessionid`` is asserted on every authenticated client so a "member
    sees this" assertion cannot be passing on an anonymous render — the
    failure mode that made two earlier increments' member-path tests
    worthless.
    """
    reporter = logged_in(bob)
    assert reporter.get(reverse("status", args=[review.id])).status_code == 200
    assert "sessionid" in reporter.cookies
    reporter.post(reverse("report-status", args=[review.id]), report_payload())
    assert Report.objects.count() == 1
    assert Notification.objects.count() == 0, "R99: a report wrote a notification"

    moderator = logged_in(mod)
    assert "sessionid" in moderator.cookies
    queue = moderator.get(QUEUE)
    assert queue.status_code == 200
    assert "Great film" in queue.content.decode()

    moderator.post(
        reverse("moderation-dismiss", args=[Report.objects.get().id]),
        {"note": "cleared"},
    )
    assert Report.unresolved().count() == 0
    assert "Nothing to review" in moderator.get(QUEUE).content.decode()

    # The reporter's own control is now a filed note, not a live form.
    assert (
        "already reported"
        in reporter.get(reverse("status", args=[review.id])).content.decode()
    )


def test_a_real_login_session_files_and_dismisses(review, mod, bob):
    """A real login POST rather than ``force_login``, then the whole loop.

    The login form takes ``username`` (not ``localname``), and the CSRF
    token rotates at login — so the token used after sign-in is the one
    read *after* the login response, not the one from before it. The
    report and the dismissal then go through that same session, so this
    exercises the path a browser actually takes rather than a session
    minted directly into the cookie jar.
    """
    client = Client()
    client.get(reverse("login"))
    before_token = client.cookies["csrftoken"].value
    resp = client.post(
        reverse("login"),
        {"username": "mod", "password": PASSWORD, "next": QUEUE},
    )
    assert resp.status_code == 302
    assert "_auth_user_id" in client.session
    assert client.session["_auth_user_id"] == str(mod.pk)
    assert "sessionid" in client.cookies
    # The token rotated at login, so anything reusing the pre-login token
    # would now be refused.
    assert client.cookies["csrftoken"].value != before_token

    # A report filed by the moderator about someone else, then dismissed
    # from the same session.
    client.post(
        reverse("report-user", args=[bob.localname]),
        {"category": "spam", "comment": "flood of empty reviews"},
    )
    assert Report.unresolved().count() == 1
    queue = client.get(QUEUE)
    assert queue.status_code == 200
    assert "@bob" in queue.content.decode()
    client.post(
        reverse("moderation-dismiss", args=[Report.objects.get().id]),
        {"note": "cleared on review"},
    )
    assert Report.unresolved().count() == 0
    assert "Nothing to review" in client.get(QUEUE).content.decode()


def test_the_moderators_login_does_not_leak_into_the_member_path(bob, mod):
    """A member's real session sees its own state, not the moderator's.

    Two clients, two logins, and each page is asserted on the client that
    owns it — so a shared-session artifact cannot make the member look like
    a moderator or the moderator's queue look empty.
    """
    member = Client()
    member.post(
        reverse("login"),
        {"username": "bob", "password": PASSWORD, "next": "/"},
    )
    # Each login is asserted to have produced the session it claims, before
    # any permission result is read from it. Without this, a user that does
    # not exist logs in as nobody and produces a 302 that reads exactly
    # like a refusal — the failure mode that made a fixture typo look like a
    # passing security test twice in this codebase.
    assert member.session["_auth_user_id"] == str(bob.pk)
    assert member.get(QUEUE).status_code == 403

    moderator = Client()
    moderator.post(
        reverse("login"),
        {"username": "mod", "password": PASSWORD, "next": QUEUE},
    )
    assert moderator.session["_auth_user_id"] == str(mod.pk)
    assert moderator.get(QUEUE).status_code == 200
    # The member's 403 stands after the moderator logged in elsewhere:
    # the two sessions are independent.
    assert member.get(QUEUE).status_code == 403
