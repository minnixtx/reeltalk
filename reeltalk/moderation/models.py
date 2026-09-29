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

from reeltalk.core.models import Status
from reeltalk.social.models import SuspensionOrigin, User


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

        Only values with a live producer appear — ``suspend`` joined with
        the queue action that writes it in increment 4, and the ban is
        increment 5. Each value arrives with its producer rather than ahead
        of it, so no value in this enum names an action no code path can
        take. A future session should add the value **with** the code that
        writes it, not in anticipation of it.

        Note there is deliberately **no ``unsuspend`` value.** Unsuspend is
        not a response to a report — it is a separate act on the account,
        taken from the profile rather than the queue, and by then the pile
        that this row belongs to has long since been resolved. Recording it
        here would mean rewriting a closed decision rather than appending a
        new one. The gap that leaves (an unsuspend carries no persisted
        moderator note) is recorded in the increment's execution record
        rather than papered over by widening this enum past what the queue
        can actually produce.
        """

        DISMISS = "dismiss", "Dismissed"
        DELETE_STATUS = "delete_status", "Deleted the post"
        SUSPEND = "suspend", "Suspended the account"
        BAN = "ban", "Banned the account"

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


# Mastodon's own inbound cap on a forwarded report comment is 5,000 chars
# (``ActivityPub::Activity::Flag::COMMENT_SIZE_LIMIT``), against 1,000 for
# a local one — a peer may be relaying text it did not write and cannot
# shorten. This instance caps its own reporters at 500, so the inbound
# number is the only one that can be reached here, and it is worth taking
# from the peer rather than inventing: a moderator reading a five-thousand
# character note is a different job from reading a five-hundred one, and
# the queue renders it inline either way.
INBOUND_COMMENT_MAX = 5000


def file_remote_report(
    sender, *, target_user=None, target_status=None, comment=""
) -> tuple:
    """Record a peer's report about one of our members (R104).

    The inbound twin of :func:`file_report`, and a separate door rather
    than a call to it, because the two have different trust properties and
    a shared function would have to be written to the worse of them.
    Everything here arrives from an unauthenticated-in-spirit peer — the
    signature proves *who sent it* and nothing else. The caller has
    already resolved the target against our own rows rather than trusting
    the declared URIs, and the reporter is the **verified sender**, which
    is the only identity this path is allowed to record.

    **The category is fixed to** ``other``. The ``Flag`` wire carries no
    category at all — Mastodon's serializer emits ``id``, ``type``,
    ``actor``, ``content`` and ``object``, and nothing in that list says
    *why*. Inventing a default of ``spam`` would put a word in the
    reporting server's mouth that it never said, and the moderator would
    triage against a label with no author. ``other`` is the honest answer:
    somebody reported this, here is what they wrote.

    The category is also why the ``known_report_category`` CHECK constraint
    earned its place in increment 2. Nothing in this path runs a form or
    ``full_clean()``; a ``create()`` walks straight past both. The CHECK is
    the only thing between a malformed inbound payload and a row the queue
    cannot render.
    """
    return file_report(
        reporter=sender,
        target_user=target_user,
        target_status=target_status,
        category=Report.Category.OTHER,
        comment=(comment or "")[:INBOUND_COMMENT_MAX],
    )


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


def suspend_reported_member(report, *, by_user, note=""):
    """Suspend the reported member and resolve the pile as suspended (R102/R106).

    The third member of the family beside :func:`dismiss_report` and
    :func:`delete_reported_status`, and it keeps their shape on purpose: one
    helper, one transaction, the whole target pile resolved with who/when/
    what/note recorded on every row. A suspend that closed only the clicked
    row would put the same card straight back on the queue.

    **The account is the target, not the post.** ``target_user`` is the
    thing being suspended even when the card was raised by a post report,
    because that is what R102 defines suspend as — a state of an account.
    The reported post is left exactly where it is: suspend hides content,
    it does not remove it, and the read-side filters are what make it
    disappear. A moderator who wants the post itself gone uses delete.

    ``User.suspend()`` is the only writer of the suspension state and
    returns ``False`` for an already-suspended account, which is passed
    straight back so the caller can tell a decision from a replay rather
    than assuming one happened. The pile is still resolved either way —
    the work item is closed whether or not this click changed the account.

    **Does not decide who may suspend** (that is ``can_act_on``, the
    caller's question) and **does not broadcast** (the local write must
    commit before any network call, and only a local account can be
    signed for). See ``views.suspend`` for both.

    Returns ``(target, suspended, count)`` — the account acted on, whether
    this call is what suspended it (``False`` on a replay), and the number
    of report rows resolved.
    """
    target = report.target_user
    now = timezone.now()
    with transaction.atomic():
        count = Report.unresolved_for_target(report.target_key).update(
            resolved_at=now,
            resolved_by=by_user,
            action=Report.Action.SUSPEND,
            note=note,
        )
        suspended = target.suspend(reason=note)
    return target, suspended, count


def ban_reported_member(report, *, by_user, note=""):
    """Ban the reported member: remove their content, resolve the pile (R102/R106).

    The fourth member of the family beside dismiss, delete and suspend, and
    it keeps their shape on purpose — one helper, one transaction, the whole
    target pile resolved with who/when/what/note on every row. A ban that
    closed only the clicked row would put the same card straight back on the
    queue.

    **This is the one action that removes content rather than hiding it, and
    the difference is the whole point of R102.** Suspend touches no
    ``Status`` row at all; ban soft-deletes every live one the target owns.
    Because ``deleted=False`` is already the filter at roughly nineteen read
    sites, that single write takes the reviews, comments, replies and notes
    out of the feed, the film pages, the user tabs, the outbox and the
    thread walk with no new read-path code — which is exactly why ban must
    NOT borrow the suspension filters. If it did, the action would hide what
    it promised to remove, and nothing would announce it.

    **The content does not come back.** ``Status.delete()`` is R17
    soft-delete and clears ``content`` and ``raw_content`` on the way to
    becoming a tombstone. An unban restores the account, the localname and
    the actor document, but the writing is destroyed. That is inherent to
    the delete semantics this instance uses everywhere — the author's own
    delete does the same thing — so the honest place to disclose it is the
    confirmation the moderator reads before clicking, not a restore path
    this increment does not have.

    **The removal itself lives in :meth:`User.ban`, not here.** This helper
    resolves the pile and calls the one writer of the ban state; that writer
    owns the soft-delete. The split is deliberate but so is the direction —
    a model method that reaches into another app's tables for one caller is
    the wrong shape, whereas a state transition that leaves out the thing
    the state *means* is worse. See the note on ``User.ban`` for how the
    second of those was found.

    **Does not decide who may ban** (that is ``can_act_on``, plus the
    moderator-may-ban call the route applies) and **does not broadcast** —
    the local write must commit before any network call, and only a local
    account can be signed for. See ``views.ban`` for both.

    Returns ``(target, banned, count, removed)`` — the account, whether this
    call is what banned it (``False`` on a replay), the number of report
    rows resolved, and the statuses this action tombstoned (empty on a
    replay, because this call tombstoned nothing).
    """
    target = report.target_user
    now = timezone.now()
    with transaction.atomic():
        count = Report.unresolved_for_target(report.target_key).update(
            resolved_at=now,
            resolved_by=by_user,
            action=Report.Action.BAN,
            note=note,
        )
        # Captured before the call, because ``User.ban()`` is what removes
        # the content and after it there is nothing left to enumerate. On a
        # replay nothing was removed by this call, so reporting the
        # pre-existing live statuses as "removed" would be a lie — hence
        # the list is cleared rather than returned.
        live_before = list(Status.objects.filter(user=target, deleted=False))
        banned = target.ban(reason=note)
    return target, banned, count, (live_before if banned else [])


def record_federation_outcome(report, line: str) -> int:
    """Append a federation outcome to every row in this report's pile (R106).

    R106's rule is that a decision must never be unexplainable after the
    fact, and the report note is the only record this arc keeps. "The post
    was deleted here but three remote servers were never told" is exactly the
    fact a reader needs six months later, and it is unknowable from the row
    otherwise — the local write and the broadcast are deliberately separate
    transactions, so the delete succeeding says nothing about delivery.

    Appended rather than replacing because the moderator's own note is
    theirs; this line is the system reporting what it managed to do.
    Target-scoped for the same reason ``dismiss_report`` and
    :func:`delete_reported_status` are — the pile is the unit of work, so
    every row in it carries the same decision and the same outcome.
    """
    rows = Report.objects.filter(
        target_user_id=report.target_user_id,
        target_status_id=report.target_status_id,
    )
    updated = 0
    for row in rows:
        row.note = f"{row.note}\n{line}".strip() if row.note else line
        row.save(update_fields=["note"])
        updated += 1
    return updated


def record_forward_outcome(report, line: str) -> int:
    """Append a forwarding outcome to the open rows of this pile (R104/R106).

    The same need as :func:`record_federation_outcome` — "we told their
    server, and here is whether it arrived" is a fact the record has to
    carry — with one difference that makes it a separate function rather
    than a flag on that one. A forward does **not** resolve the pile: the
    moderator has not decided anything yet, only passed the complaint along,
    and the report stays open so they can still delete the local copy or
    dismiss it afterwards. That means the rows being written here are still
    live decisions, whereas the rows ``record_federation_outcome`` writes
    were closed by the very action it is reporting on.

    Scoping to ``resolved_at__isnull=True`` is what keeps a forward from
    appending a brand-new line to a decision somebody already made and
    signed. Increment 4b refused to put an unsuspend note on a closed report
    for exactly this reason — it rewrites a closed decision rather than
    adding to the record — and an open pile that happens to share a target
    with an old resolved one is the same problem in a less obvious place.
    """
    rows = Report.unresolved_for_target(report.target_key)
    updated = 0
    for row in rows:
        row.note = f"{row.note}\n{line}".strip() if row.note else line
        row.save(update_fields=["note"])
        updated += 1
    return updated


class DomainBlock(models.Model):
    """A remote server this instance refuses to deal with (R105).

    Deliberately minimal, per R105: one fact — this host is blocked — with
    none of Mastodon's partial severities, no ``obfuscate``, no
    ``reject_media``, no ``reject_reports``. Those knobs exist because a
    large instance tunes moderation per server; here the question a
    moderator asks is binary, and a severity field with one value in use is
    a field that advertises choices nobody can make.

    **The block is a rule; the effect on accounts is suspension.** That is
    the generalisation the owner chose for the local/remote-target question
    §2D left open. A blocked host's existing mirrors are suspended with
    ``suspension_origin = domain_block``, which means the *hide* half of
    the block costs no new read-path code at all — the eight-odd dozen
    suspension filters built in increment 4 already do it, at the feed, the
    film page, the thread walk, the profile, the collections, the mention
    resolver and the notification producers. A second, host-keyed filter
    would have had to be written at every one of those sites and would have
    been missed at some point, which is how a blocked server keeps showing
    up in exactly one place nobody thought to check.

    The cost of choosing suspension rather than a live host check is that the
    block is a **mutation of existing rows**, not a predicate evaluated at
    read time. That is why the door check in
    :func:`~reeltalk.activitypub.mirrors._resolve_actor` is independent of
    it: a mirror that somehow is not suspended — unsuspended by hand after
    the block, created by a path that predates it — still cannot resolve.
    The rule and the effect are two mechanisms, and both have to hold.
    """

    # Stored normalized: lowercase, no scheme, no path, no trailing dot.
    # Normalizing at the boundary rather than at every comparison is what
    # lets the match below be a plain suffix test.
    domain = models.CharField(max_length=255, unique=True)
    created = models.DateTimeField(default=timezone.now, db_index=True)
    # SET_NULL, matching ``Report.resolved_by``: a moderator leaving the
    # instance must not un-block the server they blocked, and must not
    # erase the record that someone did.
    created_by = models.ForeignKey(
        "social.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="domain_blocks_created",
    )
    reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["domain"]

    def __str__(self) -> str:
        return self.domain


def normalize_domain(value: str) -> str:
    """Reduce anything a moderator pastes in to a bare lowercase host.

    A moderator types ``https://Bad.Example/path``, a peer's actor URL, or
    just ``bad.example`` — all three must land on the same stored string,
    or the same server gets two block rows and one of them stops matching
    when the other is removed.
    """
    from urllib.parse import urlparse

    raw = (value or "").strip().lower()
    if not raw:
        return ""
    parsed = urlparse(raw if "//" in raw else f"//{raw}")
    host = (parsed.netloc or parsed.path).strip().strip(".")
    # Strip any credentials a pasted URL carried; they are not a host.
    host = host.rsplit("@", 1)[-1]
    # And the port. Both sides of every comparison here have to agree on what
    # a host *is*: if the stored value kept its port, a block on
    # ``bad.example`` would miss a peer whose actor URL is written
    # ``https://bad.example:443/...`` — the exact miss that lets a blocked
    # server back in through the door. The cost is that a moderator cannot
    # scope a block to one port, which is Mastodon's behaviour and the one
    # that cannot be bypassed by a URL spelled differently. IPv6 literals
    # keep their brackets, so the split is only outside them.
    if "]" not in host:
        host = host.split(":", 1)[0]
    return host


def account_host_candidates(user) -> set:
    """The host strings a mirror's identity can be matched on.

    Both the full netloc and the bare hostname, because a ReelTalk peer on
    a non-default port carries the port in its actor URL (R52) and a
    moderator blocking it needs to be able to name either form. Local users
    have no ``actor_url`` and therefore no candidates — a domain block is
    about somebody else's server, never our own.
    """
    from urllib.parse import urlparse

    url = getattr(user, "actor_url", "") or ""
    if getattr(user, "local", True) or not url:
        return set()
    parsed = urlparse(url)
    hosts = set()
    if parsed.netloc:
        hosts.add(parsed.netloc.lower())
    if parsed.hostname:
        hosts.add(parsed.hostname.lower())
    return hosts


def blocked_domain_for(host: str, *, exclude_pk=None) -> "DomainBlock | None":
    """The block that covers ``host``, longest match winning.

    Longest-first so a moderator who blocks ``mail.bad.example`` and later
    blocks ``bad.example`` gets the more specific rule reported for the
    subdomain, and — the direction that matters — a block on
    ``bad.example`` covers ``anything.bad.example``. Matching on a label
    boundary rather than a bare ``endswith`` is not pedantry: a substring
    match would let a block on ``e.com`` swallow ``badexample.com``,
    which is a way to block a server nobody meant to block.

    ``exclude_pk`` asks the same question with one block removed from the
    set, which is exactly what an unblock needs to know: after this row
    goes away, is that account still covered by something else? Without it,
    removing an outer block would re-expose an account an inner block is
    still holding down.
    """
    host = normalize_domain(host)
    if not host:
        return None
    blocks = DomainBlock.objects.exclude(domain="")
    if exclude_pk is not None:
        blocks = blocks.exclude(pk=exclude_pk)
    match = None
    for block in blocks:
        blocked = block.domain
        if host == blocked or host.endswith(f".{blocked}"):
            if match is None or len(blocked) > len(match.domain):
                match = block
    return match


def is_domain_blocked(user) -> bool:
    """Whether this user's home server is blocked."""
    return any(blocked_domain_for(host) for host in account_host_candidates(user))


