"""S5: inbox hardening (F7 body bound + per-source floor, F8 body coverage).

Three controls, each driven through the real inbox route so the middleware
chain runs and the ordering under test is the production ordering:

* the per-route ceiling on one inbound body (F7),
* the per-source floor on POSTs that cost us an unauthenticated fetch (F7),
* the requirement that a signature actually cover the body it delivers (F8).

Every refusal is paired with its honest inverse in this file. A bound that
refused everything would pass every "refused" assertion, so the non-vacuity
half is not a courtesy: for each control there is a case that must still get
through, and if it stops getting through, a test here goes red.
"""

import base64
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import responses
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.core.management import CommandError, call_command
from django.test import RequestFactory
from django.utils import timezone

from reeltalk.activitypub import crypto, inbox_throttle, signatures
from reeltalk.activitypub.crypto import load_private_key
from reeltalk.activitypub.mirrors import mirror_user_from_person
from reeltalk.activitypub.models import DeliveredActivity, InboxAttempt
from reeltalk.activitypub.views import _inbox_too_large
from reeltalk.social.models import User

REMOTE_ACTOR = "https://remote.example/user/carol/"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"
INBOX_PATH = "/user/alice/inbox/"
# Outside the shipped TRUSTED_PROXIES default (the private ranges), so
# client_ip() resolves to exactly this address rather than to a forwarded
# header — which is the axis the floor claims to be keyed on.
PUBLIC_PEER = "203.0.113.7"
OTHER_PUBLIC_PEER = "203.0.113.8"


@pytest.fixture()
def remote_keypair():
    return crypto.generate_keypair()


@pytest.fixture()
def rsa_keypair_pem():
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
        "inbox": REMOTE_ACTOR + "inbox/",
        "outbox": REMOTE_ACTOR + "outbox/",
        "publicKey": {
            "id": REMOTE_KEY_ID,
            "owner": REMOTE_ACTOR,
            "publicKeyPem": public_pem,
        },
    }


def _mirror_ed25519(person_doc):
    mirror_user_from_person(person_doc)


def _mirror_rsa(rsa_keypair_pem):
    _private_pem, public_pem = rsa_keypair_pem
    mirror_user_from_person(
        {
            "id": REMOTE_ACTOR,
            "type": "Person",
            "preferredUsername": "carol",
            "inbox": REMOTE_ACTOR + "inbox/",
            "outbox": REMOTE_ACTOR + "outbox/",
            "publicKey": {
                "id": REMOTE_KEY_ID,
                "owner": REMOTE_ACTOR,
                "publicKeyPem": public_pem,
            },
        }
    )


def _post(client, body: bytes, headers: dict, **extra):
    meta = {"HTTP_HOST": "testserver"}
    for name, value in headers.items():
        meta[f"HTTP_{name.upper().replace('-', '_')}"] = value
    meta.update(extra)
    return client.post(
        INBOX_PATH, data=body, content_type="application/activity+json", **meta
    )


def _follow_body(activity_id: str) -> bytes:
    return json.dumps(
        {"id": activity_id, "type": "Follow", "actor": REMOTE_ACTOR}
    ).encode()


def _signed_follow(client, private_pem, activity_id, **extra):
    """One honest RFC 9421 delivery: covers the body, signed by ``private_pem``."""
    body = _follow_body(activity_id)
    headers = signatures.sign_request(
        "POST",
        f"http://testserver{INBOX_PATH}",
        private_pem,
        key_id=REMOTE_KEY_ID,
        body=body,
    )
    return body, _post(client, body, headers, **extra)


def _rfc9421_without_body_coverage(private_pem: str) -> dict:
    """A cryptographically valid RFC 9421 signature that omits the body (F8).

    Written out rather than derived from ``sign_request`` so the test owns the
    wire shape: this is exactly what a peer signing only ``@method`` and
    ``@target-uri`` sends. The signature verifies — the refusal has to come
    from the missing coverage and from nothing else, or this test would be
    proving that bad signatures are caught.
    """
    created = int(time.time())
    sig_input = f'("@method" "@target-uri");created={created};keyid="{REMOTE_KEY_ID}"'
    base = (
        f'"@method": POST\n'
        f'"@target-uri": http://testserver{INBOX_PATH}\n'
        f'"@signature-params": {sig_input}'
    ).encode()
    signature = load_private_key(private_pem).sign(base)
    return {
        "Signature-Input": f"sig1={sig_input}",
        "Signature": "sig1=:" + base64.b64encode(signature).decode() + ":",
    }


