"""Root URLconf."""

import re

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve

from reeltalk.activitypub.identity import PROFILE_LOCALNAME_RE
from reeltalk.core import admin_views as core_admin_views
from reeltalk.lists import views as lists_views
from reeltalk.social import views as social_views
from reeltalk.social.forms import AdminVerificationLoginForm, VerificationAwareLoginForm
from reeltalk.social.password_reset import RESET_PATH
from reeltalk.social.verify import VERIFY_PATH

# Minimal admin site branding (PLAN.md §3.7 v0.1 admin).
admin.site.site_header = "ReelTalk administration"
admin.site.site_title = "ReelTalk admin"
admin.site.index_title = "Site management"

# The admin's own sign-in page gains the same "your email is unverified"
# sentence the public one has, so an unverified admin is not told their
# password is wrong about a password they know is right.
#
# It is NOT the same form object as the public login, and must not become one.
# Django's admin defaults to ``AdminAuthenticationForm``, whose
# ``confirm_login_allowed`` is the only thing enforcing ``is_staff`` at this
# endpoint. Pointing ``login_form`` at the plain ``VerificationAwareLoginForm``
# reads as "same gate, better copy" and in fact deletes that check, letting a
# moderator — never staff, by R100's design — walk into the admin's login
# with a correct password. ``AdminVerificationLoginForm`` keeps that base and
# borrows only the refusal copy. There is a test on exactly this
# (``test_the_admin_login_form_rejects_the_moderators_correct_password``).
admin.site.login_form = AdminVerificationLoginForm

# Email verification (2F-3). Django route patterns are relative and never
# carry a leading slash, so the contract constant is stripped here rather
# than re-typed. Binding the route to ``verify.VERIFY_PATH`` is what stops
# the link in the mail and the page behind it drifting apart: if they ever
# disagree the link 404s loudly instead of quietly landing somewhere wrong.
_VERIFY_PREFIX = re.escape(VERIFY_PATH.lstrip("/"))

# Same treatment for the reset link (2G): the route is bound to
# ``password_reset.RESET_PATH`` rather than to a re-typed string, so the
# link in the mail and the page behind it cannot drift apart.
_RESET_PREFIX = re.escape(RESET_PATH.lstrip("/"))

