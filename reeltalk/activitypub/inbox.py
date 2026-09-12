"""Inbound activity processing (M4 increments 4-6).

The decision layer of the inbox pipeline, kept separate from the views so
it is directly testable: dedup by the activity's wire id (R41), then
dispatch on the activity type. Increment 5 registers the Follow and Undo
handlers (``follow``); increment 6 registers Create/Update/Delete
(``statuses``) for reviews, film objects, and shelf events. Activity types
with no registered handler — including types we will never support — are
ignored gracefully: that is the spec-correct behavior for shapes an instance
does not handle (§3.6: a remote sending quotations or book objects must not
crash or create content here): acknowledge with 202, create nothing, never
raise on an unfamiliar shape.
"""

from collections.abc import Callable
from typing import Any

from django.db import transaction

from .follow import handle_follow, handle_undo
from .models import DeliveredActivity
from .statuses import handle_create, handle_delete, handle_update

# Activity types this instance acts on, mapped to their handlers (each takes
# the parsed activity dict, the verified sender, and the request, and applies
# its effects). Increment 5 registers Follow + Undo; increment 6 adds
# Create/Update/Delete. Everything not in the registry — including types we
# will never support — is ignored gracefully.
HANDLERS: dict[str, Callable[[dict, Any, Any], None]] = {
    "Follow": handle_follow,
    "Undo": handle_undo,
    "Create": handle_create,
    "Update": handle_update,
    "Delete": handle_delete,
}


def process_inbound_activity(activity: Any, sender, request) -> str:
    """Process one verified inbound activity. Returns the outcome.

    ``sender`` is the user resolved and signature-verified by the view (the
    authoritative actor of record — handlers never trust the activity's
    self-declared ``actor``); ``request`` lets a handler resolve actor URLs
    against this instance's host. Outcomes: ``"duplicate"`` — the activity's
    wire id was already delivered, so nothing is re-processed; ``"ignored"`` —
    a well-formed activity of an unknown or not-yet-handled type (recorded for
    dedup, no content created); ``"handled"`` — a registered handler acted on
    it. Activities without a string ``id`` cannot be deduped and are processed
    (or ignored) without a record. The dedup row and the handler run in one
    transaction: a handler failure rolls the record back too, so the sender's
    retry is not blocked by its own failed delivery.
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
        handler(activity, sender, request)
    return "handled"
