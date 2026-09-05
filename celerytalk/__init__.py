"""Celery application. Tasks land with M2/M3 (TMDB backfill, imports)."""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "reeltalk.settings")

app = Celery("celerytalk")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
