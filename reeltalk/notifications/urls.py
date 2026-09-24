"""URLs for the notifications app (increment 3).

Both routes are members-only. ``read/`` sits under the page's own prefix so
the control reads as an action on this ledger rather than a global
endpoint, and it is a POST path precisely because it is not a page.
"""

from django.urls import path

from . import views

urlpatterns = [
    path("notifications/", views.notifications, name="notifications"),
    # Mark-all-read: a mutation, so a POST with CSRF and never a GET on the
    # page itself.
    path(
        "notifications/read/",
        views.mark_read,
        name="notifications-mark-read",
    ),
]
