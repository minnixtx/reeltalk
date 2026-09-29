"""The instance representative — who we are when we speak as the server (R104).

A forwarded report cannot carry the member who filed it. R104's reason is a
doxxing vector: revealing a reporter to a foreign admin hands that admin a
person to retaliate against, and the reporter never consented to being
introduced to a stranger's moderation team. So the ``actor`` of an outbound
``Flag`` is neither the reporter nor the moderator who pressed the button —
it is an identity that belongs to the instance itself.

**Why a dedicated account rather than the site admin.** The admin is a
person. If the Flag's actor were the admin's actor URL, every forwarded
report would publish a real human's identity as the reporting party, and
that person's profile, key and history become the instance's federation
identity for moderation. Mastodon reaches the same conclusion and keeps a
non-human actor for it (``Account.representative`` is an ``Application``
named ``mastodon.internal``, created on demand and never shown to users).
This is the same shape: a local ``User`` row that exists to hold the
instance's signing identity for server-level statements.

**Why the localname is ``_instance``.** R12's signup rule requires a
localname to *start with a letter or a digit*
(``^[a-zA-Z0-9][a-zA-Z0-9._-]*$``). A leading underscore is therefore
invalid at signup, so this name is reserved by the charset rule that
already exists rather than by a new denylist that a future session could
forget, narrow, or bypass through a path the denylist was not applied to.
Reserving by construction beats reserving by convention.

**It cannot sign in.** It is created with no password, which Django stores
as an unusable password, so there is no credential that opens a session as
this account. It holds no ``is_staff`` and no ``is_moderator``: it is a
signing identity, not a user of the site, and it never has a session at all.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError

from reeltalk.social.models import User

# Unregistrable at signup by R12's "must start with a letter or digit"
# rule — see the module docstring. Changing this value changes the published
# actor URL, so it is a federation-identity change, not a rename.
INSTANCE_ACTOR_LOCALNAME = "_instance"

INSTANCE_ACTOR_DISPLAY_NAME = "Instance representative"


def instance_representative() -> User:
    """The local account that acts as this instance, created on first use.

    Idempotent, and the race is handled by the unique ``localname`` rather
    than by a check-then-create that can lose: two forwards firing at once
    both try to create, one wins, the other re-reads the winner.

    **The password is explicitly unusable, and it has to be.** Creating this
    row through ``get_or_create`` would call the plain manager's
    ``create()``, which never runs ``set_password`` at all — leaving the
    field at its empty-string default. An empty password is *not* what
    Django considers unusable: ``is_password_usable`` only rejects
    ``None`` and a ``!``-prefixed hash, so a blank field reads as usable
    and the instance's signing identity becomes an account with a
    guessable credential. So the row goes through ``create_user`` with
    ``password=None``, which routes into ``make_password(None)`` and
    stores a real unusable hash, and the check is re-applied on every call
    so a row that somehow acquired a usable password cannot keep it.
    """
    existing = User.objects.filter(localname=INSTANCE_ACTOR_LOCALNAME).first()
    if existing is not None:
        return _ensure_unusable(existing)
    try:
        created = User.objects.create_user(
            localname=INSTANCE_ACTOR_LOCALNAME,
            password=None,
            display_name=INSTANCE_ACTOR_DISPLAY_NAME,
            local=True,
        )
    except IntegrityError:
        return _ensure_unusable(User.objects.get(localname=INSTANCE_ACTOR_LOCALNAME))
    return _ensure_unusable(created)


def _ensure_unusable(user: User) -> User:
    """Store an unusable password if this row somehow has a usable one."""
    if user.has_usable_password():
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user


def is_instance_actor(user) -> bool:
    """Whether ``user`` is the instance representative.

    Compared on the localname rather than on a flag, so there is no second
    field to keep in step with the row. A remote mirror can never match:
    mirror localnames always carry a ``@host`` suffix, and this one has
    none.
    """
    return getattr(user, "localname", None) == INSTANCE_ACTOR_LOCALNAME


def validate_instance_localname(localname: str) -> None:
    """Refuse a signup that tries to take the representative's name.

    Not wired into :class:`~reeltalk.social.forms.SignupForm` today,
    because R12's charset rule already rejects ``_instance`` — a leading
    underscore cannot start a valid localname. This exists so the guard is
    stated where a reader looking for it will find it, and so a future
    widening of the signup charset is caught by a test rather than
    discovered as a takeover.
    """
    if (localname or "").strip().lower() == INSTANCE_ACTOR_LOCALNAME:
        raise ValidationError("That name is reserved for the instance itself.")
