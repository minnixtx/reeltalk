"""Social identity models (PLAN.md §3.2).

``User`` is ReelTalk's custom auth user (decision R10): a
``localname@domain`` identity with display name, summary, avatar, a
local-vs-remote flag, and the follow/block relations that M4 federation
builds on. It is defined before any social migration exists so
``AUTH_USER_MODEL`` never has to move later.
"""

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from reeltalk.core.models import Film, Shelf, ShelfFilm, Status


class UserManager(BaseUserManager):
    def _create_user(self, localname, password, **extra_fields):
        if not localname:
            raise ValueError("The localname must be set")
        user = self.model(localname=localname, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, localname, password=None, email="", **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(localname, password, email=email or "", **extra_fields)

    def create_superuser(self, localname, password=None, email="", **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if (
            extra_fields.get("is_staff") is not True
            or extra_fields.get("is_superuser") is not True
        ):
            raise ValueError(
                "Superuser must have is_staff and is_superuser set to True."
            )
        return self._create_user(localname, password, email=email or "", **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    # The local part of the federated identity localname@domain (§3.2) and
    # the login name (USERNAME_FIELD). Uniqueness is case-sensitive at the
    # database level; signup additionally rejects case-insensitive duplicates
    # (social.forms), since "Alice" and "alice" are one identity to other
    # instances.
    localname = models.CharField(max_length=30, unique=True)
    display_name = models.CharField(max_length=255, blank=True, default="")
    # Not unique yet: password reset (later in M1) will need it; two users
    # with no email must be possible until then.
    email = models.EmailField(blank=True)
    # HTML rendered from markdown at write time (§3.2), like Film.description.
    summary = models.TextField(blank=True, default="")
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)
    # Local users are full accounts; remote users (M4) are lightweight mirrors
    # populated from federation.
    local = models.BooleanField(default=True)
    # PermissionsMixin provides is_superuser/groups/user_permissions but not
    # is_staff — custom users define it themselves.
    is_staff = models.BooleanField(
        default=False,
        help_text="Designates whether the user can log into this admin site.",
    )

    # Follow/block relations defined up front so M4 builds on them instead of
    # bolting on a profile model (R10). Server-level blocking is a federation
    # concept and lands with M4.
    follows = models.ManyToManyField(
        "self", symmetrical=False, related_name="followers", blank=True
    )
    blocks = models.ManyToManyField(
        "self", symmetrical=False, related_name="blocked_by", blank=True
    )
    blocked_films = models.ManyToManyField("core.Film", blank=True)

    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "localname"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)
        if creating and self.local:
            # Every local user starts with the two binary shelves (D1). Remote
            # mirrors (M4) receive their shelves from federation instead.
            Shelf.create_default_shelves(self)

    @property
    def username(self) -> str:
        """Full identity, localname@domain (§3.2). M4 will refine this for
        remote users, whose domain is not the local one."""
        return f"{self.localname}@{settings.DOMAIN}"

    def get_full_name(self) -> str:
        return self.display_name or self.localname

    def __str__(self) -> str:
        return self.get_full_name()

    # --- Films-page query API (PLAN.md §3.3 rule 1) -------------------------

    def films_on_shelf(self, identifier: str):
        """Films on one of this user's shelves — a tab on the films page."""
        return self._films_with_rating(
            Film.objects.filter(shelves__identifier=identifier, shelves__user=self)
        )

    def all_films(self):
        """Every film this user has a relationship with (the §3.5/D10 set).

        Films on any of the user's shelves plus films carrying one of their
        non-deleted statuses — the same set the export emits rows for.
        """
        ids = set(
            ShelfFilm.objects.filter(shelf__user=self).values_list("film_id", flat=True)
        )
        ids |= set(
            Status.objects.filter(user=self, deleted=False)
            .exclude(film=None)
            .values_list("film_id", flat=True)
        )
        return self._films_with_rating(Film.objects.filter(id__in=ids))

    def _films_with_rating(self, films):
        """Annotate films with this user's current star rating.

        D5 allows at most one live review per user per film, so the subquery
        is unambiguous; ``user_rating`` is None when the user hasn't reviewed.
        """
        rating = (
            Status.objects.filter(
                user=self,
                film=OuterRef("pk"),
                status_type__in=list(Status.REVIEW_TYPES),
                deleted=False,
            )
            .order_by("-id")
            .values("rating")[:1]
        )
        return films.annotate(user_rating=Subquery(rating))
