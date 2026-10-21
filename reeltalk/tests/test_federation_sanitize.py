"""Remote HTML is sanitized on ingest (security increment S2, F1).

Local prose is sanitized at write time by ``render_markdown``. The federation
ingest path stored a remote Note's ``content``, a Film document's ``summary``
and a list extension's ``summary`` **as received**, and every read path prints
those fields with ``|safe`` — the feed, the film page, the list page, the
status page, and the moderator's queue. So any peer could post
``<img src=x onerror=...>`` and have it execute on this instance, including in
front of a staff session reading the queue.

Every activity here goes through the real ``/inbox/`` route with a real
signature, so sender resolution and the dedup layer are exercised rather than
bypassed — the same posture as the inbound ``Like`` and list-import tests. The
assertions are on the **stored row**, because that is where the fix lives:
bleach escapes what it refuses rather than stripping it, so the proof is the
hostile tag present as inert text and absent as markup.

Ordered by how each failure would bite:

* The three ingest sites, create branch: a Note body, a Film summary, a list
  description.
* The update branches, which are a separate line of code and were separately
  unsanitized — an ``Update`` is how a peer that was missed the first time
  catches up.
* The fetched-Film entry point, which reaches ``_mirror_film`` from a review's
  film reference rather than from a ``Create`` naming the film directly.
* The moderation queue, the worst-case render: XSS delivered to a moderator.
* The controls, which are what stop any of the above passing vacuously: prose
  that should survive does survive, an already-escaped body is not escaped
  twice, and the outbound-link allowlist now applies to remote content too.
"""

import json

import pytest
import responses
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from reeltalk.activitypub import crypto, signatures
from reeltalk.activitypub.objects import LIST_EXTENSION
from reeltalk.core.models import Film, Status
from reeltalk.lists.models import FilmList
from reeltalk.social.models import LinkDomain
from reeltalk.tests.members import member, moderator

User = get_user_model()

PEER = "https://peer.example"
REMOTE_ACTOR = f"{PEER}/users/carol/"
REMOTE_INBOX = f"{REMOTE_ACTOR}inbox"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"

# A body that is hostile in the two ways the finding names: a script element,
# and an event handler on a tag the allowlist has never carried.
HOSTILE = '<p>Quiet film.</p><script>alert(1)</script><img src=x onerror="alert(2)">'
HOSTILE_TEXT = "&lt;script&gt;alert(1)&lt;/script&gt;"


@pytest.fixture()
def keypair():
    return crypto.generate_keypair()


@pytest.fixture()
def carol(db, keypair):
    """The remote peer account, already mirrored, holding the signing key.

    Created directly rather than through a first-contact ``Follow`` so that
    signature verification needs no HTTP: ``resolve_sender`` finds the mirror
    by ``actor_url`` and reads the stored key. Any call ``responses`` records
    is therefore one the ingest path made, not one establishing the sender.
    """
    _private_pem, public_pem = keypair
    user = User(
        localname="carol@peer.example",
        local=False,
        actor_url=REMOTE_ACTOR,
        inbox_url=REMOTE_INBOX,
        public_key=public_pem,
    )
    user.set_unusable_password()
    user.save()
    return user


@pytest.fixture()
def alice(db):
    return member(localname="alice", password="s3cretpass")


@pytest.fixture()
def mod(db):
    return moderator(localname="mod", password="s3cretpass")


# --- wire builders and delivery -------------------------------------------


def _deliver(private_pem, activity):
    """Send one signed activity through the real inbox route."""
    body = json.dumps(activity).encode()
    headers = signatures.sign_request(
        "POST", "http://testserver/inbox/", private_pem, key_id=REMOTE_KEY_ID, body=body
    )
    meta = {f"HTTP_{n.upper().replace('-', '_')}": v for n, v in headers.items()}
    meta["HTTP_HOST"] = "testserver"
    return Client().post(
        "/inbox/", data=body, content_type="application/activity+json", **meta
    )


def _activity(kind, obj, seq):
    return {
        "id": f"{REMOTE_ACTOR}#activity-{seq}",
        "type": kind,
        "actor": REMOTE_ACTOR,
        "object": obj,
    }


def _note(content, *, url=f"{PEER}/notes/1/", **extra):
    doc = {
        "id": url,
        "type": "Note",
        "attributedTo": REMOTE_ACTOR,
        "content": content,
        "publishedTime": "2026-10-10T10:00:00Z",
    }
    doc.update(extra)
    return doc


