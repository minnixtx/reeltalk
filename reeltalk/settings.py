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
    "reeltalk",
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
        "DIRS": [],
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
# by default. A task queue (Django-Q2) joins with M2 and uses Postgres as its
# cluster — still no Redis.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

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
# reeltalk/urls.py — same model, no separate server.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"

DATA_UPLOAD_MAX_MEMORY_SIZE = (
    env.int("DATA_UPLOAD_MAX_MEMORY_MiB", default=100) * 1024 * 1024
)

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
