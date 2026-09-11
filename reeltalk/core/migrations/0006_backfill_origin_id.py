"""Backfill the day-one ActivityPub origin identity (M4 increment 3, R41).

``origin_id`` is the id an object carries when it was created locally (§3.2);
for a local row that is its own pk. The fields landed unused in M1/M4, so
existing rows are null — fill them from the pk so wire ids (``/film/<id>/``,
``/status/<id>/``) resolve to the real routes. New rows get it on create
(Film.save / Status.save); this migration heals the pre-existing ones.

Remote mirrors (increment 4) set ``remote_id`` instead and are left alone: a
row already carrying an origin identity is never touched.
"""

from django.db import migrations, models


def backfill_origin_id(apps, schema_editor):
    Film = apps.get_model("core", "Film")
    Status = apps.get_model("core", "Status")
    # F("id") sets origin_id to the row's own pk in a single UPDATE each.
    Film.objects.filter(origin_id__isnull=True).update(origin_id=models.F("id"))
    Status.objects.filter(origin_id__isnull=True, local=True).update(
        origin_id=models.F("id")
    )


def noop(apps, schema_editor):
    # Reverting the backfill is not meaningful (the values are derivable); a
    # fresh database simply never runs the forward function.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_film_raw_description"),
    ]

    operations = [
        migrations.RunPython(backfill_origin_id, noop),
    ]
