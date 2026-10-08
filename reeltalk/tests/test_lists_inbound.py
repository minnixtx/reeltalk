"""Lists federation inbound (§2K increment 7, L11 / L12, R137).

A remote ReelTalk list arriving and becoming a real one here. Every activity
goes through the real ``/inbox/`` route with a real signature, so sender
resolution, the dedup layer and the handler dispatch are exercised rather
than bypassed — the same posture the inbound ``Like`` tests take.

Nine groups, in the order they bite:

* **The branch fires.** A list ``Note`` becomes a ``LIST`` face *and* a
  ``FilmList`` with ``ListItem`` rows. Before this increment it became a
  ``status_type=None`` status holding our prose with nothing on it saying it
  was a list, which is the whole gap.
* **The prose is not parsed.** ``content`` is a rendering this instance also
  generates. The import reads the extension and nothing else.
* **The fan-out is refused.** The headline: a list import makes **zero**
  outbound requests. Films resolve from identifiers against rows already
  here, never by fetching the peer's Film document.
* **The resolution ladder**, rung by rung — home url, tmdb, imdb, title+year.
* **Rank** is preserved as an ordering key, not normalised, and a reorder
  moves the existing rows rather than adding new ones.
* **``Update`` reaches the ``FilmList``** — rename, description, add, remove,
  reorder — and is idempotent on redelivery at both row levels.
* **Empty and malformed input** never raises: an empty list mirrors as an
  empty live list, a missing ``orderedItems`` does not wipe one, and a
  half-broken extension still answers 202.
* **``Delete`` reaches the ``FilmList``**, which is what lets R140 1's
  dismissible notice fire on a mirror instead of only on a list deleted at
  arm's length.
* **L12 and L6 together**: a mirrored list is savable, and saving it
  federates nothing.

Absence assertions name the specific key rather than the whole document, and
every "nothing happened" test has a control that makes the same thing happen.
"""

import json

import pytest
import responses
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory

from reeltalk.activitypub import crypto, signatures
from reeltalk.activitypub.objects import LIST_EXTENSION, note_document
from reeltalk.core.models import Film, Status
from reeltalk.lists.models import FilmList, ListItem, ListSave
from reeltalk.lists.services import create_list
from reeltalk.notifications.models import Notification
from reeltalk.tests.members import member

User = get_user_model()

PEER = "https://peer.example"
REMOTE_ACTOR = f"{PEER}/users/carol/"
REMOTE_INBOX = f"{REMOTE_ACTOR}inbox"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"
FORGER_ACTOR = "http://testserver/user/mallory/"
INBOUND_LOGGER = "reeltalk.activitypub.statuses"


@pytest.fixture()
def keypair():
    return crypto.generate_keypair()


@pytest.fixture()
def carol(db, keypair):
    """The remote peer account, already mirrored, holding the signing key.

    Created directly rather than through a first-contact ``Follow`` so that
    signature verification needs no HTTP: ``resolve_sender`` finds the
    mirror by ``actor_url`` and reads the stored key. That is what makes the
    zero-request assertions below mean what they say — any call ``responses``
    records is one the list import made, not one establishing the sender.
    """
    _private_pem, public_pem = keypair
    user = User(
        localname="carol@peer.example",
        local=False,
        actor_url=REMOTE_ACTOR,
        inbox_url=REMOTE_INBOX,
        public_key=public_pem,
    )
    user.set_unusable_password()
    user.save()
    return user


@pytest.fixture()
def alice(db):
    return member(localname="alice", password="s3cretpass")


@pytest.fixture()
def dune(db):
    return Film.objects.create(title="Dune", year=2021, tmdb_id=438631)


@pytest.fixture()
def alien(db):
    return Film.objects.create(title="Alien", year=1979, imdb_id="tt0078748")


@pytest.fixture()
def blade(db):
    return Film.objects.create(title="Blade Runner", year=1982)


# --- wire builders ---------------------------------------------------------


def _item(name, rank, **extra):
    doc = {"type": "reeltalk:ListItem", "reeltalk:rank": rank, "name": name}
    doc.update(extra)
    return doc


def _extension(items, *, url=f"{PEER}/list/77/", name="Noir You Must See", **extra):
    doc = {
        "type": "reeltalk:List",
        "url": url,
        "name": name,
        "orderedItems": items,
    }
    doc.update(extra)
    return doc


def _note(ext, *, url=f"{PEER}/notes/1/", **extra):
    doc = {
        "id": url,
        "type": "Note",
        "attributedTo": REMOTE_ACTOR,
        "content": "<p>a composed body</p>",
        "publishedTime": "2026-10-08T10:00:00Z",
    }
    if ext is not None:
        doc[LIST_EXTENSION] = ext
    doc.update(extra)
    return doc


