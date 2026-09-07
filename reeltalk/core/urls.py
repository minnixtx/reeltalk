"""URLs for the core (film) app."""

from django.urls import path

from . import views

urlpatterns = [
    path("film/<int:film_id>/", views.film_detail, name="film"),
]
