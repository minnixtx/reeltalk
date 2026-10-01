"""Account forms: signup, the first-run setup wizard, profile editing, sign-in."""

import re

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import authenticate
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables

from .backends import REFUSAL_INACTIVE, REFUSAL_UNVERIFIED, EmailVerificationBackend
from .models import User

# Letters/digits plus '.', '_', '-'; must start with a letter or digit.
LOCALNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


class SignupForm(forms.Form):
    localname = forms.CharField(
        max_length=30,
        widget=forms.TextInput(attrs={"autofocus": "autofocus"}),
    )
    display_name = forms.CharField(max_length=255, required=False)
    # **Required since R118**, which discharges the deferral this field
    # carried from R12. It is not a preference tweak: once R119 puts the gate
    # up, an account with no address can never be verified — ``mint()``
    # refuses a blank address, correctly, because ``"" == ""`` would read as
    # verified on nothing — so a blank here is an account that can never
    # sign in. Making the field optional at this point would be manufacturing
    # bricks one signup at a time.
    email = forms.EmailField(
        required=True,
        help_text="We send a link here to confirm it is really yours. "
        "You cannot sign in until you click it.",
    )
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput,
    )

    def clean_localname(self):
        localname = self.cleaned_data["localname"].strip()
        if not LOCALNAME_RE.match(localname):
            raise ValidationError(
                "Use 1-30 characters: letters, numbers, '.', '_' or '-', "
                "starting with a letter or number."
            )
        # Case-insensitive uniqueness: "Alice" and "alice" would be the same
        # federated identity to other instances.
        if User.objects.filter(localname__iexact=localname).exists():
            raise ValidationError("That name is already taken.")
        return localname

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        # Checked here as well as by the partial unique index so a person gets
        # told at the form rather than a 500 from the database. The index is
        # still the real guard — a management command or a fixture bypasses
        # every form — but "that address already has an account" is exactly
        # the kind of thing a human should be told rather than shown a
        # traceback.
        if User.objects.filter(email=email).exists():
            raise ValidationError(
                "That email address already belongs to an account here. "
                "Sign in with that account instead."
            )
        return email

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two password fields didn't match.")
        if password1:
            try:
                validate_password(password1)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned_data


NOT_VERIFIED_MESSAGE = (
    "That account has not confirmed its email address yet. We have "
    "sent a link — click it, or ask for another one below."
)


class VerificationRefusalMixin:
    """Turns the gate's refusal into the sentence the member needs.

    A **mixin, not one class**, and the reason is a bug this design closes.
    The public login and the admin login need the same copy, so the obvious
    move is to point ``admin.site.login_form`` at a single
    ``AuthenticationForm`` subclass. That silently drops
    ``AdminAuthenticationForm.confirm_login_allowed``, which is the only
    thing enforcing ``is_staff`` at the admin's login page — so a moderator
    (a member by design, never staff under R100) with a correct password is
    signed in through the admin's own form. The gate was holding; the form
    wiring opened a different door next to it. Each endpoint therefore keeps
    its own base and borrows only the refusal logic.

    Note what is deliberately **not** here: ``error_messages``. Django does
    not merge that dict across base classes — each class spreads its parent's
    entries by hand, and the first class in the MRO that defines the
    attribute wins outright. A dict on this mixin would therefore have
    hidden ``inactive`` and ``invalid_login`` from both forms and turned the
    suspension path into a ``KeyError``. Each concrete form below does its
    own spread.

    **The leak this accepts, priced.** A distinct "not verified" message
    tells someone who has already guessed the password that the account is
    unverified. That is taken deliberately: it requires a valid password,
    which is a far weaker position than address enumeration, and the
    alternative is a support dead-end for every member who has not yet
    clicked a link.
    """

    def __init__(self, *args, **kwargs):
        # Set before ``super().__init__`` because that is where clean() runs.
        self.not_verified = False
        super().__init__(*args, **kwargs)

    @sensitive_variables()
    def clean(self):
        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")
        if username is None or not password:
            return self.cleaned_data

        self.user_cache = authenticate(
            self.request, username=username, password=password
        )
        if self.user_cache is not None:
            # ``confirm_login_allowed`` is where the subclass's own requirement
            # lives, so it runs here rather than being reimplemented.
            self.confirm_login_allowed(self.user_cache)
            return self.cleaned_data

        # Nothing got through. Ask the gate which of its terms refused rather
        # than guessing from the fact that ``authenticate`` returned nothing.
        reason, user = EmailVerificationBackend().diagnose(username, password)
        if reason == REFUSAL_UNVERIFIED and self.name_the_gate(user):
            self.not_verified = True
            raise ValidationError(
                self.error_messages["not_verified"], code="not_verified"
            )
        if reason == REFUSAL_INACTIVE:
            raise ValidationError(self.error_messages["inactive"], code="inactive")
        raise self.get_invalid_login_error()

    def name_the_gate(self, user) -> bool:
        """Whether to tell this account its email is what is in the way.

        Always yes on the public form: every member is a member of the
        audience. Overridden by the admin form, which must stay exactly as
        silent as Django's own about accounts that could never reach it.
        """
        return True


