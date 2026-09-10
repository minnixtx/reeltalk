"""ActivityPub increment 1: keys + HTTP signatures (M4, R7).

Coverage for the crypto/signature layer in reeltalk.activitypub. Two wire
formats are exercised: RFC 9421 (HTTP Message Signatures, Ed25519) for
outgoing requests and both incoming paths — RFC 9421 and the old draft
format (rsa-sha256/hs2019) that current Mastodon still sends. Wire types
and endpoints land in later increments.
"""

import base64
import hashlib
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import urlparse

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from django.test import RequestFactory

from reeltalk.activitypub import crypto, signatures

KEY_ID = "http://example.com/user/alice/#main-key"


@pytest.fixture()
def keypair():
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


# --- crypto -----------------------------------------------------------------


def test_generate_keypair_round_trips(keypair):
    private_pem, public_pem = keypair
    private = crypto.load_private_key(private_pem)
    public = crypto.load_public_key(public_pem)
    assert isinstance(private, ed25519.Ed25519PrivateKey)
    assert isinstance(public, ed25519.Ed25519PublicKey)
    # The stored public key is the private key's own public half.
    assert public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ) == private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_generate_keypair_is_fresh():
    assert crypto.generate_keypair() != crypto.generate_keypair()


def test_load_private_key_rejects_public_pem(keypair):
    private_pem, public_pem = keypair
    # cryptography raises its own ValueError for a public-key PEM; the
    # isinstance check in load_private_key covers the other direction (a
    # foreign-algorithm private key). Either way: ValueError.
    with pytest.raises(ValueError):
        crypto.load_private_key(public_pem)


def test_load_public_key_accepts_ed25519_and_rsa(keypair, rsa_keypair_pem):
    _private_pem, public_pem = keypair
    assert isinstance(crypto.load_public_key(public_pem), ed25519.Ed25519PublicKey)
    assert isinstance(crypto.load_public_key(rsa_keypair_pem[1]), rsa.RSAPublicKey)


