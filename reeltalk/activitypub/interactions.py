"""Inbound interactions — a remote user's Like reaching us (increment 6).

The inbound counterpart of ``broadcast.broadcast_like``: what we send when a
member likes a post, arriving back the other way when a remote user likes one
of ours. It is registered in ``inbox.HANDLERS`` as ``Like``.

**Attribution: the verified sender, never ``activity["actor"]``.** A Like is
the cheapest activity to forge — it carries no content, so a handler that
trusted the declared actor would let anyone write a like as anyone, and the
only thing distinguishing the two rows afterwards is which identity we
chose to believe. The verified-sender rule was already load-bearing here in
the direction that *deletes* (``handle_undo``'s ``Like`` branch, increment
5); this is the direction that *creates*, and the rule is the same. The
signature is the authority on who the actor is; the wire field is a claim.

**An unknown target is dropped, not fetched** (owner decision, increment 6).
``resolve_status_reference`` deliberately does no fetching and this handler
keeps that posture rather than opting into ``_film_for_reference``'s
fetching behaviour. Three reasons, in order of weight:

* **The cost is out of proportion to the signal.** A Like adds nothing but a
  count, and it is attached to a row we do not have. Fetching it means
  pulling a whole Note — which for an unmirrored note cascades into its
  Film document and that film's poster. The lowest-value signal on the wire
  would drive the heaviest fetch we have.
* **It hands a peer our outbound request budget.** The verified sender
  proves *who* sent the activity; it proves nothing about the id they named
  being real, still live, or theirs to make us go and get. Every fetch added
  to an inbound handler is a way for any instance we federate with to make
  this one issue requests at a rate it chooses.
* **It is what the real peer does.** Mastodon 4.7.2's own handler opens
  ``return if original_status.nil? || !original_status.account.local? …`` —
  it drops the favourite when the status is not already in its database,
  with no fetch. Verified against the running 4.7.2 tree, not a tag.

The accepted consequence: if the note reaches us later (we follow its author
after the like was sent), the remote does not re-send the like, so its count
stays missing here. That is the same eventual-inconsistency mirrors already
live with, and it is a smaller bug than a fetch an activity should never
have caused.

**A Like against a tombstone is ignored too.** The target row exists but is a
deleted object; a count attached to something that no longer has content is
a fact about nothing, and nothing on this instance renders it.
"""

from reeltalk.core.models import Like
from reeltalk.notifications.models import Notification, notify

from .identity import reference_url
from .statuses import resolve_status_reference


def handle_like(activity, sender, request) -> str | None:
    """Record a remote user's like of a status we already have.

    Keyed on ``(sender, status)`` — the unique pair R83 decision 4 put on
    the table — so a redelivered Like lands on the same row and cannot
    double-count. ``get_or_create`` is race-safe here because the inbox
    already runs every handler inside ``transaction.atomic()``, which is
    what lets it catch the unique-constraint violation and re-read instead
    of failing the delivery.

    The target resolves only among rows this instance already holds; see the
    module docstring for why an unknown target is dropped rather than
    fetched. The reason for a drop is returned to the pipeline so it lands
    in the log — an interaction that arrived and went nowhere is exactly the
    thing a later session should not have to rediscover.
    """
    target_url = reference_url(activity.get("object"))
    status = resolve_status_reference(target_url, request)
    if status is None:
        return f"dropped, no such note here ({target_url})"
    if status.deleted:
        return "dropped, the target is a tombstone"
    _like, created = Like.objects.get_or_create(user=sender, status=status)
    # The author's notification (notifications increment 2, R92): the
    # federated half of the like pair, and the half a local-only wiring would
    # have left dark. Gated on ``created`` because the event is the like
    # arriving, not an activity mentioning it — a second Like activity for a
    # pair we already hold changes nothing, so it has nothing new to say.
    # Redelivery of the *same* activity never reaches this line: the dedup
    # row and the handler share one transaction (``inbox.py``), which is what
    # stops a retried delivery writing a second notification. The recipient
    # may be a remote mirror of another instance's post, which is why this
    # calls ``notify()`` rather than creating a row directly.
    if created:
        notify(status.user, sender, Notification.Kind.LIKE, status)
    return None
