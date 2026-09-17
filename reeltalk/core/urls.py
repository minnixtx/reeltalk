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
    # Genre subfeed (M6 artwork C, R64): the "Popular Genres" pills land here.
    path("genre/<slug:slug>/", views.genre, name="genre"),
    path("film/create/", views.film_create, name="film-create"),
    path("film/<int:film_id>/", views.film_detail, name="film"),
    # A status's wire URL (M4 increment 6): the Note document for AP clients.
    path("status/<int:status_id>/", views.status_detail, name="status"),
    # Delete the user's own review (M5 increment 4): soft-delete + broadcast.
    path(
        "status/<int:status_id>/delete/",
        views.delete_review,
        name="status-delete",
    ),
    path("film/<int:film_id>/edit/", views.film_edit, name="film-edit"),
    path("film/<int:film_id>/shelve/", views.shelve, name="film-shelve"),
    path("film/<int:film_id>/unshelve/", views.unshelve, name="film-unshelve"),
    # Block / unblock a film (M5 increment 3, R55): local-only read-side state.
    path("film/<int:film_id>/block/", views.film_block, name="film-block"),
    path("film/<int:film_id>/unblock/", views.film_unblock, name="film-unblock"),
    path(
        "film/<int:film_id>/watched/",
        views.mark_watched_view,
        name="film-mark-watched",
    ),
    # File import/export (M3, D9/D10) — the §3.5 preferences routes.
    path("preferences/import/", views.import_films, name="import-films"),
    path("preferences/export/", views.export_films, name="export-films"),
]
