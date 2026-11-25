"""Delete controls on the post page (author edit/delete arc, increment I1).

The endpoint shipped in M5 increment 4 and already federated; what this increment
adds is the page that offers it, and a notice that names what it deleted. Six
things can rot here, one test each:

* **A control whose POST lands nowhere** (R85). The page asks ``may_delete`` —
  the one predicate ``delete_review`` refuses with — and the seam is pinned from
  both sides: for every row shape, the control renders exactly when the route
  takes the POST.
* **A control on a row that is not the reader's.** A mirror, another member's
  post, a list's face: each renders nothing, and each still 404s when posted to
  anyway, because "the button is not rendered" is not a permission.
* **A reply row that forgets its flag.** The partial has three renderers — the
  post page's loop, the list page's loop, and the JSON row ``comments.js``
  appends — so the reply you just posted could arrive without a control and grow
  one only on the next reload.
* **A notice that names the wrong object** (D7): "your review" on a row that is
  a reply.
* **A redirect that does not land where the control pointed** — asserted as the
  exact ``Location``, not a 3xx shrug.
* **The tombstone itself**: soft-delete keeps the identity, clears the content.

Absence is asserted on the *set of status ids the page's delete forms target*
(``_delete_targets``), never on a substring of the body: a page-wide "not in
body" check passes vacuously the moment the markup changes shape, and a reply row
that carries no control leaves nothing distinctive to search for either.
"""

import json
import re

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import get_messages as _django_get_messages
from django.test import Client
from django.urls import reverse

from reeltalk.core.models import Film, Status, add_reply, may_delete
from reeltalk.lists.services import create_list
from reeltalk.tests.members import member

User = get_user_model()

# Every row shape the gate has to answer, and whether the reader (always alice
# here) may delete it. The fourth is the one the browser cannot show: a mirror is
# always owned by a remote account, so no real page ever pairs "this is your
# row" with "this is not local". It is pinned here anyway because the clause is
# load-bearing on the route, and a predicate is tested by its clauses.
SHAPES = {
    "your own local post": True,
    "another member's post": False,
    "a mirror of another instance's post": False,
    "your own row that is not local": False,
}


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


def _get_delete_targets(body: str) -> set[str]:
    """Every status id whose delete form this render offers.

    One regex over the whole page rather than a per-row search, so a control that
    renders twice and one that renders nowhere both fail loudly.
    """
    return set(re.findall(r'<form method="post" action="/status/(\d+)/delete/"', body))


def _body(client: Client, url: str) -> str:
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.content.decode()


def _review(user, film, *, content="<p>Spice must be reviewed.</p>", rating="4.5"):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=rating,
        content=content,
        raw_content="Spice must be reviewed.",
    )


def _rating_only(user, film):
    return Status.objects.create(
        user=user, film=film, status_type=Status.Type.REVIEW_RATING, rating="4.00"
    )


def _reply(author, parent, text="<p>Agreed.</p>", raw="Agreed."):
    return add_reply(author, parent, content=text, raw_content=raw)


def _mirror(film):
    """A mirrored review, as the inbound federation path writes one."""
    carol = User(
        localname="carol@remote.example",
        local=False,
        actor_url="https://remote.example/users/carol",
        inbox_url="https://remote.example/users/carol/inbox",
    )
    carol.set_unusable_password()
    carol.save()
    mirror = Status.objects.create(
        user=carol,
        film=film,
        status_type=Status.Type.REVIEW,
        rating="4",
        content="<p>Their review of Dune.</p>",
        local=False,
        remote_url="https://remote.example/status/carol-1",
    )
    return carol, mirror


def _not_local_review(user, film):
    """The author's own row with ``local=False`` — see ``SHAPES``."""
    return Status.objects.create(
        user=user,
        film=film,
        status_type=None,
        content="<p>A row that is mine and not ours.</p>",
        local=False,
        remote_url="https://remote.example/status/mine-not-mine",
    )


def _shape_row(shape, alice, bob, dune):
    if shape == "your own local post":
        return _review(alice, dune)
    if shape == "another member's post":
        return _review(bob, dune)
    if shape == "a mirror of another instance's post":
        return _mirror(dune)[1]
    return _not_local_review(alice, dune)


def _messages(resp):
    return [m.message for m in _django_get_messages(resp.wsgi_request)]


# --- the post page's control --------------------------------------------------


@pytest.mark.django_db
def test_the_author_gets_a_delete_on_their_own_post(alice, dune):
    post = _review(alice, dune)
    assert _get_delete_targets(_body(_login("alice"), f"/status/{post.pk}/")) == {
        str(post.pk)
    }


