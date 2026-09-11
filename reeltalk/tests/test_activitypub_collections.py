"""ActivityPub collections + object wire types (M4 increment 3, R41).

The OrderedCollection/Page pagination helpers, the Film (D15) and Note
serializers, the Create-activity outbox, the followers/following Person
collections, the inbox routes, content-negotiated Film serving, and the
day-one origin_id identity backfill.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.base import ContentFile
from django.test import RequestFactory
from django.utils import timezone
from PIL import Image

from reeltalk.activitypub.collections import (
    PAGE_SIZE,
    collection_document,
    last_page,
    page_document,
    parse_page,
)
from reeltalk.activitypub.objects import create_activity, film_document, note_document
from reeltalk.core.models import Film, Status
from reeltalk.social.models import User

AP_ACCEPT = "application/activity+json"


def _tiny_jpeg() -> bytes:
    buf = __import__("io").BytesIO()
    Image.new("RGB", (10, 10), (120, 80, 40)).save(buf, format="JPEG")
    return buf.getvalue()


# --- Collection pagination helpers ------------------------------------------


def test_collection_document_shape():
    doc = collection_document("http://testserver/user/alice/outbox/", 5)
    assert doc["id"] == "http://testserver/user/alice/outbox/"
    assert doc["type"] == "OrderedCollection"
    assert doc["totalItems"] == 5
    assert doc["first"] == "http://testserver/user/alice/outbox/?page=1"
    assert doc["last"] == "http://testserver/user/alice/outbox/?page=1"


def test_collection_document_empty_has_single_page():
    doc = collection_document("http://testserver/x/", 0)
    assert doc["totalItems"] == 0
    assert doc["last"].endswith("?page=1")


@pytest.mark.parametrize(
    ("total", "expected"),
    [(0, 1), (1, 1), (20, 1), (21, 2), (40, 2), (41, 3)],
)
def test_last_page(total, expected):
    assert last_page(total) == expected


def test_page_document_shape():
    doc = page_document("http://testserver/x/", [{"id": "a"}], start_index=21)
    assert doc["type"] == "OrderedCollectionPage"
    assert doc["partOf"] == "http://testserver/x/"
    assert doc["startIndex"] == 21
    assert doc["items"] == [{"id": "a"}]
    # The page id is its ?page= URL (start 21 -> page 2 at PAGE_SIZE 20).
    assert doc["id"] == f"http://testserver/x/?page={21 // PAGE_SIZE + 1}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 1), ("1", 1), ("3", 3), ("abc", 1), ("0", 1), ("-4", 1)],
)
def test_parse_page(raw, expected):
    query = {} if raw is None else {"page": raw}
    request = RequestFactory().get("/x/", query)
    assert parse_page(request) == expected


# --- Film serializer (D15) --------------------------------------------------


@pytest.mark.django_db
def test_film_document_minimal():
    film = Film.objects.create(title="Arrival")
    request = RequestFactory().get("/")
    doc = film_document(film, request)
    assert doc["@context"][0] == "https://www.w3.org/ns/activitystreams"
    # origin_id is backfilled to the pk on create (R41).
    assert film.origin_id == film.pk
    assert doc["id"] == f"http://testserver/film/{film.pk}/"
    assert doc["type"] == "Film"
    assert doc["name"] == "Arrival"
    assert doc["url"] == doc["id"]
    # Empty optional fields are omitted, not null.
    for key in (
        "subtitle",
        "summary",
        "year",
        "runtime",
        "genres",
        "directors",
        "cast",
        "image",
        "tmdbId",
        "imdbId",
    ):
        assert key not in doc


@pytest.mark.django_db
def test_film_document_full():
    film = Film.objects.create(
        title="The Message",
        subtitle="Extended",
        description="<p>A contact drama.</p>",
        year=2021,
        runtime=158,
        genres=["Drama", "Sci-Fi"],
        directors=["A. Director"],
        cast=["B. Actor", "C. Actress"],
        tmdb_id=78,
        imdb_id="tt0133093",
    )
    film.poster.save("p.jpg", ContentFile(_tiny_jpeg()), save=True)
    request = RequestFactory().get("/")
    doc = film_document(film, request)
    assert doc["subtitle"] == "Extended"
    assert doc["summary"] == "<p>A contact drama.</p>"
    assert doc["year"] == 2021
    assert doc["runtime"] == 158
    assert doc["genres"] == ["Drama", "Sci-Fi"]
    assert doc["directors"] == ["A. Director"]
    assert doc["cast"] == ["B. Actor", "C. Actress"]
    assert doc["image"] == "http://testserver" + film.poster.url
    assert doc["tmdbId"] == 78
    assert doc["imdbId"] == "tt0133093"


@pytest.mark.django_db
def test_film_document_uses_explicit_origin_id():
    film = Film.objects.create(title="X")
    # A non-default origin id (e.g. assigned by federation) wins over the pk.
    Film.objects.filter(pk=film.pk).update(origin_id=9999)
    film.refresh_from_db()
    doc = film_document(film, RequestFactory().get("/"))
    assert doc["id"] == "http://testserver/film/9999/"


# --- Note serializer --------------------------------------------------------


@pytest.mark.django_db
def _make_status(user, *, content="", rating=None, film=None, when=None):
    # A status anchored to a film is a review; one without is a standalone
    # note (status_type None) — typed statuses must be film-anchored (R17).
    return Status.objects.create(
        user=user,
        film=film,
        status_type="review" if film is not None else None,
        content=content,
        rating=rating,
        published_date=when or timezone.now(),
    )


@pytest.mark.django_db
def test_note_document_minimal():
    user = User.objects.create_user(localname="alice", password="p")
    status = _make_status(user, content="Solid.")
    doc = note_document(status, RequestFactory().get("/"))
    assert doc["type"] == "Note"
    assert doc["id"] == f"http://testserver/status/{status.pk}/"
    assert doc["attributedTo"] == "http://testserver/user/alice/"
    assert doc["content"] == "Solid."
    # RFC 3339 UTC with a Z suffix.
    assert doc["publishedTime"].endswith("Z")
    for key in ("rating", "film", "inReplyTo", "editedTime"):
        assert key not in doc


@pytest.mark.django_db
def test_note_document_rating_and_film():
    user = User.objects.create_user(localname="alice", password="p")
    film = Film.objects.create(title="Arrival", year=2016)
    status = _make_status(user, content="Loved it.", rating=Decimal("4.5"), film=film)
    doc = note_document(status, RequestFactory().get("/"))
    assert doc["rating"] == 4.5
    assert doc["film"] == f"http://testserver/film/{film.pk}/"


@pytest.mark.django_db
def test_note_document_reply_and_edited():
    user = User.objects.create_user(localname="alice", password="p")
    parent = _make_status(user, content="First.")
    child = _make_status(user, content="Reply.", film=None)
    Status.objects.filter(pk=child.pk).update(
        reply_parent=parent, edited_date=timezone.now()
    )
    child.refresh_from_db()
    doc = note_document(child, RequestFactory().get("/"))
    assert doc["inReplyTo"] == f"http://testserver/status/{parent.pk}/"
    assert "editedTime" in doc


@pytest.mark.django_db
def test_note_document_rating_only_omits_content():
    user = User.objects.create_user(localname="alice", password="p")
    film = Film.objects.create(title="Arrival")
    status = Status.objects.create(
        user=user,
        film=film,
        status_type="review_rating",
        rating=Decimal("3"),
    )
    doc = note_document(status, RequestFactory().get("/"))
    assert "content" not in doc
    assert doc["rating"] == 3.0


@pytest.mark.django_db
def test_create_activity_shape():
    user = User.objects.create_user(localname="alice", password="p")
    status = _make_status(user, content="Hi.")
    activity = create_activity(status, user, RequestFactory().get("/"))
    assert activity["type"] == "Create"
    assert activity["actor"] == "http://testserver/user/alice/"
    assert activity["id"].startswith("http://testserver/user/alice/outbox/#activity-")
    assert activity["object"]["id"] == f"http://testserver/status/{status.pk}/"
    assert activity["object"]["content"] == "Hi."


# --- Outbox endpoint --------------------------------------------------------


@pytest.mark.django_db
def test_outbox_unknown_user_404(client):
    assert client.get("/user/nobody/outbox/").status_code == 404


@pytest.mark.django_db
def test_outbox_empty_collection(client):
    User.objects.create_user(localname="alice", password="p")
    response = client.get("/user/alice/outbox/")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/activity+json"
    doc = response.json()
    assert doc["type"] == "OrderedCollection"
    assert doc["totalItems"] == 0


@pytest.mark.django_db
def test_outbox_lists_statuses_newest_first(client):
    user = User.objects.create_user(localname="alice", password="p")
    now = timezone.now()
    _make_status(user, content="Old.", when=now - timedelta(hours=2))
    newest = _make_status(user, content="New.", when=now)
    response = client.get("/user/alice/outbox/?page=1")
    doc = response.json()
    assert doc["type"] == "OrderedCollectionPage"
    assert doc["partOf"] == "http://testserver/user/alice/outbox/"
    assert doc["startIndex"] == 1
    contents = [item["object"]["content"] for item in doc["items"]]
    assert contents == ["New.", "Old."]
    # The newest activity carries the newest status's id.
    assert doc["items"][0]["object"]["id"].endswith(f"/status/{newest.pk}/")


@pytest.mark.django_db
def test_outbox_excludes_deleted_and_remote(client):
    user = User.objects.create_user(localname="alice", password="p")
    kept = _make_status(user, content="Kept.")
    deleted = _make_status(user, content="Gone.")
    deleted.delete()  # soft-delete -> tombstone, excluded from the outbox
    remote = Status(local=False)
    remote.user = user
    remote.content = "Remote"
    remote.save()
    response = client.get("/user/alice/outbox/?page=1")
    ids = {item["object"]["id"] for item in response.json()["items"]}
    assert ids == {f"http://testserver/status/{kept.pk}/"}


@pytest.mark.django_db
def test_outbox_pagination(client):
    user = User.objects.create_user(localname="alice", password="p")
    now = timezone.now()
    for i in range(PAGE_SIZE + 5):
        _make_status(user, content=f"S{i:02d}", when=now - timedelta(minutes=i))
    total = PAGE_SIZE + 5
    collection = client.get("/user/alice/outbox/").json()
    assert collection["totalItems"] == total
    assert collection["last"].endswith(f"?page={last_page(total)}")

    first = client.get("/user/alice/outbox/?page=1").json()
    assert len(first["items"]) == PAGE_SIZE
    assert first["startIndex"] == 1

    second = client.get("/user/alice/outbox/?page=2").json()
    assert len(second["items"]) == 5
    assert second["startIndex"] == PAGE_SIZE + 1


# --- Followers / following --------------------------------------------------


@pytest.mark.django_db
def test_followers_unknown_user_404(client):
    assert client.get("/user/nobody/followers/").status_code == 404
    assert client.get("/user/nobody/following/").status_code == 404


@pytest.mark.django_db
def test_followers_empty_collection(client):
    User.objects.create_user(localname="alice", password="p")
    doc = client.get("/user/alice/followers/").json()
    assert doc["type"] == "OrderedCollection"
    assert doc["totalItems"] == 0


@pytest.mark.django_db
def test_followers_and_following_directions(client):
    alice = User.objects.create_user(localname="alice", password="p")
    bob = User.objects.create_user(localname="bob", password="p")
    carol = User.objects.create_user(localname="carol", password="p")
    # bob and carol follow alice -> they are in alice's followers, and in
    # bob/carol's following.
    bob.follows.add(alice)
    carol.follows.add(alice)

    # totalItems lives on the collection doc; items on a page (?page=N).
    followers_collection = client.get("/user/alice/followers/").json()
    assert followers_collection["totalItems"] == 2
    followers = client.get("/user/alice/followers/?page=1").json()
    names = sorted(item["preferredUsername"] for item in followers["items"])
    assert names == ["bob", "carol"]

    bob_following = client.get("/user/bob/following/?page=1").json()
    following_names = [item["preferredUsername"] for item in bob_following["items"]]
    assert following_names == ["alice"]

    # Items are full Person documents (carry the actor id).
    assert followers["items"][0]["type"] == "Person"


# --- Inbox routes -----------------------------------------------------------


# Signed-delivery coverage (first-contact mirrors, verification, dedup,
# graceful ignore) lives in test_activitypub_inbox.py; these keep the route
# contract: GET is not an inbox operation, and an unsigned POST is rejected.


@pytest.mark.django_db
def test_per_user_inbox_get_405_unsigned_post_401(client):
    User.objects.create_user(localname="alice", password="p")
    response = client.get("/user/alice/inbox/")
    assert response.status_code == 405
    assert response["Allow"] == "POST"
    response = client.post(
        "/user/alice/inbox/", data="{}", content_type="application/json"
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_per_user_inbox_unknown_user_404(client):
    assert client.get("/user/nobody/inbox/").status_code == 404
    assert (
        client.post(
            "/user/nobody/inbox/", data="{}", content_type="application/json"
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_shared_inbox_get_405_unsigned_post_401(client):
    response = client.get("/inbox/")
    assert response.status_code == 405
    assert response["Allow"] == "POST"
    response = client.post("/inbox/", data="{}", content_type="application/json")
    assert response.status_code == 401


# --- Content-negotiated Film serving ----------------------------------------


@pytest.mark.django_db
def test_film_endpoint_serves_film_to_ap_client(client):
    film = Film.objects.create(title="Arrival", year=2016)
    response = client.get(f"/film/{film.pk}/", HTTP_ACCEPT=AP_ACCEPT)
    assert response.status_code == 200
    assert response["Content-Type"] == "application/activity+json"
    doc = response.json()
    assert doc["type"] == "Film"
    assert doc["id"] == f"http://testserver/film/{film.pk}/"
    assert doc["year"] == 2016


@pytest.mark.django_db
def test_film_endpoint_accepts_ld_json(client):
    film = Film.objects.create(title="Arrival")
    response = client.get(f"/film/{film.pk}/", HTTP_ACCEPT="application/ld+json")
    assert response.status_code == 200
    assert response.json()["type"] == "Film"


@pytest.mark.django_db
def test_film_endpoint_serves_html_to_browsers(client):
    film = Film.objects.create(title="Arrival")
    response = client.get(f"/film/{film.pk}/")
    assert response.status_code == 200
    assert "text/html" in response["Content-Type"]
    # The human page, not the wire document.
    assert b'"type": "Film"' not in response.content


# --- origin_id identity -----------------------------------------------------


@pytest.mark.django_db
def test_new_film_gets_origin_id_on_create():
    film = Film.objects.create(title="Arrival")
    assert film.origin_id == film.pk
    film.refresh_from_db()
    assert film.origin_id == film.pk


@pytest.mark.django_db
def test_new_local_status_gets_origin_id_on_create():
    user = User.objects.create_user(localname="alice", password="p")
    status = _make_status(user, content="Hi.")
    assert status.origin_id == status.pk
    status.refresh_from_db()
    assert status.origin_id == status.pk


@pytest.mark.django_db
def test_remote_status_does_not_get_origin_id():
    user = User.objects.create_user(localname="alice", password="p")
    status = Status(local=False, content="remote")
    status.user = user
    status.save()
    assert status.origin_id is None


@pytest.mark.django_db
def test_backfill_migration_fills_null_origin_ids():
    import importlib

    film = Film.objects.create(title="Arrival")
    user = User.objects.create_user(localname="alice", password="p")
    status = _make_status(user, content="Hi.")
    # Simulate pre-migration rows: clear the origin identity.
    Film.objects.filter(pk=film.pk).update(origin_id=None)
    Status.objects.filter(pk=status.pk).update(origin_id=None)

    module = importlib.import_module("reeltalk.core.migrations.0006_backfill_origin_id")

    class _Apps:
        def get_model(self, app_label, model_name):
            return {"Film": Film, "Status": Status}[model_name]

    module.backfill_origin_id(_Apps(), None)
    film.refresh_from_db()
    status.refresh_from_db()
    assert film.origin_id == film.pk
    assert status.origin_id == status.pk
