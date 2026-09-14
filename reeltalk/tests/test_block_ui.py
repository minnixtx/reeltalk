"""The block / unblock surface on the profile page (M5 increment 3, R53).

Blocking is read-side state: it only changes the local ``User.blocks`` M2M.
A remote target is handled exactly like a local one — no Block activity is
delivered over federation in v0.1 (accepted limit), so blocking never makes a
network call. The button is hidden on one's own profile and for anonymous
visitors; only resolvable profiles can be blocked (an unknown handle 404s).
The routes share the profile's extended localname pattern, so mirror handles
(<user>@<netloc>, port included) match.
"""

import pytest
import responses

from reeltalk.activitypub import crypto
from reeltalk.social.models import User

REMOTE_ACTOR = "https://remote.example:8443/user/carol/"
REMOTE_INBOX = REMOTE_ACTOR + "inbox/"


@pytest.fixture()
def remote_keypair():
    return crypto.generate_keypair()


def _mirror(public_pem: str) -> User:
    """A remote mirror as federation would create it (key + home inbox)."""
    user = User(
        localname="carol@remote.example:8443",
        local=False,
        actor_url=REMOTE_ACTOR,
        inbox_url=REMOTE_INBOX,
        public_key=public_pem,
    )
    user.set_unusable_password()
    user.save()
    return user


# --- The button on the profile page ------------------------------------------


@pytest.mark.django_db
def test_block_button_hidden_on_own_profile(client):
    User.objects.create_user(localname="alice", password="p")
    client.login(username="alice", password="p")
    content = client.get("/user/alice/").content
    assert b"/block/" not in content
    assert b"/unblock/" not in content


@pytest.mark.django_db
def test_block_button_hidden_for_anonymous(client):
    User.objects.create_user(localname="alice", password="p")
    content = client.get("/user/alice/").content
    assert b"/block/" not in content
    assert b"/unblock/" not in content


@pytest.mark.django_db
def test_block_button_shows_for_other_users(client):
    User.objects.create_user(localname="alice", password="p")
    User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")
    content = client.get("/user/alice/").content
    assert b"/user/alice/block/" in content
    assert b"Block</button>" in content
    assert b"/unblock/" not in content


@pytest.mark.django_db
def test_blocked_button_shows_when_already_blocked(client):
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    bob.blocks.add(alice)
    client.login(username="bob", password="p")
    content = client.get("/user/alice/").content
    assert b"/user/alice/unblock/" in content
    assert b"Blocked</button>" in content


# --- Local target: plain M2M, no delivery -------------------------------------


@responses.activate
@pytest.mark.django_db
def test_block_local_target_records_m2m_without_delivery(client):
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")

    response = client.post("/user/alice/block/")

    assert response.status_code == 302
    assert response["Location"] == "/user/alice/"
    assert alice in bob.blocks.all()
    # Read-side state: nothing is delivered over the network.
    assert len(responses.calls) == 0


@pytest.mark.django_db
def test_unblock_local_target_removes_m2m(client):
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    bob.blocks.add(alice)
    client.login(username="bob", password="p")

    response = client.post("/user/alice/unblock/")

    assert response.status_code == 302
    assert alice not in bob.blocks.all()


@pytest.mark.django_db
def test_block_self_is_rejected(client):
    User.objects.create_user(localname="alice", password="p")
    client.login(username="alice", password="p")

    response = client.post("/user/alice/block/")

    assert response.status_code == 302
    alice = User.objects.get(localname="alice")
    assert not alice.blocks.filter(pk=alice.pk).exists()


# --- Remote target: local-only, no delivery (v0.1 limit) ----------------------


@responses.activate
@pytest.mark.django_db
def test_block_remote_target_records_m2m_without_delivery(client, remote_keypair):
    bob = User.objects.create_user(localname="bob", password="p")
    _private_pem, public_pem = remote_keypair
    mirror = _mirror(public_pem)  # discovery created the mirror earlier
    client.login(username="bob", password="p")

    response = client.post("/user/carol@remote.example:8443/block/")

    assert response.status_code == 302
    # '@' and ':' are legal in a URL path — reverse() does not encode them.
    assert response["Location"] == "/user/carol@remote.example:8443/"
    bob.refresh_from_db()
    assert mirror in bob.blocks.all()
    # v0.1: blocking is read-side only — no Block activity is delivered.
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_unblock_remote_target_removes_m2m_without_delivery(client, remote_keypair):
    bob = User.objects.create_user(localname="bob", password="p")
    _private_pem, public_pem = remote_keypair
    mirror = _mirror(public_pem)
    bob.blocks.add(mirror)
    client.login(username="bob", password="p")

    response = client.post("/user/carol@remote.example:8443/unblock/")

    assert response.status_code == 302
    bob.refresh_from_db()
    assert mirror not in bob.blocks.all()
    assert len(responses.calls) == 0


# --- Route behavior ------------------------------------------------------------


@pytest.mark.django_db
def test_block_get_is_not_allowed(client):
    User.objects.create_user(localname="alice", password="p")
    User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")
    assert client.get("/user/alice/block/").status_code == 405


@pytest.mark.django_db
def test_block_requires_login(client):
    User.objects.create_user(localname="alice", password="p")
    response = client.post("/user/alice/block/")
    assert response.status_code == 302
    assert "/login/" in response["Location"]


@pytest.mark.django_db
def test_block_unknown_user_404(client):
    User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")
    assert client.post("/user/nobody/block/").status_code == 404


@responses.activate
@pytest.mark.django_db
def test_block_unknown_remote_404(client):
    # No mirror on this instance — only resolvable profiles can be blocked.
    bob = User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")

    response = client.post("/user/carol@remote.example:8443/block/")

    assert response.status_code == 404
    assert User.objects.filter(local=False).count() == 0
    assert not bob.blocks.exists()