@pytest.mark.django_db
def test_a_rating_only_entry_gets_the_control_too(alice, dune):
    # D4: the endpoint treats every author-owned local non-LIST row alike, so
    # the page may not offer the control to reviews only.
    entry = _rating_only(alice, dune)
    assert _get_delete_targets(_body(_login("alice"), f"/status/{entry.pk}/")) == {
        str(entry.pk)
    }


@pytest.mark.django_db
def test_no_control_for_another_member_or_for_nobody_signed_in(alice, bob, dune):
    post = _review(alice, dune)
    assert _get_delete_targets(_body(_login("bob"), f"/status/{post.pk}/")) == set()
    assert _get_delete_targets(_body(Client(), f"/status/{post.pk}/")) == set()


@pytest.mark.django_db
def test_no_control_on_a_mirrored_post(alice, dune):
    _, mirror = _mirror(dune)
    assert _get_delete_targets(_body(_login("alice"), f"/status/{mirror.pk}/")) == set()


@pytest.mark.django_db
@pytest.mark.parametrize("shape", list(SHAPES))
def test_the_control_renders_exactly_where_the_route_lands(alice, bob, dune, shape):
    """The R85 seam, both sides, one row shape at a time.

    The control is offered if and only if the same client's POST is accepted.
    Reading the page first matters: the accepted POST soft-deletes the row, so
    the page assertion has to be spent before the write.
    """
    post = _shape_row(shape, alice, bob, dune)
    client = _login("alice")
    shown = str(post.pk) in _get_delete_targets(_body(client, f"/status/{post.pk}/"))
    refused = client.post(f"/status/{post.pk}/delete/").status_code == 404
    assert shown is not refused, shape


# --- reply rows ---------------------------------------------------------------


@pytest.mark.django_db
def test_a_reply_row_carries_a_delete_for_its_author_only(alice, bob, dune):
    post = _review(alice, dune)
    mine = _reply(alice, post)
    theirs = _reply(bob, post)
    assert _get_delete_targets(_body(_login("alice"), f"/status/{post.pk}/")) == {
        str(post.pk),
        str(mine.pk),
    }
    # Bob's own view: his reply, and nothing of alice's — not the post he is
    # reading a reply under.
    assert _get_delete_targets(_body(_login("bob"), f"/status/{post.pk}/")) == {
        str(theirs.pk)
    }


@pytest.mark.django_db
def test_a_reply_posted_without_a_reload_carries_the_control(alice, bob, dune):
    # comments.js inserts the JSON's markup verbatim, so that render is a third
    # place the flag has to be set or the new row lands half-dressed.
    post = _review(alice, dune)
    resp = _login("bob").post(f"/status/{post.pk}/reply/", {"content": "Agreed."})
    assert resp.status_code == 200
    html = json.loads(resp.content.decode())["html"]
    reply = Status.objects.get(reply_parent=post)
    assert _get_delete_targets(html) == {str(reply.pk)}


@pytest.mark.django_db
def test_the_list_page_thread_carries_the_same_flag(alice, bob, dune, alien):
    # The list page builds its thread through the same helper and the same
    # partial, so a reply under a list is deletable exactly where any other
    # reply is. The face itself is not: it goes down through the list's own
    # delete, which takes both halves together.
    listing = create_list(alice, title="Cold War Sci-Fi", films=[dune, alien])
    face = listing.status
    mine = _reply(bob, face)
    theirs = _reply(alice, face)
    assert _get_delete_targets(_body(_login("bob"), f"/list/{listing.pk}/")) == {
        str(mine.pk)
    }
    # Alice's own reply under her own list is hers to delete. The face itself
    # never is, not even for the member who made the list.
    assert _get_delete_targets(_body(_login("alice"), f"/list/{listing.pk}/")) == {
        str(theirs.pk)
    }


@pytest.mark.django_db
def test_a_list_face_never_renders_a_status_delete(alice, dune):
    # Its canonical page is /list/<id>/ (L10), so the post page redirects before
    # it can offer anything — and the route refuses the face outright, pinned in
    # test_lists_authoring against the real list delete.
    listing = create_list(alice, title="Cold War Sci-Fi", films=[dune])
    face = listing.status
    resp = _login("alice").get(f"/status/{face.pk}/")
    assert resp.status_code == 302
    assert resp["Location"] == f"/list/{listing.pk}/"


# --- the route's refusals -----------------------------------------------------


