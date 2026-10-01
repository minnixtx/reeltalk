"""Starting the verification mail, and the only writer of an address (2F-2).

Two jobs live here, and they are related by being the two ways an address
either gets proven or gets moved.

**The send.** :func:`send_verification_email` is the single entry point for
"please prove this address". It decides, guards, mints and enqueues. It is
one function rather than a snippet inlined at each of the three creation
routes (signup, the setup wizard, invite acceptance) for the 2E lesson:
where there are three call sites there are three places to forget, and
forgetting here is silent — the account is still created, it just never
gets a link, and nothing about it looks broken.

**The write.** :func:`change_email` is the only path that changes a
member's address (R125). The tamper notice fires from *inside* it, so an
address change without an alert is not a thing a caller can do — not from
the admin, not from a management command, not from an import hook. That is
the same lesson applied to a new write: an alert that lives in the admin
view is an alert that the next writer silently omits.

Nothing in this module sends anything. Every send is enqueued, on 2E's rule
that the request which created the account must not be at the mercy of a
mail server: a dead SMTP host cannot roll back a signup, slow it down, or
surface an error to the person who just signed up. What *is* in the request
is the **guard**, and that asymmetry is deliberate — see
:func:`_shout_if_console_backend` for why the one thing that must happen
synchronously is the warning.
"""

import logging
from datetime import timedelta

from django.core.mail import EmailMessage
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from reeltalk.activitypub.identity import absolute_uri
from reeltalk.moderation.notify import console_backend_active

from .models import EmailVerificationToken, SiteSettings, User

logger = logging.getLogger("reeltalk.social.verify")

# The path the verify link points at. 2F-3 binds a route here; until it
# does, the link in the mail 404s, which is the honest state of an increment
# that ships the mail before the page. It is a constant in one place rather
# than a literal in a template because it is a **contract between two
# increments** — the thing a test can pin so that 2F-3's route and 2F-2's
# mail cannot silently drift apart.
VERIFY_PATH = "/account/verify/"

# The path of the logged-out resend page, which is also where signup, the
# setup wizard and invite acceptance send a person once they have stopped
# logging people in (R119). A constant for the same reason as ``VERIFY_PATH``:
# three creation routes and one recovery page have to agree on it without any
# of them hard-coding a string.
RESEND_PATH = "/account/verify/resend/"

# R122's two throttle axes. Both are floors under the abuse surface this
# increment creates, and neither is the antispam system ``PLAN.md`` §5
# describes — R107 established that dedup is not rate limiting, and the same
# distinction applies here: five minutes per address still admits roughly 288
# mails a day to one victim from a caller who wants them.
#
# The per-address window is deliberately short rather than generous. It is not
# protecting the mailbox from volume — the per-IP axis is closer to that — it
# is protecting the *member* from a page that can be pressed repeatedly and
# from a mail client that fires the form more than once.
RESEND_ADDRESS_COOLDOWN_MINUTES = 5
# Per-source, shaped on Mastodon's ``rack_attack.rb`` 25-per-5-minutes. This
# is the axis the per-address one does not have: a caller cycling through many
# addresses is completely unthrottled by a cooldown keyed on the address, and
# that is the cheap direction for an abuse caller.
RESEND_IP_LIMIT = 25
RESEND_IP_WINDOW_MINUTES = 5


def verification_url(token) -> str:
    """The link that goes in the verification mail.

    Minted from ``settings.CANONICAL_ORIGIN`` via :func:`absolute_uri`,
    never from the request. A verification link has to be openable by
    whoever receives the mail, and on this instance that may be nobody on
    the LAN the admin happens to be browsing from — the same reasoning R108
    applied to every published ActivityPub identifier, applied to a
    credential that leaves the box inside an email.
    """
    return absolute_uri(f"{VERIFY_PATH}{token.code}/")


def _site_name() -> str:
    """The instance's own name, for the subject line of both mails.

    ``SiteSettings.get_instance()`` creates the singleton with defaults on
    first use, so a half-provisioned instance still yields a name and a
    verification mail never fails over a missing settings row.
    """
    return SiteSettings.get_instance().name or "ReelTalk"


