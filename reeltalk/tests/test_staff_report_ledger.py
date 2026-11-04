"""The staff ledger path: a report raises an in-app alert (M2, D3-D5).

The mail path's twin. Every test here exists to pin one clause of the owner's
D3-D5 rulings or one face of the failure mode the plan names - **the
censorship primitive**: if any half of this path ever consulted
``recipient.blocks`` or ran through ``notify()``, a member blocking every
moderator could mute the whole queue silently. The block-survival pins live
in ``test_reports.py`` and ``test_staff_report_email.py``; what this file
owns is the positive shape of the ledger half:

* **who gets a row** - every local account passing ``may_moderate``, the
  reporter excluded, and nobody else. The set is permission-derived, so the
  matrix spans all three ways in (moderator flag alone, staff alone, the
  composed admin): replace the derivation with any flag list or fixture list
  and one cell of the matrix goes red.
* **how many** - one row per staff account per unresolved target pile (D4),
  the same dedup gate the mail send runs under, not a second one that can
  drift from it.
* **what the row says** - reporter, target, category, queue link (D5), and
  neither the reported content nor the reporter's comment: a pointer, not a
  mirror. The mail-only halves (address, ``report_email`` opt) do not apply.
* **that the existing readers pick it up for free** - the bell badge and
  mark-all-read are the same table, same index, same unread contract, pinned
  here rather than assumed.
"""

import pytest
from django.test import Client
from django.urls import reverse

from reeltalk.core.models import Film, Status
from reeltalk.moderation.models import Report, file_remote_report, file_report
from reeltalk.notifications.models import Notification
from reeltalk.social.models import User
from reeltalk.tests.members import member as create_member
from reeltalk.tests.members import site_admin as create_site_admin

PASSWORD = "s3cretpass"
BADGE_MARKER = 'class="notifications-link"'
NOTIFICATIONS_URL = reverse("notifications")
MARK_READ_URL = reverse("notifications-mark-read")
QUEUE_URL = reverse("moderation")


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def witness(db):
    """The member who files the reports."""
    return create_member(localname="witness", password=PASSWORD)


@pytest.fixture
def accused(db):
    return create_member(localname="accused", password=PASSWORD)


@pytest.fixture
def mod(db):
    """Flag-only moderator (R158): no staff, so the admin door stays shut."""
    return create_member(localname="warden", password=PASSWORD, is_moderator=True)


@pytest.fixture
def mod2(db):
    return create_member(localname="deputy", password=PASSWORD, is_moderator=True)


@pytest.fixture
def staff_only(db):
    """Staff alone - passes ``may_moderate`` on the staff clause."""
    return create_member(localname="clerk", password=PASSWORD, is_staff=True)


@pytest.fixture
def admin(db):
    """The composed site admin (R158): all three flags."""
    return create_site_admin(localname="chief", password=PASSWORD)


@pytest.fixture
def bystander(db):
    return create_member(localname="bystander", password=PASSWORD)


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def review(accused, dune):
    return Status.objects.create(
        user=accused,
        film=dune,
        status_type="review",
        content="<p>ZIGGURAT-POST-BODY</p>",
        raw_content="ZIGGURAT-POST-BODY",
    )


def make_peer(handle="reporter@peer.example"):
    """A remote mirror: the verified sender of an inbound Flag."""
    return User.objects.create_user(
        localname=handle,
        password=PASSWORD,
        local=False,
        actor_url=f"https://{handle.rsplit('@', 1)[-1]}/actors/{handle.split('@')[0]}",
    )


def file_against_status(reporter, status, comment="SECRET-REPORT-NOTE"):
    return file_report(
        reporter=reporter,
        target_status=status,
        category="spam",
        comment=comment,
    )


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


# --- who gets a row (D3: the recipient set) --------------------------------


