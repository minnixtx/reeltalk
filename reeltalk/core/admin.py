from django.contrib import admin

from .models import Film, MergedFilm


@admin.register(Film)
class FilmAdmin(admin.ModelAdmin):
    list_display = ["title", "year", "tmdb_id", "imdb_id", "created_date"]
    search_fields = ["title", "imdb_id"]
    list_filter = ["year"]
    date_hierarchy = "created_date"


@admin.register(MergedFilm)
class MergedFilmAdmin(admin.ModelAdmin):
    list_display = ["old_id", "new_id", "merged_date"]
