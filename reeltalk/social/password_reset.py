"""Self-service password reset (2G).

The request half of recovery: a logged-out person submits an address, and
if that address can be helped, a single-use link goes to it. The confirm
half — spending the link and writing the new password — completes here too,
because the transaction that makes "one click, both writes" true spans the
token and the password, and splitting them across modules is how one of the
two ends up happening without the other.

**This flow reuses §2F's token machinery and nothing else about §2F.**
:class:`~reeltalk.social.models.PasswordResetToken` inherits the same
mint / supersede / row-lock / send-state mechanics as the verification
token, on its own table with its own TTL. It is not the same credential and
must never become one (R121): a verification link is minted to an address
that is not yet proven, and letting it also set a password would make every
unproven signup link a takeover credential.

**Reset gets its own throttle budget, and that separation is load-bearing
rather than tidiness (R129).** ``RESET_ADDRESS_COOLDOWN_MINUTES`` and
``RESET_IP_LIMIT`` mirror the resend route's numbers and share none of its
counters. If they shared a budget, the unauthenticated resend route would
be a denial-of-service against password recovery: spam the resend form at
someone's address until the shared window is full, and they can no longer
reset their own password — the escape hatch jammed shut through a door we
are obliged to leave open. Same numbers keep it explainable; separate
counters stop the two routes from starving each other.

**Who this refuses, and what that costs.** An account whose address is not
verified cannot reset (owner decision, 2026-10-02): under R119 such an
account cannot sign in whatever its password is, so a reset would hand out
a credential that opens nothing. The cost is that the reset page must not
promise a link that is not coming — which is why the page says the
verification precondition out loud to every visitor, before anyone types,
and why the post-submit answer is one uniform sentence that varies for
nobody. Refusing visibly instead would turn the page into an oracle for
which addresses are registered-but-unverified.
"""

import logging
from datetime import timedelta

from django.core.mail import EmailMessage
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from reeltalk.activitypub.identity import absolute_uri
from reeltalk.moderation.notify import console_backend_active

from .models import AddressMismatchError, PasswordResetToken, SiteSettings, User
from .passwords import set_password

logger = logging.getLogger("reeltalk.social.password_reset")

# The path the reset link points at, and the page that asks for one. A
# constant in one place for the same reason ``VERIFY_PATH`` is: it is a
# contract between the mail and the route, and a test can pin the two
# together so a rename cannot leave every sent link pointing at a 404.
RESET_PATH = "/account/password-reset/"

# R129's own budget. Mirrors R122's resend shape — 5 minutes per address,
# 25 per 5 minutes per source — and shares no state with it. See the module
# docstring for why sharing would be an availability bug rather than a
# saving.
RESET_ADDRESS_COOLDOWN_MINUTES = 5
RESET_IP_LIMIT = 25
RESET_IP_WINDOW_MINUTES = 5


def reset_url(token) -> str:
    """The link that goes in the reset mail.

    Minted from ``settings.CANONICAL_ORIGIN`` via :func:`absolute_uri`,
    never from the request — the same reasoning R108 applies to every
    published identifier and 2F applies to the verification link. A reset
    link has to be openable by whoever receives it, from wherever they
    happen to be, and it is a credential rather than a navigation link.
    """
    return absolute_uri(f"{RESET_PATH}{token.code}/")


def reset_address_in_cooldown(email: str) -> bool:
    """True when a reset mail for ``email`` is too recent to send another.

    The cooldown is the token table itself, which is what makes it
    unbypassable by clearing cookies: keyed server-side on the address, not
    on anything the client holds.

    ``send_error=""`` is in the filter for the same reason it is in the
    resend one: a mail that never left the box must not make the member
    wait out a window for something that never arrived. A failed send is
    not a state a caller can arrange — it takes a broken transport, which
    is the one thing this page is not trying to throttle.
    """
    if not email:
        return False
    since = timezone.now() - timedelta(minutes=RESET_ADDRESS_COOLDOWN_MINUTES)
    return PasswordResetToken.objects.filter(
        email=email, created_at__gt=since, send_error=""
    ).exists()


