#!/bin/bash
set -e

info() { echo >&2 "[$(date --iso-8601=seconds)] $*"; }
die() {
    echo >&2 "$*"
    exit 1
}

trap exit TERM

# The web container runs gunicorn, so it is the one that applies migrations
# and collects static files. Celery containers start after it (depends_on)
# and just run their commands.
if [ "$1" = "gunicorn" ]; then
    info "Applying database migrations"
    python manage.py migrate --no-input || die "failed to migrate"

    info "Collecting static files"
    python manage.py collectstatic --no-input || die "failed to collectstatic"
fi

exec "$@"