@pytest.mark.django_db
def test_a_stranger_cannot_delete_another_members_reply(alice, bob, dune):
    # The review case is pinned in test_film_views; this is the reply case, which
    # is the row the new control sits on.
    post = _review(alice, dune)
    theirs = _reply(bob, post)
    assert _login("alice").post(f"/status/{theirs.pk}/delete/").status_code == 404
    theirs.refresh_from_db()
    assert theirs.deleted is False


@pytest.mark.django_db
def test_a_mirrored_post_cannot_be_deleted_through_the_route(alice, dune):
    _, mirror = _mirror(dune)
    assert _login("alice").post(f"/status/{mirror.pk}/delete/").status_code == 404
    mirror.refresh_from_db()
    assert mirror.deleted is False


@pytest.mark.django_db
def test_the_rule_itself_answers_its_five_clauses(alice, bob, dune, alien):
    """``may_delete`` pinned clause by clause, from the model side."""
    own = _review(alice, dune)
    assert may_delete(alice, own) is True
    assert may_delete(bob, own) is False
    assert may_delete(AnonymousUser(), own) is False
    assert may_delete(alice, _review(bob, dune)) is False
    assert may_delete(alice, _not_local_review(alice, alien)) is False
    assert (
        may_delete(alice, create_list(alice, title="X", films=[dune]).status) is False
    )
    own.delete()
    assert may_delete(alice, own) is False


# --- what the route answers ---------------------------------------------------


@pytest.mark.django_db
def test_deleting_a_review_says_post(alice, dune):
    # D7: the control now sits on rows that are not reviews, so the notice has
    # to follow the row rather than the route's original name.
    post = _review(alice, dune)
    resp = _login("alice").post(f"/status/{post.pk}/delete/")
    assert _messages(resp) == ["Your post has been deleted."]


@pytest.mark.django_db
def test_deleting_a_rating_only_entry_says_post(alice, dune):
    entry = _rating_only(alice, dune)
    resp = _login("alice").post(f"/status/{entry.pk}/delete/")
    assert _messages(resp) == ["Your post has been deleted."]


@pytest.mark.django_db
def test_deleting_a_reply_says_reply(alice, bob, dune):
    post = _review(alice, dune)
    mine = _reply(bob, post)
    resp = _login("bob").post(f"/status/{mine.pk}/delete/")
    assert _messages(resp) == ["Your reply has been deleted."]


@pytest.mark.django_db
def test_a_film_anchored_reply_lands_on_the_film(alice, bob, dune):
    # A reply inherits the film (increment 4), so its delete lands where the
    # review's does. Exact Location, not "a 302".
    post = _review(alice, dune)
    mine = _reply(bob, post)
    resp = _login("bob").post(f"/status/{mine.pk}/delete/")
    assert resp.status_code == 302
    assert resp["Location"] == f"/film/{dune.pk}/"


@pytest.mark.django_db
def test_a_film_less_reply_lands_on_the_index(alice, dune, alien):
    # A reply under a list's face has no film to inherit (L13), so the film page
    # is not a destination for its delete. This is the row that answers
    # ``redirect("index")``, and the only one.
    listing = create_list(alice, title="Cold War Sci-Fi", films=[dune, alien])
    mine = _reply(alice, listing.status)
    resp = _login("alice").post(f"/status/{mine.pk}/delete/")
    assert resp.status_code == 302
    assert resp["Location"] == reverse("index")


@pytest.mark.django_db
def test_deleting_a_reply_leaves_a_tombstone_and_clears_the_text(alice, bob, dune):
    post = _review(alice, dune)
    mine = _reply(bob, post)
    assert _login("bob").post(f"/status/{mine.pk}/delete/").status_code == 302
    mine.refresh_from_db()
    assert mine.deleted is True
    assert mine.content == ""
    assert mine.deleted_date is not None
    # The thread the author is reading no longer offers it, and no longer shows it.
    body = _body(_login("bob"), f"/status/{post.pk}/")
    assert _get_delete_targets(body) == set()
    assert "Agreed." not in body


@pytest.mark.django_db
def test_the_control_is_a_direct_post_with_no_confirm_step(alice, dune):
    # D5: no confirm dialog, matching the app's two existing deletes. The form
    # carries a csrf and nothing else — no JS hook to intercept it.
    post = _review(alice, dune)
    form = re.search(
        r'<form method="post" action="/status/\d+/delete/"[^>]*>(.*?)</form>',
        _body(_login("alice"), f"/status/{post.pk}/"),
        re.S,
    )
    assert form, "the delete control's form did not render"
    inner = form.group(1)
    assert "csrfmiddlewaretoken" in inner
    assert "onclick" not in inner and "data-" not in inner
