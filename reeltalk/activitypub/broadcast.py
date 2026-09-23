"""Outbound broadcast: statuses, shelf events, and interactions.

When a local user creates, edits, or deletes a review — or adds/removes a
film on their Watchlist — the change is delivered as a signed Create / Update
/ Delete activity to each *remote* follower's home inbox (R39 signatures via
``delivery``; the same single synchronous POST, no retry queue in v0.1).
Local followers need no delivery: they read the local rows their feed query
already sees.

**Who receives what is not one rule.** A status or shelf event is
announcement, so it goes to the author's followers. The interactions feed
interactions increment 5 adds each have their own audience, and getting it
wrong is invisible until someone replies to a stranger and the reply
vanishes: a ``Like`` goes to the **post's author** alone (it is addressed to
them, not broadcast, and is not timeline content), and a reply goes to the
**post's author *and* the replier's followers** — the author because they may
not follow the replier, the followers because it is a status they follow.

The local write happens first and commits before the broadcast runs, so a
delivery failure can never lose local state — only the remote's copy lags
until the next delivery or an outbox backfill. A dead follower must not fail
the user's request either (a "mark as watched" POST is not down because one
follower instance is), so per-recipient network failures are dropped rather
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
    like_activity,
    shelf_event_activity,
    update_activity,
)


def _deliver_signed(request, actor, activity: dict, recipients) -> None:
    """Deliver ``activity`` to each remote recipient, signed as ``actor``.

    ``actor`` signs with their own key — a local user always has one, a
    mirror never does, so the signer is whoever locally caused the activity.
    Local recipients are skipped: they read the local rows their own queries
    already see, so a delivery to one would be a POST with nothing new to
    say. A recipient whose inbox cannot be reached drops its send — v0.1 has
    no retry queue, and the caller's request must not fail because of one
    unreachable instance.
    """
    signer = absolute_uri(request, actor_path(actor.localname))
    for recipient in recipients:
        if recipient.local:
            continue
        try:
            deliver_activity(
                inbox_for(recipient),
                activity,
                actor.private_key,
                f"{signer}#main-key",
            )
        except requests.RequestException:
            # Dropped: the local state is committed; the recipient's copy lags.
            continue


def _deliver_to_followers(request, author, activity: dict) -> None:
    """Deliver ``activity`` to every remote follower of ``author``."""
    _deliver_signed(request, author, activity, author.followers.all())


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


def broadcast_like(request, status, user, *, liked: bool) -> None:
    """Tell a post's author that a local member liked or unliked it.

    The recipient is the post's **author**, not the liker's followers. A
    like is an answer addressed to the person who posted — it is not
    timeline content, and Mastodon delivers it the same way. Nothing goes out
    for a local post: its author reads the ``Like`` row their own page
    already shows, so a delivery would be a POST telling them what they can
    already see.

    ``liked`` picks the activity rather than the caller making two calls,
    because the unlike is not a separate event to model — it is the same
    sentence with "not" in it, and the pair must never drift.
    """
    activity = like_activity(user, status, request, undo=not liked)
    _deliver_signed(request, user, activity, [status.user])


def broadcast_reply(request, reply) -> None:
    """Deliver a local reply to the conversation and to the replier's followers.

    Two audiences, and neither contains the other. The author of the post
    being answered must receive it **whether or not they follow the
    replier** — otherwise a reply to a stranger never arrives, which is the
    whole point of threading. The replier's own remote followers receive it
    because it is a status they follow. The set is deduped by pk so a parent
    who already follows the replier is not posted to twice.

    The threading rides on the Note itself: ``objects.note_document``
    serialises ``inReplyTo`` from ``reply_parent``, and
    ``objects.note_reference`` makes that the parent's *home* URL, so a
    reply to a mirrored post points at the instance that actually owns the
    turn being answered rather than at a URL we minted for it.
    """
    activity = create_activity(reply, reply.user, request)
    targets = list(reply.user.followers.all())
    parent_author = reply.reply_parent.user
    if not any(user.pk == parent_author.pk for user in targets):
        targets.append(parent_author)
    _deliver_signed(request, reply.user, activity, targets)
