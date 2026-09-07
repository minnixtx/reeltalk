"""Film domain models (PLAN.md §3.2).

A flat single ``Film`` row per title (decision D2): no Work/Edition split, no
Person/Author models — directors and cast are plain name-list fields. The
binary watch state (D1) lives on the Shelf/ShelfFilm side, not here.
"""

import re
from decimal import Decimal

from django.contrib.postgres.fields import ArrayField
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q
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

    # --- merge/absorb (§3.2) ------------------------------------------------

    # Fields ``absorb_data_from`` may fill on the canonical row: metadata plus
    # the dedup IDs — D7 backfills TMDB/IMDb IDs onto manual films. Never
    # touched: title/sort_title (identity), origin_id/remote_id (the canonical
    # keeps its own provenance), created_date/updated_date.
    _ABSORB_SCALAR_FIELDS = (
        "subtitle",
        "description",
        "year",
        "runtime",
        "poster",
        "tmdb_id",
        "imdb_id",
    )
    _ABSORB_ARRAY_FIELDS = ("genres", "directors", "cast")

    def absorb_data_from(self, other: "Film") -> dict:
        """Fill this row's empty fields from an absorbed film.

        Scalar fields take the other value only when this one is empty; array
        fields are unioned (this row's order kept, new values appended in the
        other row's order). Returns ``{field: value}`` for what changed.
        """
        absorbed = {}
        for name in self._ABSORB_SCALAR_FIELDS:
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
                absorbed[name] = getattr(other, name)
        for name in self._ABSORB_ARRAY_FIELDS:
            current = list(getattr(self, name) or [])
            other_values = getattr(other, name) or []
            additions = [v for v in other_values if v not in set(current)]
            if additions:
                setattr(self, name, current + additions)
                absorbed[name] = additions
        return absorbed

    @transaction.atomic
    def merge_into(self, canonical: "Film") -> dict:
        """Merge this film into ``canonical`` and delete the absorbed row.

        Empty metadata on the canonical row is backfilled from this one (D7),
        every related row is re-pointed at the canonical, a MergedFilm row
        keeps old-id → new-id so old URLs keep resolving, and this row is
        deleted. All-or-nothing: any failure rolls the whole merge back.
        """
        if self.id == canonical.id:
            raise ValueError("Cannot merge a film into itself")
        absorbed = canonical.absorb_data_from(self)
        self._repoint_related(canonical)
        MergedFilm.objects.create(old_id=self.id, new_id=canonical.id)
        # The absorbed row is deleted before the canonical is saved: a
        # backfilled unique field (tmdb_id) would otherwise collide with the
        # value this row still holds. Everything rolls back together on error.
        self.delete()
        canonical.save()
        return absorbed

    def _repoint_related(self, canonical: "Film") -> None:
        """Re-point every row that references this film at the canonical one.

        One block per related model (R16).
        """
        self._repoint_shelf_films(canonical)
        self._repoint_statuses(canonical)
        self._repoint_blocked_films(canonical)

    def _repoint_shelf_films(self, canonical: "Film") -> None:
        """Move shelf rows onto the canonical film.

        A film already on a shelf absorbs its duplicate row instead of
        violating the one-row-per-(film, shelf) constraint; shelved_date and
        the acting user are preserved.
        """
        already_shelved = set(
            ShelfFilm.objects.filter(film=canonical).values_list("shelf_id", flat=True)
        )
        for row in ShelfFilm.objects.filter(film=self):
            if row.shelf_id in already_shelved:
                row.delete()
            else:
                row.film = canonical
                row.save(update_fields=["film"])

    def _repoint_blocked_films(self, canonical: "Film") -> None:
        """Carry film blocks over to the canonical film.

        A block on an absorbed film keeps applying after the merge; if a user
        had blocked both films, the duplicate falls out of the M2M.
        """
        # Local import: reeltalk.social imports this module (default shelves),
        # so a module-level import would cycle.
        from reeltalk.social.models import User

        for user in User.objects.filter(blocked_films=self):
            user.blocked_films.remove(self)
            user.blocked_films.add(canonical)

    def _repoint_statuses(self, canonical: "Film") -> None:
        """Carry comments/reviews/ratings over to the canonical film."""
        Status.objects.filter(film=self).update(film=canonical)


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


