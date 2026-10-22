"""Inbox retention: bound the two tables the inbox writes to forever (F7).

Two tables grew without a ceiling. ``DeliveredActivity`` gained one row per
inbound activity id since the day the inbox first opened, and the throttle
table gained one row per failed POST. Neither is garbage while it is young —
the first is what makes a redelivery a no-op, the second is what makes a
flood stop being free — and both are pure weight once they age out. This
deletes the aged rows and nothing else.

**Why the dedup rows can go at all.** A dedup row earns its keep only while a
legitimate redelivery of that activity could still arrive, which is bounded by
the *sender's* retry schedule: a peer gives up on an inbox after hours, not
months. Thirty days is therefore not a guess at "how long is long enough" so
much as a wide margin over a schedule this instance does not control. Pruning
does reopen an old activity to being processed twice if it somehow arrives
again after its row is gone, which is the honest cost of the prune and the
reason the default is a month rather than a day.

**Why the throttle rows can go sooner.** A counted failure only matters inside
its own window; past that instant the count query cannot see it, so it is
already dead weight. It is pruned on the window rather than on ``--days``
because the window is what defines its useful life, and a row older than the
window can never be read by anything again.

**Deletion is batched on purpose.** The first run on an install that has been
federation for months is deleting from a table that has never been pruned, and
one unbounded ``DELETE`` on it holds locks for as long as it takes. Rows go in
batches with a short transaction each, so the command can run against a live
instance without stalling the deliveries that keep it live.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from reeltalk.activitypub.inbox_throttle import INBOX_FAILURE_WINDOW_MINUTES
from reeltalk.activitypub.models import DeliveredActivity, InboxAttempt

# Rows per DELETE. Small enough that each statement is brief on a busy table,
# large enough that a million-row backlog is not a million round trips.
BATCH_SIZE = 10_000

# A dedup row's useful life is the sender's retry schedule, which is hours. A
# month is a wide margin over it, and it is the cost of never silently
# re-processing a recent activity.
DEFAULT_RETENTION_DAYS = 30


class Command(BaseCommand):
    help = (
        "Delete inbox dedup rows older than the retention window, and throttle "
        "rows that have aged out of their own window."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help=(
                "Keep dedup rows this many days old (default "
                f"{DEFAULT_RETENTION_DAYS}). Must be positive: 0 would delete "
                "rows a peer could still legitimately redeliver."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted and delete nothing.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days < 1:
            # A zero or negative retention is a typo with a very bad first
            # run, so it is refused rather than interpreted.
            raise CommandError("--days must be at least 1.")

        now = timezone.now()
        cutoff = now - timedelta(days=days)
        attempts_cutoff = now - timedelta(minutes=INBOX_FAILURE_WINDOW_MINUTES)

        if options["dry_run"]:
            old_dedup = DeliveredActivity.objects.filter(delivered_date__lt=cutoff)
            old_attempts = InboxAttempt.objects.filter(created_at__lt=attempts_cutoff)
            self.stdout.write(
                f"[dry run] would delete {old_dedup.count()} delivered "
                f"activity row(s) older than {days} day(s) and "
                f"{old_attempts.count()} inbox attempt row(s) older than "
                f"{INBOX_FAILURE_WINDOW_MINUTES} minute(s)."
            )
            return

        dedup_deleted = self._delete_batched(
            DeliveredActivity.objects.filter(delivered_date__lt=cutoff),
            DeliveredActivity,
        )
        attempts_deleted = self._delete_batched(
            InboxAttempt.objects.filter(created_at__lt=attempts_cutoff),
            InboxAttempt,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {dedup_deleted} delivered activity row(s) and "
                f"{attempts_deleted} inbox attempt row(s)."
            )
        )

    @staticmethod
    def _delete_batched(queryset, model) -> int:
        """Delete every row in ``queryset``, a batch at a time.

        Each batch is its own transaction, so a long backlog never becomes one
        long lock. The ids are read first and deleted by primary key: a
        ``DELETE ... LIMIT`` does not exist in Postgres, and slicing the
        queryset in the WHERE clause would re-evaluate the age filter on every
        pass.
        """
        deleted = 0
        while True:
            ids = list(queryset.values_list("id", flat=True)[:BATCH_SIZE])
            if not ids:
                return deleted
            with transaction.atomic():
                model.objects.filter(id__in=ids).delete()
            deleted += len(ids)
