"""Follow / Undo(Follow) handling (M4 increment 5).

Both directions of the follow relationship, built fresh against the
ActivityPub spec (R7):

**Inbound** — a remote user follows or unfollows one of our users. The inbox
pipeline has already resolved and verified the sender from the signature's
keyid, so the handlers record the relationship in the follow M2M *from the
verified sender* — never from the activity's self-declared ``actor`` (which an
attacker could forge). A Follow adds the sender to the followed user's
followers; an Undo whose object is a Follow removes it. An Undo whose object
is a Like removes the sender's like of the status it names (feed
interactions increment 5); an Undo of any other type, or an unresolvable
object, is ignored gracefully (§3.6: unsupported shapes create nothing and
never raise).

**Outbound** — one of our users follows or unfollows a remote user. The
relationship is recorded in the M2M and delivered as a signed Follow /
Undo(Follow) to the followed user's home inbox (R39 signatures via
``delivery``).

**The answers to our own Follow** (increment 6) — ``Accept`` and ``Reject``
arrive back at our inbox answering a Follow *we* sent. ``Accept`` changes
nothing, because we never hold a pending state; it is registered so the
outcome is recorded rather than swallowed by the generic ignore path.
``Reject`` undoes the local follow — see :func:`handle_reject`.

The follow M2M (``User.follows``, ``related_name="followers"``) is the single
source of truth: ``follower.follows.add(followed)`` puts ``followed`` in
``follower``'s following and ``follower`` in ``followed``'s followers. The
existing feed query already reads ``user.follows`` — so recording the row is
exactly what makes a followed remote user's mirrored statuses appear in a
local user's feed (increment 6 supplies those statuses).
"""

import uuid

from reeltalk.core.models import Like
from reeltalk.social.models import User

from .delivery import deliver_activity, inbox_for
from .identity import absolute_uri, actor_path, reference_url
from .mirrors import ensure_mirror, resolve_known_actor
from .statuses import resolve_status_reference

# --- Inbound handlers (registered in inbox.HANDLERS) ------------------------


def _answer_follow(
    sender, followed, request, followed_activity: dict, *, accepted: bool
) -> None:
    """Answer a remote Follow so their side stops holding it pending.

    A remote instance does not consider a Follow settled until the target
    answers it — Mastodon parks the request in a pending state until then —
    so a Follow we merely record locally leaves the follower waiting forever
    and our own posts never reach their timeline (R88).

    The answer is signed by the **local user being followed**, not by the
    instance at large: that user is the party whose consent a Follow asks
    for, and a mirror has no private key to sign with anyway. It goes to the
    follower's own home inbox.

    The ``object`` echoes the Follow activity as it arrived rather than a
    reconstruction, because the follower matches this answer to the request
    it is holding by that activity's id.

    A delivery failure is deliberately left to propagate. It runs inside the
    inbox transaction, so the follow record and the dedup row roll back
    together and the sender's retry re-processes the activity. Swallowing
    the failure here would be worse: the dedup row would survive, the retry
    would read as a duplicate, and the answer would never be attempted again.
    """
    if not followed.local or not followed.private_key:
        return
    acceptor = absolute_uri(request, actor_path(followed.localname))
    activity_type = "Accept" if accepted else "Reject"
    deliver_activity(
        inbox_for(sender),
        {
            "id": f"{acceptor}#{activity_type.lower()}-{uuid.uuid4().hex}",
            "type": activity_type,
            "actor": acceptor,
            "object": followed_activity,
        },
        followed.private_key,
        f"{acceptor}#main-key",
    )


def handle_follow(activity, sender, request):
    """A remote user follows one of ours: record it, and answer the request.

    ``sender`` is the verified signer (the follower); the object is the user
    being followed, resolved among users this instance already knows.

    A sender the local user has blocked gets a ``Reject`` and no
    relationship. Silently dropping it would leave them parked in "pending"
    with no reason, which is the exact failure this handler exists to close —
    the refusal is the useful half of the answer.
    """
    followed_url = reference_url(activity.get("object"))
    if not followed_url:
        return
    followed = resolve_known_actor(followed_url, request)
    if followed is None:
        # An actor we do not know — ignore gracefully (no fetch as a side
        # effect of processing the activity).
        return
    if followed.blocks.filter(pk=sender.pk).exists():
        _answer_follow(sender, followed, request, activity, accepted=False)
        return
    sender.follows.add(followed)
    _answer_follow(sender, followed, request, activity, accepted=True)


def handle_undo(activity, sender, request):
    """An Undo: act when it undoes a Follow or a Like; ignore everything else.

    The object of an Undo is the activity being undone — a dict whose
    ``type`` says what is being taken back and whose own ``object`` names the
    thing it was aimed at. Two types are ours to act on:

    * ``Follow`` — drop the follow M2M between the verified sender and the
      user named inside it.
    * ``Like`` (feed interactions increment 5) — drop the sender's like of
      the status named inside it. The status is resolved among rows this
      instance already has, never fetched.

    Anything else (an Undo of a Create, a malformed object, an unresolvable
    target) is not ours to act on and is ignored gracefully. The like is
    found by ``(sender, status)`` — the unique pair R83 decision 4 put on
    the table — not by resolving the inner ``Like``'s id, so an Undo
    arrives correctly even though we never stored the activity it refers to.
    """
    inner = activity.get("object")
    if not isinstance(inner, dict):
        return
    target_url = reference_url(inner.get("object"))
    if not target_url:
        return
    inner_type = inner.get("type")
    if inner_type == "Follow":
        followed = resolve_known_actor(target_url, request)
        if followed is not None:
            sender.follows.remove(followed)
    elif inner_type == "Like":
        status = resolve_status_reference(target_url, request)
        if status is not None:
            # Deleting zero rows is the correct answer to an Undo of a like
            # we never recorded, so this stays idempotent without a check.
            Like.objects.filter(user=sender, status=status).delete()