def _activity(kind, note, seq):
    return {
        "id": f"{REMOTE_ACTOR}#activity-{seq}",
        "type": kind,
        "actor": REMOTE_ACTOR,
        "object": note,
    }


def _deliver(private_pem, activity):
    """Send one signed activity through the real inbox route."""
    body = json.dumps(activity).encode()
    headers = signatures.sign_request(
        "POST", "http://testserver/inbox/", private_pem, key_id=REMOTE_KEY_ID, body=body
    )
    meta = {f"HTTP_{n.upper().replace('-', '_')}": v for n, v in headers.items()}
    meta["HTTP_HOST"] = "testserver"
    return Client().post(
        "/inbox/", data=body, content_type="application/activity+json", **meta
    )


def _mirror_pair():
    """The (face, list) pair a successful import leaves behind."""
    face = Status.objects.get(local=False, remote_url=f"{PEER}/notes/1/")
    film_list = FilmList.objects.get(remote_url=f"{PEER}/list/77/")
    return face, film_list


def _ranks(film_list):
    return list(
        ListItem.objects.filter(film_list=film_list)
        .order_by("rank", "id")
        .values_list("film__title", flat=True)
    )


# --- the branch fires ------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_list_note_mirrors_as_a_face_and_a_list(carol, keypair, dune, alien):
    private_pem, _public = keypair
    ext = _extension(
        [
            _item("Dune", 1, film=f"{PEER}/film/11/", tmdbId=438631),
            _item("Alien", 2, film=f"{PEER}/film/12/", imdbId="tt0078748"),
        ],
        summary="Black cinema, ranked.",
    )
    response = _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert response.status_code == 202

    face, film_list = _mirror_pair()
    assert face.status_type == Status.Type.LIST
    assert face.local is False
    assert face.user_id == carol.pk
    assert film_list.local is False
    assert film_list.title == "Noir You Must See"
    assert "Black cinema, ranked." in film_list.description
    assert film_list.status_id == face.pk
    assert _ranks(film_list) == ["Dune", "Alien"]


@responses.activate
@pytest.mark.django_db
def test_the_face_is_typed_list_not_the_none_the_shape_mapping_produced(
    carol, keypair, dune
):
    """The gap this increment fills, asserted directly.

    A film-less note with content and no rating resolved to
    ``Status.Type.COMMENT if film is not None else None`` — that is, ``None``
    — so the mirror landed shapeless. The feed row, the ``film_list`` reverse
    lookup and the ``/status/ -> /list/`` redirect all key off the type, so
    a ``None`` face is not a cosmetic miss.
    """
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, film=f"{PEER}/film/11/", tmdbId=438631)])
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    face, _list = _mirror_pair()
    assert face.status_type == Status.Type.LIST


@responses.activate
@pytest.mark.django_db
def test_the_two_rows_carry_two_different_origin_urls(carol, keypair, dune):
    """The face is keyed on the Note's id, the list on the extension's url.

    Both are needed: the first is what a ``Delete`` naming the post finds, the
    second is what a re-delivery of the list finds. Collapsing them onto one
    url would make one of the two lookups blind.
    """
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, film=f"{PEER}/film/11/", tmdbId=438631)])
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    face, film_list = _mirror_pair()
    assert face.remote_url == f"{PEER}/notes/1/"
    assert film_list.remote_url == f"{PEER}/list/77/"
    assert face.remote_url != film_list.remote_url
    assert film_list.remote_id == 77


@responses.activate
@pytest.mark.django_db
def test_the_mirror_belongs_to_the_verified_sender_not_the_declared_actor(
    carol, keypair, dune
):
    """``attributedTo`` names a local account the signature cannot forge.

    If the handler believed the wire, the list would land in alice's name on
    this instance rather than as carol's mirror.
    """
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, film=f"{PEER}/film/11/", tmdbId=438631)])
    _deliver(
        private_pem,
        _activity("Create", _note(ext, attributedTo=FORGER_ACTOR), 1),
    )
    _face, film_list = _mirror_pair()
    assert film_list.user_id == carol.pk
    assert film_list.user.localname == "carol@peer.example"


@responses.activate
@pytest.mark.django_db
def test_a_plain_note_still_mirrors_without_a_list_row(carol, keypair, dune):
    """The control for every assertion above: no extension, no ``FilmList``."""
    private_pem, _public = keypair
    note = {
        "id": f"{PEER}/notes/plain/",
        "type": "Note",
        "attributedTo": REMOTE_ACTOR,
        "content": "Just a note about nothing in particular.",
    }
    _deliver(private_pem, _activity("Create", note, 1))
    face = Status.objects.get(local=False, remote_url=f"{PEER}/notes/plain/")
    assert face.status_type is None
    assert FilmList.objects.count() == 0


