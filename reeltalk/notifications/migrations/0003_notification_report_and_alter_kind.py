"""``Kind.REPORT`` and the pointer a report row needs (D3/D5).

Two operations, one migration: the enum gains its fifth value at the same
moment the pointer that makes its rendered line possible is added, because
neither half is shippable alone - a kind with no way to render its D5 line,
or a pointer no kind uses.

The ``report`` FK exists because the ledger row as designed (kind, actor,
``status=None``) carries no join key to the ``Report`` whose target and
category the line must name. The alternative - matching reporter plus
timestamp at render - is a heuristic this table has no business resting on.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("moderation", "0005_domainblock"),
        ("notifications", "0002_alter_notification_kind"),
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
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="notification",
            name="report",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="notifications",
                to="moderation.report",
            ),
        ),
    ]
