"""Film merge/absorb admin tool tests (M1 increment 7, §3.7 v0.1 admin).

The tool at /admin/films/merge/ is staff-only and runs Film.merge_into over
a chosen canonical + absorbed rows; the model owns the semantics, so these
tests cover access control, form validation, and the end state of a merge.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from reeltalk.core.models import Film, MergedFilm, Shelf, ShelfFilm, Status

User = get_user_model()


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def staff(db):
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


@pytest.fixture
def staff_client(client, staff):
    assert client.login(username="admin", password="s3cretpass")
    return client


@pytest.fixture
def canonical(db):
    # The survivor: empty metadata to be backfilled.
    return Film.objects.create(title="Blade Runner", year=1982)


@pytest.fixture
def duplicate(db):
    return Film.objects.create(
        title="Blade Runner",
        year=1982,
        runtime=117,
        description="<p>A neo-noir classic.</p>",
        tmdb_id=780,
    )


def post_merge(client, canonical, duplicate):
    return client.post(
        "/admin/films/merge/",
        {"canonical": str(canonical.id), "absorbed": [str(duplicate.id)]},
    )


# --- access control -------------------------------------------------------------


@pytest.mark.django_db
def test_merge_tool_requires_login(client, staff):
    resp = client.get("/admin/films/merge/")
    assert resp.status_code == 302
    assert "/admin/login/" in resp.url


@pytest.mark.django_db
def test_merge_tool_sends_non_staff_to_admin_login(client, db):
    # staff_member_required redirects any non-staff user (logged in or not)
    # to the admin login rather than serving the page.
    User.objects.create_user(localname="alice", password="s3cretpass")
    assert client.login(username="alice", password="s3cretpass")
    resp = client.get("/admin/films/merge/")
    assert resp.status_code == 302
    assert "/admin/login/" in resp.url


@pytest.mark.django_db
def test_merge_tool_renders_for_staff(staff_client, canonical, duplicate):
    body = staff_client.get("/admin/films/merge/").content.decode()
    assert "Film merge / absorb" in body
    assert "Blade Runner" in body


# --- merging ---------------------------------------------------------------------


@pytest.mark.django_db
def test_merge_backfills_and_repoints(staff_client, canonical, duplicate):
    alice = User.objects.create_user(localname="alice", password="s3cretpass")
    bob = User.objects.create_user(localname="bob", password="s3cretpass")
    watchlist = Shelf.objects.get(user=alice, identifier=Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=watchlist, film=duplicate)
    Status.objects.create(
        user=bob,
        film=duplicate,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Neo-noir.</p>",
    )

    resp = post_merge(staff_client, canonical, duplicate)
    assert resp.status_code == 302

    # Absorbed row gone, mapping kept for old URLs.
    assert not Film.objects.filter(id=duplicate.id).exists()
    assert MergedFilm.objects.filter(old_id=duplicate.id, new_id=canonical.id).exists()

    # Empty metadata backfilled onto the survivor; identity untouched.
    canonical.refresh_from_db()
    assert canonical.runtime == 117
    assert canonical.tmdb_id == 780
    assert canonical.description == "<p>A neo-noir classic.</p>"
    assert canonical.title == "Blade Runner"

    # Shelves and reviews re-pointed at the survivor.
    row = ShelfFilm.objects.get(shelf=watchlist)
    assert row.film_id == canonical.id
    status = Status.objects.get(user=bob)
    assert status.film_id == canonical.id


@pytest.mark.django_db
def test_merge_rejects_canonical_in_absorbed(staff_client, canonical, duplicate):
    resp = staff_client.post(
        "/admin/films/merge/",
        {"canonical": str(canonical.id), "absorbed": [str(canonical.id)]},
    )
    assert resp.status_code == 200
    assert "cannot also be merged in" in resp.content.decode()
    # Nothing happened.
    assert Film.objects.filter(id=duplicate.id).exists()
    assert not MergedFilm.objects.exists()


@pytest.mark.django_db
def test_merge_redirects_back_to_the_tool(staff_client, canonical, duplicate):
    resp = post_merge(staff_client, canonical, duplicate)
    assert resp.url == "/admin/films/merge/"
