# ReelTalk
A federated social network for tracking, reviewing, and discovering films. Free yourself from corporate social media and join a close community of film fans who value being free from spyware and data collection by gathering on a free and open source platform.

## Deployment

ReelTalk ships as one Docker image running three roles (web, worker and a
daily backup) plus PostgreSQL. There are two install paths and they differ
only in whether you build the image or pull a prebuilt one:

| | Build from source | Prebuilt image |
|---|---|---|
| Get the code | `git clone` | `git clone` |
| The image | built on your machine | pulled from Docker Hub |
| Compose file | `docker-compose.yml` | `docker-compose.prebuilt.yml` |
| Command | `docker compose up -d --build` | `docker compose -f docker-compose.prebuilt.yml up -d` |

Both paths clone this repo, because the Compose files, `.env.example` and
`setup.sh` all live in it. Neither needs Python or a package manager on your
host.

This section is the source of truth for installing and upgrading.

### What you need first

- **Docker with the Compose plugin** (`docker compose version`).
- **A domain name** pointing at your server. ReelTalk federates, so its
  identity is minted from your domain. A LAN IP works for local testing, but
  nothing outside your network will ever recognise it.
- **A TLS terminator you already run.** ReelTalk serves plain HTTP and does
  not terminate TLS itself. Put your own reverse proxy (Caddy, nginx, nginx
  proxy manager, Traefik) in front and point it at the web container.
- **An SMTP server**, if you want verification and password reset mail to
  reach anyone. Without it the mail backend prints to stdout and nobody can
  verify an address, which means nobody can sign up or sign in. See step 4.

### 1. Configure

```bash
git clone https://github.com/minnixtx/reeltalk.git
cd reeltalk
cp .env.example .env
./setup.sh
```

`setup.sh` prompts for `DOMAIN` and generates `SECRET_KEY` and
`POSTGRES_PASSWORD` if they are empty. It is safe to re-run: values already
set are never overwritten.

### 2. The .env parameters

Required:

| Key | Notes |
|---|---|
| `DOMAIN` | Your public hostname, no scheme. Drives `ALLOWED_HOSTS` and the default mail domain. |
| `SECRET_KEY` | Django secret. `setup.sh` generates one. |
| `POSTGRES_PASSWORD` | Database password. `setup.sh` generates one. |

`POSTGRES_USER`, `POSTGRES_DB` and `POSTGRES_HOST=db` have working defaults.

Optional, and most installs want some of them:

| Key | Default | Notes |
|---|---|---|
| `REELTALK_TMDB_API_KEY` | unset | Free key from themoviedb.org. Without it, film search falls back to the local library only. |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | unset | Real mail. See step 4 for the sender rule that bites. |
| `EMAIL_SENDER_NAME` | `admin` | Local part of the From address. |
| `EMAIL_SENDER_DOMAIN` | `DOMAIN` | Domain part of the From address. |
| `CANONICAL_ORIGIN` | `https://DOMAIN` | The origin every ActivityPub identity is minted from. Set it only if that is wrong for you, and never to a LAN address. |
| `WEB_PORT` | `3030` | Host port the web container publishes. |
| `CSRF_TRUSTED_ORIGINS` | unset | Needed if you reach the instance by a hostname other than `DOMAIN`. |
| `TRUSTED_PROXIES` | the private ranges | Which peers may tell us a request arrived over https, and which hops to skip when reading the client address. A proxy on your LAN or on the Compose network needs no value at all. |
| `BACKUP_TIME` / `BACKUP_RETENTION` | `03:17` / `7` | Daily dump schedule. |
| `ALLOWED_HOSTS` | `DOMAIN` plus localhost | Add extra hostnames here. |

Leave `DEBUG` off. Do not turn it on for anything reachable over a network.

Two things that will bite you, both verified on a real install:

**Reaching the instance by IP address.** `ALLOWED_HOSTS` defaults to your
`DOMAIN` plus localhost. Browse to a raw IP and every request returns HTTP
400. To use an IP or an extra hostname you must name it:

```
ALLOWED_HOSTS=192.168.1.50,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=http://192.168.1.50:3030
```

