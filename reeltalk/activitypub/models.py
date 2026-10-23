"""ActivityPub app models (M4 increment 4).

The inbox dedup record: one row per inbound activity wire id this instance
has accepted, so a redelivered activity is never processed twice (§3.6;
R41's "dedup by origin id"). This is the first model in the app — it had
none while it held only crypto/wire-type code.
"""

from django.db import models
from django.utils import timezone


class DeliveredActivity(models.Model):
    """An inbound activity this instance has already accepted.

    ``activity_id`` is the activity's wire id (its ``id`` field — a URL on
    the sender's instance, sometimes with a fragment). Unique: a redelivery
    hits the existing row and the inbox pipeline drops it as a duplicate
    instead of re-processing it.

    The table only ever grew (F7): one row per activity id, forever, with no
    retention at all. A dedup row earns its keep only while a legitimate
    redelivery could still arrive — a peer's own retry schedule, which is
    hours and not months — so ``reeltalk/activitypub/management/commands/
    prune_inbox.py`` deletes them past an operator-set age. Pruning reopens
    an old activity to reprocessing, which is why the retention default is
    generous and why every handler has to stay safe to run twice.
    """

    activity_id = models.CharField(max_length=2048, unique=True)
    delivered_date = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name_plural = "delivered activities"
        indexes = [
            # The retention prune reads this column alone, and on the table's
            # only other index (activity_id) a date range is unusable. Without
            # this the prune is a sequential scan of every row ever written,
            # which is a query that gets worse forever on the exact table F7
            # says is already too big.
            models.Index(fields=["delivered_date"]),
        ]

    def __str__(self) -> str:
        return self.activity_id


class InboxAttempt(models.Model):
    """One counted inbox POST, keyed on the address it arrived from.

    The inbox is the one unauthenticated write surface the instance has:
    ``csrf_exempt`` by design, because the signature is the credential, and
    before this table existed a POST was uncounted and unlimited no matter how
    many of them arrived from one address. That is F7's amplification — an
    unauthenticated caller presenting any syntactically valid keyid makes
    ``resolve_sender`` dial out to fetch a Person document, so the cost of a
    rejected request is not zero, and it is paid by our worker pool and by
    the peer we were tricked into dialling.

    Same shape as ``reeltalk.social.models.CredentialAttempt``, for the same
    reasons: a Postgres table rather than a cache, because a LocMemCache
    counter resets on every deploy and disagrees between workers, which is a
    control that reads as protection and is not. One row per POST with the
    window read from ``created_at`` at query time, so a spent address frees
    itself the moment its oldest counted row ages out and there is no stored
    lock for an admin to clear.

    Deliberately **not** the ``CredentialAttempt`` table: that one counts
    attempts at a credential, and its surfaces, its limits, and its
    clear-on-success behavior all follow from that. Federation traffic is not
    a credential attempt, and folding the two together would let a flood of
    unsigned POSTs starve a real user's sign-in budget — the exact
    shared-budget denial-of-service that §2G learned to separate.

    ``source_ip`` is always the value from ``client_ip()``, never a raw
    header; see :mod:`reeltalk.activitypub.inbox_throttle`.
    """

    source_ip = models.CharField(max_length=45)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            # Every read is "how many from this address since T", and the
            # prune deletes "everything older than T" — both walk this index.
            models.Index(fields=["source_ip", "created_at"]),
        ]

    def __str__(self) -> str:
        return self.source_ip


class MirrorProfileAttempt(models.Model):
    """The last time we tried to fill a given remote actor's profile (F12c).

    ``refresh_mirror_profile`` fetches a mirror's home Person document to fill
    a missing avatar or bio, and must not do it on every page view of a mirror
    whose home instance never fills those fields. The throttle used to live in
    LocMemCache, which is per-process and clears on deploy: N web workers could
    each fetch once per window, and a restart reopened every window at once. A
    row per actor is shared by every process and survives a deploy, so the
    window is a real window and not a per-worker suggestion.

    Deliberately **not** the append-only shape of ``InboxAttempt``. That table
    counts distinct events, so it grows one row per event and leans on a
    retention prune; here the question is only "did we try this actor recently",
    so one row per actor is updated in place. An append-only log keyed on actor
    would grow one row per page view of an un-fillable mirror forever — exactly
    the unbounded table F7 was raised for. ``actor_url`` is unique for that
    reason: the row is the throttle, not a record of each attempt.
    """

    actor_url = models.TextField(unique=True)
    attempted_at = models.DateTimeField()

    def __str__(self) -> str:
        return self.actor_url
