"""Test accounts that can actually sign in (2F-3).

Under R119 an unverified account cannot authenticate, and — because the gate
lives in ``user_can_authenticate``, which ``get_user()`` calls on every
request — neither ``client.login()`` nor ``client.force_login()`` gets past
it. A test user with no address is worse than unverified: it can never become
verified, because ``EmailVerificationToken.mint()`` refuses a blank address
and correctly so. So every test that authenticates needs a member that was
actually verified.

**This spends a real token rather than writing the columns.** That is the
whole design of the helper, not ceremony. ``mint().consume()`` is the only
route to the verified state that production has, so a fixture that took a
shortcut would be modelling something no real account is ever in — and it
would mean the suite's "verified" users and the instance's verified users are
two different things, which is precisely the gap where a control looks
enforced and is not. The side effect is a ``used`` token row per verified
member, which is exactly what a real verified member has.

``unverified_member()`` exists for the tests that need the other state, and
naming it makes the choice explicit at every call site rather than leaving
"whatever create_user gives you" to mean two different things on either side
of this increment.
"""

from reeltalk.social.models import EmailVerificationToken, User

PASSWORD = "s3cretpass"


def _localname(args, kwargs) -> str:
    if args:
        return args[0]
    return kwargs.get("localname", "member")


def _with_address(args, kwargs) -> dict:
    """Default the address off the localname so it is unique per test.

    Only a default: a caller that passes its own ``email`` keeps it, because
    several tests need a specific address to assert who the mail went to.
    """
    kwargs.setdefault("email", f"{_localname(args, kwargs)}@example.test")
    return kwargs


def verify(user) -> User:
    """Move ``user`` into the verified state the only way it can be moved."""
    EmailVerificationToken.mint(user).consume()
    user.refresh_from_db()
    return user


def member(*args, **kwargs) -> User:
    """A regular local member who can sign in."""
    return verify(User.objects.create_user(*args, **_with_address(args, kwargs)))


def site_admin(*args, **kwargs) -> User:
    """The site admin, verified and able to reach ``/admin/``.

    R119 admits no exception for ``is_superuser``, so the admin needs a
    verified address like everyone else — a fixture that skipped it would
    quietly test a different rule than the one that ships.
    """
    return verify(User.objects.create_superuser(*args, **_with_address(args, kwargs)))


def moderator(*args, **kwargs) -> User:
    """A moderator with ``is_staff`` so the admin door and the queue both apply."""
    kwargs.setdefault("is_moderator", True)
    kwargs.setdefault("is_staff", True)
    return verify(User.objects.create_user(*args, **_with_address(args, kwargs)))


def unverified_member(*args, **kwargs) -> User:
    """A member that has **not** proven its address, and so cannot sign in.

    The point of naming it: a test that means to exercise the gate says so,
    instead of relying on ``create_user`` happening to leave the account in
    the refused state.
    """
    return User.objects.create_user(*args, **_with_address(args, kwargs))
