"""The notifications page and the mark-all-read control (increment 3).

R93 makes both halves of this surface cheap. The page is one indexed range
over a single recipient's rows against ``notif_recipient_created_idx``, and
marking read is one ``UPDATE`` on the user row that touches no notification
at all — so the control's cost does not scale with how much was unread.

Nothing here mutates on a GET. The page reads; ``mark_read`` is a POST with
CSRF. That split is the whole reason the mark-read timestamp is safe to bump
from a link-shaped control: a link that wrote would fire on prefetch, on
back-button reload, and on anyone who guessed the URL.
"""

from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from reeltalk.notifications.models import Notification, mark_all_read

# A notification row is one line, shorter than a film row and far shorter
# than a review card, so this follows the 50-row films-page precedent
# rather than the genre subfeed's 20 (R95: no pruning means the page is
# the whole ledger, and the page size is the only thing keeping it
# renderable).
NOTIFICATIONS_PAGE_SIZE = 50


@login_required
def notifications(request):
    """The member's own ledger, newest first (R80's members-only shape).

    Scoped to ``recipient=request.user`` and nothing else. The guards that
    decided what may be in this set were applied at write time inside
    ``notify()`` (R97(3)), so the page must not re-filter by block here —
    re-filtering on read is exactly the residue write time exists to
    prevent, and a second filter would also be a second source of truth
    about what the user is owed.

    Reverse-chronological by ``created`` is stated on the queryset rather
    than left to ``Meta.ordering`` because a paginated query needs a
    definite order and a reader should not have to leave the view to find
    out that it has one.
    """
    paginator = Paginator(
        Notification.objects.filter(recipient=request.user)
        .select_related("actor", "status")
        .order_by("-created"),
        NOTIFICATIONS_PAGE_SIZE,
    )
    try:
        page_obj = paginator.page(request.GET.get("page"))
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        raise Http404("Page not found.") from None
    # The unread count drives the mark-all-read control, and it is the same
    # unread contract the badge will use in increment 4 — one definition,
    # two readers (R93).
    return render(
        request,
        "notifications/index.html",
        {
            "notifications": page_obj.object_list,
            "page_obj": page_obj,
            "page_query": "?",
            "unread_count": Notification.unread_for(request.user).count(),
        },
    )


@login_required
@require_POST
def mark_read(request):
    """Mark the caller's whole ledger read, then send them back to it.

    A POST rather than a side effect of opening the page, because it is a
    write: opening a page must not change state, and a GET here would let a
    prefetched link or a reload silently consume the unread state.

    It marks *everything* read, not what the current page showed — R93's
    timestamp has no per-item state, so "read this page" is not a thing
    this model can express without marking everything older anyway.
    """
    mark_all_read(request.user)
    return redirect("notifications")
