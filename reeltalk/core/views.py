"""Film views: detail, create, edit, and global search (PLAN.md §3.4/§3.7).

Views stay thin — watch-state, shelving, and review rules live in the model
layer (``mark_watched``, the shelf helpers, D5's partial index), and the
TMDB/catalog logic lives in ``tmdb``/``catalog``.
"""

from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from reeltalk.activitypub.broadcast import (
    broadcast_shelf_event,
    broadcast_status_create,
    broadcast_status_update,
)
from reeltalk.activitypub.identity import accepts_activitypub
from reeltalk.activitypub.objects import film_document, note_document

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
    mark_watched,
    resolve_film_id,
    shelve_to_watchlist,
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
    return render(request, "core/film/detail.html", data)


def status_detail(request, status_id):
    """A status's wire URL (R41, deferred to M4 increment 6).

    ActivityPub clients get the **Note** document at its id — the fetch
    endpoint for objects that ride inline elsewhere. Only local statuses are
    served: a remote mirror's canonical id is its home instance's URL, not
    this row's, and there is no human-facing status page in v0.1 (M5), so
    other clients get 404. Deleted statuses are tombstones — not served.
    """
    status = get_object_or_404(Status, id=status_id, local=True, deleted=False)
    if not accepts_activitypub(request):
        return HttpResponse(status=404)
    return JsonResponse(
        note_document(status, request), content_type="application/activity+json"
    )


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
            content=render_markdown(raw_content),
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


def film_search(request):
    """Global search page (D6): TMDB when a key is configured, else local.

    With a key, a TMDB failure (bad key / rate limit / network) degrades to
    the local library with a user-facing message instead of an error page.
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
    TMDB failure or empty hit list), local only without a key. Anonymous
    users' TMDB rows link to the search page rather than the login-gated
    click-through route. Locally blocked films are excluded.
    """
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
            authenticated = request.user.is_authenticated
            blocked = _blocked_tmdb_ids(request.user) if authenticated else set()
            for hit in results.rows[:SUGGEST_LIMIT]:
                if hit.tmdb_id in blocked:
                    continue
                rows.append(
                    {
                        "title": hit.title,
                        "year": hit.year,
                        "poster_url": hit.poster_url,
                        "tmdb_id": hit.tmdb_id,
                        "url": (
                            reverse("search-clickthrough", args=[hit.tmdb_id])
                            if authenticated
                            else f"/search/?q={quote(hit.title)}"
                        ),
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
