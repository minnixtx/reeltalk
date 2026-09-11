"""ActivityPub identity + discovery tests (M4 increment 2, R40).

The Person wire document, the actor URL convention with content
negotiation, webfinger, nodeinfo, and the key backfill for local users
created before the key fields existed.
"""

from io import BytesIO

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import RequestFactory
from PIL import Image

from reeltalk.activitypub import crypto
from reeltalk.activitypub.identity import person_document
from reeltalk.social.models import SiteSettings

User = get_user_model()


def _tiny_jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (10, 10), (120, 80, 40)).save(buf, format="JPEG")
    return buf.getvalue()


# --- Person document ---------------------------------------------------------


@pytest.mark.django_db
def test_person_document_shape():
    user = User.objects.create_user(localname="alice", password="p")
    request = RequestFactory().get("/user/alice/")
    doc = person_document(user, request)
    actor = "http://testserver/user/alice/"
    assert doc["@context"] == [
        "https://www.w3.org/ns/activitystreams",
        "https://w3id.org/security/v1",
    ]
    assert doc["id"] == actor
    assert doc["type"] == "Person"
    assert doc["preferredUsername"] == "alice"
    # No display name yet — the localname stands in.
    assert doc["name"] == "alice"
    assert doc["url"] == actor
    assert doc["inbox"] == actor + "inbox/"
    assert doc["outbox"] == actor + "outbox/"
    assert doc["followers"] == actor + "followers/"
    assert doc["following"] == actor + "following/"
    assert doc["endpoints"]["sharedInbox"] == "http://testserver/inbox/"
    # The keyid everything else hangs off (R39): actor URL + #main-key.
    assert doc["publicKey"] == {
        "id": actor + "#main-key",
        "owner": actor,
        "publicKeyPem": user.public_key,
    }
    # Empty optional fields are omitted, not null.
    assert "summary" not in doc
    assert "image" not in doc


def test_person_document_name_and_summary():
    user = User(localname="alice", display_name="Alice A.", summary="<p>hi</p>")
    request = RequestFactory().get("/")
    doc = person_document(user, request)
    assert doc["name"] == "Alice A."
    assert doc["summary"] == "<p>hi</p>"


@pytest.mark.django_db
def test_person_document_image():
    user = User.objects.create_user(localname="alice", password="p")
    user.avatar.save("a.jpg", ContentFile(_tiny_jpeg()), save=True)
    request = RequestFactory().get("/")
    doc = person_document(user, request)
    assert doc["image"] == "http://testserver" + user.avatar.url


# --- Actor endpoint (content negotiation) -------------------------------------


@pytest.mark.django_db
def test_actor_endpoint_serves_person_to_ap_client(client):
    User.objects.create_user(localname="alice", password="p")
    response = client.get("/user/alice/", HTTP_ACCEPT="application/activity+json")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/activity+json"
    doc = response.json()
    assert doc["id"] == "http://testserver/user/alice/"
    assert doc["publicKey"]["id"] == "http://testserver/user/alice/#main-key"


@pytest.mark.django_db
def test_actor_endpoint_accepts_ld_json(client):
    User.objects.create_user(localname="alice", password="p")
    response = client.get("/user/alice/", HTTP_ACCEPT="application/ld+json")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/activity+json"


@pytest.mark.django_db
def test_actor_endpoint_mastodon_style_accept(client):
    # The real header shape current Mastodon sends: AP first, HTML low-q.
    User.objects.create_user(localname="alice", password="p")
    response = client.get(
        "/user/alice/",
        HTTP_ACCEPT=(
            "application/activity+json, application/ld+json; profile="
            '"https://www.w3.org/ns/activitystreams", text/html; q=0.1'
        ),
    )
    assert response.status_code == 200
    assert response.json()["type"] == "Person"


@pytest.mark.django_db
def test_actor_endpoint_redirects_browsers(client):
    User.objects.create_user(localname="alice", password="p")
    response = client.get("/user/alice/", HTTP_ACCEPT="text/html,*/*;q=0.8")
    assert response.status_code == 302
    assert response["Location"] == "/user/alice/films/"


@pytest.mark.django_db
def test_actor_endpoint_wildcard_accept_is_not_json(client):
    # A bare */* (browser default) must not receive the JSON document.
    User.objects.create_user(localname="alice", password="p")
    response = client.get("/user/alice/", HTTP_ACCEPT="*/*")
    assert response.status_code == 302


@pytest.mark.django_db
def test_actor_endpoint_unknown_user_404(client):
    response = client.get("/user/nobody/", HTTP_ACCEPT="application/activity+json")
    assert response.status_code == 404


