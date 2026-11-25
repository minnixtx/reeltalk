"""The edited marker (author edit/delete arc, increment I3, second half).

The marker is the bare word "edited" beside a post's date (owner ruling 2): no
date, no time, nothing in a tooltip, and everyone sees it — it is a fact about
the post, not a note to its author (ruling 5).

The surface ruling was revised in the owner's live review of I3, same day. The
word paints on the post page only: the post itself and its reply rows, the
thread counted wherever it renders — under the review's own page and under a
list's face alike. The film page's review list and a member's profile review
list are ruled off, after the first cut, because a review list reads as a feed
and the feed was deliberately left clean: "Better to only make it appear when
you're looking at the actual review post page."

Two traps the tests are built around:

* **A review list that grows the word.** Both lists render rows that look like
  feed rows and read the same ``Status`` rows whose ``edited_date`` is set, so
  a marker keyed on the field alone lands on them. The film page and the
  profile are pinned at zero markers on an edited row, one page apart from the
  post page that must show it.
* **A list face that grows the word.** ``edited_date`` is non-null on a list's
  face whenever the list was edited — the wire's ``editedTime`` has stamped it
  since lists increment 6 — while nothing on the face should ever say so.

Absence is asserted on the count of rendered markers, never on a substring of
the body, so a page that renders nothing distinctive cannot pass vacuously.
The stamp side is pinned too (ruling 4): a stars-only change is not an edit, so
re-finishing a film with the same words and a new score must not mark anything.
"""

import re
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from reeltalk.core.models import Film, Status, add_reply
from reeltalk.lists.services import create_list, rename
from reeltalk.tests.members import member, site_admin

MARKER = 'class="edited-marker"'


@pytest.fixture
def admin(db):
    # R12: / redirects to /setup/ until a superuser exists, so the feed-shape
    # tests that read the home page need one on the page.
    return site_admin(localname="admin", password="s3cretpass")


