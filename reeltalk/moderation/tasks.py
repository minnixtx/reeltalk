"""Worker tasks for the staff report notification (2E).

Thin delivery wrappers in the :mod:`reeltalk.core.tasks` style: the deciding
— who gets told, whether to tell anybody at all — lives in
:mod:`reeltalk.moderation.notify` and is testable without a queue. What is
here is the part that can only happen on the worker: actually talking to the
mail server, and recording what came of it.
"""

import logging

from django_q.tasks import async_task

from reeltalk.social.models import User

from .models import Report, record_staff_notification_outcome
from .notify import build_report_email, console_backend_active

logger = logging.getLogger("reeltalk.moderation.notify")

SEND_FUNC = "reeltalk.moderation.tasks.send_report_email"
TASK_NAME = "staff-report-email"


def enqueue_report_emails(report_pk: int, recipient_ids) -> list:
    """Put one send task per recipient on the cluster; returns the task ids.

    One task per recipient rather than one task that loops: a single bad
    address in a three-moderator setup would otherwise abort the loop and
    take the other two recipients' mail with it, silently.
    """
    return [
        async_task(SEND_FUNC, report_pk, recipient_id, task_name=TASK_NAME)
        for recipient_id in recipient_ids
    ]


def send_report_email(report_pk: int, recipient_id: int) -> dict:
    """Send one staff report email and record that it happened.

    **Task failure must be visible** (2E trap 6), which here means three
    things rather than one: a line in the worker log, a line on the
    report's own audit row, and a red ``Task`` row in django-q's table.
    The first two are written on the way out whether the send worked or not;
    the third comes from re-raising after recording, so the failure is not
    swallowed into a green result. Re-raising is safe — django-q records an
    raised task as failed and does not retry it (``retry`` governs tasks
    that were never picked up), so the audit line cannot accumulate one per
    retry.

    The audit line names the recipient by handle, not by address. The
    report's note is readable in the queue by every moderator, so it is
    not where an address list belongs; the handle identifies who was told
    without turning the queue into a directory.
    """
    try:
        report = Report.objects.get(pk=report_pk)
    except Report.DoesNotExist:
        logger.warning(
            "staff report email task dropped: report %s no longer exists "
            "(recipient %s)",
            report_pk,
            recipient_id,
        )
        return {"sent": False, "reason": "report-missing"}

    try:
        recipient = User.objects.get(pk=recipient_id)
    except User.DoesNotExist:
        logger.warning(
            "staff report email task dropped for report %s: recipient %s no "
            "longer exists",
            report_pk,
            recipient_id,
        )
        record_staff_notification_outcome(
            report, f"Staff email not sent: recipient {recipient_id} is gone."
        )
        return {"sent": False, "reason": "recipient-missing"}

    if console_backend_active():
        # The worker shouts too. An operator chasing "nobody got the email"
        # reads the worker log, not the web container, and this is the
        # place that answers the question they are actually asking.
        logger.warning(
            "STAFF REPORT EMAIL IS NOT BEING DELIVERED (report %s -> @%s): "
            "the mail backend prints to stdout. This message went nowhere.",
            report_pk,
            recipient.localname,
        )

    message = build_report_email(report, recipient)
    try:
        message.send()
    except Exception as exc:
        line = (
            f"Staff email to @{recipient.localname} FAILED: {type(exc).__name__}: {exc}"
        )
        logger.error("%s (report %s)", line, report_pk)
        record_staff_notification_outcome(report, line)
        raise

    line = f"Staff email sent to @{recipient.localname}."
    logger.info("%s (report %s)", line, report_pk)
    record_staff_notification_outcome(report, line)
    return {"sent": True, "recipient": recipient.localname}
