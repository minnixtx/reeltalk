"""TMDB-CSV import (D9) + export round-trip (D10) tests.

The import is synchronous and makes no TMDB API calls — rows become ID stubs
and the D11 backfill is queued on commit, so these tests need no ``responses``
mocks. The enqueue path is asserted on the django-q2 OrmQ package (the same
shape test_tasks.py checks).
"""

import csv
import io
from datetime import datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from django_q.models import OrmQ, SignedPackage

from reeltalk.core import import_export
from reeltalk.core.import_export import (
    TMDB_CSV_HEADER,
    TmdbCsvError,
    export_film_csv,
    find_or_create_film_stub,
    import_film_csv,
    import_row,
    parse_release_year,
    parse_tmdb_csv,
    parse_tmdb_rating,
)
from reeltalk.core.models import Film, Shelf, ShelfFilm, Status

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


def shelf_of(user, identifier):
    return Shelf.objects.get(user=user, identifier=identifier)


# kwargs -> CSV column names (the header carries spaces, so the mapping is
# explicit — a typo'd key would silently land in the wrong column).
_COLUMN_KEYS = {
    "tmdb_id": "TMDb ID",
    "imdb_id": "IMDb ID",
    "type": "Type",
    "name": "Name",
    "release_date": "Release Date",
    "season_number": "Season Number",
    "episode_number": "Episode Number",
    "rating": "Rating",
    "your_rating": "Your Rating",
    "date_rated": "Date Rated",
}


def _row(**overrides):
    row = {column: "" for column in TMDB_CSV_HEADER}
    row["Type"] = "movie"
    row["Name"] = "Test Film"
    row["Release Date"] = "1976-05-20T00:00:00Z"
    for key, value in overrides.items():
        row[_COLUMN_KEYS[key]] = value
    return row


def _csv_text(*rows, header=None):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header if header is not None else TMDB_CSV_HEADER)
    for row in rows:
        writer.writerow([row.get(column, "") for column in TMDB_CSV_HEADER])
    return buf.getvalue()


# --- parse_tmdb_csv (header validation, D9) ---------------------------------


def test_parse_valid_header():
    rows = parse_tmdb_csv(_csv_text(_row(name="A"), _row(name="B")))
    assert [r["Name"] for r in rows] == ["A", "B"]
    assert rows[0]["TMDb ID"] == ""


def test_parse_rejects_missing_columns():
    header = [c for c in TMDB_CSV_HEADER if c not in ("TMDb ID", "Date Rated")]
    with pytest.raises(TmdbCsvError) as excinfo:
        parse_tmdb_csv(_csv_text(_row(), header=header))
    assert "TMDb ID" in str(excinfo.value)
    assert "Date Rated" in str(excinfo.value)


def test_parse_rejects_over_row_cap(monkeypatch):
    monkeypatch.setattr(import_export, "MAX_IMPORT_ROWS", 2)
    with pytest.raises(TmdbCsvError) as excinfo:
        parse_tmdb_csv(_csv_text(_row(), _row(), _row()))
    assert "3 rows" in str(excinfo.value)


# --- field parsers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1976-05-20T00:00:00Z", 1976),
        ("2020-01-01", 2020),
        ("", None),
        (None, None),
        ("19", None),
        ("abcd-05-20T00:00:00Z", None),
    ],
)
def test_parse_release_year(raw, expected):
    assert parse_release_year(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7", Decimal("3.5")),
        ("8", Decimal("4")),
        ("10", Decimal("5")),
        ("1", Decimal("0.5")),
        (" 9 ", Decimal("4.5")),
        ("7.5", Decimal("4")),
        ("", None),
        (None, None),
        ("abc", None),
        ("0", None),
        ("11", None),
    ],
)
def test_parse_tmdb_rating(raw, expected):
    assert parse_tmdb_rating(raw) == expected


# --- find_or_create_film_stub (D9: no API calls, D7 matching) ----------------


@pytest.mark.django_db
def test_stub_created_from_row_data():
    film, created = find_or_create_film_stub(
        _row(tmdb_id="78", imdb_id="tt0083926", name="Blade Runner")
    )
    assert created is True
    assert Film.objects.count() == 1
    assert film.tmdb_id == 78
    assert film.imdb_id == "tt0083926"
    assert film.title == "Blade Runner"
    assert film.year == 1976
    # An ID stub — no metadata is invented during the import.
    assert film.description == ""
    assert not film.poster


@pytest.mark.django_db
def test_stub_matches_existing_tmdb_id():
    existing = Film.objects.create(title="Blade Runner", year=1982, tmdb_id=78)
    film, created = find_or_create_film_stub(_row(tmdb_id="78", name="Blade Runner"))
    assert created is False
    assert film == existing
    assert Film.objects.count() == 1


