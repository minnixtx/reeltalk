# Deploying ReelTalk

Operational guide for running a public ReelTalk instance. Written against the
`reeltalk.minnix.dev` target and verified on the real stack — the numbers and
behaviours below were measured, not assumed.

Read [PLAN.md](PLAN.md) §3.8/§3.9 for the architecture and the decisions log in
[PROGRESS.md](PROGRESS.md) §4 (R74/R75/R76 in particular) for *why* the proxy
trust and cookie rules are shaped the way they are.

---

## 1. Topology

TLS is terminated **outside** the app (decision D14). The app serves plain HTTP
and must be told, by a proxy it trusts, what the original scheme was.

```
browser ──https──▶ Cloudflare (DNS only, grey cloud)
                      │
                      ▼
              nginx proxy manager        192.168.1.141
              (TLS terminates here, LE wildcard cert)
                      │  http, X-Forwarded-Proto: https, original Host
                      ▼
              ReelTalk web container    192.168.1.138:3030
              (uvicorn + whitenoise, no TLS)
```

The stack is four compose services: `web`, `db`, `worker`, `backup`. All four
build from the same Dockerfile but are **separate image tags** — see §7.

---

## 2. Prerequisites

| Requirement | This deployment |
|---|---|
| DNS | `reeltalk.minnix.dev` → CNAME → `minnix.dev` → public IP. Cloudflare **DNS-only** (no orange-cloud proxy). |
| TLS certificate | Held by nginx proxy manager, `CN=*.minnix.dev`, Let's Encrypt `YE2`, valid to **2026-11-01**. **Check before every deploy** — an expired cert silently breaks federation. |
| Terminator address | `192.168.1.141` — measured, over the real public path, to be the address the app actually sees. |
| App box | `192.168.1.138`, web container published on `:3030`. |

Do **not** put Cloudflare's edge in the TLS path for this design. If you ever turn
on the Cloudflare proxy, the peer address changes and `TRUSTED_PROXIES` must be
rewritten to the Cloudflare IP ranges, or the forwarded scheme stops matching and
you publish `http://` actor IDs over real HTTPS.

---

## 3. Environment variables

Copy `.env.example` to `.env`. Values marked **required** have no usable default
for a public instance.

### Identity

| Variable | Value here | Notes |
|---|---|---|
| `DOMAIN` | `reeltalk.minnix.dev` | **Required.** The canonical public origin, no scheme. May carry a port for IP:port instances. Drives `ALLOWED_HOSTS` default and `DEFAULT_FROM_EMAIL`. |
| `SECRET_KEY` | generated | **Required.** `./setup.sh` generates it. |
| `POSTGRES_PASSWORD` | generated | **Required.** |

### Hosts and origins

`ALLOWED_HOSTS` defaults to `[DOMAIN_HOST, "localhost"]` — which means that once
`DOMAIN` is the public domain, **plain-HTTP LAN access 400s** unless you name the
LAN address explicitly. This instance keeps LAN HTTP working permanently, so both
origins must be listed:

| Variable | Value here |
|---|---|
| `ALLOWED_HOSTS` | `reeltalk.minnix.dev,localhost,127.0.0.1,192.168.1.138` |
| `CSRF_TRUSTED_ORIGINS` | `https://reeltalk.minnix.dev,http://192.168.1.138:3030` |

`CSRF_TRUSTED_ORIGINS` needs the **scheme included** and both origins: form POSTs
from the public https origin and from the LAN http origin are different origins to
Django's CSRF check.

### Proxy trust (the part that decides your published identity)

| Variable | Value here | Notes |
|---|---|---|
| `TRUSTED_PROXIES` | `192.168.1.141/32` | Comma-separated IPs/CIDRs. The only peers whose `X-Forwarded-Proto` is believed. **Empty means "no proxy":** the forwarded scheme is ignored entirely and `SECURE_PROXY_SSL_HEADER` is never set — a safe default, not a misconfiguration. |
| `SECURE_COOKIES` | `true` (default) | Drives `SESSION_COOKIE_SECURE` + `CSRF_COOKIE_SECURE`. |
| `COOKIES_FOLLOW_SCHEME` | `true` (default) — **leave it set** | The flag follows the request's actual scheme per-request. This is what lets one instance serve both public https and plain-HTTP LAN. Setting `false` makes `Secure` unconditional, and then **`csrftoken` is never returned over `http://` and LAN login 403s.** Only set `false` if you are deliberately giving up plain-HTTP LAN access. |

