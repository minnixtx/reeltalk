"""ActivityPub routes (M4 increment 2: identity + discovery, R40).

The collection routes (inbox/outbox/followers/following, shared inbox) join
in increments 3-4; the URL convention is fixed now so the Person document's
shape never changes.
"""

from django.urls import path, re_path

from . import views
from .identity import LOCALNAME_RE

urlpatterns = [
    path(".well-known/webfinger", views.webfinger, name="webfinger"),
    path(".well-known/nodeinfo", views.nodeinfo_index, name="nodeinfo-index"),
    path("nodeinfo/2.0", views.nodeinfo_2_0, name="nodeinfo-2.0"),
    # The actor URL (R40). The films page keeps its own more specific route
    # in reeltalk/urls.py; this matches only the bare /user/<localname>/.
    re_path(
        rf"^user/(?P<localname>{LOCALNAME_RE})/$",
        views.actor,
        name="actor",
    ),
]
