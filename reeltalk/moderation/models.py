"""The report and its queue (moderation arc increment 2, R98/R106/R107).

A report is **work to be done**, not an event a person is owed, and that
distinction is the whole reason this table exists separately from the
notification ledger (R99). ``notify()`` refuses to deliver anything to a
recipient who has blocked the actor — a guard that is exactly right for a
person's own inbox and exactly wrong here, because it would let a member
mute the entire moderation queue by blocking every moderator. Nothing in
this module calls ``notify()``, and there is no ``Notification.Kind.REPORT``.
If a later session "unifies" the two for convenience, that guard becomes a
censorship primitive and nothing will announce it.

Two shapes are borrowed rather than invented:

* **Resolution is a timestamp, not a status column** (R93's economy, and
  Mastodon's ``action_taken_at``). ``unresolved`` is ``resolved_at IS
  NULL``, so there is no state column to drift out of step with the
  timestamp that actually means the thing.
* **The report row is the audit trail** (R106). No separate audit-log
  table: what was reported, who reported it, who acted, when, what they
  did and why they said they did it all live on this one row, so a decision
  is never unexplainable after the fact.
"""

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


class Report(models.Model):
    """One member's report about one status or one account (R98).

    ``target_status`` is nullable because a report can be about a *person*
    rather than a post. It is ``SET_NULL`` rather than ``CASCADE`` for the
    same reason the notification ledger makes its status nullable: **the
    event happened whatever later became of the post.** A report filed
    against a review that is subsequently deleted still documents that the
    review was reported, and the queue renders it with the content the
    tombstone no longer carries nowhere — so the reporter's own comment is
    what the moderator reads.
    """

    class Category(models.TextChoices):
        """Why the reporter is telling us.

        Two values, arrived at by elimination rather than by taste. The
        plan's out-of-scope list removes ``legal`` (and with it DMCA
        handling). Mastodon's ``violation`` category is only meaningful
        alongside ``rule_ids`` pointing at a published server-ruleset, and
        this instance has no ruleset to violate — a ``violation`` category
        here would be a value that cannot be checked against anything,
        which is the "kind with no producer" trap in a different clothes.
        ``spam`` and ``other`` are the two that mean something with no
        rules table behind them.
        """

        SPAM = "spam", "Spam"
        OTHER = "other", "Something else"

    class Action(models.TextChoices):
        """What the moderator did.

        Only ``dismiss`` and ``delete_status`` exist today because only
        those two are built — suspension is increment 4 and the ban is 5.
        Each joins with its producer rather than ahead of it, so no value in
        this enum names an action no code path can take. A future session
        reading this should add the value **with** the code that writes it,
        not in anticipation of it.
        """

        DISMISS = "dismiss", "Dismissed"
        DELETE_STATUS = "delete_status", "Deleted the post"

    # CASCADE both ways on the people. A report is a statement made by one
    # person about another; neither half survives the deletion of the
    # speaker or the subject, and unlike the notification ledger there is
    # no third party who still needs to read it — the queue is resolved and
    # gone.
    reporter = models.ForeignKey(
        "social.User",
        on_delete=models.CASCADE,
        related_name="reports_filed",
    )
    target_user = models.ForeignKey(
        "social.User",
        on_delete=models.CASCADE,
        related_name="reports_against",
    )
    target_status = models.ForeignKey(
        "core.Status",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports",
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    # The reporter's own words. Bounded well below Mastodon's 1,000-char
    # local cap because this is a note about one post on a small instance,
    # not a legal submission, and the queue renders it inline.
    comment = models.TextField(blank=True, default="")
    created = models.DateTimeField(default=timezone.now, db_index=True)
    # The whole resolved/unresolved model (R93): one column, and
    # ``unresolved`` is ``resolved_at IS NULL``. No status column, so
    # nothing can claim to be open while carrying a resolution time.
    resolved_at = models.DateTimeField(null=True, blank=True)
    # SET_NULL, not CASCADE: a moderator leaving the instance must not
    # retroactively un-resolve work they did, and must not erase the record
    # that someone acted. The row keeps "resolved on this date by a member
    # since deleted", which is still a complete audit entry.
    resolved_by = models.ForeignKey(
        "social.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports_resolved",
    )
    action = models.CharField(
        max_length=20, choices=Action.choices, blank=True, default=""
    )
    note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created"]
        constraints = [
            # R107's dedup made structural rather than a check: one report
            # per reporter per target, enforced by the database so it holds
            # under concurrent submits and cannot be bypassed by a caller
            # that forgot to look first.
            #
            # ``nulls_distinct=False`` is load-bearing, not decoration.
            # Postgres treats NULLs as distinct in a unique index by
            # default, so without it the trio dedups a *status* report but
            # silently allows a reporter to file report #2, #3, #4 against
            # the same *person* — every one of them with target_status
            # NULL, none of them colliding. That is the common case for a
            # profile report, which is exactly where R107 matters most.
            models.UniqueConstraint(
                fields=["reporter", "target_user", "target_status"],
                name="one_report_per_reporter_and_target",
                nulls_distinct=False,
            ),
            # The category is checked at the database, not only by the form.
            #
            # This landed because the mutation pass showed a model-level
            # ``clean()`` check turning zero tests red: Django's field-level
            # ``choices`` already covers ``full_clean()``, so the extra
            # check duplicated something that was already enforced one layer
            # up — and neither one of them runs on ``Model.objects.create()``,
            # which is exactly how increment 6's inbound ``Flag`` handler
            # will build these rows, from a peer's payload with no form and
            # no ``full_clean()`` in the path. A CHECK constraint is the
            # only layer in this stack that a raw ``create()`` cannot walk
            # past.
            #
            # Literals: Meta has its own namespace, so Category is not
            # visible here (same reason as Status's review-uniqueness index).
            models.CheckConstraint(
                condition=Q(category__in=["spam", "other"]),
                name="known_report_category",
            ),
        ]

    def __str__(self) -> str:
        target = (
            f"status {self.target_status_id}"
            if self.target_status_id
            else f"@{self.target_user.localname}"
        )
        state = "resolved" if self.resolved_at else "open"
        return f"{self.reporter.localname} reported {target} ({state})"

    @property
    def is_resolved(self) -> bool:
        return self.resolved_at is not None

    @property
    def target_key(self) -> tuple:
        """The identity a queue card is built on.

        R107's second half — staff are told **once per unresolved target**,
        not once per report — is a grouping key, and this is it. Two reports
        of the same status by two members share a key; a report of a person
        and a report of that person's post do not, because they are
        different work.
        """
        return (self.target_user_id, self.target_status_id)

    @classmethod
    def unresolved(cls):
        """Every open report, newest first.

        ``resolved_at__isnull=True`` rather than a status filter, because
        there is no status column (R93).
        """
        return cls.objects.filter(resolved_at__isnull=True)

    @classmethod
    def unresolved_for_target(cls, target_key):
        """The open reports sitting on one target — the pile a dismiss clears.

        A moderator acts on the *target* they were shown, not on the
        arbitrary report row that happened to head the card, so dismissing
        one of three reports about the same post resolves all three. That
        is what "once per unresolved target" means on a queue surface: the
        card is the unit of work, and leaving two siblings open under a
        card that has been dismissed would re-notify on the next page load.
        """
        target_user_id, target_status_id = target_key
        return cls.unresolved().filter(
            target_user_id=target_user_id, target_status_id=target_status_id
        )

    def clean(self):
        """A report's two target halves must agree.

        ``target_user`` is required by the schema, but the *status* variant
        must agree with it: a report naming a status whose author is someone
        else would put the accusation against the wrong person while the
        queue links to a post they did not write. Checked on the model
        rather than only in the view because increment 6 builds these rows
        from a peer's inbound ``Flag`` payload, where the declared target is
        not trustworthy.

        There is deliberately **no category check here.** Django's field-level
        ``choices`` already rejects an unknown or empty category under
        ``full_clean()``, and a second check here duplicated it — the
        mutation pass proved that by turning zero tests red when it was
        removed. The category guarantee that actually cannot be walked past
        is the ``known_report_category`` CHECK constraint above, because a
        CHECK runs on a raw ``create()`` and ``clean()`` does not.
        """
        if self.target_status_id and self.target_status.user_id != self.target_user_id:
            raise ValidationError(
                "A status report's target_user must be the status's author."
            )


def file_report(
    *, reporter, category, target_user=None, target_status=None, comment=""
):
    """Record one report, or return the open one that already covers it.

    The single door for a locally-filed report, mirroring ``notify()``: the
    dedup rule and the target-consistency rule are not the kind of thing
    every call site can be trusted to each remember, and a caller that
    forgets the dedup does not fail loudly — it just writes a duplicate.

    Returns ``(report, created)``. ``created`` is ``False`` when R107's
    dedup swallowed the submit, which is a normal outcome and not an error:
    the second click of a double-clicked button and a member who genuinely
    re-submits both land here.

    The dedup is deliberately **not** scoped to open reports. Mastodon's
    ``unresolved_siblings?`` asks whether the target is already being
    handled; here the database constraint asks whether *this reporter* has
    ever filed against *this target*, which is the stronger and simpler
    rule, and the one that survives a moderator having already dismissed
    the report once. A member cannot re-file the same complaint to push it
    back to the top of the queue.
    """
    if target_status is not None:
        target_user = target_status.user
    report, created = Report.objects.get_or_create(
        reporter=reporter,
        target_user=target_user,
        target_status=target_status,
        defaults={"category": category, "comment": comment},
    )
    return report, created


def report_state(user, *, target_user=None, target_status=None):
    """``(may_report, already_reported)`` for a host page's report control.

    One helper for both host pages — the post page and the profile — so the
    rule about who may report what is not restated in two templates. A
    template that computes its own gate drifts from the route's, and the
    drift shows up as a button that 403s or a missing button on a page
    where the action was always allowed.

    ``already_reported`` is what makes the control honest about R107's
    dedup: rather than offering a button that will quietly do nothing, the
    page says the report is already in. The route still dedups regardless
    — this is presentation, not enforcement.
    """
    if not user.is_authenticated:
        return False, False
    if target_status is not None:
        target_user = target_status.user
    if target_user is None:
        return False, False
    if user.pk == target_user.pk:
        return False, False
    filed = Report.objects.filter(
        reporter=user,
        target_user=target_user,
        target_status=target_status,
    ).exists()
    return True, filed


def dismiss_report(report, *, by_user, note=""):
    """Resolve a report's whole target pile as dismissed (R106/R107).

    Records **who** and **when** on every row in the pile, which is the
    whole of the audit requirement (R106) — there is no separate log table
    to write to, and the report row is the record.

    Returns the number of rows resolved, so a caller can tell the
    one-report card from the three-report pile rather than assuming.
    """
    now = timezone.now()
    return Report.unresolved_for_target(report.target_key).update(
        resolved_at=now, resolved_by=by_user, action=Report.Action.DISMISS, note=note
    )


def delete_reported_status(report, *, by_user, note=""):
    """Delete the reported post and resolve its whole pile as deleted (R106/R107).

    The mirror of :func:`dismiss_report` with a heavier first half. The
    post goes through ``Status.delete()`` — R17 soft-delete — which is
    already the filter at ~19 read sites, so the content vanishes from the
    feed, the film page, the user tabs, the outbox and the thread walk with
    **no new read-path code**. The row stays as a tombstone with its
    identity intact, which is what lets the report keep pointing at it and
    what keeps the ``Delete`` activity's wire identity stable — that id is
    built from ``note_local_id(status)``, which soft-delete preserves.

    The pile is resolved with the **same** target grouping as dismiss,
    because the card the moderator acted on is the unit of work: three
    members reporting one post is one deletion with three pieces of
    evidence, not three deletions.

    **What this deliberately does not do is decide who may delete.** The
    R103 guard lives in :func:`reeltalk.moderation.decorators.can_act_on`
    and is the caller's check, not this function's — one gate, one place,
    and a helper that silently refused would make a view's own guard
    untestable. Nor does it broadcast: the local write must commit before
    any federation runs, and whether a broadcast is owed at all depends on
    the author's locality, which is the caller's call. See
    ``views.delete_status`` for both.

    Returns ``(status, count)`` — the tombstoned status, and the number of
    report rows resolved. ``status`` is ``None`` when the report was about
    a member rather than a post, which is why the caller checks before
    reaching for a broadcast.
    """
    status = report.target_status
    if status is None:
        return None, 0
    now = timezone.now()
    # Atomic so the audit record and the deletion cannot come apart: a
    # report claiming a delete that did not happen is worse than no record,
    # and a post gone with its report still open would re-present the card
    # as an unresolved decision about content that is no longer here.
    with transaction.atomic():
        count = Report.unresolved_for_target(report.target_key).update(
            resolved_at=now,
            resolved_by=by_user,
            action=Report.Action.DELETE_STATUS,
            note=note,
        )
        status.delete()
    return status, count
