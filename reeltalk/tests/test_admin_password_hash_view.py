"""The full password hash is shown only to an account that can set one (F15).

Since Django 2.1 the ``view_user`` permission alone opens the user change page
read-only, and a read-only display method renders there. The ``password_hash``
column used to render the whole hash to anyone who could open that page, so a
staff account handed view-only rights could open every account -- the site
admin's included -- and harvest the hashes to crack offline. The display
method's old justification ("whoever reaches this page can already set a
password") was simply false for a viewer who cannot write.

The control is that ``get_fieldsets`` drops the field unless the requester holds
``social.change_user``. Each test drives the real change-page endpoint and looks
for the stored hash in the rendered body, so the fix is measured on the page a
helper actually sees rather than on the fieldset structure in isolation.
"""

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from reeltalk.tests.members import member as create_member
from reeltalk.tests.members import site_admin as create_site_admin

PASSWORD = "s3cretpass"


def grant(user, *codenames):
    """Give ``user`` model permissions on ``social.User``."""
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
def member(db):
    return create_member(localname="member", password=PASSWORD)


@pytest.fixture
def viewer(db):
    """A staff account with view-only rights: view_user, never change_user."""
    return grant(
        create_member(localname="viewer", password=PASSWORD, is_staff=True),
        "view_user",
    )


@pytest.fixture
def editor(db):
    """A staff account that can write users -- the hash stays visible to it."""
    return grant(
        create_member(localname="editor", password=PASSWORD, is_staff=True),
        "change_user",
    )


def test_a_view_only_helper_cannot_see_the_password_hash(viewer, member):
    # The page still opens -- view_user is a real permission and the support
    # flow of *looking* at a member is legitimate -- but the crackable secret
    # is not on it.
    resp = as_client(viewer).get(f"/admin/social/user/{member.pk}/change/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert member.password not in body
    assert "Current password hash" not in body


def test_an_account_that_can_change_users_still_sees_the_hash(editor, member):
    # The regression pin: the fix must not over-refuse. An account that can
    # already write a password keeps the read the column exists to serve.
    resp = as_client(editor).get(f"/admin/social/user/{member.pk}/change/")
    assert resp.status_code == 200
    assert member.password in resp.content.decode()


def test_the_site_admin_still_sees_the_hash(site_admin, member):
    # A superuser has every permission implicitly, so the gate never fires for
    # them -- the read the previous page always gave is unchanged.
    resp = as_client(site_admin).get(f"/admin/social/user/{member.pk}/change/")
    assert resp.status_code == 200
    assert member.password in resp.content.decode()
