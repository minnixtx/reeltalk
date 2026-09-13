"""ActivityPub routes (M4 increments 2-3: identity, discovery, collections).

The collection URLs (inbox/outbox/followers/following, shared inbox) follow
the actor-URL convention fixed in increment 2 (R40) so the Person document's
shape never changes; their handlers land here in increment 3 (R41). The bare
actor URL /user/<localname>/ itself is served by the social app's profile
route (M5) — Person JSON-LD for AP clients, the human profile page for
browsers.
"""

from django.urls import path, re_path

from . import views
from .identity import LOCALNAME_RE

urlpatterns = [
    path(".well-known/webfinger", views.webfinger, name="webfinger"),
    path(".well-known/nodeinfo", views.nodeinfo_index, name="nodeinfo-index"),
    path("nodeinfo/2.0", views.nodeinfo_2_0, name="nodeinfo-2.0"),
    # Collections (R41): outbox/followers/following are read-side
    # OrderedCollections; the per-user inbox accepts delivery POSTs.
    re_path(
        rf"^user/(?P<localname>{LOCALNAME_RE})/outbox/$",
        views.outbox,
        name="ap-outbox",
    ),
    re_path(
        rf"^user/(?P<localname>{LOCALNAME_RE})/followers/$",
        views.followers,
        name="ap-followers",
    ),
    re_path(
        rf"^user/(?P<localname>{LOCALNAME_RE})/following/$",
        views.following,
        name="ap-following",
    ),
    re_path(
        rf"^user/(?P<localname>{LOCALNAME_RE})/inbox/$",
        views.inbox,
        name="ap-inbox",
    ),
    # The instance-wide shared inbox (endpoints.sharedInbox in the Person doc).
    path("inbox/", views.shared_inbox, name="ap-shared-inbox"),
]