def _legacy_headers(body: bytes, rsa_private_pem: str, *, with_digest: bool) -> dict:
    """An old-draft (Mastodon) signature, optionally leaving ``digest`` out."""
    date = format_datetime(datetime.now(UTC), usegmt=True)
    listed = ["(request-target)", "host", "date"]
    lines = [
        f"(request-target): post {INBOX_PATH}",
        "host: testserver",
        f"date: {date}",
    ]
    out = {"Date": date}
    if with_digest:
        digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()
        listed.append("digest")
        lines.append(f"digest: {digest}")
        out["Digest"] = digest
    key = serialization.load_pem_private_key(rsa_private_pem.encode(), password=None)
    signature = key.sign("\n".join(lines).encode(), padding.PKCS1v15(), hashes.SHA256())
    out["Signature"] = ",".join(
        [
            f'keyId="{REMOTE_KEY_ID}"',
            'algorithm="rsa-sha256"',
            f'headers="{" ".join(listed)}"',
            f'signature="{base64.b64encode(signature).decode()}"',
        ]
    )
    return out


# --- F7: the per-route ceiling on one inbound body ---------------------------


@responses.activate
@pytest.mark.django_db
def test_inbox_refuses_an_oversized_body_before_fetching_anything(client, monkeypatch):
    # The bound must precede the dial-out or it bounds nothing: the whole cost
    # of an unknown-keyid POST is the Person fetch it induces. The keyid below
    # is unresolvable, so if the bound did not fire first this request would
    # reach resolve_sender and answer 401 after trying to dial out.
    from django.conf import settings as dj_settings

    monkeypatch.setattr(dj_settings, "INBOX_MAX_BODY_BYTES", 200)
    User.objects.create_user(localname="alice", password="p")
    body = json.dumps(
        {"id": "https://remote.example/activity/big", "type": "Follow", "p": "x" * 400}
    ).encode()
    response = _post(
        client,
        body,
        {"Signature": f'keyId="{REMOTE_KEY_ID}", signature="abc"'},
        REMOTE_ADDR=PUBLIC_PEER,
    )
    assert response.status_code == 413
    # Nothing was dialed: the refusal happened before the expensive step.
    assert len(responses.calls) == 0


@pytest.mark.django_db
def test_inbox_still_accepts_a_body_under_the_bound(client, remote_keypair, person_doc):
    # Non-vacuity: the ceiling must not be a wall. Same route, honest size.
    User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, REMOTE_ACTOR, json=person_doc)
        _body, response = _signed_follow(
            client,
            private_pem,
            "https://remote.example/activity/under",
            REMOTE_ADDR=PUBLIC_PEER,
        )
    assert response.status_code == 202


@pytest.mark.django_db
def test_declared_length_cannot_hide_a_large_body(monkeypatch):
    # A caller declaring 10 bytes and sending 100 KB is the case the header
    # check alone cannot catch, so the real bytes are checked too. Built at the
    # request level because the test client derives Content-Length from the
    # body and so cannot express the lie.
    from django.conf import settings as dj_settings

    monkeypatch.setattr(dj_settings, "INBOX_MAX_BODY_BYTES", 500)
    request = RequestFactory().post(
        INBOX_PATH, data=b"x" * 100_000, content_type="application/activity+json"
    )
    request.META["CONTENT_LENGTH"] = "10"
    assert _inbox_too_large(request) is True


@pytest.mark.django_db
def test_a_honest_body_within_the_bound_is_not_flagged():
    # The inverse of the two checks above, on the same helper: a declared
    # length and a body that both fit must read as fine.
    from django.conf import settings as dj_settings

    request = RequestFactory().post(
        INBOX_PATH, data=b"x" * 50, content_type="application/activity+json"
    )
    assert int(request.META["CONTENT_LENGTH"]) <= dj_settings.INBOX_MAX_BODY_BYTES
    assert _inbox_too_large(request) is False


# --- F7: the per-source floor ------------------------------------------------


@pytest.fixture()
def broken_peer_key(person_doc):
    """A mirror whose stored key is not the key signing the delivery.

    Chosen so the only thing failing is verification: the sender resolves, so
    the request reaches the expensive work and is refused after it — precisely
    the cost the floor exists to bound.
    """
    User.objects.create_user(localname="alice", password="p")
    _mirror_ed25519(person_doc)
    return crypto.generate_keypair()[0]


def _flood(client, private_pem, n: int, addr=PUBLIC_PEER):
    for i in range(n):
        _body, response = _signed_follow(
            client,
            private_pem,
            f"https://remote.example/activity/bad-{i}",
            REMOTE_ADDR=addr,
        )
        assert response.status_code == 401
    return response


@pytest.mark.django_db
def test_unauthenticated_posts_are_counted_against_their_address(
    client, broken_peer_key, monkeypatch
):
    monkeypatch.setattr(inbox_throttle, "INBOX_FAILURE_LIMIT", 3)
    _flood(client, broken_peer_key, 3)
    assert InboxAttempt.objects.filter(source_ip=PUBLIC_PEER).count() == 3


