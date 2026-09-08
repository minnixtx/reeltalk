"""Landing + about page tests (M1 increment 7, §3.7 v0.1).

The anonymous home is the landing: instance name/description from site
settings plus a signup CTA. /about/ carries the instance info (name,
domain, software, version); every page links to it from the footer.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from reeltalk.social.models import SiteSettings

User = get_user_model()


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def admin(db):
    # R12: / is gated behind the setup wizard until a superuser exists.
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


@pytest.mark.django_db
def test_landing_shows_instance_name_and_description(client, admin):
    site = SiteSettings.get_instance()
    site.name = "My Film Club"
    site.description = "A corner of the fed for frame-by-frame people."
    site.save()

    body = client.get("/").content.decode()
    assert "<h1>My Film Club</h1>" in body
    assert "A corner of the fed for frame-by-frame people." in body
    assert 'href="/signup/"' in body


@pytest.mark.django_db
def test_landing_defaults_when_description_empty(client, admin):
    body = client.get("/").content.decode()
    # Default instance name; no description paragraph at all.
    assert "<h1>ReelTalk</h1>" in body
    assert 'class="landing-desc"' not in body


@pytest.mark.django_db
def test_landing_escapes_admin_description(client, admin):
    site = SiteSettings.get_instance()
    site.description = "<script>alert(1)</script>"
    site.save()
    body = client.get("/").content.decode()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


@pytest.mark.django_db
def test_about_page_shows_instance_info(client, admin):
    site = SiteSettings.get_instance()
    site.name = "My Film Club"
    site.save()

    resp = client.get("/about/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "My Film Club" in body
    # Domain from settings (pytest env pins it to localhost).
    assert "<dd>localhost</dd>" in body
    assert "ReelTalk 0.1" in body
    assert "AGPL-3.0" in body


@pytest.mark.django_db
def test_footer_links_to_about_on_every_page(client, admin):
    for path in ("/", "/about/", "/login/", "/signup/"):
        body = client.get(path).content.decode()
        assert 'href="/about/"' in body, f"missing footer link on {path}"


@pytest.mark.django_db
def test_logged_in_home_still_shows_feed_with_site_name(client, admin):
    User.objects.create_user(localname="alice", password="s3cretpass")
    assert client.login(username="alice", password="s3cretpass")
    body = client.get("/").content.decode()
    assert "<h1>ReelTalk</h1>" in body
    assert 'class="feed"' in body
