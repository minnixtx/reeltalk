"""The human profile surface (M5 increment 1).

The actor URL /user/<localname>/ now serves the Person document to AP
clients and a real profile page (avatar, name, handle, bio, films link) to
browsers — R40's redirect-to-films is superseded. Remote mirrors are
profiled too: their <preferredUsername>@<netloc> localname matches the
extended route pattern, and a missing avatar/bio triggers one throttled
best-effort fetch of the home Person document (identity fields untouched).
Profile editing (local users) stores the bio rendered with the markdown
source kept for pre-fill (R18's pattern).
"""

from io import BytesIO

import pytest
import responses
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from reeltalk.activitypub.crypto import generate_keypair
from reeltalk.social.models import User


def _tiny_jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (10, 10), (120, 80, 40)).save(buf, format="JPEG")
    return buf.getvalue()


def _mirror(localname="carol@remote.example", actor_url=None, **fields):
    """A remote-user mirror (public key only, as federation creates them)."""
    _private_pem, public_pem = generate_keypair()
    user = User(
        localname=localname,
        local=False,
        actor_url=actor_url
        or f"https://remote.example/user/{localname.split('@')[0]}/",
        public_key=public_pem,
        **fields,
    )
    user.set_unusable_password()
    user.save()
    return user


@pytest.fixture(autouse=True)
def _clear_cache():
    # The mirror-refresh throttle lives in the process cache; keep tests
    # independent of each other's fetch bookkeeping.
    cache.clear()
    yield
    cache.clear()


# --- Profile page (local users) ----------------------------------------------


@pytest.mark.django_db
def test_profile_page_renders_identity(client):
    user = User.objects.create_user(
        localname="alice", password="p", display_name="Alice A."
    )
    user.summary = "<p>Into the <strong>void</strong>.</p>"
    user.save()
    response = client.get("/user/alice/")
    assert response.status_code == 200
    content = response.content
    assert b"Alice A." in content
    assert b"@alice@localhost" in content  # username property (DOMAIN)
    assert b"<strong>void</strong>" in content
    assert b"/user/alice/films/" in content


@pytest.mark.django_db
def test_profile_page_avatar_and_placeholder(client):
    user = User.objects.create_user(localname="alice", password="p")
    user.avatar.save("a.jpg", SimpleUploadedFile("a.jpg", _tiny_jpeg()), save=True)
    # The stored name (get_available_name may suffix it on collision).
    assert user.avatar.url.encode() in client.get("/user/alice/").content

    bare = User.objects.create_user(localname="bob", password="p")
    page = client.get(f"/user/{bare.localname}/").content
    assert b"avatar-placeholder" in page


@pytest.mark.django_db
def test_profile_page_localname_case_insensitive(client):
    # R40: case variants are one identity; the stored spelling is canonical.
    User.objects.create_user(localname="Alice", password="p")
    response = client.get("/user/alice/")
    assert response.status_code == 200
    assert b"@Alice@localhost" in response.content


@pytest.mark.django_db
def test_profile_page_edit_button_only_for_self(client):
    User.objects.create_user(localname="alice", password="p")
    anonymous = client.get("/user/alice/").content
    assert b"profile-edit" not in anonymous

    client.login(username="alice", password="p")
    own = client.get("/user/alice/").content
    assert b"/preferences/profile/" in own

    User.objects.create_user(localname="bob", password="p")
    client.login(username="bob", password="p")
    assert b"/preferences/profile/" not in client.get("/user/alice/").content


@pytest.mark.django_db
def test_profile_page_unknown_user_404(client):
    assert client.get("/user/nobody/").status_code == 404
    assert (
        client.get("/user/nobody/", HTTP_ACCEPT="application/activity+json").status_code
        == 404
    )


# --- Profile page (remote mirrors) --------------------------------------------


@pytest.mark.django_db
def test_mirror_profile_page_renders(client):
    # Avatar + summary present, so the lazy refresh early-returns without an
    # outbound fetch (no responses mock registered here).
    mirror = _mirror(
        "carol@remote.example:8443",
        actor_url="https://remote.example:8443/user/carol/",
        display_name="Carol Remote",
        summary="<p>Fed from B.</p>",
    )
    mirror.avatar.save("c.jpg", SimpleUploadedFile("c.jpg", _tiny_jpeg()), save=True)
    # The localname carries '@' and ':' (the netloc's port) — the extended
    # route pattern must match it raw in the path…
    response = client.get("/user/carol@remote.example:8443/")
    assert response.status_code == 200
    content = response.content
    assert b"Carol Remote" in content
    assert b"@carol@remote.example:8443" in content
    assert b"remote.example:8443" in content  # home instance note
    # …and percent-encoded, as reverse() emits it for the films link.
    encoded = "/user/carol%40remote.example%3A8443/"
    assert client.get(encoded).status_code == 200
    assert client.get(encoded + "films/").status_code == 200


@pytest.mark.django_db
def test_mirror_profile_ap_client_404(client):
    # A mirror's canonical Person document lives on its home instance (R42).
    _mirror("carol@remote.example")
    response = client.get(
        "/user/carol@remote.example/", HTTP_ACCEPT="application/activity+json"
    )
    assert response.status_code == 404


# --- Mirror profile refresh (lazy avatar/bio fill) -----------------------------

REMOTE_ACTOR = "https://remote.example/user/carol/"


def _person_doc_with_profile(public_pem: str) -> dict:
    return {
        "@context": ["https://www.w3.org/ns/activitystreams"],
        "id": REMOTE_ACTOR,
        "type": "Person",
        "preferredUsername": "carol",
        "name": "Carol Remote",
        "summary": "<p>Hi, I <b>like</b> films.<script>alert(1)</script></p>",
        "image": "https://remote.example/images/carol.jpg",
        "publicKey": {
            "id": f"{REMOTE_ACTOR}#main-key",
            "owner": REMOTE_ACTOR,
            "publicKeyPem": public_pem,
        },
    }