### Application

| Variable | Value here | Notes |
|---|---|---|
| `REELTALK_TMDB_API_KEY` | set | Optional. Without it, film search is local-library only. Shared quota — see §12. |
| `EMAIL_*` | unset | Optional. Console backend when unset, so no mail is actually sent. |
| `WEB_PORT` | `3030` | Host port published for the cleartext endpoint. See §5. |
| `BACKUP_TIME` / `BACKUP_RETENTION` / `BACKUP_DIR` | `03:17` / `7` / `/app/backups` | See §8. |

---

## 4. Reverse-proxy requirements (nginx proxy manager)

The proxy **must** forward:

1. **`X-Forwarded-Proto: https`** on TLS requests. Without it the app reports the
   real transport (plain http) and publishes `http://` actor IDs, inbox URLs and
   poster URLs over a secure origin — broken federation that looks fine in a
   browser.
2. **The original `Host` header.** Do not rewrite it. When no `Host` header is
   present Django rebuilds the host from `SERVER_NAME`/`SERVER_PORT` and appends
   the port for anything that isn't 443-on-https, which is how `:3030` leaks into
   published actor IDs.

NPM's defaults do both. Verify rather than trust — see §11.

Also required on the proxy host:

- HTTP/2 pass-through is fine; nothing here needs WebSockets.
- Proxy read timeout ≥ 60s (large imports POST a whole CSV).
- Body size limit ≥ your largest import file. NPM's default 10 MB is enough for a
  ~1,670-row TMDB export; raise it if you import larger.

---

## 5. Port binding and the firewall

`docker-compose.yml` publishes `${WEB_PORT:-3030}:3030` with no bind address, so
it binds `0.0.0.0` — all interfaces.

**That port speaks cleartext HTTP and knows nothing about who is allowed in.** The
firewall is the only thing keeping it private:

- Nothing on the public internet should be able to reach `:3030`.
- Allow it only from the TLS terminator (`192.168.1.141`) and the LAN hosts you
  actually browse from.
- **The forwarded-scheme gate is not an access control.**
  `TrustedProxySchemeMiddleware` decides whom to *believe* about the scheme; it
  never decides whom to *let in*. A publicly reachable `WEB_PORT` lets anyone open
  a plain-HTTP session that skips your certificate entirely, and the gate will
  neither help them nor stop them.

Measured on this box: `3030` is closed/filtered from the public IP while LAN
hairpin works. Re-verify after any firewall change.

---

## 6. First deploy (fresh instance)

The cutover uses a **fresh database**. Do not try to migrate the old IP-keyed
rows: they carry identities derived from `192.168.1.138:3030`, and re-importing
through the tested TMDB CSV path is cleaner than rewriting them.

```bash
# 1. Configure
cp .env.example .env
./setup.sh                      # generates SECRET_KEY + POSTGRES_PASSWORD
# then set DOMAIN, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, TRUSTED_PROXIES per §3

# 2. Build EVERY service that runs app code (see §7)
docker compose build

# 3. Bring up the stack
docker compose up -d

# 4. First-run wizard: browse to https://<domain>/ and create the admin account.
#    Until a superuser exists, / and /signup/ redirect to /setup/.

# 5. Import the watchlist through the app's Import surface.

# 6. Set the signup policy (§10).

# 7. Verify (§11).
```

`entrypoint.sh` applies migrations and collects static files only when the
service starts `uvicorn`, so `web` must come up before `worker`.

---

## 7. Build gotcha: every service needs its image

`web`, `worker` and `backup` each get their **own** image tag (`reeltalk-web`,
`reeltalk-worker`, `reeltalk-backup`). Building `web` alone leaves the others on
the old code, and the failure is confusing:

