"""ActivityPub inbox core + remote fetch + mirrors (M4 increment 4).

The inbound delivery pipeline: keyid extraction, sender resolution (local
users by request-host match on the R40 actor path; remote users mirrored
from their Person document on first contact), dual-format signature
verification, dedup by activity wire id, and graceful ignoring of unknown
activity types.
"""

import base64
import hashlib
import json
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest
import requests
import responses
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.test import RequestFactory

from reeltalk.activitypub import crypto, signatures
from reeltalk.activitypub.inbox import process_inbound_activity
from reeltalk.activitypub.mirrors import (
    RemoteFetchError,
    fetch_person_document,
    mirror_user_from_person,
    resolve_sender,
)
from reeltalk.activitypub.models import DeliveredActivity
from reeltalk.core.models import Film, Status
from reeltalk.social.models import User

REMOTE_ACTOR = "https://remote.example/user/carol/"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"


@pytest.fixture()
def remote_keypair():
    return crypto.generate_keypair()


@pytest.fixture(scope="module")
def rsa_keypair_pem():
    """An RSA pair for the old-draft format (what current Mastodon sends)."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture()
def person_doc(remote_keypair):
    _private_pem, public_pem = remote_keypair
    return {
        "@context": ["https://www.w3.org/ns/activitystreams"],
        "id": REMOTE_ACTOR,
        "type": "Person",
        "preferredUsername": "carol",
        "name": "Carol Remote",
        "inbox": REMOTE_ACTOR + "inbox/",
        "outbox": REMOTE_ACTOR + "outbox/",
        "publicKey": {
            "id": REMOTE_KEY_ID,
            "owner": REMOTE_ACTOR,
            "publicKeyPem": public_pem,
        },
    }


def _request_with_host():
    """A bare request carrying the test-server Host header."""
    request = RequestFactory().get("/")
    request.META["HTTP_HOST"] = "testserver"
    return request


def _signed_post(path: str, body: bytes, private_pem: str, key_id=REMOTE_KEY_ID):
    """RFC 9421 signature headers for a POST to ``http://testserver{path}``."""
    return signatures.sign_request(
        "POST", f"http://testserver{path}", private_pem, key_id=key_id, body=body
    )


def _post_inbox(client, path: str, body: bytes, headers: dict):
    meta = {}
    for name, value in headers.items():
        meta[f"HTTP_{name.upper().replace('-', '_')}"] = value
    meta["HTTP_HOST"] = "testserver"
    return client.post(
        path, data=body, content_type="application/activity+json", **meta
    )


def _legacy_signed_post(
    path: str, body: bytes, rsa_private_pem: str, key_id=REMOTE_KEY_ID
):
    """Old-draft (rsa-sha256) signature headers — what current Mastodon sends."""
    date = format_datetime(datetime.now(UTC), usegmt=True)
    digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()
    lines = [
        f"(request-target): post {path}",
        "host: testserver",
        f"date: {date}",
        f"digest: {digest}",
    ]
    key = serialization.load_pem_private_key(rsa_private_pem.encode(), password=None)
    signature = key.sign("\n".join(lines).encode(), padding.PKCS1v15(), hashes.SHA256())
    return {
        "Date": date,
        "Digest": digest,
        "Signature": ",".join(
            [
                f'keyId="{key_id}"',
                'algorithm="rsa-sha256"',
                'headers="(request-target) host date digest"',
                f'signature="{base64.b64encode(signature).decode()}"',
            ]
        ),
    }


# --- fetch_person_document ---------------------------------------------------


@responses.activate
def test_fetch_person_document_success(person_doc):
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    doc = fetch_person_document(REMOTE_ACTOR)
    assert doc["id"] == REMOTE_ACTOR
    # The request asks for the AP media type and identifies the software.
    sent = responses.calls[0].request
    assert sent.headers["Accept"] == "application/activity+json"
    assert sent.headers["User-Agent"].startswith("reeltalk/")


@responses.activate
def test_fetch_person_document_404():
    responses.add(responses.GET, REMOTE_ACTOR, status=404)
    with pytest.raises(RemoteFetchError):
        fetch_person_document(REMOTE_ACTOR)


@responses.activate
def test_fetch_person_document_rejects_non_json():
    responses.add(responses.GET, REMOTE_ACTOR, body="<html>not json</html>")
    with pytest.raises(RemoteFetchError):
        fetch_person_document(REMOTE_ACTOR)


@responses.activate
def test_fetch_person_document_rejects_wrong_type(person_doc):
    person_doc["type"] = "Organization"
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    with pytest.raises(RemoteFetchError):
        fetch_person_document(REMOTE_ACTOR)