class Shelf(models.Model):
    """A named collection of films owned by a user (§3.2).

    D1's binary model keeps exactly two default shelves per local user —
    Watchlist (``to-read``) and Watched (``read``); the identifiers are fixed
    by the decision, not derived from the name. Shelves federate as
    ActivityPub OrderedCollections in M4.
    """

    TO_READ = "to-read"
    READ = "read"
    # (identifier, name) pairs created for every new local user (D1).
    DEFAULT_SHELVES = ((TO_READ, "Watchlist"), (READ, "Watched"))

    name = models.CharField(max_length=100)
    identifier = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    user = models.ForeignKey(
        "social.User", on_delete=models.CASCADE, related_name="shelves"
    )
    films = models.ManyToManyField(
        "Film",
        through="ShelfFilm",
        through_fields=("shelf", "film"),
        related_name="shelves",
    )

    # ActivityPub origin identity (M4) — same day-one pattern as Film.
    origin_id = models.PositiveBigIntegerField(null=True, blank=True)
    remote_id = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "identifier"], name="unique_shelf_identifier_per_user"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user.localname}: {self.name}"

    @classmethod
    def create_default_shelves(cls, user) -> None:
        """Create the two D1 default shelves for a new local user."""
        for identifier, name in cls.DEFAULT_SHELVES:
            cls.objects.create(name=name, identifier=identifier, user=user)


class ShelfFilm(models.Model):
    """Through row joining a film to a shelf (§3.2).

    ``user`` is the actor who shelved — always the shelf's owner until M4,
    carried separately because the ActivityPub wire type needs an actor. It
    defaults to the shelf's owner on save; note the M2M manager's ``.add()``
    bypasses this save(), so shelving code creates rows explicitly (or passes
    ``through_defaults``).
    """

    shelf = models.ForeignKey(Shelf, on_delete=models.CASCADE)
    # PROTECT: a film cannot be deleted while it sits on a shelf. Merge/absorb
    # re-points these rows before deleting the absorbed film, so merges are
    # unaffected.
    film = models.ForeignKey(Film, on_delete=models.PROTECT)
    user = models.ForeignKey("social.User", on_delete=models.CASCADE)
    shelved_date = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name_plural = "shelf films"
        constraints = [
            models.UniqueConstraint(
                fields=["film", "shelf"], name="unique_film_per_shelf"
            )
        ]

    def __str__(self) -> str:
        return f"{self.film} on {self.shelf}"

    def save(self, *args, **kwargs):
        if self.user_id is None and self.shelf_id is not None:
            self.user = Shelf.objects.get(pk=self.shelf_id).user
        super().save(*args, **kwargs)


class Status(models.Model):
    """A post on a film — or standalone (§3.2).

    The spec's Comment/Review/ReviewRating subtypes are one table with a
    ``status_type`` discriminator rather than per-subtype tables (R17): D5's
    one-review-per-user-per-film constraint must coexist with soft-delete,
    and the partial unique index enforcing it needs both columns in the same
    table. A ``review_rating`` row is a review without content — it always
    carries its rating, is editable in place, and can later gain text while
    keeping its type.

    ``content`` holds HTML rendered from markdown at write time (like
    Film.description); ``raw_content`` keeps the markdown source so an edit
    can pre-fill a form. Standalone notes (no film) have no v0.1 composer;
    the fields stay open for them per §3.2.
    """

    class Type(models.TextChoices):
        COMMENT = "comment", "Comment"
        REVIEW = "review", "Review"
        REVIEW_RATING = "review_rating", "Rating-only review"

    # The types that count as the user's review of a film for D5.
    REVIEW_TYPES = (Type.REVIEW, Type.REVIEW_RATING)

    user = models.ForeignKey(
        "social.User", on_delete=models.PROTECT, related_name="statuses"
    )
    film = models.ForeignKey(
        Film,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="statuses",
    )
    status_type = models.CharField(
        max_length=20, choices=Type.choices, null=True, blank=True
    )
    content = models.TextField(blank=True, default="")
    raw_content = models.TextField(blank=True, default="")
    rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal("0.5")),
            MaxValueValidator(Decimal("5")),
        ],
    )
    published_date = models.DateTimeField(default=timezone.now, db_index=True)
    edited_date = models.DateTimeField(null=True, blank=True)
    # Soft-delete (§3.2): the row is kept as a tombstone with its identity
    # intact (federation consistency in M4) and its content cleared.
    deleted = models.BooleanField(default=False)
    deleted_date = models.DateTimeField(null=True, blank=True)
    reply_parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="replies"
    )
    # Remote statuses (M4) are mirrors; local ones are full rows.
    local = models.BooleanField(default=True)

    # ActivityPub origin identity (M4) — same day-one pattern as Film/Shelf.
    origin_id = models.PositiveBigIntegerField(null=True, blank=True)
    remote_id = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-published_date"]
        constraints = [
            # D5: one review per user per film — written reviews and
            # rating-only entries alike. Soft-deleted reviews don't count, so
            # a deleted review can be replaced (partial index; Postgres).
            models.UniqueConstraint(
                fields=["user", "film"],
                # Literals: Meta has its own namespace, so Type is not visible here.
                condition=Q(
                    status_type__in=["review", "review_rating"],
                    deleted=False,
                ),
                name="unique_review_per_user_per_film",
            )
        ]

    def __str__(self) -> str:
        if self.film:
            kind = self.get_status_type_display()
            return f"{self.user.localname}: {kind} on {self.film}"
        return f"{self.user.localname}: status"

    def save(self, *args, **kwargs):
        if self.status_type == Status.Type.REVIEW_RATING and not self.rating:
            raise ValueError("A rating-only entry must carry a star rating")
        if self.status_type and not self.film_id:
            kind = self.get_status_type_display().lower()
            raise ValueError(f"A {kind} status must be anchored to a film")
        super().save(*args, **kwargs)

    @property
    def is_review(self) -> bool:
        """True for reviews and rating-only entries — the D5 review types."""
        return self.status_type in self.REVIEW_TYPES

    def delete(self, *args, **kwargs):
        """Soft-delete (§3.2): keep the row as a tombstone with its identity
        intact (M4 federation consistency) and clear the user content."""
        if self.deleted:
            return
        self.deleted = True
        self.content = ""
        self.raw_content = ""
        self.deleted_date = timezone.now()
        self.save(update_fields=["deleted", "content", "raw_content", "deleted_date"])


