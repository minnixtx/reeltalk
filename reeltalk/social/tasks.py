"""Worker side of the verification mail (2F-2).

Thin delivery wrappers in the :mod:`reeltalk.core.tasks` style: every
decision — whether to ask, what the mail says, who gets the tamper notice —
lives in :mod:`reeltalk.social.verify` and is testable without a queue.
What is here is the part that can only happen on a worker: talking to the
mail server, and recording what came of it.

**Recording the outcome is the point of this module.** A send that nobody
can ask about afterwards is the silent-non-delivery class 2E trap 6 was
written against, and verification is the sharpest case of it: the mail is
the only thing standing between a member and an account they cannot use.
Each token row carries ``sent_at`` / ``send_error`` for exactly that, and
both are written here — never in the request, which only knows it queued
something. "We queued an email" is not the fact worth keeping.
"""

import logging

from django.utils import timezone
from django_q.tasks import async_task

from reeltalk.moderation.notify import console_backend_active

from .models import EmailVerificationToken, User

logger = logging.getLogger("reeltalk.social.verify")

SEND_TOKEN_FUNC = "reeltalk.social.tasks.send_verification_token"
SEND_NOTICE_FUNC = "reeltalk.social.tasks.send_address_change_notice"
TOKEN_TASK_NAME = "email-verification"
NOTICE_TASK_NAME = "email-changed-notice"


def _one_line(exc: Exception) -> str:
    """The exception as a single line, for a column a human reads.

    SMTP errors arrive multi-line and with the server's own preamble. Newlines
    are collapsed rather than stripped so nothing is lost — the whole string
    goes in a ``TextField`` — but it occupies one row of an admin readout
    instead of pushing the rest of the record off screen.
    """
    return " ".join(f"{type(exc).__name__}: {exc}".split())


def enqueue_verification_email(token_pk: int) -> str:
    """Put one verification send on the cluster; returns the task id.

    One task per token, matching 2E's one-task-per-recipient: a send that
    dies must not take any other send with it.
    """
    return async_task(SEND_TOKEN_FUNC, token_pk, task_name=TOKEN_TASK_NAME)


def enqueue_address_change_notice(user_pk: int, old_email: str) -> str:
    """Put the R125 tamper notice on the cluster."""
    return async_task(SEND_NOTICE_FUNC, user_pk, old_email, task_name=NOTICE_TASK_NAME)


def send_verification_token(token_pk: int) -> dict:
    """Mail one verification token and write the result onto its row.

    **Task failure must be visible** (2E trap 6), which means three things
    rather than one: a line in the worker log, the error on the token row
    that ``send_state`` reads, and a red ``Task`` row in django-q's table.
    The first two are written whether the send worked or not; the third comes
    from re-raising after recording, so a failure is never swallowed into a
    green result. Re-raising is safe — django-q marks a raised task failed
    and does not re-run it, so the row cannot accumulate one error per
    retry, and ``sent_at`` stays empty, which is the truth.

    The row is written on the failure path *before* the raise. That ordering
    is the whole design: a task that raises without recording leaves an
    operator with a red ``Task`` row and no idea which address was refused
    or why, which is the same as not having recorded anything.
    """
    from .verify import build_verification_email

    try:
        token = EmailVerificationToken.objects.select_related("user").get(pk=token_pk)
    except EmailVerificationToken.DoesNotExist:
        # Not alarming: the account was deleted between signup and the
        # worker getting to it, and CASCADE took the row with it.
        logger.info(
            "verification email task dropped: token %s no longer exists", token_pk
        )
        return {"sent": False, "reason": "token-missing"}

    if console_backend_active():
        # The worker shouts too, because the operator chasing "nobody got
        # the link" reads the worker log, not the web container.
        logger.warning(
            "VERIFICATION EMAIL IS NOT BEING DELIVERED (token %s -> %s): the "
            "mail backend prints to stdout. This message went nowhere, and "
            "@%s cannot verify their address.",
            token_pk,
            token.email,
            token.user.localname,
        )

    message = build_verification_email(token.user, token)
    try:
        message.send()
    except Exception as exc:
        line = _one_line(exc)
        token.send_error = line
        token.save(update_fields=["send_error"])
        logger.error(
            "Verification email to %s for @%s FAILED: %s",
            token.email,
            token.user.localname,
            line,
        )
        raise

    token.sent_at = timezone.now()
    token.save(update_fields=["sent_at"])
    logger.info(
        "Verification email sent to %s for @%s.", token.email, token.user.localname
    )
    return {"sent": True, "email": token.email}


def send_address_change_notice(user_pk: int, old_email: str) -> dict:
    """Mail the R125 notice to the address that used to be on file.

    **Log-only, by the owner's decision.** This mail is not a credential —
    it carries no link and proves nothing — so unlike the verification mail
    there is no row of ours that its outcome belongs on. What it has is this
    log line, on a logger raised to INFO so it reaches ``docker logs``. The
    accepted cost is stated rather than glossed: if this send fails, nothing
    in the admin screen shows it, and the only way to find out is to read
    the log. A general outbox table would fix that and is a bigger piece of
    work than this increment.

    Re-raised after logging for the same reason as the verification send: a
    red ``Task`` row beats a green lie.
    """
    from .verify import build_address_change_email

    try:
        user = User.objects.get(pk=user_pk)
    except User.DoesNotExist:
        logger.warning(
            "Address-change notice not sent: account %s no longer exists "
            "(was notifying %s)",
            user_pk,
            old_email,
        )
        return {"sent": False, "reason": "user-missing"}

    if console_backend_active():
        logger.warning(
            "ADDRESS-CHANGE NOTICE IS NOT BEING DELIVERED (@%s -> %s): the "
            "mail backend prints to stdout. The member's only signal that "
            "their address moved went nowhere.",
            user.localname,
            old_email,
        )

    message = build_address_change_email(user, old_email)
    try:
        message.send()
    except Exception as exc:
        logger.error(
            "Address-change notice to %s for @%s FAILED: %s",
            old_email,
            user.localname,
            _one_line(exc),
        )
        raise

    logger.info("Address-change notice sent to %s for @%s.", old_email, user.localname)
    return {"sent": True, "email": old_email}
