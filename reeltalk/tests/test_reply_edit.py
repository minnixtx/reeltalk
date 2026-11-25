"""Editing your own reply (author edit/delete arc, increment I2).

The arc's one new route, and the four things it has to keep true:

* **The page and the route agree** (R85). ``may_edit`` is the single home of who
  may, asked by a reply row before it renders its Edit link and by the route
  before it accepts the POST. A review, a rating-only row, a list's face, a
  mirror and someone else's reply are refused at both ends: their doors are the
  film page's modal, nothing, the list editor, and not theirs.
* **An edit writes text and nothing else.** ``content`` is re-rendered from the
  new source, ``visibility`` is not in the update set, and ``edited_date`` is
  stamped only when the text actually changed — so a Save on an untouched reply
  neither marks it edited nor puts a no-op ``Update`` on the wire.
* **The editor is server-rendered.** ``?edit=<id>`` puts a real textarea in the
  HTML, pre-filled from ``raw_content``, the same shape ``?reply=1`` gives the
  composer, so it works with JavaScript switched off. The id is in the param
  because a member with two replies in one thread has to be able to open one.
* **Mentions move with the text.** The rows are re-synced before the broadcast,
  so a dropped ``@name`` stops being delivered to and a new one arrives.

Absence assertions read the set of ids a render targets (``_edit_links``,
``_edit_targets``) rather than searching the body for a phrase, for the reason
I1 filed in its own file: a page-wide "not in body" check passes vacuously the
moment the markup changes shape.
"""

import json
import re

import pytest
import responses
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import get_messages as _django_get_messages
from django.test import Client

from reeltalk.core.models import (
    REPLY_MAX_LENGTH,
    Film,
    Status,
    Visibility,
    add_reply,
    may_edit,
)
from reeltalk.lists.services import create_list
from reeltalk.mentions.models import StatusMention, sync_status_mentions
from reeltalk.mentions.parser import mentions_from_text
from reeltalk.notifications.models import Notification
from reeltalk.social.models import User
from reeltalk.tests.members import member

ZED_ACTOR = "https://remote.example/users/zed"
ZED_INBOX = ZED_ACTOR + "/inbox"

