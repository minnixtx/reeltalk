"""Social identity models (PLAN.md §3.2).

``User`` is ReelTalk's custom auth user (decision R10): a
``localname@domain`` identity with display name, summary, avatar, a
local-vs-remote flag, and the follow/block relations that M4 federation
builds on. It is defined before any social migration exists so
``AUTH_USER_MODEL`` never has to move later.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models, transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from django.utils.dateformat import format as date_format

from reeltalk.activitypub.crypto import generate_keypair
from reeltalk.core.models import Film, Shelf, ShelfFilm, Status

# A minted invite stays open this long before it lapses unused (R82). Long
# enough that a link pasted into a chat app gets opened from it days later;
# short enough that a link nobody claims stops being a live door by itself
# rather than staying one forever.
INVITE_TTL_DAYS = 7

# A verification link stays open this long before it lapses (R120). The window
# is a compromise between two failures that pull opposite ways: too long and a
# credential sitting in a mailbox — a transport that may be plaintext-capable —
# stays live for a week; too short and a member who checks mail once a day, or
# whose link lands on a Friday, reads it as expired and gives up. 72h is the
# second one priced down, and it is affordable only because R122's resend makes
# a fresh token cheap. **Longer than the Mastodon peer deliberately** — its
# ``confirm_within`` is 2 days; the divergence is recorded in PROGRESS §2F.
EMAIL_VERIFICATION_TTL_HOURS = 72


class AdminImmunityError(ValueError):
    """Raised when a ban or suspension is aimed at the site admin (R114).

    The owner's rule is absolute: **the admin can never be banned or
    suspended.** It lives here, in the two writers of those states, rather
    than only in the view guard, because "never" has to hold for callers
    that do not exist yet — a management command, an import hook, a bulk
    tool. Increment 5 learned that the hard way from the opposite
    direction: a step left outside the single writer produced a half-ban
    that no caller knew about. An invariant left outside the single writer
    produces a locked-out instance the same way.

    Raising rather than returning ``False`` is deliberate. ``suspend()``
    and ``ban()`` already use a ``False`` return to mean "already in that
    state", and overloading it with "refused" would make a refused action
    indistinguishable from a replay — the exact quiet failure this rule
    exists to prevent. A caller that means to allow this has to catch it
    by name and say so.
    """


class AddressMismatchError(ValueError):
    """Raised when a verification token is spent on a different address (R120).

    A verification token is not a generic "prove you are this account"
    credential — it is proof of control over **one specific mailbox**, and
    that address is recorded on the token when it is minted. The address on
    the account can move afterwards: the site admin corrects a typo at the
    member's request (R124), and R123 explicitly permits that, because
    correcting an address is a different act from attesting it.

    Without this guard the sequence *mint → admin corrects the address →
    somebody clicks the old link* would mark the **new** address verified on
    the strength of a link that was only ever sent to the old one. That is
    the exact state R120's binding clause exists to make unreachable, and
    the thing password reset — the next thing built on this — must not
    inherit: a "verified" flag that attests an address nobody proved.

    Raised rather than returned for the same reason as
    :class:`AdminImmunityError`: the caller needs to distinguish "refused"
    from "nothing to do", and a silent no-op here reads as success.
    """


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


class SuspensionOrigin(models.TextChoices):
    """Who decided that an account is suspended.

    Two values, each with a live producer, per the rule increment 3 set —
    a value arrives with the code that writes it and never ahead of it.

    ``LOCAL`` is the ordinary case: this instance looked at an account and
    suspended it. ``DOMAIN_BLOCK`` is the same decision taken at a
    different scope — not a suspension *learned from* a peer, which is a
    thing that does not exist on this wire (increment 4b established that
    no inbound "a peer suspended your user" activity arrives), but a
    suspension this instance imposed on accounts belonging to a server it
    has blocked.

    The distinction is load-bearing rather than decorative: it is what lets
    :func:`~reeltalk.moderation.models.unblock_domain` lift exactly the
    suspensions the block created and leave every individually-suspended
    account alone. Without a second value there would be no way to tell
    "this moderator suspended this person" from "this person's whole
    server got blocked", and unblocking a domain would either restore
    accounts somebody individually suspended or fail to restore the ones
    the block was responsible for.
    """

    LOCAL = ("local", "Suspended by this instance")
    DOMAIN_BLOCK = ("domain_block", "Suspended by a domain block")


class User(AbstractBaseUser, PermissionsMixin):
    # The local part of the federated identity localname@domain (§3.2) and
    # the login name (USERNAME_FIELD). Uniqueness is case-sensitive at the
    # database level; signup additionally rejects case-insensitive duplicates
    # (social.forms), since "Alice" and "alice" are one identity to other
    # instances. Local accounts keep R12's 1-30 char rule (enforced in
    # social.forms); the field itself is wider because remote mirrors (M4)
    # are stored as <preferredUsername>@<netloc>, which can exceed 30 chars.
    localname = models.CharField(max_length=255, unique=True)
    display_name = models.CharField(max_length=255, blank=True, default="")
    # The member's contact address. **Required at signup and unique when set**
    # since R118, which discharges the deferral this field carried from R12
    # ("email optional and non-unique until password reset needs it") — the
    # need arrived, and the owner took the strong option.
    #
    # The two halves live at different layers on purpose. *Required* is a
    # form-boundary rule (``SignupForm``), because that is where a human can
    # be told what is missing. *Unique* is a database rule, and it has to be
    # a **partial** index — see ``unique_email_when_set`` in ``Meta`` below,
    # which explains why a plain ``unique=True`` cannot be built over the
    # empty-address rows a live instance already holds. Collapsing the two
    # layers into one column definition is what breaks the migration.
    #
    # Normalised in ``save()`` rather than only in the form, because the
    # uniqueness is worthless without case-folding and everything below the
    # form bypasses it.
    email = models.EmailField(blank=True)
    # When this account proved it can receive mail, and **which address** the
    # proof was for (R118/R120). Two columns, not a boolean, because
    # verification is always verification *of an address*, and the address
    # can move afterwards.
    #
    # NULL means "never verified" — the same one-fact economy as
    # ``suspended_at`` and R93's unread timestamp.
    email_verified_at = models.DateTimeField(null=True, blank=True, default=None)
    # The address that was actually clicked. Read together with
    # ``email_verified_at`` by the ``email_verified`` property below; never
    # consulted on its own, and never meaningful without the timestamp.
    verified_email = models.EmailField(blank=True, default="")
    # HTML rendered from markdown at write time (§3.2), like Film.description.
    summary = models.TextField(blank=True, default="")
    # The markdown source of the summary (R18's raw-source pattern): the edit
    # form pre-fills this, not the stored HTML markup. Remote mirrors leave it
    # empty — their summary is sanitized HTML fetched from the home document.
    raw_summary = models.TextField(blank=True, default="")
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)
    # Local users are full accounts; remote users (M4) are lightweight mirrors
    # populated from federation.
    local = models.BooleanField(default=True)
    # The mirror's home-instance actor URL — the id of its Person document on
    # its own instance (M4 increment 4). It is what deliveries' keyids point
    # at and the wire id served back to other instances. Empty for local
    # users, whose actor URL is derived from the localname (R40).
    actor_url = models.TextField(blank=True, default="")
    # The mirror's home-instance inbox — where outbound activities (a follow
    # we initiate, M4 increment 5) are delivered. Populated from the Person
    # document at mirror creation (R42: create-only, no refresh). Empty for
    # local users and for mirrors whose document advertised no inbox.
    inbox_url = models.TextField(blank=True, default="")
    # ActivityPub key pair (M4, R7): Ed25519 keys in PEM form. Local users get
    # a pair generated at creation (save below); remote mirrors carry only the
    # public key, fetched from their Person document — private_key stays empty.
    private_key = models.TextField(blank=True, default="")
    public_key = models.TextField(blank=True, default="")
    # PermissionsMixin provides is_superuser/groups/user_permissions but not
    # is_staff — custom users define it themselves.
    is_staff = models.BooleanField(
        default=False,
        help_text="Designates whether the user can log into this admin site.",
    )
    # Deliberately a separate field rather than a reuse of is_staff (R100).
    # is_staff is what Django's admin site checks, so "promote to moderator"
    # through that field would silently mean "hand over the whole admin" —
    # site settings, user records, the merge tool. A moderator's is_staff
    # stays False, so /admin/ still refuses them, and the grant happens only
    # in UserAdmin's Roles fieldset, which itself requires is_staff to reach.
    # The escalation path is therefore absent rather than merely checked.
    is_moderator = models.BooleanField(
        default=False,
        help_text=(
            "Designates a site moderator. Independent of staff status: a "
            "moderator can moderate /moderate/ and cannot open the admin."
        ),
    )
    # Whether this account gets the staff email about newly filed reports
    # (2E, R115). **Default ON**, and the default lives *here* rather than
    # in the send path so that every creation route — signup, the setup
    # wizard, the admin's create-user form, a fixture, a management
    # command — lands on the same value. A default set in the sender would
    # only cover the routes that happen to go through it.
    #
    # The rationale for ON is that the admin already opted this person in
    # by granting the moderator role; that grant *is* the consent. A
    # default-off flag makes a freshly deployed instance look like the
    # feature is missing, which is the same silent-non-delivery failure
    # this increment exists to close.
    #
    # Meaningless without a non-empty ``email``, which is a separate filter
    # at the recipient query (2E trap 2) and is deliberately not folded in
    # here: this field says "this person wants the mail", the address says
    # whether we can actually send it, and conflating them would hide which
    # of the two is missing.
    #
    # Set and cleared by the site admin in ``UserAdmin``'s Roles fieldset,
    # beside the ``is_moderator`` grant (R116) — not self-service.
    report_email = models.BooleanField(
        default=True,
        help_text=(
            "Email this account when a new report is filed. Set by the site "
            "admin beside the moderator grant; has no effect unless an "
            "email address is on file."
        ),
    )
    # Suspension is a *state of the account*, not a Moderation row (R102's
    # shape): the code that has to answer "may this user act" runs on every
    # request and must not join across apps to do it. The audit trail of who
    # suspended whom lives on the Report that prompted it (R106); these three
    # columns answer the runtime question only.
    #
    # NULL means "not suspended" rather than a boolean plus a timestamp, so
    # there is one fact here and not two that can disagree — the same economy
    # R93 used for unread state and R102b used to justify deriving is_active.
    suspended_at = models.DateTimeField(null=True, blank=True, default=None)
    # Records who decided. Only LOCAL has a producer in this increment: a
    # remote-sourced suspension is increment 6's domain-block work, and the
    # value arrives with the code that writes it rather than ahead of it.
    suspension_origin = models.CharField(
        max_length=16,
        choices=SuspensionOrigin.choices,
        blank=True,
        default="",
    )
    # The moderator's note at the moment of suspension. Kept separate from
    # the Report's note because the Report pile can be dismissed, resolved a
    # different way, or never exist at all (a direct suspend from the member
    # page), while this account has to be able to explain itself on its own.
    suspension_reason = models.TextField(blank=True, default="")
    # Ban is a SEPARATE state from suspension, not a stronger value of the
    # same column (R102). The two differ in what they do to content —
    # suspend hides rows intact, ban removes them — and the code that
    # enforces each must stay visibly distinct. Folding ban into
    # ``suspended_at`` would make the suspension read-filters do ban's
    # work, which is the failure mode §2D names: an action that promised to
    # remove content and quietly only hid it. So ban gets its own column,
    # and nothing in ``ban()`` writes to a suspension field.
    #
    # NULL means "not banned", the same one-fact economy as ``suspended_at``.
    banned_at = models.DateTimeField(null=True, blank=True, default=None)
    # Same reasoning as ``suspension_reason``: the account has to be able to
    # explain itself independently of whichever report (if any) caused it.
    ban_reason = models.TextField(blank=True, default="")

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
    # Unread state is one timestamp on the user rather than a boolean on every
    # notification row (R93): "mark all read" is a single UPDATE here instead
    # of an UPDATE whose cost and row locks grow with the unread count, and the
    # unread set is ``created > notifications_last_read`` against the
    # notification's composite index. A new account is marked read at signup,
    # which is the same set as "never read" — nothing predates the account —
    # and keeps the badge's range query free of a NULL case.
    notifications_last_read = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "localname"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"
        constraints = [
            # One mirror per home actor URL (remote users only — local users
            # leave actor_url empty, so the condition keeps their blanks out
            # of the index).
            models.UniqueConstraint(
                fields=["actor_url"],
                condition=models.Q(local=False),
                name="unique_actor_url_for_remote_mirrors",
            ),
            # One account per address, over the accounts that have one (R118).
            #
            # The condition is not cosmetic and cannot be dropped: a plain
            # ``unique=True`` **fails to build on a live instance today**,
            # because several rows carry ``email=''`` and a unique index
            # cannot hold more than one empty string. Same device as
            # ``unique_actor_url_for_remote_mirrors`` directly above, which
            # is conditioned for exactly this reason.
            #
            # It also keeps ``_instance`` (R111) legal. That row is the
            # signing identity for every outbound ``Flag``, not a member, and
            # infrastructure must never be forced to hold an address it will
            # never read.
            models.UniqueConstraint(
                fields=["email"],
                condition=~models.Q(email=""),
                name="unique_email_when_set",
            ),
        ]

    def save(self, *args, **kwargs):
        # Normalise the address before anything else (R118), the ``LinkDomain``
        # way. Doing this here rather than only in the form is the whole point:
        # a management command, a fixture, a data migration or a direct
        # ``.create()`` bypasses every form, and without case-folding
        # ``Foo@x.com`` and ``foo@x.com`` both land — one mailbox, two
        # accounts, and a unique index that never fires.
        #
        # ``verified_email`` gets the same treatment because the verified
        # state is a *string comparison* between the two. If only one side
        # were folded, a legitimately verified address could read as
        # unverified purely on casing.
        if self.email:
            self.email = self.email.strip().lower()
        if self.verified_email:
            self.verified_email = self.verified_email.strip().lower()
        creating = self.pk is None
        if creating and self.local and not self.private_key:
            # Every local user signs federation requests with its own key
            # (M4, R7); remote mirrors are signed by their home instance.
            self.private_key, self.public_key = generate_keypair()
        super().save(*args, **kwargs)
        if creating and self.local:
            # Every local user starts with the two binary shelves (D1). Remote
            # mirrors (M4) receive their shelves from federation instead.
            Shelf.create_default_shelves(self)

    def ensure_keypair(self) -> bool:
        """Generate this user's federation key pair if it has none yet.

        Local users created before the key fields landed (M4 increment 1)
        have no keys — a data migration runs this over the table, and it is
        the fallback for any other pre-existing account. Idempotent: only
        the empty case fills in, and remote mirrors are never touched (their
        home instance signs for them). Returns True when a pair was made.
        """
        if self.pk is None or not self.local or self.private_key:
            return False
        self.private_key, self.public_key = generate_keypair()
        self.save(update_fields=["private_key", "public_key"])
        return True

    @property
    def is_active(self) -> bool:
        """Derived from both severities — there is no ``is_active`` column.

        ``AbstractBaseUser`` supplies ``is_active = True`` as a plain class
        attribute and ``ModelBackend.user_can_authenticate`` is literally
        ``getattr(user, "is_active", True)``, so before R102b there was no way
        to stop an account logging in at all: ``user.is_active = False`` wrote
        an unsaved instance attribute that vanished on the next request.

        Deriving rather than storing keeps one source of truth, and because
        ``get_user()`` runs ``user_can_authenticate`` on **every** request, an
        already-banned account's existing sessions are cut on its next click
        with no session-store work.

        **Both severities are read here, and neither stands in for the other.**
        A ban is not "suspension plus". An account can be banned with
        ``suspended_at`` NULL and must still be refused, so the ban half of
        this test is independently necessary — a mutation that drops it lets a
        banned member sign right back in. Listing both also keeps the two
        states honest in the other direction: if a later change ever made
        ``ban()`` set ``suspended_at`` as a shortcut, this property would be
        redundantly correct rather than quietly load-bearing on the wrong
        field.

        **The cost, and it is not hypothetical:** this is a Python property,
        not a field, so ``User.objects.filter(is_active=True)`` raises
        ``FieldError``. Anything that wants active users queries
        ``suspended_at__isnull=True, banned_at__isnull=True``. A test pins the
        raise so nobody rediscovers it in production.
        """
        return self.suspended_at is None and self.banned_at is None

    @property
    def email_verified(self) -> bool:
        """Whether **the address on this account right now** has been proven.

        Derived rather than stored, and the derivation is the entire point
        (R118/R120). A stored boolean would answer "was this account ever
        verified?"; what every consumer actually needs is "can we trust the
        address we are looking at?", and those come apart the moment the site
        admin edits ``email`` — which R123 explicitly permits and R124 makes
        a routine support action.

        Comparing ``verified_email`` to ``email`` makes that case impossible
        rather than handled: the address moves, the equality fails, the
        account reads as unverified, with **no save hook, no change detection
        and nothing for a future writer to forget.** A hook that misfires is
        exactly the silent staleness this avoids — and trap 4 in PROGRESS §2F
        spells out what staleness costs here: password reset, the next thing
        built on this, would mail a stranger's reset link.

        Both halves are independently necessary. ``email_verified_at`` alone
        is a proof of some address, possibly not this one; ``verified_email``
        alone is an address nobody clicked.

        **Not to be confused with** :attr:`is_active`, which R102b pins to
        suspension and ban. Verification is a third, unrelated state, and
        R119 puts its enforcement in a custom auth backend rather than in
        that property — folding the three together would make three different
        refusals indistinguishable.
        """
        return bool(self.email_verified_at) and self.verified_email == self.email

    def suspend(
        self, *, reason: str = "", origin: str = SuspensionOrigin.LOCAL
    ) -> bool:
        """Suspend this account: login cut, content hidden, **rows intact** (R102).

        The only writer of the suspension state. Suspend is the reversible
        half of the pair — Ban (increment 5) is the one that removes content.
        Nothing here touches a single ``Status`` row, and that is deliberate:
        Mastodon's suspend does not delete either, because a suspension that
        gets appealed should not have destroyed the content a hide preserved.
        The hiding comes from read-side filters, all keyed on
        ``suspended_at__isnull=True``.

        Local accounts only. A remote mirror cannot be suspended at its home
        instance from here — the effects available against a remote target are
        "remove their content here" and "refuse them here", which is
        increment 6's domain block, not this.

        Returns ``True`` when this call suspended the account and ``False``
        when it was already suspended, so a caller can tell a decision from a
        replay rather than assuming.
        """
        if self.is_superuser:
            raise AdminImmunityError(
                f"@{self.localname} is the site admin and cannot be suspended (R114)."
            )
        if self.suspended_at is not None:
            return False
        self.suspended_at = timezone.now()
        self.suspension_origin = origin
        self.suspension_reason = reason
        self.save(
            update_fields=["suspended_at", "suspension_origin", "suspension_reason"]
        )
        return True

    def unsuspend(self) -> bool:
        """Lift a suspension, restoring the account exactly as it was (R102).

        Because suspend deleted nothing, unsuspend has to restore nothing
        either — clearing the state brings the content back at every read site
        at once. That symmetry is the point: a reversible action is one whose
        inverse is cheap and total. Clears the origin and reason with it, so a
        lifted suspension does not keep a stale explanation of a decision
        that is no longer in force.
        """
        if self.suspended_at is None:
            return False
        self.suspended_at = None
        self.suspension_origin = ""
        self.suspension_reason = ""
        self.save(
            update_fields=["suspended_at", "suspension_origin", "suspension_reason"]
        )
        return True

    def remove_all_content(self) -> list:
        """Soft-delete every live status this account owns; return the tombstones.

        Split out from :meth:`ban` rather than inlined because it is the
        part worth naming on its own, and because a caller that wants the
        list for a report message must capture it *before* the removal
        rather than reconstruct it after.

        One status at a time rather than a bulk ``.update()`` because
        ``Status.delete()`` is instance-level by design (R17): it keeps the
        identity fields that hold the row's wire id stable and clears only
        the user content. A queryset update would either skip the
        content-clearing — leaving rows that read as deleted but still
        serve text, the worst of both — or re-implement that logic in SQL
        where it can drift from the model.
        """
        removed = list(Status.objects.filter(user=self, deleted=False))
        for status in removed:
            status.delete()
        return removed

    def ban(self, *, reason: str = "") -> bool:
        """Ban this account: **content removed**, profile gone, name reserved (R102).

        The only writer of the ban state. Deliberately writes nothing to a
        suspension column — see the field comment on ``banned_at``. Ban and
        suspend are different promises, and the way to keep them different
        is to keep their writers different rather than to describe the
        difference carefully.

        **The content removal happens here, not in the caller.** It is part
        of what a ban *is* rather than a step one particular entry point
        remembers. The first cut of this increment left the soft-delete in
        ``ban_reported_member``, and a test that called ``user.ban()``
        directly found the review still sitting on the film page: login cut,
        profile gone, and the content untouched — a half-ban with nothing
        announcing it. Any future caller (a management command, an import
        hook, a bulk tool) would inherit that. Putting the removal inside
        the one writer means there is no way to ban without it.

        **What a lift cannot undo, and why the UI says so.** ``Status.delete()``
        is R17 soft-delete, which *clears* ``content`` and ``raw_content`` —
        a ban therefore destroys the review text, and :meth:`unban` restores
        the account, the localname and the actor document but **not the
        writing**. That asymmetry is inherent to the delete semantics the
        whole instance uses, not something this increment could avoid without
        giving the ban its own private content backup. It is stated on the
        confirmation the moderator reads before clicking.

        Local accounts only, for the same reason suspend is: we cannot ban an
        account at its home instance, and we hold no private key to sign one
        away with.

        Returns ``True`` when this call banned the account and ``False`` when
        it already was, so a caller can tell a decision from a replay. A
        replay removes nothing — the first ban already did.
        """
        if self.is_superuser:
            raise AdminImmunityError(
                f"@{self.localname} is the site admin and cannot be banned (R114)."
            )
        if self.banned_at is not None:
            return False
        # One transaction: an account that is banned but whose content is
        # still live is exactly the half-state this method exists to make
        # unreachable.
        with transaction.atomic():
            self.banned_at = timezone.now()
            self.ban_reason = reason
            self.save(update_fields=["banned_at", "ban_reason"])
            self.remove_all_content()
        return True

    def unban(self) -> bool:
        """Lift a ban (R102 as amended 2026-09-28: a moderator may).

        Restores the sign-in, the profile, the actor document and the
        localname reservation. **It does not restore content** — see
        :meth:`ban` for why the writing is gone for good — and it cannot
        un-send a ``Delete(Person)``: peers that processed one dropped this
        actor and the follow graph on their side, so they come back only by
        being re-followed. What is restored is the *account*, not the
        history.

        Leaves any separate suspension alone. A ban and a suspend are
        independent facts, and lifting one must not silently lift the other;
        an account that was suspended *and* banned is still suspended after
        an unban, and its own lift lives on the profile it can still reach.
        """
        if self.banned_at is None:
            return False
        self.banned_at = None
        self.ban_reason = ""
        self.save(update_fields=["banned_at", "ban_reason"])
        return True

    @property
    def username(self) -> str:
        """Full identity, localname@domain (§3.2).

        Local users qualify against this instance's domain; remote mirrors
        already carry their home domain in the localname
        (<preferredUsername>@<netloc>, M4 increment 4).
        """
        if self.local:
            return f"{self.localname}@{settings.DOMAIN}"
        return self.localname

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
        The deterministic order (sort title, year, id) keeps the films page's
        pagination stable — id breaks sort-title ties and orders year-less
        stubs.
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
        return films.order_by("sort_title", "year", "id").annotate(
            user_rating=Subquery(rating)
        )

    # --- Feed membership (M5 increment 3, R54) --------------------------------

    def feed_member_ids(self):
        """The user ids whose content appears in this user's home feed.

        Self plus the users they follow, minus the users they have blocked:
        blocking a user removes them from the feed entirely — their statuses
        and shelf events alike (R54). You cannot block yourself, so self is
        always present. Following a blocked user stays allowed (R50) but their
        content stays hidden while the block stands.

        A **suspended** account is dropped here too (R102), and by a
        different rule than the block. A block is per-viewer state that
        happens to be subtracted from a shared set; suspension is a fact
        about the followed account that holds for every viewer at once, so
        it narrows the followed query itself rather than joining the
        ``blocked`` subtraction. This one method is the feed's whole
        suspension filter — home statuses and shelf events both come from
        this membership set.

        A **banned** account is dropped by the same shape and for a related
        but distinct reason. Their statuses are soft-deleted, so the status
        half of the feed is already handled by the ``deleted`` filter —
        what this clause actually catches is **shelf events**, which are
        derived from ``ShelfFilm`` rows and never pass through ``deleted``.
        Without it a banned member's "added X to their Watchlist" would go
        on appearing in their followers' feeds forever. Two columns, two
        reasons, one clause each: this is the account-visibility half of ban,
        not the content-removal half.

        Self is unconditional. A banned user cannot reach this line:
        ``is_active`` is derived from the same column, so their session is
        already cut on the request that would have rendered it (R102b).
        """
        blocked = set(self.blocks.values_list("id", flat=True))
        followed = [
            uid
            for uid in self.follows.filter(
                suspended_at__isnull=True, banned_at__isnull=True
            ).values_list("id", flat=True)
            if uid not in blocked
        ]
        return [self.id] + followed


class SiteSettings(models.Model):
    """Instance-wide settings — a single row (pk=1), admin-managed (§3.2).

    The signup policy gates /signup/ once the instance is operational (R12's
    wizard covers first run, which happens before any settings exist).
    ``invite`` means the open door is shut: every new account arrives either
    through the admin or on a link a member sent — see ``Invite`` and
    ``invite_scope`` (R82), which is the mechanism this field used to point
    at as future work.
    """

    OPEN = "open"
    INVITE = "invite"
    SIGNUP_POLICIES = ((OPEN, "Open"), (INVITE, "Invite-only"))

    INVITE_ADMINS = "admins"
    INVITE_ALL = "all"
    INVITE_SCOPES = ((INVITE_ADMINS, "Admins only"), (INVITE_ALL, "All members"))

    name = models.CharField(max_length=100, default="ReelTalk")
    description = models.TextField(blank=True, default="")
    signup_policy = models.CharField(
        max_length=20, choices=SIGNUP_POLICIES, default=OPEN
    )
    # Who may hand out an invite link (R82). Orthogonal to signup_policy:
    # this decides who can mint a link, that one decides whether a link is
    # needed at all. Deliberately conservative by default — on a live
    # invite-only instance the owner should decide to widen the circle, not
    # discover it was wide open.
    invite_scope = models.CharField(
        max_length=20, choices=INVITE_SCOPES, default=INVITE_ADMINS
    )

    class Meta:
        verbose_name_plural = "site settings"

    def __str__(self) -> str:
        return self.name

    @classmethod
    def get_instance(cls) -> "SiteSettings":
        """The single settings row, created on first use with defaults."""
        instance, _ = cls.objects.get_or_create(pk=1)
        return instance

    def may_send_invites(self, user) -> bool:
        """Whether ``user`` may mint an invite link under this scope.

        ``admins`` keeps the growth valve with ``is_staff`` — the accounts
        that can reach /admin/, which is this instance's working meaning of
        "site admin". Checked here as well as behind the login_required
        decorator, so a caller that forgets the decorator still cannot open
        the door to an anonymous request.
        """
        if not user.is_authenticated:
            return False
        if self.invite_scope == self.INVITE_ALL:
            return True
        return user.is_staff


class Invite(models.Model):
    """One single-use invitation to join the instance (R82).

    The invite *is* the credential on an invite-only instance, so it is
    treated like one: a long random code (``secrets.token_urlsafe``, not a
    counter or a short human-readable word an attacker could enumerate), a
    clock on it, and a redemption path that locks the row so two people who
    open the same link at the same moment cannot both walk through. That
    last part is the whole reason this is not a boolean column: the check and
    the mark have to be one atomic step, or "exactly one account per invite"
    is only true until someone tries twice at once.

    ``used_by`` is the account the link created, kept for the audit trail —
    who brought whom in. It is SET_NULL rather than CASCADE so deleting a
    member does not reopen a link that was already handed around; ``used_at``
    stays set, and the seat stays spent.
    """

    code = models.CharField(max_length=64, unique=True, editable=False)
    created_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="invites_sent"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_by = models.OneToOneField(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invite_received",
    )
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        state = "used" if self.used_at else ("expired" if not self.is_live else "live")
        return f"invite {self.code[:8]}… ({state})"

    @classmethod
    def mint(cls, inviter) -> "Invite":
        """Create a fresh single-use invite on ``inviter``'s behalf."""
        return cls.objects.create(
            code=cls._fresh_code(),
            created_by=inviter,
            expires_at=timezone.now() + timedelta(days=INVITE_TTL_DAYS),
        )

    @classmethod
    def _fresh_code(cls) -> str:
        # The unique index is the real guard; this loop just keeps a
        # astronomically unlucky collision from surfacing as a 500 on a
        # button click.
        while True:
            code = secrets.token_urlsafe(24)
            if not cls.objects.filter(code=code).exists():
                return code

    @property
    def is_live(self) -> bool:
        """Usable right now: redeemed by nobody and still inside its window."""
        return self.used_at is None and self.expires_at > timezone.now()

    @classmethod
    def lock_live(cls, code: str) -> "Invite | None":
        """Return the live invite for ``code`` with its row write-locked.

        ``None`` for an unknown, used, or expired code. The lock only means
        anything inside a transaction — the caller holds one across creating
        the account and calling ``redeem``, so the liveness check and the
        redemption cannot be interleaved with a second signup on the same
        code.
        """
        invite = cls.objects.select_for_update().filter(code=code).first()
        if invite is None or not invite.is_live:
            return None
        return invite

    def redeem(self, user) -> None:
        """Spend this invite on the account it just created."""
        self.used_by = user
        self.used_at = timezone.now()
        self.save(update_fields=["used_by", "used_at"])

    def unusable_reason(self) -> str | None:
        """Human sentence for why sign-up can't run on this link, if any.

        Composed here rather than in the view because both the GET branch and
        the lost-the-race POST branch have to say the same thing, and the two
        used to be one place apart in the code and three places apart in the
        copy.
        """
        if self.used_at is not None:
            return (
                "That invite link has already been used — each one opens "
                "exactly one account."
            )
        if self.expires_at <= timezone.now():
            return (
                f"That invite link expired on {date_format(self.expires_at, 'F j, Y')}."
            )
        return None


