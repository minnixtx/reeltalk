"""A staff account with change-on-users cannot climb to superuser (F13).

The user admin is a plain ``ModelAdmin``, and stock Django gives it no
per-object guard: any staff account granted ``change_user`` could open its own
row, tick ``is_superuser``, and save -- or overwrite the site admin's password
and sign in as them. That is the whole moderation model bypassed through the
one surface that never consulted it.

Every test here drives the real admin HTTP endpoints as a staff account that
*actually holds* ``change_user`` (not a superuser, and not a moderator who
never reaches ``/admin/`` at all), so the refusal is measured on the request
rather than assumed from a flag. The other half of the contract is that the
support flow the admin exists to serve -- reset a member's password, correct
an address (R124) -- still works for that same account.
"""

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from reeltalk.social.models import User
from reeltalk.tests.members import member as create_member
from reeltalk.tests.members import site_admin as create_site_admin

PASSWORD = "s3cretpass"


def grant(user, *codenames):
    """Give ``user`` model permissions on ``social.User``.

    ``change_user`` alone is the F13 threat: it is exactly the grant the owner
    hands a support/helper account to reset a member's password. The request's
    ``user`` is reloaded from the database on every request, so granting on the
    fixture object takes effect for the client that logs in as them.
    """
    for codename in codenames:
        user.user_permissions.add(
            Permission.objects.get(
                codename=codename,
                content_type__app_label="social",
                content_type__model="user",
            )
        )
    return user


def as_client(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def site_admin(db):
    return create_site_admin(localname="root", password=PASSWORD)


@pytest.fixture
def helper(db):
    """A support staff account: staff, verified, and holding change_user."""
    return grant(
        create_member(localname="helper", password=PASSWORD, is_staff=True),
        "change_user",
    )


@pytest.fixture
def member(db):
    return create_member(localname="member", password=PASSWORD)


def change_payload(user, **overrides):
    """A full change-form POST body, including the role boxes.

    The role boxes are always present so a test that means to attack with them
    only overrides the one it wants; for a non-superuser they are not bound to
    the form at all, which is the point.
    """
    data = {
        "display_name": user.display_name or user.localname,
        "email": user.email or "someone@example.com",
        "is_staff": "",
        "is_superuser": "",
        "is_moderator": "",
        "report_email": "",
    }
    data.update(overrides)
    return data


# --- self-promotion is closed ---------------------------------------------


def test_a_staff_helper_cannot_make_itself_superuser(helper):
    # The helper's own row holds a role (is_staff), so it is refused outright.
    resp = as_client(helper).post(
        f"/admin/social/user/{helper.pk}/change/",
        change_payload(helper, is_superuser="on", is_moderator="on"),
    )
    assert resp.status_code == 403
    helper.refresh_from_db()
    assert helper.is_superuser is False
    assert helper.is_moderator is False


def test_a_staff_helper_cannot_open_its_own_privileged_row(helper):
    # Not just the write: the change page itself is refused, because the row
    # the helper would be editing is one that can open the admin.
    resp = as_client(helper).get(f"/admin/social/user/{helper.pk}/change/")
    assert resp.status_code == 403


# --- the grant is closed even on a row the helper may edit -----------------


def test_a_staff_helper_cannot_grant_roles_to_a_regular_member(helper, member):
    # A regular member is a row the helper may legitimately edit, so the guard
    # here is not a refusal but the role boxes being absent from the form: a
    # hand-crafted POST carrying them is dropped, not honoured. The display
    # name still saves, which proves the save ran and simply ignored the roles.
    resp = as_client(helper).post(
        f"/admin/social/user/{member.pk}/change/",
        change_payload(
            member, display_name="Renamed", is_superuser="on", is_staff="on"
        ),
    )
    assert resp.status_code == 302
    member.refresh_from_db()
    assert member.display_name == "Renamed"
    assert member.is_superuser is False
    assert member.is_staff is False


def test_the_role_boxes_are_absent_from_a_helpers_change_form(helper, member):
    body = (
        as_client(helper)
        .get(f"/admin/social/user/{member.pk}/change/")
        .content.decode()
    )
    for field in ("is_staff", "is_superuser", "is_moderator"):
        assert f'name="{field}"' not in body


# --- privileged rows are off-limits ---------------------------------------


def test_a_staff_helper_cannot_edit_the_site_admin(helper, site_admin):
    original = site_admin.password
    resp = as_client(helper).post(
        f"/admin/social/user/{site_admin.pk}/change/",
        change_payload(
            site_admin, password1="pwned-pass-123", password2="pwned-pass-123"
        ),
    )
    assert resp.status_code == 403
    site_admin.refresh_from_db()
    assert site_admin.password == original


def test_a_staff_helper_cannot_delete_the_site_admin(db, site_admin):
    deleter = grant(
        create_member(localname="deleter", password=PASSWORD, is_staff=True),
        "change_user",
        "delete_user",
    )
    resp = as_client(deleter).post(
        f"/admin/social/user/{site_admin.pk}/delete/", {"post": "yes"}
    )
    assert resp.status_code == 403
    assert User.objects.filter(pk=site_admin.pk).exists()


def test_a_staff_helper_cannot_delete_the_instance_representative(db):
    """The representative holds no role, so the role test alone would miss it (F18b).

    Deleting it is not a tidy-up: it orphans every ``Flag`` already signed with
    that keypair, and a fresh recreation gets a new key that cannot re-sign
    them (R111). The site admin can still reach the row; a helper cannot.
    """
    from reeltalk.moderation.representative import instance_representative

    rep = instance_representative()
    assert not (rep.is_superuser or rep.is_staff or rep.is_moderator)
    deleter = grant(
        create_member(localname="deleter", password=PASSWORD, is_staff=True),
        "change_user",
        "delete_user",
    )
    resp = as_client(deleter).post(
        f"/admin/social/user/{rep.pk}/delete/", {"post": "yes"}
    )
    assert resp.status_code == 403
    assert User.objects.filter(pk=rep.pk).exists()


# --- the support flow still works -----------------------------------------


def test_a_staff_helper_can_still_edit_a_regular_member(helper, member):
    # The reason the admin exists: a helper fixing a member's details is not
    # an escalation and must not be refused.
    resp = as_client(helper).post(
        f"/admin/social/user/{member.pk}/change/",
        change_payload(member, display_name="Corrected"),
    )
    assert resp.status_code == 302
    member.refresh_from_db()
    assert member.display_name == "Corrected"


def test_a_staff_helper_can_still_reset_a_members_password(helper, member):
    resp = as_client(helper).post(
        f"/admin/social/user/{member.pk}/change/",
        change_payload(member, password1="fresh-pass-456", password2="fresh-pass-456"),
    )
    assert resp.status_code == 302
    member.refresh_from_db()
    assert member.check_password("fresh-pass-456")


# --- the superuser is untouched by the guard ------------------------------


def test_the_superuser_can_still_grant_superuser(site_admin, member):
    # The guard refuses non-superusers only; the site admin's own authority is
    # unchanged, so this is the regression pin that the fix did not over-refuse.
    resp = as_client(site_admin).post(
        f"/admin/social/user/{member.pk}/change/",
        change_payload(member, is_superuser="on", is_staff="on"),
    )
    assert resp.status_code == 302
    member.refresh_from_db()
    assert member.is_superuser is True
    assert member.is_staff is True