# Every row shape the gate answers, and whether alice (the reader in every case
# below) may edit it. The list face is here as a shape even though its own page
# is a redirect: the route's refusal of it is pinned directly, because "the UI
# never points a form at it" is not a guard.
SHAPES = {
    "your own reply": True,
    "another member's reply": False,
    "your own review": False,
    "your own rating-only row": False,
    "a list's face": False,
    "a mirror of another instance's reply": False,
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


@pytest.fixture
def zed(db):
    """A remote member: mentionable, and a delivery target when named."""
    user = User(
        localname="zed@remote.example",
        local=False,
        actor_url=ZED_ACTOR,
        inbox_url=ZED_INBOX,
    )
    user.set_unusable_password()
    user.save()
    return user


def _login(localname: str) -> Client:
    client = Client()
    assert client.login(username=localname, password="s3cretpass")
    return client


def _body(client: Client, url: str) -> str:
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.content.decode()


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


def _rating_only(user, film):
    return Status.objects.create(
        user=user, film=film, status_type=Status.Type.REVIEW_RATING, rating="4.00"
    )


def _reply(author, parent, raw="Agreed.", content=None):
    return add_reply(
        author, parent, content=content or f"<p>{raw}</p>", raw_content=raw
    )


def _mirror_reply(film):
    carol = User(
        localname="carol@remote.example",
        local=False,
        actor_url="https://remote.example/users/carol",
        inbox_url="https://remote.example/users/carol/inbox",
    )
    carol.set_unusable_password()
    carol.save()
    return _reply(carol, _review(carol, film))


def _shape_row(shape, alice, bob, dune):
    """The row, and the page on which this reader is ever offered it."""
    if shape == "your own reply":
        post = _review(bob, dune)
        return _reply(alice, post), f"/status/{post.pk}/"
    if shape == "another member's reply":
        post = _review(alice, dune)
        return _reply(bob, post), f"/status/{post.pk}/"
    if shape == "your own review":
        row = _review(alice, dune)
        return row, f"/status/{row.pk}/"
    if shape == "your own rating-only row":
        row = _rating_only(alice, dune)
        return row, f"/status/{row.pk}/"
    if shape == "a list's face":
        listing = create_list(alice, title="Cold War Sci-Fi", films=[dune])
        return listing.status, f"/list/{listing.pk}/"
    row = _mirror_reply(dune)
    return row, f"/status/{row.reply_parent_id}/"


def _edit_links(body: str) -> set[str]:
    """Every reply id this render offers an Edit link on."""
    return set(re.findall(r'class="reply-edit-link" href="\?edit=(\d+)"', body))


def _edit_targets(body: str) -> set[str]:
    """Every reply id whose inline editor this render opened."""
    pattern = r'<form class="reply-edit" method="post" action="/status/(\d+)/edit/"'
    return set(re.findall(pattern, body))


def _messages(resp):
    return [m.message for m in _django_get_messages(resp.wsgi_request)]


def _levels(resp):
    return [(m.level_tag, m.message) for m in _django_get_messages(resp.wsgi_request)]


def _urls() -> list[str]:
    return [call.request.url for call in responses.calls]


# --- the rule itself ---------------------------------------------------------


@pytest.mark.django_db
def test_the_rule_itself_answers_its_clauses(alice, bob, dune):
    """``may_edit`` pinned clause by clause, from the model side."""
    theirs = _review(bob, dune)
    mine = _reply(alice, theirs)
    assert may_edit(alice, mine) is True
    assert may_edit(bob, mine) is False
    assert may_edit(AnonymousUser(), mine) is False
    # A review's door is the film page's modal (D2), a rating row has no text to
    # rewrite, and a list's face is edited through the list editor (D4).
    assert may_edit(alice, theirs) is False
    assert may_edit(alice, _rating_only(alice, dune)) is False
    face = create_list(alice, title="X", films=[dune]).status
    assert may_edit(alice, face) is False
    # A mirror is another instance's row, and a tombstone is already the record.
    assert may_edit(alice, _mirror_reply(dune)) is False
    mine.delete()
    assert may_edit(alice, mine) is False


@pytest.mark.django_db
def test_a_reply_under_a_list_face_is_editable_without_a_status_type(alice, dune):
    # The clause this exists to pin. L13 made a reply's type follow its film, so
    # a reply under a film-less face carries no ``status_type`` at all while
    # being a reply in every way a reader can see. Reading the gate off the type
    # would refuse exactly the replies the list page renders.
    listing = create_list(alice, title="Cold War Sci-Fi", films=[dune])
    mine = _reply(alice, listing.status)
    assert mine.status_type is None
    assert may_edit(alice, mine) is True


# --- the Edit link -----------------------------------------------------------


@pytest.mark.django_db
def test_your_own_reply_row_carries_an_edit_link(alice, bob, dune):
    post = _review(bob, dune)
    mine = _reply(alice, post)
    body = _body(_login("alice"), f"/status/{post.pk}/")
    assert _edit_links(body) == {str(mine.pk)}


@pytest.mark.django_db
def test_no_edit_link_on_someone_elses_reply_or_for_nobody_signed_in(alice, bob, dune):
    post = _review(alice, dune)
    theirs = _reply(bob, post)
    assert _edit_links(_body(_login("bob"), f"/status/{post.pk}/")) == {str(theirs.pk)}
    assert _edit_links(_body(_login("alice"), f"/status/{post.pk}/")) == set()
    assert _edit_links(_body(Client(), f"/status/{post.pk}/")) == set()


@pytest.mark.django_db
def test_the_list_page_thread_carries_the_same_edit_link(alice, bob, dune, alien):
    # The owner's ruling was that the Edit goes wherever a reply row renders, and
    # the list page builds its thread through the same helper and partial. These
    # are the film-less rows, which is the case a type-based gate would drop.
    listing = create_list(alice, title="Cold War Sci-Fi", films=[dune, alien])
    mine = _reply(bob, listing.status)
    theirs = _reply(alice, listing.status)
    page = f"/list/{listing.pk}/"
    assert _edit_links(_body(_login("bob"), page)) == {str(mine.pk)}
    assert _edit_links(_body(_login("alice"), page)) == {str(theirs.pk)}


@pytest.mark.django_db
def test_a_reply_posted_without_a_reload_carries_the_edit_link(alice, bob, dune):
    # comments.js inserts the JSON's markup verbatim, so that render is a third
    # place the flag has to be set or the new row lands half-dressed.
    post = _review(alice, dune)
    resp = _login("bob").post(f"/status/{post.pk}/reply/", {"content": "Agreed."})
    html = json.loads(resp.content.decode())["html"]
    reply = Status.objects.get(reply_parent=post)
    assert _edit_links(html) == {str(reply.pk)}
    # And no editor: a row that has just been written is not a row being edited.
    assert _edit_targets(html) == set()


@pytest.mark.django_db
@pytest.mark.parametrize("shape", list(SHAPES))
def test_the_link_renders_exactly_where_the_route_lands(alice, bob, dune, shape):
    """The R85 seam, both sides, one row shape at a time.

    The control is offered if and only if the same client's POST is accepted.
    Reading the page first matters: an accepted POST rewrites the row, so the
    render assertion has to be spent before the write.
    """
    row, page = _shape_row(shape, alice, bob, dune)
    client = _login("alice")
    shown = str(row.pk) in _edit_links(_body(client, page))
    refused = client.post(f"/status/{row.pk}/edit/").status_code == 404
    assert shown is not refused, shape


# --- the editor --------------------------------------------------------------


@pytest.mark.django_db
def test_the_param_opens_the_editor_prefilled_from_the_source(alice, bob, dune):
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="My **own** words.")
    body = _body(_login("alice"), f"/status/{post.pk}/?edit={mine.pk}")
    assert _edit_targets(body) == {str(mine.pk)}
    assert f'action="/status/{mine.pk}/edit/"' in body
    # Pre-filled from ``raw_content``, never the rendered HTML: the source is the
    # only honest thing to hand back to someone about to rewrite it.
    assert "My **own** words." in body
    assert "<strong>own</strong>" not in body
    assert f'maxlength="{REPLY_MAX_LENGTH}"' in body
    assert "autofocus" in body