@responses.activate
@pytest.mark.django_db
def test_a_list_import_creates_no_notification(carol, keypair, dune):
    """A top-level list ``Create`` answers nobody, so the ledger stays empty.

    The reply producer is the only one that could fire here and it needs a
    parent, which a list face does not have.
    """
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, film=f"{PEER}/film/11/", tmdbId=438631)])
    assert Notification.objects.count() == 0
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert Notification.objects.count() == 0


# --- the prose is not parsed -----------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_the_import_reads_the_extension_and_never_the_prose(carol, keypair, dune):
    """``content`` is a rendering we generate too, not a source of truth.

    The body here names a different list and different films. Anything that
    read a title out of ``<strong>`` or a film out of ``<li>`` would import
    these and be silently coupled to our own markup, breaking the first time
    the prose is restyled.
    """
    private_pem, _public = keypair
    ext = _extension(
        [_item("Dune", 1, film=f"{PEER}/film/11/", tmdbId=438631)],
        name="The Real Title",
    )
    _deliver(
        private_pem,
        _activity(
            "Create",
            _note(
                ext,
                content=(
                    "<p><strong>A Different Title</strong></p>"
                    "<ol><li>A Different Film (1900)</li></ol>"
                ),
            ),
            1,
        ),
    )
    _face, film_list = _mirror_pair()
    assert film_list.title == "The Real Title"
    assert "Different" not in film_list.title
    assert [item.film.title for item in film_list.items.all()] == ["Dune"]


# --- the fan-out is refused ------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_importing_a_list_makes_no_http_requests_at_all(carol, keypair, dune, alien):
    """The trap this increment exists to avoid, pinned as a call count.

    Every film url below is registered as a 404, so a fetching importer would
    still "work" for the films it resolved by id and quietly drop the rest —
    the count is the only assertion that can tell the two apart. A 50-film
    list from a peer we have never exchanged films with must not become fifty
    synchronous GETs inside one inbound POST.
    """
    for film_id in (11, 12):
        responses.add(responses.GET, f"{PEER}/film/{film_id}/", status=404)
    private_pem, _public = keypair
    ext = _extension(
        [
            _item("Dune", 1, film=f"{PEER}/film/11/", tmdbId=438631),
            _item("Alien", 2, film=f"{PEER}/film/12/", imdbId="tt0078748"),
        ]
    )
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert len(responses.calls) == 0
    assert _ranks(_mirror_pair()[1]) == ["Dune", "Alien"]


@responses.activate
@pytest.mark.django_db
def test_an_unresolvable_film_is_dropped_and_the_rest_of_the_list_lands(
    carol, keypair, dune, alien
):
    """One bad film out of three must not cost the whole list, forever.

    The single-film answer the review path makes is roll back and retry, which
    is right when a review without its film is no review at all. At list
    length it means one 404 upstream destroys everything and the peer's
    retry policy hammers the same wall every time.
    """
    responses.add(responses.GET, f"{PEER}/film/99/", status=404)
    private_pem, _public = keypair
    ext = _extension(
        [
            _item("Dune", 1, film=f"{PEER}/film/11/", tmdbId=438631),
            _item(
                "The Film Nobody Has",
                2,
                film=f"{PEER}/film/99/",
                tmdbId=999999,
                year=1933,
            ),
            _item("Alien", 3, film=f"{PEER}/film/12/", imdbId="tt0078748"),
        ]
    )
    response = _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert response.status_code == 202
    assert len(responses.calls) == 0
    assert _ranks(_mirror_pair()[1]) == ["Dune", "Alien"]


@responses.activate
@pytest.mark.django_db
def test_the_drop_is_logged_with_what_was_lost(carol, keypair, dune, caplog):
    """A silent drop would leave nobody able to answer "where did my film go".

    The inbox contract is never to raise on an unfamiliar shape, so the log
    line is the only trace a dropped item leaves, and it has to name it.
    """
    private_pem, _public = keypair
    ext = _extension(
        [
            _item("Dune", 1, film=f"{PEER}/film/11/", tmdbId=438631),
            _item("Unobtainium", 2, film=f"{PEER}/film/99/", tmdbId=999999),
        ]
    )
    with caplog.at_level("INFO", logger=INBOUND_LOGGER):
        _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert "Unobtainium" in caplog.text
    assert "dropped" in caplog.text.lower()
    assert "Dune" in [item.film.title for item in _mirror_pair()[1].items.all()]


