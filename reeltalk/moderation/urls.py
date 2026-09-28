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
    # The one destructive action in this increment. Named for what it does
    # to the *post* rather than for the report, so that when increment 4
    # adds a suspend and increment 5 a ban — both aimed at the target
    # account rather than at a post — the three read as distinct verbs on
    # one card instead of three near-identical "act on this report" routes.
    path(
        "moderate/<int:report_id>/delete-status/",
        views.delete_status,
        name="moderation-delete-status",
    ),
    # Increment 4's heavier verb, aimed at the target *account* — hence a
    # distinct trailing segment rather than a verb on the delete route.
    # Unsuspend is NOT here: by the time a suspension is lifted the queue
    # card that raised it has been drained, so the lift lives on the
    # profile, which is the only surface that still shows the account.
    path(
        "moderate/<int:report_id>/suspend/",
        views.suspend,
        name="moderation-suspend",
    ),
    # Increment 5's heaviest verb, aimed at the same target account. Its
    # own trailing segment for the same reason suspend has one: three
    # distinct verbs on one card read better than three near-identical
    # "act on this report" routes.
    path(
        "moderate/<int:report_id>/ban/",
        views.ban,
        name="moderation-ban",
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
    # Lifting a suspension, from the suspended account's own profile. A
    # moderator route in the member's URL space, because the profile is the
    # only surface that still shows a suspended account once the queue has
    # drained (see ``views.unsuspend``).
    path(
        "user/<str:localname>/unsuspend/",
        views.unsuspend,
        name="user-unsuspend",
    ),
    # Lifting a ban lives under /moderate/, not in the member's URL space,
    # and the asymmetry with unsuspend above is forced rather than chosen.
    # A suspended account keeps a public profile that explains itself, so
    # its lift sits on that page. A banned account has no public page at
    # all — the profile is 410 Gone — so the only surface that still shows
    # it is the moderator's. See ``views.unban``.
    path(
        "moderate/<str:localname>/unban/",
        views.unban,
        name="moderation-unban",
    ),
]