@pytest.mark.django_db
def test_the_editor_replaces_the_reply_text_and_its_edit_link(alice, bob, dune):
    # One editor per row: the body is swapped, not doubled, and a row already
    # being edited has no reason to offer the link that opened it.
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="Agreed.", content="<p>Rendered body.</p>")
    assert "Rendered body." in _body(_login("alice"), f"/status/{post.pk}/")
    editing = _body(_login("alice"), f"/status/{post.pk}/?edit={mine.pk}")
    assert "Rendered body." not in editing
    assert _edit_links(editing) == set()


@pytest.mark.django_db
def test_only_the_row_the_param_names_opens(alice, bob, dune):
    # The reason the param carries an id rather than a 1: two of your own replies
    # in one thread, and only the one you clicked opens.
    post = _review(bob, dune)
    first = _reply(alice, post, raw="First.", content="<p>First body.</p>")
    second = _reply(alice, post, raw="Second.", content="<p>Second body.</p>")
    body = _body(_login("alice"), f"/status/{post.pk}/?edit={second.pk}")
    assert _edit_targets(body) == {str(second.pk)}
    assert "First body." in body
    assert "Second body." not in body
    assert _edit_links(body) == {str(first.pk)}


@pytest.mark.django_db
def test_the_editor_is_a_plain_post_and_not_the_composers_ajax_form(alice, bob, dune):
    # The trap this pins. ``comments.js`` claims every ``form.reply-form`` on the
    # page, calls preventDefault and answers it with a fetch that expects JSON, so
    # an editor wearing that class never navigates: the member clicks Save, the
    # browser swallows it, and nothing appears to happen. Found by clicking the
    # control in a real browser with this whole file green.
    post = _review(bob, dune)
    mine = _reply(alice, post)
    body = _body(_login("alice"), f"/status/{post.pk}/?edit={mine.pk}")
    form = re.search(
        r'<form class="([^"]*)" method="post" action="/status/\d+/edit/">(.*?)</form>',
        body,
        re.S,
    )
    assert form, "the inline editor did not render"
    classes, inner = form.groups()
    assert "reply-form" not in classes, classes
    assert "csrfmiddlewaretoken" in inner
    assert "onclick" not in inner and "data-" not in inner


@pytest.mark.django_db
def test_the_editor_does_not_open_for_a_row_you_may_not_edit(alice, bob, dune):
    # The flag and the param are both read: naming a row in the URL is not what
    # makes it yours.
    post = _review(alice, dune)
    theirs = _reply(bob, post, content="<p>Bob's body.</p>")
    body = _body(_login("alice"), f"/status/{post.pk}/?edit={theirs.pk}")
    assert _edit_targets(body) == set()
    assert "Bob's body." in body