def _answered_follow(activity, sender, request):
    """The local user whose follow of ``sender`` an Accept/Reject answers.

    The answer wraps the Follow it answers, and that wrapper carries two
    identities of which only one is proven. The inner ``actor`` names who
    asked, and is resolved only among local users of this host; the
    followed side is taken from the **verified sender**, never from the
    inner ``object``. That is the same split Mastodon's own reject handler
    makes — ``account_from_uri(@object['actor'])`` for the local side,
    ``@account`` for the remote — and it is the safe one: a peer wanting to
    break a follow between two other parties would have to sign the
    refusal as itself.

    Returns the local requester, or ``None`` when the activity answers no
    follow this instance actually holds.
    """
    inner = activity.get("object")
    if not isinstance(inner, dict) or inner.get("type") != "Follow":
        return None
    requester = resolve_known_actor(reference_url(inner.get("actor")), request)
    if requester is None or not requester.local:
        return None
    if not requester.follows.filter(pk=sender.pk).exists():
        return None
    return requester


def handle_accept(activity, sender, request) -> str | None:
    """An Accept answers a Follow we sent. Nothing changes — and registering
    that fact is the whole point.

    The follow is recorded when the member clicks Follow and delivered in
    the same request, so there is no pending state here to promote: the
    relationship is already live locally. Registering the type anyway is
    the decision — with no handler an accepted follow lands in the same
    generic ignore path as a misspelled type, and the one piece of good
    news a peer ever sends us would be discarded exactly like noise.

    The returned detail names whose follow was accepted, which is the part
    the pipeline's own line cannot know: an Accept we could not attribute
    and an Accept that confirmed a real relationship both arrive as a
    handled ``Accept`` otherwise.
    """
    requester = _answered_follow(activity, sender, request)
    if requester is None:
        return "no matching local follow to confirm"
    return f"{requester.localname}'s follow of {sender.localname} accepted"


def handle_reject(activity, sender, request) -> str | None:
    """A Reject answers a Follow we sent: undo the local follow.

    Left alone, the local row outlives a refusal the remote has already
    stated explicitly — the member's following list would keep showing an
    account whose instance has said no, and every later broadcast would go
    on POSTing to an inbox that has told us it will not take it. Removing
    the M2M row is the whole undo, the same row ``unfollow_user`` removes.

    No ``Undo(Follow)`` goes back. Their instance has just told us the
    relationship does not exist; a follow-cancellation would be a message
    about a thing already settled, sent as the user whose request was
    refused.
    """
    requester = _answered_follow(activity, sender, request)
    if requester is None:
        return "no matching local follow to undo"
    requester.follows.remove(sender)
    return f"{requester.localname}'s follow of {sender.localname} refused — removed"


# --- Outbound (a local user follows / unfollows a remote user) --------------


def _follow_activity(actor: str, mirror: User, *, undo: bool) -> dict:
    """The Follow activity, or the Undo(Follow) wrapping one.

    Activity ids are unique per event (uuid fragment): a follow → unfollow →
    re-follow must not collide on the id, or the receiving instance would
    dedup the re-follow as a redelivery of the first.
    """
    follow = {
        "id": f"{actor}#follow-{uuid.uuid4().hex}",
        "type": "Follow",
        "actor": actor,
        "object": mirror.actor_url,
    }
    if not undo:
        return follow
    return {
        "id": f"{actor}#unfollow-{uuid.uuid4().hex}",
        "type": "Undo",
        "actor": actor,
        "object": follow,
    }


def _deliver_follow(request, follower: User, mirror: User, *, undo: bool):
    """Record the M2M and deliver the signed Follow / Undo(Follow).

    The relationship is recorded before the delivery: a delivery failure (a
    ``requests.RequestException``) leaves it in place locally so the follow
    state is not lost — v0.1 has no retry queue, so the remote's copy simply
    lags until the next delivery.
    """
    if undo:
        follower.follows.remove(mirror)
    else:
        follower.follows.add(mirror)
    actor = absolute_uri(request, actor_path(follower.localname))
    activity = _follow_activity(actor, mirror, undo=undo)
    deliver_activity(
        inbox_for(mirror), activity, follower.private_key, f"{actor}#main-key"
    )


def follow_user(request, follower: User, followed_actor_url: str) -> User:
    """A local user follows a remote user.

    Records the follow (feeding the feed query and the following collection)
    and delivers a signed Follow to the followed user's home inbox. ``follower``
    must be a local user — it signs with its own key, which a mirror has none
    of. Raises the mirrors module's ``RemoteFetchError`` when the remote cannot
    be resolved. Returns the followed mirror.
    """
    if not follower.local:
        raise ValueError("Only a local user can initiate a follow")
    mirror = ensure_mirror(followed_actor_url)
    _deliver_follow(request, follower, mirror, undo=False)
    return mirror


def unfollow_user(request, follower: User, followed_actor_url: str) -> "User | None":
    """A local user unfollows a remote user — the Undo(Follow) of follow_user.

    Only acts when the user is actually following (a mirror exists and the M2M
    row is present); otherwise it is a no-op returning None, so an unfollow
    never fetches a mirror or sends a spurious Undo. Returns the unfollowed
    mirror, or None when there was nothing to undo.
    """
    if not follower.local:
        raise ValueError("Only a local user can initiate an unfollow")
    mirror = User.objects.filter(local=False, actor_url=followed_actor_url).first()
    if mirror is None or not follower.follows.filter(pk=mirror.pk).exists():
        return None
    _deliver_follow(request, follower, mirror, undo=True)
    return mirror
