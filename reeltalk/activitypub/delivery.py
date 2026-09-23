"""Outgoing ActivityPub delivery (M4 increment 5).

Sends a signed activity to a remote inbox. Increment 5 uses it for the
Follow / Undo(Follow) a local user initiates toward a remote user; increment
6 reuses it for status broadcasts and shelf events to followers' inboxes.
Built fresh against the spec (R7): the request is signed per RFC 9421 with
Ed25519 (R39) via ``signatures.sign_request``, covering ``@method`` +
``@target-uri`` and the body's content-digest.

Delivery in v0.1 is a single synchronous POST with no retry queue: a network
failure surfaces as a ``requests.RequestException`` for the caller to handle
(the local state has already been recorded, so nothing is lost — only the
remote's copy of it lags until the next delivery or an outbox backfill).
"""

import json
import logging

import requests

from .signatures import sign_request

logger = logging.getLogger(__name__)

# Same reachability budget as the inbound Person-document fetch (mirrors).
REQUEST_TIMEOUT = 10

# How much of a remote's response body reaches the log. Enough for a Mastodon
# verification error, not enough for an HTML error page to drown the line.
_LOG_BODY_LIMIT = 300

# The ActivityStreams context. Mastodon's ``ProcessActivityService`` opens with
# ``return unless supported_context?(@json)``, and ``supported_context?`` is an
# ``equals_or_includes?`` check on ``@context`` for exactly this string.
AS_CONTEXT = "https://www.w3.org/ns/activitystreams"


def with_context(activity: dict) -> dict:
    """Guarantee an outbound activity declares the ActivityStreams context.

    A document whose ``@context`` does not include the AS context is thrown
    away before any handler runs. The damage is invisible because the inbox
    answers ``202 Accepted`` when it *queues* the work, so a payload
    discarded at this line is indistinguishable from one that was acted on
    -- which is what happened to every activity this instance ever sent
    (R88). The Accept that would have cleared a pending follow died here, as
    did the Follow before it.

    Set in the primitive rather than at each call site for the same reason
    the response status is logged here: an outbound path must not be able to
    forget the thing that makes it deliverable.
    """
    if "@context" in activity:
        return activity
    return {"@context": AS_CONTEXT, **activity}


def inbox_for(follower) -> str:
    """A remote follower's home inbox — the advertised URL, else the convention.

    The mirror's ``inbox_url`` is populated from its Person document at mirror
    creation (R42 create-only). ReelTalk and current Mastodon (R39's targets)
    both place the inbox under the actor path, so a mirror whose document
    advertised no inbox falls back to ``<actor_url>/inbox``.
    """
    if follower.inbox_url:
        return follower.inbox_url
    return follower.actor_url.rstrip("/") + "/inbox"


def _brief_body(response) -> str:
    """A single-line, length-capped rendering of a response body for the log.

    A remote's error body is the answer to "why was this refused" — Mastodon
    names the actor, the key and the reason there — so it is worth keeping.
    Capped and flattened because it arrives from arbitrary servers.
    """
    try:
        body = (response.text or "").strip()
    except Exception:  # noqa: BLE001 - a broken body must not break the log
        return "<unreadable>"
    body = " ".join(body.split())
    return body[:_LOG_BODY_LIMIT] + "…" if len(body) > _LOG_BODY_LIMIT else body


def deliver_activity(inbox_url: str, activity: dict, private_pem: str, key_id: str):
    """POST ``activity`` to ``inbox_url``, signed with the sender's key.

    ``private_pem`` is the Ed25519 private key of the local user sending;
    ``key_id`` is that user's signing-key URL (actor URL + ``#main-key``,
    R39). Returns the ``requests.Response`` so a caller can inspect the
    status; raises ``requests.RequestException`` on network failure.

    The outcome of every send is logged here rather than left to the caller.
    ``requests`` only raises on a network failure, so a 500 or a 401 comes
    back as an ordinary response — and for four milestones every caller
    discarded it, which is why nothing we sent to Mastodon was ever seen to
    fail (R88). Logging in the primitive means a delivery path cannot be
    blind by forgetting to read what it got back.
    """
    body = json.dumps(with_context(activity)).encode()
    headers = sign_request("POST", inbox_url, private_pem, key_id=key_id, body=body)
    headers["Content-Type"] = "application/activity+json"
    headers["Accept"] = "application/activity+json"
    activity_type = activity.get("type", "?") if isinstance(activity, dict) else "?"
    try:
        response = requests.post(
            inbox_url, data=body, headers=headers, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        logger.warning("Outbound %s to %s failed: %s", activity_type, inbox_url, exc)
        raise
    if 200 <= response.status_code < 300:
        logger.info(
            "Outbound %s to %s -> HTTP %s",
            activity_type,
            inbox_url,
            response.status_code,
        )
    else:
        logger.warning(
            "Outbound %s to %s -> HTTP %s: %s",
            activity_type,
            inbox_url,
            response.status_code,
            _brief_body(response),
        )
    return response
