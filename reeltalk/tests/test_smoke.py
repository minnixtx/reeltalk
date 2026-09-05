"""M0 smoke test: the skeleton serves a page and Django's checks pass."""

from django.test import Client


def test_index_renders():
    response = Client().get("/")
    assert response.status_code == 200
    assert b"ReelTalk" in response.content
