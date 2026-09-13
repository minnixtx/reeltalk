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

import logging
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import IntegrityError
from PIL import Image

from reeltalk import __version__
from reeltalk.core.utils import sanitize_html
from reeltalk.social.models import User

from .identity import LOCALNAME_RE

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10

# A federated avatar is a small image; anything bigger is treated as hostile
# or broken and skipped (the poster keeps its own larger cap, R46).
AVATAR_MAX_BYTES = 2 * 1024 * 1024

# How long a mirror may not be re-fetched while its avatar/summary are still
# missing — a profile view must not become an unthrottled outbound-fetch loop
# against a home instance that never fills those fields.
PROFILE_REFRESH_THROTTLE = 3600


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


def refresh_mirror_profile(user) -> None:
    """Best-effort fill of a mirror's display name, summary, and avatar (M5).

    R42's mirrors carry the display name + public key only; the profile page
    wants an avatar and bio too. When either is missing, the home Person
    document is fetched once per throttle window and whatever it carries is
    filled in — fill-missing only, never a refresh of values we already have.
    Identity fields (actor_url, public_key, inbox_url) are never touched: no
    key rotation, no identity re-derivation (R42). Any failure is silent —
    the page renders with what we already have.
    """
    if user.local or not user.actor_url:
        return
    if user.avatar and user.summary:
        return  # nothing to fill — no fetch
    cache_key = f"mirror-profile-refresh:{user.actor_url}"
    if cache.get(cache_key) is not None:
        return
    cache.set(cache_key, True, PROFILE_REFRESH_THROTTLE)
    try:
        doc = fetch_person_document(user.actor_url)
    except RemoteFetchError:
        return
    update_fields = []
    name = doc.get("name") or ""
    if name and not user.display_name:
        user.display_name = name[: User._meta.get_field("display_name").max_length]
        update_fields.append("display_name")
    summary = doc.get("summary") or ""
    if isinstance(summary, str) and summary and not user.summary:
        # Remote summaries arrive as HTML (Mastodon) — through the same
        # user-content allowlist as locally rendered markdown.
        user.summary = sanitize_html(summary)
        update_fields.append("summary")
    image = doc.get("image")
    if not user.avatar and isinstance(image, str) and image:
        data = fetch_image_bytes(image, AVATAR_MAX_BYTES)
        if data is not None:
            user.avatar.save(
                image_storage_name(image, f"avatar-{user.pk}.jpg"),
                ContentFile(data),
                save=False,
            )
            update_fields.append("avatar")
    if update_fields:
        user.save(update_fields=update_fields)


def fetch_image_bytes(url: str, max_bytes: int) -> "bytes | None":
    """Defensively download a remote image (R46's poster pattern, shared).

    http/https only; Content-Length pre-check plus a capped streamed read so
    a hostile body cannot exhaust memory; Pillow ``verify()`` before the bytes
    are returned so a forged "image" payload is never stored. Any failure —
    scheme, network, non-2xx, oversized, non-image — logs a warning and
    returns None: the caller skips the image rather than failing its work.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        logger.warning(
            "Skipping remote image %s: unsupported scheme %r", url, parsed.scheme
        )
        return None
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": f"reeltalk/{__version__}"},
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
        if not resp.ok:
            raise ValueError(f"HTTP {resp.status_code}")
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("image exceeds the size cap")
        data = resp.raw.read(max_bytes + 1, decode_content=True)
        if len(data) > max_bytes:
            raise ValueError("image exceeds the size cap")
        Image.open(BytesIO(data)).verify()
    except Exception as err:
        logger.warning("Skipping remote image %s: %s", url, err)
        return None
    return data


def image_storage_name(url: str, fallback: str) -> str:
    """A storage name for a federated image.

    The URL's basename keeps provenance (and usually the right extension);
    it is sanitized to filename-safe characters, with ``fallback`` when
    nothing usable remains.
    """
    base = Path(unquote(urlparse(url).path)).name
    cleaned = "".join(
        ch for ch in base if ch.isascii() and (ch.isalnum() or ch in "._-")
    )
    if not cleaned:
        return fallback
    if "." not in cleaned:
        cleaned += ".jpg"
    return cleaned


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
