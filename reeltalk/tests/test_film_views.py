"""Film page view tests (PLAN.md §3.7) — detail page, reviews, merge resolution."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from reeltalk.core.models import Film, Status

User = get_user_model()


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def user(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def film(db):
    return Film.objects.create(
        title="Dune",
        year=2021,
        runtime=155,
        genres=["Sci-Fi"],
        directors=["Denis Villeneuve"],
        description="<p>A desert epic.</p>",
    )


def review(user, film, rating="4.5", content="<p>Great.</p>"):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=rating,
        content=content,
    )


@pytest.mark.django_db
def test_film_detail_renders_metadata(client, film):
    resp = client.get(f"/film/{film.id}/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Dune" in body
    assert "(2021)" in body
    assert "155 min" in body
    assert "Denis Villeneuve" in body
    assert "A desert epic." in body


@pytest.mark.django_db
def test_film_detail_shows_all_users_reviews(client, user, film):
    other = User.objects.create_user(localname="bob", password="s3cretpass")
    review(user, film, rating="4.5", content="<p>Great.</p>")
    review(other, film, rating="3", content="<p>Meh.</p>")
    resp = client.get(f"/film/{film.id}/")
    body = resp.content.decode()
    assert "alice" in body
    assert "bob" in body
    assert "Great." in body
    assert "Meh." in body
    assert "Reviews (2)" in body


@pytest.mark.django_db
def test_film_detail_no_reviews_placeholder(client, film):
    resp = client.get(f"/film/{film.id}/")
    assert "No reviews yet." in resp.content.decode()


@pytest.mark.django_db
def test_film_detail_deleted_review_not_listed(client, user, film):
    entry = review(user, film)
    entry.delete()  # soft
    resp = client.get(f"/film/{film.id}/")
    body = resp.content.decode()
    assert "Great." not in body
    assert "No reviews yet." in body


@pytest.mark.django_db
def test_film_detail_resolves_merged_film(client, film):
    old_id = film.id  # captured before the merge clears the pk
    canonical = Film.objects.create(title="Dune Part Two", year=2024)
    film.merge_into(canonical)
    # The absorbed id now serves the canonical film.
    resp = client.get(f"/film/{old_id}/")
    assert resp.status_code == 200
    assert "Dune Part Two" in resp.content.decode()


@pytest.mark.django_db
def test_film_detail_404_for_missing(client):
    assert client.get("/film/9999/").status_code == 404
