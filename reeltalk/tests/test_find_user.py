"""Remote-user discovery at /find/ (M5 increment 2).

One input, user@domain: a same-domain handle resolves the local account
directly; any other domain goes through webfinger (RFC 6454) over real HTTP
— the self link's actor URL becomes a mirror (created on first contact) and
the user is redirected to its profile. Resolve-and-redirect only: submitting
never follows anyone. The scheme rule: an explicit non-default port means
plain http at that port; without a port, https is tried first with an http
fallback.
"""

from urllib.parse import urlencode

import pytest
import requests
import responses

from reeltalk.activitypub import crypto
from reeltalk.social.models import User

REMOTE_DOMAIN = "remote.example"
REMOTE_ACTOR = f"https://{REMOTE_DOMAIN}/user/carol/"


@pytest.fixture()
def remote_keypair():
    return crypto.generate_keypair()


def _person_doc(actor_url: str, public_pem: str) -> dict:
    return {
        "@context": ["https://www.w3.org/ns/activitystreams"],
        "id": actor_url,
        "type": "Person",
        "preferredUsername": "carol",
        "name": "Carol Remote",
        "inbox": actor_url + "inbox/",
        "publicKey": {
            "id": f"{actor_url}#main-key",
            "owner": actor_url,
            "publicKeyPem": public_pem,
        },
    }


def _webfinger_jrd(actor_url: str, domain: str) -> dict:
    """A JRD document shaped like the one this project's own server emits."""
    return {
        "subject": f"acct:carol@{domain}",
        "aliases": [actor_url],
        "links": [
            {
                "rel": "lrdd",
                "type": "application/link-descriptions+json",
                "template": f"https://{domain}/.well-known/webfinger?resource={{uri}}",
            },
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": actor_url,
            },
            {"rel": "self", "type": "application/activity+json", "href": actor_url},
        ],
    }


def _webfinger_url(scheme: str, domain: str) -> str:
    """The exact webfinger URL the client builds for carol on ``domain``."""
    return (
        f"{scheme}://{domain}/.well-known/webfinger"
        + "?"
        + urlencode({"resource": f"acct:carol@{domain}"})
    )


# --- Access + form -------------------------------------------------------------


@pytest.mark.django_db
def test_find_requires_login(client):
    response = client.get("/find/")
    assert response.status_code == 302
    assert "/login/" in response["Location"]


@pytest.mark.django_db
def test_find_renders_the_form(client):
    User.objects.create_user(localname="alice", password="p")
    client.login(username="alice", password="p")
    response = client.get("/find/")
    assert response.status_code == 200
    content = response.content
    assert b'name="q"' in content
    assert b"user@domain" in content


@pytest.mark.django_db
def test_find_malformed_input_renders_error(client):
    User.objects.create_user(localname="alice", password="p")
    client.login(username="alice", password="p")
    for value in ("", "no-at-sign", "@remote.example", "carol@", "a@b@c"):
        response = client.post("/find/", {"q": value})
        assert response.status_code == 200
        assert b"Enter a full handle" in response.content


# --- Same-domain handles: local resolution, no network -------------------------


@responses.activate
@pytest.mark.django_db
def test_find_local_user_redirects_to_profile_without_network(client):
    User.objects.create_user(localname="alice", password="p")
    User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")

    response = client.post("/find/", {"q": "alice@localhost"})

    assert response.status_code == 302
    assert response["Location"] == "/user/alice/"
    # Same instance: resolved directly, no webfinger call.
    assert len(responses.calls) == 0


@pytest.mark.django_db
def test_find_local_match_is_case_insensitive(client):
    User.objects.create_user(localname="Alice", password="p")
    User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")

    response = client.post("/find/", {"q": "alice@localhost"})

    assert response.status_code == 302
    # The stored spelling wins (R40).
    assert response["Location"] == "/user/Alice/"


@pytest.mark.django_db
def test_find_unknown_local_user_renders_error(client):
    User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")

    response = client.post("/find/", {"q": "nobody@localhost"})

    assert response.status_code == 200
    assert b"No user named" in response.content


# --- Other domains: webfinger → mirror → profile -------------------------------


@responses.activate
@pytest.mark.django_db
def test_find_remote_user_creates_mirror_and_redirects(client, remote_keypair):
    bob = User.objects.create_user(localname="bob", password="p")
    _private_pem, public_pem = remote_keypair
    responses.add(
        responses.GET,
        f"https://{REMOTE_DOMAIN}/.well-known/webfinger",
        json=_webfinger_jrd(REMOTE_ACTOR, REMOTE_DOMAIN),
    )
    responses.add(
        responses.GET, REMOTE_ACTOR, json=_person_doc(REMOTE_ACTOR, public_pem)
    )
    client.login(username="bob", password="p")

    response = client.post("/find/", {"q": f"carol@{REMOTE_DOMAIN}"})

    assert response.status_code == 302
    # '@' and ':' are legal in a URL path — reverse() does not encode them.
    assert response["Location"] == "/user/carol@remote.example/"
    mirror = User.objects.get(local=False)
    assert mirror.localname == "carol@remote.example"
    assert mirror.actor_url == REMOTE_ACTOR
    assert mirror.public_key == public_pem
    # Resolve-and-redirect only: submitting never follows anyone.
    assert not bob.follows.exists()