@responses.activate
def test_fetch_person_document_rejects_missing_public_key(person_doc):
    del person_doc["publicKey"]
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    with pytest.raises(RemoteFetchError):
        fetch_person_document(REMOTE_ACTOR)


@responses.activate
def test_fetch_person_document_rejects_network_error():
    # A connection-level failure surfaces as RemoteFetchError too. It must be
    # a requests exception for the client's ``except requests.RequestException``
    # to catch it (the builtin ConnectionError would escape).
    responses.add(
        responses.GET, REMOTE_ACTOR, body=requests.exceptions.ConnectionError("refused")
    )
    with pytest.raises(RemoteFetchError):
        fetch_person_document(REMOTE_ACTOR)


def test_fetch_person_document_rejects_bad_scheme():
    with pytest.raises(RemoteFetchError):
        fetch_person_document("ftp://remote.example/user/carol/")


# --- mirror_user_from_person -------------------------------------------------


@pytest.mark.django_db
def test_mirror_created_from_person_doc(person_doc):
    user = mirror_user_from_person(person_doc)
    assert user.local is False
    assert user.actor_url == REMOTE_ACTOR
    assert user.public_key == person_doc["publicKey"]["publicKeyPem"]
    # Mirrors carry no private key — their home instance signs for them.
    assert user.private_key == ""
    assert user.display_name == "Carol Remote"
    # <preferredUsername>@<netloc> — disjoint from the local charset (R12).
    assert user.localname == "carol@remote.example"
    # No default shelves for mirrors (R15: local users only).
    assert user.shelves.count() == 0


@pytest.mark.django_db
def test_mirror_is_idempotent_per_actor_url(person_doc):
    first = mirror_user_from_person(person_doc)
    second = mirror_user_from_person(person_doc)
    assert first.pk == second.pk
    assert User.objects.filter(local=False).count() == 1


@pytest.mark.django_db
def test_mirrors_from_different_domains_get_distinct_handles(person_doc):
    other = dict(person_doc, id="https://other.example/user/carol/")
    a = mirror_user_from_person(person_doc)
    b = mirror_user_from_person(other)
    assert a.localname == "carol@remote.example"
    assert b.localname == "carol@other.example"


@pytest.mark.django_db
def test_mirror_localname_falls_back_to_path_segment(person_doc):
    doc = {k: v for k, v in person_doc.items() if k != "preferredUsername"}
    user = mirror_user_from_person(doc)
    assert user.localname == "carol@remote.example"


@pytest.mark.django_db
def test_mirror_without_public_key_rejected(person_doc):
    doc = dict(person_doc)
    doc["publicKey"] = {"id": REMOTE_KEY_ID, "owner": REMOTE_ACTOR}
    with pytest.raises(RemoteFetchError):
        mirror_user_from_person(doc)


# --- resolve_sender ----------------------------------------------------------


@pytest.mark.django_db
def test_resolve_sender_local_actor_url():
    User.objects.create_user(localname="alice", password="p")
    key_id = "http://testserver/user/alice/#main-key"
    user = resolve_sender(key_id, _request_with_host())
    assert user is not None
    assert user.local and user.localname == "alice"


@pytest.mark.django_db
def test_resolve_sender_local_actor_unknown_user_returns_none():
    # An actor on this instance that is not a local user: no self-fetch.
    assert (
        resolve_sender("http://testserver/user/nobody/#main-key", _request_with_host())
        is None
    )


@pytest.mark.django_db
def test_resolve_sender_local_actor_non_user_path_returns_none():
    # A keyid pointing at our own film URL is not a local user.
    assert (
        resolve_sender("http://testserver/film/8/#main-key", _request_with_host())
        is None
    )


@responses.activate
@pytest.mark.django_db
def test_resolve_sender_first_contact_fetches_and_mirrors(person_doc):
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    user = resolve_sender(REMOTE_KEY_ID, _request_with_host())
    assert user is not None and user.localname == "carol@remote.example"
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.django_db
def test_resolve_sender_second_contact_does_not_refetch(person_doc):
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    first = resolve_sender(REMOTE_KEY_ID, _request_with_host())
    second = resolve_sender(REMOTE_KEY_ID, _request_with_host())
    assert first.pk == second.pk
    # The Person document is fetched once, on first contact only.
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.django_db
def test_resolve_sender_unreachable_remote_returns_none():
    responses.add(responses.GET, REMOTE_ACTOR, status=500)
    assert resolve_sender(REMOTE_KEY_ID, _request_with_host()) is None


# --- process_inbound_activity (dedup + dispatch) -----------------------------


