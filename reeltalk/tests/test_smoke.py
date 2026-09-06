"""M0 smoke test: the skeleton serves a page and Django's checks pass."""

import pytest
from django.test import Client


@pytest.mark.django_db
def test_index_redirects_to_setup_on_fresh_instance():
    # A fresh instance with no admin bounces to the first-run wizard (R12).
    response = Client().get("/")
    assert response.status_code == 302
    assert response.url == "/setup/"