@pytest.mark.django_db
def test_stub_title_year_match_backfills_ids():
    manual = Film.objects.create(title="Blade Runner", year=1982)
    # "The" prefix exercises the leading-article strip in sort_title.
    film, created = find_or_create_film_stub(
        _row(
            tmdb_id="78",
            imdb_id="tt0083926",
            name="The Blade Runner",
            release_date="1982-06-25T00:00:00Z",
        )
    )
    assert created is False
    assert film == manual
    film.refresh_from_db()
    assert film.tmdb_id == 78  # backfilled onto the manual film
    assert film.imdb_id == "tt0083926"


@pytest.mark.django_db
def test_stub_imdb_match_backfills_empty_tmdb_and_year():
    partial = Film.objects.create(title="Some Film", imdb_id="tt1234567")
    # The row carries a release date so the year can be backfilled too.
    film, created = find_or_create_film_stub(
        _row(tmdb_id="99", imdb_id="tt1234567", name="Some Film")
    )
    assert created is False
    assert film == partial
    film.refresh_from_db()
    assert film.tmdb_id == 99
    assert film.year == 1976


@pytest.mark.django_db
def test_stub_non_numeric_tmdb_id_treated_as_absent():
    film, created = find_or_create_film_stub(_row(tmdb_id="abc", name="Odd Film"))
    assert created is True
    assert film.tmdb_id is None


# --- import_row (per-row outcomes, D9) ---------------------------------------


@pytest.mark.django_db
def test_unrated_row_goes_to_watchlist(user):
    watchlist = shelf_of(user, Shelf.TO_READ)
    status, note, film = import_row(user, _row(), watchlist, shelf_of(user, Shelf.READ))
    assert (status, note) == ("created", "added to Watchlist")
    assert ShelfFilm.objects.filter(film=film, shelf=watchlist).exists()


@pytest.mark.django_db
def test_unrated_row_already_on_watchlist_is_a_noop(user):
    film = Film.objects.create(title="Test Film", year=1976, tmdb_id=5)
    watchlist = shelf_of(user, Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=watchlist, film=film, user=user)
    status, note, _ = import_row(
        user, _row(tmdb_id="5"), watchlist, shelf_of(user, Shelf.READ)
    )
    assert (status, note) == ("matched", "already on your Watchlist")
    assert ShelfFilm.objects.filter(film=film).count() == 1


@pytest.mark.django_db
def test_unrated_row_refused_when_watched(user):
    # D1: a watched film can't also be wanted.
    film = Film.objects.create(title="Test Film", year=1976, tmdb_id=5)
    watchlist = shelf_of(user, Shelf.TO_READ)
    watched = shelf_of(user, Shelf.READ)
    ShelfFilm.objects.create(shelf=watched, film=film, user=user)
    status, note, _ = import_row(user, _row(tmdb_id="5"), watchlist, watched)
    assert (status, note) == ("matched", "already in your Watched list")
    assert not ShelfFilm.objects.filter(film=film, shelf=watchlist).exists()


@pytest.mark.django_db
def test_rated_row_goes_to_watched_with_rating_only_entry(user):
    watchlist = shelf_of(user, Shelf.TO_READ)
    watched = shelf_of(user, Shelf.READ)
    row = _row(tmdb_id="78", your_rating="7")
    status, note, film = import_row(user, row, watchlist, watched)
    assert (status, note) == ("created", "Watched — 3.5 stars")
    entry = Status.objects.get(user=user, film=film)
    assert entry.status_type == Status.Type.REVIEW_RATING
    assert entry.rating == Decimal("3.5")
    assert ShelfFilm.objects.filter(film=film, shelf=watched).exists()
    assert not ShelfFilm.objects.filter(film=film, shelf=watchlist).exists()


@pytest.mark.django_db
def test_rated_row_removes_film_from_watchlist(user):
    film = Film.objects.create(title="Test Film", year=1976, tmdb_id=5)
    watchlist = shelf_of(user, Shelf.TO_READ)
    watched = shelf_of(user, Shelf.READ)
    ShelfFilm.objects.create(shelf=watchlist, film=film, user=user)
    status, note, _ = import_row(
        user, _row(tmdb_id="5", your_rating="8"), watchlist, watched
    )
    assert (status, note) == ("matched", "Watched — 4 stars")
    assert not ShelfFilm.objects.filter(film=film, shelf=watchlist).exists()
    assert ShelfFilm.objects.filter(film=film, shelf=watched).exists()


