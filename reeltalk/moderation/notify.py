"""Telling staff that a report exists (2E).

Before this module a report reached staff only if somebody already thought to
open ``/moderate/``. This is the alert that finds them instead.

**Nothing here goes through** :func:`reeltalk.notifications.models.notify`,
and there is still no ``Notification.Kind.REPORT``. R99 forbids that, and it
forbids it for a reason that does not apply here: ``notify()``'s third guard
refuses to deliver anything to a recipient who has blocked the actor, which
is right for a person's own inbox and would be a censorship primitive on
the moderation queue — a member blocking every moderator would mute the
whole pile. This module computes its own recipient set from the moderator
permission and sends to *addresses*, so a block changes nothing about it.
**The trap that motivated R99 is absent here by construction, not by
checking for it.**

Three properties are lifted from Mastodon's ``notify_staff!``
(``app/services/report_service.rb:46-53``) because they are the three that
make the difference between a feature and a decoration:

* the whole notify is **deduped per unresolved target**, so N members
  reporting one spammer produce one alert rather than N (D-c);
* the recipient set is **permission-derived**, so there is no mailing list
  that can drift out of step with the role (D-f);
* the send is **never in the request that filed the report** (D-e).
"""

import logging

from django.core.mail import DEFAULT_MAILER_ALIAS, EmailMessage, mailers
from django.core.mail.backends.console import EmailBackend as ConsoleEmailBackend
from django.db import transaction
from django.template.loader import render_to_string

from reeltalk.activitypub.identity import absolute_uri
from reeltalk.social.models import SiteSettings, User

from .decorators import may_moderate
from .models import Report

logger = logging.getLogger("reeltalk.moderation.notify")


def staff_email_recipients(report) -> list:
    """The accounts to email about ``report``.

    Derived from :func:`~reeltalk.moderation.decorators.may_moderate`, the
    same predicate that gates ``/moderate/`` itself, narrowed by exactly two
    things that are **not** the moderator rule and so cannot drift from it:
    this account's own ``report_email`` opt, and having a non-empty address.
    There is deliberately no second "who is staff" list (D-f) — a second
    list is a second thing to forget to update, and the failure mode of
    forgetting it is silent.

    The candidate query narrows on the two non-role filters rather than on
    the role, so ``may_moderate`` stays the single authority on the role
    half even though it means walking the candidate set in Python. Reports
    are filed at human speed and the dedup caps this at one pass per
    unresolved target, so the walk costs nothing worth restating a settled
    permission function to avoid.

    **The reporter is excluded** (2E trap 4). This is not a corner case:
    moderators are members and file reports, so without this a moderator
    who reports something emails themselves about their own report.

    **Email is not unique** (2E trap 3) and this function does not pretend
    otherwise. Two staff sharing one address yield two recipients, each
    sent their own message — the same per-account behaviour Mastodon has.
    Nothing here collapses recipients by address, does a reverse lookup from
    an address to a user, or relies on addresses identifying one person.
    """
    reporter_id = report.reporter_id
    candidates = User.objects.filter(report_email=True).exclude(email="")
    return [
        user for user in candidates if user.pk != reporter_id and may_moderate(user)
    ]


def console_backend_active() -> bool:
    """Whether the default mail backend is the console one.

    Detected by class rather than by reading a setting, so it follows whatever
    ``MAILERS`` actually resolved to instead of a guess about how it was
    configured. ``mailers.create_connection`` reads ``settings.MAILERS`` on
    every call rather than caching, which is what makes this testable under
    ``override_settings`` — and it is the non-deprecated route: the old
    ``get_connection()`` raises a ``RemovedInDjango70Warning`` now that
    Django 6.1 has ``MAILERS`` (R2), and this project opted into ``MAILERS``
    from day one.

    This exists because **the console backend reports success.** Printing to
    stdout *is* a successful send as far as Django is concerned: the task
    goes green, the worker log shows no error, and nothing reaches anybody.
    An operator who never set ``EMAIL_HOST`` would have no way to find out,
    which is R88's "`202 Accepted` is not delivery" lesson in a new
    costume and the single most important thing in this increment to get
    right. See :func:`_warn_if_console_backend` for the diagnostic.
    """
    return isinstance(
        mailers.create_connection(DEFAULT_MAILER_ALIAS), ConsoleEmailBackend
    )


def _warn_if_console_backend(count: int) -> bool:
    """Shout if staff email is enabled but the backend cannot deliver.

    Called at enqueue time, in the process that filed the report, because
    that is the earliest moment the fact is known and the loudest place it
    will be seen. The worker shouts again at send time — an operator reading
    the worker log when "nobody got the email" is looking at the worker,
    not the web container.
    """
    if not console_backend_active():
        return False
    logger.warning(
        "STAFF REPORT EMAIL WILL NOT BE DELIVERED: the mail backend is %s, "
        "which prints messages to stdout instead of sending them. %s staff "
        "report email(s) just got enqueued and will reach nobody. A green "
        "task result means PRINTED, not DELIVERED. Set EMAIL_HOST (and the "
        "other EMAIL_* keys) in .env and restart the stack to actually send. "
        "This warning is deliberate: a silently-undelivered staff alert is "
        "indistinguishable from a feature that was never built.",
        ConsoleEmailBackend.__module__ + "." + ConsoleEmailBackend.__name__,
        count,
    )
    return True