@responses.activate
@pytest.mark.django_db
def test_a_list_of_only_unresolvable_films_mirrors_as_an_empty_live_list(
    carol, keypair
):
    """Nothing resolvable is still a list that exists, not a delivery that failed."""
    private_pem, _public = keypair
    ext = _extension(
        [_item("Ghost One", 1, film=f"{PEER}/film/91/", tmdbId=900001)],
        name="A List Of Ghosts",
    )
    response = _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert response.status_code == 202
    _face, film_list = _mirror_pair()
    assert film_list.title == "A List Of Ghosts"
    assert film_list.deleted is False
    assert film_list.items.count() == 0


# --- the resolution ladder -------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_film_we_already_mirror_matches_by_its_home_url(carol, keypair):
    """The precise rung: the item's ``film`` url is a row we hold.

    Checked before identifiers, the same order ``_mirror_film`` uses, because
    matching the exact origin row beats matching a same-titled guess.
    """
    mirrored = Film.objects.create(
        title="Stalker", year=1979, remote_url=f"{PEER}/film/555/"
    )
    private_pem, _public = keypair
    ext = _extension([_item("Stalker", 1, film=f"{PEER}/film/555/")])
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    _face, film_list = _mirror_pair()
    assert [item.film_id for item in film_list.items.all()] == [mirrored.pk]
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_a_same_host_film_url_resolves_to_the_local_row_without_a_fetch(
    carol, keypair, dune
):
    """A peer's mirror of our own film comes back to us as our own row.

    The item's url is on our host, so the same-host branch resolves it by pk
    and never re-fetches our own outbox about a film we authored.
    """
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, film=f"http://testserver/film/{dune.pk}/")])
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    _face, film_list = _mirror_pair()
    assert [item.film_id for item in film_list.items.all()] == [dune.pk]
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_resolution_falls_through_tmdb_then_imdb_then_title_and_year(
    carol, keypair, dune, alien, blade
):
    """Three films, three different rungs, none of them a fetch.

    Each item's ``film`` url names a row we do not hold, so only the
    identifiers can land it — which is exactly why increment 6 put
    ``tmdbId`` and ``imdbId`` on every item.
    """
    private_pem, _public = keypair
    ext = _extension(
        [
            _item("Whatever They Called It", 1, film=f"{PEER}/film/1/", tmdbId=438631),
            _item("Also Unknown", 2, film=f"{PEER}/film/2/", imdbId="tt0078748"),
            _item("Blade Runner", 3, film=f"{PEER}/film/3/", year=1982),
        ]
    )
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    _face, film_list = _mirror_pair()
    assert _ranks(film_list) == ["Dune", "Alien", "Blade Runner"]
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_a_title_without_a_year_does_not_match_a_titled_film(carol, keypair, blade):
    """D7's third rung needs both halves; a bare title matches nothing.

    ``find_match`` asks for ``sort_title`` **and** ``year``, so a title alone
    must drop rather than guess at which Blade Runner is meant.
    """
    private_pem, _public = keypair
    ext = _extension([_item("Blade Runner", 1, film=f"{PEER}/film/7/")])
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    _face, film_list = _mirror_pair()
    assert film_list.items.count() == 0


# --- rank ------------------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_non_dense_ranks_survive_unchanged(carol, keypair, dune, alien, blade):
    """Ranks are an ordering key, not the printed ordinal, and are not tidied.

    ``remove_film`` upstream does not close the gap it makes, so a real list
    holds 1, 2, 4. Normalising on the way in would rewrite a fact the origin
    owns for a column nothing is allowed to depend on.
    """
    private_pem, _public = keypair
    ext = _extension(
        [
            _item("Dune", 1, tmdbId=438631),
            _item("Alien", 2, imdbId="tt0078748"),
            _item("Blade Runner", 4, year=1982),
        ]
    )
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    film_list = _mirror_pair()[1]
    assert list(
        film_list.items.order_by("rank", "id").values_list("rank", flat=True)
    ) == [1, 2, 4]


@responses.activate
@pytest.mark.django_db
def test_a_missing_or_bogus_rank_falls_back_to_array_position(
    carol, keypair, dune, alien, blade
):
    """A non-integer rank is a malformed field, not a reason to lose the item.

    The position in ``orderedItems`` is the ordering the array itself asserts,
    so the fallback reads the wire rather than inventing over it. ``True`` is
    excluded on purpose — ``isinstance(True, int)`` is True in Python and a
    wire ``true`` is not rank one.
    """
    private_pem, _public = keypair
    ext = _extension(
        [
            _item("Dune", "first", tmdbId=438631),
            _item("Alien", True, imdbId="tt0078748"),
            _item("Blade Runner", None, year=1982),
        ]
    )
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    film_list = _mirror_pair()[1]
    assert list(
        film_list.items.order_by("rank", "id").values_list("rank", flat=True)
    ) == [1, 2, 3]


