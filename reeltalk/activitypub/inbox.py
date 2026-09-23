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

import logging
from collections.abc import Callable
from typing import Any

from django.db import transaction

from .follow import handle_accept, handle_follow, handle_reject, handle_undo
from .interactions import handle_like
from .models import DeliveredActivity
from .statuses import handle_create, handle_delete, handle_update

logger = logging.getLogger(__name__)

# Activity types this instance acts on, mapped to their handlers (each takes
# the parsed activity dict, the verified sender, and the request, and applies
# its effects). Increment 5 registers Follow + Undo; increment 6 adds
# Create/Update/Delete. Everything not in the registry — including types we
# will never support — is ignored gracefully.
#
# A handler may return a short human-readable detail describing what it
# actually did. The pipeline logs it, so every inbound decision is visible
# from one place with its reason — a handler that quietly decided to drop
# something is the inbound twin of a delivery call that never read the
# response it got back (R88).
HANDLERS: dict[str, Callable[[dict, Any, Any], "str | None"]] = {
    "Follow": handle_follow,
    "Undo": handle_undo,
    "Create": handle_create,
    "Update": handle_update,
    "Delete": handle_delete,
    # Increment 6: the inbound half of the interactions increment 5 put on
    # the wire, and the two answers to a Follow we sent. Accept is listed
    # even though it changes nothing so that "we were accepted" is a decided
    # outcome here rather than a miss in the ignore path.
    "Like": handle_like,
    "Accept": handle_accept,
    "Reject": handle_reject,
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

    The outcome is logged for every activity, with whatever detail the handler
    returned. The four-milestone blindness on the outbound side was
    ``deliver_activity`` discarding the response it got back (R88); this is
    the same trap read from the inside — until now the inbox decided
    handled/ignored/duplicate and told no one, so an activity we deliberately
    dropped was indistinguishable from one that never arrived.
    """
    outcome, detail = _apply_inbound_activity(activity, sender, request)
    logger.info(
        "Inbound %s from %s -> %s%s",
        activity.get("type") if isinstance(activity, dict) else "?",
        getattr(sender, "localname", "?"),
        outcome,
        f" — {detail}" if detail else "",
    )
    return outcome


def _apply_inbound_activity(activity: Any, sender, request) -> tuple[str, str | None]:
    if not isinstance(activity, dict):
        return "ignored", None
    activity_id = activity.get("id")
    with transaction.atomic():
        if isinstance(activity_id, str) and activity_id:
            _row, created = DeliveredActivity.objects.get_or_create(
                activity_id=activity_id
            )
            if not created:
                return "duplicate", None
        activity_type = activity.get("type")
        handler = (
            HANDLERS.get(activity_type) if isinstance(activity_type, str) else None
        )
        if handler is None:
            return "ignored", f"unhandled type {activity_type!r}"
        return "handled", handler(activity, sender, request)
