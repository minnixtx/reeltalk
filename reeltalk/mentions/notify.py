"""The mention producer: who a saved status actually owes a notification to.

This is the smallest piece of the mentions arc. The parser decides *who was
addressed*; the renderer decides *what it looks like*; the wire decides *who
receives it*. What is left here is one question — of the people this status
addresses, who has not already been told — and two answers, each a decision
settled before any code was written (§2C's M-c and M-e).

**M-c — a reply that also @mentions the person being replied to is one
notification.** The reply producer fires for the parent's author on every
reply, whether or not the text names them. Without this suppression a
threaded conversation lights the badge twice per turn for the person you are
talking to, which is noise enough to make the badge worthless. Note what is
*not* suppressed: the mention still renders as a link and still goes on the
wire in the ``tag`` array, because both of those are decided by the
``StatusMention`` rows, which the caller writes before calling in here. Only
the ledger row is withheld.

**M-e — an edit that newly mentions someone does notify, once.** The
producer is idempotent per ``(recipient, status)``, so N edits that keep
mentioning the same person produce one row. This is what makes it safe to
call from a path that runs on every save of a status, including the
``_mirror_status`` update branch, where the standing "the update branch never
notifies" rule lives. That rule stops a duplicate **reply** row for a reply
already notified, and it stays exactly where it is. The mention kind gets its
own guard, with its own mechanism, and the guard additionally bounds
edit-spam on our side.

**What this module deliberately does not do is decide who may be told.**
Self-mention, remote recipient and block all fall out of ``notify()`` exactly
as they did for the six notifications-increment-2 producers (R97). A
producer that pre-checked them would create a second source of truth about
who is owed, which is the exact residue write-time filtering exists to
prevent — and it would drift the moment ``notify()`` changed. Everything
still standing after the two suppressions above goes through the door.
"""

from reeltalk.notifications.models import Notification, notify


def record_mentions(status, users) -> list:
    """File a ``mention`` notification for each user in ``users`` still owed one.

    ``users`` is the resolved mention set the caller already stored — the
    same ordered list that produced the outbound ``tag`` array. Passing the
    resolved rows rather than re-parsing here keeps "who was mentioned" one
    answer in the request rather than two that can disagree.

    The actor is always ``status.user``: the person who wrote the post is the
    person who mentioned you, on both paths. On an inbound mirror that is the
    *verified sender* rather than whoever the note's ``attributedTo`` claims,
    which is the same attribution every other mirror write uses.

    Returns the rows actually written. Callers ignore it; ``notify()``
    returns ``None`` for a declined recipient, and a self-mention is normal
    rather than an error, so the count is not a success signal.
    """
    if not users:
        # The common case — most posts mention nobody — and the only thing
        # this guard saves is the suppression query below. §2C put the cost
        # of mentions on the write path; this keeps the unmentioned write at
        # zero extra queries.
        return []
    already_told = _already_told(status)
    made = []
    for user in users:
        if user.pk in already_told:
            continue
        note = notify(user, status.user, Notification.Kind.MENTION, status)
        if note is not None:
            made.append(note)
    return made


def _already_told(status) -> set:
    """Recipient pks ``record_mentions`` must not add a row for.

    Two unrelated reasons unioned into one set, because both answer the same
    question — is this person already going to see this?

    * **The author being replied to** (M-c), read off ``reply_parent`` rather
      than passed in. Deriving it from the status is what makes the rule hold
      identically at all three producers without each one having to remember
      to hand the parent over — and it is the correct source on an inbound
      mirror, where ``status.user`` is the sender and the person answered is
      ``status.reply_parent.user``.
    * **Anyone already notified about this same status** (M-e). One query
      over the ``(status, kind)`` pair rather than one per recipient, which
      also means the guard still sees rows written by an earlier request that
      this call cannot see in memory.
    """
    told = set()
    if status.reply_parent_id:
        told.add(status.reply_parent.user_id)
    told.update(
        Notification.objects.filter(
            status=status, kind=Notification.Kind.MENTION
        ).values_list("recipient_id", flat=True)
    )
    return told
