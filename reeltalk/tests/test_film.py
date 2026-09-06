"""Film domain tests (PLAN.md §3.2, decisions D2 & D7)."""

import pytest
from django.db import connection

from reeltalk.core.models import (
    Film,
    MergedFilm,
    derive_sort_title,
    resolve_film_id,
)

# --- sort_title derivation (§3.2) ------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("The Matrix", "matrix"),
        ("A Star Is Born", "star is born"),
        ("Annie Hall", "annie hall"),
        # "In" is not a leading article — only the/a/an are stripped.
        ("In the Mood for Love", "in the mood for love"),
        ("No Country for Old Men", "no country for old men"),
        ("THE HATE U GIVE", "hate u give"),
        # Surrounding whitespace is ignored; internal spacing is preserved.
        ("  The   Shining  ", "shining"),
        # A standalone article has no following word, so it stays — an empty
        # sort_title would be useless for ordering/dedup.
        ("The", "the"),
    ],
)
def test_derive_sort_title(title, expected):
    assert derive_sort_title(title) == expected


@pytest.mark.django_db
def test_save_sets_sort_title():
    film = Film.objects.create(title="The Big Lebowski", year=1998)
    assert film.sort_title == "big lebowski"


# --- dedup lookup (D7) ------------------------------------------------------


@pytest.mark.django_db
def test_find_match_by_tmdb_id():
    film = Film.objects.create(title="Dune", year=2021, tmdb_id=496243)
    assert Film.find_match(tmdb_id=496243) == film


@pytest.mark.django_db
def test_find_match_by_imdb_id():
    film = Film.objects.create(title="Dune", year=2021, imdb_id="tt1160419")
    assert Film.find_match(imdb_id="tt1160419") == film


@pytest.mark.django_db
def test_find_match_by_title_and_year():
    film = Film.objects.create(title="The Godfather", year=1972)
    # Normalized (article-stripped, lowercased) title + year is the fallback.
    assert Film.find_match(title="godfather", year=1972) == film


@pytest.mark.django_db
def test_find_match_requires_year_for_title_fallback():
    film = Film.objects.create(title="Blade Runner", year=1982)
    # No year → the title/year fallback cannot apply.
    assert Film.find_match(title="blade runner") is None
    assert Film.find_match(title="Blade Runner 2049", year=2017) is None
    assert film.pk is not None


@pytest.mark.django_db
def test_find_match_prefers_tmdb_id_over_title():
    by_id = Film.objects.create(title="Dune", year=2021, tmdb_id=496243)
    Film.objects.create(title="Dune: Part Two", year=2024)
    match = Film.find_match(tmdb_id=496243, title="dune: part two", year=2024)
    assert match == by_id


@pytest.mark.django_db
def test_find_match_none_when_nothing_matches():
    Film.objects.create(title="Blade Runner", year=1982)
    assert Film.find_match(title="Arrival", year=2016) is None


# --- merge id resolution (§3.2 MergedFilm) ----------------------------------


@pytest.mark.django_db
def test_resolve_film_id_follows_chain():
    canonical = Film.objects.create(title="Memento", year=2000)
    absorbed = Film.objects.create(title="Memento (dup)", year=2001)
    MergedFilm.objects.create(old_id=absorbed.id, new_id=canonical.id)
    assert resolve_film_id(absorbed.id) == canonical.id


@pytest.mark.django_db
def test_resolve_film_id_multihop():
    canonical = Film.objects.create(title="A", year=1)
    middle = Film.objects.create(title="B", year=2)
    oldest = Film.objects.create(title="C", year=3)
    MergedFilm.objects.create(old_id=oldest.id, new_id=middle.id)
    MergedFilm.objects.create(old_id=middle.id, new_id=canonical.id)
    assert resolve_film_id(oldest.id) == canonical.id


@pytest.mark.django_db
def test_resolve_film_id_unmerged_returns_self():
    film = Film.objects.create(title="Soylent Green", year=1973)
    assert resolve_film_id(film.id) == film.id


# --- tsvector trigger (hand-written SQL, §3.2) ------------------------------


@pytest.mark.django_db
def test_search_vector_populated_by_trigger():
    Film.objects.create(
        title="The Matrix",
        year=1999,
        subtitle="Special Edition",
        directors=["Lilly Wachowski", "Lana Wachowski"],
        cast=["Keanu Reeves"],
        genres=["Action", "Sci-Fi"],
    )
    with connection.cursor() as cur:
        cur.execute(
            "SELECT search_vector FROM core_film WHERE title = %s", ["The Matrix"]
        )
        row = cur.fetchone()
    assert row is not None
    vector = str(row[0])
    # Title words, subtitle, director/cast and genre tokens all present.
    for token in ("matrix'", "special'", "wachowski'", "keanu'", "action'"):
        assert token in vector


@pytest.mark.django_db
def test_search_vector_recomputed_on_update():
    film = Film.objects.create(title="The Departed", year=2006)
    film.title = "Se7en"
    film.save()
    with connection.cursor() as cur:
        cur.execute("SELECT search_vector FROM core_film WHERE id = %s", [film.id])
        vector = str(cur.fetchone()[0])
    assert "se7en'" in vector