Without `CSRF_TRUSTED_ORIGINS` the pages render but every form POST is
rejected with 403.

**The emailed verification link uses `https://DOMAIN`.** `CANONICAL_ORIGIN`
defaults to `https://DOMAIN`, so until TLS actually works the link in the
email points at an https URL that does not answer. Finish your proxy setup
before you create the first account, or read the link out of the worker log
as described in step 4.

### 3. Start

Build from source:

```bash
docker compose up -d --build
```

Prebuilt image:

```bash
docker compose -f docker-compose.prebuilt.yml up -d
```

The prebuilt file pins every app role to the same published tag
(`minnixtx/reeltalk:${REELTALK_VERSION:-0.1.0}`) and contains no `build:`
key anywhere, which is what makes it pull only. It uses the same project name
and the same named volumes as `docker-compose.yml`, so moving between the two
paths reuses your data instead of orphaning it.

Wait for the web container to report healthy:

```bash
docker compose ps
```

Then point your reverse proxy at `localhost:3030`, or whatever `WEB_PORT` you
set, and make sure it forwards `X-Forwarded-Proto: https` and the original
`Host` header.

### 4. Mail (SMTP)

This is the part most installs get wrong, and it is not cosmetic. Every
account has to verify its address before it can sign in, so if mail does not
work then nobody, including you, can get in.

Set the five `EMAIL_*` keys in `.env`. The rule that bites: many mail servers
reject a message whose envelope sender is not the account you authenticated
as. ReelTalk builds From from `EMAIL_SENDER_NAME@EMAIL_SENDER_DOMAIN`, so
that pair must agree with `EMAIL_HOST_USER`. A mismatch does not fail at
startup. It fails per send inside the worker, so it looks like a broken
feature rather than a bad config. Check the pair before you create the first
account:

```bash
grep -E 'EMAIL_HOST_USER|EMAIL_SENDER_NAME|EMAIL_SENDER_DOMAIN' .env
```

If you have not configured mail at all, the console backend prints each
message to the worker log instead of sending it. During setup that log is
your way in:

```bash
docker compose logs worker | grep -A5 "Click the link"
```

Copy the `/account/verify/<token>/` URL and open it in your browser. Take
the full token from the mail body. The token echoed in other log lines is
truncated and will not work.

### 5. First run: create the admin

Browse to `https://your-domain/`. A brand new instance redirects every page
to `/setup/`. That is expected. The form there creates the first account,
which becomes the instance admin.

That account cannot sign in until its email is verified, which is why step 4
comes before this step.

### 6. Signup policy

Both settings live in the database, not in `.env`. Admin, then Site settings:

- `signup_policy`: `open` or `invite`. **New instances start invite only.**
  A freshly deployed instance is usually not ready for strangers to walk
  into, so the safe state is the one you get by doing nothing. Open it
  deliberately once the proxy, the mail and the community are settled.
- `invite_scope`: who may mint invite links, `admins` (default) or `all`.

An invite link works whether or not public signup is open, so a closed
instance can still let people in one at a time.

### 7. Volumes

Named volumes, created automatically:

| Volume | Holds |
|---|---|
| `pgdata` | The database. The only thing you cannot rebuild. |
| `static_volume` | Collected static assets. Regenerated on start. |
| `media_volume` | Uploaded images and posters fetched from TMDB. |
| `backups_volume` | Daily database dumps. |

Both Compose files declare the same four, so the volumes follow you across
either install path. Backups live in a Docker volume: if you back up the host
but not that volume, you have no backups. Copy them off the machine.

### 8. Upgrading

**Prebuilt.** Pin the version in `.env` and re up:

```
REELTALK_VERSION=0.2.0
```

```bash
docker compose -f docker-compose.prebuilt.yml pull
docker compose -f docker-compose.prebuilt.yml up -d
```

Migrations run automatically when the web container starts. Read the release
notes first: if a release adds a migration you want the instance quiet while
it applies. Rolling back is changing the pin back and running `up -d` again.

Avoid floating everything on `latest`. It can move underneath you mid
incident and you lose the ability to say which version you are running.

**From source.**

```bash
git pull
docker compose build
docker compose up -d
```

