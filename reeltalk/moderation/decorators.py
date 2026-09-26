"""Who may moderate (increment 1, R100/R101).

One definition of the moderator gate, so that every moderation route in this
arc answers the question the same way and there is exactly one place to
audit.

``user_passes_test`` is deliberately NOT used, even though it looks like the
obvious Django primitive for this. Its failure path is a redirect to the
login page *regardless of whether the visitor is already signed in* — so a
signed-in member who merely lacks the flag would be bounced to ``/login/``
and told to authenticate again. R101 requires the opposite: an anonymous
visitor is sent to log in (302), and a member who is already logged in and
simply is not a moderator is refused (403). Those are different answers to
different questions, and conflating them would tell a member their session
is broken when the real answer is "this is not for you".

The two halves are therefore stacked: ``login_required`` for the anonymous
case, an explicit ``PermissionDenied`` for the authenticated-but-not-
moderator case.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def may_moderate(user):
    """The whole moderator rule in one predicate.

    A moderator flag alone is enough; staff and superusers pass because the
    site admin acts on the same ``/moderate/`` queue (R103), not through a
    second surface with a second rule.

    ``is_authenticated`` is tested first and short-circuits: ``AnonymousUser``
    has no ``is_moderator`` attribute, so any reordering here raises
    ``AttributeError`` on every anonymous request rather than refusing it.
    """
    return bool(
        user.is_authenticated
        and (user.is_moderator or user.is_staff or user.is_superuser)
    )


def moderator_required(view_func):
    """Gate a view behind :func:`may_moderate`.

    Anonymous → 302 to the login page. Signed-in non-moderator → 403.
    Moderator, staff or superuser → the view.
    """

    @login_required
    def _guarded(request, *args, **kwargs):
        if not may_moderate(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wraps(view_func)(_guarded)