@responses.activate
@pytest.mark.django_db
def test_find_existing_mirror_is_not_refetched(client):
    User.objects.create_user(localname="bob", password="p")
    mirror = User(localname="carol@remote.example", local=False, actor_url=REMOTE_ACTOR)
    mirror.set_unusable_password()
    mirror.save()
    responses.add(
        responses.GET,
        f"https://{REMOTE_DOMAIN}/.well-known/webfinger",
        json=_webfinger_jrd(REMOTE_ACTOR, REMOTE_DOMAIN),
    )
    client.login(username="bob", password="p")

    response = client.post("/find/", {"q": f"carol@{REMOTE_DOMAIN}"})

    assert response.status_code == 302
    # Webfinger only — the Person doc is fetched on first contact alone (R42).
    assert [c.request.url for c in responses.calls] == [
        _webfinger_url("https", REMOTE_DOMAIN)
    ]


@responses.activate
@pytest.mark.django_db
def test_find_remote_user_404_renders_error(client):
    User.objects.create_user(localname="bob", password="p")
    responses.add(
        responses.GET,
        f"https://{REMOTE_DOMAIN}/.well-known/webfinger",
        status=404,
    )
    client.login(username="bob", password="p")

    response = client.post("/find/", {"q": f"carol@{REMOTE_DOMAIN}"})

    assert response.status_code == 200
    assert b"Could not find" in response.content
    assert User.objects.filter(local=False).count() == 0


@responses.activate
@pytest.mark.django_db
def test_find_unreachable_domain_renders_error(client):
    User.objects.create_user(localname="bob", password="p")
    # Both schemes fail at the network level.
    responses.add(
        responses.GET,
        f"https://{REMOTE_DOMAIN}/.well-known/webfinger",
        body=requests.exceptions.ConnectionError(),
    )
    responses.add(
        responses.GET,
        f"http://{REMOTE_DOMAIN}/.well-known/webfinger",
        body=requests.exceptions.ConnectionError(),
    )
    client.login(username="bob", password="p")

    response = client.post("/find/", {"q": f"carol@{REMOTE_DOMAIN}"})

    assert response.status_code == 200
    assert b"Could not find" in response.content
    assert User.objects.filter(local=False).count() == 0


@responses.activate
@pytest.mark.django_db
def test_find_webfinger_without_self_link_renders_error(client):
    User.objects.create_user(localname="bob", password="p")
    responses.add(
        responses.GET,
        f"https://{REMOTE_DOMAIN}/.well-known/webfinger",
        json={"subject": "acct:carol@remote.example", "links": []},
    )
    client.login(username="bob", password="p")

    response = client.post("/find/", {"q": f"carol@{REMOTE_DOMAIN}"})

    assert response.status_code == 200
    assert b"Could not find" in response.content


# --- The scheme rule -------------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_find_explicit_port_uses_plain_http(client, remote_keypair):
    # An explicit non-default port marks a LAN instance: plain http at that
    # port. Nothing is registered for https — an attempt would fail the test.
    domain = "remote.example:8443"
    actor = f"http://{domain}/user/carol/"
    User.objects.create_user(localname="bob", password="p")
    _private_pem, public_pem = remote_keypair
    responses.add(
        responses.GET,
        f"http://{domain}/.well-known/webfinger",
        json=_webfinger_jrd(actor, domain),
    )
    responses.add(responses.GET, actor, json=_person_doc(actor, public_pem))
    client.login(username="bob", password="p")

    response = client.post("/find/", {"q": f"carol@{domain}"})

    assert response.status_code == 302
    mirror = User.objects.get(local=False)
    assert mirror.localname == "carol@remote.example:8443"
    assert mirror.actor_url == actor
    # http webfinger + the (http) Person doc — no https attempt.
    assert [c.request.url for c in responses.calls] == [
        _webfinger_url("http", domain),
        actor,
    ]


@responses.activate
@pytest.mark.django_db
def test_find_no_port_tries_https_first(client, remote_keypair):
    User.objects.create_user(localname="bob", password="p")
    _private_pem, public_pem = remote_keypair
    # Both schemes registered — the order is what's under test.
    responses.add(
        responses.GET,
        f"https://{REMOTE_DOMAIN}/.well-known/webfinger",
        json=_webfinger_jrd(REMOTE_ACTOR, REMOTE_DOMAIN),
    )
    responses.add(
        responses.GET,
        f"http://{REMOTE_DOMAIN}/.well-known/webfinger",
        json=_webfinger_jrd(REMOTE_ACTOR, REMOTE_DOMAIN),
    )
    responses.add(
        responses.GET, REMOTE_ACTOR, json=_person_doc(REMOTE_ACTOR, public_pem)
    )
    client.login(username="bob", password="p")

    response = client.post("/find/", {"q": f"carol@{REMOTE_DOMAIN}"})

    assert response.status_code == 302
    assert responses.calls[0].request.url.startswith("https://")


@responses.activate
@pytest.mark.django_db
def test_find_no_port_falls_back_to_http(client, remote_keypair):
    User.objects.create_user(localname="bob", password="p")
    _private_pem, public_pem = remote_keypair
    responses.add(
        responses.GET,
        f"https://{REMOTE_DOMAIN}/.well-known/webfinger",
        body=requests.exceptions.ConnectionError(),
    )
    responses.add(
        responses.GET,
        f"http://{REMOTE_DOMAIN}/.well-known/webfinger",
        json=_webfinger_jrd(REMOTE_ACTOR, REMOTE_DOMAIN),
    )
    responses.add(
        responses.GET, REMOTE_ACTOR, json=_person_doc(REMOTE_ACTOR, public_pem)
    )
    client.login(username="bob", password="p")

    response = client.post("/find/", {"q": f"carol@{REMOTE_DOMAIN}"})

    assert response.status_code == 302
    mirror = User.objects.get(local=False)
    # The self link (an https URL) is authoritative even though the webfinger
    # answer came over http.
    assert mirror.actor_url == REMOTE_ACTOR