def test_load_public_key_rejects_foreign_algorithm():
    # An EC public key is a valid PEM public key — but not one we verify.
    from cryptography.hazmat.primitives.asymmetric import ec

    ec_key = ec.generate_private_key(ec.SECP256R1())
    pem = (
        ec_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    with pytest.raises(ValueError, match="Unsupported public key type"):
        crypto.load_public_key(pem)


# --- Signature-Input parsing (RFC 9421) ---------------------------------------


def test_parse_signature_input_full():
    value = (
        'sig1=("@method" "@target-uri" "content-digest");'
        f'created=1618633672; keyid="{KEY_ID}"'
    )
    label, components, params, serialization = signatures.parse_signature_input(value)
    assert label == "sig1"
    assert components == ["@method", "@target-uri", "content-digest"]
    assert params == {"created": "1618633672", "keyid": KEY_ID}
    # The serialization (value minus the label) is preserved byte-for-byte —
    # it is reproduced verbatim in the @signature-params line.
    assert serialization == value[len("sig1=") :]


def test_parse_signature_input_without_digest():
    value = f'sig1=("@method" "@target-uri");created=1618633672;keyid="{KEY_ID}"'
    _label, components, params, _serialization = signatures.parse_signature_input(value)
    assert components == ["@method", "@target-uri"]
    assert params["created"] == "1618633672"


def test_parse_signature_input_component_parameters():
    # A component may carry its own parameters (e.g. "@authority";req) —
    # they stay attached to the component, not the signature.
    value = 'sig1=("@authority";req "@method");created=1;keyid="k"'
    _label, components, params, _serialization = signatures.parse_signature_input(value)
    assert components == ["@authority;req", "@method"]
    assert params == {"created": "1", "keyid": "k"}


@pytest.mark.parametrize(
    "value",
    [
        "no-label-here",
        'sig1=("@method";',  # unbalanced parentheses
        'sig1=(bare-name "@method");keyid="k"',  # unquoted component
        'sig1=("@method");bogusparam',  # bare name in parameter position
    ],
)
def test_parse_signature_input_rejects_malformed(value):
    with pytest.raises(ValueError):
        signatures.parse_signature_input(value)


# --- sign / verify: RFC 9421 path ---------------------------------------------


def _signed_request(method, url, private_pem, key_id=KEY_ID, body=None):
    """Build a Django request carrying the signed headers for ``url``."""
    factory = RequestFactory()
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    headers = signatures.sign_request(
        method, url, private_pem, key_id=key_id, body=body
    )
    if method.upper() == "GET":
        request = factory.get(path)
    else:
        request = factory.post(
            path, data=body or b"", content_type="application/activity+json"
        )
    # HttpHeaders is read-only — set through the underlying META. Real HTTP
    # requests always carry a Host header; RequestFactory does not.
    request.META["HTTP_HOST"] = parsed.netloc
    for name, value in headers.items():
        request.META[f"HTTP_{name.upper().replace('-', '_')}"] = value
    return request


def test_sign_request_get_headers(keypair):
    private_pem, _public_pem = keypair
    headers = signatures.sign_request(
        "GET", "http://example.com/user/alice/", private_pem, key_id=KEY_ID
    )
    assert set(headers) == {"Signature-Input", "Signature"}
    label, components, params, inner = signatures.parse_signature_input(
        headers["Signature-Input"]
    )
    assert label == "sig1"
    assert components == ["@method", "@target-uri"]
    assert params["keyid"] == KEY_ID
    assert params["created"].isdigit()
    # The Signature header carries the labeled base64 signature.
    assert headers["Signature"].startswith("sig1=")
    # inner is what sign_request put in the header — round-trips by
    # construction; the verify path reuses it verbatim.
    assert components[0] in inner


def test_sign_request_post_carries_content_digest(keypair):
    private_pem, _public_pem = keypair
    body = b'{"@context": "https://www.w3.org/ns/activitystreams"}'
    headers = signatures.sign_request(
        "POST", "http://example.com/inbox/", private_pem, key_id=KEY_ID, body=body
    )
    assert set(headers) == {"Signature-Input", "Signature", "Content-Digest"}
    assert headers["Content-Digest"] == signatures.digest_value(body)
    _label, components, params, _inner = signatures.parse_signature_input(
        headers["Signature-Input"]
    )
    assert components == ["@method", "@target-uri", "content-digest"]
    assert params["created"].isdigit()


def test_verify_rfc9421_get_round_trip(keypair):
    private_pem, public_pem = keypair
    request = _signed_request("GET", "http://example.com/user/alice/", private_pem)
    assert signatures.verify_request(request, public_pem) is True


def test_verify_rfc9421_post_with_body_round_trip(keypair):
    private_pem, public_pem = keypair
    body = b'{"type": "Follow"}'
    request = _signed_request(
        "POST", "http://example.com/user/bob/inbox/", private_pem, body=body
    )
    assert signatures.verify_request(request, public_pem) is True


def test_verify_rfc9421_query_string_is_covered(keypair):
    private_pem, public_pem = keypair
    request = _signed_request("GET", "http://example.com/outbox/?page=2", private_pem)
    assert signatures.verify_request(request, public_pem) is True
    # The same signature must not validate a different target URI: strip the
    # query string after signing.
    request.META["PATH_INFO"] = "/outbox/"
    request.META["QUERY_STRING"] = ""
    assert signatures.verify_request(request, public_pem) is False


def test_verify_rfc9421_follows_forwarded_proto(keypair):
    # Behind the operator's TLS proxy (D14), remotes sign the https target
    # URI while WSGI sees http — X-Forwarded-Proto reconciles them.
    private_pem, public_pem = keypair
    request = _signed_request("GET", "https://example.com/user/alice/", private_pem)
    request.META["HTTP_X_FORWARDED_PROTO"] = "https"
    assert signatures.verify_request(request, public_pem) is True


def test_verify_rfc9421_rejects_wrong_public_key(keypair):
    private_pem, _public_pem = keypair
    request = _signed_request("GET", "http://example.com/user/alice/", private_pem)
    other_private, other_public = crypto.generate_keypair()
    assert signatures.verify_request(request, other_public) is False


def test_verify_rfc9421_rejects_tampered_body(keypair):
    private_pem, public_pem = keypair
    request = _signed_request(
        "POST", "http://example.com/inbox/", private_pem, body=b'{"a": 1}'
    )
    # Replace the body after signing: the content-digest check must fail.
    # (Django's HttpRequest.body has no setter — _body is its read cache.)
    request._body = b'{"a": 2}'
    assert signatures.verify_request(request, public_pem) is False


def test_verify_rfc9421_rejects_tampered_host(keypair):
    private_pem, public_pem = keypair
    request = _signed_request("GET", "http://example.com/user/alice/", private_pem)
    request.META["HTTP_HOST"] = "evil.example"
    assert signatures.verify_request(request, public_pem) is False


def test_verify_rfc9421_rejects_unsigned_request(keypair):
    _private_pem, public_pem = keypair
    request = RequestFactory().get("/user/alice/")
    assert signatures.verify_request(request, public_pem) is False


def test_verify_rfc9421_rejects_missing_keyid(keypair):
    private_pem, public_pem = keypair
    request = _signed_request("GET", "http://example.com/user/alice/", private_pem)
    # Read from META, not request.headers: HttpHeaders caches on first
    # access, so a META overwrite after touching .headers would be invisible.
    header = request.META["HTTP_SIGNATURE_INPUT"]
    request.META["HTTP_SIGNATURE_INPUT"] = header[: header.index(";keyid=")] + ")"
    assert signatures.verify_request(request, public_pem) is False


def test_verify_rfc9421_rejects_garbage_signature(keypair):
    private_pem, public_pem = keypair
    request = _signed_request("GET", "http://example.com/user/alice/", private_pem)
    request.META["HTTP_SIGNATURE"] = "sig1=!!!not-base64!!!"
    assert signatures.verify_request(request, public_pem) is False


def test_verify_rfc9421_rejects_unknown_component(keypair):
    # A component we cannot reconstruct (e.g. "@path") must be rejected,
    # not silently dropped from the base.
    private_pem, public_pem = keypair
    request = _signed_request("GET", "http://example.com/user/alice/", private_pem)
    header = request.META["HTTP_SIGNATURE_INPUT"]
    request.META["HTTP_SIGNATURE_INPUT"] = header.replace(
        '("@method"', '("@path" "@method"'
    )
    assert signatures.verify_request(request, public_pem) is False


def test_verify_rfc9421_rejects_rsa_key(keypair, rsa_keypair_pem):
    # The RFC 9421 path only verifies Ed25519 keys.
    private_pem, _public_pem = keypair
    request = _signed_request("GET", "http://example.com/user/alice/", private_pem)
    assert signatures.verify_request(request, rsa_keypair_pem[1]) is False


# --- sign / verify: old draft format (current Mastodon sender) -----------------


def _legacy_signed_request(method, url, rsa_private_pem, key_id=KEY_ID, body=None):
    """Build a request signed in the old draft format (rsa-sha256).

    Mirrors what current Mastodon sends: ``name: value`` lines joined by LF
    with no trailing newline, lowercased method in ``(request-target)``,
    path without query string.
    """
    factory = RequestFactory()
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    date = format_datetime(datetime.now(UTC), usegmt=True)
    listed = ["(request-target)", "host", "date"]
    lines = [
        f"(request-target): {method.lower()} {parsed.path}",
        f"host: {parsed.netloc}",
        f"date: {date}",
    ]
    digest = None
    if body is not None:
        digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()
        listed.append("digest")
        lines.append(f"digest: {digest}")
    key = serialization.load_pem_private_key(rsa_private_pem.encode(), password=None)
    signature = key.sign("\n".join(lines).encode(), padding.PKCS1v15(), hashes.SHA256())
    signature_header = ",".join(
        [
            f'keyId="{key_id}"',
            'algorithm="rsa-sha256"',
            f'headers="{" ".join(listed)}"',
            f'signature="{base64.b64encode(signature).decode()}"',
        ]
    )
    if method.upper() == "GET":
        request = factory.get(path)
    else:
        request = factory.post(
            path, data=body or b"", content_type="application/activity+json"
        )
    request.META["HTTP_HOST"] = parsed.netloc
    request.META["HTTP_DATE"] = date
    if digest is not None:
        request.META["HTTP_DIGEST"] = digest
    request.META["HTTP_SIGNATURE"] = signature_header
    return request


def test_verify_legacy_get_round_trip(rsa_keypair_pem):
    private_pem, public_pem = rsa_keypair_pem
    request = _legacy_signed_request(
        "GET", "https://example.com/user/alice/", private_pem
    )
    assert signatures.verify_request(request, public_pem) is True


def test_verify_legacy_post_with_digest_round_trip(rsa_keypair_pem):
    private_pem, public_pem = rsa_keypair_pem
    request = _legacy_signed_request(
        "POST", "https://example.com/user/bob/inbox/", private_pem, body=b'{"a": 1}'
    )
    assert signatures.verify_request(request, public_pem) is True


def test_verify_legacy_hs2019_accepted(rsa_keypair_pem):
    # hs2019 signs byte-identically to rsa-sha256 — only the algorithm name
    # differs, and Pleroma-family servers send it.
    private_pem, public_pem = rsa_keypair_pem
    request = _legacy_signed_request(
        "GET", "https://example.com/user/alice/", private_pem
    )
    header = request.META["HTTP_SIGNATURE"].replace(
        'algorithm="rsa-sha256"', 'algorithm="hs2019"'
    )
    request.META["HTTP_SIGNATURE"] = header
    assert signatures.verify_request(request, public_pem) is True


def test_verify_legacy_rejects_tampered_body(rsa_keypair_pem):
    private_pem, public_pem = rsa_keypair_pem
    request = _legacy_signed_request(
        "POST", "https://example.com/inbox/", private_pem, body=b'{"a": 1}'
    )
    request._body = b'{"a": 2}'
    assert signatures.verify_request(request, public_pem) is False


def test_verify_legacy_rejects_wrong_algorithm(rsa_keypair_pem):
    private_pem, public_pem = rsa_keypair_pem
    request = _legacy_signed_request(
        "GET", "https://example.com/user/alice/", private_pem
    )
    header = request.META["HTTP_SIGNATURE"].replace(
        'algorithm="rsa-sha256"', 'algorithm="ed25519"'
    )
    request.META["HTTP_SIGNATURE"] = header
    assert signatures.verify_request(request, public_pem) is False


def test_verify_legacy_rejects_ed25519_key(keypair, rsa_keypair_pem):
    # The old-draft path only verifies RSA keys.
    private_pem, _public_pem = rsa_keypair_pem
    request = _legacy_signed_request(
        "GET", "https://example.com/user/alice/", private_pem
    )
    assert signatures.verify_request(request, keypair[1]) is False


def test_parse_legacy_signature_missing_required():
    with pytest.raises(ValueError):
        signatures.parse_legacy_signature('algorithm="rsa-sha256"')
