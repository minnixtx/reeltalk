"""The navigation destinations, defined once (navigation redesign N2/N3).

Before N2 the nav row was hand-written markup in ``base.html``: six anchors,
a logout form, and an ``{% if user.is_authenticated %}`` around the member
items. The redesign's surfaces render these same destinations again as a
mobile bottom bar, a More sheet, and (in N4) a desktop sidebar, so the list
lives here and the header markup lives in ``templates/_navigation.html``.
One definition, many surfaces; N3 and N4 consume this list rather than
copying it.

N3 added the two phone surfaces: ``bottom_nav`` is the bar's four links --
the More button is a control, not a destination, so the template renders it
after the list -- and ``more_nav`` is the sheet's destinations with Log out
last. The mobile labels differ from the desktop row's on purpose (Feed,
Lists); the approved design says so, and the desktop row keeps its own
words until N4 restyles it. The icons are the approved mockup's 24x24
single-path SVG data, held here so a surface renders from data instead of
copying markup per destination.

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
    icon: str = ""  # 24x24 single-path SVG data; only the phone surfaces render it


# The approved N1 mockup's icon set (24x24, stroke-drawn, one path each). The
# shield is the one new drawing: the mockup reused the bell for Moderate as a
# placeholder, and a bell for a moderation queue would misdescribe it here.
ICONS = {
    "feed": "M3 10 12 3l9 7M5 9v12h5v-7h4v7h5V9",
    "search": "M21 21l-6-6M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0",
    "films": "M3 5h18v15H3zM3 10h18M7 5l3 5M14 5l3 5",
    "lists": "M4 4h16v16H4zM8 8h8M8 12h8M8 16h5",
    "find": (
        "M16 21v-3a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v3"
        "M9 10a4 4 0 1 0 0-8 4 4 0 0 0 0 8"
        "M17 3a4 4 0 0 1 0 8M22 21v-3a4 4 0 0 0-3-4"
    ),
    "bell": "M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4",
    "settings": (
        "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8"
        "M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z"
    ),
    "shield": "M12 3l8 3v6c0 4.5-3.2 7.6-8 9-4.8-1.4-8-4.5-8-9V6l8-3z",
    "logout": "M9 3H3v18h6M9 12h12M16 7l5 5-5 5",
}


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


def bottom_nav(request):
    """The phone bottom bar's four links, in order, for this viewer.

    Members only: signed-out visitors get no bottom bar (N1 decision 8).
    The More button follows these in the template -- it opens the sheet
    rather than navigating, so it is a control and not a destination.
    """
    user = request.user
    if not user.is_authenticated:
        return []
    path = request.path
    name = user.localname
    return [
        _item("feed", "Feed", "index", path, icon=ICONS["feed"]),
        _item("search", "Search", "film-search", path, icon=ICONS["search"]),
        _item(
            "films", "My Films", "user-films", path, args=(name,), icon=ICONS["films"]
        ),
        _item("lists", "Lists", "user-lists", path, args=(name,), icon=ICONS["lists"]),
    ]


def more_nav(request):
    """The More sheet's destinations, in the approved order, Log out last.

    The divider above Log out is the template's: it is spacing between
    groups, not a destination. The unread count rides the Notifications row
    from the badge processor's own context value, so the sheet costs no
    second ledger query (the badge's one-query budget stands).
    """
    user = request.user
    if not user.is_authenticated:
        return []
    path = request.path
    items = [
        _item("find", "Find People", "find-user", path, icon=ICONS["find"]),
        _item(
            "notifications",
            "Notifications",
            "notifications",
            path,
            icon=ICONS["bell"],
        ),
        _item(
            "settings",
            "Profile / Settings",
            "profile-edit",
            path,
            icon=ICONS["settings"],
        ),
    ]
    if user.is_moderator:
        items.append(
            _item("moderate", "Moderate", "moderation", path, icon=ICONS["shield"])
        )
    items.append(
        NavItem(
            "logout",
            "Log out",
            reverse("logout"),
            kind="form",
            icon=ICONS["logout"],
        )
    )
    return items


def _item(key, label, url_name, path, args=None, icon=""):
    url = reverse(url_name, args=args or ())
    return NavItem(key=key, label=label, url=url, current=(path == url), icon=icon)


def nav_items(request):
    """Add the nav surfaces as this viewer should see them.

    One processor, three lists: the desktop row, the phone bar, the sheet.
    The phone lists answer ``[]`` signed out, so the anonymous render pays
    nothing it did not pay before.
    """
    return {
        "nav_items": primary_nav(request),
        "bottom_items": bottom_nav(request),
        "more_items": more_nav(request),
    }
