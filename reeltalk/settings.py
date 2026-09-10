"""Django settings for ReelTalk (AGPLv3 rewrite).

Fresh, minimal configuration for the M0 skeleton. Feature-specific settings
(TMDB key, storage backends, telemetry, ...) land with their milestones —
see PLAN.md §5.
"""

from pathlib import Path

import environs

env = environs.Env()
# In containers the environment comes from docker compose's env_file; this is
# a no-op when no .env file exists (e.g. inside the image).
env.read_env(".env")

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = env.str("SECRET_KEY", default="insecure-dev-only-key-change-me")
DEBUG = env.bool("DEBUG", default=False)

DOMAIN = env.str("DOMAIN", default="localhost")
WEB_PORT = env.int("WEB_PORT", default=3030)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[DOMAIN, "localhost"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
BASE_URL = f"http://{DOMAIN}:{WEB_PORT}"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Postgres field types (ArrayField for the plain name-list fields, D2).
    "django.contrib.postgres",
    # ReelTalk apps: the project package is no longer itself an app — real
    # code lives in reeltalk.core (films/shelves) and reeltalk.social (users/
    # statuses); more apps join as their milestones land (PLAN.md §5, R9).
    "reeltalk.core",
    "reeltalk.social",
    # Federation (M4, R7/R9): ActivityPub from spec — wire types, discovery,
    # inbox/outbox, delivery. No models of its own yet; crypto + signatures
    # land first (increment 1).
    "reeltalk.activitypub",
    # Task queue (M2, R4): Django-Q2's ORM cluster tables. The worker service
    # runs `qcluster` from the same image; its cluster lives in Postgres.
    "django_q",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves collected static files from the web process — this stack has no
    # separate static server (PLAN.md §3.8).
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "reeltalk.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Shared cross-app templates (base.html, auth pages) live at the repo
        # root; per-app templates use APP_DIRS under each app's templates/.
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

ASGI_APPLICATION = "reeltalk.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.str("POSTGRES_DB", default="reeltalk"),
        "USER": env.str("POSTGRES_USER", default="reeltalk"),
        "PASSWORD": env.str("POSTGRES_PASSWORD", default=""),
        "HOST": env.str("POSTGRES_HOST", default="db"),
        "PORT": env.int("POSTGRES_PORT", default=5432),
    }
}

# No Redis in this stack (PLAN.md §3.9): the cache is a performance nicety at
# single-instance scale, so it lives in process memory; sessions are DB-backed
# by default. The task queue (Django-Q2, M2) uses Postgres as its cluster —
# still no Redis.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Django-Q2 cluster (M2, R4): tasks are queued and tracked in Postgres via the
# default ORM connection — no broker service. The `worker` compose service
# runs `qcluster`; it starts after web is healthy, so the django_q tables
# exist by then (the entrypoint migrates only when it starts uvicorn).
Q_CLUSTER = {
    "name": "reeltalk",
    # One worker (R38): a re-import enqueues a second full backfill list by
    # design (it heals stale stubs, D11). With two workers the batches ran in
    # lockstep — every poster double-downloaded and TMDB traffic doubled
    # (2026-09-10 poster incident). Serialized, the second batch runs after
    # the first and is nearly free: backfill_films skips already-complete
    # films without an API call.
    "workers": 1,
    # A full import backfill runs ~15 minutes under D11 pacing; keep the
    # default 1-hour timeout explicit so a long job is never killed mid-run.
    "timeout": 3600,
    # Must be >= timeout or django-q warns at startup: how long an enqueued
    # task waits for acknowledgement before being considered undelivered.
    "retry": 7200,
    "orm": "default",
    "backend": "postgresql",
}

# Custom user model (R10): set before any social migration exists — it is
# very hard to change after the first one. localname@domain identity, defined
# in reeltalk.social.models.
AUTH_USER_MODEL = "social.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# English-only for now (PLAN.md decision D13). Fresh codebase: no i18n
# machinery at all; re-introduction is a later, deliberate step.
LANGUAGE_CODE = "en-us"
TIME_ZONE = env.str("TIME_ZONE", default="UTC")
USE_I18N = False
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = env.str("STATIC_ROOT", default=str(BASE_DIR / "static"))
MEDIA_URL = "/images/"
MEDIA_ROOT = env.str("MEDIA_ROOT", default=str(BASE_DIR / "images"))

# Whitenoise serves the collected static files (compressed, immutable-cached)
# from the web process. Media (/images/) is served by a URL pattern in
# reeltalk/urls.py — same model, no separate server. Production uses manifest
# (hashed-name) storage; under DEBUG whitenoise serves straight from the source
# directories via finders, so tests and local runs need no collectstatic step
# (the entrypoint only collects when it starts uvicorn).
if DEBUG:
    _STATICFILES_BACKEND = "whitenoise.storage.CompressedStaticFilesStorage"
else:
    _STATICFILES_BACKEND = "whitenoise.storage.CompressedManifestStaticFilesStorage"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": _STATICFILES_BACKEND},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
# Django >=5 logout is POST-only; this is where the form redirects after.
LOGOUT_REDIRECT_URL = "/"

DATA_UPLOAD_MAX_MEMORY_SIZE = (
    env.int("DATA_UPLOAD_MAX_MEMORY_MiB", default=100) * 1024 * 1024
)

# TMDB (M2, decision D8): the operator's API key, shared by all users and read
# from .env. Unset means "not configured" — search degrades to local-library
# search instead of erroring (D6). Never written to any tracked file.
TMDB_API_KEY = env.str("REELTALK_TMDB_API_KEY", default="")

# Content Security Policy. img-src allows the TMDB poster CDN from day one —
# the legacy project learned the hard way that adding it later breaks every
# page that shows search results (PLAN.md §3.4).
CSP_DEFAULT_SRC = ["'self'"]
CSP_IMG_SRC = ["'self'", "https://image.tmdb.org", "data:"]
CSP_STYLE_SRC = ["'self'", "'unsafe-inline'"]
CSP_SCRIPT_SRC = ["'self'"]

# Email: SMTP when configured, console backend otherwise (safe dev default).
# Django 6.1's MAILERS setting replaces the deprecated EMAIL_* settings —
# and defining both at once is an error, so the env values are read into
# plain locals, never EMAIL_* module attributes.
smtp_host = env.str("EMAIL_HOST", default="")
if smtp_host:
    MAILERS = {
        "default": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {
                "host": smtp_host,
                "port": env.int("EMAIL_PORT", default=587),
                "username": env.str("EMAIL_HOST_USER", default=""),
                "password": env.str("EMAIL_HOST_PASSWORD", default=""),
                "use_tls": env.bool("EMAIL_USE_TLS", default=True),
            },
        }
    }
else:
    MAILERS = {"default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"}}

DEFAULT_FROM_EMAIL = f"{env.str('EMAIL_SENDER_NAME', default='admin')}@{DOMAIN}"
SERVER_EMAIL = DEFAULT_FROM_EMAIL