@pytest.mark.django_db
def test_rated_row_never_touches_an_existing_review(user):
    film = Film.objects.create(title="Test Film", year=1976, tmdb_id=5)
    watchlist = shelf_of(user, Shelf.TO_READ)
    watched = shelf_of(user, Shelf.READ)
    review = Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=Decimal("4.5"),
        content="<p>my words</p>",
    )
    status, note, _ = import_row(
        user, _row(tmdb_id="5", your_rating="8"), watchlist, watched
    )
    assert status == "matched"
    assert note == "you already have a review; rating not imported"
    # Nothing changed: no new status, the film was not shelved.
    assert Status.objects.count() == 1
    review.refresh_from_db()
    assert review.rating == Decimal("4.5")
    assert ShelfFilm.objects.count() == 0


@pytest.mark.django_db
def test_non_movie_row_is_skipped(user):
    watchlist = shelf_of(user, Shelf.TO_READ)
    status, note, film = import_row(
        user, _row(type="tv", name="A Series"), watchlist, shelf_of(user, Shelf.READ)
    )
    assert (status, note, film) == ("skipped", "not a movie (tv)", None)
    assert Film.objects.count() == 0


@pytest.mark.django_db
def test_empty_type_defaults_to_movie(user):
    watchlist = shelf_of(user, Shelf.TO_READ)
    status, _, film = import_row(
        user, _row(type=""), watchlist, shelf_of(user, Shelf.READ)
    )
    assert status == "created"
    assert film is not None


@pytest.mark.django_db
def test_missing_name_is_skipped(user):
    watchlist = shelf_of(user, Shelf.TO_READ)
    status, note, film = import_row(
        user, _row(name="   "), watchlist, shelf_of(user, Shelf.READ)
    )
    assert (status, note, film) == ("skipped", "missing name", None)
    assert Film.objects.count() == 0


# --- import_film_csv (transaction + summary + backfill queueing) -------------


@pytest.mark.django_db
def test_import_summary_and_line_numbers(user):
    rows = [
        _row(name="Alpha"),
        _row(type="tv", name="A Series"),
        _row(name="Beta", your_rating="9"),
    ]
    result = import_film_csv(user, rows)
    assert result["summary"] == {
        "total": 3,
        "created": 2,
        "matched": 0,
        "skipped": 1,
    }
    assert [r["line"] for r in result["results"]] == [2, 3, 4]
    assert [r["status"] for r in result["results"]] == ["created", "skipped", "created"]
    assert result["results"][2]["note"] == "Watched — 4.5 stars"


@pytest.mark.django_db
@override_settings(TMDB_API_KEY="k")
def test_import_queues_backfill_on_commit(user):
    rows = [_row(name="Alpha"), _row(type="tv", name="A Series")]
    # Django 6.1: captureOnCommitCallbacks is a TestCase classmethod.
    with TestCase.captureOnCommitCallbacks(execute=True):
        result = import_film_csv(user, rows)
    assert result["backfill_queued"] is True
    # The skipped row contributes no film id.
    assert Film.objects.count() == 1
    package = SignedPackage.loads(OrmQ.objects.get().payload)
    assert package["func"] == "reeltalk.core.tasks.backfill_film_ids"
    assert tuple(package["args"]) == ([Film.objects.get().id],)


@pytest.mark.django_db
@override_settings(TMDB_API_KEY="")
def test_import_does_not_queue_backfill_without_a_key(user):
    with TestCase.captureOnCommitCallbacks(execute=True):
        result = import_film_csv(user, [_row(name="Alpha")])
    assert result["backfill_queued"] is False
    assert OrmQ.objects.count() == 0


# --- export_film_csv (D10) ---------------------------------------------------


def _export_rows(user):
    """The exported CSV as (header, row dicts)."""
    reader = csv.DictReader(io.StringIO(export_film_csv(user)))
    return reader.fieldnames, list(reader)


@pytest.mark.django_db
def test_export_unrated_film_row_is_byte_exact(user):
    film = Film.objects.create(
        title="Test Film", year=1976, tmdb_id=42, imdb_id="tt0075344"
    )
    watchlist = shelf_of(user, Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=watchlist, film=film, user=user)
    text = export_film_csv(user)
    lines = text.split("\r\n")
    assert lines[0] == ",".join(TMDB_CSV_HEADER)
    # Five empty trailing columns: season, episode, rating, your rating, date.
    assert lines[1] == "42,tt0075344,movie,Test Film,1976-01-01T00:00:00Z,,,,,"


