"""The sign-in gate (R119): an address that has not been proven cannot open an account.

The gate lives **here** rather than in ``User.is_active`` for the reason R102b
pins: ``is_active`` means *not suspended and not banned*, and it has tests
written specifically to stop a third state being folded into it. Verification
is unrelated to both — a suspended account and an unverified one are different
problems with different fixes, and merging them would make three refusals
indistinguishable to the person on the other side of the door.

``ModelBackend.user_can_authenticate`` is the idiomatic seam, and because
``get_user()`` calls it on **every** request rather than only at login, refusing
here buys the same property R102b got for suspension for free: **an
already-logged-in session is cut on its next click.** The consequence is
stated rather than discovered later — if the site admin changes a signed-in
member's address, that member is logged out at their next click until they
verify the new one. That is the consistent behaviour the derived
``User.email_verified`` produces, and the weaker alternative (gate at
``authenticate()`` only, so live sessions ride on untouched) was not taken.

**No exceptions, and the admin is not one.** R119 was offered an
``is_superuser`` exemption on R114's reasoning and the owner declined it. What
makes that safe rather than a brick is R122: the logged-out resend route works
without a session, so an admin who cannot get in can still send the link to
themselves. The residual failure is "SMTP is fundamentally not working", which
is a deployment fault that shouts via ``console_backend_active()`` rather than
failing silently. If a future change ever removes or breaks that resend route,
it re-creates the lockout-with-no-exit R114 deleted and must not be made
without the owner.

The second method here, :meth:`EmailVerificationBackend.why_refused`, exists
because the gate makes one honest question impossible to answer from
``authenticate()``'s return value alone. ``authenticate()`` answers *no* to
"wrong password" and "right password, not verified" identically, and under
R119 Django's stock copy — *"Please enter a correct username and password"* —
is actively harmful: it sends a person to a password reset that does not exist
yet (R121 keeps it out of scope) instead of to the resend route that does. So
the login form needs the reason, and the reason has to come from the one place
that knows the gate. It returns a **string and never a user object**, so
nothing reached through this diagnostic can be logged in by it.
"""

from django.contrib.auth import (
    check_password_with_timing_attack_mitigation,
    get_user_model,
)
from django.contrib.auth.backends import ModelBackend
from django.views.decorators.debug import sensitive_variables

# The refusals :meth:`EmailVerificationBackend.why_refused` can name. Strings
# rather than exceptions so the caller renders a message instead of catching a
# type, and so an unrecognised value cannot be silently read as "allowed".
REFUSAL_CREDENTIALS = "bad-credentials"
REFUSAL_INACTIVE = "inactive"
REFUSAL_UNVERIFIED = "unverified"


class EmailVerificationBackend(ModelBackend):
    """``ModelBackend`` plus R119's one extra term."""

    def user_can_authenticate(self, user):
        """Admit only an account whose own address has actually been proven.

        ``super()`` is ``getattr(user, "is_active", True)``, which on this
        model is the derived suspension-and-ban property. Both terms are
        needed and neither covers the other: dropping the ``super()`` call
        would let a banned account sign in, and dropping the verification term
        would un-gate the whole increment while leaving every other line of
        this class looking correct.
        """
        if not super().user_can_authenticate(user):
            return False
        return bool(getattr(user, "email_verified", False))

    @sensitive_variables("password")
    def why_refused(self, username, password) -> str | None:
        """Why these credentials will not open the door — or ``None``."""
        return self.diagnose(username, password)[0]

    @sensitive_variables("password")
    def diagnose(self, username, password) -> tuple[str | None, object | None]:
        """The refusal *and* the account that hit it.

        A mirror of ``ModelBackend.authenticate`` that returns the *reason*
        instead of the user. It keeps Django's own timing mitigation rather
        than writing a second password check, because a diagnostic that is
        faster for a non-existent account than for an existing one is an
        account-enumeration channel handed to anyone who can time a request —
        and that is a different leak from the one R119 deliberately accepts.

        **What the accepted leak actually is.** ``REFUSAL_UNVERIFIED`` is only
        reachable after the password has already been checked and passed, so
        learning that an account is unverified requires knowing its password.
        That is a far weaker position than address enumeration, and the UX cost
        of hiding it — a member sent to a password reset that does not exist —
        is worse than the disclosure.

        ``REFUSAL_CREDENTIALS`` deliberately covers both "no such account"
        and "wrong password" with one value. They must not be distinguished.

        The reason alone is not enough for every caller. The admin's login
        form may only name the verification problem for an account that
        could otherwise have reached the admin, so it needs to inspect the
        row as well as the verdict. Returning the user here is safe in a way
        that returning it from a login path would not be: this method cannot
        log anyone in, and nothing in the sign-in flow consumes its second
        value as an identity.
        """
        user_model = get_user_model()
        try:
            user = user_model._default_manager.get_by_natural_key(username)
        except user_model.DoesNotExist:
            user = None

        if not check_password_with_timing_attack_mitigation(user, password):
            return REFUSAL_CREDENTIALS, None
        # Suspension and ban are read through Django's own term so this stays
        # in step with whatever ``is_active`` means today.
        if not super().user_can_authenticate(user):
            return REFUSAL_INACTIVE, user
        if not user.email_verified:
            return REFUSAL_UNVERIFIED, user
        return None, user
