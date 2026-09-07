"""Film views: the film detail page (PLAN.md §3.7).

Views stay thin — watch-state, shelving, and review rules live in the model
layer (``mark_watched``, the shelf helpers, D5's partial index).
"""

from django.shortcuts import get_object_or_404, render

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
