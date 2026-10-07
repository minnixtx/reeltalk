"""The list surface: the read-only pages (increment 2) and authoring (3).

Read: one list, and one member's lists. Write: create, edit, add a film,
remove one, reorder, delete. No save button, no feed row, no wire — each of
those is a later increment and the ordering is load-bearing (§2K).

Everything social on the read pages is borrowed rather than rebuilt. The like
control posts to the existing ``/status/<id>/like/`` because the thing being
liked is the list's post face (L9), not the list row; the thread is rendered
by the same ``_thread_rows`` helper and the same ``_reply.html`` partial the
post page uses, so a reply row cannot look different in the two places.

Every write below goes through ``reeltalk/lists/services.py`` and no line here
touches ``FilmList`` or ``ListItem`` directly. That is the whole point of the
service layer, and the reason the authoring form is not a ``ModelForm``.
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import (
    Http404,
    HttpResponseBadRequest,
    HttpResponseGone,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from reeltalk.core.catalog import create_or_match_film, search_local
from reeltalk.core.models import Film
from reeltalk.core.tmdb import TmdbError, is_configured, search_films
from reeltalk.core.views import (
    SUGGEST_LIMIT,
    _blocked_tmdb_ids,
    _local_film_ids,
    _local_rows,
    _thread_rows,
    _tmdb_rows,
)
from reeltalk.lists.forms import ListForm
from reeltalk.lists.models import FilmList, ListItem, ListSave
from reeltalk.lists.services import (
    MOVE_DOWN,
    MOVE_UP,
    add_films,
    create_list,
    dismiss_deleted_notice,
    move,
    remove_film,
    rename,
    save_list,
    set_description,
    soft_delete_list,
    unsave_list,
)
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
            # Whether the viewer already has a pointer at this list, so the
            # Save control renders in its Saved state rather than being
            # painted by the browser. Same one-row lookup the like state uses.
            "saved_by_viewer": request.user.is_authenticated
            and ListSave.objects.filter(
                user=request.user, film_list=film_list
            ).exists(),
            # The thread on the list's face. Flat, labelled and block-filtered
            # by the post page's own helper, so a reply under a list reads
            # exactly as a reply under a review does.
            "replies": _thread_rows(face, blocked_ids),
            # The composer opens off the query string, the same single flag
            # the reply icon carries on the post page — no second state to
            # keep in step, and it works with JavaScript switched off.
            "reply_open": request.GET.get("reply") == "1",
            # Whose list this is, decided once here rather than re-derived in
            # the template: the edit link is the only owner-only thing on
            # this page, and the routes it leads to do this same check again
            # for themselves. The comparison is on the id rather than the
            # object so an anonymous SimpleUser cannot match by accident.
            "is_owner": request.user.is_authenticated
            and request.user.pk == film_list.user_id,
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
        # R140 1 **reverses** the filter that sat here from increment 2, which
        # excluded every deleted list and called that exclusion "not optional".
        # It is no longer optional in the opposite direction: the deleted list
        # must surface carrying its notice, and drop out only once the saver has
        # dismissed that notice. So the rule is *exclude a save when the list
        # is deleted **and** the notice has already been dismissed* —
        # ``notice_dismissed_at__isnull=False``, not ``__isnull=True``. The
        # polarity is worth spelling out because the inverted form is exactly
        # as easy to write and drops the one row this whole decision exists to
        # show; the notice test catches it.
        #
        # This is also what keeps the empty state honest. ``saves`` is the set
        # with **nothing to show**, so a member whose only saved list was
        # deleted and not yet dismissed still gets their notice rather than
        # also being told they haven't saved anything.
        #
        # ``select_related`` already pulls the whole ``FilmList`` row, so the
        # template's ``save.film_list.deleted`` costs nothing extra.
        saves = (
            ListSave.objects.filter(user=profile)
            .exclude(film_list__deleted=True, notice_dismissed_at__isnull=False)
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


# --- authoring (§2K increment 3) -------------------------------------------


def _owned_list(request, list_id):
    """The viewer's own live list, or 404. The one owner check every write uses.

    R85 says a hidden control is not a permission: if the page withholds the
    edit controls from somebody who did not make the list, the routes behind
    them have to refuse that person too, or the hiding is decoration. Scoping
    the lookup to ``user=request.user`` is what does that, and it makes "not
    yours" answer the same way "does not exist" does — which is how the rest
    of this site says *you may not touch this* (``delete_review`` scopes its
    lookup for exactly this reason).

    ``deleted=False`` carries as much weight as the owner clause. A soft
    delete hides a list rather than removing it, so without the filter the
    edit route would happily take a deleted list and write to it, and the
    save-everything-else paths would find a live row nobody can see.
    """
    return get_object_or_404(FilmList, pk=list_id, user=request.user, deleted=False)


def _positive_int(raw):
    """Parse an id from a POST body, or ``None``.

    A boundary check, not a domain check. ``get_object_or_404`` on a
    non-numeric pk raises ``ValueError`` out of the ORM and turns a
    hand-typed form field into a 500; the answer to garbage input is 400,
    and the answer to a well-formed number that names nothing is 404.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _editor_redirect(request, film_list):
    """Back to the editor, with the member's search still on screen.

    The add/remove forms carry the current ``q`` and ``page`` as hidden
    fields so a member working down a search results page can add film after
    film without retyping the query. Nothing else is preserved: the redirect
    is built from the two named parameters rather than from the referer, so
    it cannot be pointed at an arbitrary URL.
    """
    params = {}
    query = request.POST.get("q", "").strip()
    if query:
        params["q"] = query
        page = _positive_int(request.POST.get("page"))
        if page and page > 1:
            params["page"] = page
    url = reverse("list-edit", args=[film_list.pk])
    return redirect(f"{url}?{urlencode(params)}" if params else url)


