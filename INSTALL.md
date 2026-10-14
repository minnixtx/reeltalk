# Installing ReelTalk

ReelTalk ships as a single Docker image that runs three roles — web, worker and
daily backup — plus PostgreSQL. There are two ways to install it and they differ
only in whether **you** build the image or **we** give you a prebuilt one:

| | Build from source | Prebuilt image |
|---|---|---|
| Get the code | `git clone` | `git clone` |
| Image | built on your machine | pulled from Docker Hub |
| Compose file | `docker-compose.yml` | `docker-compose.prebuilt.yml` |
| Needs compilers | no (built in a container) | no |
| Command | `docker compose up -d --build` | `docker compose -f docker-compose.prebuilt.yml up -d` |

Both paths clone the same repo, because the Compose file, `.env.example` and
`setup.sh` all live in it. Neither path needs you to install Python, and neither
needs a package manager on your host.

---

## 1. Prerequisites

- **Docker** with the Compose plugin (`docker compose version`).
- **A domain name** pointing at your server. ReelTalk federates, so its identity
  is minted from your domain — a LAN IP works for testing but nothing outside
  your network will ever recognise it.
- **A TLS terminator you already run.** ReelTalk serves plain HTTP and does not
  terminate TLS itself. Put your own reverse proxy (Caddy, nginx, nginx proxy
  manager, Traefik) in front and point it at the web container.
- **An SMTP server** if you want account verification and password resets to
  reach people. Without one the mail backend prints to stdout and **nobody can
  verify their address**, which means nobody can sign up. This is not a
  cosmetic gap — see §4.

---

## 2. Configure

```bash
git clone https://github.com/minnixtx/reeltalk.git
cd reeltalk
cp .env.example .env
./setup.sh
```

`setup.sh` prompts for `DOMAIN` and generates `SECRET_KEY` and
`POSTGRES_PASSWORD` if they are empty. It is safe to re-run: values that are
already set are never overwritten.

### Required

| Key | Notes |
|---|---|
| `DOMAIN` | Your public hostname, no scheme. Drives `ALLOWED_HOSTS` and the default mail domain. |
| `SECRET_KEY` | Django secret. `setup.sh` generates one. |
| `POSTGRES_PASSWORD` | Database password. `setup.sh` generates one. |

`POSTGRES_USER`, `POSTGRES_DB` and `POSTGRES_HOST=db` have working defaults.

### Optional but usually wanted

| Key | Notes |
|---|---|
| `REELTALK_TMDB_API_KEY` | Free key from themoviedb.org. Without it, film search falls back to your local library only. |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` / `EMAIL_USE_TLS` | Real mail. See §4 for the sender rule that bites. |
| `EMAIL_SENDER_NAME` / `EMAIL_SENDER_DOMAIN` | The From address. Defaults to `admin@DOMAIN`. |
| `CANONICAL_ORIGIN` | The origin every ActivityPub identity is minted from. Defaults to `https://DOMAIN`. Set it only if that is wrong for you. |
| `WEB_PORT` | Host port the web container publishes. Default `3030`. |
| `CSRF_TRUSTED_ORIGINS` | Needed if you reach the instance by a hostname other than `DOMAIN` (a LAN IP, a second domain). |
| `TRUSTED_PROXIES` | Which peers may tell us a request arrived over `https`. Defaults to the private ranges, so a proxy on your LAN or the Compose network needs no value at all. |
| `BACKUP_TIME` / `BACKUP_RETENTION` | Daily dump schedule. Default `03:17`, keep 7. |

`DEBUG` stays off. Do not turn it on for a network-reachable instance.

### Two things that will bite you, both verified on a real install

**Reaching the instance by IP address.** `ALLOWED_HOSTS` defaults to your
`DOMAIN` plus localhost. Browse to a raw IP and every request returns **HTTP
400**. To use an IP or an extra hostname you must name it explicitly:

```
ALLOWED_HOSTS=192.168.1.50,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=http://192.168.1.50:3030
```

Without `CSRF_TRUSTED_ORIGINS` the pages render but every form POST rejects.

**The emailed verification link uses `https://DOMAIN`.** `CANONICAL_ORIGIN`
defaults to `https://DOMAIN`, so until TLS is actually working the link in the
email points at an https URL that does not answer. Finish your proxy setup
**before** you create the first account, or read the link out of the worker log
as described in §4.

---

## 3. Start

**Build from source:**

```bash
docker compose up -d --build
```

**Prebuilt image:**

```bash
docker compose -f docker-compose.prebuilt.yml up -d
```

The prebuilt file pins every app role to the *same* tag
(`minnixtx/reeltalk:${REELTALK_VERSION:-v0.1.0}`) and contains no `build:`
key anywhere, which is what makes it pull-only. It uses the same project name
and the same named volumes as `docker-compose.yml`, so moving between the two
paths reuses your data rather than orphaning it.

Wait for health:

```bash
docker compose -f docker-compose.prebuilt.yml ps   # web should read (healthy)
```

Then point your reverse proxy at `localhost:3030` (or whatever `WEB_PORT` you
set) and make sure it forwards `X-Forwarded-Proto: https` and the original
`Host` header.

---

## 4. First run: create the admin

Browse to `https://your-domain/`. A brand-new instance redirects every page to
**`/setup/`** — that is expected. The form there creates the first account,
which becomes the instance admin.

