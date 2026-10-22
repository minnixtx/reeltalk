"""Per-source floor on inbox POSTs that could not be authenticated (F7).

The inbox is the instance's one unauthenticated write surface: ``csrf_exempt``
by design, because the HTTP signature is the credential. Before this module a
POST was uncounted and unlimited no matter how many arrived from one address,
and a rejected POST was not free — ``resolve_sender`` dials out to fetch a
Person document for any syntactically valid keyid it has not seen, *before*
anything is known about the caller. So an unauthenticated attacker could make
this instance place outbound calls at a rate of their choosing, and pay for
them with our worker pool.

**What is counted: the POSTs that cost us something, not all POSTs.** The
obvious shape — count every inbox POST — spends the budget on the traffic
this instance most wants: a busy peer's legitimate deliveries to the shared
inbox. It also forces the limit to be so high that nothing under it is bound
(the plan's own note that the limit must be generous is that shape's symptom).
So this counts only a POST we could not authenticate *after* doing the work
authentication required: an unresolvable sender, or a signature that did not
verify. A successful delivery is not counted and cannot consume anyone's
budget, which is what lets the number sit low enough to bound the
amplification while a real peer never approaches it. This is the same rule
:mod:`reeltalk.social.attempts` already runs — count the failures, never the
traffic — read from the federation side.

Two consequences of counting after the work, stated plainly:

* The budget is still checked *before* the dial-out, which is the whole point:
  the Nth failure from an address stops triggering an outbound fetch, so the
  amplification is bounded at N rather than unbounded.
* A peer whose own configuration is broken (a rotated key, a clock skew) fills
  the budget with its own failures and then has valid deliveries refused with
  429 for the rest of the window. That is survivable because 429 is a
  *retryable* answer for the peers checked here — Mastodon's
  ``response_error_unsalvageable?`` treats 400-499 as final *except* 401, 408
  and 429 — so the peer backs off and resends rather than discarding the
  activity. That exclusion list is the reason the refusal here is 429 and not
  a 4xx: the other shape would silently delete the delivery on the peer.

**The axis is the source address, and only that.** Same reasoning as the
credential throttles: a per-address budget cannot hand an attacker a lever to
lock a real actor out of the federation, and it is keyed on ``client_ip()``
rather than a header, because a budget keyed on a forgeable address punishes
only the honest.

**What this does not claim.** One address is one address: an attacker with many
of them gets a budget each, exactly as with the credential throttles, and a
legitimate peer behind a shared egress address shares a budget with whoever
else is on it. The floor bounds the cheap single-source flood, which is what
F7 describes; it is not a distributed-attack defense.
"""

import math
from datetime import timedelta

from django.utils import timezone

from reeltalk.activitypub.models import InboxAttempt

# A failed-and-counted POST costs one outbound fetch plus a signature
# verification. Sixty of those per quarter hour from one address is far above
# anything a real peer's own retry schedule produces, and low enough that the
# outbound calls a single address can induce are bounded rather than open-ended.
INBOX_FAILURE_LIMIT = 60
INBOX_FAILURE_WINDOW_MINUTES = 15


def record_failure(ip: str) -> None:
    """Count one unauthenticated inbox POST from ``ip``.

    A no-op on an empty address. An unparseable source is deliberately not
    counted: putting every unparseable source into one shared empty-string
    bucket lets a single such caller lock out every other one with it, which
    is a denial-of-service invented by the control itself. The mail helpers and
    :mod:`reeltalk.social.attempts` refuse to key a limit on nothing for the
    same reason.
    """
    if not ip:
        return
    InboxAttempt.objects.create(source_ip=ip)


def inbox_blocked(ip: str) -> bool:
    """True when ``ip`` has spent the failure budget inside its window.

    One indexed count against a Postgres table, no cache: every web process
    gives the same answer and the answer survives a restart, which is the whole
    reason the table shape was chosen over a LocMemCache counter.
    """
    if not ip:
        return False
    since = timezone.now() - timedelta(minutes=INBOX_FAILURE_WINDOW_MINUTES)
    return (
        InboxAttempt.objects.filter(source_ip=ip, created_at__gt=since).count()
        >= INBOX_FAILURE_LIMIT
    )


def retry_after_seconds(ip: str) -> int:
    """Whole seconds until this address's oldest counted failure ages out.

    The sliding window drops below the limit the moment its oldest still-counted
    row is older than the window, so that instant is ``oldest + window``. Rounded
    **up** so a peer is never told a time at which retrying still fails; a peer
    with nothing counted gets 1 rather than 0, because a ``Retry-After`` of 0
    invites an immediate resend of the very request that was just refused.
    """
    if not ip:
        return 1
    since = timezone.now() - timedelta(minutes=INBOX_FAILURE_WINDOW_MINUTES)
    oldest = (
        InboxAttempt.objects.filter(source_ip=ip, created_at__gt=since)
        .order_by("created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    if oldest is None:
        return 60
    frees_at = oldest + timedelta(minutes=INBOX_FAILURE_WINDOW_MINUTES)
    return max(1, math.ceil((frees_at - timezone.now()).total_seconds()))


__all__ = [
    "INBOX_FAILURE_LIMIT",
    "INBOX_FAILURE_WINDOW_MINUTES",
    "inbox_blocked",
    "record_failure",
    "retry_after_seconds",
]
