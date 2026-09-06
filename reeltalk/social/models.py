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
from django.utils import timezone


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

    @property
    def username(self) -> str:
        """Full identity, localname@domain (§3.2). M4 will refine this for
        remote users, whose domain is not the local one."""
        return f"{self.localname}@{settings.DOMAIN}"

    def get_full_name(self) -> str:
        return self.display_name or self.localname

    def __str__(self) -> str:
        return self.get_full_name()
