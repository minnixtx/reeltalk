"""Site settings, link-domain allowlist, and signup policy (M1 increment 7).

§3.2's site settings object: the single admin-managed row (instance
name/description + signup policy), the outbound-link domain allowlist that
gates hrefs in user-authored markdown at write time, and the /signup/ gate
(open vs closed) once the instance is past first run (R12).
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from reeltalk.core.utils import render_markdown
from reeltalk.social.models import LinkDomain, SiteSettings

User = get_user_model()


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def admin(db):
    # R12: signup is gated behind the setup wizard until a superuser exists.
    return User.objects.create_superuser(localname="admin", password="s3cretpass")


# --- SiteSettings singleton ---------------------------------------------------


@pytest.mark.django_db
def test_site_settings_defaults(db):
    site = SiteSettings.get_instance()
    assert site.name == "ReelTalk"
    assert site.description == ""
    assert site.signup_policy == SiteSettings.OPEN


@pytest.mark.django_db
def test_site_settings_is_a_single_row(db):
    first = SiteSettings.get_instance()
    first.name = "My Film Club"
    first.save()
    # Second call returns the same (updated) row, not a fresh one.
    again = SiteSettings.get_instance()
    assert again.pk == first.pk
    assert again.name == "My Film Club"
    assert SiteSettings.objects.count() == 1


# --- LinkDomain allowlist ------------------------------------------------------


@pytest.mark.django_db
def test_link_domain_requires_an_exact_or_subdomain_match(db):
    LinkDomain.objects.create(domain="example.com")
    assert LinkDomain.is_allowed("example.com")
    assert LinkDomain.is_allowed("movies.example.com")
    # A bare suffix that is not a subdomain must not match.
    assert not LinkDomain.is_allowed("notexample.com")
    assert not LinkDomain.is_allowed("evil.com")


@pytest.mark.django_db
def test_link_domain_matching_is_case_insensitive(db):
    LinkDomain.objects.create(domain="Example.COM")
    assert LinkDomain.is_allowed("EXAMPLE.com")


@pytest.mark.django_db
def test_link_domain_strips_port_and_userinfo(db):
    LinkDomain.objects.create(domain="example.com")
    assert LinkDomain.is_allowed("example.com:8080")
    # Userinfo cannot smuggle a host past the allowlist.
    assert not LinkDomain.is_allowed("user@example.com.evil.net")


@pytest.mark.django_db
def test_link_domain_empty_allowlist_denies_everything(db):
    assert not LinkDomain.is_allowed("example.com")
    assert not LinkDomain.is_allowed("")


# --- render_markdown link filtering (write time) --------------------------------


@pytest.mark.django_db
def test_render_markdown_strips_links_with_no_domains_allowed(db):
    html = render_markdown("[the movie](https://themoviedb.org/movie/1)")
    assert "themoviedb.org" not in html
    assert "the movie" in html  # anchor text survives


@pytest.mark.django_db
def test_render_markdown_keeps_links_to_allowed_domains(db):
    LinkDomain.objects.create(domain="themoviedb.org")
    html = render_markdown("[the movie](https://themoviedb.org/movie/1)")
    assert 'href="https://themoviedb.org/movie/1"' in html


@pytest.mark.django_db
def test_render_markdown_strips_disallowed_and_relative_links(db):
    LinkDomain.objects.create(domain="example.com")
    html = render_markdown(
        "[ok](https://example.com/a) [no](https://evil.com/b) "
        "[rel](/images/posters/x.jpg)"
    )
    assert 'href="https://example.com/a"' in html
    assert "evil.com" not in html
    assert "/images/posters/x.jpg" not in html


# --- signup policy gate ----------------------------------------------------------


def signup_post(client, localname="newbie"):
    return client.post(
        "/signup/",
        {
            "localname": localname,
            "display_name": "",
            "email": "",
            "password1": "s3cretpass",
            "password2": "s3cretpass",
        },
    )


@pytest.mark.django_db
def test_signup_open_policy_creates_an_account(client, admin):
    assert SiteSettings.get_instance().signup_policy == SiteSettings.OPEN
    resp = signup_post(client)
    assert resp.status_code == 302
    assert User.objects.filter(localname="newbie").exists()


@pytest.mark.django_db
def test_signup_invite_policy_closes_the_form(client, admin):
    site = SiteSettings.get_instance()
    site.signup_policy = SiteSettings.INVITE
    site.save()

    body = client.get("/signup/").content.decode()
    assert "not accepting new signups" in body

    # A POST cannot sneak an account past the closed gate.
    resp = signup_post(client)
    assert resp.status_code == 200
    assert not User.objects.filter(localname="newbie").exists()


@pytest.mark.django_db
def test_signup_stays_open_when_policy_is_open_after_editing_other_fields(
    client, admin
):
    site = SiteSettings.get_instance()
    site.name = "Renamed"
    site.save()
    assert signup_post(client).status_code == 302
