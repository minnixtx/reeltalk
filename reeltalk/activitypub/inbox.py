"""Inbound activity processing (M4 increment 4).

The decision layer of the inbox pipeline, kept separate from the views so
it is directly testable: dedup by the activity's wire id (R41), then
dispatch on the activity type. Increment 4 registers no handlers —
Follow/Undo(Follow) land in increment 5 and Create/Update/Delete in
increment 6 — so every well-formed activity is accepted, recorded, and
otherwise ignored. That is the spec-correct behavior for activity types an
instance does not support (§3.6: unknown activity/object types are ignored
gracefully — a remote sending quotations or book objects must not crash or
create content here): acknowledge with 202, create nothing, never raise on
an unfamiliar shape.
"""

from collections.abc import Callable
from typing import Any

from django.db import transaction

from .models import DeliveredActivity

# Activity types this instance acts on, mapped to their handlers (each takes
# the parsed activity dict and applies its effects). Increment 4 handles none
# yet; this is where increments 5/6 register theirs. Everything not in the
# registry — including types we will never support — is ignored gracefully.
HANDLERS: dict[str, Callable[[dict], None]] = {}


def process_inbound_activity(activity: Any) -> str:
    """Process one verified inbound activity. Returns the outcome.

    ``"duplicate"`` — the activity's wire id was already delivered, so
    nothing is re-processed; ``"ignored"`` — a well-formed activity of an
    unknown or not-yet-handled type (recorded for dedup, no content
    created); ``"handled"`` — a registered handler acted on it. Activities
    without a string ``id`` cannot be deduped and are processed (or ignored)
    without a record. The dedup row and the handler run in one transaction:
    a handler failure rolls the record back too, so the sender's retry is
    not blocked by its own failed delivery.
    """
    if not isinstance(activity, dict):
        return "ignored"
    activity_id = activity.get("id")
    with transaction.atomic():
        if isinstance(activity_id, str) and activity_id:
            _row, created = DeliveredActivity.objects.get_or_create(
                activity_id=activity_id
            )
            if not created:
                return "duplicate"
        activity_type = activity.get("type")
        handler = (
            HANDLERS.get(activity_type) if isinstance(activity_type, str) else None
        )
        if handler is None:
            return "ignored"
        handler(activity)
    return "handled"