def build_report_email(report, recipient) -> EmailMessage:
    """The message one staff member gets about one report.

    **A pointer, not a mirror** (2E trap 5). It carries the queue link, who
    was reported, who reported it, the category, and how many reports are
    open on that target. It deliberately does **not** carry the reported
    post's text or the reporter's comment: this mail may be forwarded,
    quoted, or land in a mailbox that is not this instance's, and copying
    the accused content into it widens the leak surface for something the
    recipient can read in the queue in one click.
    """
    site = _site_name()
    target = report.target_user
    reporter = report.reporter
    open_count = Report.unresolved_for_target(report.target_key).count()
    context = {
        "site_name": site,
        "target_label": f"@{target.localname}",
        "reporter_label": f"@{reporter.localname}",
        "category_label": report.get_category_display(),
        "open_count": open_count,
        "filed_at": report.created,
        "queue_url": absolute_uri("/moderate/"),
    }
    body = render_to_string("moderation/email/new_report.txt", context)
    return EmailMessage(
        subject=f"[{site}] New report about {context['target_label']}",
        body=body,
        to=[recipient.email],
    )


def _site_name() -> str:
    """The instance's own name for the mail subject line.

    ``SiteSettings.get_instance()`` creates the singleton row with defaults
    on first use, so a half-provisioned instance still yields a name and a
    staff alert never fails to send over a missing settings row.
    """
    return SiteSettings.get_instance().name or "ReelTalk"


def notify_staff_of_report(report) -> dict:
    """Work out who to tell about ``report`` and queue the telling.

    Returns a small dict describing the decision — ``{"enqueued": n,
    "reason": ...}`` — so a caller and a test can see *why* nothing
    happened rather than inferring it from an empty outbox.

    **The dedup is checked here, before anything is enqueued** (D-c), not
    inside the task. A burst of reports on one target would otherwise put N
    tasks on the cluster before any of them ran and saw the siblings;
    checking at the door means the burst costs one alert. This mirrors
    Mastodon's ``return if @report.unresolved_siblings?`` gating the whole
    block, and uses this codebase's existing primitive —
    :meth:`Report.unresolved_for_target` — over the same ``target_key`` the
    queue card and ``dismiss_report`` are built on, so "the unit that was
    already told about" and "the unit a moderator dismisses" are the same
    unit. That alignment is the whole reason one dismiss does not leave a
    re-notification behind.

    **Nothing sends here.** Every send is enqueued (D-e), which gives
    R108's loud-and-non-blocking property *structurally* rather than by
    wrapping a try/except around a send: a dead SMTP server cannot roll back
    a report, slow the submit, or surface an error to the member who filed,
    because the send is not in the request at all.

    The enqueue is hung off ``transaction.on_commit`` so a report that is
    rolled back never produces mail — same shape as the backfill enqueue in
    :mod:`reeltalk.core.import_export`. Outside an atomic block Django runs
    the callback immediately, so the non-transactional call sites are
    unaffected.
    """
    if Report.unresolved_for_target(report.target_key).exclude(pk=report.pk).exists():
        logger.info(
            "staff notification skipped for report %s: target %r already has "
            "an open report, so staff were already told about it",
            report.pk,
            report.target_key,
        )
        return {"enqueued": 0, "reason": "target-already-has-an-open-report"}

    recipients = staff_email_recipients(report)
    if not recipients:
        # Loud, not silent: a report exists that nobody is going to hear
        # about. This is the case an operator most needs to see, and it is
        # reachable by two ordinary causes — every moderator has the opt
        # off, or none of them has an email address on file.
        logger.warning(
            "NO STAFF NOTIFICATION FOR REPORT %s: no eligible recipient. "
            "Either every moderator has report_email turned off or none has "
            "a non-empty email address. The report is filed and nobody has "
            "been told. Check the Roles fieldset in the user admin.",
            report.pk,
        )
        return {"enqueued": 0, "reason": "no-eligible-recipients"}

    _warn_if_console_backend(len(recipients))

    recipient_ids = [user.pk for user in recipients]
    report_pk = report.pk
    transaction.on_commit(lambda: _enqueue_report_emails(report_pk, recipient_ids))
    logger.info(
        "queued staff report email for report %s to %d recipient(s)",
        report.pk,
        len(recipient_ids),
    )
    return {"enqueued": len(recipient_ids), "reason": ""}


def _enqueue_report_emails(report_pk: int, recipient_ids: list) -> None:
    """Fan the per-recipient sends out onto the cluster.

    One task per recipient rather than one task sending N messages, so one
    recipient's bad address cannot abort the others mid-loop.
    """
    from reeltalk.moderation.tasks import enqueue_report_emails

    enqueue_report_emails(report_pk, recipient_ids)