def test_every_staff_shape_gets_a_row_and_nobody_else_does(
    witness, accused, review, mod, mod2, staff_only, admin, bystander
):
    """The permission-derived set, spread across all three ways in.

    This is the mutation check the plan demands: a derivation replaced by a
    moderator-flag list loses ``staff_only`` and ``admin``, a staff list
    loses the flag-only ``mod`` and ``mod2``, and a fixture list loses
    whichever account was not thought of. The accused and a plain member
    must never be addressed - the alert belongs to the queue's readers, not
    to the parties.
    """
    file_against_status(witness, review)
    rows = Notification.objects.filter(kind="report")
    assert {n.recipient_id for n in rows} == {mod.pk, mod2.pk, staff_only.pk, admin.pk}
    assert not Notification.objects.filter(
        recipient__in=[witness, accused, bystander]
    ).exists()


def test_the_reporter_is_excluded_from_their_own_alert(mod, mod2, review):
    """Mail trap 4, ledger edition: moderators are members and file reports.

    Without the exclusion a moderator who reports something gets notified
    about their own report - the same self-address the mail path refuses.
    """
    file_against_status(mod, review)
    rows = Notification.objects.filter(kind="report")
    assert {n.recipient_id for n in rows} == {mod2.pk}


def test_a_moderator_who_cannot_be_emailed_still_gets_the_row(review):
    """The address and ``report_email`` halves are mail-only (D3).

    A moderator with the opt off - and one with no address at all - still
    sees the bell light, so the in-app surface does not inherit the mail
    path's silent gaps.
    """
    quiet = create_member(
        localname="quiet", password=PASSWORD, is_moderator=True, report_email=False
    )
    no_address = User.objects.create_user(localname="noaddress", is_moderator=True)
    file_against_status(create_member(localname="filer", password=PASSWORD), review)
    assert (
        Notification.objects.filter(
            recipient__in=[quiet, no_address], kind="report"
        ).count()
        == 2
    )


# --- how many (D4: one alert per pile, shared gate) -------------------------


def test_three_reports_one_target_are_one_row_per_moderator(witness, mod, review, db):
    """D4 against the ledger: three reporters, one pile, one row each.

    The dedup is the mail path's gate, not a second one - ``file_report``
    calls ``notify_staff_of_report`` once and that one gate fronts both
    surfaces, so "one alert per pile" cannot mean two different numbers.
    """
    carol = create_member(localname="carol", password=PASSWORD)
    dave = create_member(localname="dave", password=PASSWORD)
    file_against_status(witness, review)
    file_against_status(carol, review)
    file_against_status(dave, review)
    assert Report.objects.count() == 3
    assert Notification.objects.filter(kind="report").count() == 1


def test_a_second_target_pile_alerts_again(witness, accused, review, mod, db):
    """The pile is the unit, so a second pile is a second alert.

    A profile report and a status report about the same person are
    different work (the ``target_key`` grouping ``dismiss_report`` uses),
    and each gets its own row.
    """
    file_against_status(witness, review)
    file_report(reporter=witness, target_user=accused, category="other")
    assert Notification.objects.filter(recipient=mod, kind="report").count() == 2


def test_the_row_points_at_the_report_it_announces(witness, review, mod):
    """The FK the D5 line renders from: target and category live on the pile."""
    report, _ = file_against_status(witness, review)
    row = Notification.objects.get(recipient=mod)
    assert row.kind == "report"
    assert row.actor_id == witness.pk
    assert row.status_id is None
    assert row.report_id == report.pk


# --- what the row says (D5: a pointer, not a mirror) ------------------------


def test_the_report_line_names_reporter_target_and_category(mod, witness, review):
    """The rendered D5 line, and the two things it must not contain.

    Reporter, target, category, queue link - and neither the reported
    post's text nor the reporter's comment. The moderator reads those in
    the queue in one click; copying them into the ledger widens the leak
    surface for nothing, same principle as the staff email.
    """
    file_against_status(witness, review)
    content = logged_in(mod).get(NOTIFICATIONS_URL).content.decode()
    assert "witness" in content
    assert "@accused" in content
    assert "Spam" in content
    assert f'href="{QUEUE_URL}"' in content
    assert "ZIGGURAT-POST-BODY" not in content
    assert "SECRET-REPORT-NOTE" not in content


