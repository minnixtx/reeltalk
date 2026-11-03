"""The moderator role and its gate (moderation arc increment 1, R100/R101).

This increment exists to prove a security boundary before anything is built
on top of it, so every test here is written against the response a real
request actually got, not against the flag that is supposed to produce it.

The distinction matters most for the admin half. ``user.is_staff is False``
is a statement about a column; whether ``/admin/`` refuses a moderator is
a statement about the site, and the two can disagree. The real refusal was
measured, not assumed: a moderator's ``GET /admin/`` is a **302 to
``/admin/login/?next=/admin/``** — no template rendered at all — and the
admin's own authentication form **rejects the moderator's correct
password** without creating a session. A test that asserted "not 200" or
"redirects to the login page" would be satisfied by several different
wrong behaviors; these assert the specific shape.

Ordered by how each failure would bite:

* A moderator opens ``/moderate/`` and cannot open the admin.
* A member is refused with 403; an anonymous visitor is sent to log in.
* The grant is made through the real admin form and does not widen to
  staff - and it refuses staff even when ticked (R158, owner 2026-11-03:
  ``is_staff`` is Django's door to ``/admin/`` and a moderator does not
  hold it).
* A moderator cannot flip the flag — on someone else, or on themselves.
* The menus carry the staff rows per the moderation arc's D1/D2: Moderate
  for anyone the queue's own predicate admits -- a moderator, a staff
  account, a superuser -- in both the desktop menu and the More sheet, and
  Administration for ``is_staff`` alone; a member sees neither row anywhere.
* The surfaces M1 retired stay retired: the settings page lists no Moderate
  row, the header corner carries none, and no CSS names the old corner parts.
"""

import re

import pytest
from django.contrib.staticfiles import finders
from django.test import Client
from django.urls import reverse

from reeltalk.tests.members import member as create_member
from reeltalk.tests.members import site_admin as create_site_admin

PASSWORD = "s3cretpass"
MODERATE_URL = reverse("moderation")
ADMIN_URL = reverse("admin:index")
ADMIN_LOGIN_URL = reverse("admin:login")
ADMIN_USER_CHANGELIST = "/admin/social/user/"
ADMIN_LOGIN_TEMPLATE = "admin/login.html"
ADMIN_INDEX_TEMPLATE = "admin/index.html"

SETTINGS_PAGE = reverse("profile-edit")
# A page that renders for any signed-in member with no gate of its own, so a
# menu assertion on it cannot be passing on a redirect. (``/`` is the
# wrong choice: the first-run wizard sends it to /setup/ until a superuser
# exists.)
HEADER_PAGE = reverse("find-user")


def rendered_templates(response):
    return [t.name for t in response.templates if t.name]


def logged_in_client(user):
    client = Client()
    client.force_login(user)
    return client


def _site_nav(client, path):
    """The desktop menu's own landmark, scoped like ``test_navigation``."""
    body = client.get(path).content.decode()
    match = re.search(r'<nav class="site-nav".*?</nav>', body, re.S)
    assert match, "no .site-nav menu rendered"
    return match.group(0)


def _more_sheet(client, path):
    body = client.get(path).content.decode()
    match = re.search(r'<dialog id="more-sheet".*?</dialog>', body, re.S)
    assert match, "no More sheet rendered"
    return match.group(0)


@pytest.fixture
def site_admin(db):
    return create_site_admin(localname="root", password=PASSWORD)


@pytest.fixture
def staff(db):
    """Staff without the moderator flag — the admin side of the same surface."""
    return create_member(localname="helper", password=PASSWORD, is_staff=True)


@pytest.fixture
def moderator(db):
    """Flagged to moderate, and pointedly NOT staff (R100, reaffirmed by
    R158: staff is the /admin/ door and a moderator does not hold it)."""
    return create_member(localname="mod", password=PASSWORD, is_moderator=True)


@pytest.fixture
def member(db):
    return create_member(localname="member", password=PASSWORD)


def change_payload(user, **overrides):
    data = {
        "display_name": user.display_name or user.localname,
        "email": user.email or "someone@example.com",
        "is_staff": "",
        "is_superuser": "",
        "is_moderator": "",
    }
    data.update(overrides)
    return data