@pytest.mark.django_db
def test_a_spent_address_is_refused_with_a_retry_after(
    client, broken_peer_key, monkeypatch
):
    monkeypatch.setattr(inbox_throttle, "INBOX_FAILURE_LIMIT", 3)
    _flood(client, broken_peer_key, 3)
    _body, response = _signed_follow(
        client,
        broken_peer_key,
        "https://remote.example/activity/over",
        REMOTE_ADDR=PUBLIC_PEER,
    )
    assert response.status_code == 429
    assert int(response["Retry-After"]) >= 1
    # Refused before the work, and not counted: a blocked request must not
    # extend the window it is waiting out.
    assert InboxAttempt.objects.filter(source_ip=PUBLIC_PEER).count() == 3


@pytest.mark.django_db
def test_a_spent_address_does_not_block_another(client, broken_peer_key, monkeypatch):
    monkeypatch.setattr(inbox_throttle, "INBOX_FAILURE_LIMIT", 2)
    _flood(client, broken_peer_key, 2)
    _body, response = _signed_follow(
        client,
        broken_peer_key,
        "https://remote.example/activity/other",
        REMOTE_ADDR=OTHER_PUBLIC_PEER,
    )
    # Refused on its own merits (401), not by the floor — the budgets are
    # per address, so one spent caller cannot silence a different one.
    assert response.status_code == 401


@pytest.mark.django_db
def test_a_successful_delivery_costs_nothing_against_the_floor(
    client, remote_keypair, person_doc
):
    # The floor bounds work we were tricked into doing, not traffic we want. A
    # peer that signs correctly must be able to deliver all day without
    # spending anything — this is the assertion that the floor is not a
    # self-inflicted denial-of-service against honest federation.
    User.objects.create_user(localname="alice", password="p")
    private_pem, _public = remote_keypair
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, REMOTE_ACTOR, json=person_doc)
        for i in range(inbox_throttle.INBOX_FAILURE_LIMIT + 2):
            _body, response = _signed_follow(
                client,
                private_pem,
                f"https://remote.example/activity/ok-{i}",
                REMOTE_ADDR=PUBLIC_PEER,
            )
            assert response.status_code == 202
    assert InboxAttempt.objects.count() == 0


@pytest.mark.django_db
def test_an_unparseable_address_is_never_counted_and_never_blocks():
    # One shared empty bucket would let a single unparseable caller lock out
    # every other one with it: a denial-of-service the control invented.
    inbox_throttle.record_failure("")
    assert InboxAttempt.objects.count() == 0
    assert inbox_throttle.inbox_blocked("") is False


@pytest.mark.django_db
def test_a_counted_failure_survives_a_rolled_back_delivery(client):
    # The counter must live outside the dedup transaction. Counted inside it,
    # a handler that raised would erase the record of the attempt that had
    # just cost us the work, and the floor would never fill.
    inbox_throttle.record_failure(PUBLIC_PEER)
    from django.db import transaction

    with pytest.raises(RuntimeError), transaction.atomic():
        assert InboxAttempt.objects.filter(source_ip=PUBLIC_PEER).count() == 1
        raise RuntimeError("handler blew up")
    assert InboxAttempt.objects.filter(source_ip=PUBLIC_PEER).count() == 1


@pytest.mark.django_db
def test_a_missing_signature_is_not_counted(client):
    # A POST with no keyid costs nothing, so charging it would let noise spend
    # the budget a real peer needs.
    User.objects.create_user(localname="alice", password="p")
    response = client.post(
        INBOX_PATH, data=b"{}", content_type="application/activity+json"
    )
    assert response.status_code == 401
    assert InboxAttempt.objects.count() == 0


# --- F8: the signature must cover the body -----------------------------------


@pytest.mark.django_db
def test_rfc9421_post_without_body_coverage_is_refused(
    client, remote_keypair, person_doc
):
    # The signature is valid; it simply does not cover the body, so the bytes
    # could be swapped in flight and still verify. That is F8 exactly.
    #
    # Signed with the mirrored actor's own key on purpose. Signing with some
    # other key would make this pass for the wrong reason — a wrong-key
    # signature is refused with or without the coverage gate, so the test
    # would prove nothing about the gate it names.
    User.objects.create_user(localname="alice", password="p")
    _mirror_ed25519(person_doc)
    body = _follow_body("https://remote.example/activity/nocov")
    headers = _rfc9421_without_body_coverage(remote_keypair[0])
    response = _post(client, body, headers, REMOTE_ADDR=PUBLIC_PEER)
    assert response.status_code == 401


