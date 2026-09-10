"""Custom User model tests (PLAN.md §3.2, decision R10)."""

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

User = get_user_model()


def test_auth_user_model_is_social_user():
    assert User._meta.label == "social.User"
    assert User.USERNAME_FIELD == "localname"


@pytest.mark.django_db
def test_create_user_defaults():
    user = User.objects.create_user(localname="alice", password="s3cretpass")
    assert user.local is True
    assert user.is_staff is False
    assert user.is_superuser is False
    assert user.check_password("s3cretpass")
    # Full identity is localname@domain (§3.2); test DOMAIN is localhost.
    assert user.username == "alice@localhost"


@pytest.mark.django_db
def test_create_user_normalizes_missing_email():
    user = User.objects.create_user(localname="alice", password="p", email=None)
    assert user.email == ""


@pytest.mark.django_db
def test_create_superuser_sets_flags():
    user = User.objects.create_superuser(localname="admin", password="s3cretpass")
    assert user.is_staff is True
    assert user.is_superuser is True


@pytest.mark.django_db
def test_create_superuser_rejects_non_superuser():
    with pytest.raises(ValueError):
        User.objects.create_superuser(localname="x", password="p", is_superuser=False)


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [("", "carol"), ("Carol D.", "Carol D.")],
)
def test_get_full_name_falls_back_to_localname(display_name, expected):
    user = User(localname="carol", display_name=display_name)
    assert user.get_full_name() == expected
    assert str(user) == expected


@pytest.mark.django_db
def test_follow_and_block_relations():
    alice = User.objects.create_user(localname="alice")
    bob = User.objects.create_user(localname="bob")
    alice.follows.add(bob)
    alice.blocks.add(bob)
    assert bob in alice.follows.all()
    assert alice in bob.followers.all()
    assert bob in alice.blocks.all()
    assert alice in bob.blocked_by.all()


@pytest.mark.django_db
def test_localname_is_unique():
    User.objects.create_user(localname="alice")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            User.objects.create_user(localname="alice")


# --- ActivityPub key pair (M4, R7) -------------------------------------------


@pytest.mark.django_db
def test_local_user_gets_keypair_on_creation():
    from reeltalk.activitypub import crypto

    user = User.objects.create_user(localname="alice", password="p")
    assert user.private_key and user.public_key
    # Both parse, and the public half matches the private key's.
    private = crypto.load_private_key(user.private_key)
    assert crypto.load_public_key(user.public_key) == private.public_key()


@pytest.mark.django_db
def test_resave_does_not_regenerate_keys():
    user = User.objects.create_user(localname="alice", password="p")
    original = (user.private_key, user.public_key)
    user.display_name = "Alice A."
    user.save()
    assert (user.private_key, user.public_key) == original


@pytest.mark.django_db
def test_remote_mirror_carries_no_private_key():
    # Remote users (M4) are mirrors signed by their home instance — they get
    # only a public key, fetched from their Person document.
    user = User(localname="remote-alice", local=False)
    user.set_unusable_password()
    user.public_key = "fetched-pem"
    user.save()
    assert user.private_key == ""
    assert user.public_key == "fetched-pem"