def _add_search(request, film_list):
    """The editor's add-film search box, as template context.

    The same path the global search page takes — TMDB when a key is
    configured, degrading to the local library on a TMDB failure rather
    than erroring the page — and the same row shape, so a result looks the
    same here as it does on ``/search/``. One thing is added: each row knows
    whether it is already in this list, because a curation tool that offers
    an "Add" button for a film that is already there reads as a broken
    button. ``add_films`` would have skipped it silently, which is the right
    write behaviour and the wrong thing to show.

    The blocked-film exclusion lives inside the shared row builders rather
    than being re-implemented here, which is why they are reused instead of
    a purpose-built list search: a second copy of that filter is a second
    place to forget it.
    """
    out = {"query": "", "rows": [], "source": None, "page": 1, "total_pages": 0}
    query = request.GET.get("q", "").strip()
    out["query"] = query
    if not query:
        return out
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        page = 1

    member_ids = set(film_list.items.values_list("film_id", flat=True))
    member_tmdb = set(
        Film.objects.filter(id__in=member_ids)
        .exclude(tmdb_id__isnull=True)
        .values_list("tmdb_id", flat=True)
    )

    if is_configured():
        try:
            results = search_films(query, page)
        except TmdbError as exc:
            messages.error(request, str(exc))
        else:
            rows = _tmdb_rows(results, request.user)
            for row in rows:
                row["in_list"] = row["tmdb_id"] in member_tmdb
            out.update(
                rows=rows,
                source="tmdb",
                page=results.page,
                total_pages=results.total_pages,
            )
            return out

    rows = _local_rows(search_local(query), request.user)
    for row in rows:
        row["in_list"] = row["film_id"] in member_ids
    out.update(rows=rows, source="local")
    return out


