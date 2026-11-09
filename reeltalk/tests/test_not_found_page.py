"""The page a reader gets when a URL answers nothing (W4, F-W4-1).

There was no ``404.html`` and no ``handler404`` anywhere in the tree until the W4
sweep, so Django's stock page answered every miss. That page has no viewport
meta, so on a phone it lays out at Chrome's 980px fallback and clips its own
sentence; it has no wordmark, no nav and no way back.

That was survivable while a 404 meant a typo. It stopped being survivable with
the audience arc: a list whose audience excludes the reader answers 404 on
purpose, on the list page, on the AP arm of its face, and at the Save door. The
plan named "a Save door that answers 200 for a reader who cannot see the list"
as the failure mode, and the answer to it was to refuse - so the refusal page is
now on a common path and belongs to the app.

Two things this file is careful about. The status code is asserted on every case,
because a template that makes a 404 look friendly must not be allowed to make it
a 200 or a 302 - the status is the security property and the page is only the
courtesy. And the tests run with ``DEBUG`` off: under ``DEBUG`` Django answers a
miss with its technical page and the one under test never renders, which would
make every assertion here pass on a page that no deployer ever sees.
"""

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from reeltalk.core.models import Visibility
from reeltalk.lists.services import create_list
from reeltalk.tests.members import member

PASSWORD = "s3cretpass"

# Django's stock page, verbatim enough to catch its return. Matching on the body
# rather than on the absence of the site chrome means a partial that grew a
# "Not Found" heading somewhere cannot make this pass by accident.
DJANGO_STOCK = "The requested resource was not found on this server."


@pytest.fixture
def author(db):
    return member(localname="author", password=PASSWORD)


@pytest.fixture
def stranger(db):
    return member(localname="stranger", password=PASSWORD)


@pytest.fixture
def private_list(author):
    return create_list(author, title="PRIVATE-404", visibility=Visibility.PRIVATE)


def _login(localname):
    client = Client()
    assert client.login(username=localname, password=PASSWORD), (
        f"{localname} did not sign in - the page below would render anonymously"
    )
    return client


@override_settings(DEBUG=False)
@pytest.mark.django_db
def test_a_miss_renders_the_site_page_not_djangos_stock_one(client):
    """The whole point of the template: the site chrome, not a bare page."""
    response = client.get("/no-such-page/")
    assert response.status_code == 404
    body = response.content.decode()
    assert DJANGO_STOCK not in body
    assert '<meta name="viewport"' in body, (
        "without the viewport meta a phone lays the page out at 980px and clips it"
    )
    assert 'class="site-header"' in body
    assert response.headers["Content-Type"].startswith("text/html")


@override_settings(DEBUG=False)
@pytest.mark.django_db
def test_the_page_says_the_number_and_nothing_that_blames_a_reader(client):
    """The page carries the numeral and no prose, which is the ambiguity doing
    work rather than an absence of copy: "it is gone" and "you may not see it"
    both land here, and settling which would hand back the fact the 404 was
    chosen over a 403 to withhold."""
    body = client.get("/no-such-page/").content.decode()
    assert "404" in body
    for giveaway in (
        "not allowed",
        "Forbidden",
        "you may not",
        "log in to see",
        "private",
    ):
        assert giveaway not in body


@override_settings(DEBUG=False)
@pytest.mark.django_db
def test_the_numeral_is_drawn_once_and_named_once(client):
    """The wordmark treatment is two stacked copies of the text, so the second
    has to stay out of the accessibility tree or the heading announces
    "404 404"."""
    body = client.get("/no-such-page/").content.decode()
    assert 'class="neon-404"' in body
    assert '<span class="neon-404-tube" aria-hidden="true">404</span>' in body


@override_settings(DEBUG=False)
@pytest.mark.django_db
def test_a_list_the_reader_may_not_read_lands_here_and_stays_a_404(
    stranger, private_list
):
    """The path the audience arc actually creates: a real reader, a real list,
    refused. The status is the property - the page is the courtesy."""
    response = _login("stranger").get(f"/list/{private_list.pk}/")
    assert response.status_code == 404
    assert "PRIVATE-404" not in response.content.decode()
    assert 'class="site-header"' in response.content.decode()


@override_settings(DEBUG=False)
@pytest.mark.django_db
def test_anonymous_gets_the_same_page_as_a_signed_in_stranger(client, private_list):
    """The floor of the read-path matrices: a login is not itself an audience,
    and the floor's page must not be the one Django ships."""
    response = client.get(f"/list/{private_list.pk}/")
    assert response.status_code == 404
    assert DJANGO_STOCK not in response.content.decode()


@override_settings(DEBUG=False)
@pytest.mark.django_db
def test_the_author_still_opens_their_own_private_list(author, private_list):
    """Guards the fix against being read as a wider change: the door still opens
    for the one reader it is supposed to open for."""
    response = _login("author").get(f"/list/{private_list.pk}/")
    assert response.status_code == 200
    assert "PRIVATE-404" in response.content.decode()


@override_settings(DEBUG=False)
@pytest.mark.django_db
def test_the_page_offers_a_way_back(client):
    """The stock page dropped the reader outside the site with no route out."""
    body = client.get("/no-such-page/").content.decode()
    assert reverse("index") in body