```
$ docker compose run --rm backup python manage.py restore_database ...
Unknown command: 'restore_database'
```

Because the image bakes the source with **no bind mount**, a tree edit is
invisible to a container until that service's image is rebuilt. Use
`docker compose build` (all services) before a deploy, not `build web`.

---

## 8. Backups

The `backup` service runs `backup-loop.sh`: a plain sleep loop that wakes daily at
`BACKUP_TIME` (default `03:17` in `TIME_ZONE`), runs `python manage.py
backup_database`, and on failure logs a warning and retries the next day rather
than exiting. It is deliberately **not** a Django-Q2 scheduled task, so backups
happen regardless of web/worker/queue health.

- Format: `pg_dump --format=custom` (gzip-compressed, `pg_restore`-able).
- Names: `<db>-YYYYMMDDTHHMMSSZ.dump`, zero-padded UTC, so lexicographic order
  **is** chronological order and retention prunes by name.
- Retention: newest `BACKUP_RETENTION` (default 7) per database.
- Location: the `backups` named volume, mounted at `/app/backups`.
- The password goes through `PGPASSWORD`, never argv — nothing sensitive appears
  in the process list or in the command's own echoed output.

Manual run:

```bash
docker compose run --rm backup python manage.py backup_database
```

**A backup you have never restored is a hope, not a backup.** Drill the restore
(§9) on a schedule you would actually be comfortable relying on.

---

## 9. Restoring a backup

`restore_database` is the counterpart to `backup_database`. Because the two
operations are not symmetric — dumping the live DB is harmless, restoring over it
destroys the owner's data — the target is **never defaulted**.

```bash
# Restore into an explicitly named database (must already exist):
docker compose run --rm backup python manage.py restore_database \
    /app/backups/reeltalk-20260921T134126Z.dump --into reeltalk_drill
```

| Flag | Meaning |
|---|---|
| `dump` (positional) | Path to the custom-format `.dump`. Must exist, or the command refuses before touching a database. |
| `--into <db>` | **Required, no default.** The command will not guess that you meant the app's own database. The target must already exist — it never creates one, so a typo is a loud connection failure, not a fresh empty database nobody is looking at. |
| `--force` | Required *in addition to* naming the live database. Without it, `--into reeltalk` is refused. |
| `--clean` | `pg_restore --clean --if-exists` — drop and recreate objects in a target that already holds a schema. Leave off for an empty target. |

Under the hood: `--exit-on-error` (fail loudly, not pg_restore's default of
carrying on), `--single-transaction` (all-or-nothing, so a mid-way failure leaves
the target empty rather than half-loaded), `--no-owner` (drops the dump's
`ALTER OWNER` so a restore under a different role works).

### Drilling the restore without risking live data

The drill must **never** restore into the live database. Throwaway target only.
This was run end to end on the real stack:

```bash
# 1. Fresh dump through the real path
docker compose run --rm backup python manage.py backup_database

# 2. Throwaway database in the same cluster
docker compose exec db psql -U reeltalk -d postgres -c 'CREATE DATABASE reeltalk_drill'

# 3. Restore into it
docker compose run --rm backup python manage.py restore_database \
    /app/backups/<newest>.dump --into reeltalk_drill

# 4. Compare per-table row counts against live
for db in reeltalk reeltalk_drill; do
  docker compose exec -T db psql -U reeltalk -d $db -At \
    -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename" \
    | while read -r t; do printf 'SELECT %s AS tbl, count(*) FROM public."%s" UNION ALL\n' "'$t'" "$t"; done \
    | sed '$ s/ UNION ALL$//' \
    | docker compose exec -T db psql -U reeltalk -d $db -At -f /dev/stdin
done

# 5. Boot the app against the restored database and probe it
docker compose run -d --rm -p 3132:3030 -e POSTGRES_DB=reeltalk_drill \
    web uvicorn reeltalk.asgi:application --host 0.0.0.0 --port 3030
curl -s http://127.0.0.1:3132/ -o /dev/null -w '%{http_code}\n'

# 6. Tear down, then confirm live is untouched
docker stop <container>
docker compose exec db psql -U reeltalk -d postgres -c 'DROP DATABASE reeltalk_drill'
```

