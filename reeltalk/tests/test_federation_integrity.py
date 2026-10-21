"""Federation ingest integrity (security increment S3, F2 + F3).

Two forged-identity gaps at the ingest door, both from the same shape of
mistake: the signature proves *who sent an activity*, and nothing more. The
module's attribution rule credits a **new** mirror to the verified sender,
which says nothing about who may **mutate a row that already exists** - and
about a fetched document, it says nothing about whether the document
describes the actor it was fetched for.

* **F2.** ``Update``/``Delete`` looked a mirror up by its object url and
  applied the change without checking the verified sender against the row's
  author. Any peer with a working keypair could overwrite or tombstone
  another remote user's mirrored content by replaying its object id. Both
  paths now drop (and log) the activity when the sender is not the author -
  the same graceful-ignore posture as the other unforged-but-unusable
  shapes.
* **F3.** A fetched Person document's ``id`` was never checked against the
  URL it was fetched from, and the mirror is keyed on that id. A server
  answering a fetch of its own actor URL with somebody else's id would mint
  a mirror impersonating that identity - and occupy the unique ``actor_url``
  slot the real actor needs to mirror here. ``fetch_person_document`` now
  refuses the mismatch beside its other unusable-document guards.

Every activity goes through the real ``/inbox/`` route with a real
signature, as ``test_federation_inbound.py`` and
``test_federation_sanitize.py`` deliver it, so sender resolution and the
dedup layer are exercised rather than bypassed. The honest ``Update`` and
``Delete`` are the regression guard: a change that dropped *every* update
would pass the forged tests just as well, and would quietly break
federation - so the author's own pair must stay green in the same file.
"""

import json
import logging

import pytest
import responses
from django.contrib.auth import get_user_model
from django.test import Client

from reeltalk.activitypub import crypto, signatures
from reeltalk.core.models import Status

User = get_user_model()

CAROL = "https://peer.example/users/carol/"
CAROL_KEY_ID = f"{CAROL}#main-key"
MALLORY = "https://mallory.example/users/mallory/"
MALLORY_KEY_ID = f"{MALLORY}#main-key"
NOTE_URL = "https://peer.example/notes/1/"
STATUSES_LOGGER = "reeltalk.activitypub.statuses"


def _mirror(localname, actor_url):
    """A remote member, already mirrored, holding its own signing key.

    Created directly rather than through first contact (as in
    ``test_federation_sanitize.py``): signature verification then needs no
    HTTP, so anything ``responses`` records is a fetch the ingest path made,
    not one establishing the sender.
    """
    private_pem, public_pem = crypto.generate_keypair()
    user = User(
        localname=localname,
        local=False,
        actor_url=actor_url,
        inbox_url=f"{actor_url}inbox",
        public_key=public_pem,
    )
    user.set_unusable_password()
    user.save()
    return user, private_pem


@pytest.fixture()
def carol(db):
    """The note's author: the mirror every forged activity replays."""
    return _mirror("carol@peer.example", CAROL)


@pytest.fixture()
def mallory(db):
    """A second verified sender: valid of its own, not of Carol's rows."""
    return _mirror("mallory@mallory.example", MALLORY)


# --- wire builders and delivery -------------------------------------------


def _deliver(private_pem, key_id, activity):
    """Send one signed activity through the real inbox route."""
    body = json.dumps(activity).encode()
    headers = signatures.sign_request(
        "POST", "http://testserver/inbox/", private_pem, key_id=key_id, body=body
    )
    meta = {f"HTTP_{n.upper().replace('-', '_')}": v for n, v in headers.items()}
    meta["HTTP_HOST"] = "testserver"
    return Client().post(
        "/inbox/", data=body, content_type="application/activity+json", **meta
    )


def _activity(kind, obj, seq, actor):
    return {
        "id": f"{actor}#activity-{seq}",
        "type": kind,
        "actor": actor,
        "object": obj,
    }


