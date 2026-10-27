"""The main nav row's destinations, defined once (navigation redesign N2).

Before N2 the nav row was hand-written markup in ``base.html``: six anchors,
a logout form, and an ``{% if user.is_authenticated %}`` around the member
items. The redesign's later increments render these same destinations again
as a mobile bottom bar, a More sheet, and a desktop sidebar, so the list
lives here and the header markup lives in ``templates/_navigation.html``.
One definition, many surfaces; N3 and N4 consume this list rather than
copying it.

The active rule is exact path equality, unchanged from the markup it
replaces. A list's own page (``/list/<id>/``) deliberately does not light
"My Lists" (R138), and prefix matching would light it wrongly, so equality
stays even where a prefix rule would read more helpful.

Log out is a destination of a different kind: it acts rather than
navigates, so it ships as a POST form with CSRF and never as a GET link.
It rides the same list with ``kind="form"`` so every surface renders the
one real control instead of inventing a link to a POST-only route.

The processor adds no query. ``reverse`` is in-memory, so carrying the list
to every render costs a handful of string joins -- nothing like the badge
processor's indexed COUNT, which is why that one guards on authentication
and this one need not.
"""

from dataclasses import dataclass

from django.urls import reverse


@dataclass(frozen=True)
class NavItem:
    """One entry of the nav row: what it says, where it goes, whether it's lit."""

    key: str
    label: str
    url: str
    current: bool = False
    kind: str = "link"  # "link", or "form" for the POST-only Log out control


def primary_nav(request):
    """The destinations in the main nav row, in order, for this viewer."""
    path = request.path
    items = [_item("home", "Home", "index", path)]
    user = request.user
    if user.is_authenticated:
        name = user.localname
        items += [
            _item("films", "My Films", "user-films", path, args=(name,)),
            _item("lists", "My Lists", "user-lists", path, args=(name,)),
            _item("find", "Find People", "find-user", path),
            _item("import", "Import", "import-films", path),
            _item("export", "Export", "export-films", path),
            NavItem("logout", "Log out", reverse("logout"), kind="form"),
        ]
    return items


def _item(key, label, url_name, path, args=None):
    url = reverse(url_name, args=args or ())
    return NavItem(key=key, label=label, url=url, current=(path == url))


def nav_items(request):
    """Add ``nav_items``: the main nav row as this viewer should see it."""
    return {"nav_items": primary_nav(request)}