def _film(summary, *, url=f"{PEER}/film/11/", **extra):
    doc = {
        "id": url,
        "type": "Film",
        "name": "The Hostile Reissue",
        "summary": summary,
    }
    doc.update(extra)
    return doc


def _mirrored_note():
    return Status.objects.get(local=False, remote_url=f"{PEER}/notes/1/")


# --- the three ingest sites, create branch ---------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_note_body_is_stored_escaped(carol, keypair):
    private_pem, _public = keypair
    assert (
        _deliver(private_pem, _activity("Create", _note(HOSTILE), 1)).status_code == 202
    )

    content = _mirrored_note().content
    assert "<script>" not in content
    assert "<img" not in content
    assert HOSTILE_TEXT in content
    # The prose the body came with is still there: escaping, not deleting.
    assert "Quiet film." in content


@responses.activate
@pytest.mark.django_db
def test_a_film_mirror_summary_is_stored_escaped(carol, keypair):
    private_pem, _public = keypair
    assert (
        _deliver(private_pem, _activity("Create", _film(HOSTILE), 2)).status_code == 202
    )

    film = Film.objects.get(remote_url=f"{PEER}/film/11/")
    assert "<script>" not in film.description
    assert "<img" not in film.description
    assert HOSTILE_TEXT in film.description


@responses.activate
@pytest.mark.django_db
def test_a_list_description_is_stored_escaped(carol, keypair):
    private_pem, _public = keypair
    note = _note(
        "<p>a composed body</p>",
        **{
            LIST_EXTENSION: {
                "type": "reeltalk:List",
                "url": f"{PEER}/list/77/",
                "name": "Noir You Must See",
                "summary": HOSTILE,
                "orderedItems": [],
            }
        },
    )
    assert _deliver(private_pem, _activity("Create", note, 3)).status_code == 202

    film_list = FilmList.objects.get(remote_url=f"{PEER}/list/77/")
    assert "<script>" not in film_list.description
    assert "<img" not in film_list.description
    assert HOSTILE_TEXT in film_list.description


# --- the update branches ----------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_note_update_is_stored_escaped(carol, keypair):
    """The update branch is its own line of code and was its own hole.

    Deliberately create-with-clean then update-to-hostile: a create that was
    already hostile would pass here even if the update path stored raw bytes,
    which is exactly how this half of the finding stayed open.
    """
    private_pem, _public = keypair
    _deliver(private_pem, _activity("Create", _note("<p>Plain.</p>"), 4))
    _deliver(
        private_pem,
        _activity("Update", _note(HOSTILE, editedTime="2026-10-11T10:00:00Z"), 5),
    )

    content = _mirrored_note().content
    assert "<script>" not in content
    assert "<img" not in content
    assert HOSTILE_TEXT in content


@responses.activate
@pytest.mark.django_db
def test_a_list_update_description_is_stored_escaped(carol, keypair):
    private_pem, _public = keypair
    ext = {
        "type": "reeltalk:List",
        "url": f"{PEER}/list/77/",
        "name": "Noir You Must See",
        "summary": "<p>Clean.</p>",
        "orderedItems": [],
    }
    _deliver(
        private_pem,
        _activity("Create", _note("<p>a body</p>", **{LIST_EXTENSION: ext}), 6),
    )
    hostile = {**ext, "summary": HOSTILE}
    _deliver(
        private_pem,
        _activity(
            "Update",
            _note("<p>a body</p>", **{LIST_EXTENSION: hostile}),
            7,
        ),
    )

    description = FilmList.objects.get(remote_url=f"{PEER}/list/77/").description
    assert "<script>" not in description
    assert HOSTILE_TEXT in description


# --- the fetched-Film entry point ------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_film_fetched_for_a_review_is_stored_escaped(carol, keypair):
    """The other door into ``_mirror_film``: a review naming a film we lack.

    ``_mirror_film`` is create-only and has one body, but it is reached from
    two places — a ``Create`` naming the Film directly (above) and a review
    whose ``film`` reference resolves only by fetching. Both must land
    sanitized, so both are pinned.
    """
    private_pem, _public = keypair
    responses.add(
        responses.GET,
        f"{PEER}/film/11/",
        json=_film(HOSTILE),
    )
    note = _note(
        "<p>A reissue.</p>",
        url=f"{PEER}/notes/9/",
        film=f"{PEER}/film/11/",
        rating=4,
    )
    assert _deliver(private_pem, _activity("Create", note, 8)).status_code == 202

    film = Film.objects.get(remote_url=f"{PEER}/film/11/")
    assert "<script>" not in film.description
    assert HOSTILE_TEXT in film.description