@responses.activate
@pytest.mark.django_db
def test_two_items_resolving_to_one_film_do_not_breach_the_unique_constraint(
    carol, keypair, dune
):
    """Duplicate tmdb ids upstream hit ``one_row_per_film_per_list``.

    Resolved to one row here rather than discovered as an ``IntegrityError``
    in production, with the second entry reported as the duplicate.
    """
    private_pem, _public = keypair
    ext = _extension(
        [
            _item("Dune", 1, tmdbId=438631),
            _item("Dune Again", 2, tmdbId=438631),
        ]
    )
    response = _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert response.status_code == 202
    film_list = _mirror_pair()[1]
    assert film_list.items.count() == 1


# --- Update reaches the FilmList -------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_remote_rename_lands_on_the_list_not_only_the_face(carol, keypair, dune):
    """If only the mirrored prose moved, the page and its post would disagree
    permanently and invisibly."""
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, tmdbId=438631)], name="First Name")
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert _mirror_pair()[1].title == "First Name"

    _deliver(
        private_pem,
        _activity(
            "Update",
            _note(_extension([_item("Dune", 1, tmdbId=438631)], name="Second Name")),
            2,
        ),
    )
    _face, film_list = _mirror_pair()
    assert film_list.title == "Second Name"
    assert FilmList.objects.count() == 1


@responses.activate
@pytest.mark.django_db
def test_a_remote_description_change_lands_on_the_list(carol, keypair, dune):
    private_pem, _public = keypair
    _deliver(
        private_pem,
        _activity(
            "Create",
            _note(_extension([_item("Dune", 1, tmdbId=438631)], summary="Before.")),
            1,
        ),
    )
    _deliver(
        private_pem,
        _activity(
            "Update",
            _note(_extension([_item("Dune", 1, tmdbId=438631)], summary="After.")),
            2,
        ),
    )
    assert _mirror_pair()[1].description == "After."


@responses.activate
@pytest.mark.django_db
def test_a_film_removed_upstream_deletes_the_row_not_its_rank(
    carol, keypair, dune, alien
):
    """Removing means the row goes, not that its rank is zeroed.

    A zeroed rank would leave the film in the list rendering at position
    zero — the offer-with-no-content shape again, one table lower down.
    """
    private_pem, _public = keypair
    ext = _extension(
        [_item("Dune", 1, tmdbId=438631), _item("Alien", 2, imdbId="tt0078748")]
    )
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert _ranks(_mirror_pair()[1]) == ["Dune", "Alien"]

    _deliver(
        private_pem,
        _activity("Update", _note(_extension([_item("Dune", 1, tmdbId=438631)])), 2),
    )
    assert _ranks(_mirror_pair()[1]) == ["Dune"]


@responses.activate
@pytest.mark.django_db
def test_a_film_added_upstream_appears_at_its_rank(carol, keypair, dune, alien):
    private_pem, _public = keypair
    _deliver(
        private_pem,
        _activity("Create", _note(_extension([_item("Dune", 1, tmdbId=438631)])), 1),
    )
    _deliver(
        private_pem,
        _activity(
            "Update",
            _note(
                _extension(
                    [
                        _item("Alien", 1, imdbId="tt0078748"),
                        _item("Dune", 2, tmdbId=438631),
                    ]
                )
            ),
            2,
        ),
    )
    assert _ranks(_mirror_pair()[1]) == ["Alien", "Dune"]


@responses.activate
@pytest.mark.django_db
def test_a_reorder_moves_the_existing_rows_rather_than_adding_new_ones(
    carol, keypair, dune, alien
):
    """Re-ranking is an update to the same rows, which is what makes the
    mirror's identity stable across a reorder."""
    private_pem, _public = keypair
    ext = _extension(
        [_item("Dune", 1, tmdbId=438631), _item("Alien", 2, imdbId="tt0078748")]
    )
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    before = {
        item.film_id: item.pk
        for item in ListItem.objects.filter(film_list=_mirror_pair()[1])
    }
    _deliver(
        private_pem,
        _activity(
            "Update",
            _note(
                _extension(
                    [
                        _item("Alien", 1, imdbId="tt0078748"),
                        _item("Dune", 2, tmdbId=438631),
                    ]
                )
            ),
            2,
        ),
    )
    after = {
        item.film_id: item.pk
        for item in ListItem.objects.filter(film_list=_mirror_pair()[1])
    }
    assert after == before
    assert _ranks(_mirror_pair()[1]) == ["Alien", "Dune"]


