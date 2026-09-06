"""Add the Postgres full-text search column + maintenance trigger (PLAN.md §3.2).

``search_vector`` is deliberately *not* a Django model field: it is owned
entirely by the database trigger below, so the ORM never reads or writes it
and an ORM save can never clobber the computed value. M2's local search will
query it via RawSQL.

Weights follow §3.2 — title (A) > subtitle (B) > directors+cast (C) > genres
(D). The ``simple`` text-search config is used on purpose: film titles and
names are proper nouns, and a stemming config would mangle them.
"""

from django.db import migrations

ADD_COLUMN = "ALTER TABLE core_film ADD COLUMN IF NOT EXISTS search_vector tsvector;"

CREATE_FUNCTION = """
CREATE OR REPLACE FUNCTION core_film_update_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('simple', coalesce(NEW.title, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(NEW.subtitle, '')), 'B') ||
        setweight(
            to_tsvector('simple',
                coalesce(array_to_string(NEW.directors, ' '), '') || ' ' ||
                coalesce(array_to_string(NEW."cast", ' '), '')),
            'C') ||
        setweight(to_tsvector('simple', coalesce(array_to_string(NEW.genres, ' '), '')), 'D');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

CREATE_TRIGGER = """
DROP TRIGGER IF EXISTS core_film_search_vector_trigger ON core_film;
CREATE TRIGGER core_film_search_vector_trigger
    BEFORE INSERT OR UPDATE ON core_film
    FOR EACH ROW EXECUTE FUNCTION core_film_update_search_vector();
"""

# Fires the BEFORE trigger on every existing row so a re-run (or a table that
# already held data) ends up fully populated. Setting title to itself changes
# nothing but still fires the trigger.
BACKFILL = "UPDATE core_film SET title = title;"

REVERSE_DROP_TRIGGER = "DROP TRIGGER IF EXISTS core_film_search_vector_trigger ON core_film;"
REVERSE_DROP_FUNCTION = "DROP FUNCTION IF EXISTS core_film_update_search_vector();"
REVERSE_DROP_COLUMN = "ALTER TABLE core_film DROP COLUMN IF EXISTS search_vector;"


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(ADD_COLUMN),
        migrations.RunSQL(CREATE_FUNCTION),
        migrations.RunSQL(CREATE_TRIGGER),
        migrations.RunSQL(BACKFILL),
    ]

    # RunSQL reverses in the order given here, so this drops trigger -> function
    # -> column.
    operations_reverse = [
        migrations.RunSQL(REVERSE_DROP_TRIGGER),
        migrations.RunSQL(REVERSE_DROP_FUNCTION),
        migrations.RunSQL(REVERSE_DROP_COLUMN),
    ]