**That account cannot sign in until its email is verified**, and verification
mail only arrives if you configured SMTP. If you did not, the console backend
prints the message to the worker log and you can read the link there:

```bash
docker compose -f docker-compose.prebuilt.yml logs worker | grep -A3 "Click the link"
```

Copy the `/account/verify/<token>/` URL and open it in your browser.

**The sender rule.** Many mail servers reject a message whose envelope sender is
not the account you authenticated as. ReelTalk builds From from
`EMAIL_SENDER_NAME@EMAIL_SENDER_DOMAIN`, so those two must agree with
`EMAIL_HOST_USER`. A mismatch does **not** fail at startup — it fails per-send
inside the worker, so it looks like a broken feature rather than a bad config.
Check the pair before you create the account:

```bash
grep -E 'EMAIL_HOST_USER|EMAIL_SENDER_NAME|EMAIL_SENDER_DOMAIN' .env
```

---

## 5. Signup policy

Set in the database, not in `.env`. Admin → **Site settings**:

- `signup_policy` — `open` (default) or `invite` (invite-only).
- `invite_scope` — who may mint invite links: `admins` (default) or `all`.

New instances start **open**. Change it in the admin before you point the
domain at anything public if you want a closed community.

---

## 6. Volumes

Named volumes, created automatically:

| Volume | Holds |
|---|---|
| `pgdata` | The database. The only thing you cannot rebuild. |
| `static_volume` | Collected static assets. Regenerated on start. |
| `media_volume` | Uploaded images and posters fetched from TMDB. |
| `backups_volume` | Daily database dumps. |

Both compose files declare the same four, so the volumes follow you across
either install path.

---

## 7. Upgrading

**Prebuilt.** Pin the version in `.env` and re-up. Change one line, roll it
back by changing it again:

```
REELTALK_VERSION=v0.2.0
```

```bash
docker compose -f docker-compose.prebuilt.yml pull
docker compose -f docker-compose.prebuilt.yml up -d
```

Migrations run automatically when the web container starts. Read the release
notes first — if a release adds a migration you want the instance quiet while
it applies.

Avoid floating everything on `latest`: it can move underneath you mid-incident
and you lose the ability to say which version you are running.

**From source.**

```bash
git pull
docker compose build
docker compose up -d
```

Build **all** services, not just `web`. The worker and backup roles run from
their own image tags built from the same Dockerfile, and a half-rebuilt stack
is how you end up with two code versions against one database.

---

## 8. Backups and restore

The `backup` service dumps the database once a day at `BACKUP_TIME` into the
`backups_volume`, keeping the newest `BACKUP_RETENTION`.

```bash
# On-demand dump
docker compose -f docker-compose.prebuilt.yml run --rm backup python manage.py backup_database
```

Restore names its target explicitly and never defaults it, because dumping the
live database is harmless and restoring over it is not:

```bash
# Target database must already exist
docker compose exec db psql -U reeltalk -d postgres -c 'CREATE DATABASE reeltalk_restore'

docker compose -f docker-compose.prebuilt.yml run --rm backup \
  python manage.py restore_database /app/backups/<dump>.dump --into reeltalk_restore
```

| Flag | Meaning |
|---|---|
| `dump` | Path to the custom-format dump. Refuses if it does not exist. |
| `--into <db>` | Required, no default. Never creates a database, so a typo fails loudly instead of silently making an empty one. |
| `--force` | Additionally required to restore over the live database. |
| `--clean` | Drop and recreate objects in a target that already has a schema. |

Backups live in a Docker volume. If you back up the host but not that volume,
you have no backups — copy them off the machine.

---

## 9. Image tags

Published on Docker Hub as `minnixtx/reeltalk`:

- `0.1.0` — exact version. **Pin this.**
- `0.1` — the minor line.
- `0` — the major line.
- `latest` — newest stable release. Prereleases publish their own tag and do
  **not** move `latest`.

---

## 10. Troubleshooting

| Symptom | Cause |
|---|---|
| Every request returns **400** | Hostname or IP not in `ALLOWED_HOSTS`. |
| Pages render but forms **403** | Origin missing from `CSRF_TRUSTED_ORIGINS`. |
| Verification email never arrives | No SMTP configured — check `docker compose logs worker`. |
| Mail rejected with "Sender is not same as SMTP authenticate username" | From address ≠ `EMAIL_HOST_USER`. Set `EMAIL_SENDER_NAME`/`EMAIL_SENDER_DOMAIN` to match. |
| Verification link is `https://` and nothing answers there | TLS not set up yet. Finish the proxy, or read the link from the worker log. |
| `failed to resolve reference ... not found` | The pinned `REELTALK_VERSION` tag does not exist on Docker Hub. |
| Worker cannot reach posters | `media_volume` must be shared with web — both files already do this. |

---

## 11. Notes for people packaging this

The published image currently includes the Python dev toolchain (`pytest`,
`ruff`, `mypy`, ~26MB) because the project's test gate runs inside the baked
image. It is dead weight at runtime but harmless; a separate slim runtime stage
is planned.

The image is built multi-arch (`linux/amd64`, `linux/arm64`) from one
Dockerfile. Every dependency resolves to a native wheel on both, so no
architecture compiles from source.
