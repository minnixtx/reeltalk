"""``Kind.REPLAY`` — the sixth kind, with its producer (replay arc P2, D7).

One operation, because there is nothing else to do: a replay is addressed to the
original's author through the ``recipient``/``actor``/``status`` columns every
other kind already uses, so the row needs no new column the way ``REPORT`` did.
The enum widening is the whole schema change.

It arrives with its producer rather than ahead of it, which is what R90 requires
of a new kind and what both ``MENTION`` and ``REPORT`` did: a value nobody can
trigger is not a placeholder but a code path no test can write against anything
real. The producer here is the replay route, which writes the row when the replay
row is created and not when the toggle runs, so taking a replay back does not
leave a second ledger line behind.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0003_notification_report_and_alter_kind"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="kind",
            field=models.CharField(
                choices=[
                    ("follow", "Follow"),
                    ("like", "Like"),
                    ("reply", "Reply"),
                    ("mention", "Mention"),
                    ("report", "Report"),
                    ("replay", "Replay"),
                ],
                max_length=20,
            ),
        ),
    ]
