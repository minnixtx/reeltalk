from django.contrib import admin

from .models import Film, MergedFilm, Shelf, ShelfFilm


@admin.register(Film)
class FilmAdmin(admin.ModelAdmin):
    list_display = ["title", "year", "tmdb_id", "imdb_id", "created_date"]
    search_fields = ["title", "imdb_id"]
    list_filter = ["year"]
    date_hierarchy = "created_date"


@admin.register(MergedFilm)
class MergedFilmAdmin(admin.ModelAdmin):
    list_display = ["old_id", "new_id", "merged_date"]


@admin.register(Shelf)
class ShelfAdmin(admin.ModelAdmin):
    list_display = ["name", "identifier", "user"]
    search_fields = ["name", "user__localname"]


@admin.register(ShelfFilm)
class ShelfFilmAdmin(admin.ModelAdmin):
    list_display = ["film", "shelf", "user", "shelved_date"]