def list_suggest(request, list_id):
    """JSON suggestions for the editor's add-film typeahead.

    Same shape, same TMDB-first-with-local-fallback rule, same blocked-film
    exclusion and same row cap as the header's ``/search/suggest/`` — those
    are imported rather than re-derived so the two boxes cannot drift apart.
    Two things differ, and both are here because this picker *adds* rather
    than *navigates*:

    * each row carries the identifier the add route needs — ``tmdb_id`` for
      a hit that is not a local film yet, ``film_id`` for one the library
      already holds. The header box needs neither, because clicking it just
      goes to a page.
    * each row says whether it is already in this list, so the dropdown can
      show that instead of offering an add that would silently do nothing.

    Not ``@login_required``: this answers an XHR that parses JSON, and a 302
    to the login page would hand it HTML where it expects a payload — the
    same reasoning ``search_suggest`` gives for making the same choice.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login required"}, status=401)
    film_list = _owned_list(request, list_id)
    query = request.GET.get("q", "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})

    member_ids = set(film_list.items.values_list("film_id", flat=True))
    member_tmdb = set(
        Film.objects.filter(id__in=member_ids)
        .exclude(tmdb_id__isnull=True)
        .values_list("tmdb_id", flat=True)
    )

    rows = []
    if is_configured():
        try:
            results = search_films(query)
        except TmdbError:
            results = None
        if results is not None:
            blocked = _blocked_tmdb_ids(request.user)
            for hit in results.rows[:SUGGEST_LIMIT]:
                if hit.tmdb_id in blocked:
                    continue
                rows.append(
                    {
                        "title": hit.title,
                        "year": hit.year,
                        "poster_url": hit.poster_url,
                        "tmdb_id": hit.tmdb_id,
                        "film_id": None,
                        "in_list": hit.tmdb_id in member_tmdb,
                    }
                )
    if not rows:
        blocked = _local_film_ids(request.user)
        for film in search_local(query, limit=SUGGEST_LIMIT):
            if film.id in blocked:
                continue
            rows.append(
                {
                    "title": film.title,
                    "year": film.year,
                    "poster_url": film.poster.url if film.poster else None,
                    "tmdb_id": None,
                    "film_id": film.id,
                    "in_list": film.id in member_ids,
                }
            )
    return JsonResponse({"results": rows})


@login_required
def list_create(request):
    """Make a list: a name and a description, then straight into the editor.

    The create page deliberately does not collect films. A ranked row needs a
    list to hang on, so the list is made first and the films come after, from
    the editor's search — which is the single add door L5 asks for either
    way. The alternative, collecting films before the list exists, means
    holding a half-made list somewhere between the two steps, and this stack
    has no session machinery for that and no reason to grow any.

    ``create_list`` takes the **raw markdown** and renders it itself. Nothing
    here renders the description, and nothing here writes a ``FilmList``.
    """
    if request.method == "POST":
        form = ListForm(request.POST)
        if form.is_valid():
            film_list = create_list(
                request.user,
                title=form.cleaned_data["title"],
                description=form.cleaned_data["description"],
            )
            messages.success(request, "List created — now add some films.")
            return redirect("list-edit", list_id=film_list.pk)
    else:
        form = ListForm()
    return render(request, "lists/create.html", {"form": form})


@login_required
def list_edit(request, list_id):
    """Edit the member's own list: its text, its films, their order.

    The name and description are one form and one submit. Each half is
    written **only if it changed**, so pressing save without changing
    anything cannot stamp the post face as edited — an edited stamp the wire
    reports as ``editedTime`` has to mean something.

    The film rows are not part of this form. Add, remove and reorder are each
    their own POST, because they act on one row at a time and a single form
    covering all of them would have to reconcile the whole ranking on every
    submit, which is a far bigger and far easier-to-get-wrong write than the
    adjacent swap ``services.move`` actually performs.
    """
    film_list = _owned_list(request, list_id)
    if request.method == "POST":
        form = ListForm(request.POST)
        if form.is_valid():
            title = form.cleaned_data["title"]
            description = form.cleaned_data["description"]
            changed = False
            if title != film_list.title:
                rename(film_list, title)
                changed = True
            if description != film_list.raw_description:
                set_description(film_list, description)
                changed = True
            messages.success(request, "List updated." if changed else "Saved.")
            return redirect("list-edit", list_id=film_list.pk)
    else:
        form = ListForm(
            initial={
                "title": film_list.title,
                "description": film_list.raw_description,
            }
        )
    return render(
        request,
        "lists/edit.html",
        {
            "film_list": film_list,
            "form": form,
            "items": list(
                film_list.items.select_related("film").order_by("rank", "id")
            ),
            "search": _add_search(request, film_list),
        },
    )


@login_required
@require_POST
def list_add_film(request, list_id):
    """Add one film to the member's own list, from the editor's search box.

    Two ways in, matching the two sources the search box answers from. A
    ``tmdb_id`` runs the existing D7 find-or-create so the hit becomes a
    local film first — the same call the global search's one-click watchlist
    makes, so a film added to a list is created exactly the way it would be
    added to a watchlist, not a second way. A ``film_id`` adds a film the
    library already holds, which is all the no-TMDB-key fallback can offer,
    since its rows are local films already.

    Duplicate adds are a no-op with an "already in here" message rather than
    an error: ``add_films`` skips what is already present, so a double-click
    cannot fail, and the member still gets told why the row count did not move.
    """
    film_list = _owned_list(request, list_id)
    tmdb_id = request.POST.get("tmdb_id")
    film_id = request.POST.get("film_id")
    if bool(tmdb_id) == bool(film_id):
        return HttpResponseBadRequest("Expected exactly one of tmdb_id or film_id.")

    if tmdb_id:
        key = _positive_int(tmdb_id)
        if key is None:
            return HttpResponseBadRequest("Malformed tmdb_id.")
        try:
            film = create_or_match_film(key)
        except TmdbError as exc:
            messages.error(request, str(exc))
            return _editor_redirect(request, film_list)
    else:
        key = _positive_int(film_id)
        if key is None:
            return HttpResponseBadRequest("Malformed film_id.")
        try:
            film = Film.objects.get(pk=key)
        except Film.DoesNotExist:
            raise Http404 from None

    if add_films(film_list, [film]):
        messages.success(request, f"Added “{film.title}”.")
    else:
        messages.info(request, f"“{film.title}” is already in this list.")
    return _editor_redirect(request, film_list)


@login_required
@require_POST
def list_remove_film(request, list_id):
    """Take one film out of the member's own list.

    The rank gap the removal leaves is left exactly as ``remove_film`` leaves
    it. The editor prints ``forloop.counter`` like the list page does, so the
    visible numbering stays 1..N while the ranks underneath keep the holes
    they were given — nothing here renumbers to tidy them, which would break
    the read side's whole reason for not printing ``rank``.
    """
    film_list = _owned_list(request, list_id)
    key = _positive_int(request.POST.get("film_id"))
    if key is None:
        return HttpResponseBadRequest("Malformed film_id.")
    try:
        film = Film.objects.get(pk=key)
    except Film.DoesNotExist:
        raise Http404 from None

    if remove_film(film_list, film):
        messages.success(request, f"Removed “{film.title}”.")
    else:
        messages.info(request, f"“{film.title}” is not in this list.")
    return _editor_redirect(request, film_list)


@login_required
@require_POST
def list_move(request, list_id):
    """Move one row up or down inside the member's own list.

    The direction is checked at the boundary, and that is the only check here.
    ``services.move`` *raises* on an unrecognised direction because that is a
    caller's typo; a hand-typed form field is not a caller's typo, so it gets
    a 400 rather than a 500. The ends are the opposite case and are
    deliberately not pre-checked: ``move`` returns ``False`` there because
    "already first" is a normal outcome of pressing the button, and the view
    passes that straight through to an "already first" message.
    """
    film_list = _owned_list(request, list_id)
    direction = request.POST.get("direction")
    if direction not in (MOVE_UP, MOVE_DOWN):
        return HttpResponseBadRequest("direction must be 'up' or 'down'.")
    key = _positive_int(request.POST.get("item_id"))
    if key is None:
        return HttpResponseBadRequest("Malformed item_id.")
    # Scoped to this list, so an item id from somebody else's list 404s
    # rather than being moved inside ours.
    item = get_object_or_404(ListItem, pk=key, film_list=film_list)

    if move(item, direction):
        label = "up" if direction == MOVE_UP else "down"
        messages.success(request, f"Moved “{item.film.title}” {label}.")
    else:
        end = "first" if direction == MOVE_UP else "last"
        messages.info(request, f"“{item.film.title}” is already {end}.")
    return _editor_redirect(request, film_list)


@login_required
@require_POST
def list_delete(request, list_id):
    """Take the member's own list down, together with its post face.

    This route exists because of the edge increment 2 left open.
    ``delete_review`` looks up a ``Status``, and a list's face is a
    ``Status``, so a crafted POST to that route could soft-delete the face on
    its own and leave a live ``FilmList`` whose social half is gone — a list
    that can be read but never applauded, replied to, or seen in a feed. That
    route now refuses a ``LIST`` status outright, so the only way to delete a
    list is through this one, which calls ``soft_delete_list`` and takes both
    halves down in one transaction.
    """
    film_list = _owned_list(request, list_id)
    title = film_list.title
    soft_delete_list(film_list)
    messages.success(request, f"Deleted “{title}”.")
    return redirect("user-lists", localname=request.user.localname)


# --- saving (increment 5: L2 / L6 / L12, R140) ----------------------------


def _savable_list(request, list_id):
    """A list the requesting member may point their Saved tab at, or 404.

    Deliberately **not** ``_owned_list``. Saving is the one list write that is
    not owner-scoped: L4 makes every list public and L12 makes every visible
    list savable, so any member may save anybody's. Reaching for the owner
    helper here would be the wrong guard and would quietly make the feature do
    nothing at all.

    Also deliberately unfiltered on ``local=True``. Increment 7 has to save a
    remote mirror through this exact route, so baking in an assumption that
    the list lives here would leave that path blocked from the inside out.

    The three refusals are copied from ``list_detail``'s hide rules rather
    than re-derived, because R85 says a route must refuse exactly what the
    page withholds:

    * **deleted** — ``/list/<id>/`` 404s on ``deleted=False``, so there is
      no page on which saving could ever have been offered. Refusing here is
      that same rule stated on the write side, not a new one. Without it a
      hand-made POST would resurrect a pointer at a list nobody can open;
    * **suspended author** — the page 404s (R102) and ``like_status`` 404s
      on the same row for the same stated reason: a hidden post that still
      accepts writes accumulates interactions nobody can ever see the
      reason for;
    * **blocked author** — the page 404s rather than offering a lesser view,
      so the save route must not offer one either.
    """
    film_list = get_object_or_404(
        FilmList.objects.select_related("user"), pk=list_id, deleted=False
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
    return film_list


def _not_your_own_list(film_list, user):
    """Refuse a save aimed at the member's own list.

    L2 frames saving as taking a pointer at *somebody else's* list. Nothing
    in the model forbids pointing at your own — the unique constraint is on
    ``(user, film_list)``, not on the maker being a different person — so
    the rule has to be stated. It is stated here as well as in the template
    because R85 is symmetric: a control hidden from the owner's page is
    decoration unless the route behind it refuses the owner too.

    A JSON 400 rather than a 404: the list exists and the caller may see it,
    so "not found" would be false. What is wrong is the action, not the
    resource.
    """
    if film_list.user_id == user.pk:
        return JsonResponse({"error": "You can't save your own list."}, status=400)
    return None


@login_required
@require_POST
def list_save(request, list_id):
    """Save the list: put a live pointer at it on the viewer's Saved tab.

    AJAX, mirroring ``like_status`` — JSON in, JSON out, no reload, no
    messages framework. What the answer carries is the caller's own state and
    nothing else: unlike a like, a save has no tally anywhere on the page to
    keep in step, so there is no count to return and no second fact the
    client could otherwise be permitted to guess at.

    **Idempotent.** A double press lands here twice and produces one row,
    because ``save_list`` uses ``get_or_create``; the unique constraint on
    ``(user, film_list)`` would otherwise turn a double-click into an
    ``IntegrityError`` and a 500.

    **Silent, per L6.** There is no ``notify()`` call on this path and there
    never will be — no new ``Notification.Kind``, no ledger row, nothing sent
    to the maker. The proof is not that the Saved page displays no
    notification but that ``Notification.objects.count()`` is identical
    before and after a save, which is the only assertion that would notice a
    producer that existed and was merely not rendered.
    """
    film_list = _savable_list(request, list_id)
    refused = _not_your_own_list(film_list, request.user)
    if refused is not None:
        return refused
    save_list(request.user, film_list)
    return JsonResponse({"saved": True})


@login_required
@require_POST
def list_unsave(request, list_id):
    """Take the viewer's pointer at this list back off again.

    The other half of the toggle, with the same guards and the same answer
    shape. Idempotent in its own direction: a delete that matched nothing is
    a no-op that still succeeds, so a double press cannot fail here either,
    and unsaving a list that was never saved is not an error.
    """
    film_list = _savable_list(request, list_id)
    refused = _not_your_own_list(film_list, request.user)
    if refused is not None:
        return refused
    unsave_list(request.user, film_list)
    return JsonResponse({"saved": False})


@login_required
@require_POST
def list_save_dismiss(request, list_id):
    """Retire the "this list was deleted" notice on the viewer's own save.

    A form post with a redirect rather than AJAX, unlike the save toggle right
    above it, because the two controls do different things: Save flips a
    control in place, while Dismiss takes a card off a list. That is the
    ``list_remove_film`` shape — post, and let the next render come from the
    database rather than splicing the row out client-side, so there is no
    browser-side copy of the Saved tab that could drift from the real one.

    Note what this lookup does **not** filter. ``deleted=False`` would defeat
    the whole route, because the notice exists precisely on the deleted list.
    The scope is the save row and its owner; the deleted half is the
    precondition, not something to exclude.

    A live list 404s here rather than quietly accepting the marker. That keeps
    ``notice_dismissed_at`` meaning exactly one thing — this saver saw the
    notice about this list — instead of also being a field somebody can set on
    a live save for no reason.
    """
    save = get_object_or_404(
        ListSave.objects.select_related("film_list"),
        user=request.user,
        film_list_id=list_id,
    )
    if not save.film_list.deleted:
        raise Http404
    title = save.film_list.title
    dismiss_deleted_notice(request.user, save.film_list)
    messages.success(request, f"Dismissed “{title}”.")
    return redirect(f"{reverse('user-lists', args=[request.user.localname])}?tab=saved")
