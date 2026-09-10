"""URLs for the core (film) app."""

from django.urls import path

from . import views

urlpatterns = [
    # Global search (M2, D6): results page, click-through create-or-match,
    # one-click watchlist, and the header suggest endpoint.
    path("search/", views.film_search, name="film-search"),
    path(
        "search/film/<int:tmdb_id>/",
        views.search_clickthrough,
        name="search-clickthrough",
    ),
    path(
        "search/watchlist/<int:tmdb_id>/",
        views.search_watchlist,
        name="search-watchlist",
    ),
    path("search/suggest/", views.search_suggest, name="search-suggest"),
    path("film/create/", views.film_create, name="film-create"),
    path("film/<int:film_id>/", views.film_detail, name="film"),
    path("film/<int:film_id>/edit/", views.film_edit, name="film-edit"),
    path("film/<int:film_id>/shelve/", views.shelve, name="film-shelve"),
    path("film/<int:film_id>/unshelve/", views.unshelve, name="film-unshelve"),
    path(
        "film/<int:film_id>/watched/",
        views.mark_watched_view,
        name="film-mark-watched",
    ),
    # File import/export (M3, D9/D10) — the §3.5 preferences routes.
    path("preferences/import/", views.import_films, name="import-films"),
    path("preferences/export/", views.export_films, name="export-films"),
]
