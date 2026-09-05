"""Root URLconf. Real routes land with M1 (PLAN.md §5)."""

from django.contrib import admin
from django.http import HttpResponse
from django.urls import path


def index(request):
    return HttpResponse("ReelTalk — AGPLv3 rewrite in progress. See PLAN.md.")


urlpatterns = [
    path("", index),
    path("admin/", admin.site.urls),
]
