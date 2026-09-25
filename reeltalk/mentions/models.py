"""Which users a status mentioned, as stored rows (§2C).

Stored rather than re-parsed at read time, for three reasons that each sink a
re-parse on their own:

* A remote mirror keeps ``raw_content=""`` — the wire carries rendered HTML,
  so a mirror has no markdown left to re-parse at all.
* The edit path needs the *previous* set to know what changed. Re-parsing the
  new text says what is mentioned now, not what stopped being mentioned, and
  "what stopped" is the half an edit has to answer.
* Re-resolving at serialize time would let an account registered next week
  change what a post written today claims to have mentioned. The row freezes
  the answer at write time, which is the only version of it that was ever
  true.
"""

from django.db import models, transaction
from django.utils import timezone


class StatusMention(models.Model):
    """One (status, user) pair: this post addressed this member.

    ``status`` CASCADEs because a mention is a statement *about* a post; with
    the post gone hard there is nothing the row could still describe.
    ``Status.delete()`` is soft (R17), so in ordinary use the row outlives a
    deleted-looking post exactly as a notification does — the same edge
    notifications increment 3 found, and the reason a reader must check
    ``deleted`` rather than trust the FK.

    ``user`` CASCADEs because a mention names a person; when that account is
    deleted the claim that the post addressed it has no subject left.
    """

    status = models.ForeignKey(
        "core.Status",
        on_delete=models.CASCADE,
        related_name="mentions",
    )
    user = models.ForeignKey(
        "social.User",
        on_delete=models.CASCADE,
        related_name="mentions_received",
    )
    created = models.DateTimeField(default=timezone.now)

    class Meta:
        # Insertion order, not reverse-chronological: this ordering *is* the
        # order the parser found the handles in, which is the order the
        # outbound ``tag`` array wants. A timestamp-ordered tag array would
        # reshuffle a post's mentions on every read.
        ordering = ["id"]
        constraints = [
            # The same handle typed twice in one post is one mention. The
            # guard sits in the database rather than in the parser because
            # the parser is not the only thing that will ever write here,
            # and a duplicate row would be a duplicate delivery.
            models.UniqueConstraint(
                fields=["status", "user"],
                name="status_mention_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user.localname} mentioned in status {self.status_id}"


def sync_status_mentions(status, users) -> None:
    """Make ``status``'s mention rows exactly ``users``.

    Called at the two local status write sites *before* the broadcast, so
    the outbound ``tag`` array and the delivery audience are both built from
    what the saved text actually says rather than from a second parse of it.

    A **sync**, not an append, because ``mark_watched`` updates a review in
    place (D5): on the second save of a review the previous rows are
    already there, and blindly inserting would trip
    ``status_mention_unique``. Leaving the old rows alone is worse than the
    constraint — an edit that drops ``@bob`` would keep broadcasting an
    ``Update`` that tags him and delivers to his inbox, long after the text
    stopped addressing him.

    Rows that survive are left as they were rather than deleted and
    re-inserted, so a mention that persists across edits keeps its row and
    its id. That is also what makes increment 4's per-(recipient, status)
    idempotency cheap: the row it guards on is still the same row. The
    ordering consequence is worth stating — surviving rows keep the
    position they were first inserted at, so the ``tag`` order is
    first-mentioned, not re-sorted to match a later edit's wording.

    One transaction, because a half-applied sync would leave the wire
    describing a set that is neither the old one nor the new one.
    """
    wanted = list(users)
    current = set(status.mentions.values_list("user_id", flat=True))
    wanted_ids = {user.pk for user in wanted}
    stale = current - wanted_ids
    fresh = [user for user in wanted if user.pk not in current]
    with transaction.atomic():
        if stale:
            status.mentions.filter(user_id__in=stale).delete()
        if fresh:
            StatusMention.objects.bulk_create(
                StatusMention(status=status, user=user) for user in fresh
            )