# --- the gate itself ------------------------------------------------------


def test_moderator_opens_the_moderation_page(moderator):
    resp = logged_in_client(moderator).get(MODERATE_URL)
    assert resp.status_code == 200
    assert "Moderation" in resp.content.decode()


def test_member_is_refused_with_403_not_a_login_redirect(member):
    # 403, not 302: this visitor is already signed in, and the honest
    # answer is "not for you" rather than "sign in again".
    resp = logged_in_client(member).get(MODERATE_URL)
    assert resp.status_code == 403


def test_anonymous_is_sent_to_log_in():
    resp = Client().get(MODERATE_URL)
    assert resp.status_code == 302
    assert reverse("login") in resp.headers["Location"]


def test_staff_and_superuser_pass_the_gate(site_admin, staff):
    # R103: the site admin acts on the same /moderate/ queue rather than a
    # second surface with a second rule, so both pass.
    assert logged_in_client(site_admin).get(MODERATE_URL).status_code == 200
    assert logged_in_client(staff).get(MODERATE_URL).status_code == 200


def test_the_moderator_flag_alone_does_not_grant_staff(moderator):
    # The decoupling R100 is built on, reaffirmed by R158: staff is the
    # /admin/ door, and moderating never carries it.
    assert moderator.is_moderator is True
    assert moderator.is_staff is False
    assert moderator.is_superuser is False


# --- the admin refusal, driven through the real admin view ----------------


def test_moderator_is_turned_away_from_the_admin(moderator):
    client = logged_in_client(moderator)
    # The session is live — proved on the same client before the admin call,
    # so the refusal below cannot be an anonymous-request artifact.
    assert client.get(MODERATE_URL).status_code == 200

    resp = client.get(ADMIN_URL)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    # Sent to the ADMIN's own login page, not the member login. The
    # distinction matters: bouncing a moderator to /login/ would imply a
    # session problem, and re-logging in would never fix it.
    assert location.startswith(ADMIN_LOGIN_URL)
    assert "next=/admin" in location
    # Nothing of the admin rendered on the refused response.
    assert ADMIN_INDEX_TEMPLATE not in rendered_templates(resp)


def test_following_the_redirect_never_reaches_the_admin_index(moderator):
    resp = logged_in_client(moderator).get(ADMIN_URL, follow=True)
    names = rendered_templates(resp)
    assert ADMIN_LOGIN_TEMPLATE in names
    assert ADMIN_INDEX_TEMPLATE not in names


def test_the_admin_login_form_rejects_the_moderators_correct_password(moderator):
    # The strongest form of the guarantee: the moderator's real credentials,
    # offered to the admin's real login form, do not open it.
    client = Client()
    resp = client.post(
        ADMIN_LOGIN_URL,
        {"username": "mod", "password": PASSWORD, "next": "/admin/"},
    )
    assert resp.status_code == 200  # re-rendered with the error
    assert "_auth_user_id" not in client.session
    # And the admin is still closed afterwards.
    assert client.get(ADMIN_URL).status_code == 302
    assert ADMIN_INDEX_TEMPLATE not in rendered_templates(client.get(ADMIN_URL))


def test_moderator_cannot_open_the_user_changelist(moderator, member):
    resp = logged_in_client(moderator).get(ADMIN_USER_CHANGELIST)
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith(ADMIN_LOGIN_URL)
    # No row data leaks into the page they were refused.
    assert member.localname not in resp.content.decode()


def test_staff_control_proves_the_admin_assertions_can_reach_an_index(staff):
    # Without this, every admin assertion above could be passing because no
    # test can reach an admin index at all.
    resp = logged_in_client(staff).get(ADMIN_URL)
    assert resp.status_code == 200
    assert ADMIN_INDEX_TEMPLATE in rendered_templates(resp)


def test_moderator_cannot_grant_moderation_through_the_admin_change_form(
    moderator, member
):
    logged_in_client(moderator).post(
        f"/admin/social/user/{member.pk}/change/",
        change_payload(member, is_moderator="on", is_staff="on", is_superuser="on"),
    )
    member.refresh_from_db()
    assert member.is_moderator is False
    assert member.is_staff is False
    assert member.is_superuser is False


