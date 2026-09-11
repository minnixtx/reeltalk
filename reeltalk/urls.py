"""Root URLconf."""

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve

from reeltalk.activitypub.identity import LOCALNAME_RE
from reeltalk.core import admin_views as core_admin_views
from reeltalk.social import views as social_views

# Minimal admin site branding (PLAN.md §3.7 v0.1 admin).
admin.site.site_header = "ReelTalk administration"
admin.site.site_title = "ReelTalk admin"
admin.site.index_title = "Site management"

urlpatterns = [
    path("", social_views.index, name="index"),
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
        auth_views.LoginView.as_view(template_name="login.html"),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("signup/", social_views.signup, name="signup"),
    path("setup/", social_views.setup, name="setup"),
    path("about/", social_views.about, name="about"),
    # Shares the actor route's localname pattern (R40) — the old ``<str>``
    # converter rejected dots, which are legal localnames (R12).
    re_path(
        rf"^user/(?P<localname>{LOCALNAME_RE})/films/",
        social_views.user_films,
        name="user-films",
    ),
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