@pytest.mark.django_db
def test_actor_endpoint_remote_mirror_404(client):
    # Mirrors of remote actors are not served from this instance.
    mirror = User(localname="remote-alice", local=False)
    mirror.set_unusable_password()
    mirror.save()
    response = client.get(
        "/user/remote-alice/", HTTP_ACCEPT="application/activity+json"
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_actor_endpoint_follows_forwarded_proto(client):
    User.objects.create_user(localname="alice", password="p")
    response = client.get(
        "/user/alice/",
        HTTP_ACCEPT="application/activity+json",
        HTTP_X_FORWARDED_PROTO="https",
    )
    assert response.json()["id"] == "https://testserver/user/alice/"


@pytest.mark.django_db
def test_actor_endpoint_localname_case_insensitive(client):
    User.objects.create_user(localname="Alice", password="p")
    response = client.get("/user/alice/", HTTP_ACCEPT="application/activity+json")
    assert response.status_code == 200
    # The stored spelling is canonical in the document.
    assert response.json()["preferredUsername"] == "Alice"


# --- Webfinger (RFC 6454) -----------------------------------------------------


@pytest.mark.django_db
def test_webfinger_resolves_local_user(client):
    User.objects.create_user(localname="alice", password="p")
    response = client.get(
        "/.well-known/webfinger", {"resource": "acct:alice@localhost"}
    )
    assert response.status_code == 200
    assert response["Content-Type"] == "application/jrd+json"
    doc = response.json()
    actor = "http://testserver/user/alice/"
    assert doc["subject"] == "acct:alice@localhost"
    assert doc["aliases"] == [actor]
    rels = {link["rel"]: link for link in doc["links"]}
    assert rels["self"] == {
        "rel": "self",
        "type": "application/activity+json",
        "href": actor,
    }
    assert rels["http://webfinger.net/rel/profile-page"] == {
        "rel": "http://webfinger.net/rel/profile-page",
        "type": "text/html",
        "href": actor,
    }
    lrdd = rels["lrdd"]
    assert lrdd["type"] == "application/link-descriptions+json"
    assert lrdd["template"] == (
        "http://testserver/.well-known/webfinger?resource={uri}"
    )


@pytest.mark.django_db
def test_webfinger_localname_case_insensitive(client):
    User.objects.create_user(localname="Alice", password="p")
    response = client.get(
        "/.well-known/webfinger", {"resource": "acct:alice@localhost"}
    )
    assert response.status_code == 200
    # The stored spelling is carried back — remotes learn the canonical case.
    assert response.json()["subject"] == "acct:Alice@localhost"


@pytest.mark.django_db
def test_webfinger_wrong_domain_404(client):
    User.objects.create_user(localname="alice", password="p")
    response = client.get(
        "/.well-known/webfinger", {"resource": "acct:alice@elsewhere.social"}
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_webfinger_unknown_user_404(client):
    response = client.get(
        "/.well-known/webfinger", {"resource": "acct:nobody@localhost"}
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "query",
    [
        {"resource": ""},
        {"resource": "https://testserver/user/alice/"},
        {"resource": "acct:alice"},  # no domain
        {},  # missing resource entirely
    ],
)
def test_webfinger_malformed_resource_404(client, query):
    response = client.get("/.well-known/webfinger", query)
    assert response.status_code == 404


# --- NodeInfo ------------------------------------------------------------------


def test_nodeinfo_index_links_to_2_0(client):
    response = client.get("/.well-known/nodeinfo")
    assert response.status_code == 200
    assert response.json() == {
        "links": [
            {
                "rel": "http://nodeinfo.digip.org/spec/2.0",
                "href": "http://testserver/nodeinfo/2.0",
            }
        ]
    }


@pytest.mark.django_db
def test_nodeinfo_2_0_document(client):
    from reeltalk import __version__

    response = client.get("/nodeinfo/2.0")
    assert response.status_code == 200
    doc = response.json()
    assert doc["version"] == "2.0"
    assert doc["software"]["name"] == "reeltalk"
    assert doc["software"]["version"] == __version__
    assert doc["protocols"] == ["activitypub"]
    # The default signup policy is open.
    assert doc["openRegistration"] is True


@pytest.mark.django_db
def test_nodeinfo_open_registration_follows_signup_policy(client):
    site = SiteSettings.get_instance()
    site.signup_policy = SiteSettings.INVITE
    site.save()
    response = client.get("/nodeinfo/2.0")
    assert response.json()["openRegistration"] is False


# --- Key backfill (users created before the key fields) -------------------------


@pytest.mark.django_db
def test_ensure_keypair_generates_when_missing():
    user = User.objects.create_user(localname="alice", password="p")
    # Simulate a pre-key-field account: the row exists but carries no keys.
    user.private_key = ""
    user.public_key = ""
    user.save()
    assert user.ensure_keypair() is True
    user.refresh_from_db()
    assert user.private_key and user.public_key
    private = crypto.load_private_key(user.private_key)
    assert crypto.load_public_key(user.public_key) == private.public_key()


@pytest.mark.django_db
def test_ensure_keypair_noop_when_present():
    user = User.objects.create_user(localname="alice", password="p")
    original = (user.private_key, user.public_key)
    assert user.ensure_keypair() is False
    user.refresh_from_db()
    assert (user.private_key, user.public_key) == original


@pytest.mark.django_db
def test_ensure_keypair_remote_mirror_not_touched():
    mirror = User(localname="remote-alice", local=False)
    mirror.set_unusable_password()
    mirror.public_key = "fetched-pem"
    mirror.save()
    assert mirror.ensure_keypair() is False
    mirror.refresh_from_db()
    assert mirror.private_key == ""
    assert mirror.public_key == "fetched-pem"