Build all services, not just `web`. The worker and backup roles run from
their own image tags built from the same Dockerfile, and a half rebuilt stack
is how you end up with two code versions against one database.

### 9. Backups and restore

The `backup` service dumps the database once a day at `BACKUP_TIME` into the
`backups_volume`, keeping the newest `BACKUP_RETENTION`.

```bash
docker compose run --rm backup python manage.py backup_database
```

Restore names its target explicitly and never defaults it, because dumping
the live database is harmless and restoring over it is not:

```bash
docker compose exec db psql -U reeltalk -d postgres -c 'CREATE DATABASE reeltalk_restore'
docker compose run --rm backup python manage.py restore_database /app/backups/<dump>.dump --into reeltalk_restore
```

| Flag | Meaning |
|---|---|
| `<dump>` | Path to the custom format dump. Refuses if it does not exist. |
| `--into <db>` | Required, no default. Never creates a database, so a typo fails loudly instead of silently making an empty one. |
| `--force` | Additionally required to restore over the live database. |
| `--clean` | Drop and recreate objects in a target that already has a schema. |

### 10. Image tags

Published on Docker Hub as `minnixtx/reeltalk`, with no `v` prefix:

- `0.1.0`: exact version. Pin this.
- `0.1`: the minor line.
- `0`: the major line.
- `latest`: newest stable release. Prereleases publish their own tag and do
  not move `latest`.

### 11. Troubleshooting

| Symptom | Cause |
|---|---|
| Every request returns 400 | Hostname or IP not in `ALLOWED_HOSTS`. |
| Pages render but forms return 403 | Origin missing from `CSRF_TRUSTED_ORIGINS`. |
| Verification email never arrives | No SMTP configured. Check `docker compose logs worker`. |
| Mail rejected with "Sender is not same as SMTP authenticate username" | From address does not equal `EMAIL_HOST_USER`. Set `EMAIL_SENDER_NAME` and `EMAIL_SENDER_DOMAIN` to match. |
| Verification link is https and nothing answers there | TLS not set up yet. Finish the proxy, or read the link from the worker log. |
| A verification link you pasted says it did not work | You used a truncated token. The full token is in the mail body. |
| `failed to resolve reference ... not found` | The pinned `REELTALK_VERSION` tag does not exist on Docker Hub. Tags carry no `v` prefix. |
| Everyone shares one throttle bucket | A CDN sits in front and its ranges are not in `TRUSTED_PROXIES`. `python manage.py check_client_ip` shows why. |

## Status
The first release is **v0.1.0**. The feature set is complete and the app runs a
full test gate of 2500+ tests, but it is a young project with a single
maintainer: expect rough edges, and read the release notes before upgrading.

ReelTalk was originally built as a fork of BookWyrm under the Anti-Capitalist Software License v1.4. Because ACRL is not an OSI-approved free/open-source license, the project is being rewritten from scratch under the AGPLv3, using the original codebase purely as a functional reference. See REWRITE.md for the rationale and the rules that govern the rewrite.

The frozen original lives at minnixtx/reeltalk-legacy (archived, reference-only - feature inventory, behavior, design decisions; no code is carried over).

## What Is ReelTalk?

🌐 Federated - built on ActivityPub; instances can follow each other across the fediverse  
🎬 Film-first - track what you've watched, rate it, write reviews, and build shelves of favorites  
👥 Community-driven - small, trusted communities instead of one giant feed  
🔓 Free and open source - AGPLv3, no corporate middleman

## License
GNU AGPLv3

## AI Disclosure
ReelTalk was written in conjunction with local AI hosted on my own infrastructure. No code was written by or exposed to an external provider. Nothing is in the repo that I do not understand and did not co-author. I am not an application developer by trade, but a DevOps engineer, so local AI has been instrumental in allowing me to contribute to the open source software community and build something I've always wanted. Many people have their reasons for opposing AI, and I fully respect those beliefs. I have done my best to minimize the impact that my use of AI has contributed to the environment during this project.

No external API provider has had any part in the ReelTalk project. All inference has been ran in-house. The inference server used is based on AMD's Strix Halo platform and idles at ~ 5 watts, with the power limited to 90 watts at full load. My energy provider is 100% renewable energy.