@responses.activate
@pytest.mark.django_db
def test_a_redelivered_create_changes_nothing_at_either_row_level(
    carol, keypair, dune, alien
):
    """The same ``Create`` twice: one face, one list, the same items.

    The dedup layer catches the identical activity id; this delivers the same
    *object* under a fresh activity id, which is the case dedup cannot see
    and only the origin-keyed upsert can.
    """
    private_pem, _public = keypair
    ext = _extension(
        [_item("Dune", 1, tmdbId=438631), _item("Alien", 2, imdbId="tt0078748")]
    )
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    face_pk = _mirror_pair()[0].pk
    list_pk = _mirror_pair()[1].pk
    item_pks = sorted(ListItem.objects.values_list("pk", flat=True))

    _deliver(private_pem, _activity("Create", _note(ext), 2))
    assert Status.objects.filter(local=False).count() == 1
    assert FilmList.objects.count() == 1
    assert _mirror_pair()[0].pk == face_pk
    assert _mirror_pair()[1].pk == list_pk
    assert sorted(ListItem.objects.values_list("pk", flat=True)) == item_pks


@responses.activate
@pytest.mark.django_db
def test_a_redelivered_update_writes_nothing_at_all(carol, keypair, dune):
    """Idempotence strong enough to show up in ``updated_date``.

    A field-by-field ``save()`` with no change check would bump the timestamp
    on every redelivery and make an untouched mirror look edited.
    """
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, tmdbId=438631)], name="Stable", summary="Same.")
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    first = _mirror_pair()[1].updated_date

    _deliver(
        private_pem,
        _activity(
            "Update",
            _note(
                _extension(
                    [_item("Dune", 1, tmdbId=438631)], name="Stable", summary="Same."
                )
            ),
            2,
        ),
    )
    film_list = _mirror_pair()[1]
    film_list.refresh_from_db()
    assert film_list.updated_date == first


# --- empty and malformed ---------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_an_empty_list_mirrors_as_an_empty_live_list(carol, keypair):
    """``orderedItems: []`` is a valid list with zero films, not a nothing.

    The "an empty note mirrors nothing" early return does not cover this:
    increment 6 always composes a body — an empty list says "No films in
    this list yet" — so the extension is the only thing distinguishing the
    two, and it says *list*.
    """
    private_pem, _public = keypair
    ext = _extension([], name="Still Deciding")
    response = _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert response.status_code == 202
    face, film_list = _mirror_pair()
    assert face.status_type == Status.Type.LIST
    assert film_list.deleted is False
    assert film_list.items.count() == 0


@responses.activate
@pytest.mark.django_db
def test_a_missing_ordered_items_leaves_an_existing_mirror_s_rows_alone(
    carol, keypair, dune, alien
):
    """A missing array is a malformed partial, not a deliberately emptied list.

    Reading the two the same would let a peer that sent a truncated extension
    destroy a mirror it meant to refresh. An empty array still empties it —
    the next test pair.
    """
    private_pem, _public = keypair
    ext = _extension(
        [_item("Dune", 1, tmdbId=438631), _item("Alien", 2, imdbId="tt0078748")]
    )
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert _mirror_pair()[1].items.count() == 2

    broken = _extension([], name="Changed Name")
    del broken["orderedItems"]
    _deliver(private_pem, _activity("Update", _note(broken), 2))
    _face, film_list = _mirror_pair()
    assert film_list.title == "Changed Name"
    assert film_list.items.count() == 2


@responses.activate
@pytest.mark.django_db
def test_an_empty_array_does_emptied_the_list(carol, keypair, dune, alien):
    """The other half of the pair above: an empty array is a decision."""
    private_pem, _public = keypair
    ext = _extension(
        [_item("Dune", 1, tmdbId=438631), _item("Alien", 2, imdbId="tt0078748")]
    )
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    _deliver(
        private_pem,
        _activity("Update", _note(_extension([], name="Emptied")), 2),
    )
    assert _mirror_pair()[1].items.count() == 0


@responses.activate
@pytest.mark.django_db
def test_an_overlong_title_is_clamped_not_a_database_error(carol, keypair, dune):
    """``varchar(200)`` overflow is a ``DatabaseError``, and inside an inbox
    handler that means the sender retries into the same wall forever."""
    private_pem, _public = keypair
    long_name = "A" * 400
    ext = _extension([_item("Dune", 1, tmdbId=438631)], name=long_name)
    response = _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert response.status_code == 202
    assert len(_mirror_pair()[1].title) == 200


@pytest.mark.django_db
@pytest.mark.parametrize(
    "ext",
    [
        pytest.param("not an object", id="extension-is-a-string"),
        pytest.param([], id="extension-is-a-list"),
        pytest.param({"name": "No Url"}, id="no-url"),
        pytest.param({"url": f"{PEER}/list/9/"}, id="no-name"),
        pytest.param({"url": f"{PEER}/list/9/", "name": "   "}, id="blank-name"),
        pytest.param(
            {"url": f"{PEER}/list/9/", "name": "Bad Items", "orderedItems": "nope"},
            id="ordered-items-not-a-list",
        ),
    ],
)
def test_a_malformed_extension_never_raises_and_still_answers_202(carol, keypair, ext):
    """§3.6: an unfamiliar shape creates nothing and never raises.

    A 500 from an inbox handler means the peer retries forever, so the
    contract is that nothing on the wire can make this path throw.
    """
    private_pem, _public = keypair
    response = _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert response.status_code == 202


