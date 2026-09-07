"""Film views: detail, create, and edit pages (PLAN.md §3.7).

Views stay thin — watch-state, shelving, and review rules live in the model
layer (``mark_watched``, the shelf helpers, D5's partial index).
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import FilmForm
from .models import Film, Status, resolve_film_id


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
    return render(request, "core/film/detail.html", {"film": film, "reviews": reviews})


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
