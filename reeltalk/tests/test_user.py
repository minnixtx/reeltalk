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