def block_domain(domain: str, *, by_user=None, reason: str = "") -> tuple:
    """Block a remote server and suspend every mirror we hold from it (R105).

    Returns ``(block, suspended)`` — the block row and the list of accounts
    this call suspended. The list is the important half: it is exactly the
    set the matching unblock may lift, because :meth:`User.suspend` returns
    ``False`` for an account already suspended, so a mirror someone had
    already suspended individually keeps its own ``suspension_origin`` and
    stays suspended after the domain is unblocked. The block does not
    quietly absorb somebody else's decision, and it does not hand theirs
    back when the block is removed.

    One transaction: a block row with its mirrors still live is a state
    where the rule exists and nothing enforces it for the accounts already
    here, which is the least useful half of both.
    """
    normalized = normalize_domain(domain)
    if not normalized:
        raise ValueError("A domain block needs a host to block.")
    with transaction.atomic():
        block = DomainBlock.objects.create(
            domain=normalized, created_by=by_user, reason=reason
        )
        suspended = []
        # ``is_superuser`` is excluded rather than left to the AdminImmunityError
        # backstop in ``suspend()`` because a sweep that raises halfway leaves
        # the moderator with a block row and no explanation. A superuser with
        # ``local=False`` is nonsense the schema does not prevent, so the sweep
        # declines to touch one rather than trusting it never happens.
        for user in User.objects.filter(local=False, is_superuser=False):
            if not any(
                blocked_domain_for(host) is not None
                for host in account_host_candidates(user)
            ):
                continue
            note = reason or f"Blocked domain: {normalized}"
            if user.suspend(origin=SuspensionOrigin.DOMAIN_BLOCK, reason=note):
                suspended.append(user)
    return block, suspended


