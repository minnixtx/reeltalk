# ReelTalk (AGPLv3 rewrite) — Progress Tracker

**Last updated:** 2026-09-05
**Audience:** any new session picking up this project. Read [REWRITE.md](REWRITE.md) first (the binding clean-room rules), then [PLAN.md](PLAN.md) (functional spec + build plan + license audit), then this file for current state.

---

## 1. Current state

| Area | State |
|---|---|
| M0 — functional spec, license audit, dev environment | ✅ Done 2026-09-05, verified (stack healthy, site on :3030, pytest green, ruff green) |
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

## 3. Host facts (this box)

- Fedora 44, Docker via dnf; compose project **`reeltalk`**, port **3030** owned by this stack (legacy stack torn down 2026-09-05).
- Legacy insurance: six named volumes `reeltalk-work_*` kept until the new instance is proven, then `docker volume rm`. Pre-freeze DB dump at `/home/minnix/backups/reeltalk/pre-freeze-20260905.sql.gz`.
- Binding host quirks (all in PLAN.md §6): nginx restart after any web rebuild; `:z` on bind mounts; `curl -4` (IPv6 docker-proxy resets); separate image tags per service — rebuild web + celery_worker + celery_beat together when Celery tasks change.
- Resource constraint: local LLM inference on a separate server — at most ONE background subagent at a time; serialize heavy docker/pytest runs.

## 4. Decision log (rewrite-era)

New owner decisions for the rewrite are recorded here, numbered R1, R2, … The legacy decision log (its §8 #1–34) remains binding and is distilled as D1–D17 in PLAN.md §2 — do not re-litigate those.

*(none yet)*
