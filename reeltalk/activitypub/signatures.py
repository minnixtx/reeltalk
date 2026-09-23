"""ActivityPub HTTP request signatures (M4, R7).

Two wire formats exist in the field, and this module handles both:

**Outgoing** requests are signed per RFC 9421 (HTTP Message Signatures)
with Ed25519 — a ``Signature-Input`` header naming the covered components
plus ``created``/``keyid``, and a ``Signature`` header carrying the
signature as an inner-coded byte string (``sig1=:base64:``). The signature
base is one ``"component": value`` line per covered component
(LF-terminated), ending with a final ``"@signature-params": <inner list>``
line that carries no trailing LF (RFC 9421 §2.5).

R88 measured this against the verifier Mastodon 4.7.2 actually runs
(``linzer`` 0.8.0): the base we build verifies byte-for-byte, but the
header must be inner-coded — the bare ``sig1=<base64>`` we used to send
does not parse at all, so the peer fails before it ever looks at the key.
Mastodon reaches the key only for a key it knows is Ed25519, which is why
the Person document also publishes the FEP-521a ``Multikey`` form
(``identity.person_document``); its legacy ``publicKey``/``publicKeyPem``
path pins every key it reads to RSA.

**Incoming** requests may arrive either way: a ``Signature-Input`` header
means RFC 9421 (Ed25519 keys); a bare ``Signature`` header carrying
``keyId/algorithm/headers/signature`` parameters is the old draft format
(``rsa-sha256``/``hs2019``, RSA keys) — what current Mastodon still
sends. Verification reconstructs the sender's base from the actual
request and checks it; any malformation or mismatch returns False rather
than raising (an invalid signature means "reject this activity").

Deliberately minimal for v0.1: no replay window on ``created``/``date``
(activities are idempotent, keyed by origin ids), one signature label per
request, and the legacy path covers only the ``(request-target) host date
digest`` components in use. The ``@target-uri`` scheme is ``request.scheme``,
which only the trusted-proxy gate may raise (D14's operator proxy forwards
``X-Forwarded-Proto``; R74 makes it trustworthy before we read it).
"""

import base64
import hashlib
import re
import time
from typing import Any
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from .crypto import load_private_key