def test_moderator_cannot_grant_themselves_staff_through_the_admin(moderator):
    logged_in_client(moderator).post(
        f"/admin/social/user/{moderator.pk}/change/",
        change_payload(moderator, is_staff="on", is_superuser="on"),
    )
    moderator.refresh_from_db()
    assert moderator.is_staff is False
    assert moderator.is_superuser is False
    # Still not able to moderate-escalate either: the flag they already hold
    # did not become a way to change anyone's roles.
    assert moderator.is_moderator is True


# --- the grant, made through the real admin form --------------------------


def test_a_staff_session_grants_the_flag_through_the_real_admin_form(
    site_admin, member
):
    resp = logged_in_client(site_admin).post(
        f"/admin/social/user/{member.pk}/change/",
        change_payload(member, is_moderator="on"),
    )
    assert resp.status_code == 302
    member.refresh_from_db()
    assert member.is_moderator is True
    # Granting moderation did not hand over the admin (R158, owner
    # 2026-11-03: is_staff is Django's door to /admin/ and a moderator
    # does not hold it).
    assert member.is_staff is False
    # And the grant never reaches superuser from here either: only an
    # account that already holds it can hand it out (F13).
    assert member.is_superuser is False


def test_a_grant_that_ticks_staff_beside_the_flag_refuses_it(site_admin, member):
    # The R158 rule is composed, not hoped for: even a form post that
    # ticks staff beside the moderator flag lands without it, because a
    # moderator who is not the site admin must stay out of /admin/.
    resp = logged_in_client(site_admin).post(
        f"/admin/social/user/{member.pk}/change/",
        change_payload(member, is_moderator="on", is_staff="on"),
    )
    assert resp.status_code == 302
    member.refresh_from_db()
    assert member.is_moderator is True
    assert member.is_staff is False


def test_a_superuser_grant_lands_all_three_flags(site_admin, member):
    # The other half of R158: the site admin carries staff, moderator and
    # superuser, so promoting a member to admin composes the whole shape.
    resp = logged_in_client(site_admin).post(
        f"/admin/social/user/{member.pk}/change/",
        change_payload(member, is_superuser="on"),
    )
    assert resp.status_code == 302
    member.refresh_from_db()
    assert member.is_superuser is True
    assert member.is_staff is True
    assert member.is_moderator is True


def test_the_granted_member_moderates_and_still_cannot_open_admin(site_admin, member):
    logged_in_client(site_admin).post(
        f"/admin/social/user/{member.pk}/change/",
        change_payload(member, is_moderator="on"),
    )
    client = logged_in_client(member)
    assert client.get(MODERATE_URL).status_code == 200
    assert client.get(ADMIN_URL).status_code == 302
    assert ADMIN_INDEX_TEMPLATE not in rendered_templates(
        client.get(ADMIN_URL, follow=True)
    )


def test_the_flag_is_revocable_through_the_same_form(site_admin, moderator):
    resp = logged_in_client(site_admin).post(
        f"/admin/social/user/{moderator.pk}/change/",
        change_payload(moderator, is_moderator=""),
    )
    assert resp.status_code == 302
    moderator.refresh_from_db()
    assert moderator.is_moderator is False
    # Revocation takes effect on the next request: request.user is reloaded
    # from the session, so no session store needs invalidating.
    assert logged_in_client(moderator).get(MODERATE_URL).status_code == 403


def test_the_moderator_field_is_on_the_admin_change_form_only(site_admin, member):
    body = (
        logged_in_client(site_admin)
        .get(f"/admin/social/user/{member.pk}/change/")
        .content.decode()
    )
    assert 'name="is_moderator"' in body
    # Not on the add form, which is the invite-only create path.
    add_body = (
        logged_in_client(site_admin).get("/admin/social/user/add/").content.decode()
    )
    assert 'name="is_moderator"' not in add_body


# --- the staff rows in the two menus (M1, owner rulings D1/D2) -------------


