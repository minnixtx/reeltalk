"""Who may moderate, and who may be acted on (R100/R101/R103).

One definition of each half of the permission model, so that every
moderation route in this arc answers both questions the same way and there
is exactly one place to audit. ``may_moderate`` is the gate on the surface;
``can_act_on`` is the gate on the *target* of a destructive action. They
are separate questions and are deliberately not collapsed into one
predicate: passing the first says you may open the queue, and says nothing
about whether the thing in front of you is yours to destroy.

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

from reeltalk.moderation.representative import is_instance_actor


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


def can_act_on(actor, target) -> bool:
    """R103, as amended by the owner on 2026-09-27 (R103b).

    This instance has three kinds of account and no others:

    * **the admin** — ``is_superuser``.
    * **a moderator** — ``is_moderator``.
    * **a regular user** — neither.

    ``is_staff`` is not a fourth kind. It is Django's door to ``/admin/``,
    and R100 keeps a moderator's copy of it ``False`` precisely so a
    moderator never reaches admin chrome. What it does decide here is which
    side of the line an account sits on: an account holding the admin door
    is on the admin's side, so a moderator never touches it. That is the
    narrowest reading of "only a regular user", and it needs no new role to
    express.

    The rule reads the **actor**, not the surface they are standing on, so
    one ``/moderate/`` page serves both the admin and a moderator without
    a second page carrying a second rule. Hiding the control in a template
    is not the guarantee; this is what the action itself calls.

    * **The admin** may act on anyone, including another moderator and
      including themselves. Checked *before* every shield, so the ordering
      here is load-bearing and not incidental.
    * **A moderator** may act only on a **regular user**, and not on
      themselves. Never on the admin — that is the escalation path R100
      exists to keep absent rather than merely checked. Never on another
      moderator — peers do not moderate peers.

    The self check is **defense-in-depth, not a load-bearing clause.** Any
    actor who reaches it already holds at least one of the three flags in
    the shield below, so the shield refuses them anyway; deleting the self
    check turns no test red, and that is the honest description rather than
    a claim of necessity. It stays because ``may_moderate`` and this
    function are edited independently, and a future change to what a
    moderator carries should not silently let one delete their own post.

    The same rule covers every destructive action in this arc, content or
    account: deleting a post, suspending, banning. A moderator's reach is
    "regular users who are not me", and it does not widen for the heavier
    actions — if anything the heavier ones are where the line matters more.

    **One exception, and it narrows the admin half rather than the
    moderator half: nobody may act on the instance representative.** R103b
    enumerates three kinds of *person*, and the representative is not one —
    it is the instance's signing identity, a row that exists so the server
    can make statements at all. Suspending it takes the ``Flag`` wire down;
    banning it goes further, because the ban 410s ``/user/_instance/`` and
    a peer that can no longer fetch that Person document can no longer
    verify anything we sign with that key. Every future report we forward
    would then fail at the door of every instance that had already accepted
    us, quietly and forever. That is not a moderation target, it is the
    moderation system, and the guard belongs in the one function every
    destructive action passes through rather than in each caller's memory.

    It is placed **before** the superuser short-circuit deliberately, so it
    binds the site admin too. The raw override is still available in
    Django's own admin, which does not consult this function — an admin
    who truly means it can still reach the row, and has to go somewhere
    else to say so.
    """
    if not actor.is_authenticated or target is None:
        return False
    if is_instance_actor(target):
        return False
    if actor.is_superuser:
        return True
    if not may_moderate(actor):
        return False
    if actor.pk == target.pk:
        return False
    return not (target.is_superuser or target.is_moderator or target.is_staff)


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