Result of the drill on 2026-09-21: **24 public tables, row counts identical to
live**, the app booted against the restored DB reporting `No migrations to apply`
(the dump carries a current schema), and it served restored content over HTTP.
After teardown the cluster held only `postgres`/`reeltalk`/`template0`/
`template1`, and live row counts diffed clean against the pre-drill baseline.

The same round trip runs in the test suite (`reeltalk/tests/test_restore.py`), so
the restore path stays proven rather than merely documented.

---

## 10. Invite-only signup and invite links

Two settings in Django admin → **Site settings**, and they are independent:
`signup_policy` decides whether a link is *needed*; `invite_scope` decides who
may *make* one.

### `signup_policy` = Invite-only

- `/signup/` renders "not accepting new signups right now" and a `POST` to it
  returns **200** with that same closed page and no account created. (Not 403 —
  the gate is the view declining, not the CSRF layer.)
- All four signup calls-to-action (header, home landing, Getting Started, login
  page) are **hidden**, not redirected onto the closed notice. `Log in` is
  untouched everywhere.
- nodeinfo reports `openRegistration: false`.

### `invite_scope`

| Value | Who sees the INVITE button |
|---|---|
| **Admins only** (default) | `is_staff` accounts |
| **All members** | every signed-in user |

Anonymous can never mint under either value. The check lives on
`SiteSettings.may_send_invites()`, not only behind the `login_required`
decorator, so a caller that forgets the decorator still cannot open the door.

### The link flow

