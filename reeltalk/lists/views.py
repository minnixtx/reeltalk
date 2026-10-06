"""The read-only list surface (§2K increment 2, R137/R138).

Two pages and nothing else: one list, and one member's lists. No create, no
edit, no reorder, no save, no feed row, no wire — each of those is a later
increment and the ordering is load-bearing (§2K), so lists exist only from
fixtures here.

Everything social on these pages is borrowed rather than rebuilt. The like
control posts to the existing ``/status/<id>/like/`` because the thing being
liked is the list's post face (L9), not the list row; the thread is rendered
by the same ``_thread_rows`` helper and the same ``_reply.html`` partial the
post page uses, so a reply row cannot look different in the two places.
"""

from django.db.models import Count
from django.http import Http404, HttpResponseGone
from django.shortcuts import get_object_or_404, redirect, render

from reeltalk.core.views import _thread_rows
from reeltalk.lists.models import FilmList, ListSave
from reeltalk.social.views import _resolve_profile_user


def list_detail(request, list_id):
    """One list: its head, its ranked films, and the thread on its face (L9/L10).

    Publicly readable, anonymously, exactly as the post page is (L4 makes a
    list public, so there is no privacy rule to apply on this read path). The
    hide rules are copied from ``status_detail`` rather than re-derived,
    because this page renders the same ``Status`` under another name and the
    two must not disagree about who can see it:

    * a **deleted** list 404s — ``FilmList.delete()`` is soft, so the row is
      still in the table and the filter is what hides it. Every read path this
      increment adds carries that filter for the same reason;
    * a **suspended** author's list 404s (R102) — suspension is a
      server-level answer, so the whole page goes, not just part of it;
    * a viewer who has **blocked** the author 404s rather than getting a
      lesser view, the same per-viewer rule the post page and the film page
      apply.

    The ranked rows are numbered by ``forloop.counter`` in the template, never
    by ``rank``: ``remove_film`` leaves the gap it makes, so ranks can read
    1, 2, 4 while the list has three films.
    """
    film_list = get_object_or_404(
        FilmList.objects.select_related("user", "status"),
        pk=list_id,
        deleted=False,
    )
    if film_list.user.suspended_at is not None:
        raise Http404
    blocked_ids = (
        set(request.user.blocks.values_list("id", flat=True))
        if request.user.is_authenticated
        else set()
    )
    if film_list.user_id in blocked_ids:
        raise Http404

    face = film_list.status
    items = list(film_list.items.select_related("film").order_by("rank", "id"))
    # The same tally the post page computes for the same row, by the same
    # path — a raw count that names the suspension clause itself rather than
    # going through ``like_counts()``, which batches and does not apply to a
    # single-object page.
    like_count = face.likes.filter(user__suspended_at__isnull=True).count()
    return render(
        request,
        "lists/detail.html",
        {
            "film_list": film_list,
            "items": items,
            "like_count": like_count,
            "liked_by_viewer": request.user.is_authenticated
            and face.likes.filter(user=request.user).exists(),
            # The thread on the list's face. Flat, labelled and block-filtered
            # by the post page's own helper, so a reply under a list reads
            # exactly as a reply under a review does.
            "replies": _thread_rows(face, blocked_ids),
            # The composer opens off the query string, the same single flag
            # the reply icon carries on the post page — no second state to
            # keep in step, and it works with JavaScript switched off.
            "reply_open": request.GET.get("reply") == "1",
        },
    )


def user_lists(request, localname):
    """One member's lists, at ``/user/<localname>/lists/`` (L7, R138).

    The URL mirrors ``user/<localname>/films/`` rather than being a top-level
    ``/lists/``, which is what makes the header's "My Lists" item and the
    profile's "Lists" tab one page with two entry points instead of two
    pages. The account-level answers (banned, suspended, unknown) are the
    same three the films page gives in the same order, because a per-user tab
    that answers differently from its sibling is two answers where one
    would do.

    Only lists the member **made** appear by default. **Two tabs, and only on
    your own page** (owner decision, 2026-10-06): *My Lists* — the lists you
    made — and *Saved Lists* — the lists you saved from other members. On
    somebody else's page the tab row is absent entirely and the ``tab``
    param is ignored rather than honoured, because a ``ListSave`` row
    records what one member chose to keep and nothing in R137 or L4 made
    that public. Falling back to the made-lists view instead of 404-ing is
    how ``user_films`` treats an unrecognised tab.

    The Saved tab renders today with an empty state. The save **button** and
    its write path are increment 5; the read side is here so the shape under
    review is the real thing rather than a stub.
    """
    profile = _resolve_profile_user(localname)
    if profile is None:
        raise Http404("No such user")
    if profile.banned_at is not None:
        return HttpResponseGone("This account has been removed.")
    if profile.suspended_at is not None:
        return redirect("user-profile", localname=profile.localname)

    is_self = request.user.is_authenticated and request.user.pk == profile.pk
    tab = "saved" if is_self and request.GET.get("tab") == "saved" else "made"
    made = saves = []
    if tab == "saved":
        # ``film_list__deleted=False`` is not optional: FilmList.delete() is
        # soft, so a save can be pointing at a list that is gone.
        saves = (
            ListSave.objects.filter(user=profile, film_list__deleted=False)
            .select_related("film_list", "film_list__user")
            .annotate(item_count=Count("film_list__items"))
            .order_by("-created")
        )
    else:
        made = (
            FilmList.objects.filter(user=profile, deleted=False)
            .annotate(item_count=Count("items"))
            .order_by("-created_date", "-id")
        )
    return render(
        request,
        "lists/user_lists.html",
        {
            "profile_user": profile,
            "is_self": is_self,
            "tab": tab,
            "made": made,
            "saves": saves,
        },
    )