def _note(content, *, url=NOTE_URL, attributed_to=CAROL, **extra):
    doc = {
        "id": url,
        "type": "Note",
        "attributedTo": attributed_to,
        "content": content,
        "publishedTime": "2026-10-10T10:00:00Z",
    }
    doc.update(extra)
    return doc


def _mirrored_note(url=NOTE_URL):
    return Status.objects.get(local=False, remote_url=url)


# --- F2: the author's own Update and Delete still work ----------------------


@responses.activate
@pytest.mark.django_db
def test_the_author_can_still_update_her_mirror(carol):
    """The regression guard: the honest Update path stays open.

    Without this pair the forged tests would pass just as well if the guard
    dropped *every* update, federation quietly broken.
    """
    _carol_user, carol_key = carol
    create = _activity("Create", _note("<p>First draft.</p>"), 1, CAROL)
    assert _deliver(carol_key, CAROL_KEY_ID, create).status_code == 202
    update = _activity(
        "Update",
        _note("<p>Second draft.</p>", editedTime="2026-10-11T10:00:00Z"),
        2,
        CAROL,
    )
    assert _deliver(carol_key, CAROL_KEY_ID, update).status_code == 202

    status = _mirrored_note()
    assert status.content == "<p>Second draft.</p>"
    assert status.edited_date is not None


@responses.activate
@pytest.mark.django_db
def test_the_author_can_still_delete_her_mirror(carol):
    """The other half of the regression guard: the honest Delete lands."""
    _carol_user, carol_key = carol
    create = _activity("Create", _note("<p>Draft.</p>"), 1, CAROL)
    assert _deliver(carol_key, CAROL_KEY_ID, create).status_code == 202
    delete = _activity("Delete", {"id": NOTE_URL, "type": "Note"}, 2, CAROL)
    assert _deliver(carol_key, CAROL_KEY_ID, delete).status_code == 202

    assert _mirrored_note().deleted is True


# --- F2: a stranger's Update and Delete are dropped -------------------------


@responses.activate
@pytest.mark.django_db
def test_a_forged_update_from_another_sender_is_ignored(carol, mallory, caplog):
    """Mallory signs honestly - for a row that is not hers (F2).

    The signature verifies, so the only thing standing between her and
    Carol's words is the ownership check. The row must stay exactly as its
    author left it: same content, same author, no edited stamp - and the
    drop must be logged, so a forgery attempt is visible from the logs.
    """
    carol_user, carol_key = carol
    _mallory_user, mallory_key = mallory
    create = _activity("Create", _note("<p>Carol's own words.</p>"), 1, CAROL)
    assert _deliver(carol_key, CAROL_KEY_ID, create).status_code == 202

    forged = _note(
        "<p>Put in Carol's mouth.</p>",
        attributed_to=MALLORY,
        editedTime="2026-10-11T10:00:00Z",
    )
    with caplog.at_level(logging.INFO, logger=STATUSES_LOGGER):
        response = _deliver(
            mallory_key, MALLORY_KEY_ID, _activity("Update", forged, 2, MALLORY)
        )

    # Graceful-ignore posture: acknowledged, so Mallory's queue drains,
    # and nothing applied.
    assert response.status_code == 202
    status = _mirrored_note()
    assert status.content == "<p>Carol's own words.</p>"
    assert status.user_id == carol_user.pk
    assert status.edited_date is None
    assert "not the author" in caplog.text


@responses.activate
@pytest.mark.django_db
def test_a_forged_delete_from_another_sender_is_ignored(carol, mallory, caplog):
    """The tombstone door is the same door: replaying an id is not owning it."""
    _carol_user, carol_key = carol
    _mallory_user, mallory_key = mallory
    create = _activity("Create", _note("<p>Carol's own words.</p>"), 1, CAROL)
    assert _deliver(carol_key, CAROL_KEY_ID, create).status_code == 202

    forged = _activity("Delete", {"id": NOTE_URL, "type": "Note"}, 2, MALLORY)
    with caplog.at_level(logging.INFO, logger=STATUSES_LOGGER):
        response = _deliver(mallory_key, MALLORY_KEY_ID, forged)

    assert response.status_code == 202
    assert _mirrored_note().deleted is False
    assert "not the author" in caplog.text