@pytest.mark.django_db
def test_a_param_that_names_nothing_opens_nothing(alice, bob, dune):
    # A junk value, a bare param, and a review's own id all leave the page as it
    # arrived. The review is not a reply, so it has no inline editor at all (D2):
    # its door is the film page's modal, and that is I3's work, not this route's.
    post = _review(alice, dune)
    client = _login("alice")
    for query in ("?edit=", "?edit=x", f"?edit={post.pk}", "?edit=999999"):
        body = _body(client, f"/status/{post.pk}/{query}")
        assert _edit_targets(body) == set(), query
        assert "Spice must be reviewed." in body


# --- the route: what it writes -----------------------------------------------


@pytest.mark.django_db
def test_editing_writes_both_halves_and_stamps_the_date(alice, bob, dune):
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="Agreed.", content="<p>Agreed.</p>")
    assert mine.edited_date is None
    resp = _login("alice").post(
        f"/status/{mine.pk}/edit/", {"content": "Agreed, **strongly**."}
    )
    assert resp.status_code == 302
    mine.refresh_from_db()
    assert mine.raw_content == "Agreed, **strongly**."
    # The rendered half is re-rendered from the new source, not patched into the
    # stored HTML.
    assert mine.content == "<p>Agreed, <strong>strongly</strong>.</p>"
    assert mine.edited_date is not None


@pytest.mark.django_db
def test_an_edit_sanitizes_the_new_markdown_like_any_new_reply(alice, bob, dune):
    # With no allowed link domains the href is stripped and the anchor text
    # stays — the same write-time gate ``reply_to_status`` applies.
    post = _review(bob, dune)
    mine = _reply(alice, post)
    _login("alice").post(
        f"/status/{mine.pk}/edit/",
        {"content": "Like it, see [here](https://elsewhere.example/x)."},
    )
    mine.refresh_from_db()
    assert "href" not in mine.content
    assert "elsewhere.example" not in mine.content


@pytest.mark.django_db
def test_the_notice_names_the_reply(alice, bob, dune):
    post = _review(bob, dune)
    mine = _reply(alice, post)
    resp = _login("alice").post(f"/status/{mine.pk}/edit/", {"content": "New words."})
    assert _messages(resp) == ["Your reply has been updated."]


@pytest.mark.django_db
def test_an_unchanged_save_writes_nothing_and_stamps_nothing(alice, bob, dune):
    # The precedent is ``mark_watched``: stamp on change, not on every save. A
    # Save that changed nothing must not make the reply look edited, and the
    # notice must not claim an update that did not happen.
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="Agreed.")
    resp = _login("alice").post(f"/status/{mine.pk}/edit/", {"content": "Agreed."})
    assert _levels(resp) == [("info", "Your reply is unchanged.")]
    mine.refresh_from_db()
    assert mine.edited_date is None
    assert mine.raw_content == "Agreed."


@pytest.mark.django_db
def test_an_empty_body_is_refused_and_writes_nothing(alice, bob, dune):
    # Clearing the text is not a way to delete a reply: that is what the Delete
    # beside the Edit link is for (D5). The refusal leaves the editor open, so
    # the reader is back where they were with the reason on the page.
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="Agreed.")
    for blank in ("", "   ", "\n\n"):
        resp = _login("alice").post(f"/status/{mine.pk}/edit/", {"content": blank})
        assert resp["Location"] == f"/status/{post.pk}/?edit={mine.pk}"
    mine.refresh_from_db()
    assert mine.raw_content == "Agreed."
    assert mine.edited_date is None


@pytest.mark.django_db
def test_an_over_cap_body_is_refused_and_writes_nothing(alice, bob, dune):
    # Same cap and same sentence ``add_reply`` refuses with, checked before the
    # write — pinned against the writer's own message so the two doors cannot
    # drift apart on the number or on the wording.
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="Agreed.")
    with pytest.raises(ValueError) as exc:
        add_reply(bob, post, content="", raw_content="x" * (REPLY_MAX_LENGTH + 1))
    resp = _login("alice").post(
        f"/status/{mine.pk}/edit/", {"content": "x" * (REPLY_MAX_LENGTH + 1)}
    )
    assert _messages(resp) == [str(exc.value)]
    mine.refresh_from_db()
    assert mine.raw_content == "Agreed."
    assert mine.edited_date is None