def unblock_domain(block, *, by_user=None) -> tuple:
    """Remove a domain block and lift the suspensions that block imposed.

    Lifts only rows whose ``suspension_origin`` is ``domain_block`` **and**
    whose host still matches this block's domain. Both halves are needed.
    The origin half is what keeps an unblock from restoring an account some
    other moderator suspended on its own merits. The host half is what
    keeps a block on ``bad.example`` from lifting a suspension that a
    *different* block — say ``mail.bad.example`` — is still responsible
    for.

    A third check belongs here that the first two do not cover: an account
    under this block may also sit under another block that is *staying*.
    Lifting it because this row was removed would re-expose an account the
    remaining rule still refuses at the door — visible here, unable to
    federate, which is the worst of both. So the lift asks whether anything
    else still covers the host once this block is gone, and leaves the
    account alone if it does.

    The known edge, accepted with the design: a mirror blocked first and
    then individually refused cannot express that, because it is already
    suspended and ``suspend()`` is a no-op — so it carries the domain's
    origin and an unblock lifts it. Refusing one account *and* its whole
    server at once is the one case this generalisation cannot separate, and
    it is the price of not running a second refusal column at every read
    site.
    """
    with transaction.atomic():
        lifted = []
        for user in User.objects.filter(
            local=False,
            suspension_origin=SuspensionOrigin.DOMAIN_BLOCK,
            suspended_at__isnull=False,
        ):
            if not sits_under_domain(user, block.domain):
                continue
            if any(
                blocked_domain_for(host, exclude_pk=block.pk) is not None
                for host in account_host_candidates(user)
            ):
                continue
            if user.unsuspend():
                lifted.append(user)
        DomainBlock.objects.filter(pk=block.pk).delete()
    return block, lifted


def sits_under_domain(user, domain: str) -> bool:
    """Whether ``user``'s home host sits under ``domain`` by the label rule."""
    domain = normalize_domain(domain)
    if not domain:
        return False
    return any(
        host == domain or host.endswith(f".{domain}")
        for host in account_host_candidates(user)
    )