class EmailVerificationToken(models.Model):
    """One single-use proof that a member controls their own email address (2F).

    Deliberately the same shape as :class:`Invite`: an invite and a
    verification link are the same kind of object — a long random credential
    with a clock on it, which has to be *spent* atomically, because the
    liveness check and the marking cannot be two separate statements or "one
    click" is only true until two things click at once.

    Two fields diverge from the invite, and both divergences are named rather
    than incidental (R120):

    * ``user`` replaces ``created_by``. An invite has two parties — the
      inviter and the redeemer — and ``created_by`` names the first.
      Verification has **one subject**: the account itself. Nobody else mints
      it, so there is no second party to name.
    * ``email`` replaces ``used_by``. The consumer of a verification token is
      the same account by construction, so ``used_by`` would carry exactly no
      information. What this flow needs in that slot is the **address
      binding** — the mailbox the link was actually sent to — so that a token
      minted for A can never verify B. ``consume()`` enforces it rather than
      leaving the check to each caller to remember.

    A token *table* rather than a column on ``User`` is an audit property, and
    it is the reason this is not shaped like Mastodon's ``confirmation_token``.
    There the raw token lives on the user row, is never cleared on confirm,
    and is *reused* while it is still live — so five resends email the same
    URL five times, a leak from any copy is the live credential, and nothing
    records which copy was clicked. Here every send is its own row, a
    superseded token is genuinely dead rather than merely old, and the table
    says which one was used and when.

    Nothing in this class sends mail. Deciding to send, the transport, and the
    console-backend guard are 2F-2; the gate and the routes are 2F-3.
    """

    code = models.CharField(max_length=64, unique=True, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="email_verification_tokens",
    )
    # The address this token proves control of. Required, never blank, and
    # ``mint()`` refuses to issue one to an address-less account — a token
    # bound to the empty string would make ``User.email_verified`` true for a
    # user with no address at all, because ``"" == ""``. That is not
    # hypothetical on this schema: blank addresses are legal by design (R118's
    # partial index exists precisely to allow them).
    email = models.EmailField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    # Closed out by a newer mint (R120's supersede). Its own column rather
    # than a value in ``used_at``, because the two are different facts and
    # only one of them is provenance: ``used_at`` means "a human clicked
    # this", which is the single meaning R123 refuses to dilute. Folding
    # supersede into it would make a resend indistinguishable from a click —
    # and the admin's token inline (R123) exists to tell those apart.
    #
    # Superseded rows are kept, not deleted, for the same reason: a row that
    # is gone explains nothing about the send it recorded.
    superseded_at = models.DateTimeField(null=True, blank=True)
    # What came of actually mailing this (2F-2). Two facts and deliberately
    # no stored status label — :attr:`send_state` below reads them, so the
    # queued/sent/failed wording an admin is shown can never disagree with
    # what happened. Same one-fact economy as ``email_verified`` and R93's
    # unread timestamp.
    #
    # ``sent_at`` means **the transport accepted the message**, not that
    # anybody received it. That is the honest ceiling on what a send can
    # attest — R88's "`202 Accepted` is not delivery", which 2E trap 6 is
    # built on — and it is stated here so the column name does not overclaim
    # what the code behind it knows.
    #
    # Not redundant with ``created_at``. That is when the link was minted,
    # which in the signup flow is the same second the account appeared.
    # The mail can fail seconds afterwards, and the 2F-3 resend cooldown has
    # to know which of the two it is counting from: keyed on ``created_at``,
    # a send that never left the box still makes the member wait out a
    # window for mail that never came. Keyed on ``sent_at``, a failed send
    # costs them nothing.
    sent_at = models.DateTimeField(null=True, blank=True, default=None)
    # The failure, as one line. ``TextField`` rather than a bounded
    # ``CharField`` because an SMTP diagnostic is precisely the thing that
    # must not be cut short to fit: ``554 5.7.1 ... Sender is not same as
    # SMTP authenticate username`` is the sentence that explained a whole
    # broken deploy in 2E, and truncating it would amputate the part that
    # names the cause.
    send_error = models.TextField(blank=True, default="")
    # Where the mint was asked for (2F-3). This exists for one reason: R122's
    # second throttle axis has to count *mails sent from a source*, and the
    # only durable, shared record of a mail being sent is this table. Putting
    # the per-IP counter in the cache instead would mean one axis of one
    # control lives in Postgres and the other in a process-memory store the
    # project itself describes as a nicety — wiped by every deploy, invisible
    # to the other web process, and unreadable by the admin who is trying to
    # work out why a member keeps getting links.
    #
    # It is also the audit answer R123's token inline wants: not just which
    # send happened, but who asked for it and from where. Blank means "not
    # from a request" — a fixture or a management command.
    #
    # Stored as text rather than ``GenericIPAddressField`` on purpose: an
    # unknown or malformed source must be recordable rather than rejected, and
    # nothing here queries by subnet.
    request_ip = models.CharField(max_length=45, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        if self.used_at:
            state = "used"
        elif self.superseded_at:
            state = "superseded"
        elif self.expires_at <= timezone.now():
            state = "expired"
        else:
            state = "live"
        return f"verification {self.code[:8]}… for {self.user.localname} ({state})"

    def save(self, *args, **kwargs):
        # Normalised exactly like ``User.email`` (R118), because the address
        # binding is a string comparison. If the token held ``Foo@x.com`` and
        # the account held ``foo@x.com``, a perfectly good token would read as
        # a mismatch and the member would be stuck on a link that "doesn't
        # work" for a reason nobody can see. Both sides fold, so the
        # comparison means what it looks like.
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    @classmethod
    def live(cls):
        """Every live token, as a queryset — the query-level ``is_live``.

        ``mint()`` needs to close out a user's live tokens in one UPDATE
        rather than load-then-loop, and ``lock_live()`` filters on the same
        predicate. Kept as one expression so the two cannot drift apart from
        the property they mirror.
        """
        return cls.objects.filter(
            used_at__isnull=True,
            superseded_at__isnull=True,
            expires_at__gt=timezone.now(),
        )

    @property
    def is_live(self) -> bool:
        """Usable right now: unspent, unsuperseded, still inside its window.

        Stays exactly equivalent to ``unusable_reason() is None`` — the
        ``lock_live()`` path filters on this and then reports that, so if the
        two ever diverged a token would be refused with no reason given, or
        accepted while a reason exists.
        """
        return (
            self.used_at is None
            and self.superseded_at is None
            and self.expires_at > timezone.now()
        )

    @property
    def send_state(self) -> str:
        """``"sent"`` / ``"failed"`` / ``"queued"`` — read off the two facts.

        Derived rather than stored, for the standing reason (R93, R102b,
        ``email_verified``): a stored label is a third thing that has to be
        kept in step with the two it summarises, and the failure mode of
        forgetting is a row that says "sent" with ``sent_at`` empty — the
        exact unreadable state this whole table exists to prevent.

        The three are mutually exclusive because **one row is one send
        attempt**. Every send mints a fresh row (R120's row-per-send) and
        django-q does not re-run a task that raised, so a row is never
        mailed twice and ``sent_at`` and ``send_error`` never both hold. If
        a future change ever re-sends a row in place, this derivation is
        the thing to revisit — not the columns.

        Note what ``"queued"`` covers: it is both "not picked up yet" and
        "never scheduled". The code cannot tell those apart from the row
        alone, which is why the loud console-backend warning lives at enqueue
        time in the request rather than being reconstructed from here.
        """
        if self.sent_at is not None:
            return "sent"
        if self.send_error:
            return "failed"
        return "queued"

    @classmethod
    def mint(cls, user, request_ip: str = "") -> "EmailVerificationToken":
        """Open a fresh verification token for ``user``'s current address.

        ``request_ip`` is recorded for R122's per-source throttle and for the
        admin's audit view of who asked for a send. It is optional because
        not every mint comes from a request — a fixture or a management
        command has no source to name.

        Supersedes the user's prior live tokens first (R120). That clause is
        not tidiness: without it N tokens stay live per account and "strictly
        single-use" is false whenever any older one still works. With a row per
        send plus a supersede marker, a leaked stale token is dead *and* the
        table still records every send.

        Raises ``ValueError`` when the account has no address. There is no
        such thing as proving control of nothing, and minting one anyway
        would put a token into the world whose consumption satisfies
        ``"" == ""`` — reading as a verified address on an account that has
        none.
        """
        if not user.email:
            raise ValueError(
                f"@{user.localname} has no email address to verify; "
                "a verification token must be bound to a real address."
            )
        now = timezone.now()
        with transaction.atomic():
            # Atomic because a mint that superseded but failed to create would
            # leave the member with no live token and no new link — strictly
            # worse than either outcome alone.
            cls.live().filter(user=user).update(superseded_at=now)
            return cls.objects.create(
                code=cls._fresh_code(),
                user=user,
                email=user.email,
                request_ip=request_ip or "",
                expires_at=now + timedelta(hours=EMAIL_VERIFICATION_TTL_HOURS),
            )

    @classmethod
    def _fresh_code(cls) -> str:
        # 32 bytes, ~256 bits, from ``secrets`` — a credential, not a
        # guessable word. The unique index is the real guard; this loop just
        # keeps an astronomically unlucky collision from surfacing as a 500.
        #
        # Stronger than the invite's 24 bytes on purpose. A verification token
        # is what password reset will one day be built on, and unlike an
        # invite link it gets opened by mail clients and link scanners whose
        # behaviour we do not control.
        while True:
            code = secrets.token_urlsafe(32)
            if not cls.objects.filter(code=code).exists():
                return code

    @classmethod
    def lock_live(cls, code: str) -> "EmailVerificationToken | None":
        """Return the live token for ``code`` with its row write-locked.

        ``None`` for an unknown, spent, superseded or expired token. The lock
        only means anything inside a transaction — the caller holds one across
        the consume, so the liveness check and the spend cannot be
        interleaved. Two things click a verification link at once all the time
        in practice: a person and their mail client's link prefetcher.
        """
        token = cls.objects.select_for_update().filter(code=code).first()
        if token is None or not token.is_live:
            return None
        return token

    def consume(self) -> None:
        """Spend this token and record the address it proved as verified.

        Both halves in one method, on the 2E "one call site, not several"
        lesson. If the view had to mark the token spent *and* stamp the user,
        a later caller could do one without the other — a token that verifies
        nothing, or a verified state no token paid for. R123's whole claim is
        that *every* verified flag traces to a link that was actually clicked,
        and that is only a property of the code if the two writes are one
        operation.

        Assumes the caller reached this token through :meth:`lock_live`, which
        is what enforces single use. The spend itself is idempotent in effect
        — re-running it writes the same values — because a second consume
        cannot verify anything the first one did not.

        Raises :class:`AddressMismatchError` when the account's address is no
        longer the one this token was minted for. That is the R120 binding
        made real: the admin may correct an address at any moment (R124), and
        a link sent to the old one must never stand in as proof of the new
        one.
        """
        if self.email != self.user.email:
            raise AddressMismatchError(
                f"This token was issued for {self.email}, but @{self.user.localname} "
                f"is at {self.user.email} now."
            )
        with transaction.atomic():
            self.used_at = timezone.now()
            self.save(update_fields=["used_at"])
            self.user.email_verified_at = self.used_at
            self.user.verified_email = self.email
            self.user.save(update_fields=["email_verified_at", "verified_email"])

    def unusable_reason(self) -> str | None:
        """Human sentence for why this link can't verify anything, if any.

        Specific rather than uniform, and that is a deliberate asymmetry with
        the resend form (R122). The enumeration risk lives on the path where
        you submit an **address** and learn whether it is registered; this
        path needs an unguessable token to reach at all, so whoever reads this
        message already holds a real token and is entitled to know what is
        wrong with it. Telling a person whose link expired that it expired —
        with a way to get a new one — is the difference between a recoverable
        annoyance and a dead end.
        """
        if self.used_at is not None:
            return "That verification link has already been used."
        if self.superseded_at is not None:
            return (
                "That verification link was replaced by a newer one — use the "
                "most recent link we sent you."
            )
        if self.expires_at <= timezone.now():
            return (
                f"That verification link expired on "
                f"{date_format(self.expires_at, 'F j, Y')}."
            )
        return None


class LinkDomain(models.Model):
    """One outbound-link domain the instance allows (§3.2 site settings).

    User-authored markdown renders a link's ``href`` only when its host
    matches an allowed domain — exactly or as a subdomain of it. With no
    domains allowed, every external link in user content is stripped (the
    anchor text stays). Checked at write time in ``render_markdown``.
    """

    domain = models.CharField(max_length=253, unique=True)

    def save(self, *args, **kwargs):
        self.domain = self.domain.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.domain

    @classmethod
    def is_allowed(cls, host: str) -> bool:
        """True when ``host`` (a URL netloc) matches an allowed domain."""
        host = host.lower().strip(".")
        # Deny-by-default on anything odd: userinfo and ports are stripped so
        # "user@evil.com" / "example.com:8080" reduce to their bare host.
        if "@" in host:
            host = host.rsplit("@", 1)[-1]
        host = host.split(":", 1)[0]
        if not host:
            return False
        allowed = set(cls.objects.values_list("domain", flat=True))
        return any(host == d or host.endswith("." + d) for d in allowed)
