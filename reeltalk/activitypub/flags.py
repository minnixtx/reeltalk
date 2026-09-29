"""Inbound ``Flag`` — a peer telling us about one of our members (increment 6, R104).

The inbound half of the report wire. A moderator on another instance has
been told something about an account on *this* one, and forwards it here,
because we are the only server that can act on our own users. Registered in
``inbox.HANDLERS`` as ``Flag``; before this increment a ``Flag`` was
ignored gracefully, which meant a peer's report simply vanished.

**Attribution is the verified sender, never** ``activity["actor"]``. This
is the standing inbound rule and it matters more here than anywhere else on
the wire, because a ``Flag`` is the one activity whose whole purpose is to
make this instance write down an accusation about somebody. A handler that
read the declared actor would let any peer write a report against anyone as
anyone, and the queue would render the forgery with the same authority it
renders a real one. The signature is the authority; the ``actor`` field is
a claim about it.

**The reporter we can record is the server, not the person.** Mastodon
masks its own reporters exactly as R104 requires us to mask ours, so what
arrives is signed by *their* instance actor. The human who filed it is
unknowable here, and that is symmetric rather than a gap: neither side
exposes its reporters, and both sides still get the words. The queue shows
the sending server as the reporter because that is the only identity the
signature supports.

**Only local targets are filed.** A ``Flag`` naming a remote account is
addressed to the wrong server — that account's home instance owns it, not
us. Filing it here would create work against an account we cannot act on
and, worse, would let any peer pile noise onto another instance's queue by
routing a report through the wrong box.

**A suspended target resolves to nothing and is dropped; a banned target is
skipped explicitly.** The suspension half is not a choice made here —
:func:`~reeltalk.activitypub.mirrors.resolve_known_actor` already refuses
a suspended account, on either branch, and this handler inherits that
posture rather than working around it. The consequence is recorded in the
increment's execution record: a peer's report about an account this
instance has already suspended does not land. The ban half *is* a choice,
because a banned row still resolves (ban is a different column, and
:func:`resolve_known_actor` does not read it) and filing work against an
account that is permanently gone is noise with no possible outcome.
"""

from reeltalk.moderation.models import file_remote_report

from .identity import reference_url
from .mirrors import resolve_known_actor
from .statuses import resolve_status_reference


def _object_uris(activity) -> list:
    """The flat URI list a ``Flag`` carries, in whatever shape it arrived.

    Their serializer emits an array; the spec allows a bare string or a
    single object, and a peer could send either. ``reference_url`` unwraps
    one item, so this walks the list and keeps what resolves to a string.
    """
    raw = activity.get("object")
    items = raw if isinstance(raw, list) else [raw]
    uris = []
    for item in items:
        url = reference_url(item)
        if url:
            uris.append(url)
    return uris


def handle_flag(activity, sender, request) -> str | None:
    """File a peer's report about a local member or one of their posts.

    Statuses are resolved first and win. If the ``Flag`` names both a
    status and its author, filing two reports would be two work items for
    one decision — and the status report already carries the author as
    ``target_user``, which is the whole of what the account-level report
    would have said. Only when nothing in the payload resolves to a status
    do we fall back to filing against the account itself.

    The return value is the detail the inbox pipeline logs, and every drop
    path states its reason. A report that arrived and went nowhere is
    exactly the thing a later session should not have to rediscover by
    re-sending payloads by hand.
    """
    uris = _object_uris(activity)
    if not uris:
        return "dropped, the Flag names no object"

    comment = activity.get("content")
    if not isinstance(comment, str):
        comment = ""

    statuses = []
    accounts = []
    for uri in uris:
        status = resolve_status_reference(uri, request)
        if status is not None:
            statuses.append(status)
            continue
        account = resolve_known_actor(uri, request)
        if account is not None:
            accounts.append(account)

    if statuses:
        filed = 0
        skipped = []
        for status in statuses:
            author = status.user
            if not author.local:
                skipped.append(f"status {status.pk} is not ours")
                continue
            if author.banned_at is not None:
                skipped.append(f"status {status.pk}'s author is banned")
                continue
            if status.deleted:
                skipped.append(f"status {status.pk} is already deleted")
                continue
            _report, created = file_remote_report(
                sender, target_status=status, comment=comment
            )
            filed += 1 if created else 0
        detail = f"filed {filed} report(s) against local posts"
        if skipped:
            detail += f"; skipped {'; '.join(skipped)}"
        return detail

    if accounts:
        filed = 0
        skipped = []
        for account in accounts:
            if not account.local:
                skipped.append(f"@{account.localname} is not ours")
                continue
            if account.banned_at is not None:
                skipped.append(f"@{account.localname} is banned")
                continue
            _report, created = file_remote_report(
                sender, target_user=account, comment=comment
            )
            filed += 1 if created else 0
        detail = f"filed {filed} report(s) against local members"
        if skipped:
            detail += f"; skipped {'; '.join(skipped)}"
        return detail

    return (
        "dropped, nothing named here is an account or post "
        f"on this instance ({len(uris)} uris)"
    )
