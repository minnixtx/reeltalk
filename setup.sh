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

domain="$(get_var DOMAIN)"
if [ -z "$domain" ] || [ "$domain" = "your-instance.example.com" ]; then
    if [ -t 0 ]; then
        read -r -p "DOMAIN (e.g. films.example.com): " domain || domain=""
    fi
    if [ -z "${domain:-}" ]; then
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

echo
echo "Done. Next steps:"
echo "  docker compose up -d --build"
echo "  curl -4 http://localhost:3030   (IPv6 docker-proxy quirk on this host)"
echo "The site is plain HTTP; terminate TLS with your own reverse proxy."
