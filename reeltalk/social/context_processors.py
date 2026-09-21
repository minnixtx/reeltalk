"""Tell every template whether this instance will actually take a new account.

``site`` reaches templates only where a view remembers to pass it, so the
signup call-to-action on the four anonymous surfaces -- the header, the
landing block on ``/``, the getting-started page and the login page -- was
written as a hard link and stayed policy-blind. An instance set to
``invite`` kept offering "Sign up" on every one of them, and the offer led
to ``signup.html``'s "not accepting new signups" notice. This processor
makes the policy visible everywhere so those CTAs can disappear with it.

Only the boolean is exposed, not the ``SiteSettings`` row: the CTAs need
one bit, and handing a template the settings object invites it to render
whatever the view never approved. The value is derived exactly as the
signup view gates it and as nodeinfo already derives ``openRegistration``,
so the three cannot drift.

The first-run wizard needs no special case here. A fresh instance defaults
to ``open``, so the CTA still shows before any admin exists and still
lands the operator on ``/setup/`` the way it always did.
"""

from reeltalk.social.models import SiteSettings


def signup_open(request):
    """Add ``signup_open``: True unless the instance is invite-only."""
    return {
        "signup_open": SiteSettings.get_instance().signup_policy == SiteSettings.OPEN
    }