urlpatterns = [
    path("", social_views.index, name="index"),
    # The home feed's fragment route (§2I increment 3): the same rows the home
    # page renders, without the page around them, for the endless scroll to
    # append. Trailing-slash like every other route here; CommonMiddleware's
    # APPEND_SLASH turns the shorter form into a redirect rather than a 404.
    path("feed/page/", social_views.feed_page, name="feed-page"),
    # The film merge/absorb tool sits under /admin/ but is its own view — the
    # ModelAdmin framework has no multi-object action like this. It must be
    # matched before admin.site.urls swallows the whole /admin/ prefix.
    path(
        "admin/films/merge/",
        core_admin_views.merge_films,
        name="admin-merge-films",
    ),
    path("admin/", admin.site.urls),
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="login.html",
            authentication_form=VerificationAwareLoginForm,
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Email verification (2F-3): the logged-out recovery page and the
    # verify-link endpoint. ``resend`` is matched first so the literal word is
    # never swallowed as somebody's token — the same ordering reason
    # ``invite/create/`` sits above ``invite/<code>/`` below. The token
    # charset is what ``EmailVerificationToken._fresh_code`` emits, so junk in
    # the URL 404s at the router instead of reaching a database lookup.
    #
    # Both are on ``social_views.GATED_EXEMPT_URLS``: they must stay
    # reachable with an unverified account in the database, because they are
    # the only way out of the gate R119 shuts.
    path(
        f"{VERIFY_PATH.lstrip('/')}resend/",
        social_views.verify_resend,
        name="verify-resend",
    ),
    re_path(
        rf"^{_VERIFY_PREFIX}(?P<code>[A-Za-z0-9_-]{{8,64}})/$",
        social_views.verify_link,
        name="verify-link",
    ),
    # Password reset (2G). Same shape and the same reasons as the two above:
    # the request page is a literal bound to ``RESET_PATH`` so the mail and
    # the route cannot drift, and the confirm route takes only the charset
    # ``SingleUseToken._fresh_code`` emits, so junk in the URL 404s at the
    # router instead of reaching a database lookup.
    #
    # The literal sits first for the same reason ``resend`` does above — a
    # stray word must never be swallowed as somebody's token.
    path(
        RESET_PATH.lstrip("/"),
        social_views.password_reset_request,
        name="password-reset",
    ),
    re_path(
        rf"^{_RESET_PREFIX}(?P<code>[A-Za-z0-9_-]{{8,64}})/$",
        social_views.password_reset_confirm,
        name="password-reset-confirm",
    ),
    path("signup/", social_views.signup, name="signup"),
    path("setup/", social_views.setup, name="setup"),
    # Invites (R82): the mint is a POST from the inviter's own profile;
    # the landing page is what the invitee opens. The create route sits
    # first so "create" is never swallowed as someone's code. The charset is
    # what ``Invite.mint`` emits (``secrets.token_urlsafe``), so junk in the
    # URL 404s instead of reaching a database lookup.
    path("invite/create/", social_views.invite_create, name="invite-create"),
    re_path(
        r"^invite/(?P<code>[A-Za-z0-9_-]{8,64})/$",
        social_views.invite_accept,
        name="invite-accept",
    ),
    path("about/", social_views.about, name="about"),
    # Moderation (R101): /moderate/ sits outside Django admin, because a
    # moderator cannot reach admin chrome at all (R100) and the queue could
    # not live inside it.
    path("", include("reeltalk.moderation.urls")),
    # Getting-started page (M6): the core loop for new users; public.
    path("welcome/", social_views.welcome, name="welcome"),
    # The human profile page at the actor URL (M5): Person JSON-LD for AP
    # clients by content negotiation, the profile for browsers. It matches
    # before the activitypub include so it owns the bare /user/<localname>/;
    # the extended pattern also covers mirror localnames (<user>@<netloc>).
    re_path(
        rf"^user/(?P<localname>{PROFILE_LOCALNAME_RE})/$",
        social_views.user_profile,
        name="user-profile",
    ),
    # The films page shares the profile pattern (R40's local charset plus
    # the mirror '@' and ':').
    re_path(
        rf"^user/(?P<localname>{PROFILE_LOCALNAME_RE})/films/",
        social_views.user_films,
        name="user-films",
    ),
    # A member's lists page (L7, R138 decision 1). It deliberately reuses
    # the films route's exact shape — same ``re_path``, same
    # ``PROFILE_LOCALNAME_RE`` — rather than a plain ``path()``. That is not
    # stylistic: the character class is what lets a mirror handle
    # (``user@host``) match, so ``/user/alice@elsewhere.example/lists/``
    # resolves for a remote member instead of 404-ing. A top-level
    # ``/lists/`` was rejected outright — see R138 — because it would be a
    # third URL shape for a per-user page and would duplicate the profile's
    # own Lists tab.
    re_path(
        rf"^user/(?P<localname>{PROFILE_LOCALNAME_RE})/lists/",
        lists_views.user_lists,
        name="user-lists",
    ),
    # A list's canonical page (L10). ``/status/<id>/`` for a LIST status
    # 302s here; the like and reply endpoints stay keyed on the status id,
    # which is the half of L10 that keeps the social machinery unchanged.
    path("list/<int:list_id>/", lists_views.list_detail, name="list-detail"),
    # Authoring (§2K increment 3). ``/lists/new/`` is the only top-level list
    # URL and it is a create form, not an index — R138 turned ``/lists/`` down
    # as a *page*, and this is not one; the plural is simply the collection
    # you are adding to. Everything that acts on a list that exists hangs off
    # ``/list/<id>/``, the same way the like and reply endpoints already do.
    # Order does not matter here: ``<int:list_id>`` cannot swallow ``1/edit/``,
    # so the detail route above still resolves on its own.
    path("lists/new/", lists_views.list_create, name="list-create"),
    path("list/<int:list_id>/edit/", lists_views.list_edit, name="list-edit"),
    path(
        "list/<int:list_id>/add-film/",
        lists_views.list_add_film,
        name="list-add-film",
    ),
    # The typeahead behind the editor's add-film box. Its own route rather
    # than a mode of the add route, because one answers an XHR with JSON and
    # the other performs a write, and folding them together would leave one
    # view having to tell a GET probe from a real submission.
    path(
        "list/<int:list_id>/suggest/",
        lists_views.list_suggest,
        name="list-suggest",
    ),
    path(
        "list/<int:list_id>/remove-film/",
        lists_views.list_remove_film,
        name="list-remove-film",
    ),
    path("list/<int:list_id>/move/", lists_views.list_move, name="list-move"),
    path("list/<int:list_id>/delete/", lists_views.list_delete, name="list-delete"),
    # Saving (§2K increment 5, L2/L6/L12, R140). Two toggle routes rather
    # than one that flips a flag, because the two halves are different writes
    # -- an insert and a delete -- and a single "toggle" endpoint has to work
    # out which one it means from the current state, which is a race with
    # every other tab the member has open. Two named routes make the intent
    # explicit in the URL.
    path("list/<int:list_id>/save/", lists_views.list_save, name="list-save"),
    path(
        "list/<int:list_id>/unsave/",
        lists_views.list_unsave,
        name="list-unsave",
    ),
    # The dismiss for R140 1's deleted-list notice hangs off the same
    # ``/list/<id>/save/`` stem rather than being its own shape: it acts on
    # the same save row, and nesting it says so.
    path(
        "list/<int:list_id>/save/dismiss/",
        lists_views.list_save_dismiss,
        name="list-save-dismiss",
    ),
    # Follow / unfollow a profile (M5 increment 2): POST-only routes sharing
    # the extended pattern so mirror handles (<user>@<netloc>) match too.
    re_path(
        rf"^user/(?P<localname>{PROFILE_LOCALNAME_RE})/follow/$",
        social_views.user_follow,
        name="user-follow",
    ),
    re_path(
        rf"^user/(?P<localname>{PROFILE_LOCALNAME_RE})/unfollow/$",
        social_views.user_unfollow,
        name="user-unfollow",
    ),
    # Block / unblock a profile (M5 increment 3): read-side state — a local
    # M2M change only, no federation delivery (R53). Same extended pattern so
    # mirror handles (<user>@<netloc>) match too.
    re_path(
        rf"^user/(?P<localname>{PROFILE_LOCALNAME_RE})/block/$",
        social_views.user_block,
        name="user-block",
    ),
    re_path(
        rf"^user/(?P<localname>{PROFILE_LOCALNAME_RE})/unblock/$",
        social_views.user_unblock,
        name="user-unblock",
    ),
    # Remote-user discovery (M5 increment 2): user@domain → their profile.
    path("find/", social_views.find_user, name="find-user"),
    path("preferences/profile/", social_views.profile_edit, name="profile-edit"),
    # Notifications (increment 3): the member's ledger page and the
    # mark-all-read POST.
    path("", include("reeltalk.notifications.urls")),
    # Film domain (detail now; create/edit/shelve/finish join in later pieces).
    path("", include("reeltalk.core.urls")),
    # Federation (M4): the actor URL + webfinger/nodeinfo discovery.
    path("", include("reeltalk.activitypub.urls")),
]

# User-uploaded media (posters, avatars) is served by the web process itself —
# this stack has no separate static/media server (PLAN.md §3.8).
urlpatterns += [
    re_path(r"^images/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
