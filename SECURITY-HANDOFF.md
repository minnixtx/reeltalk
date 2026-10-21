# Security audit handoff

> Status: S1 landed 2026-10-20 (`1b4ec4b`) and runs live on the dev box;
> the full gate is green as of 2026-10-21 (**2528 passed, 5 skipped**).
> This file now hands off increment **S2**. The `## NOW` block in
> `PROGRESS.md` is the current source of truth; everything below is the
> handoff as written at S1's close.

Point the next session at this file. Everything needed is in the repo; no
production access is required to start S2.

## State (as of the S1 close, 2026-10-21)

- S1 closed **F4 (SSRF)** and **F5 (CSP never sent)**:
  `reeltalk/public_http.py` fronts every outbound fetch (one DNS
  resolution, socket pinned to the checked literal, 8 MiB cap, redirects
  re-checked, signed POSTs never follow them); non-global destinations
  need a `FEDERATION_ALLOWED_NETWORKS` CIDR (default deny, multicast
  always refused, cloud metadata stays blocked); django-csp wired with
  `CSPMiddleware` above `SecurityMiddleware`; compose hardening plus
  parameterized `WEB_BIND`; Pillow 13 / urllib3 2.8.0 / certifi bump.
  The owner's four S1 decisions are recorded in `security-plan.md`
  ("Decisions", 2026-10-20).
- The dev-box instance (`reeltalk.minnix.dev`) runs S1 live: CSP header
  observed on real responses, worker qcluster proven by a consumed task,
  `FEDERATION_ALLOWED_NETWORKS=192.168.1.0/24` set in this box's `.env`.
- **VPS redeploy is deferred until the whole audit is finished (owner,
  2026-10-21).** reeltalk.dev still runs pre-S1 code. When it eventually
  redeploys from the repo, its `.env` needs `WEB_BIND=127.0.0.1` and
  `FEDERATION_ALLOWED_NETWORKS=192.168.1.0/24`.
- Gate baseline: **2528 passed, 5 skipped** (2026-10-21). One hermeticity
  fix rode with it: the pytest-env block pins
  `FEDERATION_ALLOWED_NETWORKS` empty (`92415ba`), because the gate
  container inherits the env_file `.env` and an operator's allowlist must
  never decide what the suite proves.
- Open findings: **F1 (S2, next)**, F2 + F3 (S3), F6 (S4), F7-F9 (S5),
  F10-F12 (S6). The DB role split ships in the default deploy flow and
  lands with S4 (owner decision).

## What the next session should do

Execute increment **S2** from `security-plan.md`: F1, sanitize remote HTML
on ingest - the worst open finding (stored XSS from any peer, including
into the moderator queue).

- `reeltalk/activitypub/statuses.py` (line refs verified 2026-10-21):
  run `sanitize_html()` on
  (a) Note `content` in `_mirror_status` (line 419) and its update branch
  `_apply_note_fields` (line 397),
  (b) Film `summary` in `_mirror_film` (line 188),
  (c) list extension `summary` in `_mirror_list` (line 725) and
  `_apply_list_fields` (line 711).
- Pattern to follow: `mirrors.py:281` - `user.summary =
  sanitize_html(summary)`, imported from `reeltalk.core.utils`.
- Tests (from the plan): inbox-deliver a Note with `<script>` and
  `<img onerror>` and assert the stored row is escaped (bleach escapes
  rather than strips); same for a Film mirror summary and a list
  description; assert the moderation queue page renders the stored value
  safely.

## Ground rules (full list in security-plan.md)

- Rebuild the Docker image before running pytest; the gate runs the baked
  image.
- Targeted pytest + ruff per increment. The full gate (~36 min) only on
  the owner's explicit signal.
- One commit per increment, no co-author trailer. Rewrite the
  `PROGRESS.md` NOW block as the increment's last step.
- Deployer-agnostic: safe no-config defaults in shipped code; host infra
  stays out of the repo.
- The VPS is production: read-only unless the owner approves a change,
  and all VPS work is deferred until the audit is complete.
- No em dashes in user-facing text.
- The suite must not inherit operator env: any setting `.env` may
  legitimately set that tests assume a default of belongs pinned in
  pyproject's pytest-env block.

## Read in this order

1. `security-plan.md` - ground rules first, then S2.
2. `SECURITY-AUDIT.md` - F1 evidence with file:line.
3. `reeltalk/activitypub/statuses.py` and the `mirrors.py` sanitize
   pattern.

No open questions for the owner going into S2; the four S1 decisions are
already recorded in `security-plan.md`.
