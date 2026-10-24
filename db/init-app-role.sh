#!/bin/sh
# First-boot half of the DB role split. Mounted into
# /docker-entrypoint-initdb.d/ by both compose files, so it runs exactly once:
# when the postgres image initialises an empty data directory.
#
# It creates the application login and hands it the database, then retires the
# bootstrap password. See db/role-split.sql for the DDL and db/upgrade-role-split.sh
# for installs that predate the split (their data directory is already
# initialised, so this file never runs on them).
#
# Runs while the image's temporary server is up on the unix socket, where
# pg_hba trusts the local superuser, so no password is needed here.
set -eu

APP_USER="${POSTGRES_APP_USER:-}"
SQL_FILE=/usr/local/share/reeltalk/role-split.sql

if [ -z "$APP_USER" ]; then
    printf 'reeltalk: POSTGRES_APP_USER is empty, so the app keeps using the %s login.\n' "${POSTGRES_USER}"
    printf 'reeltalk: no separate application role was created. To add one later, set POSTGRES_APP_USER and run ./db/upgrade-role-split.sh.\n'
    exit 0
fi

if [ -z "${POSTGRES_APP_PASSWORD:-}" ]; then
    printf 'reeltalk: POSTGRES_APP_USER is set but POSTGRES_APP_PASSWORD is empty.\n' >&2
    printf 'reeltalk: refusing to create a database role with no password.\n' >&2
    printf 'reeltalk: run ./setup.sh (it generates the secrets) or set the value by hand.\n' >&2
    exit 1
fi

REELTALK_APP_USER="$APP_USER"
REELTALK_APP_PASSWORD="$POSTGRES_APP_PASSWORD"
REELTALK_BOOTSTRAP_USER="$POSTGRES_USER"
# Rotation is the default, not an option a deployer has to discover: the seed
# password is in .env, and .env is the web container's whole environment.
REELTALK_ROTATE_BOOTSTRAP="${REELTALK_ROTATE_BOOTSTRAP:-true}"
export REELTALK_APP_USER REELTALK_APP_PASSWORD REELTALK_BOOTSTRAP_USER REELTALK_ROTATE_BOOTSTRAP

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$SQL_FILE"