def test_moderator_sees_moderate_only_in_the_menu(moderator):
    client = logged_in_client(moderator)
    nav = _site_nav(client, HEADER_PAGE)
    assert '<a href="/moderate/"' in nav
    assert ">Moderate<" in nav
    # The flag-only moderator (R158) holds no staff, so no admin row.
    assert 'href="/admin/"' not in nav
    assert ">Administration<" not in nav


def test_moderator_sees_moderate_in_the_more_sheet(moderator):
    sheet = _more_sheet(logged_in_client(moderator), HEADER_PAGE)
    assert ">Moderate<" in sheet
    assert ">Administration<" not in sheet


def test_member_sees_neither_row_anywhere(member):
    client = logged_in_client(member)
    for surface in (_site_nav(client, HEADER_PAGE), _more_sheet(client, HEADER_PAGE)):
        assert 'href="/moderate/"' not in surface
        assert 'href="/admin/"' not in surface
        assert ">Moderate<" not in surface
        assert ">Administration<" not in surface


def test_anonymous_sees_neither_row():
    body = Client().get(HEADER_PAGE).content.decode()
    assert 'href="/moderate/"' not in body
    assert 'href="/admin/"' not in body


def test_the_site_admin_sees_both_rows_in_both_surfaces(site_admin):
    client = logged_in_client(site_admin)
    nav = _site_nav(client, HEADER_PAGE)
    assert 'href="/admin/"' in nav and 'href="/moderate/"' in nav
    # D1's order in the menu: Administration above Moderate.
    assert nav.index(">Administration<") < nav.index(">Moderate<")
    sheet = _more_sheet(client, HEADER_PAGE)
    assert ">Administration<" in sheet and ">Moderate<" in sheet
    assert sheet.index(">Administration<") < sheet.index(">Moderate<")


def test_a_staff_account_without_the_flag_sees_both_rows(staff):
    # D2 widened the queue row's gate to the queue's own predicate: staff
    # pass may_moderate (R103's defense-in-depth), so the row is a way onto
    # the queue the account can actually open -- and Administration is the
    # staff door it always was.
    client = logged_in_client(staff)
    nav = _site_nav(client, HEADER_PAGE)
    assert 'href="/moderate/"' in nav and 'href="/admin/"' in nav


def test_the_moderate_row_lights_as_current_on_the_queue(moderator):
    # The exact-equality active rule applied to the row on its own page:
    # exactly one lit row, and it is Moderate.
    nav = _site_nav(logged_in_client(moderator), MODERATE_URL)
    assert nav.count('aria-current="page"') == 1
    assert '<a href="/moderate/" class="active" aria-current="page">' in nav


# --- the surfaces M1 retired ------------------------------------------------


def test_the_settings_page_lists_no_moderate_for_anyone(moderator, site_admin, staff):
    # The row moved into the menus; the hub keeps Import and Export only,
    # even for the accounts whose menus now carry the queue row.
    for user in (moderator, site_admin, staff):
        body = logged_in_client(user).get(SETTINGS_PAGE).content.decode()
        hub = re.search(r'<nav class="settings-links".*?</nav>', body, re.S)
        assert hub, "no settings links nav rendered"
        assert ">Moderate<" not in hub.group(0)
        assert 'href="/moderate/"' not in hub.group(0)
        # Control: the hub rendered its two survivors, so the absence above
        # is about the moved row, not an empty page.
        assert ">Import<" in hub.group(0) and ">Export<" in hub.group(0)


def test_the_header_corner_carries_no_moderate(moderator):
    # The corner used to hold the moderator's desk link (R101); M1 deleted
    # it, so a moderator's corner holds the badge and the handle only.
    content = logged_in_client(moderator).get(HEADER_PAGE).content.decode()
    start = content.index('<div class="header-user">')
    corner = content[start : content.index("</div>", start)]
    assert "Moderate" not in corner
    assert "corner-desk" not in corner


def test_no_css_names_the_retired_corner_parts():
    # The corner link's markup is gone (pinned above via base.html through
    # the rendered corner); this pins that its CSS followed it, so no orphan
    # rule survives the deletion.
    from pathlib import Path

    found = finders.find("css/reeltalk.css")
    assert found
    css = Path(found).read_text()
    assert "moderation-link" not in css
    assert "corner-desk" not in css
