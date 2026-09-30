"""Admin registrations.

The user admin deliberately uses Django's auth forms rather than a bare
``ModelAdmin``. A plain ``ModelAdmin`` on a custom user model exposes the
model's ``password`` column as an ordinary text input and writes whatever is
typed straight into it — unhashed, so the account cannot log in
(``check_password`` expects a hash) and a plaintext secret is left sitting in
the database. ``UserCreationForm`` hashes through ``set_password``, and
``UserChangeForm``'s password field is read-only and re-submits the stored
hash, so no admin edit can put plaintext in that column.

The federation internals are read-only or absent for the same kind of reason:
they are written by key generation and by the mirror path, and an admin typo
in one of them breaks signing or deliveries in a way that is hard to undo.
"""

from django import forms
from django.contrib import admin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.core.exceptions import ValidationError
from django.utils.html import format_html

from .forms import LOCALNAME_RE
from .models import Invite, LinkDomain, SiteSettings, User
from .verify import change_email


class InviteUserCreationForm(UserCreationForm):
    """Admin-side account creation — the invite-only path (R76).

    Named before the R82 invite links existed: this is "the admin creates
    the account directly", not the other half of that feature. The two are
    alternatives on an invite-only instance, not steps of one flow.

    Hashes the password through ``set_password`` and applies the same identity
    rules as signup (R12), so an admin cannot mint a name that other instances
    would treat as a different identity from one that already exists. The
    30-char cap is the signup rule, restated here rather than inherited: the
    model field is wider only so remote mirrors can hold
    ``<preferredUsername>@<netloc>``.
    """

    localname = forms.CharField(max_length=30)

    class Meta:
        model = User
        fields = ("localname", "display_name", "email")

    def clean_localname(self):
        localname = self.cleaned_data["localname"].strip()
        if not LOCALNAME_RE.match(localname):
            raise ValidationError(
                "Use 1-30 characters: letters, numbers, '.', '_' or '-', "
                "starting with a letter or number."
            )
        if User.objects.filter(localname__iexact=localname).exists():
            raise ValidationError("That name is already taken.")
        return localname


