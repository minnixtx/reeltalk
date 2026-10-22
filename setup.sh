#!/usr/bin/env bash
# Generate the secrets ReelTalk needs and fill them into .env.
# Safe to re-run: values that are already set are never overwritten.
set -euo pipefail

ENV_FILE="${1:-.env}"

if [ ! -f "$ENV_FILE" ]; then
    echo "No $ENV_FILE found. Run: cp .env.example $ENV_FILE" >&2
    exit 1
fi

get_var() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }

set_var() {
    local tmp
    tmp="$(mktemp)"
    awk -v k="$1" -v v="$2" 'BEGIN{FS=OFS="="} $1==k{print k OFS v; next}{print}' \
        "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE"
}

gen_secret() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import secrets; print(secrets.token_urlsafe(50))'
    else
        openssl rand -hex 50
    fi
}

PLACEHOLDER_DOMAIN="your-instance.example.com"

domain="$(get_var DOMAIN)"
if [ -z "$domain" ] || [ "$domain" = "$PLACEHOLDER_DOMAIN" ]; then
    if [ -t 0 ]; then
        read -r -p "DOMAIN (e.g. films.example.com): " domain || domain=""
    fi
    # Re-checked against the placeholder, not just emptiness. With no TTY the
    # prompt above is skipped, so the placeholder survives as a non-empty
    # value and a scripted install would come up minted on a domain nobody
    # owns. Fail loudly instead.
    if [ -z "${domain:-}" ] || [ "$domain" = "$PLACEHOLDER_DOMAIN" ]; then
        echo "DOMAIN is required. Set it in $ENV_FILE and re-run." >&2
        exit 1
    fi
    set_var DOMAIN "$domain"
fi

for var in SECRET_KEY POSTGRES_PASSWORD; do
    if [ -z "$(get_var "$var")" ]; then
        echo "Generating $var..."
        set_var "$var" "$(gen_secret)"
    else
        echo "$var already set, skipping."
    fi
done

# The application's own database login. Only fill its password when the login
# is actually named: an install that predates the split has no POSTGRES_APP_USER
# line at all, and inventing a password for a role that does not exist would
# point the app at a login that cannot authenticate. Those installs opt in with
# db/upgrade-role-split.sh instead.
if [ -n "$(get_var POSTGRES_APP_USER)" ] && [ -z "$(get_var POSTGRES_APP_PASSWORD)" ]; then
    echo "Generating POSTGRES_APP_PASSWORD..."
    set_var POSTGRES_APP_PASSWORD "$(gen_secret)"
fi

echo
# Enable the versioned git hooks (blocks co-author trailers, which GitHub would
# otherwise turn into contributor-graph entries for accounts we don't control).
# Best effort: a non-git checkout just skips it.
if git rev-parse --show-toplevel >/dev/null 2>&1 && [ -d .githooks ]; then
    bash .githooks/install.sh
else
    echo "(not a git checkout -- skipping attribution guard; run 'bash .githooks/install.sh' once cloned)"
fi

echo
echo "Done. Start the stack one of two ways:"
echo
echo "  Build the image yourself:"
echo "    docker compose up -d --build"
echo
echo "  Or pull the prebuilt image from Docker Hub (no build):"
echo "    docker compose -f docker-compose.prebuilt.yml up -d"
echo
echo "  Then: curl http://localhost:3030"
echo
echo "The site is plain HTTP; terminate TLS with your own reverse proxy."
echo "A brand-new instance redirects to /setup/ to create the first admin."
echo "See the Deployment section of README.md for both paths, the optional"
echo "settings, SMTP, and upgrades."