@pytest.mark.django_db
def test_a_body_at_the_cap_is_accepted(alice, bob, dune):
    # The boundary either side of the refusal above: the cap is inclusive, so a
    # reply of exactly the limit is a write and not a refusal.
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="Agreed.")
    at_the_cap = "x" * REPLY_MAX_LENGTH
    _login("alice").post(f"/status/{mine.pk}/edit/", {"content": at_the_cap})
    mine.refresh_from_db()
    assert mine.raw_content == at_the_cap


@pytest.mark.django_db
def test_visibility_is_not_in_the_update_set(alice, bob, dune):
    # An audience is fixed at writing (owner ruling 2026-10-28), so a posted
    # ``visibility`` is ignored rather than merely absent from the form.
    post = _review(bob, dune, visibility=Visibility.FOLLOWERS)
    mine = _reply(alice, post)
    assert mine.visibility == Visibility.FOLLOWERS
    _login("alice").post(
        f"/status/{mine.pk}/edit/",
        {"content": "New words.", "visibility": Visibility.PUBLIC},
    )
    mine.refresh_from_db()
    assert mine.visibility == Visibility.FOLLOWERS


# --- the route: who it refuses -----------------------------------------------


@pytest.mark.django_db
def test_a_stranger_cannot_edit_another_members_reply(alice, bob, dune):
    post = _review(alice, dune)
    theirs = _reply(bob, post, raw="Bob's own words.")
    resp = _login("alice").post(f"/status/{theirs.pk}/edit/", {"content": "Mine now."})
    assert resp.status_code == 404
    theirs.refresh_from_db()
    assert theirs.raw_content == "Bob's own words."


@pytest.mark.django_db
@pytest.mark.parametrize("shape", [s for s in SHAPES if not SHAPES[s]])
def test_the_route_refuses_every_shape_the_page_withholds(alice, bob, dune, shape):
    row, _page = _shape_row(shape, alice, bob, dune)
    before = (row.raw_content, row.edited_date)
    resp = _login("alice").post(f"/status/{row.pk}/edit/", {"content": "No."})
    assert resp.status_code == 404, shape
    row.refresh_from_db()
    assert (row.raw_content, row.edited_date) == before


@pytest.mark.django_db
def test_a_deleted_reply_cannot_be_edited(alice, bob, dune):
    post = _review(bob, dune)
    mine = _reply(alice, post)
    mine.delete()
    resp = _login("alice").post(f"/status/{mine.pk}/edit/", {"content": "Back."})
    assert resp.status_code == 404


@pytest.mark.django_db
def test_the_route_requires_login(dune):
    zed = member(localname="zed", password="s3c")
    theirs = _reply(zed, _review(zed, dune))
    resp = Client().post(f"/status/{theirs.pk}/edit/", {"content": "hi"})
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


@pytest.mark.django_db
def test_the_route_is_post_only(alice, bob, dune):
    post = _review(bob, dune)
    mine = _reply(alice, post)
    assert _login("alice").get(f"/status/{mine.pk}/edit/").status_code == 405


@pytest.mark.django_db
def test_the_route_needs_a_csrf_token(alice, bob, dune):
    post = _review(bob, dune)
    mine = _reply(alice, post)
    client = Client(enforce_csrf_checks=True)
    assert client.login(username="alice", password="s3cretpass")
    resp = client.post(f"/status/{mine.pk}/edit/", {"content": "hi"})
    assert resp.status_code == 403


# --- where the redirect lands ------------------------------------------------


@pytest.mark.django_db
def test_a_saved_edit_lands_back_on_the_thread_page(alice, bob, dune):
    # Exact Location, not a 3xx shrug: the page whose thread shows this row is
    # the parent's page, which is where the Edit link was clicked.
    post = _review(bob, dune)
    mine = _reply(alice, post)
    resp = _login("alice").post(f"/status/{mine.pk}/edit/", {"content": "New."})
    assert resp["Location"] == f"/status/{post.pk}/"


@pytest.mark.django_db
def test_an_edit_under_a_list_face_lands_on_the_list(alice, dune):
    # A list's canonical page is /list/<id>/ (L10), so the status URL bounces —
    # and the bounce carries the query string, which is why the editor can be
    # opened from the list page and still be refused back open there.
    listing = create_list(alice, title="Cold War Sci-Fi", films=[dune])
    mine = _reply(alice, listing.status)
    resp = _login("alice").post(f"/status/{mine.pk}/edit/", {"content": "New."})
    assert resp["Location"] == f"/status/{listing.status.pk}/"
    followed = _login("alice").get(resp["Location"], follow=True)
    assert followed.redirect_chain[0][0] == f"/list/{listing.pk}/"


