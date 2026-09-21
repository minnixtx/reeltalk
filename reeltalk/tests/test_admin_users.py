"""Admin account creation and editing (deploy-readiness increment 5).

A bare ``ModelAdmin`` on a custom user model writes the typed password straight
into the ``password`` column, unhashed — the account then cannot log in and a
plaintext secret is left in the database. These tests pin the fix: creating
through the admin hashes, editing cannot swap the hash for plaintext, and the
federation internals are not editable from the admin at all.

Everything here goes through the real admin HTTP endpoints rather than the forms
directly, so it also proves the admin is wired to those forms.
"""

import pytest
from django.contrib.auth import authenticate

from reeltalk.social.models import User

STRONG = "a-strong-pass-phrase"


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def member(db):
    return User.objects.create_user(
        localname="member", password="original-pass", display_name="Member"
    )


def add_payload(**overrides):
    data = {
        "localname": "invitee",
        "display_name": "Invitee",
        "email": "invitee@example.com",
        "password1": STRONG,
        "password2": STRONG,
    }
    data.update(overrides)
    return data


# --- creating an account --------------------------------------------------


def test_admin_add_hashes_the_password(admin_client):
    resp = admin_client.post("/admin/social/user/add/", add_payload())
    assert resp.status_code == 302

    user = User.objects.get(localname="invitee")
    assert user.password != STRONG
    assert user.password.startswith("pbkdf2_")
    assert user.check_password(STRONG)


def test_admin_add_leaves_no_plaintext_on_the_row(admin_client):
    admin_client.post("/admin/social/user/add/", add_payload())
    user = User.objects.get(localname="invitee")
    for field in ("password", "display_name", "email", "localname"):
        assert STRONG not in getattr(user, field)


def test_admin_created_account_can_authenticate(admin_client):
    admin_client.post("/admin/social/user/add/", add_payload())
    assert authenticate(username="invitee", password=STRONG) is not None
    assert authenticate(username="invitee", password="wrong") is None


def test_admin_add_does_not_grant_privileges(admin_client):
    admin_client.post("/admin/social/user/add/", add_payload())
    user = User.objects.get(localname="invitee")
    assert not user.is_staff
    assert not user.is_superuser


def test_admin_add_creates_the_default_shelves(admin_client):
    # Going through the model means the D1 shelf bootstrap still happens.
    admin_client.post("/admin/social/user/add/", add_payload())
    user = User.objects.get(localname="invitee")
    assert user.shelves.count() == 2


@pytest.mark.parametrize("bad", ["_leading", "has space", "no!punctuation"])
def test_admin_add_rejects_an_unusable_localname(admin_client, bad):
    resp = admin_client.post("/admin/social/user/add/", add_payload(localname=bad))
    assert resp.status_code == 200  # re-rendered with the error
    assert not User.objects.filter(localname=bad).exists()


def test_admin_add_rejects_a_case_insensitive_duplicate(admin_client, member):
    resp = admin_client.post("/admin/social/user/add/", add_payload(localname="MEMBER"))
    assert resp.status_code == 200
    assert not User.objects.filter(localname="MEMBER").exists()


def test_admin_add_requires_the_password_confirmation(admin_client):
    resp = admin_client.post(
        "/admin/social/user/add/",
        add_payload(password2="something-else"),
    )
    assert resp.status_code == 200
    assert not User.objects.filter(localname="invitee").exists()


# --- editing an account ---------------------------------------------------


def change_payload(member, **overrides):
    data = {
        "display_name": "Renamed",
        "email": member.email,
        "is_staff": "",
        "is_superuser": "",
    }
    data.update(overrides)
    return data


def test_admin_edit_cannot_replace_the_hash_with_plaintext(admin_client, member):
    original = member.password

    resp = admin_client.post(
        f"/admin/social/user/{member.pk}/change/",
        change_payload(member, password="plaintext-injection"),
    )
    assert resp.status_code == 302  # the save really happened

    member.refresh_from_db()
    assert member.display_name == "Renamed"
    assert member.password == original
    assert member.check_password("original-pass")
    assert not member.check_password("plaintext-injection")


def test_admin_edit_cannot_rename_a_federated_identity(admin_client, member):
    resp = admin_client.post(
        f"/admin/social/user/{member.pk}/change/",
        change_payload(member, localname="evil"),
    )
    member.refresh_from_db()
    assert member.localname == "member"
    assert resp.status_code == 302


def test_private_key_is_not_on_any_admin_form(admin_client, member):
    add_body = admin_client.get("/admin/social/user/add/").content.decode()
    change_body = admin_client.get(
        f"/admin/social/user/{member.pk}/change/"
    ).content.decode()
    assert "private_key" not in add_body
    assert "private_key" not in change_body


def test_federation_fields_are_read_only_on_change(admin_client, member):
    body = admin_client.get(f"/admin/social/user/{member.pk}/change/").content.decode()
    # Rendered as read-only text, not as inputs an edit could submit.
    for field in ("actor_url", "inbox_url", "public_key", "raw_summary"):
        assert f'name="{field}"' not in body
