"""The header badge: one unread count, handed to every rendered page (R94).

The count is the feature's only entry point. Without it the unread state
lives in the database and nobody can see it, which is why R96 calls the
badge functional UI rather than parked decoration — what is parked is its
*visual treatment*, never its existence. This processor is the number; the
look stays the owner's.

One query per rendered authenticated page, and one only. It calls
``Notification.unread_for`` — the same unread contract the notifications
page reads — so the badge and the page cannot disagree about what is
unread, and the count rides the composite ``(recipient_id, created)``
index the ledger was built with. That is a ``COUNT(*)`` over an indexed
range: it returns one integer and does not scale with the ledger. The
home page's history of rendering 1,378 rows is therefore not an argument
against a badge; that was a rendering-volume failure, and this is a
counter.

Context processors run only for a template render, so the ActivityPub
endpoints and the CSV export — which answer ``JsonResponse`` and
``StreamingHttpResponse`` — never reach this code and pay nothing for it.

The authentication guard lives here rather than in each template for two
reasons: an anonymous visitor must not pay a query for a badge they cannot
have, and ``AnonymousUser`` has no ``notifications_last_read``, so a
template that forgot the guard would raise rather than render nothing.
"""

from reeltalk.notifications.models import Notification


def unread_notifications(request):
    """Add ``unread_notifications``: rows this member has not read yet.

    ``0`` for an anonymous request, so the key is always present and a
    template can test one truthy value rather than also asking whether the
    key exists.
    """
    if not request.user.is_authenticated:
        return {"unread_notifications": 0}
    return {"unread_notifications": Notification.unread_for(request.user).count()}
