"""Welcome / getting-started page tests (M6 increment 2).

The static onboarding page (Letterboxd-style, owner decision): the core loop
in four steps — find a film, watchlist it, mark watched, follow people. It is
public (anonymous + signed-in), reached right after signup and from the footer
on every page. No JS; a plain template over base.html (R6).
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

User = get_user_model()


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def admin(db):
    # R12: the index and other pages are gated behind the setup wizard until a
    # superuser exists; the footer-link test walks those pages.
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


@pytest.mark.django_db
def test_welcome_renders_anonymous_with_signup_cta(client, admin):
    resp = client.get(reverse("welcome"))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "<h1>Getting started</h1>" in body
    # Anonymous visitors get a signup CTA.
    assert 'href="/signup/"' in body


@pytest.mark.django_db
def test_welcome_walks_the_core_loop_in_order(client, admin):
    body = client.get(reverse("welcome")).content.decode()
    steps = (
        "Find a film",
        "Add it to your Watchlist",
        "Mark it watched",
        "Follow film fans",
    )
    positions = [body.index(step) for step in steps]
    assert positions == sorted(positions), "steps out of order"
    # Search is the primary add-film flow — linked from step 1.
    assert 'href="/search/"' in body


@pytest.mark.django_db
def test_welcome_signed_in_shows_handle_and_find_people(client, admin):
    User.objects.create_user(localname="alice", password="s3cretpass")
    assert client.login(username="alice", password="s3cretpass")
    body = client.get(reverse("welcome")).content.decode()
    assert "alice" in body
    # Signed-in step 4 links to Find people; no signup CTA remains.
    assert 'href="/find/"' in body
    assert 'href="/signup/"' not in body


@pytest.mark.django_db
def test_footer_links_to_welcome_on_every_page(client, admin):
    for path in ("/", "/about/", "/welcome/", "/login/", "/signup/"):
        body = client.get(path).content.decode()
        assert 'href="/welcome/"' in body, f"missing footer link on {path}"
