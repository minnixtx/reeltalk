"""Follow / Undo(Follow) handling (M4 increment 5).

Both directions of the follow relationship, built fresh against the
ActivityPub spec (R7):

**Inbound** — a remote user follows or unfollows one of our users. The inbox
pipeline has already resolved and verified the sender from the signature's
keyid, so the handlers record the relationship in the follow M2M *from the
verified sender* — never from the activity's self-declared ``actor`` (which an
attacker could forge). A Follow adds the sender to the followed user's
followers; an Undo whose object is a Follow removes it. An Undo of any other
type, or an unresolvable object, is ignored gracefully (§3.6: unsupported
shapes create nothing and never raise).

**Outbound** — one of our users follows or unfollows a remote user. The
relationship is recorded in the M2M and delivered as a signed Follow /
Undo(Follow) to the followed user's home inbox (R39 signatures via
``delivery``).

The follow M2M (``User.follows``, ``related_name="followers"``) is the single
source of truth: ``follower.follows.add(followed)`` puts ``followed`` in
``follower``'s following and ``follower`` in ``followed``'s followers. The
existing feed query already reads ``user.follows`` — so recording the row is
exactly what makes a followed remote user's mirrored statuses appear in a
local user's feed (increment 6 supplies those statuses).
"""

import uuid

from reeltalk.social.models import User

from .delivery import deliver_activity
from .identity import absolute_uri, actor_path
from .mirrors import (
    fetch_person_document,
    mirror_user_from_person,
    resolve_known_actor,
)


def _actor_url(value) -> str | None:
    """The actor URL an activity field carries — a bare string or ``{"id": ...}``."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return value.get("id") or None
    return None


def _ensure_mirror(actor_url: str) -> User:
    """The existing mirror for ``actor_url``, fetched + created on first contact.

    Creation (not a refresh of an existing mirror) is the only fetch, per R42.
    Raises the mirrors module's ``RemoteFetchError`` when the remote cannot be
    resolved — the caller surfaces it rather than recording a follow to an
    unknown user.
    """
    mirror = User.objects.filter(local=False, actor_url=actor_url).first()
    if mirror is not None:
        return mirror
    doc = fetch_person_document(actor_url)
    return mirror_user_from_person(doc)


def _inbox_for(mirror: User) -> str:
    """The mirror's home inbox — the advertised URL, else the path convention.

    ReelTalk and current Mastodon (R39's targets) both place the inbox under
    the actor path, so a mirror whose document advertised no inbox falls back
    to ``<actor_url>/inbox``.
    """
    if mirror.inbox_url:
        return mirror.inbox_url
    return mirror.actor_url.rstrip("/") + "/inbox"


# --- Inbound handlers (registered in inbox.HANDLERS) ------------------------


def handle_follow(activity, sender, request):
    """A remote user follows one of ours: record ``sender follows <object>``.

    ``sender`` is the verified signer (the follower); the object is the user
    being followed, resolved among users this instance already knows.
    """
    followed_url = _actor_url(activity.get("object"))
    if not followed_url:
        return
    followed = resolve_known_actor(followed_url, request)
    if followed is None:
        # An actor we do not know — ignore gracefully (no fetch as a side
        # effect of processing the activity).
        return
    sender.follows.add(followed)


def handle_undo(activity, sender, request):
    """An Undo: act only when it undoes a Follow; ignore everything else.

    The object of an Undo(Follow) is the original Follow activity — a dict
    whose ``type`` is ``Follow`` and whose own ``object`` names the followed
    user. Any other shape (an Undo of a Create, a malformed object, ...) is
    not ours to act on and is ignored gracefully.
    """
    inner = activity.get("object")
    if not isinstance(inner, dict) or inner.get("type") != "Follow":
        return
    followed_url = _actor_url(inner.get("object"))
    if not followed_url:
        return
    followed = resolve_known_actor(followed_url, request)
    if followed is None:
        return
    sender.follows.remove(followed)


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
        _inbox_for(mirror), activity, follower.private_key, f"{actor}#main-key"
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
    mirror = _ensure_mirror(followed_actor_url)
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