class VerificationAwareLoginForm(VerificationRefusalMixin, AuthenticationForm):
    """Public sign-in that says which door actually shut (R119).

    Django's stock answer to every refusal is *"Please enter a correct
    username and password."* Before the gate that was merely imprecise. After
    it, that copy is actively harmful: a member who has not clicked their
    verification link is told their password is wrong and sent looking for a
    password reset that **does not exist yet** (R121 keeps it out of this
    increment), when the one page that would help them is the resend route.
    """

    error_messages = {
        **AuthenticationForm.error_messages,
        "not_verified": NOT_VERIFIED_MESSAGE,
    }


class AdminVerificationLoginForm(VerificationRefusalMixin, AdminAuthenticationForm):
    """The admin's sign-in, with the same gate and the same staff rule.

    ``AdminAuthenticationForm`` is not optional sugar at this endpoint — it is
    the ``is_staff`` check. This class exists so the admin can gain the
    "your email is unverified" sentence without losing it.

    The unverified message is withheld from any account that could not have
    reached the admin anyway. Telling a plain member probing ``/admin/login/``
    that their password was *nearly* right would make this form a better
    oracle than Django's default one, for no benefit: that person cannot
    verify their way into the admin regardless.
    """

    error_messages = {
        **AdminAuthenticationForm.error_messages,
        "not_verified": NOT_VERIFIED_MESSAGE,
    }

    def name_the_gate(self, user) -> bool:
        return bool(user and user.is_staff)


class ResendVerificationForm(forms.Form):
    """The logged-out "I never got the link" form (R122).

    Deliberately thin: it validates that something address-shaped was typed
    and nothing more. **It must not report whether the address is
    registered** — not in an error, not in a delay, not in a different
    message — because this is the one surface in this whole increment where
    an unauthenticated caller submits an *address* and could learn something
    about it. Everything that knows whether the address exists lives behind
    :func:`reeltalk.social.verify.request_resend`, and the view answers every
    outcome with the same sentence.
    """

    email = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(
            attrs={"autofocus": True, "autocomplete": "email", "dir": "ltr"}
        ),
    )


class ProfileForm(forms.Form):
    """Profile editing (M5): display name, markdown bio, avatar upload.

    The bio is stored rendered (``summary``) with the markdown source kept in
    ``raw_summary`` (R18's pattern) so a later edit pre-fills markdown, not
    markup. Rendering + link-domain filtering happen in the view via
    ``render_markdown`` — one write path for all user content.
    """

    display_name = forms.CharField(max_length=255, required=False)
    summary = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Markdown. Links are kept only for the instance's allowed domains.",
    )
    avatar = forms.ImageField(required=False)
