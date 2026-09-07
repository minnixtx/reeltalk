"""Film views: detail, create, and edit pages (PLAN.md §3.7).

Views stay thin — watch-state, shelving, and review rules live in the model
layer (``mark_watched``, the shelf helpers, D5's partial index).
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import FilmForm
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
from .utils import render_markdown


def film_detail(request, film_id):
    """A film: metadata, poster, reviews from all users (§3.7)."""
    # Absorbed films' URLs keep resolving to the canonical row (§3.2).
    film = get_object_or_404(Film, id=resolve_film_id(film_id))
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


@login_required
@require_POST
def mark_watched_view(request, film_id):
    """Finish flow (§3.3): mark a film watched with a required star rating.

    The view is thin — rating validation, shelving (Watched + off Watchlist),
    and the create-or-update-in-place review rule (D5) all live in
    ``mark_watched``. A missing/invalid rating raises before any write; we
    surface it as a message and send the user back to the film page.
    """
    film = get_object_or_404(Film, id=resolve_film_id(film_id))
    raw_content = request.POST.get("content", "")
    try:
        mark_watched(
            request.user,
            film,
            rating=request.POST.get("rating"),
            content=render_markdown(raw_content),
            raw_content=raw_content,
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("film", film_id=film.id)
    messages.success(request, "Marked as watched.")
    return redirect("film", film_id=film.id)


@login_required
@require_POST
def shelve(request, film_id):
    """Add a film to the user's Watchlist (D1 mutual exclusion enforced)."""
    film = get_object_or_404(Film, id=resolve_film_id(film_id))
    notices = {
        "added": ("success", "Added to your watchlist."),
        "already": ("info", "Already on your watchlist."),
        "watched": ("warning", "This film is in your Watched list."),
    }
    level, text = notices[shelve_to_watchlist(request.user, film)]
    getattr(messages, level)(request, text)
    return redirect("film", film_id=film.id)


@login_required
@require_POST
def unshelve(request, film_id):
    """Remove a film from the user's Watchlist."""
    film = get_object_or_404(Film, id=resolve_film_id(film_id))
    if unshelve_from_watchlist(request.user, film):
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