@responses.activate
@pytest.mark.django_db
def test_the_guard_keys_on_ownership_not_on_the_sender(mallory):
    """Non-vacuity for the forged pair: Mallory is not banned, just not Carol.

    Mallory mirrors her own note and edits her own note: both land. A guard
    that froze every second-sender activity would pass the forged tests too;
    this one cannot.
    """
    mallory_user, mallory_key = mallory
    own_url = f"{MALLORY}notes/2/"
    create = _activity(
        "Create",
        _note("<p>Mallory's own words.</p>", url=own_url, attributed_to=MALLORY),
        1,
        MALLORY,
    )
    assert _deliver(mallory_key, MALLORY_KEY_ID, create).status_code == 202
    update = _activity(
        "Update",
        _note(
            "<p>Mallory's edit.</p>",
            url=own_url,
            attributed_to=MALLORY,
            editedTime="2026-10-11T10:00:00Z",
        ),
        2,
        MALLORY,
    )
    assert _deliver(mallory_key, MALLORY_KEY_ID, update).status_code == 202

    status = _mirrored_note(own_url)
    assert status.content == "<p>Mallory's edit.</p>"
    assert status.user_id == mallory_user.pk


# --- F3: a Person document must describe the actor it was fetched for -------


def _person_doc(actor_url, localname, public_pem):
    return {
        "id": actor_url,
        "type": "Person",
        "preferredUsername": localname,
        "inbox": f"{actor_url}inbox",
        "publicKey": {
            "id": f"{actor_url}#main-key",
            "owner": actor_url,
            "publicKeyPem": public_pem,
        },
    }


@responses.activate
@pytest.mark.django_db
def test_a_person_document_impersonating_another_actor_is_refused():
    """F3 through the real door: first contact whose document lies.

    Mallory's keyid resolves - by fetch of Mallory's URL - to a Person
    document carrying the victim's id and *Mallory's* key. The signature
    would verify against that key, which is exactly why the identity has to
    come from the id/url match: a mirror minted here would impersonate the
    victim's actor url on this box and take the unique ``actor_url`` slot
    the real victim needs to federate here at all.
    """
    private_pem, public_pem = crypto.generate_keypair()
    victim = "https://good.example/users/carol/"
    responses.add(responses.GET, MALLORY, json=_person_doc(victim, "carol", public_pem))

    activity = _activity(
        "Create",
        _note("<p>Someone else's voice.</p>", attributed_to=victim),
        1,
        MALLORY,
    )
    assert _deliver(private_pem, MALLORY_KEY_ID, activity).status_code == 401
    # Neither identity is minted: the impersonated url takes no slot, and
    # the lying server acquires no mirror of its own either.
    assert not User.objects.filter(local=False).exists()


@responses.activate
@pytest.mark.django_db
def test_first_contact_with_a_matching_document_still_mirrors():
    """The control: honest first contact still mints, through the same door."""
    private_pem, public_pem = crypto.generate_keypair()
    responses.add(
        responses.GET, MALLORY, json=_person_doc(MALLORY, "mallory", public_pem)
    )

    activity = _activity(
        "Create",
        _note("<p>Mallory's own voice.</p>", attributed_to=MALLORY),
        1,
        MALLORY,
    )
    assert _deliver(private_pem, MALLORY_KEY_ID, activity).status_code == 202

    mirror = User.objects.get(local=False, actor_url=MALLORY)
    assert mirror.public_key == public_pem
    assert _mirrored_note().user_id == mirror.pk
