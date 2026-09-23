"""Key handling for ActivityPub (M4, R7).

ActivityPub identifies actors by URL and authenticates requests with key
signatures. ReelTalk signs outgoing requests with Ed25519 (RFC 9421, see
``signatures``) and verifies incoming ones with either Ed25519 or RSA —
current Mastodon still sends the old draft format with RSA keys. Keys are
stored as PEM strings on ``User``: local users carry a full key pair
generated at creation; remote mirrors carry only the public key, fetched
from their Person document. The stdlib has no Ed25519 or PKCS#1v1.5
verification helpers of our own, so this module is a thin wrapper over
the audited ``cryptography`` package (PLAN.md §4.6).
"""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa


def generate_keypair() -> tuple[str, str]:
    """Generate a fresh Ed25519 key pair as ``(private_pem, public_pem)``."""
    private_key = ed25519.Ed25519PrivateKey.generate()
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


def load_private_key(pem: str) -> ed25519.Ed25519PrivateKey:
    """Parse a PEM-encoded Ed25519 private key; raise ValueError otherwise.

    ReelTalk only ever signs with Ed25519, so anything else is rejected —
    including a public-key PEM (cryptography raises its own ValueError for
    that shape) and foreign-algorithm private keys.
    """
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError("Not an Ed25519 private key")
    return key


def load_public_key(pem: str) -> ed25519.Ed25519PublicKey | rsa.RSAPublicKey:
    """Parse a PEM public key (Ed25519 or RSA); raise ValueError otherwise.

    Callers dispatch on the type: the RFC 9421 verification path needs
    Ed25519, the old-draft path RSA.
    """
    key = serialization.load_pem_public_key(pem.encode())
    if not isinstance(key, (ed25519.Ed25519PublicKey, rsa.RSAPublicKey)):
        raise ValueError("Unsupported public key type")
    return key


# FEP-521a key encoding (R88). A PEM says how the bytes are wrapped, not what
# algorithm they are, so a peer that has to pick a verifier from the published
# document needs the multicodec tag carried alongside the key.
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Multicodec for ed25519-pub is 0xED written as a varint, which is two bytes.
ED25519_PUB_MULTICODEC = b"\xed\x01"


def _base58_encode(data: bytes) -> str:
    """Base58 (Bitcoin alphabet) with leading-zero bytes as leading '1's."""
    number = int.from_bytes(data, "big")
    digits: list[str] = []
    while number > 0:
        number, remainder = divmod(number, 58)
        digits.append(_BASE58_ALPHABET[remainder])
    leading_zeroes = len(data) - len(data.lstrip(b"\x00"))
    return _BASE58_ALPHABET[0] * leading_zeroes + "".join(reversed(digits))


def public_key_multibase(pem: str) -> str:
    """The FEP-521a ``Multikey`` encoding of an Ed25519 public key PEM.

    ``z`` (base58btc) over ``multicodec || raw32``. Peers that must learn the
    key's algorithm from the published document read the tag here; Mastodon
    4.7.2's ``Multibase.decode_key_to_pem`` turns this back into the same PEM
    we publish, tagged ``ed25519``.
    """
    key = serialization.load_pem_public_key(pem.encode())
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise ValueError("Only Ed25519 keys have a Multikey encoding")
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return "z" + _base58_encode(ED25519_PUB_MULTICODEC + raw)