@pytest.fixture
def alice(db):
    return member(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return member(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def alien(db):
    return Film.objects.create(title="Alien", year=1979)


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


def _body(client: Client, url: str) -> str:
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.content.decode()


def _markers(body: str) -> int:
    """How many times this render draws the word."""
    return len(re.findall(MARKER, body))


def _review(user, film, **extra):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="4.5",
        content="<p>Spice must be reviewed.</p>",
        raw_content="Spice must be reviewed.",
        **extra,
    )


def _finish(client, film, raw):
    """The real finish chain: POST the modal's form, the member's own write."""
    resp = client.post(f"/film/{film.pk}/watched/", {"rating": "4", "content": raw})
    assert resp.status_code == 302
    return Status.objects.get(user__localname="alice", film=film)


def _edit_reply(client, reply, raw):
    resp = client.post(f"/status/{reply.pk}/edit/", {"content": raw})
    assert resp.status_code == 302


# --- the surface that carries it -----------------------------------------------


@pytest.mark.django_db
def test_an_untouched_review_carries_no_word(alice, dune):
    client = _login("alice")
    review = _finish(client, dune, "First take.")
    assert review.edited_date is None
    assert _markers(_body(client, f"/status/{review.pk}/")) == 0


@pytest.mark.django_db
def test_an_edited_review_shows_the_word_on_its_post_page(alice, dune):
    client = _login("alice")
    review = _finish(client, dune, "First take.")
    _finish(client, dune, "Second take, after a rewatch.")
    review.refresh_from_db()
    assert review.edited_date is not None
    assert _markers(_body(client, f"/status/{review.pk}/")) == 1


@pytest.mark.django_db
def test_every_reader_sees_the_word_not_only_the_author(alice, bob, dune):
    # Ruling 5: the word is a fact about the post. Bob never wrote it and sees
    # it just the same.
    client = _login("alice")
    review = _finish(client, dune, "First take.")
    _finish(client, dune, "Second take.")
    assert _markers(_body(_login("bob"), f"/status/{review.pk}/")) == 1


@pytest.mark.django_db
def test_a_stars_only_change_is_not_an_edit(alice, dune):
    # Ruling 4, at the marker end: the stamp rule already lives in mark_watched
    # and is pinned there; what rots here is a marker that renders off the
    # rating change anyway. Same words, new score — no word anywhere.
    client = _login("alice")
    review = _finish(client, dune, "Same words.")
    client.post(
        f"/film/{dune.pk}/watched/", {"rating": "2.5", "content": "Same words."}
    )
    review.refresh_from_db()
    assert review.rating == Decimal("2.5")
    assert review.edited_date is None
    assert _markers(_body(client, f"/status/{review.pk}/")) == 0


@pytest.mark.django_db
def test_an_edited_reply_shows_the_word_on_its_row(alice, bob, dune):
    post = _review(alice, dune)
    reply = add_reply(bob, post, content="<p>Agreed.</p>", raw_content="Agreed.")
    assert _markers(_body(_login("alice"), f"/status/{post.pk}/")) == 0
    _edit_reply(_login("bob"), reply, "Agreed, **strongly**.")
    # One marker: the reply's row. The post above it is untouched and must not
    # light up because someone else edited under it.
    assert _markers(_body(_login("alice"), f"/status/{post.pk}/")) == 1


@pytest.mark.django_db
def test_a_reply_under_a_list_shows_the_word_on_the_list_page(alice, bob, dune, alien):
    # The thread is part of the post page's rule, not of the review lists: the
    # face stays clean (next test) while its thread carries the word like any
    # other thread.
    listing = create_list(alice, title="Cold War Sci-Fi", films=[dune])
    face = listing.status
    reply = add_reply(
        bob, face, content="<p>Missing Alien.</p>", raw_content="Missing Alien."
    )
    _edit_reply(_login("bob"), reply, "Missing Alien, and The Thing.")
    body = _body(_login("alice"), f"/list/{listing.pk}/")
    assert _markers(body) == 1


# --- the surfaces that never do ------------------------------------------------


@pytest.mark.django_db
def test_the_review_lists_never_show_the_word(alice, dune):
    # The revised ruling in one test: the film page's review list and the
    # profile's review list read the same edited row as the post page and must
    # still draw nothing — a review list reads as a feed, and the feed is kept
    # clean on purpose. The post page assertion rides along so the test cannot
    # pass green by the marker going missing everywhere.
    client = _login("alice")
    review = _finish(client, dune, "First take.")
    _finish(client, dune, "Second take.")
    assert _markers(_body(client, f"/film/{dune.pk}/")) == 0
    assert _markers(_body(client, "/user/alice/")) == 0
    assert _markers(_body(client, f"/status/{review.pk}/")) == 1


@pytest.mark.django_db
def test_a_renamed_list_face_never_shows_the_word(alice, dune, alien):
    # The other trap: the rename stamps the face's edited_date — the wire
    # needs it — and the face's own page must still draw nothing.
    listing = create_list(alice, title="Cold War Sci-Fi", films=[dune])
    rename(listing, "Cold War Cinema")
    listing.refresh_from_db()
    assert listing.status.edited_date is not None
    assert _markers(_body(_login("alice"), f"/list/{listing.pk}/")) == 0


@pytest.mark.django_db
def test_the_feed_never_shows_the_word_in_any_shape(alice, bob, dune, alien, admin):
    # Two edited rows of two different shapes, both of which reach alice's
    # feed: a review with no Watched row under it, and a renamed list's face.
    # The word must appear on neither, in either of the feed's two shapes.
    _login("alice")
    standalone = _review(alice, alien)
    standalone.edited_date = timezone.now()
    standalone.save(update_fields=["edited_date"])
    listing = create_list(alice, title="Cold War Sci-Fi", films=[dune])
    rename(listing, "Cold War Cinema")
    for client in (_login("alice"), _login("bob")):
        for url in ("/", "/feed/page/", "/feed/page/?scope=local"):
            assert _markers(_body(client, url)) == 0, url