def reset_ip_budget_spent(ip: str) -> bool:
    """True when this source has already minted its limit of reset mails.

    Counted from the token table rather than a cache so the limit survives a
    deploy and is shared by every web process.

    **This is the axis R127 made possible.** A per-source budget keyed on
    an address the caller can forge is worse than no budget at all — it is
    a limit that never binds against anyone who knows to lie, and it
    punishes only the honest. That is why reset was sequenced after the
    right-to-left forwarded-header walk, and why every call here goes
    through ``client_ip()`` rather than reading a header itself.
    """
    if not ip:
        return False
    since = timezone.now() - timedelta(minutes=RESET_IP_WINDOW_MINUTES)
    return (
        PasswordResetToken.objects.filter(
            request_ip=ip, created_at__gt=since, send_error=""
        ).count()
        >= RESET_IP_LIMIT
    )


def request_reset(raw_email: str, ip: str) -> dict:
    """A logged-out request to be sent a password reset link.

    Returns a reason for the **log**, never for the page. The view renders
    one uniform sentence whatever comes back, because this is an
    unauthenticated form that takes an address and must not tell anyone
    which addresses are registered, verified, or refused.

    **The order of the checks is the defence, not an implementation
    detail.** The per-source limit is tested before anything is looked up,
    because it is the guard that has to hold even when everything behind it
    is being exercised as hard as it can be.
    """
    email = (raw_email or "").strip().lower()
    if not email:
        return {"enqueued": 0, "reason": "no-address-given"}

    if reset_ip_budget_spent(ip):
        logger.warning(
            "Password reset refused: %s is over the limit of %d per %d "
            "minutes. Uniform response sent.",
            ip or "(unknown source)",
            RESET_IP_LIMIT,
            RESET_IP_WINDOW_MINUTES,
        )
        return {"enqueued": 0, "reason": "ip-limited"}

    user = User.objects.filter(email=email).first()
    if user is None:
        # Silent by design: indistinguishable from a successful send.
        logger.info("Password reset requested for an unknown address.")
        return {"enqueued": 0, "reason": "no-account"}

    if not user.local:
        # A remote mirror has no password here to reset — it signs in on its
        # own home instance. Mailing a reset link for it would be a link to
        # a page that can never work, sent to a person we cannot help this
        # way, and it would read to the recipient as though their account
        # lived here when it does not.
        logger.info(
            "Password reset refused for @%s: a remote account resets on its "
            "own instance.",
            user.localname,
        )
        return {"enqueued": 0, "reason": "remote-account"}

    if not user.email_verified:
        # The owner's rule: an unverified account cannot do anything here,
        # and that includes recovery. Under R119 a new password would not
        # open the account, so sending one would be a credential that does
        # nothing and a page that implied otherwise.
        #
        # What the member actually needs is the verification route, and the
        # reset page tells every visitor that — in the same words, whoever
        # they are — rather than revealing it only to the addresses that
        # happen to be in this state.
        logger.info(
            "Password reset refused for @%s: the address on this account is "
            "not verified, so the account cannot sign in whatever its "
            "password is.",
            user.localname,
        )
        return {"enqueued": 0, "reason": "not-verified"}

    if reset_address_in_cooldown(email):
        logger.info(
            "Password reset for @%s is inside the %d-minute cooldown; no new mail.",
            user.localname,
            RESET_ADDRESS_COOLDOWN_MINUTES,
        )
        return {"enqueued": 0, "reason": "address-cooldown"}

    try:
        token = PasswordResetToken.mint(user, request_ip=ip)
    except ValueError as exc:
        # Unreachable in practice — a verified account necessarily has an
        # address, since verification is derived from one. Kept because the
        # mint owns that rule and this caller should not assume it.
        logger.warning("No password reset email for @%s: %s", user.localname, exc)
        return {"enqueued": 0, "reason": "no-address"}

    if console_backend_active():
        logger.warning(
            "PASSWORD RESET EMAIL WILL NOT BE DELIVERED: the mail backend "
            "prints to stdout instead of sending. A reset link was enqueued "
            "for @%s and will reach nobody. Set EMAIL_HOST in .env and "
            "rebuild the images to actually send.",
            user.localname,
        )

    token_pk = token.pk
    transaction.on_commit(lambda: _enqueue_send(token_pk))
    logger.info(
        "queued password reset email for @%s to %s (token %s)",
        user.localname,
        token.email,
        token.code[:8],
    )
    return {"enqueued": 1, "reason": ""}