@pytest.mark.django_db
def test_rfc9421_post_covering_the_body_is_accepted(client, remote_keypair, person_doc):
    # Non-vacuity: same route, same actor, same body shape — the only
    # difference is that this signature covers the body, and it gets through.
    User.objects.create_user(localname="alice", password="p")
    _mirror_ed25519(person_doc)
    private_pem, _public = remote_keypair
    _body, response = _signed_follow(
        client,
        private_pem,
        "https://remote.example/activity/cov",
        REMOTE_ADDR=PUBLIC_PEER,
    )
    assert response.status_code == 202


@pytest.mark.django_db
def test_legacy_post_without_digest_is_refused(client, rsa_keypair_pem):
    # Mastodon's own verifier demands digest on POST, so refusing without it
    # matches the reference peer rather than diverging from it.
    User.objects.create_user(localname="alice", password="p")
    _mirror_rsa(rsa_keypair_pem)
    private_pem, _public = rsa_keypair_pem
    body = _follow_body("https://remote.example/activity/nodigest")
    headers = _legacy_headers(body, private_pem, with_digest=False)
    response = _post(client, body, headers, REMOTE_ADDR=PUBLIC_PEER)
    assert response.status_code == 401


@pytest.mark.django_db
def test_legacy_post_with_digest_is_accepted(client, rsa_keypair_pem):
    # The inverse, and the interop case that matters: a Mastodon-shaped
    # sender keeps working.
    User.objects.create_user(localname="alice", password="p")
    _mirror_rsa(rsa_keypair_pem)
    private_pem, _public = rsa_keypair_pem
    body = _follow_body("https://remote.example/activity/withdigest")
    headers = _legacy_headers(body, private_pem, with_digest=True)
    response = _post(client, body, headers, REMOTE_ADDR=PUBLIC_PEER)
    assert response.status_code == 202


def test_get_never_needs_body_coverage():
    # The rule is about bodies. A signed GET has none, and refusing it for
    # lacking a digest would be a new way to break federation, not security.
    private_pem, public_pem = crypto.generate_keypair()
    headers = signatures.sign_request(
        "GET", "http://example.com/user/alice/", private_pem, key_id=REMOTE_KEY_ID
    )
    request = RequestFactory().get("/user/alice/")
    request.META["HTTP_HOST"] = "example.com"
    for name, value in headers.items():
        request.META[f"HTTP_{name.upper().replace('-', '_')}"] = value
    assert signatures.verify_request(request, public_pem) is True


# --- F7: the retention prune --------------------------------------------------


@pytest.mark.django_db
def test_prune_deletes_only_rows_past_their_useful_life():
    old = DeliveredActivity.objects.create(
        activity_id="https://remote.example/activity/old",
        delivered_date=timezone.now() - timedelta(days=40),
    )
    fresh = DeliveredActivity.objects.create(
        activity_id="https://remote.example/activity/fresh"
    )
    attempt = InboxAttempt.objects.create(source_ip=PUBLIC_PEER)
    # auto_now_add wins on insert, so the age is set after the row exists.
    InboxAttempt.objects.filter(pk=attempt.pk).update(
        created_at=timezone.now() - timedelta(hours=2)
    )

    call_command("prune_inbox")

    assert not DeliveredActivity.objects.filter(pk=old.pk).exists()
    assert DeliveredActivity.objects.filter(pk=fresh.pk).exists()
    assert not InboxAttempt.objects.filter(pk=attempt.pk).exists()


@pytest.mark.django_db
def test_prune_dry_run_deletes_nothing():
    DeliveredActivity.objects.create(
        activity_id="https://remote.example/activity/old2",
        delivered_date=timezone.now() - timedelta(days=40),
    )
    call_command("prune_inbox", dry_run=True)
    assert DeliveredActivity.objects.count() == 1


@pytest.mark.django_db
def test_prune_refuses_a_retention_that_would_delete_live_rows():
    DeliveredActivity.objects.create(activity_id="https://remote.example/activity/x")
    with pytest.raises(CommandError):
        call_command("prune_inbox", days=0)
    assert DeliveredActivity.objects.count() == 1


@pytest.mark.django_db
def test_prune_batches_through_a_backlog(monkeypatch):
    # The first run on an install that has never been pruned is a backlog, and
    # the batching is what keeps it from being one long lock.
    from reeltalk.activitypub.management.commands import prune_inbox

    monkeypatch.setattr(prune_inbox, "BATCH_SIZE", 2)
    for i in range(5):
        DeliveredActivity.objects.create(
            activity_id=f"https://remote.example/activity/b{i}",
            delivered_date=timezone.now() - timedelta(days=40),
        )
    call_command("prune_inbox")
    assert DeliveredActivity.objects.count() == 0