def test_a_report_line_without_its_report_row_still_renders(mod, witness, review):
    """The SET_NULL fallback: the alert happened whatever became of the row.

    Reporter deletion CASCADEs the report away (and SET_NULLs the actor
    too); the ledger line must degrade to an honest "no longer here", not
    vanish and not raise.
    """
    file_against_status(witness, review)
    Report.objects.all().delete()
    content = logged_in(mod).get(NOTIFICATIONS_URL).content.decode()
    assert "filed a report that is no longer here" in content
    assert f'href="{QUEUE_URL}"' in content


def test_a_remote_reporter_renders_like_any_other_actor(mod, accused):
    """D5's follow-the-existing-style clause, pinned rather than assumed.

    Remote mirrors carry their home domain in the localname, so the actor
    link the page already draws renders a peer reporter the way every other
    page surface renders them. No third style.
    """
    peer = make_peer()
    file_remote_report(peer, target_user=accused, comment="stolen art")
    content = logged_in(mod).get(NOTIFICATIONS_URL).content.decode()
    assert peer.localname in content
    # The profile URL is built by reverse, not spelled: the localname's
    # "@" is percent-encoded in the rendered href, and the trailing slash
    # belongs; a hand-written literal would be testing the test.
    assert f'href="{reverse("user-profile", args=[peer.localname])}"' in content


# --- the existing readers pick it up for free (same table, same contract) ---


def test_the_badge_counts_a_report_row(mod, witness, review):
    """The plan's "pin it rather than assume it": the bell needs no change.

    Same table, same composite index, same ``unread_for`` count - so the
    badge lights for a report row exactly as it lights for a like.
    """
    file_against_status(witness, review)
    content = logged_in(mod).get(reverse("find-user")).content.decode()
    assert content.count(BADGE_MARKER) == 1
    assert "1 unread" in content


def test_mark_all_read_clears_report_rows_like_any_other(mod, witness, review):
    """R93's timestamp marker is kind-blind; report rows are no exception."""
    file_against_status(witness, review)
    assert Notification.unread_for(mod).count() == 1
    logged_in(mod).post(MARK_READ_URL)
    # The POST moved the marker on the request's own user instance; the
    # fixture object is stale until refreshed, and unread_for reads the
    # marker off the object it is handed.
    mod.refresh_from_db()
    assert Notification.unread_for(mod).count() == 0
    content = logged_in(mod).get(reverse("find-user")).content.decode()
    assert content.count(BADGE_MARKER) == 0


def test_a_report_row_lights_as_unseen_and_keeps_its_own_copy(mod, witness, review):
    """M3's row display reaches report rows without changing what they say.

    D6's unseen flag is a property of the ledger, not of a kind, so a fresh
    report row lights like a fresh like. D7's film copy deliberately does
    **not** reach the report branch: a report is about a person, and the
    queue is where the content is read.
    """
    file_against_status(witness, review)
    content = logged_in(mod).get(NOTIFICATIONS_URL).content.decode()
    assert content.count('class="review review--unseen"') == 1
    assert "notif-film" not in content
    assert f'href="{QUEUE_URL}"' in content


def test_a_report_row_sits_in_the_ledger_alongside_the_ordinary_kinds(
    mod, witness, review
):
    """The one-column claim of D3: staff see reports **in the same column**.

    A like and a report row render in one list, each with its own line -
    the report branch does not take over the page and the ordinary kinds'
    copy is untouched by it.
    """
    file_against_status(witness, review)
    Notification.objects.create(
        recipient=mod, actor=witness, kind=Notification.Kind.LIKE, status=review
    )
    accused_url = reverse("user-profile", args=["accused"])
    status_url = reverse("status", args=[review.id])
    content = logged_in(mod).get(NOTIFICATIONS_URL).content.decode()
    assert f'reported <a href="{accused_url}">@accused</a>' in content
    assert f'applauded <a href="{status_url}">your post</a>' in content
