#!/usr/bin/env bash
# Produce .env for a ReelTalk instance.
#
# With no .env present this copies .env.example and fills the copy, so a fresh
# clone needs nothing but bash to get a working configuration. It is safe to
# re-run: a value that is already set is never overwritten. The whole repair
# vocabulary is one rule long, blank a value in .env and re-run to have it
# filled again.
#
# Nothing is ever written as KEY= with an empty value. environs reads the typed
# settings with bool and int parsers that refuse an empty string outright, so a
# blank line stops the boot. env.list is worse than an error: "present but
# empty" resolves to an empty list rather than the shipped default, which for
# TRUSTED_PROXIES means "believe no proxy at all" with nothing printed. A
# question that went unanswered therefore stays commented, exactly as
# .env.example carries it, and the app's own default applies.
set -euo pipefail

ENV_FILE="${1:-.env}"
TEMPLATE=".env.example"
PLACEHOLDER_DOMAIN="your-instance.example.com"

# Staged during the interview, applied only after the summary, so nothing
# half-lands when somebody walks away from a half-answered prompt.
PENDING_KEYS=()
PENDING_VALUES=()
PENDING_NOTES=()
# Every key actually written, printed at the end: a deliberate rotation should
# be visible and an accidental one should not be silent.
WRITTEN=()

get_var() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }

# Write KEY=value. Replaces the live line if there is one, otherwise the
# template's commented "# KEY=value" line (which is how an optional gets
# enabled), otherwise appends. The value travels through ENVIRON rather than
# awk -v, because -v processes backslash escapes and would silently eat a
# backslash somebody typed into an answer.
set_var() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"
    RT_KEY="$key" RT_VAL="$value" awk '
        BEGIN { k = ENVIRON["RT_KEY"]; v = ENVIRON["RT_VAL"]; hit = 0 }
        hit == 0 && $0 ~ "^" k "=" { print k "=" v; hit = 1; next }
        hit == 0 && $0 ~ "^[#][ \t]*" k "=" { print k "=" v; hit = 1; next }
        { print }
        END { if (hit == 0) print k "=" v }
    ' "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    WRITTEN+=("$key")
}

# Queue a write. An empty answer queues nothing, which is what keeps an
# unanswered optional commented, and a value equal to what the file already
# holds queues nothing either, which is what makes a re-run change no bytes.
stage() {
    local key="$1" value="$2" note="${3:-}"
    [ -n "$value" ] || return 0
    [ "$(get_var "$key")" = "$value" ] && return 0
    PENDING_KEYS+=("$key")
    PENDING_VALUES+=("$value")
    PENDING_NOTES+=("${note:-$key=$value}")
}

# True when none of the named keys carries a value, so the question that feeds
# them is worth asking.
needs() {
    local key
    for key in "$@"; do
        if [ -n "$(get_var "$key")" ]; then
            return 1
        fi
    done
    return 0
}

gen_secret() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import secrets; print(secrets.token_urlsafe(50))'
    else
        openssl rand -hex 50
    fi
}

trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

is_uint() { case "$1" in '' | *[!0-9]*) return 1 ;; *) return 0 ;; esac; }

# Reads one answer into ANSWER. Bare Enter takes the default shown in brackets,
# and so does an end of input.
ask() {
    local prompt="$1" def="${2:-}" ans
    [ -n "$def" ] && prompt="$prompt [$def]"
    printf '%s: ' "$prompt"
    IFS= read -r ans || ans=""
    ans="$(trim "$ans")"
    ANSWER="${ans:-$def}"
}

# Same, for a password: nothing echoed, so nothing shown to accept.
ask_hidden() {
    local prompt="$1" ans
    printf '%s: ' "$prompt"
    IFS= read -r -s ans || ans=""
    printf '\n'
    ANSWER="$(trim "$ans")"
}

# Reads a yes/no into ANSWER as yes or no. Loops rather than guessing, because
# a blank answer here can decide whether a plain-HTTP port is open to the
# internet.
ask_yes() {
    local prompt="$1" def="$2" ans
    while :; do
        ask "$prompt" "$def"
        ans="$(lower "$ANSWER")"
        case "$ans" in
            y | yes | true) ANSWER=yes; return 0 ;;
            n | no | false) ANSWER=no; return 0 ;;
            *) echo "  Answer yes or no." ;;
        esac
    done
}

# DOMAIN carries no scheme and no path. Rather than refuse a paste that arrived
# as a URL, take the host out of it and say so on stderr, never on stdout: the
# caller captures this function's output as the value.
clean_domain() {
    local d="$1"
    case "$d" in
        *://*)
            d="${d#*://}"
            printf '  (using the host part of what you pasted)\n' >&2
            ;;
    esac
    d="${d%%/*}"
    printf '%s' "$(lower "$d")"
}

