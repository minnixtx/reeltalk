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
    """

    activity_id = models.CharField(max_length=2048, unique=True)
    delivered_date = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name_plural = "delivered activities"

    def __str__(self) -> str:
        return self.activity_id
