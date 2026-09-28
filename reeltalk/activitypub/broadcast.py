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

from reeltalk.core.models import Status

from .delivery import DeliveryFailure, deliver_activity, inbox_for
from .identity import (
    absolute_uri,
    actor_path,
    actor_update_activity,
    person_delete_activity,
)
from .objects import (
    create_activity,
    delete_activity,
    like_activity,
    shelf_event_activity,
    update_activity,
)


def _deliver_signed(
    request, actor, activity: dict, recipients
) -> list[DeliveryFailure]:
    """Deliver ``activity`` to each remote recipient, signed as ``actor``.

    ``actor`` signs with their own key — a local user always has one, a
    mirror never does, so the signer is whoever locally caused the activity.
    Local recipients are skipped: they read the local rows their own queries
    already see, so a delivery to one would be a POST with nothing new to
    say.

    **A failed send is returned, not swallowed.** v0.1 has no retry queue and
    the caller's request must not fail because of one unreachable instance,
    so this never raises — but it used to ``continue`` in silence, which left
    the caller unable to tell a moderator that the remote copy is still up.
    Every recipient that did not get a 2xx comes back in the returned list
    with the reason, so a surface that owes its actor the truth can pay it.

    A **suspended** recipient is skipped (R102). We cannot suspend a remote
    mirror from this increment's UI — suspend is defined for local accounts
    only — so this branch is unreachable from the moderator queue today. It
    is here because it is the one place every outbound audience passes
    through, and increment 6's domain block needs exactly this skip rather
    than three new ones at the three audience builders. It is tested by
    suspending a mirror directly, so the branch is covered and a mutation
    that removes it turns a test red rather than hiding behind an
    unproducible state.
    """
    signer = absolute_uri(actor_path(actor.localname))
    key_id = f"{signer}#main-key"
    failures: list[DeliveryFailure] = []
    for recipient in recipients:
        if recipient.local:
            continue
        if recipient.suspended_at is not None:
            continue
        inbox = inbox_for(recipient)
        try:
            response = deliver_activity(inbox, activity, actor.private_key, key_id)
        except requests.RequestException as exc:
            failures.append(
                DeliveryFailure(recipient, inbox, str(exc) or type(exc).__name__)
            )
            continue
        if not 200 <= response.status_code < 300:
            failures.append(
                DeliveryFailure(recipient, inbox, f"HTTP {response.status_code}")
            )
    return failures


def _deliver_to_followers(request, author, activity: dict) -> list[DeliveryFailure]:
    """Deliver ``activity`` to every remote follower of ``author``."""
    return _deliver_signed(request, author, activity, author.followers.all())


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
    return _deliver_signed(request, status.user, activity, _status_targets(status))


def broadcast_status_update(request, status) -> None:
    """Broadcast an edited review/rating to followers and the remotes it mentions.

    The audience is recomputed from the mention rows on every call, so an
    edit that newly mentions a remote user reaches them and one that drops
    them stops delivering — provided the caller re-synced the rows first,
    which ``sync_status_mentions`` is what the write sites call for.
    """
    activity = update_activity(status, status.user, request)
    return _deliver_signed(request, status.user, activity, _status_targets(status))