@pytest.mark.django_db
def test_export_only_films_with_a_relationship(user):
    related = Film.objects.create(title="Related", year=2000, tmdb_id=1)
    Film.objects.create(title="Unrelated", year=2001, tmdb_id=2)
    watchlist = shelf_of(user, Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=watchlist, film=related, user=user)
    _, rows = _export_rows(user)
    assert [r["Name"] for r in rows] == ["Related"]


@pytest.mark.django_db
def test_export_rated_review_row(user):
    film = Film.objects.create(title="Test Film", year=1976, tmdb_id=42)
    Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=Decimal("4.5"),
        content="<p>good</p>",
        published_date=timezone.make_aware(datetime(2026, 9, 1, 12, 30, 15)),
    )
    _, rows = _export_rows(user)
    assert rows[0]["Your Rating"] == "9"
    assert rows[0]["Date Rated"] == "2026-09-01T12:30:15Z"


@pytest.mark.django_db
def test_export_review_without_rating_exports_unrated(user):
    film = Film.objects.create(title="Test Film", year=1976, tmdb_id=42)
    watchlist = shelf_of(user, Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=watchlist, film=film, user=user)
    Status.objects.create(
        user=user, film=film, status_type=Status.Type.REVIEW, content="<p>no stars</p>"
    )
    _, rows = _export_rows(user)
    assert rows[0]["Your Rating"] == ""
    assert rows[0]["Date Rated"] == ""


@pytest.mark.django_db
def test_export_soft_deleted_review_is_excluded(user):
    film = Film.objects.create(title="Test Film", year=1976, tmdb_id=42)
    watchlist = shelf_of(user, Shelf.TO_READ)
    ShelfFilm.objects.create(shelf=watchlist, film=film, user=user)
    dead = Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW_RATING,
        rating=Decimal("3.5"),
    )
    dead.delete()  # soft delete — a tombstone, not gone
    _, rows = _export_rows(user)
    assert rows[0]["Your Rating"] == ""


@pytest.mark.django_db
def test_export_relationship_set_and_deterministic_order(user):
    watchlist = shelf_of(user, Shelf.TO_READ)
    watched = shelf_of(user, Shelf.READ)
    zeta = Film.objects.create(title="Zeta", year=2020, tmdb_id=3)
    alpha = Film.objects.create(title="Alpha", year=1999, tmdb_id=1)
    comment_only = Film.objects.create(title="Gamma", year=2010, tmdb_id=2)
    ShelfFilm.objects.create(shelf=watchlist, film=zeta, user=user)
    ShelfFilm.objects.create(shelf=watched, film=alpha, user=user)
    Status.objects.create(
        user=user,
        film=alpha,
        status_type=Status.Type.REVIEW_RATING,
        rating=Decimal("4"),
    )
    # A comment-only film is in the relationship set too (reachable via M4).
    Status.objects.create(
        user=user,
        film=comment_only,
        status_type=Status.Type.COMMENT,
        content="<p>hi</p>",
    )
    _, rows = _export_rows(user)
    assert [r["Name"] for r in rows] == ["Alpha", "Gamma", "Zeta"]


@pytest.mark.django_db
def test_export_reimport_is_a_noop(user):
    # A mixed state of everything reachable in v0.1: a watchlist stub, a
    # watched film with a written review, and a manual (no TMDB id) film on
    # the watchlist. Exporting and re-importing changes nothing (D10).
    watchlist = shelf_of(user, Shelf.TO_READ)
    watched = shelf_of(user, Shelf.READ)
    stub = Film.objects.create(
        title="Trackdown", year=1976, tmdb_id=102938, imdb_id="tt0075344"
    )
    review_film = Film.objects.create(title="Blade Runner", year=1982, tmdb_id=78)
    manual = Film.objects.create(title="A Manual Film", year=1969)
    ShelfFilm.objects.create(shelf=watchlist, film=stub, user=user)
    ShelfFilm.objects.create(shelf=watched, film=review_film, user=user)
    ShelfFilm.objects.create(shelf=watchlist, film=manual, user=user)
    review = Status.objects.create(
        user=user,
        film=review_film,
        status_type=Status.Type.REVIEW,
        rating=Decimal("4.5"),
        content="<p>tears</p>",
        raw_content="tears",
    )

    rows = parse_tmdb_csv(export_film_csv(user))
    result = import_film_csv(user, rows)

    # Nothing created — every row matched its existing film.
    assert result["summary"]["created"] == 0
    assert result["summary"]["matched"] == 3
    assert Film.objects.count() == 3
    assert ShelfFilm.objects.count() == 3
    assert Status.objects.count() == 1
    # The review is untouched (the existing-review guard).
    review.refresh_from_db()
    assert review.rating == Decimal("4.5")
    assert review.content == "<p>tears</p>"
