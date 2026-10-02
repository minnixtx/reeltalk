"""Admin registrations.

The user admin deliberately uses Django's auth forms rather than a bare
``ModelAdmin``. A plain ``ModelAdmin`` on a custom user model exposes the
model's ``password`` column as an ordinary text input and writes whatever is
typed straight into it — unhashed, so the account cannot log in
(``check_password`` expects a hash) and a plaintext secret is left sitting in
the database. ``UserCreationForm`` hashes through ``set_password``, and the
change form here goes through the same writer rather than the column.

That writer is the point. ``UserChangeForm`` used to give this page a
read-only hash field that no admin could overwrite, which kept plaintext out
but also meant **an admin could not set a member's password at all** — the
only path was a shell on the server. §2G made the field real, and the way
back into "no admin edit can put plaintext in that column" is
:func:`reeltalk.social.passwords.set_password`: the admin submits a
plaintext credential through a validated form, and the single writer hashes
it. Nothing in this class touches ``obj.password`` directly, which is what
makes the guarantee about the code rather than about the widget.

The federation internals are read-only or absent for the same kind of reason:
they are written by key generation and by the mirror path, and an admin typo
in one of them breaks signing or deliveries in a way that is hard to undo.
"""

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.html import format_html, format_html_join
from django.utils.timesince import timesince
from django.utils.timezone import localtime

from reeltalk.moderation.representative import is_instance_actor
from reeltalk.proxy_trust import client_ip

from .forms import LOCALNAME_RE, NewPasswordPairMixin
from .models import (
    EmailVerificationToken,
    Invite,
    LinkDomain,
    PasswordResetToken,
    SiteSettings,
    User,
)
from .passwords import set_password
from .verify import change_email, send_verification_email

# How many of an account's verification sends the inline shows. Bounded
# because the row count is not ours to control — every resend adds one — and
# an admin page that renders ten thousand rows is unusable for the ten that
# matter. The total is always stated, so a cap never hides the shape of what
# happened.
TOKEN_INLINE_LIMIT = 15

# The admin text colours, matching the ones ``report_email_status`` already
# uses so the verification block reads as part of the same screen rather
# than a new visual language.
RED = "#b3261e"
GREEN = "#237a4b"


def redirect_to_user_change(obj):
    """Back to this account's own change page."""
    return redirect(reverse("admin:social_user_change", args=[obj.pk]))


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