@pytest.mark.django_db
def test_process_records_delivered_activity():
    result = process_inbound_activity(
        {"id": "https://remote.example/activity/1", "type": "Follow"}
    )
    # No handlers are registered in increment 4 — accepted, recorded, ignored.
    assert result == "ignored"
    assert DeliveredActivity.objects.count() == 1


@pytest.mark.django_db
def test_process_duplicate_is_not_reprocessed():
    activity = {"id": "https://remote.example/activity/1", "type": "Follow"}
    assert process_inbound_activity(activity) == "ignored"
    assert process_inbound_activity(activity) == "duplicate"
    assert DeliveredActivity.objects.count() == 1


@pytest.mark.django_db
def test_process_unknown_types_ignored_gracefully():
    # Types we will never support (and non-string shapes) are ignored without
    # raising and without creating content.
    for type_name in ("Announce", "Add", "Book", 42, ["Follow"], None):
        result = process_inbound_activity(
            {"id": f"https://remote.example/activity/{type_name!s}", "type": type_name}
        )
        assert result == "ignored"


@pytest.mark.django_db
def test_process_non_dict_ignored_without_record():
    assert process_inbound_activity("not an object") == "ignored"
    assert process_inbound_activity(None) == "ignored"
    assert DeliveredActivity.objects.count() == 0


# --- extract_key_id ----------------------------------------------------------


def _bare_post_request(headers: dict | None = None):
    """A bare POST request.

    RequestFactory's ``post`` defaults to a multipart content type, which
    would try to encode the raw bytes as form data — name it explicitly.
    """
    request = RequestFactory().post(
        "/inbox/", data=b"{}", content_type="application/activity+json"
    )
    for name, value in (headers or {}).items():
        request.META[f"HTTP_{name.upper().replace('-', '_')}"] = value
    return request


def test_extract_key_id_rfc9421(remote_keypair):
    private_pem, _public_pem = remote_keypair
    headers = signatures.sign_request(
        "POST",
        "http://testserver/inbox/",
        private_pem,
        key_id=REMOTE_KEY_ID,
        body=b"{}",
    )
    request = _bare_post_request(headers)
    assert signatures.extract_key_id(request) == REMOTE_KEY_ID


def test_extract_key_id_legacy_format(rsa_keypair_pem):
    private_pem, _public_pem = rsa_keypair_pem
    headers = _legacy_signed_post("/inbox/", b"{}", private_pem)
    request = _bare_post_request(headers)
    assert signatures.extract_key_id(request) == REMOTE_KEY_ID


def test_extract_key_id_none_without_signature():
    request = _bare_post_request()
    assert signatures.extract_key_id(request) is None


# --- Inbox views: the delivery pipeline --------------------------------------


@pytest.mark.django_db
def test_inbox_rejects_unsigned_post(client):
    User.objects.create_user(localname="alice", password="p")
    response = client.post(
        "/user/alice/inbox/", data=b"{}", content_type="application/activity+json"
    )
    assert response.status_code == 401


@responses.activate
@pytest.mark.django_db
def test_inbox_first_contact_mirrors_sender_and_accepts(
    client, remote_keypair, person_doc
):
    User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    private_pem, _public_pem = remote_keypair
    activity = {
        "id": "https://remote.example/activity/10",
        "type": "Follow",
        "actor": REMOTE_ACTOR,
    }
    body = json.dumps(activity).encode()
    headers = _signed_post("/user/alice/inbox/", body, private_pem)
    response = _post_inbox(client, "/user/alice/inbox/", body, headers)
    assert response.status_code == 202
    # The sender was mirrored from their Person document (public key stored).
    mirror = User.objects.get(local=False)
    assert mirror.actor_url == REMOTE_ACTOR
    assert mirror.public_key == person_doc["publicKey"]["publicKeyPem"]
    assert DeliveredActivity.objects.count() == 1


@responses.activate
@pytest.mark.django_db
def test_inbox_duplicate_delivery_is_idempotent(client, remote_keypair, person_doc):
    User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    private_pem, _public_pem = remote_keypair
    activity = {"id": "https://remote.example/activity/11", "type": "Follow"}
    body = json.dumps(activity).encode()
    for _ in range(2):
        headers = _signed_post("/user/alice/inbox/", body, private_pem)
        response = _post_inbox(client, "/user/alice/inbox/", body, headers)
        assert response.status_code == 202
    # One dedup row for both deliveries.
    assert DeliveredActivity.objects.count() == 1