class AdminUserChangeForm(UserChangeForm):
    """Editing an existing account.

    ``UserChangeForm`` declares ``password`` as a read-only hash field whose
    ``clean_password`` returns the stored value regardless of what was
    submitted, so this column can never be overwritten with plaintext.
    """

    class Meta:
        model = User
        fields = (
            "display_name",
            "email",
            "avatar",
            "is_staff",
            "is_superuser",
            # Kept consistent with the Roles fieldset below. Which half is
            # load-bearing was measured, not assumed: ModelAdmin.get_form
            # rebuilds this form from flatten_fieldsets(self.get_fieldsets(...)),
            # so on the admin change page the FIELDSET is what puts the field on
            # screen and this tuple is overridden. Deleting it from here changes
            # nothing in the admin; deleting it from the fieldset takes the field
            # off the page. This entry is what makes AdminUserChangeForm correct
            # for anyone using the form directly.
            "is_moderator",
            "report_email",
        )


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    form = AdminUserChangeForm
    list_display = ["localname", "display_name", "email", "local", "is_staff"]
    list_filter = ["local", "is_staff", "is_superuser"]
    search_fields = ["localname", "display_name", "email"]
    # private_key is deliberately absent everywhere: nothing in the admin needs
    # to read a signing key, and putting one on screen invites a copy into a
    # screenshot, a ticket, or a support chat.
    readonly_fields = [
        "raw_summary",
        "summary",
        "local",
        "actor_url",
        "inbox_url",
        "public_key",
        "date_joined",
        "last_login",
        "report_email_status",
    ]
    fieldsets = [
        (None, {"fields": ("localname", "password")}),
        ("Profile", {"fields": ("display_name", "email", "avatar")}),
        (
            "Roles",
            {
                "fields": (
                    "is_staff",
                    "is_superuser",
                    "is_moderator",
                    # The notification behaviour sits beside the grant that
                    # causes it, so one screen shows a moderator's whole
                    # capability (R116).
                    "report_email",
                    "report_email_status",
                )
            },
        ),
        (
            "Federation (read-only)",
            {
                "classes": ["collapse"],
                "fields": (
                    "local",
                    "actor_url",
                    "inbox_url",
                    "public_key",
                    "raw_summary",
                    "summary",
                    "date_joined",
                    "last_login",
                ),
            },
        ),
    ]
    add_fieldsets = [
        (
            None,
            {
                "classes": ["wide"],
                "fields": (
                    "localname",
                    "display_name",
                    "email",
                    "password1",
                    "password2",
                ),
            },
        )
    ]

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = InviteUserCreationForm
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return self.add_fieldsets
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        readonly = list(self.readonly_fields)
        if obj is not None:
            # A localname IS the federated identity: renaming it orphans every
            # URL already published for that account and invalidates the mirrors
            # other instances hold. Editable only when creating.
            readonly.append("localname")
        return readonly

    def save_model(self, request, obj, form, change):
        """Route every address change through :func:`change_email` (R125).

        The address is taken out of the plain save's hands entirely. The
        row is written with the address it already had, and the submitted
        one arrives only through ``change_email`` — the sole writer, which
        fires the notice to the previous address from inside itself. The
        alternative, letting ``form.save()`` write the new address and then
        sending the alert from here, is what R125 rules out: an alert that
        lives in the admin view is an alert that the next writer of the
        field omits, and on this instance the notice is the only signal a
        member ever gets that their mail now lands elsewhere.

        Only the address gets this treatment. Every other field on the form
        saves normally, so a profile edit that leaves the address alone
        produces no write to it, no superseded tokens, and no notice.

        The **add** path deliberately does not send. R124's admin-triggered
        *send verification* action is 2F-3's surface; an admin creating an
        account here is not the same act as a member signing up, and
        giving the create form a side-effecting mail would put a second
        unsolicited sender on the instance that nobody asked for.
        """
        if not change:
            super().save_model(request, obj, form, change)
            return

        submitted_email = obj.email
        obj.email = (
            User.objects.filter(pk=obj.pk).values_list("email", flat=True).first() or ""
        )
        super().save_model(request, obj, form, change)
        change_email(obj, submitted_email, changed_by=request.user)

    @admin.display(description="Report email delivery")
    def report_email_status(self, obj):
        """Say out loud whether the toggle above can actually do anything.

        ``email`` is ``blank=True`` — unique only when set, per R118's
        partial index — so a moderator with the ``report_email`` box ticked
        and no address on file receives nothing, and the recipient query
        skips them silently, because skipping is
        the only correct thing it can do. 2E trap 2's requirement is that
        the skip be **visible**: without this line the admin ticks a box,
        sees it saved, and has no way to know the mail will never go out.
        The toggle is not lying, but the screen would be incomplete.
        """
        if not obj.email:
            return format_html(
                '<span style="color:#b3261e;font-weight:600;">{}</span>',
                "Nothing will be sent — this account has no email address.",
            )
        if not obj.report_email:
            return format_html(
                '<span class="help-text">Opted out. Nothing is sent to {}.</span>',
                obj.email,
            )
        return format_html(
            '<span style="color:#237a4b;">Will be sent to <strong>{}</strong>.</span>',
            obj.email,
        )


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ["name", "signup_policy", "invite_scope"]


@admin.register(Invite)
class InviteAdmin(admin.ModelAdmin):
    """The invite ledger, read-only (R82).

    No add: an invite minted here has no inviter to credit, and an admin
    who just wants an account already has the direct create-user path. No
    edit either — the code is generated, not authored, and the used/live
    state is the record of something that happened rather than a setting.
    What this page is for is answering who sent what, whether it landed,
    and who it landed on. Deleting a row stays available: that is how an
    owner clears an outstanding invite for good.
    """

    list_display = ["short_code", "created_by", "created_at", "state", "used_by"]
    list_filter = ["created_by"]
    search_fields = ["code", "created_by__localname", "used_by__localname"]
    date_hierarchy = "created_at"
    readonly_fields = [
        "code",
        "created_by",
        "created_at",
        "expires_at",
        "used_by",
        "used_at",
    ]

    @admin.display(description="Invite code", ordering="code")
    def short_code(self, obj):
        # Truncated in the list on purpose: a full live code on a screen is
        # a credential a screenshot can carry, the same reason private_key
        # never reaches this page. The detail view shows it in full for the
        # admin who actually needs to send it.
        return f"{obj.code[:8]}…"

    @admin.display(description="State", ordering="used_at")
    def state(self, obj):
        if obj.used_at:
            return "used"
        return "live" if obj.is_live else "expired"

    def has_add_permission(self, request):
        return False


@admin.register(LinkDomain)
class LinkDomainAdmin(admin.ModelAdmin):
    list_display = ["domain"]
    search_fields = ["domain"]
