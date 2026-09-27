"""The ``Report`` model and its rules (moderation arc increment 2, R98/R106/R107).

Three of these tests exist because a rule that lives only in a view is a
rule a later view can forget. They are written at the model layer so the
guarantee holds for whatever writes a report next — including increment
6's inbound ``Flag`` handler, which builds these rows from a peer's payload
rather than from a form.

The one to read first is
``test_a_second_profile_report_is_blocked_even_though_its_status_is_null``.
It pins the trap that the shape note in §2D does not mention: Postgres
treats NULLs as distinct in a unique index by default, so the
``(reporter, target_user, target_status)`` constraint dedups a *status*
report for free and does **nothing at all** for a *profile* report — every
one of those has ``target_status`` NULL, and NULL never collides with NULL.
``nulls_distinct=False`` is what makes R107 mean anything on the profile
path, and without this test it would read like a decorative argument.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from reeltalk.core.models import Film, Status
from reeltalk.moderation.models import (
    Report,
    dismiss_report,
    file_report,
    report_state,
)
from reeltalk.notifications.models import Notification

User = get_user_model()


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return User.objects.create_user(localname="bob", password="s3cretpass")


@pytest.fixture
def carol(db):
    return User.objects.create_user(localname="carol", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def review(alice, dune):
    return Status.objects.create(
        user=alice,
        film=dune,
        status_type="review",
        content="<p>spam review</p>",
        raw_content="spam review",
    )


# --- filing --------------------------------------------------------------


def test_filing_a_report_about_a_status_derives_the_target_user(alice, review, bob):
    """The accusation must land on the person who wrote the thing.

    ``target_user`` is a required column and a caller could leave it pointing
    anywhere. ``file_report`` re-derives it from the status so the queue can
    never show "reported against Carol" over a post Bob wrote.
    """
    report, created = file_report(
        reporter=bob, target_status=review, category="spam", comment="junk"
    )
    assert created is True
    assert report.target_user_id == review.user_id == alice.pk
    assert report.target_status_id == review.pk
    assert report.category == "spam"
    assert report.comment == "junk"


def test_a_report_is_unresolved_until_something_resolves_it(review, bob):
    report, _ = file_report(reporter=bob, target_status=review, category="spam")
    assert report.resolved_at is None
    assert report.is_resolved is False
    assert report.resolved_by is None
    assert report.action == ""
    assert Report.unresolved().filter(pk=report.pk).exists()


def test_an_unknown_category_is_refused_at_the_database(alice, review):
    """The CHECK constraint — the layer a raw ``create()`` cannot pass.

    Three layers could enforce a category, and only this one is
    non-bypassable:

    * the form's ``ChoiceField`` — covers the web path and nothing else;
    * Django's field ``choices`` under ``full_clean()`` — covers anything
      that remembers to call it, which ``create()`` does not;
    * the ``known_report_category`` CHECK — runs on every INSERT, including
      the one increment 6 will issue from an inbound ``Flag`` payload with
      no form and no ``full_clean()`` anywhere in the path.

    A model-level ``clean()`` check was written first, and the mutation pass
    showed removing it turned **zero** tests red because the second bullet
    already covered it. This is the check that actually bites.
    """
    for bad in ("harassment", "legal", ""):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Report.objects.create(
                    reporter=alice, target_user=review.user, category=bad
                )
    assert Report.objects.count() == 0


def test_both_known_categories_write(alice, review):
    # Distinct (reporter, target) pairs, because the dedup constraint is
    # also live here — re-using one pair would fail on the unique index and
    # prove nothing about the category CHECK. target_user is passed
    # explicitly because only ``file_report`` derives it; a bare create()
    # does not.
    Report.objects.create(
        reporter=alice,
        target_user=review.user,
        target_status=review,
        category="spam",
    )
    Report.objects.create(reporter=alice, target_user=review.user, category="other")
    assert sorted(r.category for r in Report.objects.all()) == ["other", "spam"]


def test_full_clean_also_rejects_a_bad_category(alice, review):
    # Django's own field-level choices, asserted so that a later change to
    # the field (blank=True, choices dropped) shows up here rather than
    # silently weakening the web path.
    with pytest.raises(ValidationError):
        Report(
            reporter=alice, target_user=review.user, category="harassment"
        ).full_clean()
    with pytest.raises(ValidationError):
        Report(reporter=alice, target_user=review.user, category="").full_clean()


def test_a_known_category_passes_full_clean(alice, review):
    Report(reporter=alice, target_user=review.user, category="spam").full_clean()


def test_only_the_two_declared_categories_exist():
    # The set is spam + other by elimination (§2D removes `legal`, and
    # Mastodon's `violation` needs a rule_ids ruleset this instance does not
    # have). Pinned so a later session adding a third value does so as a
    # decision rather than a drift.
    assert list(Report.Category.values) == ["spam", "other"]


# --- R107's dedup --------------------------------------------------------


def test_a_second_report_of_the_same_status_is_not_created(review, bob):
    first, created_first = file_report(
        reporter=bob, target_status=review, category="spam", comment="first"
    )
    second, created_second = file_report(
        reporter=bob, target_status=review, category="other", comment="second"
    )
    assert created_first is True
    assert created_second is False
    # The original row is returned untouched — a re-submit does not
    # overwrite the first report's category or comment.
    assert second.pk == first.pk
    assert second.category == "spam"
    assert second.comment == "first"
    assert Report.objects.count() == 1


def test_a_second_profile_report_is_blocked_even_though_its_status_is_null(alice, bob):
    """THE null-dedup trap. Without ``nulls_distinct=False`` this fails.

    Every profile report has ``target_status = NULL``, and Postgres' default
    unique-index semantics treat NULL as distinct from NULL — so the trio
    would let Bob file report after report against Alice and none of them
    would collide. That is the common case for a profile report, which is
    precisely where R107 matters most.
    """
    first, created_first = file_report(reporter=bob, target_user=alice, category="spam")
    second, created_second = file_report(
        reporter=bob, target_user=alice, category="other"
    )
    assert first.target_status is None
    assert created_first is True
    assert created_second is False, (
        "R107 dedup did not fire on a NULL target_status — the unique "
        "constraint needs nulls_distinct=False."
    )
    assert Report.objects.filter(target_status__isnull=True).count() == 1


def test_the_constraint_is_enforced_at_the_database_not_only_in_the_helper(bob, alice):
    """``file_report`` is not the guarantee; the index is.

    A direct ``create()`` that bypasses the helper must still be refused, so
    the rule cannot be defeated by a caller that forgets to look first.
    """
    Report.objects.create(reporter=bob, target_user=alice, category="spam")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Report.objects.create(reporter=bob, target_user=alice, category="other")


def test_two_reporters_on_one_target_are_two_reports(alice, bob, carol, review):
    """Dedup is per **reporter**, not per target.

    Collapsing the pile into one row would lose who said what, and the queue
    shows each reporter's words. R107 groups them for display; it does not
    merge them in storage.
    """
    file_report(reporter=bob, target_status=review, category="spam")
    file_report(reporter=carol, target_status=review, category="other")
    assert Report.objects.filter(target_status=review).count() == 2


def test_one_reporter_can_report_a_person_and_that_persons_post_separately(
    alice, bob, review
):
    """Different targets, so not a duplicate.

    "This post is spam" and "this person is a spammer" are different work:
    one is answered by deleting a post, the other by acting on an account.
    ``target_key`` keeps them apart, and merging them would make the cheaper
    action invisible once the heavier report existed.
    """
    on_post, _ = file_report(reporter=bob, target_status=review, category="spam")
    on_person, created = file_report(reporter=bob, target_user=alice, category="spam")
    assert created is True
    assert on_post.target_key != on_person.target_key
    assert Report.objects.filter(reporter=bob).count() == 2


def test_a_resolved_report_is_not_re_fileable(alice, bob, review, carol):
    """Dedup is not scoped to open reports.

    Mastodon's ``unresolved_siblings?`` asks whether the target is already
    being handled; here the constraint asks whether this reporter has **ever**
    filed against this target. The stronger rule is the one that survives a
    moderator having already dismissed the report once — otherwise a member
    could re-file a dismissed complaint to push it back to the top of the
    queue.
    """
    report, _ = file_report(reporter=bob, target_status=review, category="spam")
    dismiss_report(report, by_user=carol)
    _, created = file_report(reporter=bob, target_status=review, category="spam")
    assert created is False
    assert Report.objects.filter(reporter=bob, target_status=review).count() == 1


# --- the target-consistency invariant ------------------------------------


def test_a_status_report_must_name_the_statuses_author(alice, bob, dune, review):
    """Bob cannot file a report "against Carol" over Alice's post.

    Checked in ``clean()`` rather than only in the view because increment 6
    builds these rows from a peer's ``Flag`` payload, where the declared
    target is not trustworthy.
    """
    carol = User.objects.create_user(localname="carol", password="s3cretpass")
    report = Report(
        reporter=bob,
        target_user=carol,
        target_status=review,
        category="spam",
    )
    with pytest.raises(ValidationError):
        report.full_clean()


# --- dismissal -----------------------------------------------------------


def test_dismiss_records_who_when_and_what(alice, bob, review, carol):
    report, _ = file_report(reporter=bob, target_status=review, category="spam")
    before = timezone.now()
    count = dismiss_report(report, by_user=carol, note="not a rule break")
    report.refresh_from_db()
    assert count == 1
    assert report.resolved_at is not None
    assert report.resolved_at >= before
    assert report.resolved_by_id == carol.pk
    assert report.action == Report.Action.DISMISS
    assert report.note == "not a rule break"
    assert report.is_resolved is True
    assert not Report.unresolved().filter(pk=report.pk).exists()


def test_dismissing_one_report_resolves_the_whole_target_pile(
    alice, bob, carol, review
):
    """R107 on a queue surface: the card is the unit of work.

    Leaving two siblings open under a card a moderator just dismissed would
    put the same card straight back on the queue, which is the
    re-notification the rule exists to prevent.
    """
    first, _ = file_report(reporter=bob, target_status=review, category="spam")
    file_report(reporter=carol, target_status=review, category="other")
    assert Report.unresolved_for_target(first.target_key).count() == 2
    count = dismiss_report(first, by_user=carol)
    assert count == 2
    assert Report.unresolved().count() == 0


def test_dismiss_does_not_touch_a_different_target(alice, bob, carol, dune, review):
    other = Status.objects.create(
        user=alice,
        film=dune,
        status_type="comment",
        content="<p>c</p>",
        raw_content="c",
    )
    mine, _ = file_report(reporter=bob, target_status=review, category="spam")
    theirs, _ = file_report(reporter=bob, target_status=other, category="other")
    dismiss_report(mine, by_user=carol)
    theirs.refresh_from_db()
    assert theirs.resolved_at is None
    assert Report.unresolved().count() == 1


def test_dismiss_is_idempotent_and_reports_zero_the_second_time(bob, review, carol):
    report, _ = file_report(reporter=bob, target_status=review, category="spam")
    assert dismiss_report(report, by_user=carol) == 1
    assert dismiss_report(report, by_user=carol) == 0


# --- the audit trail (R106) ----------------------------------------------


def test_the_report_row_is_the_whole_record(bob, review, carol):
    """What was reported, by whom, acted on by whom, when, and why.

    No separate audit table exists (R106), so every field a moderator would
    need to explain a decision later must be on this row. Asserted as one
    row so a change that moves any half elsewhere shows up here.
    """
    report, _ = file_report(
        reporter=bob, target_status=review, category="spam", comment="links to a scam"
    )
    dismiss_report(report, by_user=carol, note="clear spam")
    report.refresh_from_db()
    assert report.reporter_id == bob.pk
    assert report.target_user_id == review.user_id
    assert report.target_status_id == review.pk
    assert report.category == "spam"
    assert report.comment == "links to a scam"
    assert report.resolved_by_id == carol.pk
    assert report.action == "dismiss"
    assert report.note == "clear spam"
    assert Report.objects.count() == 1


# --- R99: a report is NOT a notification ---------------------------------


def test_filing_a_report_writes_no_notification(bob, alice, review):
    """R99's pin: reports never touch the notification ledger.

    The ledger's ``notify()`` refuses delivery to a recipient who blocked the
    actor. Route reports through it and a member could mute the entire
    moderation queue by blocking every moderator — the guard that protects
    a personal inbox becoming a way to hide abuse. Asserted by counting the
    whole table rather than filtering, so a stray write of any kind fails
    here.
    """
    file_report(reporter=bob, target_status=review, category="spam")
    file_report(reporter=bob, target_user=alice, category="spam")
    assert Notification.objects.count() == 0


def test_there_is_no_report_kind_on_the_notification_enum():
    assert "report" not in Notification.Kind.values
    assert set(Notification.Kind.values) == {"follow", "like", "reply", "mention"}


def test_a_report_survives_the_reporter_blocking_the_moderator(bob, carol, review):
    """The block guard must not reach the queue.

    Bob reports a post, then blocks the moderator. His report must still
    exist and still be unresolved — otherwise a reporter could withdraw a
    report by blocking staff, and the queue would be silently filterable by
    the person who filed it.
    """
    report, _ = file_report(reporter=bob, target_status=review, category="spam")
    bob.blocks.add(carol)
    report.refresh_from_db()
    assert report.resolved_at is None
    assert Report.unresolved().filter(pk=report.pk).exists()


# --- FK behaviour --------------------------------------------------------


def test_target_status_set_null_when_the_status_row_is_hard_deleted(bob, review):
    """The event happened whatever later became of the post.

    ``Status.delete()`` is soft, so this fires only on a hard delete — an
    admin changelist delete, which does go through queryset delete. The
    report must survive with its reporter and comment intact; losing the
    report would lose the record that anyone complained.
    """
    report, _ = file_report(reporter=bob, target_status=review, category="spam")
    Status.objects.filter(pk=review.pk).delete()
    report.refresh_from_db()
    assert report.target_status is None
    # The reporter, the target person and the reporter's own words all
    # survive — that is the record. Only the post reference goes.
    assert report.reporter_id == bob.pk
    assert report.target_user_id == review.user_id
    assert report.comment == ""
    assert report.category == "spam"
    assert Report.objects.filter(pk=report.pk).exists()


def test_reporter_deletion_removes_their_reports(bob, review):
    file_report(reporter=bob, target_status=review, category="spam")
    bob.delete()
    assert Report.objects.count() == 0


def test_target_user_deletion_removes_reports_against_them(alice, bob):
    """No ``review`` fixture here on purpose.

    ``Status.user`` is ``PROTECT``, so an author with a live review cannot be
    deleted at all — pulling that fixture in would test the PROTECT edge and
    never reach the report cascade it is meant to pin.
    """
    file_report(reporter=bob, target_user=alice, category="spam")
    alice.delete()
    assert Report.objects.count() == 0


def test_resolved_by_set_null_keeps_the_resolution_when_the_moderator_leaves(
    bob, review, carol
):
    report, _ = file_report(reporter=bob, target_status=review, category="spam")
    dismiss_report(report, by_user=carol)
    carol.delete()
    report.refresh_from_db()
    # Still resolved — a moderator leaving must not un-resolve finished work.
    assert report.resolved_at is not None
    assert report.resolved_by is None
    assert report.action == "dismiss"


# --- report_state --------------------------------------------------------


def test_report_state_denies_anonymous(alice, review):
    from django.contrib.auth.models import AnonymousUser

    assert report_state(AnonymousUser(), target_status=review) == (False, False)
    assert report_state(AnonymousUser(), target_user=alice) == (False, False)


def test_report_state_denies_reporting_yourself(alice):
    assert report_state(alice, target_user=alice) == (False, False)


def test_report_state_denies_reporting_your_own_post(alice, review):
    assert report_state(alice, target_status=review) == (False, False)


def test_report_state_allows_another_members_post(bob, review):
    assert report_state(bob, target_status=review) == (True, False)


def test_report_state_reports_the_dedup_state(bob, review):
    file_report(reporter=bob, target_status=review, category="spam")
    assert report_state(bob, target_status=review) == (True, True)


def test_report_state_keeps_a_post_report_and_a_profile_report_apart(
    bob, alice, review
):
    """Already having reported the *person* does not mark their *post* reported.

    The two are different work with different answers, so the control on the
    post must still be offered after a profile report and vice versa. A
    helper that matched on reporter + user alone would hide a live control.
    """
    file_report(reporter=bob, target_user=alice, category="spam")
    assert report_state(bob, target_status=review) == (True, False)
    file_report(reporter=bob, target_status=review, category="spam")
    assert report_state(bob, target_user=alice) == (True, True)


def test_a_resolved_report_still_counts_as_already_reported(bob, review, carol):
    """The control does not come back after a dismissal.

    R107's dedup is not scoped to open reports, so offering the button
    again would offer a submit that does nothing.
    """
    report, _ = file_report(reporter=bob, target_status=review, category="spam")
    dismiss_report(report, by_user=carol)
    assert report_state(bob, target_status=review) == (True, True)
