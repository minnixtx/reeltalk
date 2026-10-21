"""The CSP header rides every response, including middleware short-circuits.

F5: the policy sat configured for months with django-csp never wired up, so
no response ever carried the header. These tests fail if the middleware is
removed from the chain or moved below SecurityMiddleware, whose SSL redirect
short-circuits before the view ever runs.
"""

import pytest
from django.test import override_settings

from reeltalk.tests.members import site_admin

CSP = "Content-Security-Policy"


@pytest.fixture
def admin(db):
    # R12: / is gated behind the setup wizard until a superuser exists.
    return site_admin(localname="admin", password="s3cretpass")


@pytest.mark.django_db
def test_home_page_carries_the_csp_header(client, admin):
    response = client.get("/")
    assert response.status_code == 200
    assert "default-src 'self'" in response[CSP]


@override_settings(SECURE_SSL_REDIRECT=True)
def test_ssl_redirect_carries_the_csp_header(client):
    # CSPMiddleware sits above SecurityMiddleware precisely so this
    # short-circuit response is still decorated.
    response = client.get("/")
    assert response.status_code == 301
    assert "default-src 'self'" in response[CSP]
