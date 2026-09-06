# ReelTalk (AGPLv3 rewrite) — Progress Tracker

**Last updated:** 2026-09-06
**Audience:** any new session picking up this project. Read [REWRITE.md](REWRITE.md) first (the binding clean-room rules), then [PLAN.md](PLAN.md) (functional spec + build plan + license audit), then this file for current state.

---

## 1. Current state

| Area | State |
|---|---|
| M0 — functional spec, license audit, dev environment | ✅ Done 2026-09-05, verified (stack healthy, site on :3030, pytest green, ruff green) |
| Stack audit (pre-M1) | ✅ Done 2026-09-05 — stack restructured to web+db; findings in PLAN.md §3.9, decisions R1–R8 below |
| M1 — core film domain (first working version, part 1) | 🔄 In progress — increment 1 of ~7 done: core app + Film domain (commit `cbf72c2`). **Next session starts at increment 2: social app + custom User model + auth + setup wizard.** Plan for the increments is in §2 below. |
| M2 — TMDB integration | ⬜ Not started |
| M3 — file import/export | ⬜ Not started |
| M4 — federation (ActivityPub from spec) | ⬜ Not started |
| M5 — social surface | ⬜ Not started |
| M6 — polish & public deploy | ⬜ Not started |

Milestone definitions and exit bars: [PLAN.md §5](PLAN.md).

## 2. Execution record

### M1 — increment plan (forward-looking; ~7 increments across sessions)

M1 = "core film domain, first working version, part 1" (PLAN.md §5). It is built as small verified increments; **each ends in a committed green checkpoint** (pytest + ruff clean) with this file updated, then the session stops. Scope per increment:

1. ✅ **Core app + Film domain** — `reeltalk.core`: Film + MergedFilm models, `sort_title`, D7 dedup (`find_match`), tsvector trigger. *(done 2026-09-06, `cbf72c2`.)*
2. ⬜ **Social app + User + auth** — `reeltalk.social`: custom `User` model set as `AUTH_USER_MODEL` (R10), signup/login/logout views, first-run setup wizard. Migrations + tests.
3. ⬜ **Shelf/ShelfFilm + binary defaults** — Shelf + ShelfFilm models; default shelves (`to-read`=Watchlist, `read`=Watched) created on user save; the full merge/absorb *logic* (re-point shelves/statuses onto a canonical Film) lands here now that related models exist.
4. ⬜ **Status/Review/ReviewRating + watch rules** — Status base + Comment/Review/ReviewRating subtypes; §3.3 rules: rating required to mark watched, one Review per user per film, rating-only ReviewRating.
5. ⬜ **Film pages + shelve controls + finish flow** — create/edit/view film views + templates; shelve/unshelve; "mark watched" finish flow that enforces the rating requirement; edit-review.
6. ⬜ **User films page (3 tabs) + minimal feed** — All / Watchlist / Watched tabs on the user's films page; minimal home feed/timeline.
7. ⬜ **Admin basics + landing/about + exit-bar verification** — admin registration polish, landing/about page, end-to-end check of the full loop, final PROGRESS.md.

**M1 exit bar (across all sessions):** owner can sign up on the local instance and run the whole watchlist→watched→review loop. No new runtime deps or services may be introduced in M1 (web + db only; Django templates + vanilla JS). Conventions to follow are recorded in §4 — especially R9 (app structure), R10 (custom user model now), R11 (minimal neutral CSS; real styling deferred to M6 with the owner per D17).

### M0 (executed 2026-09-05)

