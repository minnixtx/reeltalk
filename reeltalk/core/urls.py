"""URLs for the core (film) app."""

from django.urls import path

from . import views

urlpatterns = [
    path("film/create/", views.film_create, name="film-create"),
    path("film/<int:film_id>/", views.film_detail, name="film"),
    path("film/<int:film_id>/edit/", views.film_edit, name="film-edit"),
    path("film/<int:film_id>/shelve/", views.shelve, name="film-shelve"),
    path("film/<int:film_id>/unshelve/", views.unshelve, name="film-unshelve"),
]