def _shout_if_console_backend(what: str, count: int) -> None:
    """Warn, in the calling request, that the backend cannot deliver.

    Called from the request that created the account rather than only from
    the worker, because 2F trap 1 is worse than 2E's version of itself. The
    console backend **reports success** — printing to stdout *is* a
    successful send as far as Django is concerned — so with no guard a
    signup silently produces an account that can never be verified, and a
    green task result means PRINTED rather than DELIVERED. In 2E that meant
    an alert nobody received. Here it means an account nobody can use, and
    once 2F-3 puts the gate up it means an account nobody can sign into at
    all. The person who needs to know is standing at the signup form, so
    that is where the noise goes.
    """
    if not console_backend_active():
        return
    console_name = "django.core.mail.backends.console.EmailBackend"
    logger.warning(
        "VERIFICATION EMAIL WILL NOT BE DELIVERED: the mail backend is %s, "
        "which prints messages to stdout instead of sending them. %s %s "
        "just got enqueued and will reach nobody, so the account cannot be "
        "verified. Set EMAIL_HOST (and the other EMAIL_* keys) in .env and "
        "rebuild the images to actually send. This warning is deliberate: a "
        "silently-undelivered verification link is indistinguishable from a "
        "feature that was never built.",
        console_name,
        count,
        what,
    )


def build_verification_email(user, token) -> EmailMessage:
    """The message a member gets to prove their own address.

    Carries the link and nothing else that matters. It deliberately carries
    **no unsubscribe link** (R121): this is a transactional mail proving the
    recipient's own address to the service they just signed up for, and
    making it unsubscribable would mean a recipient could opt out of the one
    message their account depends on. Unsubscribe belongs to the staff
    report mail, whose recipient is logged out, whose link may be prefetched
    by a mail scanner, and whose one-click semantics are a different thing
    in a different threat model.
    """
    from reeltalk.social.models import EMAIL_VERIFICATION_TTL_HOURS

    context = {
        "site_name": _site_name(),
        "localname": user.localname,
        "verify_url": verification_url(token),
        "ttl_hours": EMAIL_VERIFICATION_TTL_HOURS,
    }
    body = render_to_string("social/email/verify_email.txt", context)
    return EmailMessage(
        subject=f"[{_site_name()}] Verify your email address",
        body=body,
        to=[token.email],
    )


def send_verification_email(user, request_ip: str = "") -> dict:
    """Ask ``user`` to prove the address on their account, if we can.

    The single entry point. Returns a small dict — ``{"enqueued": n,
    "reason": ...}`` — so a caller and a test can see *why* nothing
    happened instead of inferring it from an empty outbox.

    **There is no address check here, on purpose.** :meth:`EmailVerificationToken.mint`
    owns that check, because it is the only writer of the binding and a
    duplicated predicate in the caller is a second thing to keep in step.
    What this function does is translate mint's refusal into a logged no-op
    rather than a 500: ``SignupForm.email`` is still ``required=False``
    until a later increment, so an address-less signup is a normal,
    working thing today and must stay working. Swallowing the exception is
    not the point — **logging it loudly is**, because that account is the
    one that will not be able to sign in once 2F-3 goes up, and the operator
    needs to have been told at the moment it was created.

    ``request_ip`` rides through to the token row so the per-source throttle
    and the admin's audit view can see who asked.
    """
    try:
        token = EmailVerificationToken.mint(user, request_ip=request_ip)
    except ValueError as exc:
        logger.warning(
            "No verification email for @%s: %s The account was created "
            "without an address, so there is nothing to verify and — once "
            "email verification is enforced — nothing that would let it "
            "sign in. An address has to be set by the site admin and a "
            "verification mail triggered.",
            user.localname,
            exc,
        )
        return {"enqueued": 0, "reason": "no-address"}

    _shout_if_console_backend("verification email(s)", 1)

    token_pk = token.pk
    transaction.on_commit(lambda: _enqueue_send(token_pk))
    logger.info(
        "queued verification email for @%s to %s (token %s)",
        user.localname,
        token.email,
        token.code[:8],
    )
    return {"enqueued": 1, "reason": ""}


