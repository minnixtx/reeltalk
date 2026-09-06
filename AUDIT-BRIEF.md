# ReelTalk — Stack Audit Session (pre-M1)

> **Consumable document:** this is the brief for the audit session. Delete this file at the end of that session once its work is committed.

**Repo state:** `/home/minnix/reeltalk` at `d81dc8f` (M0: functional spec + license audit + working Docker dev stack). This session's one job: audit the planned stack against current best practice, modernize and simplify where justified, and update the spec in place. M1 (core film domain) starts the session after this one — your findings directly shape what gets built, so be decisive.

**Read first:** `REWRITE.md` (binding rules), `PLAN.md` (the spec being audited — §3.8 deployment shape, §4 license audit, §5 milestones, §6 dev environment), `PROGRESS.md` (state + host facts). The frozen legacy fork at `/home/minnix/reeltalk-legacy` is reference-only (clean-room) and ~5 years old — do not inherit its stack choices out of inertia.

**Current stack (what you're auditing):** Python 3.11 + pip; Django 5.2.16 LTS on gunicorn (WSGI); Celery 5.6 + django-celery-beat; Redis 7 (cache DB0 + broker DB1); Postgres 17; nginx 1.30 plain-HTTP :3030; pytest 9 / ruff 0.14.7 / mypy 1.7.1 (stale pin); Django templates only, no JS framework yet. Six compose services: `nginx`, `web`, `db`, `redis`, `celery_worker`, `celery_beat`.

## Authority

**Full discretion** to re-architect the stack within the hard constraints below — apply changes in-repo, don't just recommend. Where a choice is genuinely owner-facing (product-visible or hard-to-reverse), record it as a decision-log entry (`R#`) in `PROGRESS.md` and flag it in your final summary instead of blocking.

## Scope 1 — Modernize

Review at minimum:

- **Python version + package manager** — 3.12/3.13? uv vs pip (build speed, lockfile hygiene)?
- **Web server layer** — gunicorn/WSGI vs uvicorn/ASGI (+ Django async where it actually helps)?
- **Django version and foundation** — 5.2 LTS vs whatever is current; and whether Django remains the right foundation at all. Only swap with overwhelming evidence: admin, ORM, auth, and migrations are load-bearing for M1+.
- **Task queue** — note M1 has *zero* tasks; the first real tasks are the M2 TMDB backfill and M3 import backfill. "Defer entirely" and "replace" are live options (consider Django-Q2, ARQ, Dramatiq, plain async views, or nothing yet).
- **Redis necessity** at this scale — DB cache? local memory? one service doing three jobs?
- **Reverse proxy / static serving** — nginx vs Caddy vs no proxy in dev (Django `staticfiles`)?
- **Frontend interactivity layer** for the planned UX (search-as-you-type, one-click watchlist) — vanilla JS vs HTMX/Alpine.js/etc.?
- **ActivityPub tooling** — existing free-licensed Python AP libraries (maintenance status, license, spec coverage) vs writing from spec. Third-party OSS is clean-room compliant; federation is M4, so this can be lighter research.
- **Test/lint/type toolchain freshness** — the mypy 1.7.1 pin is years stale; ruff config; pytest plugins.
- **Dockerfile hygiene** — layer caching, image size, non-root user, base image choice.

## Scope 2 — Simplify

**Binding owner direction: grow the stack as needed.** Define the leanest per-milestone service set (M1 = `web` + `db` + whatever is truly required), state when each deferred service joins, and restructure compose/env/scripts/entrypoint to match. Separate "dev stack" from "public-deploy stack" explicitly in §3.8 if that's the cleaner model — the final deploy may still need nginx/celery; dev doesn't have to.

## Scope 3 — Update the spec in place

- Dated **"Stack audit (2026-09-XX)"** section in `PLAN.md`: findings table, what changed and why, options considered and rejected.
- Direct edits to **§4** (deps + license verdicts for any new dep), **§5** (per-milestone stack requirements), **§6** (dev environment runbook).
- `PROGRESS.md`: execution record with commit hashes + `R#` decision entries.
- Bar: a new session reading only `PLAN.md` must know exactly what stack to build M1 with.

## Hard constraints

- **AGPLv3:** every new/changed dependency gets a license verdict, same method as PLAN.md §4 (PyPI metadata + upstream confirmation). No non-free licenses.
- **Clean-room:** no code from the legacy repo. Free-licensed third-party OSS is fine once audited.
- **Owner decisions D1–D17** in PLAN.md §2 remain binding (notably D14 plain-HTTP :3030 + operator TLS, D13 English-only, D6/D8 TMDB key in `.env`).
- **Host:** Fedora 44; SELinux — `:z` on bind mounts; test with `curl -4`; local LLM inference on a separate server → at most ONE background subagent at a time, serialize heavy docker/pytest runs. Small throwaway experiments (a uv build trial, a Django 6 / Python 3.13 smoke container) are allowed — never run two heavy builds concurrently, and never leave the running stack broken without restoring it.
- **Baseline discipline:** record test counts before changing anything; after applied changes, rebuild + `curl -4 http://localhost:3030/` (or the new shape's equivalent) + pytest must be green before committing. The repo is clean at `d81dc8f` — that's your restore point.
- **Local commits only** as work progresses; do not push to any remote.

## Tools

- **searxng MCP web-search tools are available** for current best practice, project health, and alternatives — use them. Results may be sparse from this network (some upstream engines are degraded); vary phrasing before concluding "no results".
- GitHub via web/`gh` for repo health signals (release cadence, last commit, issue activity).
- PyPI JSON API (`pypi.org/pypi/<pkg>/json`) for license metadata.

## Definition of done

1. `PLAN.md` updated in place and internally consistent — a new session reading only it knows exactly what stack to build M1 with.
2. Applied repo changes keep the dev stack green; test baseline before/after reported.
3. `PROGRESS.md` updated (execution record + `R#` decisions).
4. Local commits; final summary: what changed, why, rejected alternatives, open owner decisions, next session = M1.
5. Project memory updated (`rewrite-m0-state.md` or a successor) so the following session inherits the new stack state.

## Tone

Creative but best-practice-anchored — prefer boring, actively maintained projects over novel ones; every change needs a "why now" and a maintenance-risk assessment; keeping a legacy-stack component is a valid finding if it's still best practice (Postgres probably stays).
