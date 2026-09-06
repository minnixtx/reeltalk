"""Film domain models (PLAN.md §3.2).

A flat single ``Film`` row per title (decision D2): no Work/Edition split, no
Person/Author models — directors and cast are plain name-list fields. The
binary watch state (D1) lives on the Shelf/ShelfFilm side, not here.
"""

import re

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone

# Leading article stripped for sort_title and the title/year dedup fallback
# (D7). One leading article only; "The The X" keeps its second "the".
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def derive_sort_title(title: str) -> str:
    """Auto-derived sort key: leading article stripped, lowercased (§3.2)."""
    return _LEADING_ARTICLE.sub("", title.strip()).lower()


class Film(models.Model):
    title = models.CharField(max_length=512)
    # Auto-derived from title on save; used for ordering and title/year dedup.
    sort_title = models.CharField(max_length=512, db_index=True, editable=False)
    subtitle = models.CharField(max_length=512, blank=True, default="")
    # HTML rendered from markdown at write time (§3.2).
    description = models.TextField(blank=True, default="")
    year = models.PositiveIntegerField(null=True, blank=True)
    runtime = models.PositiveIntegerField(
        null=True, blank=True, help_text="Runtime in minutes."
    )
    # Plain name-lists per D2 — no Person/Author/Genre relation models.
    genres = ArrayField(models.CharField(max_length=128), blank=True, default=list)
    directors = ArrayField(models.CharField(max_length=256), blank=True, default=list)
    cast = ArrayField(models.CharField(max_length=256), blank=True, default=list)
    poster = models.ImageField(upload_to="posters/", null=True, blank=True)

    # Dedup identity (D7): exact tmdb_id, then imdb_id, then title/year.
    tmdb_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    imdb_id = models.CharField(max_length=32, null=True, blank=True, db_index=True)

    # ActivityPub origin identity (M4). Present from day one so local objects
    # can carry provenance; federation populates them when it lands.
    origin_id = models.PositiveBigIntegerField(null=True, blank=True)
    remote_id = models.PositiveBigIntegerField(null=True, blank=True)

    created_date = models.DateTimeField(default=timezone.now, db_index=True)
    updated_date = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_title", "year"]

    def __str__(self) -> str:
        if self.year is not None:
            return f"{self.title} ({self.year})"
        return self.title

    def save(self, *args, **kwargs):
        self.sort_title = derive_sort_title(self.title)
        super().save(*args, **kwargs)

    @classmethod
    def find_match(
        cls,
        *,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        title: str | None = None,
        year: int | None = None,
    ) -> "Film | None":
        """Dedup lookup per D7: exact tmdb_id, then imdb_id, then title+year.

        Returns the existing row to match (and backfill onto) or None when no
        local film corresponds — in which case the caller creates a new one.
        """
        if tmdb_id is not None:
            match = cls.objects.filter(tmdb_id=tmdb_id).first()
            if match is not None:
                return match
        if imdb_id:
            match = cls.objects.filter(imdb_id=imdb_id).first()
            if match is not None:
                return match
        if title and year is not None:
            match = (
                cls.objects.filter(sort_title=derive_sort_title(title), year=year)
                .order_by("id")
                .first()
            )
            if match is not None:
                return match
        return None


class MergedFilm(models.Model):
    """Old-id → new-id map so absorbed films' URLs keep resolving (§3.2).

    Plain integer references (not FKs): the absorbed row is deleted, so a
    constraint to it could never hold.
    """

    old_id = models.PositiveBigIntegerField(unique=True)
    new_id = models.PositiveBigIntegerField()
    merged_date = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name_plural = "merged films"

    def __str__(self) -> str:
        return f"{self.old_id} -> {self.new_id}"


def resolve_film_id(film_id: int) -> int:
    """Follow the MergedFilm chain to the current canonical film id.

    Bounded by a hard hop limit so a corrupt cycle (should be impossible via
    the merge path, which always points at a live row) cannot loop forever.
    """
    seen = 0
    while True:
        merged = MergedFilm.objects.filter(old_id=film_id).first()
        if merged is None or seen >= 100:
            return film_id
        film_id = merged.new_id
        seen += 1
