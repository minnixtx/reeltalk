"""The follow / unfollow surface on the profile page (M5 increment 2).

The button is hidden on one's own profile and for anonymous visitors. A
local target is a plain M2M change — same instance, no delivery; a remote
target goes through the signed Follow / Undo(Follow) delivery (M4). Only
resolvable profiles can be followed (an unknown handle 404s — discovery at
/find/ creates an unknown remote's mirror first), and a delivery to a down
home instance records the local state with a warning instead of a 500. The
routes share the profile's extended localname pattern, so mirror handles
(<user>@<netloc>, port included) match.
"""

import json

import pytest
import requests
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
def test_follow_button_hidden_on_own_profile(client):
    User.objects.create_user(localname="alice", password="p")
    client.login(username="alice", password="p")
    content = client.get("/user/alice/").content
    assert b"/follow/" not in content
    assert b"/unfollow/" not in content


@pytest.mark.django_db
def test_follow_button_hidden_for_anonymous(client):
    User.objects.create_user(localname="alice", password="p")
    content = client.get("/user/alice/").content
    assert b"/follow/" not in content
    assert b"/unfollow/" not in content


@pytest.mark.django_db
def test_follow_button_shows_for_other_users(client):
    User.objects.create_user(localname="alice", password="p")
    User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")
    content = client.get("/user/alice/").content
    assert b"/user/alice/follow/" in content
    assert b"Follow</button>" in content
    assert b"/unfollow/" not in content


@pytest.mark.django_db
def test_following_button_shows_when_already_following(client):
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    bob.follows.add(alice)
    client.login(username="bob", password="p")
    content = client.get("/user/alice/").content
    assert b"/user/alice/unfollow/" in content
    assert b"Following</button>" in content


# --- Local target: plain M2M, no delivery -------------------------------------


@responses.activate
@pytest.mark.django_db
def test_follow_local_target_records_m2m_without_delivery(client):
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")

    response = client.post("/user/alice/follow/")

    assert response.status_code == 302
    assert response["Location"] == "/user/alice/"
    assert alice in bob.follows.all()
    # Same instance: the M2M is the whole relationship — nothing delivered.
    assert len(responses.calls) == 0


@pytest.mark.django_db
def test_unfollow_local_target_removes_m2m(client):
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    bob.follows.add(alice)
    client.login(username="bob", password="p")

    response = client.post("/user/alice/unfollow/")

    assert response.status_code == 302
    assert alice not in bob.follows.all()


@pytest.mark.django_db
def test_follow_self_is_rejected(client):
    User.objects.create_user(localname="alice", password="p")
    client.login(username="alice", password="p")

    response = client.post("/user/alice/follow/")

    assert response.status_code == 302
    alice = User.objects.get(localname="alice")
    assert not alice.follows.filter(pk=alice.pk).exists()


# --- Remote target: signed Follow / Undo(Follow) delivery ---------------------


@responses.activate
@pytest.mark.django_db
def test_follow_remote_target_records_and_delivers(client, remote_keypair):
    bob = User.objects.create_user(localname="bob", password="p")
    _private_pem, public_pem = remote_keypair
    # Discovery (/find/ or first contact) created the mirror earlier — the
    # follow route only acts on resolvable profiles.
    mirror = _mirror(public_pem)
    responses.add(responses.POST, REMOTE_INBOX)
    client.login(username="bob", password="p")

    response = client.post("/user/carol@remote.example:8443/follow/")

    assert response.status_code == 302
    # '@' and ':' are legal in a URL path — reverse() does not encode them.
    assert response["Location"] == "/user/carol@remote.example:8443/"
    bob.refresh_from_db()
    assert mirror in bob.follows.all()
    # The mirror already existed: no fetch, one signed Follow to the inbox.
    assert [c.request.method for c in responses.calls] == ["POST"]
    sent = json.loads(responses.calls[0].request.body)
    assert sent["type"] == "Follow"
    assert sent["object"] == REMOTE_ACTOR


@responses.activate
@pytest.mark.django_db
def test_unfollow_remote_target_delivers_undo(client, remote_keypair):
    bob = User.objects.create_user(localname="bob", password="p")
    _private_pem, public_pem = remote_keypair
    mirror = _mirror(public_pem)
    bob.follows.add(mirror)
    responses.add(responses.POST, REMOTE_INBOX)
    client.login(username="bob", password="p")

    response = client.post("/user/carol@remote.example:8443/unfollow/")

    assert response.status_code == 302
    bob.refresh_from_db()
    assert mirror not in bob.follows.all()
    # The mirror already existed: no fetch, one Undo(Follow) delivery.
    assert [c.request.method for c in responses.calls] == ["POST"]
    sent = json.loads(responses.calls[0].request.body)
    assert sent["type"] == "Undo"
    assert sent["object"]["type"] == "Follow"
    assert sent["object"]["object"] == REMOTE_ACTOR


@responses.activate
@pytest.mark.django_db
def test_unfollow_remote_target_not_following_is_a_noop(client, remote_keypair):
    User.objects.create_user(localname="bob", password="p")
    _private_pem, public_pem = remote_keypair
    _mirror(public_pem)  # the mirror exists, but bob does not follow it
    client.login(username="bob", password="p")

    response = client.post("/user/carol@remote.example:8443/unfollow/")

    assert response.status_code == 302
    assert len(responses.calls) == 0  # no spurious Undo


@responses.activate
@pytest.mark.django_db
def test_follow_unknown_remote_404s(client):
    # No mirror on this instance — you can only follow users whose profile
    # resolves (discovery at /find/ creates an unknown remote's mirror first).
    bob = User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")

    response = client.post("/user/carol@remote.example:8443/follow/")

    assert response.status_code == 404
    # Nothing recorded locally — the mirror was never resolved.
    assert User.objects.filter(local=False).count() == 0
    assert not bob.follows.exists()


@responses.activate
@pytest.mark.django_db
def test_follow_down_instance_records_locally_with_warning(client, remote_keypair):
    # The mirror exists (first contact happened earlier) but the home
    # instance is down now: the follow is recorded locally and a warning
    # surfaces instead of a 500 — v0.1 has no retry queue.
    bob = User.objects.create_user(localname="bob", password="p")
    _private_pem, public_pem = remote_keypair
    mirror = _mirror(public_pem)
    responses.add(
        responses.POST, REMOTE_INBOX, body=requests.exceptions.ConnectionError()
    )
    client.login(username="bob", password="p")

    response = client.post("/user/carol@remote.example:8443/follow/")

    assert response.status_code == 302
    bob.refresh_from_db()
    assert mirror in bob.follows.all()  # recorded locally
    own_profile = client.get("/user/bob/")
    # (the apostrophe in "couldn't" is HTML-escaped in the rendered page)
    assert b"may not have received the follow yet" in own_profile.content


# --- Route behavior ------------------------------------------------------------


@pytest.mark.django_db
def test_follow_get_is_not_allowed(client):
    User.objects.create_user(localname="alice", password="p")
    User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")
    assert client.get("/user/alice/follow/").status_code == 405


@pytest.mark.django_db
def test_follow_requires_login(client):
    User.objects.create_user(localname="alice", password="p")
    response = client.post("/user/alice/follow/")
    assert response.status_code == 302
    assert "/login/" in response["Location"]


@pytest.mark.django_db
def test_follow_unknown_user_404(client):
    User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")
    assert client.post("/user/nobody/follow/").status_code == 404
