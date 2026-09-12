"""Remote-user mirrors (M4 increment 4).

When an activity arrives signed by a user this instance has never seen, the
sender is resolved from the signature's keyid: stripping its fragment gives
the actor URL, and the mirror is looked up by it. On first contact the
actor's Person document is fetched from that URL and a lightweight mirror
record (``local=False``) is created carrying the public key — later
deliveries verify against the stored key without re-fetching (§3.6: mirrors
are created on first interaction, updated from inbound Person documents).

Built fresh against the ActivityPub spec (R7); federation targets are
ReelTalk instances and current Mastodon (R39), whose Person documents both
carry ``publicKeyPem``.
"""

import re
from urllib.parse import urlparse

import requests
from django.db import IntegrityError

from reeltalk import __version__
from reeltalk.social.models import User

from .identity import LOCALNAME_RE

REQUEST_TIMEOUT = 10


class RemoteFetchError(Exception):
    """The remote actor's Person document could not be fetched or parsed."""


def fetch_person_document(actor_url: str) -> dict:
    """GET a remote actor's Person document (public — no signature needed).

    Raises :class:`RemoteFetchError` on any failure: unsupported scheme,
    network error, non-2xx status, non-JSON body, or a document that is not
    a usable Person (missing id/publicKeyPem or the wrong type).
    """
    parsed = urlparse(actor_url)
    if parsed.scheme not in ("http", "https"):
        raise RemoteFetchError(f"Unsupported actor URL scheme: {parsed.scheme!r}")
    try:
        resp = requests.get(
            actor_url,
            headers={
                "Accept": "application/activity+json",
                # Identify the software so remote instances can filter by it.
                "User-Agent": f"reeltalk/{__version__}",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as err:
        raise RemoteFetchError(f"Could not reach {actor_url}") from err
    if resp.status_code == 404:
        raise RemoteFetchError(f"No actor at {actor_url}")
    if not resp.ok:
        raise RemoteFetchError(f"Actor fetch failed (HTTP {resp.status_code})")
    try:
        doc = resp.json()
    except ValueError as err:
        raise RemoteFetchError("Actor document is not JSON") from err
    if not isinstance(doc, dict) or not doc.get("id"):
        raise RemoteFetchError("Not a usable Person document")
    doc_type = doc.get("type")
    types = [doc_type] if isinstance(doc_type, str) else list(doc_type or [])
    if "Person" not in types:
        raise RemoteFetchError(f"Not a Person document (type {doc_type!r})")
    public_key = (doc.get("publicKey") or {}).get("publicKeyPem", "")
    if not public_key:
        # Without the public key we could never verify this sender.
        raise RemoteFetchError("Person document carries no publicKeyPem")
    return doc


def _mirror_localname(doc: dict) -> str:
    """The local handle for a remote mirror: ``<preferredUsername>@<netloc>``.

    Local localnames can never contain ``@`` (R12's charset), so the two
    namespaces are disjoint — a remote user cannot collide with a local
    account, and users of the same name on different instances stay apart
    because the netloc (host, plus port when non-default) differs. The
    result is truncated to the field width; beyond that pathological case
    the actor_url remains the authoritative identity (it is unique).
    """
    actor_url = doc["id"]
    parsed = urlparse(actor_url)
    netloc = parsed.netloc.lower() or "unknown"
    username = doc.get("preferredUsername") or ""
    if not username:
        # Fallback: the last path segment (the R40 convention is
        # /user/<localname>/; a leading '@' is stripped for other shapes).
        segments = [segment for segment in parsed.path.split("/") if segment]
        username = segments[-1].lstrip("@") if segments else "remote"
    max_length = User._meta.get_field("localname").max_length
    return f"{username}@{netloc}"[:max_length]


def mirror_user_from_person(doc: dict) -> User:
    """Create (or return the existing) mirror for a remote Person document.

    The mirror is keyed on the document's id URL (``actor_url``): it carries
    the home-instance wire id everything else about this user hangs off,
    plus the public key later deliveries verify against. Create-only — an
    existing mirror is returned untouched (v0.1: no document refresh and no
    key rotation; see R42).
    """
    actor_url = doc["id"]
    existing = User.objects.filter(local=False, actor_url=actor_url).first()
    if existing is not None:
        return existing
    public_key = (doc.get("publicKey") or {}).get("publicKeyPem", "")
    if not public_key:
        raise RemoteFetchError("Person document carries no publicKeyPem")
    user = User(
        localname=_mirror_localname(doc),
        display_name=doc.get("name") or "",
        local=False,
        actor_url=actor_url,
        # The home inbox advertised by the document — where outbound
        # activities (a follow we initiate) are delivered (increment 5).
        inbox_url=doc.get("inbox") or "",
        public_key=public_key,
    )
    try:
        user.save()
    except IntegrityError as err:
        # A differently-spelled actor URL for the same <username>@<netloc>
        # handle — a pathological identity collision; fail the delivery
        # rather than merge two identities.
        raise RemoteFetchError("Mirror localname collision") from err
    return user


# The R40 actor-path convention, matched against an absolute URL's path.
_ACTOR_PATH_RE = re.compile(rf"^/user/({LOCALNAME_RE})/$")


def _resolve_actor(actor_url: str, request, *, fetch: bool) -> "User | None":
    """Resolve an actor URL to a user this instance can represent.

    A URL on this instance — netloc compared against the request's Host
    header, so it works behind the operator proxy (D14) and in same-host
    multi-instance setups where only the port differs — resolves to the local
    user at its R40 path; there is no remote mirror of ourselves to fetch.
    Otherwise the remote mirror is looked up by its ``actor_url``; when
    ``fetch`` is set, a first contact fetches the Person document and creates
    the mirror (R42). Returns None when the URL cannot be resolved (unknown
    local user, no mirror and fetch disabled, or an unreachable/unusable
    remote document).
    """
    base = actor_url.split("#", 1)[0]
    if not base:
        return None
    parsed = urlparse(base)
    if parsed.netloc.lower() == request.get_host().lower():
        match = _ACTOR_PATH_RE.match(parsed.path)
        if not match:
            return None
        return User.objects.filter(local=True, localname__iexact=match.group(1)).first()
    mirror = User.objects.filter(local=False, actor_url=base).first()
    if mirror is not None:
        return mirror
    if not fetch:
        return None
    try:
        doc = fetch_person_document(base)
        return mirror_user_from_person(doc)
    except RemoteFetchError:
        return None


def resolve_sender(key_id: str, request) -> "User | None":
    """Resolve a signature's keyid to the user who signed the request.

    The keyid is the actor URL with a ``#main-key`` fragment (R39). A first
    contact with a remote sender fetches its Person document and creates the
    mirror, so later deliveries verify against the stored key without
    re-fetching. Returns None when the sender cannot be resolved — the caller
    rejects the delivery.
    """
    return _resolve_actor(key_id, request, fetch=True)


def resolve_known_actor(actor_url: str, request) -> "User | None":
    """Resolve an actor URL to a user this instance already knows.

    Local users by their R40 path on this host, remote users by an existing
    mirror — no first-contact fetch. Used to resolve the object of an inbound
    Follow/Undo(Follow) (increment 5): an unknown actor is ignored gracefully
    rather than fetched as a side effect of processing the activity.
    """
    return _resolve_actor(actor_url, request, fetch=False)
