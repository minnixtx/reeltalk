from django.apps import AppConfig


class ActivityPubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "reeltalk.activitypub"
    verbose_name = "ActivityPub (federation, M4)"
