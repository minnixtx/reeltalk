"""The navigation destinations, defined once (navigation redesign N2/N3/N4).

Before N2 the nav row was hand-written markup in ``base.html``: six anchors,
a logout form, and an ``{% if user.is_authenticated %}`` around the member
items. The redesign's surfaces render these same destinations again as a
desktop sidebar, a mobile bottom bar and a More sheet, so the list lives here
and each surface keeps only its own markup. One definition, many surfaces; no
surface copies the list.

N3 added the two phone surfaces: ``bottom_nav`` is the bar's four links --
the More button is a control, not a destination, so the template renders it
after the list -- and ``more_nav`` is the sheet's destinations with Log out
last. N4 replaced the desktop row with ``sidebar_nav``, the wide left menu the
approved design asks for; its labels now agree with the bar's (Feed, Lists)
because one menu speaks at each width instead of two competing ones. Import,
Export and Moderate ride in none of them by design: they live inside Profile /
Settings, which every surface links to. The icons are the approved mockup's
24x24 single-path SVG data, held here so a surface renders from data instead
of copying markup per destination.

The sidebar is the grouped surface: ``group`` names the block an item sits in
and the partial draws a rule wherever the group changes, so the grouping lives
in the definition rather than in template markup that would have to be edited
in step with the list.

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
    """One entry of a nav surface: what it says, where it goes, whether it's lit."""

    key: str
    label: str
    url: str
    current: bool = False
    kind: str = "link"  # "link", or "form" for the POST-only Log out control
    icon: str = ""  # 24x24 single-path SVG data; the menu surfaces render it
    group: str = ""  # sidebar block name; the partial rules between groups


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


def sidebar_nav(request):
    """The desktop menu's destinations, in the approved order, Log out last.

    Members only (N1 decision 8): a signed-out visitor gets no menu, because
    the wordmark is the way home and the corner carries Log in / Sign up.

    Import, Export and Moderate are deliberately absent. The approved design
    nests them inside Profile / Settings, so the menu stays seven rows instead
    of ten and the destinations stay reachable from the one page that acts on
    them. The moderator keeps the corner's own Moderate link (R101), which the
    stylesheet retires below 1024px in favor of the More sheet's row.
    """
    user = request.user
    if not user.is_authenticated:
        return []
    path = request.path
    name = user.localname
    return [
        _item("feed", "Feed", "index", path, icon=ICONS["feed"], group="feed"),
        _item(
            "films",
            "My Films",
            "user-films",
            path,
            args=(name,),
            icon=ICONS["films"],
            group="feed",
        ),
        _item(
            "lists",
            "Lists",
            "user-lists",
            path,
            args=(name,),
            icon=ICONS["lists"],
            group="feed",
        ),
        _item(
            "find",
            "Find People",
            "find-user",
            path,
            icon=ICONS["find"],
            group="discover",
        ),
        _item(
            "notifications",
            "Notifications",
            "notifications",
            path,
            icon=ICONS["bell"],
            group="discover",
        ),
        _item(
            "settings",
            "Profile / Settings",
            "profile-edit",
            path,
            icon=ICONS["settings"],
            group="account",
        ),
        NavItem(
            "logout",
            "Log out",
            reverse("logout"),
            kind="form",
            icon=ICONS["logout"],
            group="session",
        ),
    ]


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


def _item(key, label, url_name, path, args=None, icon="", group=""):
    url = reverse(url_name, args=args or ())
    return NavItem(
        key=key,
        label=label,
        url=url,
        current=(path == url),
        icon=icon,
        group=group,
    )


def nav_items(request):
    """Add the nav surfaces as this viewer should see them.

    One processor, three lists: the desktop menu, the phone bar, the sheet.
    All three answer ``[]`` signed out, so the anonymous render pays nothing
    it did not pay before.
    """
    return {
        "sidebar_items": sidebar_nav(request),
        "bottom_items": bottom_nav(request),
        "more_items": more_nav(request),
    }