# --- the moderator's queue --------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_the_moderation_queue_renders_a_hostile_body_as_text(
    carol, keypair, alice, mod
):
    """The worst-case render: the payload reaches a staff session.

    Filed through the real report route and read through the real queue page,
    because the finding is about the render and a unit assert on the row would
    not notice a template that stopped using ``|safe`` in either direction.
    """
    private_pem, _public = keypair
    _deliver(private_pem, _activity("Create", _note(HOSTILE), 9))
    status = _mirrored_note()

    reporter = Client()
    reporter.force_login(alice)
    assert (
        reporter.post(
            reverse("report-status", args=[status.pk]), {"category": "spam"}
        ).status_code
        == 302
    )

    queue = Client()
    queue.force_login(mod)
    response = queue.get(reverse("moderation"))
    assert response.status_code == 200
    page = response.content.decode()
    # Asserted as markup, not as a substring of the payload: bleach escapes a
    # refused tag rather than deleting it, so the escaped text of the handler
    # legitimately survives on the page as words a moderator can read.
    assert "<script>alert(1)</script>" not in page
    assert '<img src=x onerror="alert(2)">' not in page
    assert HOSTILE_TEXT in page


# --- the controls -----------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_ordinary_remote_prose_survives_the_gate(carol, keypair):
    """Non-vacuity for all three asserts above.

    A change that blanked or shredded every remote body would pass them. The
    allowlist's own tags come through untouched.
    """
    private_pem, _public = keypair
    prose = "<p>Two <em>words</em>: <strong>great</strong> film.</p>"
    _deliver(private_pem, _activity("Create", _note(prose), 10))
    assert _mirrored_note().content == prose


@responses.activate
@pytest.mark.django_db
def test_an_already_escaped_body_is_not_escaped_twice(carol, keypair):
    """One sanitize pass per field, not one per branch.

    A peer that pre-escapes its own prose must not arrive doubled: ``&lt;``
    surviving as ``&lt;`` is the signature of exactly one pass.
    """
    private_pem, _public = keypair
    _deliver(
        private_pem,
        _activity("Create", _note("<p>&lt;script&gt;</p>"), 11),
    )
    assert _mirrored_note().content == "<p>&lt;script&gt;</p>"


@responses.activate
@pytest.mark.django_db
def test_a_remote_link_follows_the_same_domain_allowlist_as_local_content(
    carol, keypair
):
    """The consequence to own: remote bodies now pass the local gate, whole.

    ``sanitize_html`` gates an anchor's ``href`` on the instance's
    ``LinkDomain`` allowlist. Applying it to remote content therefore also
    applies that rule to remote content — a link to a domain this instance
    has not allowed loses its ``href``, exactly as a local member's would.
    Pinned in both directions so the gate cannot be quietly widened here.
    """
    private_pem, _public = keypair
    body = '<p>See <a href="https://elsewhere.example/x">this</a>.</p>'
    _deliver(private_pem, _activity("Create", _note(body), 12))

    assert _mirrored_note().content == "<p>See <a>this</a>.</p>"


@responses.activate
@pytest.mark.django_db
def test_a_remote_link_to_an_allowed_domain_keeps_its_href(carol, keypair):
    private_pem, _public = keypair
    LinkDomain.objects.create(domain="example.org")
    body = '<p>See <a href="https://example.org/x">this</a>.</p>'
    _deliver(private_pem, _activity("Create", _note(body), 13))

    assert _mirrored_note().content == body


@pytest.mark.django_db
def test_a_non_string_content_is_ignored_rather_than_raising(carol, keypair):
    """A hostile shape must not become an undeliverable one.

    bleach takes strings only. A ``TypeError`` inside a handler rolls the
    activity back, so a peer sending ``"content": 5`` would be handed a
    delivery it can never complete; §3.6 wants the shape ignored instead.
    """
    private_pem, _public = keypair
    assert _deliver(private_pem, _activity("Create", _note(5), 14)).status_code == 202
    assert (
        Status.objects.filter(local=False, remote_url=f"{PEER}/notes/1/").count() == 0
    )
