"""Credential-surface throttling for sign-in, admin sign-in, and signup.

Nothing here was true before this module: the only throttles in the codebase
(§2F's resend, §2G's reset) guard **outbound mail**, and a failed sign-in
or a signup attempt was uncounted and unlimited. This closes the three
credential surfaces, on the shape the mail throttles already established —
a count read from a Postgres table rather than a cache — because the reason
mail used the table applies here too: it is shared by every web process and
survives every restart, where a LocMemCache counter would reset on each
deploy and disagree between workers, which is a control that reads as
protection and is not.

**The axis is the source address, and only that.** A per-address budget
stops one machine hammering and cannot lock a real user out of their own
account. A per-account budget would catch an attacker spread across many
addresses, but it hands that attacker a lever to lock the real owner out —
and this instance has one admin with no second admin, so a lock on the
admin account that only an admin can clear has no clearing hand short of a
shell on the server. The spread case is priced as the cost of not building
the lockout lever. Every limit here keys on ``client_ip()``, never a raw
header, for the reason R127 made real: a budget keyed on a forgeable
address punishes only the honest.

**Three separate budgets, never one.** Login, admin login and signup each
get their own constants and their own count, so filling one never starves
another. This is the same lesson §2G learned when reset and resend were
given separate counters: a shared budget turns one unauthenticated route
into a denial-of-service against a different one.

**What a blocked address sees, and why it is safe to say it.** The reveal
message names the *address* and the wait, never an account, so it leaks
nothing about which accounts exist — the enumeration oracle §2F/§2G were
careful not to open. Revealing the wait rather than a bare refusal matters
because the person who needs it most is a legitimate user on a busy shared
address whose own correct credentials never even ran: a silent block reads
to them as a broken password, and they keep trying.
"""

import math
from datetime import timedelta

from django.utils import timezone

from reeltalk.social.models import CredentialAttempt

# Per-surface budgets. Three numbers, not one, because the three surfaces
# face different threats — see the module docstring and the notes below.
#
# Sign-in bounds a guessed password against accounts that already exist.
# Ten per quarter hour is generous enough that a real person fumbling a
# password a couple of times is never stopped, and slow enough that
# guessing against this instance's small account base is not worth doing.
LOGIN_IP_LIMIT = 10
LOGIN_IP_WINDOW_MINUTES = 15

# Admin sign-in is bounded tighter than the public one: the door guards the
# whole instance, and a legitimate admin does not mistype ten times inside
# a quarter hour. Same window as the public login so the two read side by
# side; only the count differs.
ADMIN_LOGIN_IP_LIMIT = 5
ADMIN_LOGIN_IP_WINDOW_MINUTES = 15

# Signup bounds a different thing entirely: volume and enumeration, not a
# guessed password. It is counted on every attempt that reaches the
# name/email uniqueness checks (see ``social_views.signup``), so a bot
# walking a list of names through the "that name is already taken" oracle
# is bounded whether or not each name is free — counting only successful
# creations would let the walk run for free on already-taken names. The
# window is wider because registration is a rare, deliberate act: a person
# signs up once, so five in an hour is no friction for them and a hard cap
# on a bot.
SIGNUP_IP_LIMIT = 5
SIGNUP_IP_WINDOW_MINUTES = 60

_BUDGETS = {
    CredentialAttempt.LOGIN: (LOGIN_IP_LIMIT, LOGIN_IP_WINDOW_MINUTES),
    CredentialAttempt.ADMIN: (
        ADMIN_LOGIN_IP_LIMIT,
        ADMIN_LOGIN_IP_WINDOW_MINUTES,
    ),
    CredentialAttempt.SIGNUP: (SIGNUP_IP_LIMIT, SIGNUP_IP_WINDOW_MINUTES),
}


def _budget(surface: str) -> tuple[int, int]:
    """``(limit, window_minutes)`` for a surface.

    Raises rather than defaulting: a surface with no budget is a wiring bug,
    and defaulting it to "unlimited" would turn a new credential surface
    into a silently-unthrottled one — the exact half-finished state this
    module exists to prevent.
    """
    try:
        return _BUDGETS[surface]
    except KeyError:
        raise ValueError(f"no throttle budget defined for surface {surface!r}")


def record_attempt(surface: str, ip: str) -> None:
    """Count one attempt from ``ip`` against ``surface``'s budget.

    A no-op on an empty address. An unparseable source is deliberately not
    throttled: counting every unparseable source into one shared empty-string
    bucket would let a single such caller lock out every other one with it,
    which is a denial-of-service invented by the throttle itself. This
    matches the mail helpers, which likewise refuse to key a limit on
    nothing.
    """
    if not ip:
        return
    CredentialAttempt.objects.create(surface=surface, source_ip=ip)


def attempts_blocked(surface: str, ip: str) -> bool:
    """True when ``ip`` has spent ``surface``'s budget inside its window.

    The mirror of ``verify.ip_budget_spent``: one indexed count against the
    attempts table, no cache, so every web process gives the same answer and
    the answer survives a restart.
    """
    if not ip:
        return False
    limit, window = _budget(surface)
    since = timezone.now() - timedelta(minutes=window)
    return (
        CredentialAttempt.objects.filter(
            surface=surface, source_ip=ip, created_at__gt=since
        ).count()
        >= limit
    )


def retry_at(surface: str, ip: str):
    """When this address's oldest counted attempt ages out of the window.

    The sliding window drops below the limit the moment its oldest
    still-counted attempt is older than the window, so that instant is
    ``oldest + window``. Returns ``None`` when nothing is counted, so a
    caller never shows a wait with no cause. Only worth calling once
    ``attempts_blocked`` is True.
    """
    if not ip:
        return None
    _, window = _budget(surface)
    since = timezone.now() - timedelta(minutes=window)
    oldest = (
        CredentialAttempt.objects.filter(
            surface=surface, source_ip=ip, created_at__gt=since
        )
        .order_by("created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    if oldest is None:
        return None
    return oldest + timedelta(minutes=window)


def clear_attempts(surface: str, ip: str) -> None:
    """Drop this address's counted attempts for ``surface``.

    Called on a successful sign-in so a person who knows their password is
    never punished for the failures that came before it. The signup surface
    is never cleared this way — a successful registration is not a reason
    to hand a bot a fresh budget — so for signup the window alone frees the
    address.
    """
    if not ip:
        return
    CredentialAttempt.objects.filter(surface=surface, source_ip=ip).delete()


def reveal_message(retry_at_moment) -> str:
    """The sentence a blocked address sees.

    Names the address and the wait, never an account, so it discloses
    nothing about which accounts exist. The wait is rounded **up** to whole
    minutes so the person is never told a time at which retrying still
    fails.
    """
    if retry_at_moment is None:
        return "Too many attempts from this address. Please try again later."
    minutes = (retry_at_moment - timezone.now()).total_seconds() / 60.0
    whole = max(1, math.ceil(minutes))
    if whole == 1:
        return "Too many attempts from this address. Try again in a minute."
    return f"Too many attempts from this address. Try again in {whole} minutes."


__all__ = [
    "ADMIN_LOGIN_IP_LIMIT",
    "ADMIN_LOGIN_IP_WINDOW_MINUTES",
    "LOGIN_IP_LIMIT",
    "LOGIN_IP_WINDOW_MINUTES",
    "SIGNUP_IP_LIMIT",
    "SIGNUP_IP_WINDOW_MINUTES",
    "attempts_blocked",
    "clear_attempts",
    "record_attempt",
    "retry_at",
    "reveal_message",
]