@responses.activate
@pytest.mark.django_db
def test_a_malformed_extension_still_makes_the_face_a_list_but_no_list(carol, keypair):
    """The extension's *presence* is the declaration; its contents build the row.

    A half-broken extension yields a ``LIST`` face with nothing behind it,
    which is the degraded state increment 6 already handles on the way out
    rather than a new one.
    """
    private_pem, _public = keypair
    _deliver(private_pem, _activity("Create", _note({"name": "No Url"}), 1))
    face = Status.objects.get(local=False, remote_url=f"{PEER}/notes/1/")
    assert face.status_type == Status.Type.LIST
    assert FilmList.objects.count() == 0


@responses.activate
@pytest.mark.django_db
def test_an_item_with_no_film_reference_at_all_is_dropped(carol, keypair, dune):
    private_pem, _public = keypair
    ext = _extension(
        [
            _item("Dune", 1, tmdbId=438631),
            {"type": "reeltalk:ListItem", "reeltalk:rank": 2},
        ]
    )
    response = _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert response.status_code == 202
    assert _ranks(_mirror_pair()[1]) == ["Dune"]


@responses.activate
@pytest.mark.django_db
def test_a_non_dict_item_is_dropped_rather_than_raising(carol, keypair, dune):
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, tmdbId=438631), "just a string"])
    response = _deliver(private_pem, _activity("Create", _note(ext), 1))
    assert response.status_code == 202
    assert _ranks(_mirror_pair()[1]) == ["Dune"]


# --- the extension is the one we publish -----------------------------------


@responses.activate
@pytest.mark.django_db
def test_what_we_publish_is_what_we_import(carol, keypair, dune, alien):
    """Round trip: the exact bytes increment 6 emits, re-hosted onto a peer.

    A local list is serialized with the real serializer, its host rewritten
    to the peer's, and fed back in through the inbox. Whatever the importer
    reads has to be whatever the exporter wrote, with no field invented on
    either side.
    """
    mine = create_list(
        member(localname="origin", password="s3cretpass"),
        title="Imported Verbatim",
        description="Round tripped.",
        films=[dune, alien],
    )
    doc = note_document(mine.status, RequestFactory().get("/"))
    peer_doc = json.loads(json.dumps(doc).replace("http://testserver", PEER))
    private_pem, _public = keypair
    response = _deliver(
        private_pem,
        {
            "id": f"{REMOTE_ACTOR}#round-trip-1",
            "type": "Create",
            "actor": REMOTE_ACTOR,
            "object": peer_doc,
        },
    )
    assert response.status_code == 202
    film_list = FilmList.objects.get(remote_url=f"{PEER}/list/{mine.pk}/")
    assert film_list.title == "Imported Verbatim"
    assert "Round tripped." in film_list.description
    assert _ranks(film_list) == ["Dune", "Alien"]
    assert len(responses.calls) == 0


# --- delete reaches the FilmList -------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_remote_delete_soft_deletes_the_list_and_not_only_the_face(
    carol, keypair, dune
):
    """The gap: the face was tombstoned and the ``FilmList`` stayed live.

    A live list with a dead post keeps rendering, keeps being savable, and
    never tells anybody it was deleted.
    """
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, tmdbId=438631)])
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    face, film_list = _mirror_pair()
    assert film_list.deleted is False

    _deliver(private_pem, _activity("Delete", _note(ext), 2))
    face.refresh_from_db()
    film_list.refresh_from_db()
    assert face.deleted is True
    assert film_list.deleted is True
    assert film_list.deleted_date is not None


@responses.activate
@pytest.mark.django_db
def test_a_remote_delete_reaches_the_saver_s_deleted_notice(
    carol, keypair, alice, dune
):
    """R140 1 fires on a mirror, which it could not before this increment.

    The saver sees the notice rather than watching the card vanish, and can
    dismiss it — the whole point of the soft delete surviving.
    """
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, tmdbId=438631)])
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    film_list = _mirror_pair()[1]
    client = Client()
    assert client.login(username="alice", password="s3cretpass")
    assert client.post(f"/list/{film_list.pk}/save/").status_code == 200

    _deliver(private_pem, _activity("Delete", _note(ext), 2))

    saved = client.get("/user/alice/lists/?tab=saved")
    assert saved.status_code == 200
    assert b"list-deleted-notice" in saved.content
    assert b"Noir You Must See" in saved.content


