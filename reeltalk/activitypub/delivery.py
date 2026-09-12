"""Outgoing ActivityPub delivery (M4 increment 5).

Sends a signed activity to a remote inbox. Increment 5 uses it for the
Follow / Undo(Follow) a local user initiates toward a remote user; increment
6 reuses it for status broadcasts to followers' inboxes. Built fresh against
the spec (R7): the request is signed per RFC 9421 with Ed25519 (R39) via
``signatures.sign_request``, covering ``@method`` + ``@target-uri`` and the
body's content-digest — the format current Mastodon verifies.

Delivery in v0.1 is a single synchronous POST with no retry queue: a network
failure surfaces as a ``requests.RequestException`` for the caller to handle
(the local follow state has already been recorded, so the relationship is not
lost — only the remote's copy of it lags).
"""

import json

import requests

from .signatures import sign_request

# Same reachability budget as the inbound Person-document fetch (mirrors).
REQUEST_TIMEOUT = 10


def deliver_activity(inbox_url: str, activity: dict, private_pem: str, key_id: str):
    """POST ``activity`` to ``inbox_url``, signed with the sender's key.

    ``private_pem`` is the Ed25519 private key of the local user sending;
    ``key_id`` is that user's signing-key URL (actor URL + ``#main-key``,
    R39). Returns the ``requests.Response`` so a caller can inspect the
    status; raises ``requests.RequestException`` on network failure.
    """
    body = json.dumps(activity).encode()
    headers = sign_request("POST", inbox_url, private_pem, key_id=key_id, body=body)
    headers["Content-Type"] = "application/activity+json"
    headers["Accept"] = "application/activity+json"
    return requests.post(inbox_url, data=body, headers=headers, timeout=REQUEST_TIMEOUT)
