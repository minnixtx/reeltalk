"""Outbound status + shelf-event broadcast (M4 increment 6).

When a local user creates, edits, or deletes a review — or adds/removes a
film on their Watchlist — the change is delivered as a signed Create / Update
/ Delete activity to each *remote* follower's home inbox (R39 signatures via
``delivery``; the same single synchronous POST, no retry queue in v0.1).
Local followers need no delivery: they read the local rows their feed query
already sees.

The local write happens first and commits before the broadcast runs, so a
delivery failure can never lose local state — only the remote's copy lags
until the next delivery or an outbox backfill. A dead follower must not fail
the user's request either (a "mark as watched" POST is not down because one
follower instance is), so per-follower network failures are dropped rather
than raised.

The broadcast functions take the already-saved row and are directly testable;
the views that mutate statuses/shelves call them after the model layer
succeeds. File import deliberately broadcasts nothing (§3.5: importing your
own data is not a social act).
"""

import requests

from .delivery import deliver_activity, inbox_for
from .identity import absolute_uri, actor_path
from .objects import (
    create_activity,
    delete_activity,
    shelf_event_activity,
    update_activity,
)


def _deliver_to_followers(request, author, activity: dict) -> None:
    """Deliver ``activity`` to every remote follower of ``author``.

    Signed with the author's key (local users always have one; mirrors never
    author). A follower whose inbox cannot be reached drops its send — v0.1
    has no retry queue, and the caller's request must not fail because of a
    dead follower.
    """
    actor = absolute_uri(request, actor_path(author.localname))
    for follower in author.followers.all():
        if follower.local:
            continue
        try:
            deliver_activity(
                inbox_for(follower), activity, author.private_key, f"{actor}#main-key"
            )
        except requests.RequestException:
            # Dropped: the local state is committed; the follower's copy lags.
            continue


def broadcast_status_create(request, status) -> None:
    """Broadcast a new review/rating/comment to the author's remote followers."""
    _deliver_to_followers(
        request, status.user, create_activity(status, status.user, request)
    )


def broadcast_status_update(request, status) -> None:
    """Broadcast an edited review/rating to the author's remote followers."""
    _deliver_to_followers(
        request, status.user, update_activity(status, status.user, request)
    )


def broadcast_status_delete(request, status) -> None:
    """Broadcast a deleted review to the author's remote followers.

    The tombstone keeps its identity (soft-delete — §3.2), so the Note rides
    inline and the receiver keys the deletion by origin id.
    """
    _deliver_to_followers(
        request, status.user, delete_activity(status, status.user, request)
    )


def broadcast_shelf_event(request, user, film, identifier: str, *, added: bool) -> None:
    """Broadcast a Watchlist add/remove to the user's remote followers.

    ``identifier`` is the D1 shelf identifier (``to-read`` in v0.1 — the
    Watched side rides on the review broadcast instead). The ShelfEvent object
    id is stable per (user, film, shelf); the activity id is unique per event,
    so an unshelve-then-re-shelve is not deduped away on the receiving side.
    """
    activity = shelf_event_activity(user, film, identifier, request, added=added)
    _deliver_to_followers(request, user, activity)
