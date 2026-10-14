"""Explicit signup-policy helpers for tests that drive the open door.

A new instance ships invite-only, so the ambient default no longer admits a
signup. A test that wants the open path has to open it by name, for the same
reason ``members.py`` names ``member`` / ``unverified_member`` instead of
leaving the verified state to whatever ``create_user`` happened to produce:
a test that passes because of an unstated default is not testing the thing
its name says it tests.

``open_signup()`` is the only helper here on purpose. The closed state is
the default, so tests get it for free and the existing
``site.signup_policy = SiteSettings.INVITE`` lines already state it where
they mean it.
"""

from reeltalk.social.models import SiteSettings


def open_signup() -> SiteSettings:
    """Open this instance for public signup and return the settings row."""
    site = SiteSettings.get_instance()
    site.signup_policy = SiteSettings.OPEN
    site.save()
    return site
