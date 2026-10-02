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

from .models import EmailVerificationToken, PasswordResetToken, User

logger = logging.getLogger("reeltalk.social.verify")

SEND_TOKEN_FUNC = "reeltalk.social.tasks.send_verification_token"
SEND_NOTICE_FUNC = "reeltalk.social.tasks.send_address_change_notice"
SEND_RESET_FUNC = "reeltalk.social.tasks.send_password_reset_token"
SEND_PW_NOTICE_FUNC = "reeltalk.social.tasks.send_password_changed_notice"
TOKEN_TASK_NAME = "email-verification"
NOTICE_TASK_NAME = "email-changed-notice"
RESET_TASK_NAME = "password-reset"
PW_NOTICE_TASK_NAME = "password-changed-notice"


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


def enqueue_password_reset_email(token_pk: int) -> str:
    """Put one password reset send on the cluster; returns the task id."""
    return async_task(SEND_RESET_FUNC, token_pk, task_name=RESET_TASK_NAME)


def enqueue_password_changed_notice(user_pk: int) -> str:
    """Put the 'your password was changed by someone else' notice on the cluster."""
    return async_task(SEND_PW_NOTICE_FUNC, user_pk, task_name=PW_NOTICE_TASK_NAME)


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


def send_password_reset_token(token_pk: int) -> dict:
    """Mail one password reset link and write the result onto its row.

    Same contract as :func:`send_verification_token`, because the failure
    it has to survive is the same one: a reset mail that vanishes is worse
    than one that never existed, since the member is standing at a login
    page that will not admit them and has no way to tell that a send was
    even attempted. ``sent_at`` / ``send_error`` on the row are what the
    admin reads to answer that, and both are written here rather than in
    the request, which only knows it queued something.

    Re-raised after recording, so a failure is a red ``Task`` row and a
    logged cause rather than a green result that means nothing.
    """
    from .password_reset import build_reset_email

    try:
        token = PasswordResetToken.objects.select_related("user").get(pk=token_pk)
    except PasswordResetToken.DoesNotExist:
        logger.info(
            "password reset email task dropped: token %s no longer exists", token_pk
        )
        return {"sent": False, "reason": "token-missing"}

    if console_backend_active():
        logger.warning(
            "PASSWORD RESET EMAIL IS NOT BEING DELIVERED (token %s -> %s): "
            "the mail backend prints to stdout. This message went nowhere, "
            "and @%s cannot recover their account.",
            token_pk,
            token.email,
            token.user.localname,
        )

    message = build_reset_email(token.user, token)
    try:
        message.send()
    except Exception as exc:
        line = _one_line(exc)
        token.send_error = line
        token.save(update_fields=["send_error"])
        logger.error(
            "Password reset email to %s for @%s FAILED: %s",
            token.email,
            token.user.localname,
            line,
        )
        raise

    token.sent_at = timezone.now()
    token.save(update_fields=["sent_at"])
    logger.info(
        "Password reset email sent to %s for @%s.", token.email, token.user.localname
    )
    return {"sent": True, "email": token.email}


def send_password_changed_notice(user_pk: int) -> dict:
    """Mail the notice that someone else replaced this member's password.

    **Log-only, exactly like the address-change notice.** The mail carries
    no link and is not a credential, so there is no row of ours for its
    outcome to live on. The accepted cost is the same one recorded for
    R125's notice: if this send fails, nothing on an admin screen shows
    it, and the only way to find out is to read the log — which is why the
    line is written at WARNING rather than INFO. For a mail whose whole job
    is telling a member that somebody else got into their account, a
    silent failure is the worst available outcome, so it is made as loud
    as a log line can be until there is an outbox table to put it on.
    """
    from .passwords import build_password_changed_email

    try:
        user = User.objects.get(pk=user_pk)
    except User.DoesNotExist:
        logger.warning(
            "Password-changed notice not sent: account %s no longer exists.", user_pk
        )
        return {"sent": False, "reason": "user-missing"}

    if console_backend_active():
        logger.warning(
            "PASSWORD-CHANGED NOTICE IS NOT BEING DELIVERED (@%s -> %s): "
            "the mail backend prints to stdout. The member's only signal "
            "that someone else changed their password went nowhere.",
            user.localname,
            user.email,
        )

    message = build_password_changed_email(user)
    try:
        message.send()
    except Exception as exc:
        logger.error(
            "Password-changed notice to %s for @%s FAILED: %s",
            user.email,
            user.localname,
            _one_line(exc),
        )
        raise

    logger.info(
        "Password-changed notice sent to %s for @%s.", user.email, user.localname
    )
    return {"sent": True, "email": user.email}
