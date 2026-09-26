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
* The grant is made through the real admin form and does not widen to staff.
* A moderator cannot flip the flag — on someone else, or on themselves.
* The header link appears for a moderator and for nobody else.
"""

import pytest
from django.contrib.staticfiles import finders
from django.test import Client
from django.urls import reverse

from reeltalk.social.models import User

PASSWORD = "s3cretpass"
MODERATE_URL = reverse("moderation")
ADMIN_URL = reverse("admin:index")
ADMIN_LOGIN_URL = reverse("admin:login")
ADMIN_USER_CHANGELIST = "/admin/social/user/"
ADMIN_LOGIN_TEMPLATE = "admin/login.html"
ADMIN_INDEX_TEMPLATE = "admin/index.html"

MODERATOR_LINK = 'class="moderation-link"'
# A page that renders for any signed-in member with no gate of its own, so a
# header assertion on it cannot be passing on a redirect. (``/`` is the
# wrong choice: the first-run wizard sends it to /setup/ until a superuser
# exists.)
HEADER_PAGE = reverse("find-user")


def rendered_templates(response):
    return [t.name for t in response.templates if t.name]


def logged_in_client(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def site_admin(db):
    return User.objects.create_superuser(localname="root", password=PASSWORD)


@pytest.fixture
def staff(db):
    """Staff without the moderator flag — the admin side of the same surface."""
    return User.objects.create_user(
        localname="helper", password=PASSWORD, is_staff=True
    )


@pytest.fixture
def moderator(db):
    """Flagged to moderate, and pointedly NOT staff (R100)."""
    return User.objects.create_user(
        localname="mod", password=PASSWORD, is_moderator=True
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(localname="member", password=PASSWORD)


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
    # The decoupling R100 is built on, asserted on the row the fixture made.
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
    # Granting moderation did not hand over the admin.
    assert member.is_staff is False


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


# --- the header link ------------------------------------------------------


def test_moderator_sees_the_header_link(moderator):
    body = logged_in_client(moderator).get(HEADER_PAGE).content.decode()
    assert MODERATOR_LINK in body
    assert 'href="/moderate/"' in body
    # An <a> that only navigates must not wear .btn — that is the lit
    # crimson control (R96), and the markup looks harmless either way.
    assert "btn" not in body.split(MODERATOR_LINK)[1][:40]


def test_member_sees_no_moderator_link(member):
    body = logged_in_client(member).get(HEADER_PAGE).content.decode()
    assert MODERATOR_LINK not in body
    assert 'href="/moderate/"' not in body


def test_anonymous_sees_no_moderator_link():
    body = Client().get(HEADER_PAGE).content.decode()
    assert MODERATOR_LINK not in body
    assert 'href="/moderate/"' not in body


def test_a_staff_user_without_the_flag_sees_no_link(staff):
    # The link reads is_moderator, not the gate: staff reach /moderate/ by
    # permission but are not advertised a moderator role they do not hold.
    body = logged_in_client(staff).get(HEADER_PAGE).content.decode()
    assert MODERATOR_LINK not in body


def test_no_css_was_added_for_the_moderation_link():
    # R101: the surface reuses existing classes and adds no stylesheet rules.
    from pathlib import Path

    found = finders.find("css/reeltalk.css")
    assert found
    assert "moderation-link" not in Path(found).read_text()