**Spec + audit:** `PLAN.md` written — functional spec distilled from the legacy `PROGRESS.md` feature inventory, a walk of the legacy codebase (Film model, `tmdb.py`, AP wire types, deployment files), and the owner decision log (legacy §8 #1–34, renumbered D1–D17 in PLAN.md §2). License audit of every legacy dependency (PyPI metadata + GitHub where sparse) and every static asset/font — verdicts in PLAN.md §4. Key results: **no dependency is license-blocked**; `bw-file-resubmit` (the flagged first target) is **MIT** → compatible, but a native ~20-line widget is recommended instead; the icomoon icon font and all BookWyrm artwork are ❌ do-not-carry-over.

**Dev environment:** repo scaffolding — lean audited `pyproject.toml` (15 main deps, 9 dev), two-stage `Dockerfile`, `docker-compose.yml` (nginx :3030 / web / db postgres:17 / redis / celery_worker / celery_beat; project name `reeltalk` so volumes are `reeltalk_*`, no collision with the kept legacy `reeltalk-work_*` insurance volumes), `entrypoint.sh` (auto migrate + collectstatic on web start), `.env.example` + `setup.sh`, fresh plain-HTTP `nginx/default.conf` (no proxy cache in v0.1; `X-Forwarded-Proto $scheme` passthrough), minimal Django project (`reeltalk/` settings/urls/wsgi, `celerytalk/`) + smoke test.

**Verification:** `docker compose up -d --build` → all services healthy; entrypoint applied migrations (admin/auth/contenttypes/sessions/django_celery_beat) + collectstatic; `curl -4 http://localhost:3030/` serves the index through nginx; `/admin/login/` 200; in-container `pytest` **1 passed** (smoke); `ruff check` + `ruff format --check` green.

**Gotcha hit & fixed:** first build failed at web start — `ENTRYPOINT ["/entrypoint.sh"]` but `COPY . /app/` only placed the script at `/app/entrypoint.sh`. Fixed with an explicit `COPY entrypoint.sh /entrypoint.sh` (legacy had the same shape).

**Test baseline:** 1 passed (smoke only — real coverage starts with M1).

### Stack audit (executed 2026-09-05, commit `95b332c`)

Per AUDIT-BRIEF.md (consumed + deleted in that commit): audited the M0 stack against current best practice and applied changes in-repo. Full findings table + rejected alternatives: PLAN.md §3.9; license verdicts for new/changed deps: PLAN.md §4.4.

**What changed (applied):**
- **Python 3.11 → 3.13**, pip → **uv** with committed `uv.lock` (51 packages, resolves in <1 s).
- **Django 5.2.16 → 6.1.1** (current stable; adopted the new `MAILERS` setting — `EMAIL_*` is deprecated and cannot coexist with `MAILERS`).
- **gunicorn/WSGI → uvicorn/ASGI** (`reeltalk/asgi.py`; `wsgi.py` removed).
- **Task queue deferred to M2**: celery + django-celery-beat + redis broker removed from deps/compose; when M2 lands, **Django-Q2 on a Postgres cluster** (license-audited now, §4.4) replaces all three — no Redis service at any milestone.
- **Redis removed** entirely: LocMemCache + DB sessions.
- **nginx removed**: whitenoise serves static in-process; media via a `serve` URL pattern; web exposes :3030 directly (D14 operator TLS unchanged).
- **Dockerfile**: lockfile-first layer caching, venv at /app/.venv, **non-root `appuser`**.
- **Toolchain**: ruff 0.16.6 (config: +`I`/`UP` rules, legacy-carried ignores dropped), mypy 1.7.1 → 2.3.1 + django-stubs[compatible-mypy] 6.1.0, pytest 9.1.1 stack, responses 0.26.3; pins bumped (requests 2.34.2, mistune 3.3.4, django-csp 4.0, environs 15.2.0, psycopg 3.3.5).
- **Compose**: 6 services → **2** (`web`, `db`); M2 adds a third (`worker`, same image).

**Gotchas hit (fixed, documented in PLAN.md §3.9):** uv silently skips a custom `[dependency-groups] main` (PEP 735 reserves the name — the first build installed only the dev group; runtime deps moved to `[project.dependencies]`); Django 6.1 rejects `MAILERS` alongside any `EMAIL_*` module attribute; root-owned artifact volumes broke the non-root switch (verified `static_volume`/`media_volume` held only regenerable files, then deleted + re-initialized them, plus removed the orphaned `redis_data`).

**Test baseline:** before = 1 passed (smoke), `curl -4 /` + `/admin/login/` 200, ruff green. After = **1 passed**, all endpoints 200 incl. whitenoise-served static, ruff check + format green. Suite count unchanged by design — real coverage starts with M1.

**Decisions:** R1–R8 in §4 below (none owner-blocking; recorded for the record).

### M1 increment 1 — core app + Film domain (executed 2026-09-06, commit `cbf72c2`)

First increment of M1. New `reeltalk.core` app with the flat **Film** model (PLAN.md §3.2, D2) and **MergedFilm**. No new runtime deps — everything used was already in the audited set (`django.contrib.postgres` ArrayField is built into Django).

**What landed:**
- `Film`: title / `sort_title` (auto-derived on save — leading article stripped, lowercased) / subtitle / description (HTML) / year / runtime / `genres`+`directors`+`cast` as Postgres `ArrayField(text[])` (plain name-lists per D2) / poster / `tmdb_id`+`imdb_id` dedup identity / `origin_id`+`remote_id` (M4 federation identity, present but unused until M4).
- `Film.find_match()` — the D7 dedup order: exact `tmdb_id`, then `imdb_id`, then normalized title+year. Returns the row to match/backfill onto, else None.
- `MergedFilm` (old_id→new_id) + `resolve_film_id()` — keeps absorbed-film URLs redirecting (§3.2). The full merge/absorb *logic* (re-pointing shelves/statuses) is deferred to the Shelf increment (needs those models); only the id-mapping + resolution landed here.
- `search_vector`: a **trigger-maintained Postgres tsvector column** added by raw-SQL migration `0002` — deliberately NOT a Django model field, so the ORM never reads/writes it and can't clobber it. Weights per §3.2: A=title, B=subtitle, C=directors+cast, D=genres; `simple` text-search config (titles are proper nouns — no stemming). M2's local search will query it via RawSQL.
- Settings: registered `reeltalk.core` + `django.contrib.postgres`; added a shared top-level `templates/` dir (repo root) to TEMPLATES DIRS; the project package `reeltalk` is no longer itself an app.

**Workflow note for future sessions (this box):** the image has **no live source mount** (`COPY . /app/` at build), so every code change needs `docker compose build web` before the container/tests see it. Fast loop that works:
- Generate migrations locally (no rebuild): `docker run --rm -v /home/minnix/reeltalk:/src:z -w /src --entrypoint /app/.venv/bin/python reeltalk-web manage.py makemigrations <app>` (the `failed to resolve host 'db'` warning is harmless — makemigrations doesn't need the DB).
- Lint/format local files without a rebuild: same `/src` mount with `--entrypoint /app/.venv/bin/ruff reeltalk-web check --fix --no-cache .` (and `format --no-cache .`). `--no-cache` is required — the bind-mounted `.ruff_cache` isn't writable by the container user.
- Then `docker compose build web` and `docker compose run --rm web bash -c 'ruff check . && ruff format --check . && pytest'`.
- If a test-DB migration fails mid-run, a stale `test_reeltalk` DB is left behind: drop it with `docker compose exec -T db psql -U reeltalk -d postgres -c 'DROP DATABASE IF EXISTS test_reeltalk;'` before re-running.

**Test baseline:** before = 1 passed (smoke). After = **21 passed** (19 new: sort_title derivation, D7 dedup paths, merge-id resolution, tsvector trigger populate + recompute), ruff check + format green, `makemigrations --check` reports no drift, migrations apply to dev DB (`core_film`, `core_mergedfilm` + trigger present), site HTTP 200.

**Decisions:** R9–R11 in §4 below (engineering calls, none owner-blocking; recorded so the owner can veto).

## 3. Host facts (this box)

- Fedora 44, Docker via dnf; compose project **`reeltalk`**, port **3030** owned by this stack (legacy stack torn down 2026-09-05).
- Legacy insurance: six named volumes `reeltalk-work_*` kept until the new instance is proven, then `docker volume rm`. Pre-freeze DB dump at `/home/minnix/backups/reeltalk/pre-freeze-20260905.sql.gz`.
- Binding host quirks (all in PLAN.md §6): `:z` on bind mounts; `curl -4` (IPv6 docker-proxy resets); artifact volumes (`static_volume`, `media_volume`) re-initialize from the image after a runtime-user change — never touch `pgdata`. (The nginx-restart-after-web-rebuild and rebuild-all-celery-images quirks are gone with those services.)
- Resource constraint: local LLM inference on a separate server — at most ONE background subagent at a time; serialize heavy docker/pytest runs.

## 4. Decision log (rewrite-era)

New owner decisions for the rewrite are recorded here, numbered R1, R2, … The legacy decision log (its §8 #1–34) remains binding and is distilled as D1–D17 in PLAN.md §2 — do not re-litigate those. Stack-audit decisions below were taken with full discretion per the audit brief; none are product-visible or hard to reverse, so they are recorded rather than blocking.

- **R1 — Python 3.13 + uv.** Runtime on 3.13 (3.14 is current stable but only ~11 months out); uv for lockfile hygiene + build speed (`uv.lock` committed). Replaces M0's 3.11/pip.
- **R2 — Django 6.1 over staying on 5.2 LTS.** Current stable (6.1.1, 2026-09-02); zero migration cost on a fresh codebase, while 5.2's security EOL (2028-04) would force the same move within a year. New `MAILERS` setting adopted from day one.
- **R3 — uvicorn/ASGI replaces gunicorn/WSGI.** Django's forward path (async views, future WebSockets); no async views needed yet — sync code runs fine under ASGI.
- **R4 — Task queue deferred to M2, then Django-Q2 on Postgres.** M1 has zero background tasks; when the TMDB backfill lands (D11), Django-Q2 (MIT, active) with a Postgres cluster replaces celery + django-celery-beat + the redis broker in one package, its built-in scheduler covering periodic jobs. Rejected: Celery (inertia — 3 packages + Redis), ARQ (needs Redis), Dramatiq (LGPLv3+, heavier model), plain async views (no retry/survival for a ~15-min job).
- **R5 — No proxy, no Redis in the stack.** whitenoise serves static in-process and web exposes :3030 directly (D14 operator TLS unchanged); LocMemCache + DB sessions. nginx + redis services removed; artifact volumes re-initialized for the non-root image.
- **R6 — Frontend: vanilla JS, no framework, no build step** for v0.1 (search-as-you-type + one-click watchlist are ~50 lines of trivial JS). HTMX/Alpine rejected at this surface size; revisit if M5+ grows it.
- **R7 — M4 federation written from spec.** No maintained Python ActivityPub library exists (verified 2026-09-05: PyPI's `activitypub` is 2018-era, Fedify is TypeScript); clean-room rules already mandate building against the W3C specs.
- **R8 — Toolchain refresh.** ruff 0.16.6 (config modernized: +isort/+pyupgrade, legacy-carried ignores dropped), mypy 1.7.1 → 2.3.1 + django-stubs[compatible-mypy] 6.1.0, pytest 9.1.1 stack, responses 0.26.3.
- **R9 — App structure: `reeltalk.core` + `reeltalk.social`.** The project package `reeltalk` is no longer itself an app; real code lives in sub-apps split by domain — `core` (Film/MergedFilm/Shelf/ShelfFilm) and `social` (User/Status, lands increment 2). M4 federation will add a third (`reeltalk.activitypub`). Shared cross-app templates live at the repo-root `templates/` dir (TEMPLATES DIRS), not inside the project package.
- **R10 — Custom user model now.** `reeltalk.social.User` will be set as `AUTH_USER_MODEL` in increment 2, before any social migrations exist — it is very hard to change after the first migration. Defining it up front (with localname/display_name/summary/avatar/local-vs-remote + follow/block relations) keeps M4 federation clean instead of bolting on a separate profile model.
- **R11 — Minimal original CSS for M1; visual design deferred to M6 with the owner (D17).** M1 pages use a small, deliberately neutral hand-written stylesheet (original AGPLv3 code) just enough to make forms/lists legible — not a design statement and no third-party framework. The Bulma re-vendoring option (license-cleared in PLAN.md §4.2) and all real styling/artwork happen at M6 *with* the owner per D17. Owner may veto; nothing here is hard to reverse.