@responses.activate
@pytest.mark.django_db
def test_a_redelivered_delete_changes_nothing(carol, keypair, dune):
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, tmdbId=438631)])
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    _face, film_list = _mirror_pair()
    first_deleted_date = None

    _deliver(private_pem, _activity("Delete", _note(ext), 2))
    film_list.refresh_from_db()
    first_deleted_date = film_list.deleted_date
    _deliver(private_pem, _activity("Delete", _note(ext), 3))
    film_list.refresh_from_db()
    assert film_list.deleted_date == first_deleted_date


@responses.activate
@pytest.mark.django_db
def test_deleting_an_ordinary_note_still_only_tombstones_the_status(carol, keypair):
    """The control: a note with no list behind it takes no list down."""
    private_pem, _public = keypair
    note = {
        "id": f"{PEER}/notes/plain/",
        "type": "Note",
        "attributedTo": REMOTE_ACTOR,
        "content": "A plain note, deleted.",
    }
    _deliver(private_pem, _activity("Create", note, 1))
    face = Status.objects.get(local=False, remote_url=f"{PEER}/notes/plain/")
    _deliver(private_pem, _activity("Delete", note, 2))
    face.refresh_from_db()
    assert face.deleted is True
    assert FilmList.objects.count() == 0


# --- L12: a mirrored list is savable, and saving it is silent --------------


@responses.activate
@pytest.mark.django_db
def test_a_mirrored_list_page_renders_for_a_member_and_anonymously(
    carol, keypair, alice, dune, alien
):
    """L4 makes it public and the templates needed no change to render it."""
    private_pem, _public = keypair
    ext = _extension(
        [_item("Dune", 1, tmdbId=438631), _item("Alien", 2, imdbId="tt0078748")],
        summary="Black cinema, ranked.",
    )
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    film_list = _mirror_pair()[1]

    anonymous = Client().get(f"/list/{film_list.pk}/")
    assert anonymous.status_code == 200
    assert b"Noir You Must See" in anonymous.content
    assert b"Black cinema, ranked." in anonymous.content
    assert b"Dune" in anonymous.content and b"Alien" in anonymous.content

    client = Client()
    assert client.login(username="alice", password="s3cretpass")
    assert client.get(f"/list/{film_list.pk}/").status_code == 200


@responses.activate
@pytest.mark.django_db
def test_a_remote_list_is_savable_through_the_same_route(carol, keypair, alice, dune):
    """L12: a remote mirror saves through the exact route a local list does.

    ``_savable_list`` is deliberately unfiltered on ``local`` for this. A
    ``local=True`` filter added "to be safe" would block L12 from the inside
    out, and this is the test that would catch it.
    """
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, tmdbId=438631)])
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    film_list = _mirror_pair()[1]
    assert film_list.local is False

    client = Client()
    assert client.login(username="alice", password="s3cretpass")
    assert client.post(f"/list/{film_list.pk}/save/").status_code == 200
    assert ListSave.objects.filter(user=alice, film_list=film_list).count() == 1
    assert client.post(f"/list/{film_list.pk}/unsave/").status_code == 200
    assert ListSave.objects.filter(user=alice, film_list=film_list).count() == 0


@responses.activate
@pytest.mark.django_db
def test_saving_a_mirrored_list_federates_nothing(carol, keypair, alice, dune):
    """L6 is absolute, and a remote save is the same rule as a local one.

    ``responses`` is live with carol's inbox registered, so a broadcast that
    fired would show up here even if nothing rendered it.
    """
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    private_pem, _public = keypair
    ext = _extension([_item("Dune", 1, tmdbId=438631)])
    _deliver(private_pem, _activity("Create", _note(ext), 1))
    responses.reset()

    client = Client()
    assert client.login(username="alice", password="s3cretpass")
    assert client.post(f"/list/{_mirror_pair()[1].pk}/save/").status_code == 200
    assert client.post(f"/list/{_mirror_pair()[1].pk}/unsave/").status_code == 200
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
def test_the_l6_silence_control_a_local_edit_on_a_list_does_deliver(carol, alice, dune):
    """Non-vacuity for the test above: the delivery path works, so silence
    on save is silence and not a broken pipe.

    The follower is carol, the same remote account the silence test's
    mirror came from, so the control sends to the very inbox that test left
    quiet. A follower whose inbox was unreachable would make the silence
    test pass on an empty recipient set instead of on silence.
    """
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    carol.follows.add(alice)
    film_list = create_list(alice, title="Local Edit Lands", films=[dune])
    client = Client()
    assert client.login(username="alice", password="s3cretpass")
    client.post(f"/list/{film_list.pk}/edit/", {"title": "Renamed", "description": ""})
    assert len(responses.calls) == 1
    assert json.loads(responses.calls[0].request.body)["type"] == "Update"