def address_in_cooldown(email: str) -> bool:
    """True when a verification mail for ``email`` is too recent to send another.

    The cooldown is the token table itself, which is what makes it unbypassable
    by clearing cookies: it is keyed server-side on the address, not on
    anything the client holds.

    **Why ``send_error=""`` is in the filter, and why that is not an oddity.**
    The window counts a mail that was sent *or is still on its way*, and
    excludes one that already failed. A failed send must not make the member
    wait out a window for mail that never arrived — that is the reason
    ``sent_at`` was added in 2F-2 rather than reading ``created_at`` alone.
    Excluding failures does not open a bypass, because a failed send is not a
    thing the caller can arrange: it takes a broken transport, which is the
    one state this page is not trying to throttle.
    """
    if not email:
        return False
    since = timezone.now() - timedelta(minutes=RESEND_ADDRESS_COOLDOWN_MINUTES)
    return EmailVerificationToken.objects.filter(
        email=email, created_at__gt=since, send_error=""
    ).exists()


def ip_budget_spent(ip: str) -> bool:
    """True when this source has already minted its limit of mails recently.

    Counted from the token table rather than a cache so the limit survives a
    deploy and is shared by every web process — see
    ``EmailVerificationToken.request_ip`` for why that mattered more than the
    simplicity of ``cache.incr``.
    """
    if not ip:
        return False
    since = timezone.now() - timedelta(minutes=RESEND_IP_WINDOW_MINUTES)
    return (
        EmailVerificationToken.objects.filter(
            request_ip=ip, created_at__gt=since, send_error=""
        ).count()
        >= RESEND_IP_LIMIT
    )


def request_resend(raw_email: str, ip: str) -> dict:
    """A logged-out request to re-send a verification link (R122).

    **The caller's response must never vary with whether the address exists.**
    This function therefore returns a reason for the *log*, and the view
    renders one uniform sentence whatever comes back. Several of the reasons
    below are indistinguishable from outside by design: ``no-account`` and
    ``address-cooldown`` cannot both be visible, because "we just sent one"
    is a confirmation that the address is registered, and that is the one
    enumeration surface R122 exists to close.

    **What the per-IP axis bounds, precisely.** It bounds *mail* from a
    source, because that is the cost worth bounding and the thing the token
    table can count. A flood of addresses that match no account mints
    nothing, so it never trips this axis — each such request costs one
    indexed lookup and gets the same uniform page. Request-level flooding of a
    near-noop endpoint is the separate, still-unbuilt antispam layer R107
    points at, and this increment does not pretend to have answered it.
    """
    email = (raw_email or "").strip().lower()
    if not email:
        return {"enqueued": 0, "reason": "no-address-given"}

    # The source limit is checked before the account lookup on purpose: it is
    # the defence that has to hold even when everything behind it is being
    # exercised as hard as it can.
    if ip_budget_spent(ip):
        logger.warning(
            "Verification resend refused: %s is over the limit of %d per %d "
            "minutes. Uniform response sent.",
            ip or "(unknown source)",
            RESEND_IP_LIMIT,
            RESEND_IP_WINDOW_MINUTES,
        )
        return {"enqueued": 0, "reason": "ip-limited"}

    user = User.objects.filter(email=email).first()
    if user is None:
        # Silent by design. This is the branch that must be indistinguishable
        # from a successful send, so it logs at info and says nothing more
        # than the fact.
        logger.info("Verification resend requested for an unknown address.")
        return {"enqueued": 0, "reason": "no-account"}

    if address_in_cooldown(email):
        logger.info(
            "Verification resend for @%s is inside the %d-minute cooldown; "
            "no new mail.",
            user.localname,
            RESEND_ADDRESS_COOLDOWN_MINUTES,
        )
        return {"enqueued": 0, "reason": "address-cooldown"}

    result = send_verification_email(user, request_ip=ip)
    if result["enqueued"]:
        logger.info("Verification resend queued for @%s.", user.localname)
    return result


def _enqueue_send(token_pk: int) -> None:
    """Put the one send on the cluster.

    Deferred import so that importing this module never pulls in the task
    runner, same as :mod:`reeltalk.moderation.notify` does.
    """
    from reeltalk.social.tasks import enqueue_verification_email

    enqueue_verification_email(token_pk)