def validate_star_rating(rating) -> Decimal:
    """D3/§3.3: a film cannot be marked watched without a star rating.

    Accepts the int/float/str/Decimal forms a form can submit; the value must
    be 0.5–5 in half-star steps (half steps keep D10's export — rating ×2 —
    an integer). Raises ValueError; callers run it before any database write.
    """
    if rating is None or rating == "":
        raise ValueError("A star rating is required to mark a film as watched")
    value = Decimal(str(rating))
    if not (Decimal("0.5") <= value <= Decimal("5")):
        raise ValueError("Rating must be between 0.5 and 5 stars")
    if (value * 2) % 1 != 0:
        raise ValueError("Rating must use half-star steps")
    return value


@transaction.atomic
def mark_watched(
    user, film: Film, *, rating, content: str = "", raw_content: str = ""
) -> Status:
    """Mark ``film`` as watched for ``user`` (§3.3 rules 3–4).

    The star rating is validated before any database write (D3). Shelves the
    film onto the user's Watched shelf and off their Watchlist — D1's binary
    model: a film is either watched or not — then creates or updates the
    user's review entry: a written review when text is given, a rating-only
    entry otherwise. Re-finishing a film that already has a review updates it
    in place (D5); empty text keeps the existing content. Posts no automatic
    feed note (D3) — only the review itself exists.
    """
    star_rating = validate_star_rating(rating)
    watched_shelf = Shelf.objects.get(user=user, identifier=Shelf.READ)
    watchlist_shelf = Shelf.objects.get(user=user, identifier=Shelf.TO_READ)

    existing = (
        Status.objects.filter(
            user=user,
            film=film,
            status_type__in=list(Status.REVIEW_TYPES),
            deleted=False,
        )
        .order_by("id")
        .first()
    )

    ShelfFilm.objects.get_or_create(
        shelf=watched_shelf, film=film, defaults={"user": user}
    )
    # D1: Watchlist and Watched are mutually exclusive.
    ShelfFilm.objects.filter(shelf=watchlist_shelf, film=film).delete()

    if existing is not None:
        update_fields = []
        if existing.rating != star_rating:
            existing.rating = star_rating
            update_fields.append("rating")
        if content and (
            existing.content != content or existing.raw_content != raw_content
        ):
            existing.content = content
            existing.raw_content = raw_content
            existing.edited_date = timezone.now()
            update_fields += ["content", "raw_content", "edited_date"]
        if update_fields:
            existing.save(update_fields=update_fields)
        return existing

    status_type = Status.Type.REVIEW if content else Status.Type.REVIEW_RATING
    return Status.objects.create(
        user=user,
        film=film,
        status_type=status_type,
        rating=star_rating,
        content=content,
        raw_content=raw_content,
    )