# Every comma-separated entry must be a full origin: scheme, host, and the port
# if there is one. A bare hostname is refused on purpose, because guessing the
# scheme is how a CSRF origin ends up wrong and every form POST then 403s.
is_origin_list() {
    local part rest="$1"
    while [ -n "$rest" ]; do
        part="${rest%%,*}"
        if [ "$part" = "$rest" ]; then
            rest=""
        else
            rest="${rest#*,}"
        fi
        part="$(trim "$part")"
        [ -n "$part" ] || continue
        case "$part" in
            http://* | https://*) ;;
            *) return 1 ;;
        esac
    done
    return 0
}

# Each comma-separated entry has to be an address with an optional prefix
# length: dotted digits for v4, hex and colons for v6. Letters are allowed only
# where an address can actually contain them, so a stray value that is not a
# network at all is refused here rather than at every boot. Spaces are stripped
# first, which also keeps the character classes free of a space: an unquoted
# space inside a case pattern ends the pattern.
is_net_list() {
    local entry rest="$1" addr prefix
    while [ -n "$rest" ]; do
        entry="${rest%%,*}"
        if [ "$entry" = "$rest" ]; then
            rest=""
        else
            rest="${rest#*,}"
        fi
        entry="${entry// /}"
        [ -n "$entry" ] || continue
        addr="${entry%%/*}"
        if [ "$addr" != "$entry" ]; then
            prefix="${entry#*/}"
            case "$prefix" in
                '' | *[!0-9]*) return 1 ;;
            esac
        fi
        case "$addr" in
            *.*)
                case "$addr" in *[!0-9.]*) return 1 ;; esac
                ;;
            *:*)
                case "$addr" in *[!0-9A-Fa-f:]*) return 1 ;; esac
                ;;
            *) return 1 ;;
        esac
    done
    return 0
}

# Split a comma-separated origin list into the bare hostnames ALLOWED_HOSTS
# wants, appended to the seed list it is given.
#
# Each local is its own statement on purpose: bash expands every word of a
# single local before running any of the assignments, so "local a=$1 b=$a" reads
# b's source while it is still unset. Under set -u that aborts the function, and
# because this runs in a command substitution passed as an argument, set -e does
# not catch it and the caller silently receives an empty string.
origin_hosts() {
    local seed="$1"
    local origins="$2"
    local out="$seed"
    local rest="$origins"
    local entry host
    while [ -n "$rest" ]; do
        entry="${rest%%,*}"
        if [ "$entry" = "$rest" ]; then
            rest=""
        else
            rest="${rest#*,}"
        fi
        entry="$(trim "$entry")"
        [ -n "$entry" ] || continue
        host="${entry#*://}"
        host="${host%%/*}"
        host="${host%%:*}"
        case ",$out," in
            *",$host,"*) ;;
            *) out="$out,$host" ;;
        esac
    done
    printf '%s' "$out"
}

# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

if [ ! -f "$ENV_FILE" ]; then
    if [ ! -f "$TEMPLATE" ]; then
        echo "No $ENV_FILE and no $TEMPLATE beside this script. Run it from the" >&2
        echo "root of a ReelTalk clone, which is where the template lives." >&2
        exit 1
    fi
    cp "$TEMPLATE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Created $ENV_FILE from $TEMPLATE."
    echo
fi

# Whether a secret still has to be minted, needed before the summary so the
# summary can promise it honestly.
secrets_pending=0
for var in SECRET_KEY POSTGRES_PASSWORD; do
    [ -z "$(get_var "$var")" ] && secrets_pending=1
done
if [ -n "$(get_var POSTGRES_APP_USER)" ] && [ -z "$(get_var POSTGRES_APP_PASSWORD)" ]; then
    secrets_pending=1
fi