@pytest.mark.django_db
@responses.activate
def test_mirror_refresh_fills_summary_and_avatar(client):
    mirror = _mirror("carol@remote.example", actor_url=REMOTE_ACTOR)
    doc = _person_doc_with_profile(mirror.public_key)
    responses.add(responses.GET, REMOTE_ACTOR, json=doc)
    responses.add(responses.GET, doc["image"], body=_tiny_jpeg())

    response = client.get("/user/carol@remote.example/")
    assert response.status_code == 200
    assert len(responses.calls) == 2  # Person doc + avatar

    mirror.refresh_from_db()
    assert mirror.display_name == "Carol Remote"
    # Sanitized through the user-content allowlist: <b> kept, <script> gone.
    assert "<b>like</b>" in mirror.summary
    assert "<script>" not in mirror.summary
    assert mirror.avatar  # downloaded and stored


@pytest.mark.django_db
@responses.activate
def test_mirror_refresh_is_throttled(client):
    mirror = _mirror("carol@remote.example", actor_url=REMOTE_ACTOR)
    doc = _person_doc_with_profile(mirror.public_key)
    responses.add(responses.GET, REMOTE_ACTOR, json=doc)
    responses.add(responses.GET, doc["image"], body=_tiny_jpeg())

    client.get("/user/carol@remote.example/")
    # Second view inside the throttle window: no further outbound fetches.
    client.get("/user/carol@remote.example/")
    assert len(responses.calls) == 2


@pytest.mark.django_db
def test_mirror_refresh_skips_when_complete(client):
    mirror = _mirror(
        "carol@remote.example",
        actor_url=REMOTE_ACTOR,
        display_name="Carol Remote",
        summary="<p>Done.</p>",
    )
    mirror.avatar.save("c.jpg", SimpleUploadedFile("c.jpg", _tiny_jpeg()), save=True)

    response = client.get("/user/carol@remote.example/")
    assert response.status_code == 200
    # Nothing missing — no fetch at all (no responses mock registered).


@pytest.mark.django_db
@responses.activate
def test_mirror_refresh_failure_is_silent(client):
    mirror = _mirror("carol@remote.example", actor_url=REMOTE_ACTOR)
    responses.add(responses.GET, REMOTE_ACTOR, status=404)

    response = client.get("/user/carol@remote.example/")
    assert response.status_code == 200  # renders with what we have
    mirror.refresh_from_db()
    assert not mirror.summary and not mirror.avatar


@pytest.mark.django_db
@responses.activate
def test_mirror_refresh_never_touches_identity_fields(client):
    mirror = _mirror("carol@remote.example", actor_url=REMOTE_ACTOR)
    original_key = mirror.public_key
    doc = _person_doc_with_profile(original_key)
    # A rotated key in the document must not replace the stored one (R42:
    # no key rotation — a rotated key would fail signature verification).
    _private_pem, rotated_pem = generate_keypair()
    doc["publicKey"]["publicKeyPem"] = rotated_pem
    responses.add(responses.GET, REMOTE_ACTOR, json=doc)
    responses.add(responses.GET, doc["image"], body=_tiny_jpeg())

    client.get("/user/carol@remote.example/")
    mirror.refresh_from_db()
    assert mirror.public_key == original_key


# --- Profile editing (local users) ---------------------------------------------


@pytest.mark.django_db
def test_profile_edit_requires_login(client):
    response = client.get("/preferences/profile/")
    assert response.status_code == 302
    assert "/login/" in response["Location"]


@pytest.mark.django_db
def test_profile_edit_renders_prefilled(client):
    user = User.objects.create_user(
        localname="alice", password="p", display_name="Alice A."
    )
    user.raw_summary = "Into the *void*."
    user.save()
    client.login(username="alice", password="p")
    response = client.get("/preferences/profile/")
    assert response.status_code == 200
    content = response.content
    assert b"Alice A." in content
    # The markdown source pre-fills the bio, not the stored HTML markup.
    assert b"Into the *void*." in content


@pytest.mark.django_db
def test_profile_edit_saves_name_bio_and_avatar(client):
    user = User.objects.create_user(localname="alice", password="p")
    client.login(username="alice", password="p")
    response = client.post(
        "/preferences/profile/",
        {
            "display_name": "Alice Anderson",
            "summary": "Likes *noir*. See https://example.com/x.",
            "avatar": SimpleUploadedFile("a.jpg", _tiny_jpeg(), "image/jpeg"),
        },
    )
    assert response.status_code == 302
    assert response["Location"] == "/user/alice/"

    user.refresh_from_db()
    assert user.display_name == "Alice Anderson"
    assert user.raw_summary == "Likes *noir*. See https://example.com/x."
    # Rendered at write time; the disallowed link's href is stripped but its
    # anchor text stays (R25) — no live <a> remains.
    assert "<em>noir</em>" in user.summary
    assert "<a href=" not in user.summary
    assert user.avatar


@pytest.mark.django_db
def test_profile_edit_keeps_existing_avatar_when_none_uploaded(client):
    user = User.objects.create_user(localname="alice", password="p")
    user.avatar.save("old.jpg", SimpleUploadedFile("old.jpg", _tiny_jpeg()), save=True)
    client.login(username="alice", password="p")
    client.post(
        "/preferences/profile/",
        {"display_name": "A.", "summary": ""},
    )
    user.refresh_from_db()
    assert user.avatar.name.endswith("old.jpg")
