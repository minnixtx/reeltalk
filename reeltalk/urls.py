"""Root URLconf. Real routes land with M1 (PLAN.md §5)."""

from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, re_path
from django.views.static import serve


def index(request):
    return HttpResponse("ReelTalk — AGPLv3 rewrite in progress. See PLAN.md.")


urlpatterns = [
    path("", index),
    path("admin/", admin.site.urls),
]

# User-uploaded media (posters, avatars) is served by the web process itself —
# this stack has no separate static/media server (PLAN.md §3.8).
urlpatterns += [
    re_path(r"^images/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