if [ -t 0 ]; then
    echo "ReelTalk setup. Each question says what the variable controls. Bare"
    echo "Enter takes the default in brackets, and a question you skip keeps the"
    echo "shipped default in place. Secrets are generated, never asked."
    echo

    # 1. DOMAIN.
    domain="$(get_var DOMAIN)"
    if [ -z "$domain" ] || [ "$domain" = "$PLACEHOLDER_DOMAIN" ]; then
        echo "DOMAIN is the hostname people type into a browser: no scheme, no"
        echo "path. It seeds ALLOWED_HOSTS, it is what federation identities are"
        echo "minted from, and it is the domain on the From address of every mail"
        echo "this instance sends."
        echo
        while :; do
            ask "DOMAIN (e.g. films.example.com)" ""
            domain="$(clean_domain "$ANSWER")"
            if [ -z "$domain" ] || [ "$domain" = "$PLACEHOLDER_DOMAIN" ]; then
                echo "  A real hostname is required and there is no usable"
                echo "  default: an instance minted on a domain nobody owns"
                echo "  cannot federate, so this question has no skip."
                continue
            fi
            break
        done
        stage DOMAIN "$domain"
    fi

    # 2. A proxy on this host, which decides WEB_BIND.
    if needs WEB_BIND; then
        echo
        echo "WEB_BIND decides which interface the app port is published on. The"
        echo "app serves plain HTTP. If a reverse proxy runs on this same machine"
        echo "and nothing off this machine needs the port, binding to loopback"
        echo "closes the unencrypted port to the world. A proxy on another host,"
        echo "or no proxy at all, needs the default, which is every interface."
        ask_yes "Reverse proxy on this same host?" n
        if [ "$ANSWER" = yes ]; then
            stage WEB_BIND 127.0.0.1
            echo "  TRUSTED_PROXIES keeps its shipped default (the private"
            echo "  ranges), which already covers a proxy on this host or on the"
            echo "  Compose network."
        fi
    fi

    # 3. Extra origins, which feed ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS.
    if needs ALLOWED_HOSTS CSRF_TRUSTED_ORIGINS; then
        echo
        echo "ALLOWED_HOSTS is every host a browser may name in the URL, and"
        echo "CSRF_TRUSTED_ORIGINS is every origin a browser may post a form to."
        echo "Both default to DOMAIN plus localhost, so a single-domain install"
        echo "needs nothing here. Reach the instance by a raw IP without naming"
        echo "it and every page returns 400; without the CSRF entry the pages"
        echo "render but every form POST returns 403."
        while :; do
            ask "Extra origins, comma-separated (e.g. http://192.168.1.50:3030,https://films.example.com), blank for none" ""
            if [ -z "$ANSWER" ] || is_origin_list "$ANSWER"; then
                extra_origins="$ANSWER"
                break
            fi
            echo "  Each entry needs its scheme, and its port if it has one:"
            echo "  http://192.168.1.50:3030 or https://films.example.com."
        done
        if [ -n "$extra_origins" ]; then
            stage ALLOWED_HOSTS "$(origin_hosts "$domain,localhost" "$extra_origins")" \
                "ALLOWED_HOSTS from the extra origins"
            stage CSRF_TRUSTED_ORIGINS "$extra_origins" \
                "CSRF_TRUSTED_ORIGINS from the extra origins"
        fi
    fi

    # 4. WEB_PORT.
    if needs WEB_PORT; then
        echo
        echo "WEB_PORT is the port on this machine that the web container's port"
        echo "3030 is published on. Change it only for a port conflict, and move"
        echo "the reverse proxy's upstream with it."
        while :; do
            ask "WEB_PORT" 3030
            if is_uint "$ANSWER" && [ "$ANSWER" -ge 1 ] && [ "$ANSWER" -le 65535 ]; then
                web_port="$ANSWER"
                break
            fi
            echo "  A port is a number between 1 and 65535."
        done
        stage WEB_PORT "$web_port"
    fi

    # 5. Transport, which decides the redirect and HSTS pair.
    if needs SECURE_SSL_REDIRECT; then
        echo
        echo "ReelTalk never terminates TLS itself. Say how people actually reach"
        echo "the site: https means something in front does TLS and forwards here"
        echo "over http, plain http means there is no certificate at all yet, a"
        echo "LAN box or a proxy you have not finished."
        while :; do
            ask "https through a proxy, or plain http?" https
            case "$(lower "$ANSWER")" in
                https | https-through-a-proxy | proxy | tls | through-a-proxy)
                    transport=https
                    break
                    ;;
                http | plain | plain-http | none)
                    transport=http
                    break
                    ;;
                *) echo "  Answer https or http." ;;
            esac
        done
        if [ "$transport" = https ]; then
            echo
            echo "SECURE_SSL_REDIRECT makes the app itself answer a plain-HTTP"
            echo "request with a redirect to https, and SECURE_HSTS_SECONDS tells"
            echo "a browser to keep using https for that many days. Both are off"
            echo "by default and both are fine to leave off: your proxy can do the"
            echo "redirect, and HSTS is a deliberate, hard to undo choice."
            ask_yes "SECURE_SSL_REDIRECT, redirect http to https?" n
            [ "$ANSWER" = yes ] && stage SECURE_SSL_REDIRECT true
            while :; do
                ask "SECURE_HSTS_SECONDS (0 leaves it off)" 0
                if is_uint "$ANSWER"; then
                    [ "$ANSWER" != 0 ] && stage SECURE_HSTS_SECONDS "$ANSWER"
                    break
                fi
                echo "  Seconds as a whole number, or 0."
            done
        else
            echo
            echo "  Plain HTTP. Session cookies keep working over it because"
            echo "  COOKIES_FOLLOW_SCHEME defaults to true; leave that alone until"
            echo "  you have a certificate. Federation identities still mint on"
            echo "  https://DOMAIN, so a remote peer cannot reach this instance"
            echo "  until that address really answers. CANONICAL_ORIGIN in"
            echo "  .env.example is the override, and it is not for a LAN address."
        fi
    fi

    # 6. Outbound mail.
    if needs EMAIL_HOST; then
        echo
        echo "Mail decides whether anyone can sign up. Every account has to prove"
        echo "its address before it can sign in, with no exception for the admin,"
        echo "so with no mail server the links only print to the container log"
        echo "and nobody off this machine can ever get an account. Leave it blank"
        echo "to run that way on purpose, and read the link out of the log."
        ask "EMAIL_HOST (blank to leave mail off)" ""
        mail_host="$ANSWER"
        if [ -n "$mail_host" ]; then
            stage EMAIL_HOST "$mail_host"
            while :; do
                ask "EMAIL_PORT" 587
                if is_uint "$ANSWER"; then
                    stage EMAIL_PORT "$ANSWER"
                    break
                fi
                echo "  A port is a number. 587 is submission with STARTTLS; 465"
                echo "  is implicit TLS and needs a different backend."
            done
            ask "EMAIL_HOST_USER (the login; blank for a server that needs none)" ""
            if [ -n "$ANSWER" ]; then
                stage EMAIL_HOST_USER "$ANSWER"
                ask_hidden "EMAIL_HOST_PASSWORD (blank for no password)"
                stage EMAIL_HOST_PASSWORD "$ANSWER"
            fi
            ask_yes "EMAIL_USE_TLS, STARTTLS on this port?" y
            [ "$ANSWER" = no ] && stage EMAIL_USE_TLS false
            echo
            echo "The From address is name@domain. On a mail server that refuses a"
            echo "sender other than the account it authenticated, those two halves"
            echo "have to match EMAIL_HOST_USER or every send fails."
            ask "EMAIL_SENDER_NAME (the part before the @)" admin
            stage EMAIL_SENDER_NAME "$ANSWER"
            ask "EMAIL_SENDER_DOMAIN (blank to use $(get_var DOMAIN))" ""
            stage EMAIL_SENDER_DOMAIN "$ANSWER"
        fi
    fi

    # 7. TMDB.
    if needs REELTALK_TMDB_API_KEY; then
        echo
        echo "REELTALK_TMDB_API_KEY is a free key from themoviedb.org/settings/api."
        echo "Without it the search box only knows the films already in this"
        echo "library, so a film nobody here has added cannot be found."
        ask "REELTALK_TMDB_API_KEY (blank to skip)" ""
        stage REELTALK_TMDB_API_KEY "$ANSWER"
    fi

    # 8. Federation peers on the local network.
    if needs FEDERATION_ALLOWED_NETWORKS; then
        echo
        echo "Every outbound fetch refuses loopback, private, and cloud metadata"
        echo "addresses. That is the guard against this box being used to probe its"
        echo "own network, so a peer that genuinely shares this network is"
        echo "unreachable until its CIDR is named here. Naming a LAN range does not"
        echo "reopen the metadata addresses."
        while :; do
            ask "FEDERATION_ALLOWED_NETWORKS (e.g. 192.168.1.0/24, blank for none)" ""
            if [ -z "$ANSWER" ] || is_net_list "$ANSWER"; then
                stage FEDERATION_ALLOWED_NETWORKS "$ANSWER"
                break
            fi
            echo "  A comma-separated list of CIDRs, like 192.168.1.0/24."
        done
    fi

    # 9. Time zone.
    if needs TIME_ZONE; then
        echo
        echo "TIME_ZONE only decides how timestamps are displayed. They are stored"
        echo "in UTC whatever you answer."
        ask "TIME_ZONE" UTC
        stage TIME_ZONE "$ANSWER"
    fi

    # 10. Confirm, then write.
    echo
    echo "About to write into $ENV_FILE:"
    if [ ${#PENDING_KEYS[@]} -gt 0 ]; then
        for note in "${PENDING_NOTES[@]}"; do
            echo "  $note"
        done
    fi
    if [ "$secrets_pending" = 1 ]; then
        echo "  the secrets that are still blank (SECRET_KEY, POSTGRES_PASSWORD,"
        echo "  and POSTGRES_APP_PASSWORD), generated, never asked"
    fi
    if [ ${#PENDING_KEYS[@]} -eq 0 ] && [ "$secrets_pending" = 0 ]; then
        echo "  nothing: every value this script asks about is already set."
        echo
        echo "To change one, edit $ENV_FILE (blank a line to be asked again) and"
        echo "re-run. Nothing to do now."
        exit 0
    fi
    echo "Nothing already set in the file is touched."
    echo
    ask_yes "Write that now?" y
    echo
    if [ "$ANSWER" = no ]; then
        echo "Nothing written."
        exit 0
    fi
fi

for i in "${!PENDING_KEYS[@]}"; do
    set_var "${PENDING_KEYS[$i]}" "${PENDING_VALUES[$i]}"
done

# DOMAIN has to be real whether or not anyone was here to be asked. Checked
# against the placeholder and not just against emptiness: with no TTY the prompt
# above never ran, so the placeholder survives as a non-empty value and a
# scripted install would come up minted on a domain nobody owns.
domain="$(get_var DOMAIN)"
if [ -z "$domain" ] || [ "$domain" = "$PLACEHOLDER_DOMAIN" ]; then
    echo "DOMAIN is required and $ENV_FILE still has the placeholder from" >&2
    echo "$TEMPLATE. Set it in $ENV_FILE and re-run, or run this on a terminal" >&2
    echo "and answer the prompt." >&2
    exit 1
fi

# Secrets: generated, never asked.
for var in SECRET_KEY POSTGRES_PASSWORD; do
    if [ -z "$(get_var "$var")" ]; then
        echo "Generating $var..."
        set_var "$var" "$(gen_secret)"
    else
        echo "$var already set, leaving it alone."
    fi
done

# The application's own database login. Only fill its password when the login is
# actually named: an install that predates the split has no POSTGRES_APP_USER
# line at all, and inventing a password for a role that does not exist would
# point the app at a login that cannot authenticate. Those installs opt in with
# db/upgrade-role-split.sh instead.
if [ -n "$(get_var POSTGRES_APP_USER)" ] && [ -z "$(get_var POSTGRES_APP_PASSWORD)" ]; then
    echo "Generating POSTGRES_APP_PASSWORD..."
    set_var POSTGRES_APP_PASSWORD "$(gen_secret)"
fi

echo
# Enable the versioned git hooks (blocks co-author trailers, which GitHub would
# otherwise turn into contributor-graph entries for accounts we do not control).
# Best effort: a non-git checkout just skips it.
if git rev-parse --show-toplevel >/dev/null 2>&1 && [ -d .githooks ]; then
    bash .githooks/install.sh
else
    echo "(not a git checkout -- skipping attribution guard; run 'bash .githooks/install.sh' once cloned)"
fi

echo
if [ ${#WRITTEN[@]} -gt 0 ]; then
    echo "Wrote: ${WRITTEN[*]}"
    echo
fi

echo "Start the stack one of two ways:"
echo
echo "  Build the image yourself:"
echo "    docker compose up -d --build"
echo
echo "  Or pull the prebuilt image from Docker Hub (no build):"
echo "    docker compose -f docker-compose.prebuilt.yml up -d"
echo
# get_var prints nothing for a line that is still commented, so the fallback has
# to be a default rather than a ||, which would never fire on empty output.
published_port="$(get_var WEB_PORT)"
published_port="${published_port:-3030}"
echo "  Then: curl http://localhost:${published_port}"
echo
echo "To change an answer later, edit $ENV_FILE and run ./setup.sh again. A value"
echo "that is set is never overwritten, so blank the line to be asked again or, for"
echo "a secret, to have a fresh one generated. Rotating a secret logs every session"
echo "out and invalidates every outstanding verification and reset link."
echo
echo "POSTGRES_PASSWORD is a first-boot seed only: the db container rotates it to a"
echo "value recorded nowhere, deliberately. Maintenance goes over the unix socket"
echo "inside that container: docker compose exec db psql -U <bootstrap user>."
echo
echo "The site is plain HTTP; terminate TLS with your own reverse proxy."
echo "A brand-new instance redirects to /setup/ to create the first admin."
echo "See the Deployment section of README.md for both paths, the optional"
echo "settings, SMTP, and upgrades."
