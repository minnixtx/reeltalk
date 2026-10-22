![ReelTalk](assets/reeltalk-github-banner.jpg)

# ReelTalk

A federated social network for tracking, reviewing, and discovering films. Free yourself from corporate social media and join a close community of film fans who value being free from spyware and data collection by gathering on a free and open source platform.

🌐 Federated - built on ActivityPub; instances can follow each other across the fediverse  
🎬 Film-first - track what you've watched, rate it, write reviews, and build shelves of favorites  
👥 Community-driven - small, trusted communities instead of one giant feed  
🔓 Free and open source - AGPLv3, no corporate middleman  

Available for amd64 and aarch64.

## Deployment

ReelTalk ships as one Docker image running two roles (web and worker) plus
PostgreSQL. There are two install paths and they differ
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

`SECRET_KEY` is not a formality: it signs session and CSRF cookies, and the
built-in development value is published in the source, so an instance running
on it has a secret everybody knows. With `DEBUG` off the app therefore refuses
to start when the key is missing, blank, or still the development value, and
tells you to run `setup.sh`. That refusal is the safety, not a bug: the
alternative is coming up looking healthy while every session cookie is
forgeable, admin included.

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

ReelTalk builds its From address from `EMAIL_SENDER_NAME@EMAIL_SENDER_DOMAIN`,
so that pair has to add up to `EMAIL_HOST_USER`. Check the pair before you
create the first account:

```bash
grep -E 'EMAIL_HOST_USER|EMAIL_SENDER_NAME|EMAIL_SENDER_DOMAIN' .env
```

Gmail setup:

```
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=true
EMAIL_HOST_USER=you@gmail.com
EMAIL_HOST_PASSWORD=xxxxxxxxxxxxxxxx
EMAIL_SENDER_NAME=you
EMAIL_SENDER_DOMAIN=gmail.com
```

Domain mail server:

```
EMAIL_HOST=mail.example.com
EMAIL_PORT=587
EMAIL_USE_TLS=true
EMAIL_HOST_USER=reeltalk@example.com
EMAIL_HOST_PASSWORD=...
EMAIL_SENDER_NAME=reeltalk
EMAIL_SENDER_DOMAIN=example.com
```

If you have not configured mail at all, the console backend prints each
message to the worker log instead of sending it. During setup that log is
your way in:

```bash
docker compose logs worker | python3 -c "
import quopri,re,sys
raw=re.sub(rb'(?m)^[^ ]+ +\| ',b'',sys.stdin.buffer.read())
t=quopri.decodestring(raw).decode('utf8','replace')
for u in sorted(set(re.findall(r'https?://\S+/account/verify/[A-Za-z0-9_-]+/',t))): print(u)
"
```

Open the `/account/verify/<token>/` URL it prints in your browser.

The token echoed in other log lines is truncated and will not work.

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

Both Compose files declare the same three, so the volumes follow you across
either install path.

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

### 9. Troubleshooting

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

## License
GNU AGPLv3

## AI Disclosure
ReelTalk was written in conjunction with local AI hosted on my own infrastructure. No code was written by or exposed to an external provider. Nothing is in the repo that I do not understand and did not co-author. I am not an application developer by trade, but a DevOps engineer, so local AI has been instrumental in allowing me to contribute to the open source software community and build something I've always wanted. Many people have their reasons for opposing AI, and I fully respect those beliefs. I have done my best to minimize the impact that my use of AI has contributed to the environment during this project.

No external API provider has had any part in the ReelTalk project. All inference has been ran in-house. The inference server used is based on AMD's Strix Halo platform and idles at ~ 5 watts, with the power limited to 90 watts at full load. My energy provider is 100% renewable energy.
