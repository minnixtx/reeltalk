# ReelTalk (AGPLv3 rewrite) — Build Plan & Functional Spec

**Status:** Phase 1 (M0) complete, plus the pre-M1 stack audit (2026-09-05, §3.9). This document is the build plan for this repo. Next session starts executing M1 on the audited stack.
**Governing rules:** [REWRITE.md](REWRITE.md) — clean-room rewrite under AGPLv3; the frozen legacy fork (`/home/minnix/reeltalk-legacy`, GitHub `minnixtx/reeltalk-legacy`) is a **functional reference only**. No verbatim or near-verbatim code from it. Federation is built against the [ActivityPub](https://www.w3.org/TR/activitypub/) and [Activity Streams 2.0](https://www.w3.org/TR/activitystreams-core/) specs, not BookWyrm source.

---

## 1. Source material

- `PROGRESS.md` in the legacy repo — feature inventory, execution record, ops runbook (host quirks still apply).
- Legacy decision log (its §8, decisions #1–34) — **binding owner decisions**, distilled in §2 below. Do not re-litigate.
- A walk of the legacy codebase (models, `tmdb.py`, activitypub wire types, views, deployment files) — used to describe *behavior and shape*, never copied.

## 2. Binding owner decisions (distilled from the legacy decision log)

These carry over into the rewrite as product requirements:

| # | Decision |
|---|---|
| D1 | **Binary film model.** A film is either watched or not. Exactly two default shelves per user: **Watchlist** (identifier `to-read`) and **Watched** (identifier `read`). No in-progress/watching state, no custom-shelf UI (the Shelf object stays — it is a federated OrderedCollection). |
| D2 | **Flat single `Film` model.** One object per title. No Work/Edition split, no ISBN dedup, no Person/Author models — directors and cast are plain name-list fields on Film. |
| D3 | **Silent watched + rating required.** Marking a film watched posts *no* auto-generated feed note; sharing happens via reviews. A film cannot be added to Watched without a star rating (0.5–5, half steps); the written review is optional. Rating-only entries are first-class (`ReviewRating`). |
| D4 | **TMDB is the source of truth for metadata.** Films with a `tmdb_id` are locked from user editing in the UI; manually created films (no TMDB ID) stay fully editable. Only user-generated content (review text, rating, comments) is editable. |
| D5 | **One review per film per user.** If a user already reviewed a film, the UI offers *edit that review* only; re-finishing a reviewed film updates it in place. |
| D6 | **TMDB as primary catalog via global search** (Letterboxd model): the main search box queries TMDB when an API key is configured, with one-click "Add to Watchlist" and click-through to the film page. Without a key, search degrades gracefully to local-library search. |
| D7 | **Dedup = ID match + title/year fallback.** Exact `tmdb_id` (then `imdb_id`) match first; else normalized title (leading article stripped) + year — which *backfills* the TMDB ID and empty metadata onto an existing manual film instead of creating a duplicate. |
| D8 | **API key in `.env`** (`REELTALK_TMDB_API_KEY`), operator-set, shared by all users; unset → not-configured notice, no per-user keys. |
| D9 | **File import = TMDB-style CSV only.** Canonical ten-column header (TMDB's own export shape): `TMDb ID, IMDb ID, Type, Name, Release Date, Season Number, Episode Number, Rating, Your Rating, Date Rated`. No API calls during import; synchronous single-transaction flow with per-row results + summary. Unrated row → Watchlist; rated row → TMDB's 1–10 rating ÷2 to nearest half-star → Watched + `ReviewRating`. Non-movie rows and missing names skipped with a note. Existing reviews never touched. |
| D10 | **Export round-trips.** "Export Film List" emits the same ten-column format (one row per film the user has a relationship with: shelved/reviewed/commented), so exports re-import as a no-op. Review text drops out — the format carries ratings, not reviews. |
| D11 | **Async TMDB backfill after import.** Import creates ID stubs; a background task then fetches details + poster for every imported film (created *and* matched). Idempotent, rate-limit-friendly pacing (~0.25 s/fetch), per-film failures skipped and logged, no-op without a key. |
| D12 | **No Quotation feature.** Book-era holdover; film users only review and rate. Removed from the product entirely (inbound remote quotation activities are ignored gracefully). |
| D13 | **English-only for now.** No translation component in v0.1. Keep re-introduction cheap (gettext markers optional; a Crowdin project can be added later). |
| D14 | **No HTTPS/Let's Encrypt in the project.** Plain-HTTP endpoint on port 3030; the operator terminates TLS with their own reverse proxy and forwards `X-Forwarded-Proto: https`. No bot-protection wall (anubis was removed upstream for good reason — it broke plain-HTTP access). |
| D15 | **New `"Film"` ActivityPub wire type.** Clean break from Book/Edition/Author wire types; film objects federate between ReelTalk instances. |
| D16 | **Removed features (do not build):** barcode/ISBN scanning, code of conduct page, reading/watching goals, Patreon/support footer, connectors/Readwise import, user book-list importers (OpenReads/LibraryThing/etc.), suggestion lists, cover-maintenance jobs, custom shelf management UI, in-progress status. |
| D17 | **Letterboxd is the loose UX template** for design alignment with the owner. Custom artwork happens at the very end, *with* the owner — no solo design decisions. |

## 3. Functional spec

### 3.1 Product overview

A federated (ActivityPub) social site for film tracking, reviewing, and discovery. Users track what they've watched (Watched + star rating, optional written review), keep a Watchlist, follow other users and groups, exchange reviews on a shared timeline, curate lists, and discover films through TMDB-backed search. Instances of ReelTalk federate with each other.

### 3.2 Domain model

Core objects (described functionally; the rewrite implements these fresh):

- **Film** — one row per title. Fields: `title`, `sort_title` (auto-derived: leading article "the/a/an" stripped, lowercased), `subtitle`, `description` (HTML, from markdown), `year` (int), `runtime` (int minutes), `genres[]`, `directors[]`, `cast[]` (plain name strings), `poster` (image), `tmdb_id`, `imdb_id` (dedup fields), `origin_id`/`remote_id` (ActivityPub identity: `origin_id` is the id when the object was created locally, `remote_id` when received from another instance). Full-text search via a Postgres tsvector column maintained by a trigger (title weighted highest, then subtitle, directors+cast, genres).
  - **Merge/absorb:** two Film rows for the same title can be merged — empty fields on the canonical row are filled from the absorbed one, all related objects (shelves, statuses, lists) are re-pointed, and a `MergedFilm` row keeps old id → new id so old URLs redirect.
- **Shelf / ShelfFilm** — a named collection of films for a user. Default shelves per local user: `to-read` ("Watchlist") and `read` ("Watched"). `ShelfFilm` is the through row (user, film, shelf). Shelves are federated as ActivityPub OrderedCollections.
- **Status** — a post anchored to a film (or standalone). Markdown content → HTML, published date, soft-delete (kept with a `deleted_id` tombstone for federation consistency). Subtypes:
  - **Comment** — short note on a film; multiple allowed per user.
  - **Review** — the user's review of a film; carries an optional star rating (float 0.5–5). **One per user per film** (D5).
  - **ReviewRating** — rating-only entry (a Review with no content); editable in place, can later gain text.
- **List / ListItem** — curated or personal lists of films, with ordering and per-item notes; federated as OrderedCollections. Group members can contribute to group lists (pending/curation state).
- **User** — `localname@domain` identity, display name, summary (markdown), avatar, local vs remote flag, follows/followers, blocks (users, films, servers), feed filters (which status types to see), 2FA support, email verification. Remote users are lightweight mirrors populated from federation.
- **Group** — a named community of users with its own feed and lists.
- **Notification** — per-user event rows (follow, follow request, review on your film, comment, mention, import/export job completion, …).
- **Hashtag** — `#tag` tokens extracted from status content; federated objects; searchable.
- **Site settings** — instance name/description, signup policy, admin-managed link-domain allowlist for safe outbound links.
- **Antispam** — basic rate/limit protections on signup and posting (fresh, simple implementation).

### 3.3 Watch state & review rules (the core UX contract)

1. Films page has exactly three tabs: **All films / Watchlist / Watched**.
2. "Want to watch" → shelve onto Watchlist; may post an optional comment or a generated "wants to watch" note.
3. "Mark as watched" → **requires a star rating** (validated before any DB write); shelves the film to Watched; creates a `ReviewRating` (rating-only) or `Review` (with text). **Posts no automatic feed note** — only the review/rating, if written, is shared.
4. Re-finishing a film that already has a review **updates** that review (new rating applied; empty modal text keeps existing content).
5. TMDB-sourced films: metadata fields hidden from editing; manual films fully editable.

### 3.4 TMDB integration

- **Client:** TMDB v3 REST — `GET /search/movie` (paginated, returns total counts), `GET /movie/{id}?append_to_response=credits,images`, poster download from the image CDN (`https://image.tmdb.org`). User-facing error type for bad key (401), rate limit (429), network failure.
- **Field mapping:** TMDB details → Film fields 1:1 (title, year, runtime, genres, directors, cast capped at ~10, description); `tmdb_id` stored; film page links out to themoviedb.org.
- **Search as primary catalog (D6):** global film search queries TMDB when the key is set; each result row shows poster + title + year, links to a click-through route that runs create-or-match (D7) and lands on the film page; one-click "Add to Watchlist" POST per row. Anonymous users see results without actions. Locally blocked films are excluded. No key → local-library search only.
- **Search-as-you-type:** `GET /search/suggest/?q=<term>&type=film` JSON endpoint — top ~8 of TMDB page 1 (or local matches without a key), rows minimal (title/year/poster, no inline actions), local users only, click-only navigation, ~300 ms debounce, min 2 chars.
- **Backfill task (D11):** Celery task walks imported film IDs; skips films without `tmdb_id` or already complete (poster + description); fetches details, fills empty fields, downloads poster; 0.25 s spacing (TMDB allows ~50 req/10 s — a 1,400-film import takes ~15 min); per-film errors logged and skipped; queued after the import transaction commits.
- **Rate-limit posture:** all TMDB traffic is server-side and paced; no client-side calls (CSP `img-src` allows the poster CDN for display only).

### 3.5 File import / export

- **Import** (`/preferences/import/`, local users only): upload a TMDB-style CSV (ten-column header validated up front — missing columns rejected with a message), `utf-8-sig` decode (TMDB exports carry a BOM), 20,000-row cap, whole import in one transaction. Per row: create-or-match (TMDb ID → IMDb ID → normalized title+year; on match, empty local IDs/year backfilled). Unrated → Watchlist; rated → ÷2 nearest half-star → Watched + `ReviewRating` (D9). Skips non-movie rows and missing names with per-row notes. All saves are local-only (no federation broadcast — importing your own data is not a social act). Results page: per-row table (line / name / status / note) + summary counts; then the async backfill runs (D11).
- **Export** (`/preferences/export/`): one row per film with any relationship (shelved/reviewed/commented), canonical ten columns: TMDb + IMDb IDs from the film, `Type` = "movie", Release Date = stored year as `YYYY-01-01T00:00:00Z`, Your Rating = most recent rated review ×2, Date Rated from that review's published date (UTC); season/episode/rating columns empty. Round-trips byte-exactly for unrated films; re-import is a no-op (D10).

### 3.6 Federation surface (built against the ActivityPub spec)

Implemented fresh against W3C ActivityPub + Activity Streams 2.0 — **not** from BookWyrm source:

- **Identity & discovery:** `/.well-known/webfinger` (acct → Person), `/.well-known/nodeinfo` + nodeinfo 2.0 document (software name `reeltalk`, protocol `activitypub`, open-registration policy), per-user public pages serving the **Person** JSON-LD document with outbox/inbox URLs.
- **Objects:** **Person** (users), **Film** (the new wire type, D15 — a custom type in ReelTalk's namespace; film objects are exchanged between ReelTalk instances and deduped via `tmdb_id`/`imdb_id`/origin ids per D7), **Note** (statuses: comments/reviews/ratings, with `inReplyTo` threading and film reference), **OrderedCollection** (shelves, lists, outboxes).
- **Activities:** Create/Update/Delete for Note and Film objects; Follow/Accept/Reject between Persons; Like (favorite) on statuses. Inbound handling tolerates unknown activity/object types by ignoring them gracefully (a remote instance sending e.g. quotations or book objects must not crash or create content here — D12, D16).
- **Delivery:** per-user inbox + a shared instance inbox; outbox as paginated OrderedCollection; signed HTTP requests (ActivityPub signature scheme); broadcast only to known followers/mention targets; film-metadata updates are sent to ReelTalk instances (software-filtered) so non-ReelTalk servers never receive the custom type.
- **Remote users:** lightweight mirror records created on first interaction (follow, mention, reply), updated from inbound Person documents.
- **Followers/following** drive the feed: a user's timeline = statuses from followed users/groups + their own; group feeds aggregate member posts.

### 3.7 UX surface & first-working-version scope

Pages/features of the finished product (legacy parity list), with **[v0.1]** marking what belongs in the first working version:

- Setup wizard (first-run: create admin) [v0.1]
- Auth: signup (open or invite-gated per site settings), login, logout, 2FA, email verification, password reset [v0.1 core auth; 2FA/email later]
- Landing/about page, instance info [v0.1 minimal]
- User pages: profile (avatar, name, summary, follow button, films tab, lists tab, reviews), user's films (3 tabs per D1) [v0.1]
- Film pages: metadata display, poster, shelve controls, review/comment compose, "View on TMDB" link, block/unblock film [v0.1]
- Feed/timeline (followed users + groups), suggested users/films [feed v0.1; suggestions later]
- Global search: TMDB-backed with watchlist action + click-through (D6); local fallback; suggest dropdown (search-as-you-type) [v0.1 search page; dropdown right after]
- Lists: create/edit, add/remove/reorder films, notes, group lists with curation [later milestone]
- Groups: create/join/leave, group feed, group lists [later milestone]
- Notifications page + count [later milestone]
- Directory (browse local users), discover page (trending/popular) [later milestone]
- Preferences: profile edit, theme (dark/light), timezone, **Import Film List / Export Film List** [import/export milestone]
- RSS feeds for user reviews/films [later milestone]
- Admin: site settings, link domains, user management, film merge/absorb tool, reports/moderation [v0.1 minimal admin; moderation later]
- Guided tour for new users [polish, with owner]

**First working version (M1+M2 target):** a locally runnable instance where the owner can sign up, create films manually or via TMDB search, watchlist them, mark watched with rating/review, and see their films + feed. That is the "it works" bar before federation starts.

### 3.8 Deployment shape

Docker-first. The stack grows per milestone (owner direction: grow the stack as needed — §3.9):

- **M1–M3 services (2):** `web` (uvicorn + Django; serves the site AND static/media itself via whitenoise + a media URL pattern) on `${WEB_PORT:-3030}`, and `db` (Postgres 17). No proxy, no Redis, no task queue — M1 has zero background tasks.
- **M2+ services (3):** a `worker` (same image, Django-Q2 `qcluster`) joins for the TMDB backfill task; its cluster lives in Postgres, so still no Redis.
- **Public deploy (M6):** the same containers behind the operator's own TLS-terminating reverse proxy (D14). No in-project proxy at any milestone; a daily `pg_dump` backup job lands before public deploy.
- **Volumes:** `pgdata`, `static_volume`, `media_volume` (posters/avatars); `exports_volume` + `backups` land with M3/M6 as needed.
- **Entrypoint behavior:** the web container auto-runs migrations + collectstatic on start (no manual migrate step); an `initdb` management command seeds permissions/site settings idempotently. The image runs as a non-root user.
- **TLS policy (D14):** none in-project; operator reverse proxy in front of :3030 forwarding `X-Forwarded-Proto`. `CSRF_TRUSTED_ORIGINS` env-overridable for extra access origins (LAN IP, etc.).
- **Env:** `.env` with `DOMAIN`, generated secrets (`SECRET_KEY`, `POSTGRES_PASSWORD`), optional `REELTALK_TMDB_API_KEY`, optional SMTP vars; a `setup.sh` generates the secrets safely.
- **Host quirks (verified real on this Fedora box):**
  1. SELinux: ad-hoc bind mounts need the `:z` label or containers get Permission denied even as root (compose uses named volumes; one-off `docker run -v` still needs it).
  2. IPv6 `[::1]` through docker-proxy resets HTTP connections → test with `curl -4`.
  3. Named volumes inherit ownership from the image on first init — after changing the image's runtime user, artifact-only volumes (`static_volume`, `media_volume`) must be deleted and re-initialized or collectstatic hits Permission denied. Never touch data volumes (`pgdata`).

(The legacy runbook's nginx-restart-after-web-rebuild and rebuild-all-three-celery-images quirks no longer apply — those services are gone.)

### 3.9 Stack audit (2026-09-05)

Pre-M1 audit of the planned stack against current best practice, executed per AUDIT-BRIEF.md (consumed and deleted at session end). Method: PyPI JSON metadata for every package below (license + freshness), GitHub health checks where a dependency is new or load-bearing, web research for framework-level choices. All verdicts dated 2026-09-05.

**Result: the M1 stack shrank from 6 services to 2, and the runtime dependency set from 15 to 12 packages.**

| Area | Was (M0) | Now | Why |
|---|---|---|---|
| Python | 3.11 | **3.13** (`python:3.13-slim`) | 3.14 is current stable but ~11 months out from release; 3.13 has a year of ecosystem bake-out, support to 2029-10, and covers the whole v0.1 lifecycle. Django 6.1 supports 3.12–3.14. |
| Package manager | pip, no lockfile | **uv** + committed `uv.lock` | Lockfile hygiene + build speed: all 51 packages resolve in <1 s (pip took minutes); reproducible builds via `uv sync --frozen`. uv is MIT OR Apache-2.0 and very active (0.12.10, 2026-09-04). |
| Web framework | Django 5.2.16 (LTS) | **Django 6.1** (current stable; 6.1.1 released 2026-09-02) | New codebase: zero migration cost now, while staying on 5.2 LTS (security EOL 2028-04) would force a 6.x migration within a year anyway. 6.1 requires Python ≥3.12 and Postgres ≥15 (we run 17); django-stubs 6.1.0 tracks it. The new `MAILERS` setting is adopted from day one (`EMAIL_*` settings are deprecated in 6.1). |
| Server | gunicorn (WSGI) | **uvicorn** (ASGI) | ASGI is Django's forward path (async views, future WebSockets); uvicorn is BSD-3-Clause and very active (0.52.4, 2026-08-19). M1 needs no async views — sync code runs fine under ASGI. |
| Task queue | Celery 5.6 + django-celery-beat + Redis broker | **None in M1; Django-Q2 at M2** | M1 has zero tasks; the first real task is the M2 TMDB backfill (D11). When it lands, Django-Q2 (MIT, active — v1.11.1 2026-08-26) with a **Postgres cluster** replaces celery + beat + broker in one package; its built-in scheduler covers periodic jobs. Rejected: Celery (inertia — 3 packages + Redis), ARQ (needs Redis), Dramatiq (LGPLv3+, heavier model), plain async views (no retry/survival for a ~15-minute backfill job). |
| Cache / sessions | Redis DB0 | **LocMemCache + DB sessions** | At single-instance scale the cache is a performance nicety, not correctness; once the broker was gone, Redis had no remaining job. |
| Static / media serving | nginx | **whitenoise in-process** (static) + Django `serve` URL pattern (media) | Removes an entire service and the nginx-upstream-restart host quirk. whitenoise is MIT and active (6.12.0); serves compressed, immutable-cached static from the web process. At this scale (single instance, operator proxy in front per D14) a dedicated static server buys nothing. |
| Reverse proxy | nginx on :3030 | **None** — web exposes :3030 directly | Same rationale; TLS stays the operator's job (D14). Caddy was considered and rejected for the same "no proxy needed" conclusion. |
| Frontend JS layer | (unspecified) | **Vanilla JS, no framework, no build step** | The v0.1 interactivity surface is small: search-as-you-type dropdown (~300 ms debounced fetch) + one-click watchlist POSTs. HTMX/Alpine considered and rejected for now — a dependency plus learning curve to save ~50 lines of trivial JS; revisit if the surface grows (M5+). |
| ActivityPub tooling (M4) | (unspecified) | **Write from spec** | No maintained Python AP library exists: PyPI's `activitypub` package is 2018-era, Fedify (the reference framework per activitypub.rocks) is TypeScript, the rest are prototypes. The clean-room rules already mandate building against the W3C specs; the custom `Film` wire type + software-filtered delivery would be bespoke code regardless. |
| Lint / format | ruff 0.14.7 | **ruff 0.16.6** (MIT, 2026-09-03) | Freshness; config modernized — added `I` (isort) + `UP` (pyupgrade), dropped the legacy-carried ignores (E501/E722/E731/F405). |
| Types | mypy 1.7.1 (a 2023 pin) | **mypy 2.3.1** + django-stubs[compatible-mypy] 6.1.0 | The pin was years stale; mypy is at 2.x now. django-stubs 6.1.0 tracks Django 6.1 and resolves cleanly against mypy 2.3.1 (verified in the lock). |
| Tests | pytest 9.0.3 stack | **pytest 9.1.1** + current plugins (pytest-django 4.14.0, pytest-cov 7.1.0, pytest-env 1.7.0, pytest-xdist 3.8.0), responses 0.26.3 | Freshness; all MIT/Apache-class licenses. |
| Dockerfile | python:3.11-slim, pip venv at /venv, **runs as root** | python:3.13-slim, uv venv at /app/.venv, lockfile-first layer caching, **non-root `appuser`** | Non-root is table stakes for a public-deploy-bound image; the switch forced artifact-volume re-init (§3.8 quirk 3). |

**Bumped pins (same packages, licenses re-verified via PyPI metadata):** Django 6.1.1 (BSD-3-Clause), psycopg[binary] 3.3.5 (LGPL-3.0-only), requests 2.34.2 (Apache-2.0), mistune 3.3.4 (BSD-3-Clause), django-csp 4.0 (BSD), environs 15.2.0 (MIT). Pillow 12.3.0, bleach 6.4.0, python-dateutil 2.9.0.post0 and django-pgtrigger 4.17.0 were already current.

**Removed from the M1 stack:** gunicorn, celery[redis], django-celery-beat, redis, hiredis (verdicts remain on record in §4.1; none are license-blocked — removal is a lean-stack decision, not a compliance one).

**Gotchas hit during the switch (all fixed):**
1. **`[dependency-groups] main` is a trap.** PEP 735 reserves the name `main` for `[project.dependencies]`; uv silently skips a custom group named `main` (pip accepted it, which is why M0 worked). The first uv build installed only the dev group — caught via crash-loop + site-packages inspection. Runtime deps now live in `[project.dependencies]`.
2. **Django 6.1 rejects mixed email config.** Defining `MAILERS` alongside any legacy `EMAIL_*` module attribute is an `ImproperlyConfigured` error — env values must be read into plain locals, never `EMAIL_*` settings.
3. **Root-owned named volumes break a non-root switch.** `static_volume`/`media_volume` were initialized under the old root image; collectstatic hit Permission denied. Both contained only regenerable artifacts (verified) and were deleted + re-initialized from the new image.

**Rejected alternatives, in one line each:** stay on Django 5.2 LTS (migration debt within a year); gunicorn+uvicorn workers (two servers for no benefit at this scale); Celery at M2 (3 packages + Redis vs 1 package + Postgres we already run); Caddy (no proxy needed); HTMX/Alpine (a dependency to save ~50 lines of JS); third-party AP library (none maintained in Python).

## 4. License audit (AGPLv3 compatibility)

Method: every package in the legacy `pyproject.toml` was checked against PyPI metadata (license field + SPDX expression + classifiers), with GitHub used to confirm where PyPI metadata is sparse. Assets were inventoried in the legacy `static/` tree and checked for bundled license files. Verdicts below are **for this audit date (2026-09-05)**; re-verify before any new dependency lands.

### 4.1 Python dependencies — verdicts

Legend: ✅ = AGPLv3-compatible, clear to use · ⚠️ = compatible but with a caveat or replacement recommended · ❌ = do not use.

| Package (legacy pin) | License (verified) | Verdict |
|---|---|---|
| **bw-file-resubmit** (0.6.0rc2) — *flagged first target* | **MIT** (PyPI metadata + GitHub repo LICENSE both confirm MIT) | ✅ Compatible. It is a small drop-in widget that preserves file uploads across form validation errors. May be carried over, but it is BookWyrm-specific and tiny — **recommend implementing the ~20-line widget natively** in this codebase and skipping the dependency (fewer moving parts, no rc-version pin). Owner's call either way; the license does not force replacement. |
| Django (5.2.16) | BSD-3-Clause | ✅ |
| celery[redis] (5.6.2) | BSD-3-Clause | ✅ |
| django-celery-beat (2.8.1) | BSD | ✅ |
| aiohttp (3.14.3) | Apache-2.0 AND MIT | ✅ |
| arabic-reshaper (3.0.1) | MIT | ✅ (only needed for RTL title rendering — drop unless required) |
| bleach (6.4.0) | Apache-2.0 | ✅ (HTML sanitization for user content) |
| boto3 (1.34.74) | Apache-2.0 | ✅ (S3 storage backend — optional; local volumes suffice for v0.1) |
| colorthief (0.2.1) | BSD | ✅ (dominant-color extraction — optional polish) |
| django-compressor (4.6) | MIT | ✅ (asset pipeline; can also be skipped with plain static files) |
| django-csp (3.8) | BSD | ✅ (CSP headers — keep, needed for TMDB CDN posters) |
| django-imagekit (6.0.0) + pilkit | BSD | ✅ (poster thumbnail variants — optional; can start with a single poster size) |
| django-model-utils (4.4.0) | BSD-3-Clause | ✅ |
| django-oauth-toolkit (3.2.0) | BSD | ✅ (only if an OAuth API surface is ever wanted — not in v0.1 scope) |
| django-pgtrigger (4.17.0) | BSD-3-Clause | ✅ (tsvector search trigger — keep, or write the trigger SQL directly in migrations) |
| django-sass-processor (1.4.2) + libsass | MIT | ✅ (runtime SCSS compilation; alternative: precompile in CI/build) |
| django-storages (1.14.6) | BSD-3-Clause | ✅ (S3/Azure backends — optional) |
| environs (14.5.0) | MIT | ✅ (env parsing) |
| gunicorn (25.0.3) | MIT | ✅ |
| hiredis (2.3.2) | MIT | ✅ |
| mistune (3.3.0) | BSD-3-Clause | ✅ (markdown → HTML for reviews/statuses) |
| opentelemetry-* (api/sdk/exporter/instrumentations, 1.24/0.45b0) | Apache-2.0 | ✅ (telemetry — optional; drop from v0.1 to stay lean, re-add later) |
| Pillow (12.3.0) | MIT-CMU (PIL license) | ✅ (image processing — core) |
| psycopg[binary] (3.2.9) | **LGPL-3.0-only** | ✅ Compatible with AGPLv3 (LGPL-3.0 permits linking into AGPLv3 programs; the binary wheel is a dynamically-loaded extension module). Keep — it is the standard Postgres driver for Django. |
| pycryptodome (3.20.0) | BSD / Public Domain mix | ✅ (crypto primitives — keep if used for token hashing/etc.) |
| pyotp (2.9.0) | MIT | ✅ (TOTP 2FA) |
| python-bidi (0.6.10) | LGPL | ⚠️ Compatible (LGPL), but only needed alongside arabic-reshaper for RTL text — drop both unless RTL support is in scope. |
| python-dateutil (2.9.0.post0) | Apache-2.0 / BSD dual | ✅ |
| qrcode (7.4.2) | BSD | ✅ (2FA setup QR codes) |
| redis (5.0.3) | MIT | ✅ |
| requests (2.33.0) | Apache-2.0 | ✅ (TMDB client, federation HTTP) |
| responses (0.25.0) | Apache-2.0 | ✅ (dev-only: HTTP mocking in tests) |
| s3-tar (0.1.13) | MIT | ✅ (S3 tar uploads — only with S3 storage; optional) |
| sqlparse (0.6.0) | BSD | ✅ (Django dependency anyway) |
| ua-parser[regex] (1.0.1) | Apache-2.0 | ✅ (user-agent parsing for antispam — keep or drop per antispam design) |
| grpcio / setuptools / tornado (indirect, security-pinned) | Apache-2.0 / MIT / Apache-2.0 | ✅ (transitive; pin for security as legacy did) |

**Dev group** (not distributed with the app): pytest, pytest-django, pytest-cov, pytest-env, pytest-xdist, fakeredis, ruff, mypy + django-stubs, types-* stubs, pytidylib — all MIT/BSD/Apache-class; ✅.

### 4.2 Static assets & fonts — verdicts

| Asset (legacy location) | License status | Verdict |
|---|---|---|
| Bulma CSS framework (`static/css/vendor/bulma/`) | **MIT** (LICENSE file present in-tree, © Jeremy Thomas) | ✅ May be re-vendored with its LICENSE. It is a generic framework, not BookWyrm code — but re-download from the official release rather than copying files from the legacy tree. |
| Public Sans (`static/fonts/public_sans/`) | **SIL OFL 1.1** (OFL.txt in-tree) | ✅ Free font; re-obtain from the official source with its license file. |
| Source Han Sans (`static/fonts/source_han_sans/`) | **SIL OFL 1.1** (LICENSE.txt in-tree; only license+README vendored, no actual font files) | ✅ if CJK fallback is wanted; otherwise skip entirely. |
| DM Serif Display (`static/css/fonts/dm_serif_display/`) | Google font, **SIL OFL 1.1** upstream — but **no license file was vendored** in the legacy tree | ⚠️ The font itself is free; do not copy the files from the legacy tree. Re-obtain from Google Fonts with the OFL text + attribution if it is still wanted (it was the display/heading face). |
| Icomoon icon font (`static/css/fonts/icomoon.{ttf,woff,eot,svg}`) | **Provenance unverifiable** — generated icon font, no license file; glyph names (arrow-*, barcode, bell, book, boost, graphic-banknote, …) suggest a Boxicons-derived set (Boxicons is OFL), but the icomoon project source that would prove it is not in the repo | ❌ **Do not carry over.** Regenerate the needed icon set from a clearly free source (e.g. Boxicons under OFL, or Feather under MIT) with the license file committed alongside. |
| Shepherd.js (`static/js/vendor/shepherd.min.js`) | **MIT** upstream (shepherdjs) — no license vendored in legacy tree | ⚠️ Re-obtain from the official release with its MIT license if the guided-tour feature is kept; or drop the tour for v0.1. |
| `logo.png`, `logo-small.png` (`static/images/`) | BookWyrm wyrm artwork — **copyrighted, no free license** | ❌ Do not carry over. Replace with original ReelTalk artwork (final art happens at the end, with the owner — D17). Use a plain placeholder in v0.1. |
| `default_avi.jpg` (default avatar) | BookWyrm asset — provenance unclear | ❌ Replace with an original/CC0 placeholder. |
| `no_cover.jpg` (missing-poster placeholder) | Provenance unclear (simple generated image, but unverified) | ⚠️ Regenerate a fresh placeholder (trivial to make original). |
| `favicon.ico`, `star-{empty,half,full}.png` | Provenance unclear | ⚠️ Replace with originals (stars are trivial to draw; favicon comes with the new logo work). |
| All SCSS/JS/CSS authored in `static/css/reeltalk/`, `static/js/*.js` | ACRL-licensed BookWyrm-derived code | ❌ Out of scope for asset carryover by definition — **rewritten fresh** under AGPLv3 (this is code, not an auditable free asset). |

### 4.3 Net result for the new `pyproject.toml`

No legacy dependency is license-blocked. The v0.1 dependency set (see §5, M0) is deliberately **leaner** than the legacy one: it drops optional/telemetry/storage/RTL packages and replaces `bw-file-resubmit` with a native widget, keeping only what the first working version needs. Everything retained is ✅ above.

### 4.4 Stack audit additions (2026-09-05)

New and changed dependencies from the stack audit (§3.9), same method as §4.1 (PyPI metadata + upstream confirmation where sparse):

| Package | License (verified) | Verdict |
|---|---|---|
| **uvicorn** (0.52.4) — new | BSD-3-Clause (PyPI `license_expression`) | ✅ Active (release 2026-08-19). Replaces gunicorn as the server. |
| **whitenoise** (6.12.0) — new | MIT (PyPI) | ✅ Active (2026-02-27). Serves collected static in-process; replaces nginx's static role. Note: PyPI also carries an unrelated 2015 package named `white-noise` — the correct one is `whitenoise`. |
| **django-q2** (1.11.1) — deferred to M2 | MIT (PyPI + GitHub `GDay/django-q2`) | ✅ Audited now so M2 can land it without a fresh audit: active (v1.11.1 2026-08-26, quarterly release cadence), and its Postgres cluster support means no Redis service ever. Replaces celery + django-celery-beat + the redis broker when it lands. |
| **uv** (0.12.10) — build tooling only | MIT OR Apache-2.0 (PyPI) | ✅ Not a runtime dependency (installed in the Docker build stage); provides the lockfile + install speed. Very active (2026-09-04). |
| Django 6.1.1 (was 5.2.16) | BSD-3-Clause | ✅ Same verdict as §4.1, current stable (6.1.1 released 2026-09-02). |
| psycopg[binary] 3.3.5 (was 3.2.9) | LGPL-3.0-only | ✅ Same verdict as §4.1 (LGPL-3.0 links fine into AGPLv3; the binary wheel is a dynamically-loaded extension module). |
| requests 2.34.2 / mistune 3.3.4 / django-csp 4.0 / environs 15.2.0 | Apache-2.0 / BSD-3-Clause / BSD / MIT | ✅ Licenses unchanged from the §4.1 verdicts; pins refreshed. |
| mypy 2.3.1, ruff 0.16.6, pytest 9.1.1 stack, responses 0.26.3 (dev group) | MIT / MIT / MIT + Apache-class / Apache-2.0 | ✅ Dev-only; licenses unchanged from the §4.1 dev-group verdict. |
| gunicorn, celery[redis], django-celery-beat, redis, hiredis | — | ⚠️ **Removed from the M1 stack** (§3.9). The §4.1 verdicts remain valid if any is ever re-introduced; none are license-blocked. |

### 4.5 M2 additions (2026-09-09)

| Package | License (verified) | Verdict |
|---|---|---|
| **django-picklefield** (3.4.0) — new, transitive via django-q2 | MIT (PyPI `license_expression`) | ✅ Small field helper django-q2 uses to serialize task arguments; AGPLv3-compatible. |

## 5. Build plan (milestones for this repo)

Sizing note: federation-from-spec (M4) is the largest single chunk of new code; it was inherited for free in the legacy fork and must be written here against the W3C specs.

- **M0 — Dev environment & skeleton** *(2026-09-05)*: repo scaffolding — `pyproject.toml` (lean audited dep set), Dockerfile, docker-compose stack, entrypoint with auto-migrate+collectstatic, `.env.example` + `setup.sh`, minimal Django project that passes `manage.py check` and a smoke pytest. ✅ done; the original 6-service shape was restructured the same day by the stack audit (§3.9) into the current web+db stack — see §6.
- **M1 — Core film domain (first working version, part 1)**: User/auth (signup/login, minimal), Film model + migrations (+ MergedFilm, tsvector trigger), Shelf/ShelfFilm with the binary defaults, Status/Comment/Review/ReviewRating with the watch-state & review rules of §3.3, film pages (create/edit/view, shelve controls, finish flow with rating requirement), user films page (3 tabs), minimal feed, setup wizard, admin basics. **Stack: web + db only** (§3.8) — no new services or runtime deps are needed for M1; the frontend is Django templates + vanilla JS (§3.9). **Exit bar: owner can sign up on a local instance and run the whole watchlist→watched→review loop.**
- **M2 — TMDB integration (first working version, part 2)**: TMDB client, global search as primary catalog + click-through + one-click Watchlist (D6/D7), suggest endpoint + dropdown, metadata lock for TMDB films (D4), backfill task (D11). **Stack: the `worker` service joins here** — Django-Q2 `qcluster` from the same image, Postgres cluster, per §3.9 (dependency already license-audited in §4.4). **Exit bar: the owner's day-to-use flow works — search "Blade Runner", add to watchlist, mark watched with a rating.**
- **M3 — File import/export**: TMDB-CSV import (D9) + export round-trip (D10), wired to the async backfill. Exit bar: owner's real TMDB export imports cleanly and re-imports as a no-op.
- **M4 — Federation (ActivityPub from spec)**: webfinger/nodeinfo, Person & Film wire types, inbox/outbox, follow/unfollow, status Create/Update/Delete broadcast, remote-user mirrors, graceful ignore of unknown types. Exit bar: two local ReelTalk instances follow each other and exchange reviews + film objects; a third-party AP tool (e.g. a Mastodon instance) can follow a ReelTalk user's Person without breaking.
- **M5 — Social surface**: lists (+group lists/curation), groups, notifications, directory/discover, RSS, moderation/reports, 2FA + email verification, antispam hardening.
- **M6 — Polish & public deploy**: artwork with the owner (D17), guided tour decision, daily `pg_dump` backup job, real-domain deployment of the same web+db(+worker) containers behind the operator's TLS proxy (§3.8 — no in-project proxy), first public instance.

**First working version = M1 + M2.** Federation (M4) is what makes it a *federated* product; everything before that must stand alone as a useful local film tracker.

## 6. Dev environment (set up at M0, restructured by the stack audit 2026-09-05)

Layout in this repo:

```
pyproject.toml          # AGPLv3-audited dependency set ([project.dependencies] + dev group)
uv.lock                 # committed lockfile — Docker builds use `uv sync --frozen`
Dockerfile              # python:3.13-slim, uv venv at /app/.venv, non-root appuser;
                        # one image serves web (and the M2 worker role)
docker-compose.yml      # web (uvicorn on :3030), db (postgres:17) — the whole stack
entrypoint.sh           # auto migrate + collectstatic on web start
.env.example / setup.sh # config template + secret generation
manage.py               # Django entry point
reeltalk/               # Django project: settings, urls, asgi (+ app code from M1 on)
reeltalk/tests/         # pytest suite (smoke test for now)
```

Run it:

```sh
cp .env.example .env && ./setup.sh        # one-time: set DOMAIN + generate secrets
docker compose up -d --build              # start the stack
curl -4 http://localhost:3030/            # IPv6 docker-proxy quirk → use -4
```

Host-quirk reminders (binding on every session in this repo):
- **Bind mounts need `:z`** on this SELinux host (compose uses named volumes; ad-hoc `docker run -v` one-offs still need the label).
- **Test with `curl -4`** — IPv6 `[::1]` through docker-proxy resets HTTP connections.
- **Artifact volumes are re-initializable, data volumes are not.** If the image's runtime user changes (or static files get wiped), delete `static_volume`/`media_volume` and let them re-init from the image; never touch `pgdata`.
- Test flow: `docker compose run --rm web pytest` (the dev group is installed in the image); lint/format: `ruff check . && ruff format --check .` (run inside the container, where the venv lives).

## 7. Handoff notes for the next session

1. Start at **M1** (§5) on the audited stack — web + db, uvicorn/Django 6.1/Python 3.13/uv (§3.8/§3.9); do not re-audit or re-grow it mid-milestone. The functional contract to implement against is §3 + the decision table in §2 — those are the owner's settled requirements; anything not listed there is a new design decision to make *with the owner* (D17: no solo design decisions on UX/artwork).
2. Clean-room discipline while working: it is fine to *read* legacy code to confirm intended behavior; it is not fine to copy. When in doubt, describe the behavior in your own terms and implement from the description + specs.
3. Keep a `PROGRESS.md` in this repo as work happens (the legacy one is the model for what it should contain: state table, execution records with commit hashes, test baselines, decision log). Update it at each milestone boundary.
4. Test baseline discipline: record the green suite count after each milestone; CI-faithful runs happen in Docker (see §6).
5. The legacy stack is torn down and port 3030 is owned by this project's compose file. Legacy named volumes (`reeltalk-work_*`) are kept as insurance until the new instance is proven, then `docker volume rm`.
