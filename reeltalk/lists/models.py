"""User-made lists of films (§2K, R137).

Three tables and no more. ``FilmList`` is the object; ``ListItem`` is one ranked
film inside it; ``ListSave`` is one member's live pointer at somebody else's
list (L2). The social half is not modelled here at all: every list owns one
``Status`` row -- its *post face* (L9) -- so applause, replies, notifications
and their federation are the existing code paths rather than a second copy of
them.
"""

from django.db import models
from django.utils import timezone


class FilmList(models.Model):
    """A named, ranked list of films made by one member (L1--L5).

    The list owns its own identity and its own post face. ``status`` is required
    rather than nullable on purpose: L9 makes the face part of what a list *is*,
    so a ``FilmList`` without one is not a state worth being able to express.
    ``services.create_list`` builds the face first for that reason.

    The face deliberately carries **no text of its own**. The title and the
    description live here and nowhere else, so an L1 edit (freely editable
    forever) has one copy to change. The federated ``Note`` body is composed at
    broadcast time from this row plus its ranked items (increment 6), and the
    feed row reads the title off this row (increment 4) -- a stored copy of the
    body would be a second source of truth that every edit has to keep in step.

    ``description`` holds HTML rendered from markdown at write time and
    ``raw_description`` the markdown source, the same pair ``Film`` keeps (R18)
    so an edit form can pre-fill what the member typed rather than the markup.
    """

    user = models.ForeignKey(
        "social.User", on_delete=models.PROTECT, related_name="lists"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    raw_description = models.TextField(blank=True, default="")
    status = models.OneToOneField(
        "core.Status", on_delete=models.PROTECT, related_name="film_list"
    )

    created_date = models.DateTimeField(default=timezone.now, db_index=True)
    updated_date = models.DateTimeField(auto_now=True)

    # Soft-delete, the same instance-level shape as Status (R17): the row is
    # kept with its identity intact and only the deleted flag marks it gone.
    # ``services.soft_delete_list`` is the production path -- see there for why
    # a soft delete *hides* a list rather than cascading its saves away.
    deleted = models.BooleanField(default=False)
    deleted_date = models.DateTimeField(null=True, blank=True)

    # ActivityPub identity, present from day one as with Film/Shelf/Status
    # (R41/R42). Increment 7 is what populates the remote half; a local list
    # leaves remote_url blank and mints origin_id = its own pk.
    local = models.BooleanField(default=True)
    origin_id = models.PositiveBigIntegerField(null=True, blank=True)
    remote_id = models.PositiveBigIntegerField(null=True, blank=True)
    remote_url = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_date"]
        constraints = [
            # One mirror row per home-instance object URL (mirrors only), the
            # same partial-unique discipline Film and Status use so inbound
            # processing is idempotent at the database rather than in Python.
            models.UniqueConstraint(
                fields=["remote_url"],
                condition=models.Q(remote_url__gt=""),
                name="unique_remote_url_for_list_mirrors",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user.localname}: {self.title}"

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)
        if creating and self.local and not self.origin_id:
            # Day-one origin identity (R41), written with a queryset update so
            # the auto_now updated_date is not disturbed by our own backfill.
            FilmList.objects.filter(pk=self.pk).update(origin_id=self.pk)
            self.origin_id = self.pk

    def delete(self, *args, **kwargs):
        """Soft-delete: keep the row and mark it, never wipe it.

        Mirrors ``Status.delete()``. Overriding it rather than leaving the
        service as the only soft path means a stray ``.delete()`` anywhere in
        the codebase cannot take a list and its members' saved pointers down for
        good. A *hard* delete (``queryset.delete()``, raw SQL) still cascades
        ``ListItem`` and ``ListSave`` away -- that is what L2's "cascades away
        with the creator's delete" means at the schema level; the soft path
        hides the list instead, and every reader filters on ``deleted``.
        """
        if self.deleted:
            return
        self.deleted = True
        self.deleted_date = timezone.now()
        self.save(update_fields=["deleted", "deleted_date", "updated_date"])


class ListItem(models.Model):
    """One film in one list, at one position (L3).

    ``rank`` is an **ordering key, not the displayed ordinal**. It is dense
    while a list is only ever appended to, and ``remove_film`` deliberately does
    not close the gap it leaves, so a list that has lost a film has ranks like
    1, 2, 4. Anything that renders a position number must number the rows in
    order rather than print ``rank`` -- otherwise a removal would make the
    visible numbering skip.
    """

    film_list = models.ForeignKey(
        FilmList, on_delete=models.CASCADE, related_name="items"
    )
    film = models.ForeignKey(
        "core.Film", on_delete=models.PROTECT, related_name="list_items"
    )
    rank = models.PositiveIntegerField()

    class Meta:
        ordering = ["rank", "id"]
        constraints = [
            # A film appears once per list. This is the constraint a film merge
            # has to respect -- see Film._repoint_list_items, which dedups
            # rather than blindly re-pointing.
            models.UniqueConstraint(
                fields=["film_list", "film"], name="one_row_per_film_per_list"
            )
        ]

    def __str__(self) -> str:
        return f"{self.film_list.title}: {self.film} ({self.rank})"


class ListSave(models.Model):
    """One member's save of somebody else's list (L2, L12).

    A live pointer, not a copy: the card and the page always read the list
    through this row, so the saver sees every edit the maker makes, and the
    pointer is CASCADE because a pointer at nothing is not worth keeping.
    Saving is silent (L6) -- there is no notification and no ledger row for
    this event anywhere, and the absence is a rule, not an omission.
    """

    user = models.ForeignKey(
        "social.User", on_delete=models.CASCADE, related_name="saved_lists"
    )
    film_list = models.ForeignKey(
        FilmList, on_delete=models.CASCADE, related_name="saves"
    )
    created = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "film_list"], name="one_save_per_user_per_list"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user.localname} saved {self.film_list.title}"
