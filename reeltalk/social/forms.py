"""Account forms: signup, the first-run setup wizard, profile editing, sign-in."""

import re

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import authenticate
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables

from reeltalk.proxy_trust import client_ip
from reeltalk.social import attempts

from .backends import REFUSAL_INACTIVE, REFUSAL_UNVERIFIED, EmailVerificationBackend
from .models import CredentialAttempt, User

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

    # Whether this submission reached a name/email uniqueness check — the
    # enumeration surface the signup throttle bounds. Set the moment either
    # ``clean_localname`` or ``clean_email`` gets far enough to run its
    # ``User.objects.filter(...)`` lookup, and read by the view to decide
    # whether to count the attempt. A submission that never reaches a
    # lookup (empty, or a malformed name that fails the format regex before
    # the query) does not set it and is not counted — it learned nothing
    # about which names exist. Defaults False so a form that never ran
    # either clean method reads as "no attempt to count".
    reached_uniqueness_check = False

    def clean_localname(self):
        localname = self.cleaned_data["localname"].strip()
        if not LOCALNAME_RE.match(localname):
            raise ValidationError(
                "Use 1-30 characters: letters, numbers, '.', '_' or '-', "
                "starting with a letter or number."
            )
        # Past the format check, the lookup below is the oracle: its answer
        # ("taken" / "free") is a fact about the account set. Count it.
        self.reached_uniqueness_check = True
        # Case-insensitive uniqueness: "Alice" and "alice" would be the same
        # federated identity to other instances.
        if User.objects.filter(localname__iexact=localname).exists():
            raise ValidationError("That name is already taken.")
        return localname

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        # Same: this lookup answers whether an address has an account, so
        # reaching it is a counted attempt whether or not it finds one.
        self.reached_uniqueness_check = True
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

    # Which credential surface this form throttles against. Set on each
    # concrete form below and deliberately left ``None`` here: a login form
    # that forgets to name its surface raises in ``_budget`` rather than
    # running silently unthrottled, so the half-finished "two of three
    # surfaces covered" state cannot come back unnoticed.
    throttle_surface = None

    @sensitive_variables()
    def clean(self):
        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")
        if username is None or not password:
            return self.cleaned_data

        # Refuse before touching the password once this address is spent.
        # There is no work worth doing on a blocked address, and skipping
        # the credential check denies an attacker the timing difference a
        # real password verification makes. The address is always
        # ``client_ip()``, never a header read here.
        ip = client_ip(self.request) if self.request is not None else ""
        if attempts.attempts_blocked(self.throttle_surface, ip):
            raise ValidationError(
                attempts.reveal_message(attempts.retry_at(self.throttle_surface, ip)),
                code="throttled",
            )

        self.user_cache = authenticate(
            self.request, username=username, password=password
        )
        if self.user_cache is not None:
            # ``confirm_login_allowed`` is where the subclass's own requirement
            # lives, so it runs here rather than being reimplemented.
            self.confirm_login_allowed(self.user_cache)
            # A correct credential that actually got through resets the
            # address's budget, so a person who knows their password is
            # never punished for the failures that came before it. Cleared
            # only after ``confirm_login_allowed`` passes: a correct password
            # on an account this door still refuses (a non-staff user at the
            # admin) is not a success to reward with a fresh budget.
            attempts.clear_attempts(self.throttle_surface, ip)
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
        # Only a wrong password (or no such account) is counted against the
        # budget. An unverified or suspended account got its password right,
        # so it is not a guess — counting it would let someone testing their
        # own not-yet-verified account drain their address's budget for a
        # reason unrelated to guessing at anything.
        attempts.record_attempt(self.throttle_surface, ip)
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
    throttle_surface = CredentialAttempt.LOGIN


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
    throttle_surface = CredentialAttempt.ADMIN

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


class NewPasswordPairMixin:
    """The validation behind any "type the new password twice" control.

    Shared by the admin's user form and the self-service reset confirm
    because the two must not disagree about what a usable password is, and
    because the pair check — the thing that stops a typo locking somebody
    out of their own account — is exactly the kind of rule that gets fixed
    in one place and forgotten in the other.

    Both fields treat an **empty value as "no change"**, which is what the
    admin needs (edit a profile without re-typing a password) and is never
    wrong for the reset flow either, because that flow's view requires the
    fields and would have failed validation before this ran. The writer
    downstream is the thing that decides whether an empty value is legal.
    """

    def clean_password1(self):
        password = self.cleaned_data.get("password1") or ""
        if not password:
            return ""
        try:
            validate_password(password)
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages)
        return password

    @sensitive_variables("password1", "password2")
    def clean(self):
        cleaned = super().clean()
        first = cleaned.get("password1") or ""
        second = cleaned.get("password2") or ""
        if first != second:
            # Named on the confirmation field rather than as a non-field
            # error, so it lands beside the box that is actually wrong
            # instead of floating above the form.
            self.add_error("password2", "The two passwords did not match.")
        return cleaned


class PasswordResetRequestForm(forms.Form):
    """The logged-out "I forgot my password" form (2G).

    Thin on purpose, for the same reason :class:`ResendVerificationForm`
    is: it checks that something address-shaped was typed and nothing else.
    **It must not report whether the address is registered, verified, or
    refused** — not in an error, not in a delay, not in different copy.
    Everything that knows any of that lives behind
    :func:`reeltalk.social.password_reset.request_reset`, and the view
    answers every outcome with the same sentence.
    """

    email = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(
            attrs={"autofocus": True, "autocomplete": "email", "dir": "ltr"}
        ),
    )


class PasswordResetConfirmForm(NewPasswordPairMixin, forms.Form):
    """The new-password form opened from a reset link (2G).

    A POST rather than a click-through: the person types a password, so
    the credential that authorises the change arrives with the change
    rather than sitting in a URL that a browser history, a proxy log or a
    mail scanner has already seen. The link in the mail only *opens* this
    page; spending it takes the form.
    """

    password1 = forms.CharField(
        label="New password",
        required=True,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirm new password",
        required=True,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