class AdminUserChangeForm(NewPasswordPairMixin, UserChangeForm):
    """Editing an existing account.

    ``password`` is set to ``None`` to drop the field ``UserChangeForm``
    declares — a ``ReadOnlyPasswordHashField`` with ``disabled=True`` whose
    ``clean_password`` returned the stored hash no matter what was
    submitted. That field is why an admin could not set a member's password
    from the browser at all, and why "the admin keeps a raw password field"
    had to be re-asked before §2G was built on it.

    In its place: two real inputs, blank meaning "change nothing". They are
    **not** bound to the model, so ``form.save()`` cannot write the
    ``password`` column and no plaintext can reach it by accident. The write
    happens in :meth:`UserAdmin.save_model`, through
    :func:`~reeltalk.social.passwords.set_password`, which hashes and
    carries every consequence of the change with it.
    """

    password = None
    password1 = forms.CharField(
        label="New password",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=(
            "Leave both password boxes blank to keep the current password. "
            "Filling them in replaces it and signs that member out "
            "everywhere, including here if this is your own account."
        ),
    )
    password2 = forms.CharField(
        label="Confirm new password",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

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
        "verification_status",
        "send_verification",
        "verification_tokens",
        "password_hash",
        "reset_tokens",
    ]
    fieldsets = [
        (
            None,
            {
                "fields": (
                    "localname",
                    # The hash stays visible because it is the read the
                    # previous page had — "is a password set, and which
                    # one" — and removing it would take something away that
                    # nobody asked to lose. It is not the write path, and
                    # it cannot be: it is a display method, not a field.
                    "password_hash",
                    "password1",
                    "password2",
                )
            },
        ),
        ("Profile", {"fields": ("display_name", "email", "avatar")}),
        (
            "Email verification",
            {
                "fields": (
                    "verification_status",
                    "send_verification",
                    "verification_tokens",
                )
            },
        ),
        (
            "Password reset",
            {
                # Read-only, and deliberately placed right below the
                # verification block rather than down by the password
                # inputs: the question an admin brings here is usually
                # "did the recovery mail go out and what happened to it",
                # and that answer sits beside the other mail answer.
                "fields": ("reset_tokens",)
            },
        ),
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

        The **add** path deliberately does not send, and still does not after
        2F-3b. An admin creating an account here is not the same act as a
        member signing up, and a side-effecting mail on the create form
        would put a second unsolicited sender on the instance that nobody
        asked for — plus a checkbox left ticked sends mail the admin did not
        mean to send at that moment. The unblock is the explicit button on
        the account's own change page, which is a deliberate press rather
        than a default.
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

        # The password gets the same treatment as the address: the plain
        # save never had it in its hands, and the submitted one arrives
        # only through the single writer. ``AdminUserChangeForm`` drops the
        # model-bound ``password`` field entirely, so ``form.save()`` has
        # nothing to write to that column — which is what makes "no
        # plaintext in the password column" a property of the code rather
        # than of a disabled widget.
        #
        # Blank means no change, and that is checked here rather than by
        # the writer, because "did the admin mean to rotate this
        # credential?" is a question about this form, not about what a
        # password is.
        new_password = form.cleaned_data.get("password1") or ""
        if new_password:
            result = set_password(obj, new_password, changed_by=request.user)
            self.message_user(
                request,
                "Password changed. Every session that account had is now "
                "invalid, including your own if this is your own account — "
                "sign in again to keep working there. "
                + (
                    "The member has been mailed a notice."
                    if result["notified"]
                    else "No notice mailed: this account has no address on file."
                ),
                messages.SUCCESS,
            )

        if "_send_verification" in request.POST:
            self.send_verification_now(request, obj)

    def response_change(self, request, obj):
        """Stay on the account after a send, rather than bouncing to the list.

        The send button is a submit on this account's own change form, so
        without this the admin is thrown back to the user list and loses
        the page they were reading — including the status line that just
        changed. Django's own ``_continue`` does exactly this for Save; the
        send button needs the same treatment because it is pressed from the
        middle of the page rather than from the submit row.
        """
        if "_send_verification" in request.POST:
            return redirect_to_user_change(obj)
        return super().response_change(request, obj)

    def send_verification_now(self, request, obj):
        """Trigger the verification mail for this account (R124).

        **This is the same send path signup uses**, not a parallel one. It
        calls :func:`~reeltalk.social.verify.send_verification_email`,
        which mints a token bound to the account's current address and
        enqueues the mail on the worker. Nothing here writes a verified
        column, and nothing here *can* — the only writer of that state is
        ``consume()``, reached by a human clicking the link. That is the
        whole of R123: the admin may ask, only the click attests.

        Going through the shared helper rather than a bespoke admin send is
        also what keeps the two paths from drifting. A second send
        implementation is a second set of guards, and the first thing a
        future change to the send path (a new throttle, a new template, a
        new binding rule) would miss is the copy nobody thought to update.

        The address used is the one that just saved, so correcting a typo and
        pressing send in one save mails the **corrected** address — which is
        precisely R124's recovery flow, and works because ``change_email``
        has already run and left ``obj.email`` at the new value.
        """
        if not obj.email:
            self.message_user(
                request,
                "Nothing sent: this account has no email address to verify.",
                messages.ERROR,
            )
            return
        result = send_verification_email(obj, request_ip=client_ip(request))
        if result["enqueued"]:
            self.message_user(
                request,
                format_html(
                    "Verification email queued to <strong>{}</strong>. It "
                    "replaces any link sent before this one.",
                    obj.email,
                ),
                messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                f"Nothing sent ({result['reason']}).",
                messages.WARNING,
            )

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

    # --- the 2F-3b verification surface ---------------------------------
    #
    # Everything the admin is allowed to know about an account's verified
    # state, and everything they are allowed to *do* about it, in one place.
    # R123 draws the line through the middle of this block: the admin may
    # read the state and may trigger the mail, and may never move the state.
    # That is why ``email_verified_at`` and ``verified_email`` appear
    # nowhere in this class — not editable, not read-only, not at all. The
    # only thing on screen is the read derived from them.

    @admin.display(description="Verified address")
    def verification_status(self, obj):
        """The one line that says whether this account can sign in, and why not.

        Same job ``report_email_status`` does for the report toggle: a
        state the screen would otherwise leave the admin to guess at. Under
        R119 the guess is expensive, because an unverified account is not
        broken — it is unfinished, and there is exactly one way to finish
        it, which is a click this page cannot make on anyone's behalf.

        **Why the instance representative gets its own sentence.** Left to
        the generic branch it would read as a stuck account with no address,
        which invites an admin to "fix" it by giving it one. ``_instance``
        is the signing identity for every outbound ``Flag`` (R111) and must
        stay address-less, so the page says so rather than leaving the
        inference to be drawn wrong.
        """
        if is_instance_actor(obj):
            return format_html(
                '<span class="help-text">{}</span>',
                "Instance representative — infrastructure, not a member. "
                "It never signs in and needs no address. Do not give it one.",
            )
        if not obj.local:
            return format_html(
                '<span class="help-text">{}</span>',
                "Remote account — it signs in on its own home instance, so "
                "verification does not apply here.",
            )
        if obj.email_verified:
            return format_html(
                '<span style="color:{};">Verified {}, {}</span>',
                GREEN,
                _stamp(obj.email_verified_at),
                obj.email,
            )
        if not obj.email:
            return format_html(
                '<span style="color:{};font-weight:600;">{}</span>',
                RED,
                "Not verified, and there is no address to verify. This "
                "account cannot sign in until an address is set above and a "
                "link is sent to it.",
            )

        live = EmailVerificationToken.live().filter(user=obj).first()
        if live is not None:
            return format_html(
                '<span style="color:{};font-weight:600;">{}</span>'
                '<br><span class="help-text">A link was sent {} and is '
                "still live until {}. Sending another replaces it — the one "
                "in the mailbox stops working the moment the new one goes "
                "out.</span>",
                RED,
                f"Not verified — this account cannot sign in until someone "
                f"clicks the link sent to {obj.email}.",
                _since(live.sent_at or live.created_at),
                _stamp(live.expires_at),
            )
        return format_html(
            '<span style="color:{};font-weight:600;">{}</span>'
            '<br><span class="help-text">No live link. The most recent '
            "send is in the list below.</span>",
            RED,
            f"Not verified — this account cannot sign in until someone "
            f"clicks the link sent to {obj.email}.",
        )

    @admin.display(description="Send verification")
    def send_verification(self, obj):
        """The button that asks, and nothing more.

        A plain submit on this account's own change form. That placement is
        deliberate: the admin is already looking at the status line above,
        so the read and the response are on the same screen rather than
        separated by a click into a list. It also means the button inherits
        the form's CSRF protection and its validation instead of
        reinventing either.

        The label changes when a live link already exists. Every send
        supersedes the prior token — that is what makes single-use mean
        anything — so pressing this twice silently kills the link the
        member is currently holding. Saying so on the button is cheaper
        than a rule that blocks the press, and leaves the admin free to
        make the call when they are deliberately rotating a link.
        """
        if not self._may_send_verification(obj):
            return format_html('<span class="help-text">{}</span>', _why_not(obj))
        live = EmailVerificationToken.live().filter(user=obj).first()
        label = (
            "Send a new link (replaces the live one)"
            if live is not None
            else "Send verification email"
        )
        return format_html(
            '<button type="submit" name="_send_verification" value="1"'
            ' class="button">{}</button>',
            label,
        )

    def _may_send_verification(self, obj) -> bool:
        """Whether this account is one a send can actually help."""
        return (
            obj.pk is not None
            and obj.local
            and bool(obj.email)
            and not obj.email_verified
            and not is_instance_actor(obj)
        )

    @admin.display(description="Verification sends")
    def verification_tokens(self, obj):
        """Every send for this account, newest first, with what came of it.

        R123's requirement is that the admin can see *exactly* what
        happened and *why it failed*, and the two columns that answer that
        are the derived ``send_state`` and the raw ``send_error``. The
        error is rendered whole rather than truncated: it is an SMTP
        diagnostic, and the 2E lesson is that the sentence explaining a
        broken deploy is the long one.

        **The code is truncated, and that is a control rather than tidiness.**
        A full live token on a screen is a credential the admin could open
        themselves and complete the click without the member ever seeing
        the mail — which is hand-verify walking back in through the display
        layer after R123 turned it off at the form layer. The eight
        characters shown are enough to tell one send from another and not
        enough to spend one. The invite admin already draws the same line.
        """
        if obj.pk is None:
            return ""
        sends = EmailVerificationToken.objects.filter(user=obj)
        total = sends.count()
        if not total:
            return format_html(
                '<span class="help-text">{}</span>',
                "No verification mail has ever been sent to this account.",
            )
        rows = list(sends[:TOKEN_INLINE_LIMIT])
        body = format_html_join(
            "\n",
            (
                "<tr><td><code>{}…</code></td><td>{}</td><td>{}</td>"
                "<td>{}</td><td>{}</td></tr>"
            ),
            (_token_cells(token) for token in rows),
        )
        more = (
            ""
            if total <= TOKEN_INLINE_LIMIT
            else format_html(
                '<br><span class="help-text">…and {} earlier send(s) not shown.</span>',
                total - TOKEN_INLINE_LIMIT,
            )
        )
        return format_html(
            '<table class="verification-sends">\n'
            "<thead><tr><th>Token</th><th>Minted</th><th>Send</th>"
            "<th>Link</th><th>Requested from</th></tr></thead>\n"
            "<tbody>\n{}\n</tbody>\n"
            "</table>{}",
            body,
            more,
        )

    @admin.display(description="Current password hash")
    def password_hash(self, obj):
        """What is actually stored, shown the way the old field showed it.

        Read-only, and present for one reason: the field §2G replaced was a
        read-only hash display, and taking that read away was never asked
        for. It is not the write path and cannot become one — it is a
        display method, so there is no widget here whose value could be
        tampered with and no way for this row to put anything into the
        column.

        Shown whole rather than truncated. It is already one-way, and an
        admin who can reach this page can already set a new password
        through the writer above, so nothing is disclosed here that the
        page does not already grant the power to do.
        """
        if not obj.pk or not obj.password:
            return format_html(
                '<span class="help-text">{}</span>', "No password is set."
            )
        return format_html("<code>{}</code>", obj.password)

    @admin.display(description="Password reset sends")
    def reset_tokens(self, obj):
        """Every reset link ever sent for this account, newest first.

        The same read :attr:`verification_tokens` gives, on the other
        table, because the same support question lands here: *did we send
        one, did it fail, who asked and from where*. The two tables are
        separate credentials and stay separate on the screen for the same
        reason — an admin trying to work out what happened to an account
        needs to be able to tell a verification link from a reset link at
        a glance, not read a ``purpose`` column.

        The code is truncated to the same eight characters, for the same
        control that truncates the verification one: a full live token on a
        screen is a credential the viewer could open and spend themselves.
        Eight characters tell one send from another and cannot buy anything.
        """
        if obj.pk is None:
            return ""
        sends = PasswordResetToken.objects.filter(user=obj)
        total = sends.count()
        if not total:
            return format_html(
                '<span class="help-text">{}</span>',
                "No password reset mail has ever been sent to this account.",
            )
        rows = list(sends[:TOKEN_INLINE_LIMIT])
        body = format_html_join(
            "\n",
            (
                "<tr><td><code>{}…</code></td><td>{}</td><td>{}</td>"
                "<td>{}</td><td>{}</td></tr>"
            ),
            (_token_cells(token) for token in rows),
        )
        more = (
            ""
            if total <= TOKEN_INLINE_LIMIT
            else format_html(
                '<br><span class="help-text">…and {} earlier send(s) not shown.</span>',
                total - TOKEN_INLINE_LIMIT,
            )
        )
        return format_html(
            '<table class="verification-sends">\n'
            "<thead><tr><th>Token</th><th>Minted</th><th>Send</th>"
            "<th>Link</th><th>Requested from</th></tr></thead>\n"
            "<tbody>\n{}\n</tbody>\n"
            "</table>{}",
            body,
            more,
        )


def _why_not(obj) -> str:
    """Why the send button is not showing for this account."""
    if is_instance_actor(obj):
        return "Not applicable — the instance representative has no address to verify."
    if not obj.local:
        return "Not applicable — this is a remote account."
    if not obj.email:
        return "Nothing to send — this account has no email address."
    if obj.email_verified:
        return "Nothing to send — this address is already verified."
    return "Not available."


def _since(value) -> str:
    """'4 minutes ago'.

    ``timesince`` returns the bare span ("4 minutes"), so the "ago" is ours.
    The floor matters: it returns "" for anything under a second, and an
    empty string where a timestamp should be reads as a bug on a page whose
    whole job is to be the truth about when something happened.
    """
    if value is None:
        return "just now"
    span = timesince(value)
    return f"{span} ago" if span else "just now"


def _stamp(value) -> str:
    """An unambiguous timestamp. ``localtime`` first so the page is readable
    whatever ``USE_TZ`` says; the value is stored in UTC and the admin is
    the one person who needs to line these up against mail headers."""
    if value is None:
        return "—"
    return date_format(localtime(value), "Y-m-d H:i")


def _token_cells(token):
    """The five cells for one send row, in table order."""
    if token.sent_at is not None:
        send = f"sent {_stamp(token.sent_at)}"
    elif token.send_error:
        send = format_html("failed<br><code>{}</code>", token.send_error)
    else:
        send = "queued (never left the box)"
    link = token.link_state
    if link == "used":
        link = f"used {_stamp(token.used_at)}"
    elif link == "superseded":
        link = f"superseded {_stamp(token.superseded_at)}"
    elif link == "expired":
        link = f"expired {_stamp(token.expires_at)}"
    return (
        token.code[:8],
        _stamp(token.created_at),
        send,
        link,
        token.request_ip or "—",
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
