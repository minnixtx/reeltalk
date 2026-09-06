# ReelTalk (AGPLv3 rewrite) — Progress Tracker

**Last updated:** 2026-09-05
**Audience:** any new session picking up this project. Read [REWRITE.md](REWRITE.md) first (the binding clean-room rules), then [PLAN.md](PLAN.md) (functional spec + build plan + license audit), then this file for current state.

---

## 1. Current state

| Area | State |
|---|---|
| M0 — functional spec, license audit, dev environment | ✅ Done 2026-09-05, verified (stack healthy, site on :3030, pytest green, ruff green) |
| Stack audit (pre-M1) | ✅ Done 2026-09-05 — stack restructured to web+db; findings in PLAN.md §3.9, decisions R1–R8 below |
| M1 — core film domain (first working version, part 1) | ⬜ Next session starts here |
| M2 — TMDB integration | ⬜ Not started |
| M3 — file import/export | ⬜ Not started |
| M4 — federation (ActivityPub from spec) | ⬜ Not started |
| M5 — social surface | ⬜ Not started |
| M6 — polish & public deploy | ⬜ Not started |

Milestone definitions and exit bars: [PLAN.md §5](PLAN.md).

## 2. Execution record

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
