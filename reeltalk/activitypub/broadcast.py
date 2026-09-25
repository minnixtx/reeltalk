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


def _mentioned_remote_users(status):
    """The remote users ``status`` mentions, in the order it mentions them.

    Remotes only. A local member has no inbox, so they are not a delivery
    target even though they are still a ``tag`` on the Note — the two sets
    are different questions and only one of them is "who gets a POST".
    Filtering here rather than leaning on ``_deliver_signed``'s local skip is
    deliberate: the test that pins "a local-only mention adds nothing to the
    audience" has to go red when *this* filter goes, not stay green because a
    downstream stage declined. That is mentions increment 2's lesson about a
    negative test passing for the wrong reason.
    """
    return [
        mention.user
        for mention in status.mentions.select_related("user")
        if not mention.user.local
    ]


def _status_targets(status, extra=()):
    """Everyone a status activity must be delivered to, deduped by pk.

    Two sets that overlap. The author's remote followers, because they follow
    the author. The remote users the status *mentions*, because a mention is
    an address and a person addressed in a post they would not otherwise
    see has to be sent it or the mention never arrives — which is also why
    the follower list alone was never enough.

    ``extra`` is an audience a particular broadcast knows about that the
    status itself does not: ``broadcast_reply`` passes the parent author,
    who must receive the reply whether or not they follow the replier. The
    union is deduped by pk so a person in more than one of these sets is
    posted to once, the way ``broadcast_reply`` already deduped its parent.
    """
    targets = list(status.user.followers.all())
    seen = {user.pk for user in targets}
    for user in (*_mentioned_remote_users(status), *extra):
        if user.pk in seen:
            continue
        seen.add(user.pk)
        targets.append(user)
    return targets


def broadcast_status_create(request, status) -> None:
    """Broadcast a new status to the author's remote followers and its mentions.

    The audience is wider than the followers' because a mention is an
    address, not a broadcast: the person named has to be sent the post or
    the mention never reaches them. A dead mentioned instance drops its send
    exactly as a dead follower does — the member's post must not fail
    because of somebody else's server.
    """
    activity = create_activity(status, status.user, request)
    _deliver_signed(request, status.user, activity, _status_targets(status))


def broadcast_status_update(request, status) -> None:
    """Broadcast an edited review/rating to followers and the remotes it mentions.

    The audience is recomputed from the mention rows on every call, so an
    edit that newly mentions a remote user reaches them and one that drops
    them stops delivering — provided the caller re-synced the rows first,
    which ``sync_status_mentions`` is what the write sites call for.
    """
    activity = update_activity(status, status.user, request)
    _deliver_signed(request, status.user, activity, _status_targets(status))


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

    Three audiences, and none of them contains another. The author of the
    post being answered must receive it **whether or not they follow the
    replier** — otherwise a reply to a stranger never arrives, which is the
    whole point of threading. The replier's own remote followers receive it
    because it is a status they follow. The set is deduped by pk so a parent
    who already follows the replier is not posted to twice.

    A third set rides along for free from ``_status_targets``: any remote
    user the reply itself *mentions*. Without it a reply that names a third
    person would carry that person in its ``tag`` array and never deliver to
    them — the mention would be inert on exactly the surface where people
    use it most. The parent author is passed as ``extra`` rather than folded
    into the status's own sets because the parent is not a mention: they are
    owed the reply by the threading, whether or not the text names them.

    The threading rides on the Note itself: ``objects.note_document``
    serialises ``inReplyTo`` from ``reply_parent``, and
    ``objects.note_reference`` makes that the parent's *home* URL, so a
    reply to a mirrored post points at the instance that actually owns the
    turn being answered rather than at a URL we minted for it.
    """
    activity = create_activity(reply, reply.user, request)
    targets = _status_targets(reply, extra=[reply.reply_parent.user])
    _deliver_signed(request, reply.user, activity, targets)
