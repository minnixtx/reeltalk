"""Routes for the moderation surface (R101).

``/moderate/`` sits outside Django admin on purpose — the merge-tool
precedent under ``/admin/films/merge/`` would have put the report queue in
admin chrome, and a moderator cannot reach admin chrome at all (R100).
"""

from django.urls import path

from . import views

urlpatterns = [
    path("moderate/", views.index, name="moderation"),
]