def change_email(user, new_email, *, changed_by) -> dict:
    """Move ``user``'s address, and tell the old one that it happened.

    **The only writer of an address** (R125). Every path that changes one —
    the admin's user form today, a management command or an import hook
    tomorrow — goes through here, and the notice to the previous address
    fires from inside this function rather than from any caller. That
    placement is the whole control: an alert written in the admin view is an
    alert that the next writer omits by accident, and on this instance the
    notice is the only signal a member ever gets that their mail now lands
    somewhere else.

    Three things happen on a real change, in one transaction:

    1. the address is written (normalised by ``User.save()``, like every
       other write);
    2. the account's **live tokens are superseded**. Safety does not need
       this — ``consume()`` refuses a token whose bound address no longer
       matches, so an old link can never verify the new address. What it
       buys is that the state is unreachable rather than handled: no live
       credential sits in a mailbox pointing at an address this account no
       longer holds, and nobody spends a click on a link that can only
       produce a mismatch error. The member is not stranded by this — the
       logged-out resend route (2F-3) mints on demand, and the notice they
       just received is what tells them to.
    3. the notice is enqueued to the **old** address.

    Skipped when the old address is empty: there is nobody to tell.

    ``changed_by`` is required and is never merely decorative — it is the
    audit answer to who moved a member's mail. It is deliberately **not**
    named in the mail: the notice goes to an address that may belong to a
    stranger (a corrected typo, a hijacked account), and putting another
    member's handle in it is a leak with no upside. The mail says "the site
    administrator", which is the truth and nothing more.
    """
    new_email = (new_email or "").strip().lower()

    if user.pk is None:
        # Not a change, a first write. Nothing to notify and nothing live
        # to supersede; the normal creation flow owns this account.
        user.email = new_email
        user.save(update_fields=["email"])
        return {"changed": True, "notified": "", "reason": "no-prior-row"}

    old_email = User.objects.filter(pk=user.pk).values_list("email", flat=True).first()
    old_email = old_email or ""
    if old_email == new_email:
        # No write, no supersede, no alert. A form that resubmits an
        # unchanged address must not produce a tamper notice, or the admin
        # saving an unrelated field would mail the member a false alarm
        # about a change that never happened.
        return {"changed": False, "notified": "", "reason": "unchanged"}

    now = timezone.now()
    with transaction.atomic():
        user.email = new_email
        user.save(update_fields=["email"])
        closed = (
            EmailVerificationToken.live().filter(user=user).update(superseded_at=now)
        )
        if old_email:
            transaction.on_commit(lambda: _enqueue_notice(user.pk, old_email))

    # The verified state needs no handling here. ``User.email_verified`` is
    # derived from ``verified_email == email``, so writing a new address
    # invalidates the old proof by itself — no hook, no change detection,
    # nothing for a future writer to forget. R125's notice is the separate
    # control: the derivation protects the system from trusting a stale
    # proof, the notice protects the member from not knowing their mail
    # moved. Neither covers the other.
    notice_note = (
        f"Notice enqueued to {old_email}."
        if old_email
        else "No notice: no prior address."
    )
    logger.warning(
        "Email address changed for @%s: %s -> %s by @%s. %s live "
        "verification token(s) superseded. %s",
        user.localname,
        old_email or "(none)",
        new_email or "(none)",
        getattr(changed_by, "localname", "?"),
        closed,
        notice_note,
    )
    return {
        "changed": True,
        "notified": old_email,
        "reason": "",
        "superseded": closed,
    }


def build_address_change_email(user, old_email) -> EmailMessage:
    """The R125 tamper notice, to the address that used to be on file.

    **A notice, not an action.** It carries no link of any kind — not to
    undo the change, not to the instance, not to a support page. The
    recipient is an address that an attacker may still control: if the
    change was hostile, this mail is the one thing guaranteed to reach the
    person being harmed, and it is also guaranteed to reach whoever the
    attacker put in charge of that mailbox. A privileged link here would be
    an unauthenticated change primitive delivered to the wrong party, which
    is worse than the silence this control replaces. The absence of any URL
    in the body is asserted in the test suite rather than trusted.
    """
    context = {
        "site_name": _site_name(),
        "localname": user.localname,
        "old_email": old_email,
        "new_email": user.email,
        "changed_at": timezone.now(),
    }
    body = render_to_string("social/email/email_changed.txt", context)
    return EmailMessage(
        subject=f"[{_site_name()}] Your email address was changed",
        body=body,
        to=[old_email],
    )


def _enqueue_notice(user_pk: int, old_email: str) -> None:
    """Deferred for the same reason as :func:`_enqueue_send`."""
    from reeltalk.social.tasks import enqueue_address_change_notice

    enqueue_address_change_notice(user_pk, old_email)
