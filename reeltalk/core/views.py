"""Film views: detail, create, edit, and global search (PLAN.md §3.4/§3.7).

Views stay thin — watch-state, shelving, and review rules live in the model
layer (``mark_watched``, the shelf helpers, D5's partial index), and the
TMDB/catalog logic lives in ``tmdb``/``catalog``.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from reeltalk.activitypub.broadcast import (
    broadcast_like,
    broadcast_reply,
    broadcast_shelf_event,
    broadcast_status_create,
    broadcast_status_delete,
    broadcast_status_update,
)
from reeltalk.activitypub.identity import accepts_activitypub
from reeltalk.activitypub.objects import film_document, note_document
from reeltalk.notifications.models import Notification, notify

from .catalog import create_or_match_film, search_local
from .forms import FilmForm
from .import_export import (
    TmdbCsvError,
    export_film_csv,
    import_film_csv,
    parse_tmdb_csv,
)
from .models import (
    Film,
    Shelf,
    ShelfFilm,
    Status,
    add_reply,
    conversation,
    genre_from_slug,
    live_reviews,
    mark_watched,
    resolve_film_id,
    shelve_to_watchlist,
    toggle_like,
    unshelve_from_watchlist,
)
from .tmdb import TmdbError, is_configured, search_films
from .utils import render_markdown


def film_detail(request, film_id):
    """A film: metadata, poster, reviews from all users (§3.7).

    ActivityPub clients get the **Film** wire document (D15) by content
    negotiation — the same split as the actor endpoint (R40) — so a remote
    instance can fetch a referenced film object by its id URL.
    """
    # Absorbed films' URLs keep resolving to the canonical row (§3.2).
    film = get_object_or_404(Film, id=resolve_film_id(film_id))
    if accepts_activitypub(request):
        return JsonResponse(
            film_document(film, request),
            content_type="application/activity+json",
        )
    reviews = (
        Status.objects.filter(
            film=film, status_type__in=list(Status.REVIEW_TYPES), deleted=False
        )
        .select_related("user")
        .order_by("-published_date")
    )
    if request.user.is_authenticated:
        # R56: hide the reviews of users this viewer has blocked. Blocking is
        # per-logged-in-user state, so anonymous visitors see every review.
        blocked_ids = set(request.user.blocks.values_list("id", flat=True))
        if blocked_ids:
            reviews = reviews.exclude(user_id__in=blocked_ids)
    data = {"film": film, "reviews": reviews}
    if request.user.is_authenticated:
        # D1 binary state drives the shelve controls on the page.
        shelf_ids = set(
            ShelfFilm.objects.filter(user=request.user, film=film)
            .select_related("shelf")
            .values_list("shelf__identifier", flat=True)
        )
        data["on_watchlist"] = Shelf.TO_READ in shelf_ids
        data["is_watched"] = Shelf.READ in shelf_ids
        # The user's own review/rating (D5) pre-fills the finish/edit modal.
        data["user_review"] = (
            Status.objects.filter(
                user=request.user,
                film=film,
                status_type__in=list(Status.REVIEW_TYPES),
                deleted=False,
            )
            .order_by("id")
            .first()
        )
        # The block-film control's state (R55).
        data["is_film_blocked"] = request.user.blocked_films.filter(pk=film.id).exists()
    return render(request, "core/film/detail.html", data)


def _reply_to_label(parent, root, blocked_ids) -> str:
    """Who a thread row reads as answering — ``""`` for a direct reply.

    A flat thread still has to say who each turn is addressed to. When the
    row we would name is itself hidden from this viewer we say that instead
    of naming it: printing a blocked author's handle on a page whose whole
    block rule is that they are not here would leak the one thing blocking
    is meant to hide, and naming a deleted reply would point at a row the
    reader cannot see.
    """
    if parent.pk == root.pk:
        return ""
    if parent.user_id in blocked_ids:
        return "a hidden reply"
    if parent.deleted:
        return "a deleted reply"
    return parent.user.localname


def _thread_rows(root, blocked_ids) -> list[tuple[Status, str]]:
    """The thread as the post page renders it: live, flat, labelled.

    ``(reply, reply_to)`` pairs in conversation order. Blocking is applied
    here rather than inside ``conversation`` so the read-side rule stays in
    the view with every other read-side block rule (R56), and so a blocked
    author's reply does not take its live replies down with it — the walk
    already goes through hidden rows, and dropping a node here only drops
    that node.
    """
    return [
        (reply, _reply_to_label(parent, root, blocked_ids))
        for reply, parent in conversation(root)
        if reply.user_id not in blocked_ids
    ]


def status_detail(request, status_id):
    """A post: the human page, or its Note wire document (R83 decision 3).

    One URL, two arms, negotiated the same way as the actor and film
    endpoints (R40). ActivityPub clients get the **Note** document at its
    id, and only for **local** statuses — a mirror's canonical id is its
    home instance's URL, and we never mint identity for another instance's
    object (R41/R42). Browsers get the human page for local statuses
    *and* mirrors: that is the whole point of reusing this URL, and it is
    what makes a federated review in the home feed openable here at all —
    before this increment a remote post had no resolvable page on this
    instance for anybody.

    The human page is publicly readable, anonymously (decision 6, as film
    pages are under R56). The block rule matches ``film_detail`` exactly:
    a logged-in viewer who has blocked the author gets 404 rather than a
    lesser view, and anonymity hides nothing because blocking is
    per-viewer state. Replies are filtered by the same rule. Deleted
    statuses are tombstones and are served by neither arm.
    """
    status = get_object_or_404(
        Status.objects.select_related("reply_parent"), id=status_id, deleted=False
    )
    if accepts_activitypub(request):
        if not status.local:
            raise Http404
        return JsonResponse(
            note_document(status, request),
            content_type="application/activity+json",
        )
    blocked_ids = (
        set(request.user.blocks.values_list("id", flat=True))
        if request.user.is_authenticated
        else set()
    )
    if status.user_id in blocked_ids:
        raise Http404
    # The thread: every live reply under this post, flat and in conversation
    # order. The depth policy is written out on ``conversation``; the short
    # form is that the data keeps its real nesting (so inReplyTo stays
    # honest) and the page refuses to render it as indentation.
    replies = _thread_rows(status, blocked_ids)
    return render(
        request,
        "core/status/detail.html",
        {
            "status": status,
            "replies": replies,
            # A reply whose parent is a tombstone says so out loud. The
            # deleted post itself 404s, so this page is the only place the
            # orphaned reply can be read — and a reply that reads as
            # addressed to nothing is the silent-vanish failure in another
            # form. ``reply_parent`` is PROTECT, so the row is still here
            # precisely because soft-delete kept it.
            "parent_deleted": bool(
                status.reply_parent_id and status.reply_parent.deleted
            ),
            # The like control's state (increment 3). Mirrors carry no
            # control — see ``FeedEntry.interactive`` for the same gate on
            # a feed row — but the count is shown for them too, because a
            # like count is a fact about the post rather than an offer.
            "like_count": status.likes.count(),
            "liked_by_viewer": request.user.is_authenticated
            and status.likes.filter(user=request.user).exists(),
        },
    )


@login_required
@require_POST
def reply_to_status(request, status_id):
    """Reply to a post (feed interactions increment 4, R83 decision 2).

    The first writer ``Status.reply_parent`` ever had. AJAX, shaped like
    ``like_status``: csrf from the base.html meta tag, one round trip, no
    reload. The answer carries the new row rendered by the **same Django
    partial the page itself loops over**, so there is one source of truth
    for a reply row's markup — the client inserts what the server rendered
    rather than rebuilding it in JS and drifting from the template.

    The lookup is no longer scoped to ``local=True``. R85's rule is that the
    route refuses exactly what the page withholds, and increment 5 put a
    threaded ``Create`` on the wire, so the page now offers the composer on
    a mirror too — which means the route has to accept one. The four halves
    of that gate (this lookup, the like lookup, ``FeedEntry.interactive``,
    and the post page's control gate) move together; opening some and not
    others is the button-with-no-route / route-behind-no-button bug R85
    exists to prevent.

    A reply to a mirror is a local ``comment`` whose ``reply_parent`` is
    another instance's row. That is deliberate and it is why
    ``objects.note_reference`` exists: the ``inReplyTo`` we emit carries the
    parent's *home* URL, so the instance that owns that turn sees its own
    post being answered rather than a URL of ours.
    """
    parent = get_object_or_404(Status, id=status_id, deleted=False)
    raw_content = request.POST.get("content", "")
    if not raw_content.strip():
        return JsonResponse({"error": "A reply needs some text."}, status=400)
    try:
        reply = add_reply(
            request.user,
            parent,
            content=render_markdown(raw_content, mentions=True),
            raw_content=raw_content,
        )
    except ValueError as exc:
        # ``Status.save`` refuses a typed status with no film, which is what
        # a reply to a film-less post would be. v0.1 cannot create such a
        # *local* post, but a remote one arrives whenever a Mastodon note
        # with no film reference is mirrored — so opening the gate is what
        # made this path reachable, and the composer is withheld on exactly
        # those pages (``detail.html`` gates on ``status.film``) to keep the
        # offer and the refusal agreeing. A 400 rather than a stack trace
        # either way.
        return JsonResponse({"error": str(exc)}, status=400)
    # Federation broadcast (increment 5): the threaded Create to the post's
    # author and the replier's remote followers. A dead recipient drops its
    # send — this request must not fail because of one unreachable instance.
    broadcast_reply(request, reply)
    # The parent author's notification (notifications increment 2, R92).
    # This is the local half of the reply pair; the federated half is written
    # by ``_mirror_status``. ``notify()`` owns who actually gets told — it
    # no-ops when the parent is the replier's own post, and a reply to a
    # mirror of a remote post has a remote author for exactly that reason.
    notify(parent.user, request.user, Notification.Kind.REPLY, reply)
    blocked_ids = set(request.user.blocks.values_list("id", flat=True))
    return JsonResponse(
        {
            "html": render_to_string(
                # The composer always answers the post you are reading, so
                # a composed reply is a direct reply and carries no
                # "replying to" label.
                "core/status/_reply.html",
                {"reply": reply, "reply_to": ""},
                request,
            ),
            # The number the heading has to show after this row lands —
            # counted the same way the page counts it, not guessed here.
            "count": len(_thread_rows(parent, blocked_ids)),
        }
    )


@login_required
@require_POST
def like_status(request, status_id):
    """Toggle the viewer's like on a status (increment 3, R83 decision 4).

    AJAX endpoint returning JSON — no reload, no messages framework. The
    response carries the new count as well as the caller's own state, so
    one round trip updates both the button and the tally beside it.

    The lookup is no longer scoped to ``local=True``. Increment 5 made a
    like on another instance's post deliverable, so the control now shows on
    a mirror and the route accepts one — the same reason it used to refuse
    was the reason to withhold the button, and both halves came out together
    (R85). Liking a mirror writes a local ``Like`` row against *our* mirror
    of their post and sends the ``Like`` to their author; unliking deletes
    the row and sends the ``Undo``.
    """
    status = get_object_or_404(Status, id=status_id, deleted=False)
    liked = toggle_like(request.user, status)
    # Federation broadcast (increment 5): the Like / Undo(Like) to the
    # post's author, and nothing at all when that author is local. A dead
    # recipient drops its send — this request must not fail because of one
    # unreachable instance.
    broadcast_like(request, status, request.user, liked=liked)
    # The author's notification (notifications increment 2, R92). Only the
    # like half of the toggle is an event: taking a like back is not a
    # second "liked this" for the ledger. Unlike the follow route, this one
    # carries no self-guard upstream, so a member liking their own post is
    # reachable and arrives here — ``notify()`` is what stops it.
    if liked:
        notify(status.user, request.user, Notification.Kind.LIKE, status)
    return JsonResponse({"liked": liked, "count": status.likes.count()})


@login_required
@require_POST
def mark_watched_view(request, film_id):
    """Finish flow (§3.3): mark a film watched with a required star rating.

    The view is thin — rating validation, shelving (Watched + off Watchlist),
    and the create-or-update-in-place review rule (D5) all live in
    ``mark_watched``. A missing/invalid rating raises before any write; we
    surface it as a message and send the user back to the film page.
    """
    user = request.user
    film = get_object_or_404(Film, id=resolve_film_id(film_id))
    raw_content = request.POST.get("content", "")
    try:
        # Both checked before the write: which broadcast the success takes
        # (create vs update) and whether mark_watched takes the film off the
        # Watchlist (D1) — so that removal is broadcast too.
        existing_review = Status.objects.filter(
            user=user,
            film=film,
            status_type__in=list(Status.REVIEW_TYPES),
            deleted=False,
        ).exists()
        was_on_watchlist = ShelfFilm.objects.filter(
            user=user, shelf__identifier=Shelf.TO_READ, film=film
        ).exists()
        status = mark_watched(
            user,
            film,
            rating=request.POST.get("rating"),
            content=render_markdown(raw_content, mentions=True),
            raw_content=raw_content,
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("film", film_id=film.id)
    # Federation broadcast (M4 increment 6): the review to remote followers,
    # plus the watchlist removal when mark_watched took the film off it. A
    # dead follower drops its send — this request must not fail because of
    # one unreachable instance.
    if existing_review:
        broadcast_status_update(request, status)
    else:
        broadcast_status_create(request, status)
    if was_on_watchlist:
        broadcast_shelf_event(request, user, film, Shelf.TO_READ, added=False)
    messages.success(request, "Marked as watched.")
    return redirect("film", film_id=film.id)


@login_required
@require_POST
def delete_review(request, status_id):
    """Delete the user's own review (M5 increment 4).

    Author-only: the lookup is scoped to the requesting user's local,
    non-deleted statuses, so someone else's review, a remote mirror, or an
    already-deleted tombstone all 404. Soft-delete keeps the row as a
    tombstone with its identity intact (§3.2) and clears its content; the
    film stays on the Watched shelf (v0.1 has no unwatch — R19), so the feed
    shows a bare "watched" entry (R35). The deletion is broadcast to the
    author's remote followers (M4 increment 6); local followers read the
    local tombstone their feed query already filters on ``deleted=False``.
    """
    status = get_object_or_404(
        Status, id=status_id, user=request.user, local=True, deleted=False
    )
    film_id = status.film_id
    status.delete()
    # Federation broadcast (M4 increment 6): the deletion to remote followers.
    # A dead follower drops its send — this request must not fail because of
    # one unreachable instance.
    broadcast_status_delete(request, status)
    messages.success(request, "Your review has been deleted.")
    if film_id is not None:
        return redirect("film", film_id=film_id)
    return redirect("index")


@login_required
@require_POST
def shelve(request, film_id):
    """Add a film to the user's Watchlist (D1 mutual exclusion enforced)."""
    film = get_object_or_404(Film, id=resolve_film_id(film_id))
    outcome = shelve_to_watchlist(request.user, film)
    if outcome == "added":
        # Federation broadcast (M4 increment 6): the shelf event to remote
        # followers ("already" and "watched" change nothing on the wire).
        broadcast_shelf_event(request, request.user, film, Shelf.TO_READ, added=True)
    notices = {
        "added": ("success", "Added to your watchlist."),
        "already": ("info", "Already on your watchlist."),
        "watched": ("warning", "This film is in your Watched list."),
    }
    level, text = notices[outcome]
    getattr(messages, level)(request, text)
    return redirect("film", film_id=film.id)


@login_required
@require_POST
def unshelve(request, film_id):
    """Remove a film from the user's Watchlist."""
    film = get_object_or_404(Film, id=resolve_film_id(film_id))
    if unshelve_from_watchlist(request.user, film):
        # Federation broadcast (M4 increment 6): the removal to remote
        # followers.
        broadcast_shelf_event(request, request.user, film, Shelf.TO_READ, added=False)
        messages.success(request, "Removed from your watchlist.")
    else:
        messages.info(request, "Not on your watchlist.")
    return redirect("film", film_id=film.id)


@login_required
@require_POST
def film_block(request, film_id):
    """Block a film (M5 increment 3, R55): local-only ``User.blocked_films``.

    Read-side state — in v0.1 the effect is exclusion from search (already
    wired in M2); nothing is delivered over federation and the film page
    itself stays reachable by direct URL.
    """
    film = get_object_or_404(Film, id=resolve_film_id(film_id))
    request.user.blocked_films.add(film)
    messages.success(request, f"You have blocked “{film.title}”.")
    return redirect("film", film_id=film.id)


@login_required
@require_POST
def film_unblock(request, film_id):
    """Unblock a film (M5 increment 3, R55)."""
    film = get_object_or_404(Film, id=resolve_film_id(film_id))
    request.user.blocked_films.remove(film)
    messages.success(request, f"You have unblocked “{film.title}”.")
    return redirect("film", film_id=film.id)


@login_required
def film_create(request):
    """Create a manual film (no tmdb_id — fully editable per D4)."""
    if request.method == "POST":
        form = FilmForm(request.POST, request.FILES)
        if form.is_valid():
            data = form.cleaned_data
            # D7: don't create an obvious duplicate — match on title + year.
            match = Film.find_match(title=data["title"], year=data.get("year"))
            if match is not None:
                messages.info(
                    request, "A film with this title and year already exists."
                )
                return redirect("film", film_id=match.id)
            film = form.save()
            messages.success(request, "Film created.")
            return redirect("film", film_id=film.id)
    else:
        form = FilmForm()
    return render(request, "core/film/form.html", {"form": form})


@login_required
def film_edit(request, film_id):
    """Edit a manual film. D4: TMDB-sourced films are locked from editing."""
    film = get_object_or_404(Film, id=resolve_film_id(film_id))
    if film.tmdb_id:
        messages.info(request, "Films with TMDB metadata are locked from editing.")
        return redirect("film", film_id=film.id)
    if request.method == "POST":
        form = FilmForm(request.POST, request.FILES, instance=film)
        if form.is_valid():
            form.save()
            messages.success(request, "Film updated.")
            return redirect("film", film_id=film.id)
    else:
        form = FilmForm(instance=film)
    return render(request, "core/film/form.html", {"form": form, "film": film})


# --- Global search (M2, D6) -------------------------------------------------

SUGGEST_LIMIT = 8


def _blocked_tmdb_ids(user) -> set:
    """tmdb_ids of films this user has blocked locally (§3.4 exclusion)."""
    return set(
        Film.objects.filter(id__in=user.blocked_films.values_list("id", flat=True))
        .exclude(tmdb_id__isnull=True)
        .values_list("tmdb_id", flat=True)
    )


def _local_film_ids(user) -> set:
    """Ids of films this user has blocked locally (§3.4 exclusion)."""
    if not user.is_authenticated:
        return set()
    return set(user.blocked_films.values_list("id", flat=True))


def _tmdb_rows(results, user) -> list[dict]:
    """Normalize a TMDB search page into template rows (D6).

    Anonymous users see results without actions — no click-through link.
    Locally blocked films are excluded by tmdb_id.
    """
    blocked = _blocked_tmdb_ids(user) if user.is_authenticated else set()
    rows = []
    for hit in results.rows:
        if hit.tmdb_id in blocked:
            continue
        rows.append(
            {
                "title": hit.title,
                "year": hit.year,
                "poster_url": hit.poster_url,
                "tmdb_id": hit.tmdb_id,
                "link": (
                    reverse("search-clickthrough", args=[hit.tmdb_id])
                    if user.is_authenticated
                    else None
                ),
            }
        )
    return rows


def _local_rows(films, user) -> list[dict]:
    """Normalize local films into the same row shape as TMDB results."""
    blocked = _local_film_ids(user)
    rows = []
    for film in films:
        if film.id in blocked:
            continue
        rows.append(
            {
                "title": film.title,
                "year": film.year,
                "poster_url": film.poster.url if film.poster else None,
                "tmdb_id": film.tmdb_id,
                # Film pages are public — the link is available to everyone.
                "link": reverse("film", args=[film.id]),
            }
        )
    return rows


@login_required
def film_search(request):
    """Global search page (D6): TMDB when a key is configured, else local.

    With a key, a TMDB failure (bad key / rate limit / network) degrades to
    the local library with a user-facing message instead of an error page.

    Login-gated (R80): every query spends the instance's shared paid TMDB
    quota, so the whole search surface is members-only.
    """
    query = request.GET.get("q", "").strip()
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        page = 1

    data = {"query": query, "rows": [], "source": None, "page": page, "total_pages": 0}
    if not query:
        return render(request, "core/search.html", data)

    if is_configured():
        try:
            results = search_films(query, page)
        except TmdbError as exc:
            messages.error(request, str(exc))
            data["rows"] = _local_rows(search_local(query), request.user)
            data["source"] = "local"
        else:
            data["rows"] = _tmdb_rows(results, request.user)
            data["source"] = "tmdb"
            data["page"] = results.page
            data["total_pages"] = results.total_pages
    else:
        data["rows"] = _local_rows(search_local(query), request.user)
        data["source"] = "local"
    return render(request, "core/search.html", data)


@login_required
def search_clickthrough(request, tmdb_id):
    """D6 click-through: run D7 create-or-match and land on the film page."""
    existed = Film.objects.filter(tmdb_id=tmdb_id).exists()
    try:
        film = create_or_match_film(tmdb_id)
    except TmdbError as exc:
        messages.error(request, str(exc))
        return redirect("film-search")
    if not existed:
        messages.success(request, f"Added “{film.title}” to your library.")
    return redirect("film", film_id=film.id)


@login_required
@require_POST
def search_watchlist(request, tmdb_id):
    """D6 one-click watchlist: materialize the TMDB hit (D7), then shelve.

    JSON for the results-page button — ``status`` is the shelf outcome
    (added/already/watched); a TMDB failure comes back as an error payload.
    """
    try:
        film = create_or_match_film(tmdb_id)
    except TmdbError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    outcome = shelve_to_watchlist(request.user, film)
    if outcome == "watched":
        # D1: a watched film can't also be wanted — refuse with 409.
        return JsonResponse({"status": outcome}, status=409)
    if outcome == "added":
        # Federation broadcast (M4 increment 6): same shelf event as the
        # film-page control — this is a second entry point onto the Watchlist.
        broadcast_shelf_event(request, request.user, film, Shelf.TO_READ, added=True)
    return JsonResponse({"status": outcome})


def search_suggest(request):
    """JSON suggestions for the header dropdown (search-as-you-type, D6).

    TMDB when a key is configured (falling back to the local library on a
    TMDB failure or empty hit list), local only without a key. Locally
    blocked films are excluded.

    Members-only like the rest of the surface (R80), but *not* via
    ``@login_required``: this answers an XHR that parses JSON, and a 302 to
    the login page would hand the client HTML where it expects a payload.
    Anonymous callers get an explicit 401 instead.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login required"}, status=401)

    query = request.GET.get("q", "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})

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
                        "url": reverse("search-clickthrough", args=[hit.tmdb_id]),
                    }
                )
    if not rows:
        films = search_local(query, limit=SUGGEST_LIMIT)
        blocked = _local_film_ids(request.user)
        for film in films:
            if film.id in blocked:
                continue
            rows.append(
                {
                    "title": film.title,
                    "year": film.year,
                    "poster_url": film.poster.url if film.poster else None,
                    "tmdb_id": None,
                    "url": reverse("film", args=[film.id]),
                }
            )
    return JsonResponse({"results": rows[:SUGGEST_LIMIT]})


# --- File import/export (M3, D9/D10) -----------------------------------------


@login_required
def import_films(request):
    """TMDB-CSV import (D9): upload page, then per-row results + summary.

    The view only handles the upload — utf-8-sig decode (TMDB exports carry a
    BOM), header validation, and the row cap; matching/shelving/rating all
    live in ``import_export``. The D11 backfill for the imported ID stubs is
    queued on commit inside ``import_film_csv``.
    """
    if not request.user.local:
        # Import is a local-user feature (§3.5); remote mirrors are M4.
        messages.error(request, "Film import is only available on your home instance.")
        return redirect("user-films", localname=request.user.localname)

    data = {}
    if request.method == "POST":
        upload = request.FILES.get("csv_file")
        if not upload:
            messages.error(request, "Choose a CSV file to import.")
        else:
            try:
                text = upload.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                messages.error(
                    request, "That file isn't readable UTF-8; expected a CSV export."
                )
            else:
                try:
                    rows = parse_tmdb_csv(text)
                except TmdbCsvError as exc:
                    messages.error(request, str(exc))
                else:
                    data = import_film_csv(request.user, rows)
    return render(request, "core/import_films.html", data)


@login_required
def export_films(request):
    """TMDB-CSV export (D10): a page with a download button; POST streams CSV."""
    if request.method == "POST":
        disposition = 'attachment; filename="reeltalk-export.csv"'
        return HttpResponse(
            export_film_csv(request.user),
            content_type="text/csv",
            headers={"Content-Disposition": disposition},
        )
    return render(request, "core/export_films.html")


# --- Genre subfeed (M6 artwork sub-increment C, R64) -------------------------

# Review cards run long, so a page holds fewer rows than the 50-row films list.
GENRE_PAGE_SIZE = 20


def genre(request, slug):
    """A genre subfeed: the instance's reviews of films in that genre (R64).

    A "Popular Genres" pill lands here — newest review first, every author
    whose review this instance holds (mirrors included). The film page is the
    target of each row's title link, so the subfeed reads as a feed rather
    than a list of names. R55/R56 read-side rules apply to a signed-in viewer:
    reviews of locally blocked films and by blocked users stay out. Unknown or
    stale slugs 404 — ``genre_from_slug`` knows only genres carrying reviews,
    so a pill never leads to an empty page.
    """
    genre_row = genre_from_slug(slug)
    if genre_row is None:
        raise Http404("No such genre.")
    reviews = (
        live_reviews()
        .filter(film__genres__contains=[genre_row.name])
        .select_related("user", "film")
        .order_by("-published_date")
    )
    if request.user.is_authenticated:
        reviews = reviews.exclude(film_id__in=_local_film_ids(request.user))
        blocked_user_ids = set(request.user.blocks.values_list("id", flat=True))
        if blocked_user_ids:
            reviews = reviews.exclude(user_id__in=blocked_user_ids)
    paginator = Paginator(reviews, GENRE_PAGE_SIZE)
    try:
        page_obj = paginator.page(request.GET.get("page"))
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        raise Http404("Page not found.") from None
    return render(
        request,
        "core/genre.html",
        {
            "genre": genre_row,
            "reviews": page_obj.object_list,
            "page_obj": page_obj,
            "page_query": "?",
        },
    )