_LABEL_RE = re.compile(r"^([A-Za-z0-9_-]+)=\s*")
# Signature parameters take a quoted string or a bare integer (created).
_PARAM_RE = re.compile(r'^(\S+)=(?:"((?:[^"\\]|\\.)*)"|(\S+))$')
_LEGACY_PARAM_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _find_closing_paren(text: str, open_index: int) -> int:
    """Index of the parenthesis matching ``text[open_index]`` (quote-aware)."""
    depth = 0
    in_quotes = False
    escaped = False
    for i in range(open_index, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if in_quotes:
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("Unbalanced parentheses")


def _split_outside_quotes(value: str, sep: str) -> list[str]:
    """Split on ``sep`` except where it occurs inside double quotes."""
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    escaped = False
    for ch in value:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            continue
        if ch == sep and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [part.strip() for part in parts if part.strip()]


def parse_signature_input(value: str) -> tuple[str, list[str], dict[str, str], str]:
    """Parse a ``Signature-Input`` header value (RFC 9421 §2.1).

    The value is ``label=(component-list);signature-params`` — the
    parentheses wrap only the component list (a component may carry its own
    parameters, e.g. ``"@authority";req``), and the signature parameters
    (``created``, ``keyid``, ...) follow the closing parenthesis.

    Returns ``(label, components, params, serialization)`` where
    ``components`` is the ordered list of covered component identifiers
    (their parameters included), ``params`` holds the signature parameters,
    and ``serialization`` is the value minus the label — byte-for-byte as
    sent: it must be reproduced verbatim in the signature base's final line.
    Raises ValueError on malformed input.
    """
    stripped = value.strip()
    match = _LABEL_RE.match(stripped)
    if not match or "(" not in stripped[match.end() :]:
        raise ValueError(f"Malformed Signature-Input header: {value!r}")
    label = match.group(1)
    serialization = stripped[match.end() :].lstrip()
    open_index = serialization.index("(")
    close_index = _find_closing_paren(serialization, open_index)

    components: list[str] = []
    for token in serialization[open_index + 1 : close_index].split():
        if not token.startswith('"'):
            raise ValueError(f"Expected quoted component, got: {token!r}")
        close_quote = token.find('"', 1)
        if close_quote < 0:
            raise ValueError(f"Malformed component name: {token!r}")
        # The name is the quoted part; anything after it must be the
        # component's own parameters (;req, ...) and stays attached.
        suffix = token[close_quote + 1 :]
        if suffix and not suffix.startswith(";"):
            raise ValueError(f"Malformed component identifier: {token!r}")
        components.append(token[1:close_quote] + suffix)

    params: dict[str, str] = {}
    param_text = serialization[close_index + 1 :]
    if param_text:
        if not param_text.startswith(";"):
            raise ValueError(f"Expected ';' after component list: {value!r}")
        for segment in _split_outside_quotes(param_text[1:], ";"):
            param_match = _PARAM_RE.match(segment)
            if not param_match:
                raise ValueError(f"Malformed signature parameter: {segment!r}")
            name = param_match.group(1)
            # Quoted string (group 2) or bare integer (group 3).
            params[name] = (
                param_match.group(2)
                if param_match.group(2) is not None
                else param_match.group(3)
            )
    return label, components, params, serialization


def parse_legacy_signature(value: str) -> dict[str, str]:
    """Parse an old-draft ``Signature`` header (keyId/algorithm/headers/...).

    Returns the parameter dict; raises ValueError when the two required
    parameters (``keyId``, ``signature``) are missing.
    """
    params = dict(_LEGACY_PARAM_RE.findall(value))
    if not params.get("keyId") or not params.get("signature"):
        raise ValueError(f"Malformed legacy Signature header: {value!r}")
    return params


def extract_key_id(request: Any) -> str | None:
    """The signing key's URL from the request's signature headers.

    Reads whichever live format is present (R39): an RFC 9421
    ``Signature-Input`` carries ``keyid``, a bare old-draft ``Signature``
    header carries ``keyId``. Returns None when no usable signature header
    exists — the caller treats that as "reject this activity". The returned
    keyid is the actor URL with a ``#main-key`` fragment (R39).
    """
    signature_input = request.headers.get("Signature-Input")
    if signature_input:
        try:
            _label, _components, params, _serialization = parse_signature_input(
                signature_input
            )
        except ValueError:
            return None
        return params.get("keyid") or None
    signature = request.headers.get("Signature")
    if signature:
        try:
            params = parse_legacy_signature(signature)
        except ValueError:
            return None
        return params.get("keyId") or None
    return None


def digest_value(body: bytes) -> str:
    """RFC 9530 ``Content-Digest`` value (sha-256, base64, colon-wrapped)."""
    return "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"


def _rfc9421_base(
    components: list[str], values: dict[str, str], serialization: str
) -> bytes:
    """The RFC 9421 signature base (§2.5): one quoted ``"component": value``
    line per covered component (LF-terminated), then the final
    ``"@signature-params": <serialization>`` line — no trailing LF."""
    lines = [f'"{name}": {values[name]}' for name in components]
    lines.append(f'"@signature-params": {serialization}')
    return "\n".join(lines).encode()


def _unwrap_inner_bytes(value: str) -> str:
    """Strip the RFC 8941 inner-coded byte-string delimiters from a value.

    A conforming RFC 9421 peer writes the signature as ``:base64:``. The bare
    form is accepted as well so a peer that has not made the R88 framing fix
    can still be verified — the bytes are what the signature covers, so the
    wrapping carries nothing an attacker could exploit.
    """
    stripped = value.strip()
    if len(stripped) >= 2 and stripped.startswith(":") and stripped.endswith(":"):
        return stripped[1:-1]
    return stripped


def sign_request(
    method: str,
    url: str,
    private_pem: str,
    *,
    key_id: str,
    body: bytes | None = None,
) -> dict[str, str]:
    """Headers that make a request a valid signed ActivityPub request.

    Signs per RFC 9421 with Ed25519, covering ``@method`` and
    ``@target-uri`` plus ``content-digest`` for requests with a body.
    Returns ``Signature-Input`` + ``Signature`` (and ``Content-Digest``);
    merge them onto the outgoing request. ``key_id`` is the URL of the
    signing key — the Person document's publicKey id, i.e. the actor URL
    with a ``#main-key`` fragment.
    """
    parsed = urlparse(url)
    target_uri = f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"
    if parsed.query:
        target_uri += "?" + parsed.query

    created = int(time.time())
    components = ["@method", "@target-uri"]
    values: dict[str, str] = {"@method": method.upper(), "@target-uri": target_uri}
    headers: dict[str, str] = {}
    if body is not None:
        digest = digest_value(body)
        values["content-digest"] = digest
        components.append("content-digest")
        headers["Content-Digest"] = digest

    # The parentheses wrap only the component list; the signature parameters
    # follow the closing parenthesis (RFC 9421 §2.1).
    quoted = " ".join(f'"{name}"' for name in components)
    serialization = f'({quoted});created={created};keyid="{key_id}"'
    base = _rfc9421_base(components, values, serialization)
    signature = load_private_key(private_pem).sign(base)
    headers["Signature-Input"] = f"sig1={serialization}"
    # The Signature header is a Structured Fields dictionary whose members are
    # *inner-coded* byte strings, so the base64 rides between colons
    # (RFC 9421 §4.2). Without them the whole header fails to parse — a peer
    # never reaches the signature at all (R88).
    headers["Signature"] = "sig1=:" + base64.b64encode(signature).decode() + ":"
    return headers


def _target_uri(request: Any) -> str:
    """Reconstruct the request's target URI for ``@target-uri``.

    The scheme is ``request.scheme`` — the one value the trusted-proxy gate
    (R74) has already vouched for, so it is the terminator's forwarded
    scheme or the real transport and nothing else. Reading
    ``X-Forwarded-Proto`` here would be a second path to the scheme, this
    one bypassing the gate, and the URI we compare against what the sender
    signed would move under us. The authority is the Host header as sent, so
    default-port and case quirks match what the sender signed.
    """
    host = request.headers.get("Host", "")
    return f"{request.scheme}://{host}{request.get_full_path()}"


def _verify_rfc9421(request: Any, public_key: Any) -> bool:
    signature_input = request.headers.get("Signature-Input")
    signature_value = request.headers.get("Signature")
    if not signature_input or not signature_value:
        return False
    label, components, params, serialization = parse_signature_input(signature_input)
    if not params.get("keyid"):
        return False

    # One signature per request; take the value for our label (a bare
    # unlabeled value is accepted too).
    values: dict[str, str] = {}
    for part in signature_value.split(","):
        name, _, b64 = part.strip().partition("=")
        if name in ("", label):
            values["_b64"] = _unwrap_inner_bytes(b64)
    if "_b64" not in values:
        return False

    base_values: dict[str, str] = {}
    for name in components:
        component = name.split(";")[0]  # drop the component's own parameters
        if component == "@method":
            base_values[name] = request.method
        elif component == "@target-uri":
            base_values[name] = _target_uri(request)
        elif component == "content-digest":
            digest = request.headers.get("Content-Digest", "")
            # Never trust the header — check it against the real body.
            if not digest or digest != digest_value(request.body):
                return False
            base_values[name] = digest
        else:
            # A plain covered header field (date, host, ...).
            header_value = request.headers.get(component)
            if header_value is None:
                return False
            base_values[name] = header_value

    base = _rfc9421_base(components, base_values, serialization)
    public_key.verify(base64.b64decode(values["_b64"]), base)
    return True


def _verify_legacy(request: Any, public_key: rsa.RSAPublicKey) -> bool:
    """The old draft format current Mastodon still sends (rsa-sha256/hs2019).

    Signed string: one ``name: value`` line per listed header in order,
    joined by LF with no trailing newline; ``(request-target)`` is the
    lowercased method + space + path (query omitted — that is what current
    Mastodon signs). A covered ``digest`` must match the real body.
    """
    value = request.headers.get("Signature")
    params = parse_legacy_signature(value)
    if params.get("algorithm") not in ("rsa-sha256", "hs2019"):
        return False

    lines: list[str] = []
    for name in params["headers"].split():
        if name == "(request-target)":
            target = f"{request.method.lower()} {request.path}"
            lines.append(f"(request-target): {target}")
        elif name == "digest":
            digest = request.headers.get("Digest", "")
            algorithm, _, b64 = digest.partition("=")
            if algorithm.upper() != "SHA-256":
                return False
            try:
                expected = base64.b64decode(b64)
            except ValueError:
                return False
            if expected != hashlib.sha256(request.body).digest():
                return False
            lines.append(f"digest: {digest}")
        else:
            header_value = request.headers.get(name)
            if header_value is None:
                return False
            lines.append(f"{name}: {header_value}")

    base = "\n".join(lines).encode()
    public_key.verify(
        base64.b64decode(params["signature"]), base, padding.PKCS1v15(), hashes.SHA256()
    )
    return True


def verify_request(request: Any, public_pem: str) -> bool:
    """Verify an incoming signed request against the sender's public key.

    Dispatches on the header shape: ``Signature-Input`` present → RFC 9421
    (the key must be Ed25519); otherwise the old draft format (RSA).
    Returns False on any malformation, unsupported algorithm, or mismatch
    rather than raising.
    """
    try:
        if not request.headers.get("Signature"):
            return False
        public_key = serialization.load_pem_public_key(public_pem.encode())
        if request.headers.get("Signature-Input"):
            if not isinstance(public_key, ed25519.Ed25519PublicKey):
                return False
            return _verify_rfc9421(request, public_key)
        if not isinstance(public_key, rsa.RSAPublicKey):
            return False
        return _verify_legacy(request, public_key)
    except (ValueError, KeyError, InvalidSignature, TypeError):
        return False