A member opens their own profile and clicks **Invite** ("Invite a fellow movie
freak to ReelTalk"). That `POST`s to `/invite/create/`, which mints a code and
redirects back to the profile carrying it: a read-only field with a **Copy link**
button, and a line saying what state the link is in (live / used by whom /
expired). The invitee opens `https://<domain>/invite/<code>/` and gets the
normal signup page, prefaced with who invited them.

Properties worth knowing before you rely on one of these:

- **One account per link.** The liveness check and the mark-as-used happen under
  a single `select_for_update` row lock inside the signup transaction. Checked-
  then-marked as two statements is true until two strangers open the same link in
  the same second — on an invite-only instance that is an extra account the
  owner never approved.
- **It expires in 7 days** whether or not anyone claims it
  (`INVITE_TTL_DAYS` in `reeltalk/social/models.py`).
- **The code is a credential**: 32 characters from `secrets.token_urlsafe`, not
  a counter or a guessable word. The route's charset matches that alphabet, so a
  malformed code 404s at the URL rather than reaching a lookup.
- **Spent stays spent.** Deleting the account a link created leaves the invite
  used — the link had already been handed around once.
- **A valid link works whatever `signup_policy` says.** The link is a stronger
  statement than the setting; making it depend on the setting would mean flipping
  signup open silently invalidated every link already sent.
- **Every mint is a new code.** Nothing is reused, so a leaked link costs one
  seat, not the whole feature.

The mint is `POST`-only because it is not idempotent — a `GET` that mints would
let a page render or a browser prefetch spend someone's invite.

### Creating accounts directly

The admin can still just make an account, and under `invite_scope = admins`
that is the same thing as sending an invite, minus the link.

Django admin → **Users** → **Add user**. The form takes `localname`, display
name, email, and a password + confirmation; it hashes through `set_password`
and enforces the same identity rules as signup (1–30 chars of
`[a-zA-Z0-9._-]` starting with a letter or digit, no case-insensitive
duplicate). New accounts are not staff and not superusers.

Deliberately not editable from the admin:

- **`private_key`** is on no form at all. Nothing in the admin needs to read a
  signing key, and putting one on screen invites a copy into a screenshot or a
  support ticket.
- **`localname` is read-only after creation.** It *is* the federated identity —
  renaming an account orphans every URL already published for it and invalidates
  the mirrors other instances hold.
- **`raw_summary` / `summary`** are read-only because `summary` is rendered from
  markdown by the profile-edit view, not by `save()`. Editing the raw source in
  admin would leave the two permanently out of step.
- The follow/block relations are app-managed and not exposed here.

### The invite ledger

Django admin → **Invites** is read-only: who minted it, when, whether it is
live, and who joined on it. There is no **Add** — a link minted here has no
inviter to credit, and an admin who just wants an account has the path above.
The list shows the code truncated; the full code appears only on the detail page,
for the same screenshot reason as `private_key`. Deleting a row is left
available — that is how you clear an outstanding invite for good.

### CLI equivalents, if you prefer

```bash
# create an account directly
docker compose run --rm web python manage.py shell -c \
  "from reeltalk.social.models import User; \
   User.objects.create_user('friend', password='their password', \
                          email='', display_name='Their Name')"

# mint an invite link from the command line
docker compose run --rm web python manage.py shell -c \
  "from reeltalk.social.models import User, Invite; \
   print(Invite.mint(User.objects.get(localname='minnix')).code)"
```

`createsuperuser` and the first-run wizard are both fine — they go through
`set_password` too.

---

## 11. Post-deploy verification

Run these after the domain goes live. The first is the check that everything else
in §3 and §4 was actually configured right.

**1. The published actor ID is https, with no `:3030`.**

```bash
curl -s -H 'Accept: application/activity+json' \
  'https://reeltalk.minnix.dev/.well-known/webfinger?resource=acct:admin@reeltalk.minnix.dev'
# follow the "self" link and confirm:
#   "id": "https://reeltalk.minnix.dev/user/<localname>/"
#   "inbox": "https://reeltalk.minnix.dev/user/<localname>/inbox/"
#   "sharedInbox": "https://reeltalk.minnix.dev/inbox/"
```

This is the **only** real proof that NPM forwards `X-Forwarded-Proto: https`. It
cannot be verified before the domain is admitted at `ALLOWED_HOSTS`, because the
`DisallowedHost` 400 fires before anything scheme-dependent renders. A `http://`
or a `:3030` here means the header is not arriving or the trust list does not
match — fix it before federating, because every URL you publish is wrong until you
do.

**2. A spoofed header from an untrusted peer does nothing.**

```bash
# From the LAN (NOT the terminator), the scheme must stay http:
curl -s -H 'Accept: application/activity+json' -H 'X-Forwarded-Proto: https' \
  http://192.168.1.138:3030/user/<localname>/ | grep '"id"'
# expect http://192.168.1.138:3030/... — the header is inert
```

**3. LAN plain-HTTP login still works.** Load `http://192.168.1.138:3030/login/`,
log in, confirm the redirect and the signed-in page. If `csrftoken` never appears
in the response cookies, `COOKIES_FOLLOW_SCHEME` has been turned off.

**4. Signup CTAs are gone** while the policy is `invite` — header, home,
`/welcome/`, `/login/`.

**5. `3030` is still closed from outside** the LAN.

---

## 12. Known limits at this point

Named rather than hidden, so nobody discovers them in production:

- **Film pages and genre subfeeds are readable by anyone who has the URL.** The
  home rail shows strangers the trending titles and popular genres as plain
  text and links nothing out (R81), but `/film/<id>/` and `/genre/<slug>/`
  themselves carry no login gate. Removing the link removes the affordance,
  not the access. This cannot be closed with a one-line `@login_required`:
  `film_detail` serves the Film wire document to ActivityPub clients by
  content negotiation so a remote instance can resolve a referenced film by
  its id URL, and that fetch is anonymous. Gating it means gating only the
  HTML branch and leaving the AP branch open.
- **No login brute-force throttling.**
- **No HSTS preload.** Worth adding once the domain is stable.
- **Invite-only has no invite mechanism** — no codes, no expiry, no email invites.
  It is a closed door plus manual account creation.
- **Plain-HTTP LAN sessions are cleartext by design.** A session started on the
  LAN has a cookie readable by anyone on that network path. This is the owner's
  explicit, standing choice, not an oversight — see R74.
- The PLAN.md §5 feature list (notifications, directory, lists, groups, RSS,
  moderation, 2FA/email verification) is unbuilt.
