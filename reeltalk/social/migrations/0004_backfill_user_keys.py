"""Backfill federation keys for local users created before M4 increment 1.

Keys are generated in ``User.save()`` at creation, so pre-existing accounts
(the instance admin from the first-run wizard, anyone imported before) carry
none and could not sign federation requests. The web entrypoint migrates on
every start, so this runs automatically — no manual step per instance.
"""

from django.db import migrations


def backfill_keys(apps, schema_editor):
    # crypto is app code, not a model: safe to import here (the historical
    # model's methods are not relied upon).
    from reeltalk.activitypub.crypto import generate_keypair

    User = apps.get_model("social", "User")
    for user in User.objects.filter(local=True, private_key=""):
        user.private_key, user.public_key = generate_keypair()
        user.save(update_fields=["private_key", "public_key"])


class Migration(migrations.Migration):
    dependencies = [
        ("social", "0003_user_private_key_user_public_key"),
    ]

    operations = [
        migrations.RunPython(backfill_keys, migrations.RunPython.noop),
    ]
