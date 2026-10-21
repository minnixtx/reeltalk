# Security audit handoff

Point the next session at this file. Everything needed is in the repo; no
production access is required to start increment S1.

## State (as of the 2026-10-20 audit session)

- Audit complete: findings F1-F12 in `SECURITY-AUDIT.md`. Worst: F1 stored
  XSS via remote ActivityPub HTML (unsanitized on ingest, rendered `|safe`),
  F2 forged Update/Delete accepted from any verified sender, F3 a Person
  document's id is never checked against the URL it was fetched from.
- Fix plan complete: `security-plan.md`, increments S1-S6, one per session.
- No fixes have been applied to the repo yet. This session's only repo
  changes are untracked docs (`SECURITY-AUDIT.md`, `security-plan.md`, this
  file) and the capture under `.qwen/tmp/vps-hardening/`.
- VPS recon complete: production (`/srv/reeltalk`, same commit `19696d3` as
  local) carries uncommitted hardening code - the `public_http.py` SSRF
  guard, django-csp wiring, HSTS/HTTPS-redirect settings, compose
  hardening, and dependency bumps. The full diff plus both new files are
  captured under `.qwen/tmp/vps-hardening/` (`vps-hardening.diff`,
  `public_http.py`, `test_public_http.py`).
- The captured diff was reviewed; the verdict is folded into S1 of the plan.
  Most of it is sound, but two must-fix defects stand out before it lands:
  1. `public_http.py` hard-rejects every non-global IP with no opt-in, so
     LAN federation (Mastodon peer 192.168.1.85, test instance
     192.168.1.40) breaks the moment it ships.
  2. The VPS compose hardcodes `127.0.0.1:3030:3030`, dropping the shipped
     `${WEB_PORT:-3030}` host mapping; a fresh deployer without a local
     proxy loses external access.
  Plus two nits: `CSPMiddleware` sits last in `MIDDLEWARE` so short-circuit
  responses (e.g. the SSL redirect) skip the CSP header, and
  `ipaddress.ip_address` can raise an uncaught `ValueError` on scoped IPv6
  literals like `fe80::1%eth0`.

## What the next session should do

Execute increment S1 from `security-plan.md`: apply the captured diff, apply
S1's must-fix corrections and nits, add the listed tests, and commit
`SECURITY-AUDIT.md` + `security-plan.md` + `SECURITY-HANDOFF.md` as the
session's first commit. Then S2 (F1 sanitization) in the session after that.

## Read in this order

1. `security-plan.md` - ground rules first, then S1.
2. `SECURITY-AUDIT.md` - findings detail with file:line evidence.
3. `.qwen/tmp/vps-hardening/` - the VPS code S1 lands (`vps-hardening.diff`,
   `public_http.py`, `test_public_http.py`).
4. `~/design-elements/HARDENING.md` - the VPS operator changelog; its infra
   half (nftables, sshd, sysctl, nginx, backups) stays out of the repo.

## Ground rules (full list in security-plan.md)

- Rebuild the Docker image before running pytest; the gate runs the baked
  image.
- Targeted pytest + ruff per increment. The full gate (~36 min) only on the
  owner's explicit signal.
- One commit per increment, no co-author trailer. Rewrite the `PROGRESS.md`
  NOW block as the increment's last step.
- Deployer-agnostic: safe no-config defaults in shipped code; host infra
  stays out of the repo.
- The VPS is production: read-only unless the owner approves a change
  (access: `minnix@37.60.246.66`, recorded in memory).
- No em dashes in user-facing text.

## Open questions for the owner

Also listed at the end of `security-plan.md`: the LAN opt-in shape and env
name for `public_http`, the shipped `WEB_BIND` default, accepting the
Pillow 12-to-13 major bump, and whether the DB role split ships in the
default flow or stays an ops recipe.
