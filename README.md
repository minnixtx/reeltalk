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

### Requirements

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
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | unset | Your email parameters. |
| `EMAIL_SENDER_NAME` | `admin` | Local part of the From address. |
| `EMAIL_SENDER_DOMAIN` | `DOMAIN` | Domain part of the From address. |
| `CANONICAL_ORIGIN` | `https://DOMAIN` | The origin every ActivityPub identity is minted from. Set it only if that is wrong for you, and never to a LAN address. |
| `WEB_PORT` | `3030` | Host port the web container publishes. |
| `CSRF_TRUSTED_ORIGINS` | unset | Comma-separated list of full origins (scheme, host and port) that a browser may post forms to. Not a yes/no switch, and the scheme is required. Leave it unset for a normal single-domain https install; add one entry per extra origin you actually type into a browser. Example: `CSRF_TRUSTED_ORIGINS=https://reeltalk.example.com,http://192.168.1.50:3030` |
| `TRUSTED_PROXIES` | the private ranges | Which peers may tell us a request arrived over https, and which hops to skip when reading the client address. A proxy on your LAN or on the Compose network needs no value at all. |
| `BACKUP_TIME` / `BACKUP_RETENTION` | `03:17` / `7` | Daily dump schedule. |
| `ALLOWED_HOSTS` | `DOMAIN` plus localhost | Add extra hostnames here. |

Leave `DEBUG` off. Do not turn it on for anything reachable over a network.

Please note the following:

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

To build the image yourself:

```bash
docker compose up -d --build
```

To use the official maintainer image:

```bash
docker compose -f docker-compose.prebuilt.yml up -d
```

Wait for the web container to report healthy:

```bash
docker compose ps
```

Then point your reverse proxy at `localhost:3030`, or whatever `WEB_PORT` you
set, and make sure it forwards `X-Forwarded-Proto: https` and the original
`Host` header.

### 4. Mail (SMTP)

If you do not setup email, you will have to access your token from the
docker logs.

Set the five `EMAIL_*` keys in `.env`.

One rule breaks more installs than everything else combined. Many mail servers
refuse to send a message whose envelope sender is not the account you
authenticated as. ReelTalk builds its From address from
`EMAIL_SENDER_NAME@EMAIL_SENDER_DOMAIN`, so that pair has to add up to
`EMAIL_HOST_USER`. Get the two to match and the rule never fires.

It bites by default because `EMAIL_SENDER_NAME` and `EMAIL_SENDER_DOMAIN`
fall back to `admin@<your instance host>`, and that is almost never a real
mailbox on your mail server. A mismatch also does not fail at startup: it
fails on every individual send inside the worker, so it reads as a broken
feature rather than a bad setting. Check the pair before you create the first
account:

```bash
grep -E 'EMAIL_HOST_USER|EMAIL_SENDER_NAME|EMAIL_SENDER_DOMAIN' .env
```

Two worked examples.

A mailbox on a provider you sign up for, Gmail for instance. Google will not
accept your normal account password over SMTP, so turn on two-step
verification first and then generate a 16-character app password at
myaccount.google.com/apppasswords. Gmail enforces the sender rule too, so the
From pair has to be your Gmail address and not your instance domain:

```
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=true
EMAIL_HOST_USER=you@gmail.com
EMAIL_HOST_PASSWORD=xxxxxxxxxxxxxxxx
EMAIL_SENDER_NAME=you
EMAIL_SENDER_DOMAIN=gmail.com
```

Yahoo works the same way: `smtp.mail.yahoo.com`, port 587, and an app password
from your Yahoo account security page. A free mailbox is a good fit for a
private single-user instance, which is what most people want here. It is a poor
fit for an open one, because a free mailbox caps how many messages you can send
a day and every verification mail arrives from a personal address.

A mail server on your own domain, which is what you want once the instance has
a few people on it:

```
EMAIL_HOST=mail.example.com
EMAIL_PORT=587
EMAIL_USE_TLS=true
EMAIL_HOST_USER=reeltalk@example.com
EMAIL_HOST_PASSWORD=...
EMAIL_SENDER_NAME=reeltalk
EMAIL_SENDER_DOMAIN=example.com
```

Notice that in both examples the From pair is just `EMAIL_HOST_USER` split at
the `@`. That is the whole rule. If you can say the two halves out loud and
they reassemble into the account you log in with, your mail will go out.

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

### 10. Troubleshooting

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
