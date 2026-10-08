"""Re-publish every live local list as an ``Update`` (§2K increment 6).

**Why this exists at all.** ``create_activity``'s id is
``{outbox}#activity-<local_id>`` -- stable per status, deliberately, so
redeliveries dedup. Every list Note this instance delivered *before* the list
body existed therefore arrived under exactly the id a re-sent ``Create``
would use again, and a peer holding that id discards the new one as a
redelivery of the old. The fix would land in the outbox for future fetchers
and never touch the empty copies already sitting on peers.

Only an ``Update`` gets through, because ``update_activity`` puts a uuid in
its id and no peer has seen that id before. This command is the one-off pass
that uses it: every live local list goes out again with its real body, so a
follower holding an empty Note gets the list.

**Idempotence, stated honestly.** Running this twice sends two ``Update``
activities with two different activity ids and one identical object id. The
peer applies the second to the same object and writes the same values, so
the *result* is idempotent -- but the *traffic* is not. It is a repair
tool, not a steady-state loop; run it once per deploy that needs it.

**Nothing here creates, edits or deletes a row.** It reads, it serialises,
it delivers. A list that has since been edited simply goes out in its
current state, which is the correct state to repair a peer to.
"""

from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory

from reeltalk.activitypub.broadcast import broadcast_list_update
from reeltalk.lists.models import FilmList


class Command(BaseCommand):
    help = (
        "Re-send every live local list as an Update activity, so peers "
        "holding a copy made before the list body existed get the real one."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            dest="list_ids",
            type=int,
            action="append",
            help="Only this list id. Repeatable. Default: every live local list.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be sent and to how many remote followers, "
            "without sending anything.",
        )

    def handle(self, *args, **options):
        lists = FilmList.objects.filter(deleted=False, local=True).select_related(
            "user", "status"
        )
        if options["list_ids"]:
            lists = lists.filter(pk__in=options["list_ids"])
        lists = list(lists)

        if not lists:
            # Not an error: a box with no lists has nothing to repair. But
            # say it loudly rather than silently succeeding, so an operator
            # who expected three lists notices they are not here.
            self.stdout.write(self.style.WARNING("No live local lists to re-send."))
            return

        # A management command has no request, but the broadcast layer needs
        # one to sign as. ``RequestFactory`` is enough: every published URL
        # is minted from ``settings.CANONICAL_ORIGIN`` rather than from the
        # request, so a synthetic request cannot put a wrong host on the
        # wire -- which is the whole reason ``absolute_uri`` ignores it.
        request = RequestFactory().get("/")

        if options["dry_run"]:
            for film_list in lists:
                audience = film_list.user.followers.exclude(local=True).count()
                self.stdout.write(
                    f"[dry run] list {film_list.pk} "
                    f"({film_list.user.localname}: {film_list.title}) "
                    f"-> {audience} remote follower(s)"
                )
            return

        failures = []
        for film_list in lists:
            for failure in broadcast_list_update(request, film_list) or []:
                failures.append(failure)
                self.stderr.write(
                    f"list {film_list.pk} -> {failure.inbox}: {failure.reason}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Re-sent {len(lists)} list(s) as Update; "
                f"{len(failures)} delivery failure(s)."
            )
        )
        if failures:
            # Non-zero so a deploy script cannot read a partial repair as a
            # complete one. The command ran fine; the network did not.
            raise CommandError(f"{len(failures)} delivery failure(s)")