@pytest.mark.django_db
def test_an_edit_of_an_orphaned_reply_lands_on_the_reply_itself(alice, bob, dune):
    # The parent is a tombstone, and a deleted post 404s on its own URL, so the
    # reply's own page is the only place it can be read. Landing on a 404 after
    # a saved edit would read as the edit having deleted the reply.
    post = _review(bob, dune)
    mine = _reply(alice, post)
    post.delete()
    resp = _login("alice").post(f"/status/{mine.pk}/edit/", {"content": "Still here."})
    assert resp["Location"] == f"/status/{mine.pk}/"
    assert _login("alice").get(resp["Location"]).status_code == 200


# --- the wire ----------------------------------------------------------------


@pytest.mark.django_db
@responses.activate
def test_the_update_carries_the_new_body(alice, bob, dune, zed):
    # zed follows alice, so he is in the audience for anything she writes.
    zed.follows.add(alice)
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="First version.", content="<p>First version.</p>")
    responses.add(responses.POST, ZED_INBOX)
    _login("alice").post(f"/status/{mine.pk}/edit/", {"content": "Second version."})
    assert _urls() == [ZED_INBOX]
    sent = json.loads(responses.calls[0].request.body)
    assert sent["type"] == "Update"
    assert "Second version." in sent["object"]["content"]


@pytest.mark.django_db
@responses.activate
def test_an_unchanged_save_puts_nothing_on_the_wire(alice, bob, dune, zed):
    # The control for the test above: same audience, same route, no change in the
    # text, so no ``Update``. Without this, the send above could be the route
    # broadcasting whatever arrived.
    zed.follows.add(alice)
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="First version.")
    responses.add(responses.POST, ZED_INBOX)
    _login("alice").post(f"/status/{mine.pk}/edit/", {"content": "First version."})
    assert _urls() == []


@pytest.mark.django_db
@responses.activate
def test_an_edit_that_names_someone_reaches_them(alice, bob, dune, zed):
    # zed follows nobody, so the only reason he receives this is the mention.
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="Asking nobody.")
    responses.add(responses.POST, ZED_INBOX)
    _login("alice").post(
        f"/status/{mine.pk}/edit/", {"content": "Asking @zed@remote.example."}
    )
    assert _urls() == [ZED_INBOX]
    sent = json.loads(responses.calls[0].request.body)
    assert sent["object"]["tag"][0]["name"] == "@zed@remote.example"


@pytest.mark.django_db
@responses.activate
def test_an_edit_that_drops_a_name_stops_reaching_them(alice, bob, dune, zed):
    # Both halves in one test, so the zero is a refusal and not an audience that
    # never existed: the first save keeps the mention and sends, the second drops
    # it and does not.
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="Asking @zed@remote.example.")
    sync_status_mentions(mine, mentions_from_text(mine.raw_content))
    responses.add(responses.POST, ZED_INBOX)
    client = _login("alice")
    client.post(
        f"/status/{mine.pk}/edit/", {"content": "Still asking @zed@remote.example."}
    )
    assert len(responses.calls) == 1
    client.post(f"/status/{mine.pk}/edit/", {"content": "Nobody now."})
    assert len(responses.calls) == 1
    assert not StatusMention.objects.filter(status=mine).exists()


@pytest.mark.django_db
def test_an_edit_that_names_a_member_notifies_them_once(alice, bob, dune):
    # M-e: an edit that newly mentions someone does notify, and the producer is
    # idempotent per (recipient, status), so a second edit does not stack. The
    # mentionee is local on purpose: ``notify`` refuses a remote recipient, so a
    # remote ``@name`` travels on the wire and never in the ledger.
    carol = member(localname="carol", password="s3cretpass")
    post = _review(bob, dune)
    mine = _reply(alice, post, raw="Asking nobody.")
    client = _login("alice")
    client.post(f"/status/{mine.pk}/edit/", {"content": "Asking @carol about this."})
    kind = Notification.Kind.MENTION
    assert (
        Notification.objects.filter(recipient=carol, actor=alice, kind=kind).count()
        == 1
    )
    client.post(
        f"/status/{mine.pk}/edit/", {"content": "Still asking @carol about this."}
    )
    assert (
        Notification.objects.filter(recipient=carol, actor=alice, kind=kind).count()
        == 1
    )
