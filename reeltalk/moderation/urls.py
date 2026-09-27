"""Routes for the moderation surface (R101) and the report controls.

``/moderate/`` sits outside Django admin on purpose — the merge-tool
precedent under ``/admin/films/merge/`` would have put the report queue in
admin chrome, and a moderator cannot reach admin chrome at all (R100).

The two **report** routes deliberately do not live under ``/moderate/``.
They are member actions, not moderator actions, and a member has no reason
to be introduced to the moderation URL just to press "Report". They sit on
the thing being reported — the post's URL and the profile's URL — the same
shape as ``/status/<id>/like/`` and ``/user/<name>/block/``. The views
still live in this app so all report-writing code stays in one place.

No pattern here collides with an existing one: each carries a trailing
segment the neighbouring patterns do not accept, so the include order in
the root urlconf does not decide anything.
"""

from django.urls import path

from . import views

urlpatterns = [
    path("moderate/", views.index, name="moderation"),
    path(
        "moderate/<int:report_id>/dismiss/",
        views.dismiss,
        name="moderation-dismiss",
    ),
    # Members filing a report, from the reported object's own page.
    path(
        "status/<int:status_id>/report/",
        views.report_status,
        name="report-status",
    ),
    path(
        "user/<str:localname>/report/",
        views.report_user,
        name="report-user",
    ),
]
