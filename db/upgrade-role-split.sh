#!/usr/bin/env bash
# Land the DB role split on an install that already has a database.
#
# The compose init script only runs against an empty data directory, so an
# existing install never gets the split by pulling a new image. This is that
# missing step: it creates the application login, transfers ownership of every
# object to it, and retires the bootstrap password.
#
# Safe to re-run. It reads and writes the same .env the stack already uses, and
# it does not touch the running containers: recreate web and worker afterwards
# so they pick up the new login.
#
#   ./db/upgrade-role-split.sh            # reads .env
#   ENV_FILE=.env.b ./db/upgrade-role-split.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="${ENV_FILE:-.env}"
SQL_FILE=db/role-split.sql

die() {
    echo "$*" >&2
    exit 1
}

[ -f "$ENV_FILE" ] || die "No $ENV_FILE found."
[ -f "$SQL_FILE" ] || die "Missing $SQL_FILE."

get_var() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }

set_var() {
    local tmp
    tmp="$(mktemp)"
    awk -v k="$1" -v v="$2" 'BEGIN{FS=OFS="="} $1==k{print k OFS v; next}{print}' \
        "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE"
}

BOOT_USER="$(get_var POSTGRES_USER)"
BOOT_USER="${BOOT_USER:-reeltalk}"
APP_USER="$(get_var POSTGRES_APP_USER)"
APP_PASSWORD="$(get_var POSTGRES_APP_PASSWORD)"

if [ -z "$APP_USER" ]; then
    echo "POSTGRES_APP_USER is not set in $ENV_FILE."
    echo "ReelTalk will keep connecting as the bootstrap login, which is a superuser."
    echo "Set POSTGRES_APP_USER=reeltalk_app in $ENV_FILE and re-run this script."
    exit 1
fi

if [ "$APP_USER" = "$BOOT_USER" ]; then
    die "POSTGRES_APP_USER and POSTGRES_USER are both $APP_USER. They must be different logins."
fi

if [ -z "$APP_PASSWORD" ]; then
    echo "Generating POSTGRES_APP_PASSWORD..."
    if command -v python3 >/dev/null 2>&1; then
        APP_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')"
    else
        APP_PASSWORD="$(openssl rand -hex 50)"
    fi
    set_var POSTGRES_APP_PASSWORD "$APP_PASSWORD"
fi

echo "App role:   $APP_USER"
echo "Bootstrap:  $BOOT_USER (its password is rotated below and not recorded)"
echo "Database:   $(get_var POSTGRES_DB)"
echo

# The db container is the only thing that can reach the server: it publishes no
# port. Feed the SQL to psql over stdin so the password stays in the container
# environment and never lands in a process listing on either side.
REELTALK_APP_USER="$APP_USER" \
REELTALK_APP_PASSWORD="$APP_PASSWORD" \
REELTALK_BOOTSTRAP_USER="$BOOT_USER" \
REELTALK_ROTATE_BOOTSTRAP="${REELTALK_ROTATE_BOOTSTRAP:-true}" \
    docker compose exec -T -e REELTALK_APP_USER -e REELTALK_APP_PASSWORD \
        -e REELTALK_BOOTSTRAP_USER -e REELTALK_ROTATE_BOOTSTRAP \
        db psql -v ON_ERROR_STOP=1 -U "$BOOT_USER" -d "$(get_var POSTGRES_DB)" -f - \
        < "$SQL_FILE" || die "role split failed"

cat <<EOF

Done. Two things this changed that you need to know about:

  1. The bootstrap role '$BOOT_USER' now has a random password that is not
     written down anywhere. This is deliberate: its seed was in $ENV_FILE, and
     that file is the web container's whole environment, so leaving it live
     would hand the app a path to a superuser login. Maintenance does not need
     it -- the db container publishes no port, and

         docker compose exec db psql -U $BOOT_USER

     connects over the unix socket, which pg_hba trusts. If you do want a
     recorded maintenance password, set one yourself:

         docker compose exec db psql -U $BOOT_USER -c "ALTER ROLE $BOOT_USER PASSWORD '...'"

  2. Recreate the app containers so they connect as $APP_USER:

         docker compose up -d --force-recreate web worker

Check it landed with:

    docker compose exec db psql -U $APP_USER -d $(get_var POSTGRES_DB) -c '\du'

EOF
