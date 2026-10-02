# Credential-surface throttle table (login / admin login / signup).

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("social", "0018_passwordresettoken"),
    ]

    operations = [
        migrations.CreateModel(
            name="CredentialAttempt",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "surface",
                    models.CharField(
                        choices=[
                            ("login", "Sign-in"),
                            ("admin_login", "Admin sign-in"),
                            ("signup", "Signup"),
                        ],
                        max_length=20,
                    ),
                ),
                ("source_ip", models.CharField(max_length=45)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddIndex(
            model_name="credentialattempt",
            index=models.Index(
                fields=["surface", "source_ip", "created_at"],
                name="credattempt_surface_ip_created",
            ),
        ),
    ]