def broadcast_status_delete(request, status) -> None:
    """Broadcast a deleted review to the author's remote followers.

    The tombstone keeps its identity (soft-delete — §3.2), so the Note rides
    inline and the receiver keys the deletion by origin id.
    """
    return _deliver_to_followers(
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
    return _deliver_to_followers(request, user, activity)


def broadcast_actor_update(request, user) -> list[DeliveryFailure]:
    """Tell a user's remote followers that their Person document changed (R102).

    The suspend/unsuspend broadcast. It is the only way a peer learns that
    an account it already holds has been suspended or restored — no status
    activity is involved, since suspend hides content rather than deleting
    it, so without this call the peer keeps a Person document that says
    nothing about a decision we made.

    **The signer is the suspended user themselves, not the moderator.**
    Same rule increment 3 pinned for the delete: the identity on the wire
    is the actor the document describes, never whoever pressed the button.
    That means a suspension of a *mirror* could not be broadcast at all —
    we hold no private key for another instance's identity — which is a
    second reason suspend stays a local-account action.

    Deliberately **not** silenced on failure. R108's call is that a
    moderation action which did not federate must be loud, and the caller
    gets the failure list back so it can write the audit line and tell the
    moderator, exactly as the delete view does.
    """
    return _deliver_to_followers(request, user, actor_update_activity(user, request))


def person_delete_audience(user) -> list:
    """Every remote account that ever received this actor's content.

    The audience question §2D flags as the hard part of a Person delete, and
    the reason the status helper cannot be reused. ``broadcast_status_delete``
    addresses the author's followers, which is right for a post: the people
    who subscribed to it. A Person delete has to reach **everyone who holds a
    copy of anything this actor sent them**, and three groups received that
    actor's content without following them:

    * **Remote users the actor mentioned.** A mention is an address, and
      ``broadcast_status_create`` delivered to them precisely so the mention
      arrived. They hold the Note and do not follow the author, so the
      follower list would never reach them.
    * **Remote authors the actor replied to.** ``broadcast_reply`` delivers
      to the parent author whether or not they follow the replier — that is
      the whole point of threading — so a stranger who was answered holds a
      reply from this actor in their thread.
    * **The actor's own remote followers**, who need it most.

    Mastodon solves the same problem bluntly: ``Account.inboxes`` minus the
    followers, i.e. every account this instance knows about, at low priority.
    That is a reasonable hammer, but here the three sets *are* enumerable —
    the mention rows and the reply edges are in our own tables — so we name
    them instead of fanning out to every inbox on the box. Our deliveries are
    synchronous with no retry queue, so the difference between "everyone who
    got this actor's content" and "every account we have ever seen" is the
    difference between a request that finishes and one that does not.

    Deliberately **not** filtered on ``deleted``. A status this actor deleted
    months ago is still a status those recipients hold, and telling them the
    actor is gone is exactly as true now as it would have been then. Scoping
    to live statuses would under-deliver the one message that matters.

    Returns remote users only. A local member has no inbox and reads the
    local rows anyway, so they are not a delivery target for anything.
    """
    recipient_ids: set[int] = set(user.followers.values_list("id", flat=True))
    own_statuses = Status.objects.filter(user=user)
    recipient_ids.update(own_statuses.values_list("mentions__user", flat=True))
    recipient_ids.update(
        own_statuses.filter(reply_parent__isnull=False).values_list(
            "reply_parent__user", flat=True
        )
    )
    recipient_ids.discard(user.pk)
    recipient_ids.discard(None)
    if not recipient_ids:
        return []
    # ``type(user).objects`` rather than an imported ``User``: social.models
    # already imports activitypub.crypto at module level, so a model import
    # back the other way here is the kind of edge that turns into a cycle the
    # next time either side grows an import.
    return list(type(user).objects.filter(pk__in=recipient_ids, local=False))


def broadcast_person_delete(request, user, audience=None) -> list[DeliveryFailure]:
    """Tell the network that this actor is gone (R102, ban).

    One activity, not one per status. A peer that accepts a ``Delete(Person)``
    removes its mirror of the account *and everything hanging off it*, so
    the per-status deletes a naive reading of "content removed" suggests
    would multiply the fan-out by the target's status count to say something
    the person delete already says. With synchronous delivery and no retry
    queue, ``statuses x followers`` POSTs inside one moderator request is
    not a shape that survives a member with a few hundred reviews.

    **Signed as the banned user, never the moderator** — see
    :func:`~reeltalk.activitypub.identity.person_delete_activity` for why
    that is a correctness requirement rather than a convention. A mirror
    cannot be broadcast at all, which is a second reason ban stays a
    local-account action.

    ``audience`` lets a caller pass a pre-computed set
    (:func:`person_delete_audience`); it defaults to computing it, because
    the common case is exactly that set and a caller who forgets it should
    get the right answer rather than a silent empty send.
    """
    activity = person_delete_activity(user)
    if audience is None:
        audience = person_delete_audience(user)
    return _deliver_signed(request, user, activity, audience)


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
    return _deliver_signed(request, user, activity, [status.user])


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
    return _deliver_signed(request, reply.user, activity, targets)
