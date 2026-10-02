"""The only writer of a password on this instance (2G).

R129's fourth decision is that every password change kills the account's
sessions — admin-set or self-service, no exception — and the way a rule
with no exception actually holds is that there is one door. This is that
door, and it is the same discipline :func:`reeltalk.social.verify.change_email`
applies to addresses: **the consequence of the write lives inside the
write**, so no caller — not the admin, not a management command, not an
import hook added next year — can perform one and skip it.

**The eviction itself needs no code here, and that looks like an omission
until you know where it happens.** Django's ``get_user()`` runs on every
request and compares the ``_auth_user_hash`` stored in the session against
an HMAC of the user's *current* password. When the two stop matching it
flushes the session. So the moment ``set_password`` is saved, every live
session belonging to that account is dead on its next click — constant
per request, shared by every web process, correct across restarts, with no
window in which an old session survives. There is nothing to scan and no
index to add, which is why the alternative of walking the session table
was dropped: the framework already does the job at a better price.

What that means for this module is a prohibition rather than an
implementation. **Nothing here, or anywhere downstream of here, may call
``django.contrib.auth.update_session_auth_hash``.** That function exists
to keep the acting session alive across a password change — exactly the
exception R129 rules out. An admin who changes their own password through
the admin is logged out on their next click like any other member, and the
self-service flow never had a session to lose.

One path deliberately does not come through here: ``manage.py
changepassword``. It is a host-level act by whoever already owns the
server, and Django's own command writes the password directly. The session
eviction still applies to it, because the eviction hangs off the stored
hash rather than off this function. What it skips is the notice below,
which is the right trade — the notice exists to tell a member that
*somebody else* changed their credential, and a command they ran themselves
is not that event.
"""

import logging

from django.core.mail import EmailMessage
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from reeltalk.moderation.notify import console_backend_active

from .models import User

logger = logging.getLogger("reeltalk.social.passwords")


def validate_new_password(password: str) -> None:
    """Run the instance's password policy, as one call from any direction.

    Shared by the forms and by :func:`set_password` so the rule cannot be
    enforced at the keyboard and skipped at the door. The forms want it in
    ``clean()`` because that is where a person sees it; the writer wants it
    anyway because the writer's callers include code that has no keyboard.
    """
    from django.contrib.auth.password_validation import validate_password

    validate_password(password)


def set_password(user, new_password, *, changed_by=None) -> dict:
    """Replace ``user``'s password, and carry every consequence of doing so.

    **The single writer.** The admin's user form and the self-service reset
    both arrive here, which is what keeps the two flows from drifting: a
    future change to what a password change *means* — a new validator, a
    different notice, a wider eviction rule — lands on both at once because
    there is only one place for it to land.

    ``changed_by`` is required to be thought about and defaults to ``None``
    only so that a host-level caller can say "nobody is asking on behalf of
    an account". It is the audit answer to who moved a member's credential,
    and it decides whether the member gets mailed:

    * ``changed_by.pk == user.pk`` — a self-change. No notice. The person
      who typed the new password does not need to be told they typed it.
    * anything else, including ``None`` — somebody else moved this
      credential, and the member is told.

    That second branch is the whole reason this notice exists. Silently
    replacing a member's password is the obvious first move for an attacker
    holding an admin account, because it locks the real owner out and the
    real owner has no other way to find out. It is the same threat R125
    covers for addresses, arriving one field over.

    Raises :class:`~django.core.exceptions.ValidationError` when the new
    password fails the instance's policy, before anything is written.
    """
    validate_new_password(new_password)

    localname = user.localname
    with transaction.atomic():
        user.set_password(new_password)
        user.save(update_fields=["password"])
        # Every session for this account is now invalid. Nothing writes that
        # fact; Django reads it back off the password on the next request.
        # See the module docstring — this is the seam, and the rule for
        # keeping it working is that nobody calls update_session_auth_hash.
        notify = changed_by is None or changed_by.pk != user.pk
        if notify and user.email:
            changer = getattr(changed_by, "localname", None)
            transaction.on_commit(lambda: _enqueue_notice(user.pk, changer))

    actor = f"@{changed_by.localname}" if changed_by is not None else "host command"
    logger.warning(
        "Password changed for @%s by %s. All existing sessions for this "
        "account are now invalid and will be dropped on their next request. "
        "Notice %s.",
        localname,
        actor,
        "enqueued" if (notify and user.email) else "skipped",
    )
    return {
        "changed": True,
        "notified": bool(notify and user.email),
        "reason": "" if notify else "self-change",
    }


def build_password_changed_email(user) -> EmailMessage:
    """The notice that a member's password was replaced by someone else.

    **A notice, not an action** — the same shape R125 forces on the address
    change mail, for the same reason. It carries no link of any kind. The
    recipient is an address that may be reaching them while the person who
    changed the password still sits in the account, so anything clickable
    in here is something that person can act on too, and a "revert this
    change" link delivered to a mailbox an attacker can read is not a
    recovery path but a second change primitive.

    It also deliberately does not name *which* administrator did it. The
    member cannot act on that name, and putting another member's handle in a
    mail about a security event is a disclosure with no upside. "The site
    administrator" is the truth and the whole of what is useful.
    """
    context = {
        "site_name": _site_name(),
        "localname": user.localname,
        "changed_at": timezone.now(),
    }
    body = render_to_string("social/email/password_changed.txt", context)
    return EmailMessage(
        subject=f"[{_site_name()}] Your password was changed",
        body=body,
        to=[user.email],
    )


def _site_name() -> str:
    from .models import SiteSettings

    return SiteSettings.get_instance().name or "ReelTalk"


def _enqueue_notice(user_pk: int, changed_by_localname) -> None:
    """Put the notice on the cluster.

    Deferred import so importing this module never pulls in the task runner,
    the way :mod:`reeltalk.social.verify` and
    :mod:`reeltalk.moderation.notify` both do.
    """
    from reeltalk.social.tasks import enqueue_password_changed_notice

    if console_backend_active():
        logger.warning(
            "PASSWORD-CHANGED NOTICE WILL NOT BE DELIVERED: the mail backend "
            "prints to stdout instead of sending. The account @%s was changed "
            "by %s and its member will not be told. Set EMAIL_HOST in .env "
            "and rebuild to actually send.",
            User.objects.filter(pk=user_pk).values_list("localname", flat=True).first(),
            changed_by_localname or "a host command",
        )
    enqueue_password_changed_notice(user_pk)