def classify_reset_code(code: str) -> tuple[str, str | None]:
    """``(outcome, problem_sentence)`` for a code, without spending it.

    The confirm page needs this on **GET** to decide whether to show a
    password form or an explanation, and it is safe to be specific here for
    the reason R122 already established: reaching this page at all requires
    an unguessable ~256-bit token, so whoever reads "your link expired on
    such-and-such a date" already holds a real credential and is entitled
    to know what is wrong with it. The enumeration surface is the address
    form, not this one.
    """
    token = PasswordResetToken.objects.filter(code=code).first()
    if token is None:
        return "invalid", "That password reset link is not valid."
    if not token.is_live:
        return _dead_outcome(token), token.unusable_reason()
    if token.email != token.user.email:
        return "mismatch", _MISMATCH
    return "live", None


def _dead_outcome(token) -> str:
    if token.used_at is not None:
        return "used"
    if token.superseded_at is not None:
        return "superseded"
    return "expired"


_MISMATCH = (
    "That link was sent to a different address than the one on this "
    "account now. Ask for a new link and it will go to the current one."
)


def complete_reset(code: str, new_password: str) -> dict:
    """Spend a reset link and write the new password, or explain why not.

    **Both writes, one transaction.** The link and the password are one
    event: a spend that did not change the password strands the member with
    a dead link and their old credential, and a password changed without
    the link spent leaves a live credential sitting behind a change it
    already paid for. Neither is a state worth having, so neither is
    reachable.

    The password goes through :func:`~reeltalk.social.passwords.set_password`
    rather than being written here, which is what makes an admin-set change
    and a self-service change the same event with the same consequences.
    ``changed_by`` is the account itself, so no notice is mailed: the
    person who just typed a new password does not need to be told they
    typed it, and the reset mail they already opened is the record of this
    event for them.

    The address binding is checked before anything is written. A link minted
    for one address cannot reset another's password even if the admin has
    since moved the address (R120's binding, which matters more here than
    on verification because what it protects is the account itself).
    """
    with transaction.atomic():
        token = PasswordResetToken.lock_live(code)
        if token is None:
            # Re-derive the reason for the log; the page already showed it
            # on the GET that preceded this POST.
            looked_up = PasswordResetToken.objects.filter(code=code).first()
            reason = _dead_outcome(looked_up) if looked_up is not None else "invalid"
            logger.info("Password reset rejected: link %s.", reason)
            return {"changed": False, "reason": reason}

        if token.email != token.user.email:
            logger.info(
                "Password reset rejected for @%s: the link was minted for "
                "%s and the account is now at %s.",
                token.user.localname,
                token.email,
                token.user.email,
            )
            return {"changed": False, "reason": "mismatch"}

        set_password(token.user, new_password, changed_by=token.user)
        token.mark_used()

    logger.warning(
        "Password reset completed for @%s. All sessions that existed before "
        "this moment are now invalid.",
        token.user.localname,
    )
    return {"changed": True, "reason": "", "localname": token.user.localname}


def build_reset_email(user, token) -> EmailMessage:
    """The message a member opens to recover their own account.

    Carries the link and nothing else that matters. Like the verification
    mail it has **no unsubscribe link** (R121) — this is a transactional
    message the recipient asked for about their own account, and making it
    unsubscribable would let a member opt out of the one mail their ability
    to recover the account depends on.

    It says plainly that the address must already be verified for the link
    to be worth anything, so a member who somehow receives one on an
    unverified account is not sent chasing a link that cannot help them.
    """
    from .models import PASSWORD_RESET_TTL_HOURS

    context = {
        "site_name": _site_name(),
        "localname": user.localname,
        "reset_url": reset_url(token),
        "ttl_hours": PASSWORD_RESET_TTL_HOURS,
    }
    body = render_to_string("social/email/password_reset.txt", context)
    return EmailMessage(
        subject=f"[{_site_name()}] Reset your password",
        body=body,
        to=[token.email],
    )


def _site_name() -> str:
    return SiteSettings.get_instance().name or "ReelTalk"


def _enqueue_send(token_pk: int) -> None:
    """Put the one send on the cluster; deferred import as everywhere else."""
    from reeltalk.social.tasks import enqueue_password_reset_email

    enqueue_password_reset_email(token_pk)


__all__ = [
    "AddressMismatchError",
    "RESET_ADDRESS_COOLDOWN_MINUTES",
    "RESET_IP_LIMIT",
    "RESET_IP_WINDOW_MINUTES",
    "RESET_PATH",
    "build_reset_email",
    "classify_reset_code",
    "complete_reset",
    "request_reset",
    "reset_address_in_cooldown",
    "reset_ip_budget_spent",
    "reset_url",
]
