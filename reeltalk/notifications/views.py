"""The notifications page and the mark-all-read control (increment 3).

R93 makes both halves of this surface cheap. The page is one indexed range
over a single recipient's rows against ``notif_recipient_created_idx``, and
marking read is one ``UPDATE`` on the user row that touches no notification
at all — so the control's cost does not scale with how much was unread.

Nothing here mutates on a GET. The page reads; ``mark_read`` is a POST with
CSRF. That split is the whole reason the mark-read timestamp is safe to bump
from a link-shaped control: a link that wrote would fire on prefetch, on
back-button reload, and on anyone who guessed the URL.

M3 (owner rulings D6 and D7) adds the row display: which rows are unseen,
and copy that names the film each row is about. Both are computed here rather
than in the template — see :class:`LedgerRow` for why each one has to be.
"""

from dataclasses import dataclass

from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from reeltalk.core.models import review_excerpt
from reeltalk.notifications.models import Notification, mark_all_read

# A notification row is one line, shorter than a film row and far shorter
# than a review card, so this follows the 50-row films-page precedent
# rather than the genre subfeed's 20 (R95: no pruning means the page is
# the whole ledger, and the page size is the only thing keeping it
# renderable).
NOTIFICATIONS_PAGE_SIZE = 50

# D7's cap on the one-line excerpt a reply or a mention carries. The rail's
# 60-char whisper (``TOP_REVIEW_EXCERPT_CHARS``) is sized for a sidebar
# column; a ledger row owns the column and can show a real sentence, so it
# gets the same truncation rule at a wider limit rather than a second rule.
NOTIFICATION_EXCERPT_CHARS = 140


@dataclass(frozen=True)
class LedgerRow:
    """One notification plus the three facts the page renders beside it.

    The template gets values, not derivations, and each of the three has its
    own reason to be here rather than in a tag:

    * ``unseen`` is a comparison of two aware datetimes (D6). A template
      filter compares what the template engine can see, which after the
      timezone conversion is strings — and the rule belongs beside
      ``Notification.unread_for``, which decides the same question for the
      badge with the same ``>`` against the same marker.
    * ``has_target`` and ``film_label`` exist because a row's subject goes
      missing two different ways — the FK is null after a hard delete, or the
      status is soft-deleted and its page 404s — and the fallback wording for
      each belongs in one place instead of three copies of the same ``{% if %}``
      stack.
    * ``excerpt`` is D7's quoted line, truncated by the rail's own rule.
    """

    note: Notification
    unseen: bool
    has_target: bool
    film_label: str
    excerpt: str


def _film_label(status) -> str:
    """The film a row is about, in the house's ``Title (Year)`` form.

    Empty when there is no film to name, which is the signal the template
    needs to fall back to the film-less wording (D7).
    """
    if status is None or status.film is None:
        return ""
    film = status.film
    return f"{film.title} ({film.year})" if film.year else film.title


def _ledger_rows(user, notes) -> list[LedgerRow]:
    """Wrap a page of rows for display (D6's unseen flag, D7's copy parts).

    Only a reply and a mention quote what arrived: a like carries no words of
    its own and a follow has no post at all, so an excerpt there would be a
    quotation of nothing.
    """
    marker = user.notifications_last_read
    rows = []
    for note in notes:
        has_target = note.status is not None and not note.status.deleted
        excerpt = ""
        if has_target and note.kind in (
            Notification.Kind.REPLY,
            Notification.Kind.MENTION,
        ):
            excerpt = review_excerpt(
                note.status.raw_content, NOTIFICATION_EXCERPT_CHARS
            )
        rows.append(
            LedgerRow(
                note=note,
                unseen=note.created > marker,
                has_target=has_target,
                film_label=_film_label(note.status) if has_target else "",
                excerpt=excerpt,
            )
        )
    return rows


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
        .select_related("actor", "status__film", "report__target_user")
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
    # two readers (R93). ``_ledger_rows`` is the third reader of it.
    return render(
        request,
        "notifications/index.html",
        {
            "notifications": _ledger_rows(request.user, page_obj.object_list),
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
