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

# DOMAIN may carry an explicit port (an IP:port operator instance, R52); the
# bare host is what ALLOWED_HOSTS and From addresses need.
DOMAIN = env.str("DOMAIN", default="localhost")
WEB_PORT = env.int("WEB_PORT", default=3030)
DOMAIN_HOST = DOMAIN.split(":", 1)[0]
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[DOMAIN_HOST, "localhost"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Which peers are allowed to tell us the scheme a request arrived with (the
# TLS terminator's address/CIDR, e.g. 192.168.1.141/32). Empty means no proxy
# in front of us: nothing forwarded is believed and SECURE_PROXY_SSL_HEADER stays
# off, so a misconfigured deploy can never trust a scheme from a stranger.
TRUSTED_PROXIES = env.list("TRUSTED_PROXIES", default=[])
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https") if TRUSTED_PROXIES else None
)

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
    # Notifications (R97): the event ledger for follow/like/reply and the
    # unread contract. Its own app because its producers live in all three
    # apps above and its readers — the page and the badge — belong to none of
    # them, so one shared home beats bolting the model onto whichever app
    # asked first.
    "reeltalk.notifications",
    # Mentions (§2C): the parser and the storage. Its own app for the same
    # reason notifications got one -- the feature spans core (write + render),
    # activitypub (the wire in both directions) and notifications (the kind),
    # so none of those three is the natural owner. Increment 1 only: nothing
    # calls into it yet.
    "reeltalk.mentions",
    # Moderation (moderation arc, R100/R101): the moderator role's gate and
    # surface. Its own app because the arc reaches into core (statuses, the
    # delete path), social (the role, suspension) and activitypub (the Flag
    # wire, the domain block), so none of those three is the natural owner --
    # the same span that justified the two apps above.
    "reeltalk.moderation",
    # Task queue (M2, R4): Django-Q2's ORM cluster tables. The worker service
    # runs `qcluster` from the same image; its cluster lives in Postgres.
    "django_q",
]

MIDDLEWARE = [
    # Must be first: it decides whether the forwarded scheme downstream code
    # (request.scheme, build_absolute_uri, ActivityPub IDs) may be believed.
    "reeltalk.proxy_trust.TrustedProxySchemeMiddleware",
    # Must stay after the gate above: it keys the cookie Secure flag off
    # request.is_secure(), which is only trustworthy once the gate has run.
    "reeltalk.cookie_policy.SchemeAwareCookieMiddleware",
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
                # The signup CTA on the anonymous surfaces has to follow the
                # instance policy; `site` is only passed by the views that
                # happen to need it, so the bit itself goes through here.
                "reeltalk.social.context_processors.signup_open",
                # The header badge (R94): server-rendered, no polling. One
                # indexed COUNT on an authenticated render, guarded inside
                # the processor, and it never runs for a JSON response.
                "reeltalk.notifications.context_processors.unread_notifications",
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

# Database backups (M6): the daily pg_dump job writes custom-format dumps to
# BACKUP_DIR and keeps the newest BACKUP_RETENTION per database. The `backup`
# compose service runs it on a schedule; the management command is also runnable
# by hand or from host cron. BACKUP_DIR defaults to /app/backups — the backups
# named volume mounted in docker-compose.yml.
BACKUP_DIR = env.str("BACKUP_DIR", default=str(BASE_DIR / "backups"))
BACKUP_RETENTION = env.int("BACKUP_RETENTION", default=7)

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

# Cookies carry the session, so they must never travel over plain HTTP. Both
# Secure flags default off in Django, which means an instance reached once over
# http:// leaks the session id in the clear. SameSite=Lax is already the
# Django default and is spelled out here because the invite-only deployment
# relies on it: top-level navigations still carry the cookie (a follow link
# from another instance still logs you in), cross-site POSTs do not.
SECURE_COOKIES = env.bool("SECURE_COOKIES", default=True)
# One instance can front two transports: the public https domain and a plain
# http://<lan-ip> endpoint. true (default) sets Secure only on requests that
# actually arrived over https, so the LAN endpoint can still hold a session;
# false makes the Secure flag unconditional (strict -- and it makes plain-HTTP
# login impossible anywhere, including the LAN).
COOKIES_FOLLOW_SCHEME = env.bool("COOKIES_FOLLOW_SCHEME", default=True)
SESSION_COOKIE_SECURE = SECURE_COOKIES
CSRF_COOKIE_SECURE = SECURE_COOKIES
SESSION_COOKIE_SAMESITE = "Lax"

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

# The From address needs a bare domain — no port (R52).
DEFAULT_FROM_EMAIL = f"{env.str('EMAIL_SENDER_NAME', default='admin')}@{DOMAIN_HOST}"
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Logging. With no LOGGING setting every app logger falls through to
# ``logging.lastResort``, which only emits WARNING and above — so the
# success line for a delivered activity never reached ``docker logs`` and
# an outbound federation delivery that worked looked identical to one that
# was never attempted (R88).
#
# The root stays at WARNING rather than turning on INFO across Django; the
# delivery logger is raised on its own because its INFO line is the only
# record we keep that a delivery happened at all.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "line": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "line",
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "reeltalk.activitypub.delivery": {
            "handlers": ["console"],
            "level": env.str("DELIVERY_LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
        # The inbound mirror of the delivery line: one record per activity the
        # inbox accepts, naming its type, its verified sender and the outcome
        # it decided. Same reason as above — before this the inbox discarded
        # its own outcome the way delivery used to discard the response
        # status, so an activity we deliberately ignored looked exactly like
        # one that never arrived.
        "reeltalk.activitypub.inbox": {
            "handlers": ["console"],
            "level": env.str("INBOX_LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
    },
}