@pytest.mark.django_db
def test_inbox_rejects_invalid_signature(client, person_doc):
    User.objects.create_user(localname="alice", password="p")
    mirror_user_from_person(person_doc)  # carol's mirror exists...
    attacker_private, _public = crypto.generate_keypair()
    # ...but the delivery is signed with a different key.
    activity = {"id": "https://remote.example/activity/12", "type": "Follow"}
    body = json.dumps(activity).encode()
    headers = _signed_post("/user/alice/inbox/", body, attacker_private)
    response = _post_inbox(client, "/user/alice/inbox/", body, headers)
    assert response.status_code == 401
    assert DeliveredActivity.objects.count() == 0


@responses.activate
@pytest.mark.django_db
def test_inbox_rejects_unresolvable_sender(client):
    # No mirror and an unreachable remote: the delivery is rejected before
    # anything is parsed or recorded.
    User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, status=500)
    private_pem, _public_pem = crypto.generate_keypair()
    activity = {"id": "https://remote.example/activity/13", "type": "Follow"}
    body = json.dumps(activity).encode()
    headers = _signed_post("/user/alice/inbox/", body, private_pem)
    response = _post_inbox(client, "/user/alice/inbox/", body, headers)
    assert response.status_code == 401
    assert DeliveredActivity.objects.count() == 0


@responses.activate
@pytest.mark.django_db
def test_inbox_rejects_malformed_json_body(client, remote_keypair, person_doc):
    User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    private_pem, _public_pem = remote_keypair
    body = b"this is not json"
    headers = _signed_post("/user/alice/inbox/", body, private_pem)
    response = _post_inbox(client, "/user/alice/inbox/", body, headers)
    assert response.status_code == 400


@responses.activate
@pytest.mark.django_db
def test_inbox_accepts_legacy_format_signature(client, rsa_keypair_pem):
    # A Mastodon-style sender: RSA key + old-draft signature format.
    User.objects.create_user(localname="alice", password="p")
    private_pem, public_pem = rsa_keypair_pem
    doc = {
        "id": REMOTE_ACTOR,
        "type": "Person",
        "preferredUsername": "carol",
        "publicKey": {
            "id": REMOTE_KEY_ID,
            "owner": REMOTE_ACTOR,
            "publicKeyPem": public_pem,
        },
    }
    responses.add(responses.GET, REMOTE_ACTOR, json=doc)
    activity = {"id": "https://remote.example/activity/14", "type": "Follow"}
    body = json.dumps(activity).encode()
    headers = _legacy_signed_post("/user/alice/inbox/", body, private_pem)
    response = _post_inbox(client, "/user/alice/inbox/", body, headers)
    assert response.status_code == 202
    mirror = User.objects.get(local=False)
    assert mirror.public_key == public_pem


@pytest.mark.django_db
def test_inbox_accepts_local_sender_signature(client):
    # A delivery signed by one of our own users (self-delivery robustness):
    # the keyid resolves locally, no fetch involved.
    alice = User.objects.create_user(localname="alice", password="p")
    body = json.dumps(
        {"id": "http://testserver/user/alice/outbox/#activity-1"}
    ).encode()
    headers = signatures.sign_request(
        "POST",
        "http://testserver/inbox/",
        alice.private_key,
        key_id="http://testserver/user/alice/#main-key",
        body=body,
    )
    response = _post_inbox(client, "/inbox/", body, headers)
    assert response.status_code == 202


@responses.activate
@pytest.mark.django_db
def test_shared_inbox_processes_like_user_inbox(client, remote_keypair, person_doc):
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    private_pem, _public_pem = remote_keypair
    activity = {"id": "https://remote.example/activity/15", "type": "Follow"}
    body = json.dumps(activity).encode()
    headers = _signed_post("/inbox/", body, private_pem)
    response = _post_inbox(client, "/inbox/", body, headers)
    assert response.status_code == 202
    assert User.objects.filter(local=False).count() == 1


@responses.activate
@pytest.mark.django_db
def test_inbox_unknown_type_from_known_sender_ignored_gracefully(
    client, remote_keypair, person_doc
):
    # A type we will never support from an authenticated sender: accepted,
    # deduped, no content created.
    User.objects.create_user(localname="alice", password="p")
    responses.add(responses.GET, REMOTE_ACTOR, json=person_doc)
    private_pem, _public_pem = remote_keypair
    activity = {
        "id": "https://remote.example/activity/16",
        "type": "Announce",
        "object": {"id": "https://remote.example/book/9", "type": "Book"},
    }
    body = json.dumps(activity).encode()
    headers = _signed_post("/user/alice/inbox/", body, private_pem)
    response = _post_inbox(client, "/user/alice/inbox/", body, headers)
    assert response.status_code == 202
    assert DeliveredActivity.objects.count() == 1
    # Graceful ignore means no content was created.
    assert Film.objects.count() == 0
    assert Status.objects.count() == 0
