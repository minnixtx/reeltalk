"""Notifications: the event ledger and the unread contract (R90–R97).

A notification is a write that becomes a permanent read. It is created once,
by whichever producer saw the event, and read many times afterwards — by the
page, by the badge, and by whatever surface lands next. That asymmetry is why
every guard here sits on the write side rather than at render (R97(3)): a row
that exists but is filtered at render is a row that comes back the moment the
filter is dropped, and a count answered from a set every reader must
re-filter is two sources of truth behind one number.

``notify()`` is the single door (R97(2)). Six producers cannot each remember
three rules, and the code upstream makes the gap easy to miss: of the three
local producers, only ``_change_follow`` refuses a self-action. The like and
the reply carry no such guard, so a call site that *looks* guarded is no
evidence that its neighbours are.
"""

from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone


class Notification(models.Model):
    """One event, addressed to one local user.

    ``kind`` carries exactly the three events that have a producer on this
    instance (R90). The enum is narrow on purpose: a value with no producer is
    not a free placeholder but a code path nobody can trigger and a test that
    can only be written against a fiction.
    """

    class Kind(models.TextChoices):
        FOLLOW = "follow", "Follow"
        LIKE = "like", "Like"
        REPLY = "reply", "Reply"

    # CASCADE: a notification is addressed to a person and means nothing
    # without them. This is the one edge that decides how the table is read, so
    # it is also the edge the composite index below is built around.
    recipient = models.ForeignKey(
        "social.User",
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    # SET_NULL, not CASCADE: one member leaving must not erase what the rest of
    # the instance did. The row keeps its meaning — an event arrived on this
    # date from a member since deleted — and the page renders it without the
    # actor's deep link.
    actor = models.ForeignKey(
        "social.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications_about_them",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # SET_NULL because the event happened whatever later became of the post.
    # ``Status.delete()`` is soft, so this fires on a hard delete — an admin
    # changelist delete, which does go through queryset delete — and the page
    # renders such a row without its deep link.
    status = models.ForeignKey(
        "core.Status",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    # The ledger's own clock, not the event's. There is deliberately no second
    # "happened_at" column: what we act on is when we learned of the event, and
    # one timestamp therefore serves the page ordering, the badge range scan,
    # and the mark-read comparison. Two clocks would eventually disagree, and
    # the disagreement would be about which notifications you owe a read on.
    created = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created"]
        indexes = [
            # The one access path both readers want: the badge's
            # ``recipient = ? AND created > last_read`` range scan, and the
            # page's newest-first listing of one recipient's rows. A
            # single-column index on either half serves one of them and sorts
            # or scans the other.
            models.Index(
                fields=["recipient", "created"],
                name="notif_recipient_created_idx",
            ),
        ]

    def __str__(self) -> str:
        actor = self.actor.localname if self.actor else "someone"
        return f"{actor} {self.get_kind_display().lower()} → {self.recipient.localname}"

    @classmethod
    def unread_for(cls, user):
        """Notifications ``user`` has not seen yet (R93).

        The entire unread model is one timestamp comparison against the
        composite index, which is the same index the page's ordering wants.
        There is no per-row read state, and that is the decision rather than
        an omission: "mark all read" stays one UPDATE on the user row instead
        of an UPDATE whose cost and lock footprint grow with the unread count.
        The cost is that one item cannot be dismissed without marking
        everything older read.
        """
        return cls.objects.filter(
            recipient=user, created__gt=user.notifications_last_read
        )


def notify(recipient, actor, kind, status=None):
    """Record one event for ``recipient``, or record nothing.

    Every notification in the app comes through here, because the three
    invariants below are not the kind of thing six call sites can be trusted
    to each remember — and a producer that forgets one does not fail loudly,
    it writes a row that should not exist.

    Returns the row, or ``None`` when a guard stopped it. Callers must treat
    ``None`` as normal: liking your own post is not an error.

    The guards, and why each one is here rather than at the call site:

    * **No self-notification.** A self-like and a self-reply are both
      reachable — ``like_status`` and ``reply_to_status`` have no guard of
      their own — so a notification for your own like of your own post is a
      real row unless something stops it here.
    * **No remote recipient.** ``handle_like`` aimed at a mirror of another
      instance's post has ``status.user`` = a *remote* user. Notifying them
      writes a row no account on this instance will ever read or clear, and
      with two remote users of different instances interacting on our
      mirrored content that is the ordinary case, not the edge.
    * **No notification to someone who blocked the actor** — checked at write
      time, deliberately (R97(3)). A render-time filter leaves the row in the
      table, so a later unblock resurrects a notification for an interaction
      we chose not to deliver. This is not an invented write-side block rule:
      R85 refused one on the *like* write because the read path had none. Here
      the notification **is** the read surface, so filtering at its creation
      filters the read surface.
    """
    if recipient.pk == actor.pk:
        return None
    if not recipient.local:
        return None
    if recipient.blocks.filter(pk=actor.pk).exists():
        return None
    return Notification.objects.create(
        recipient=recipient, actor=actor, kind=kind, status=status
    )


def mark_all_read(user):
    """Mark everything addressed to ``user`` as read — one UPDATE on the user.

    Deliberately not an UPDATE across notification rows (R93): that query's
    cost and its row locks both grow with the unread count, which is the
    scaling behaviour the timestamp exists to avoid.

    The caller's instance is updated in place as well as the database, so a
    view can mark-read and then render the badge in the same request without
    re-reading the user and getting the stale value back.
    """
    now = timezone.now()
    get_user_model().objects.filter(pk=user.pk).update(notifications_last_read=now)
    user.notifications_last_read = now
    return now
