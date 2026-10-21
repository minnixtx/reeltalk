# Security fix plan

Inputs: `SECURITY-AUDIT.md` (findings F1-F12, 2026-10-20) and the VPS
hardening pass recorded in `HARDENING.md` (applied on the reeltalk.dev VPS,
2026-10-20). One increment per session. Each increment ends at a green,
committed checkpoint.

## Critical context: the repo and production have diverged

The hardening pass changed application code **on the VPS only, uncommitted**.
Verified against the local tree on 2026-10-20:

| VPS (per HARDENING.md) | Local repo |
|---|---|
| `reeltalk/public_http.py` SSRF guard | does not exist |
| django-csp enabled (v4 format) | not wired; no CSP header (verified live) |
| HSTS / HTTPS-redirect / strict-cookie settings in `.env` | no such settings in `settings.py` |
| Pillow 13.0.0, urllib3 2.8.0, certifi 2026.10.19 | Pillow 12.3.0, certifi 2026.7.22 |
| compose: db gets DB-only env; caps dropped; no-new-privileges; resource ceilings | db gets full app `env_file`; no hardening keys |
| `.gitignore` ignores all `.env.*` except `.env.example` | only `.env`, `.env.dev`, `.env.b` |

So increment S1 is not optional polish: it is how production code and the
shipped code stop being different programs.

Captured 2026-10-20 (audit session, read-only): the VPS sits on the same
commit as local (`19696d3`), and the full diff plus both new files are saved
under `.qwen/tmp/vps-hardening/` (`vps-hardening.diff`, `public_http.py`,
`test_public_http.py`). The VPS tree does ship tests for the new guard.

## Ground rules (every increment)

- The gate's pytest runs the **baked image**: rebuild (`docker compose build`)
  before running tests after any source edit.
- Per increment: targeted pytest + ruff green. The full gate only on the
  owner's explicit signal.
- Commit per increment, no co-author trailer. Rewrite the `PROGRESS.md` NOW
  block as the last step of the increment's commit.
- Deployer-agnostic: fixes land in shipped code with safe no-config defaults.
  Host infra (nftables, sshd, sysctl, logrotate, nginx realip/timeouts,
  backups, certbot, the host-side DB role split) stays out of the repo.
- The VPS is production: read-only access unless the owner approves a change.
- The LAN Mastodon peer (192.168.1.x) is the federation test partner; any
  new outbound guard must keep working against it, via an opt-in if the
  default is deny-private.
- No em dashes in user-facing text.

## S1 - Land the VPS hardening diff (closes F4, F5)

Prereq done: the diff is captured under `.qwen/tmp/vps-hardening/` (see the
note above) and VPS access is recorded in memory (`minnix@37.60.246.66`,
read-only). Apply the captured diff (it is against `19696d3`, local HEAD, so
`git apply` is clean) plus the two new files, then fix the review findings
below before committing.

1. Verified sound as written: every outbound call site routes through
   `public_http` (delivery, mirrors x3, statuses, tmdb x2 - re-grep for
   bare `requests.get/post` after applying); single DNS resolution with the
   socket pinned to the checked literal (rebinding safe); mixed
   public/private DNS rejected; TLS keeps hostname SNI and verification;
   `trust_env=False` closes proxy egress; signed POSTs never follow
   redirects; the 8 MiB cap applies after decompression; the new pytest env
   pins keep tests hermetic; HSTS/redirect/cookie settings default off.
2. Must fix before committing:
   - **LAN federation opt-in.** `public_http` hard-rejects every non-global
     address, so LAN federation (this network's Mastodon peer at
     192.168.1.85, the test instance at 192.168.1.40, and any deployer
     federating on a LAN) breaks the moment this lands. HARDENING.md calls
     private destinations "intentionally unavailable"; that is the wrong
     default for a federated app whose peers can share a network. Add an
     env opt-in (boolean allow-private, or a peer CIDR/domain allowlist;
     default deny) and document it in `.env.example`. The unit tests mock at
     the socket/adapter layer, so the suite stays green - the break only
     shows in a live exchange, which is why the owner's LAN federation test
     is part of S1's exit.
   - **Compose host binding.** The VPS compose hardcodes
     `127.0.0.1:3030:3030`, dropping the shipped `${WEB_PORT:-3030}` host
     mapping. Loopback-only is this VPS's topology (nginx runs on the host);
     a fresh deployer without a local proxy would lose external access and
     `WEB_PORT` would stop working. Parameterize the bind (e.g. `WEB_BIND`
     default empty = all interfaces, `127.0.0.1` set in the VPS `.env`) and
     keep `${WEB_PORT:-3030}`.
   - **CSP middleware position.** `csp.middleware.CSPMiddleware` is last in
     `MIDDLEWARE`, so short-circuit responses from earlier middleware (the
     SSL redirect, once `SECURE_SSL_REDIRECT` is on) go out without the CSP
     header. Move it next to `SecurityMiddleware` and assert the header on
     both a 200 and a 301.
   - **public_http nit.** `ipaddress.ip_address(address[0])` raises
     `ValueError` (not caught by the `except OSError`) on scoped IPv6
     literals like `fe80::1%eth0`; guard it.
   - **Healthcheck coupling (keep, with a comment).** The healthcheck now
     sends `X-Forwarded-Proto: https`; that is required once
     `SECURE_SSL_REDIRECT=true`, or the plain-HTTP probe gets a 301 and the
     container reads unhealthy forever. Note the pairing in `.env.example`.
3. Tests to add: CSP header presence (200 and 301); the LAN opt-in once its
   shape is chosen. The VPS already ships `test_public_http.py` (blocked
   addresses, rebinding, mixed DNS, redirect block, POST no-redirect, size
   cap). Pillow 12 to 13 is a major bump: run the avatar/poster/image tests.
4. Also commit `SECURITY-AUDIT.md`, this plan, and `SECURITY-HANDOFF.md`
   (first commit of the session).

Exit: targeted pytest + ruff green, committed, NOW block rewritten.
Owner follow-up after deploy: admin login, verification email, film search,
one federation exchange with the LAN Mastodon peer (first live test of the
guard plus opt-in). After merge, the VPS should redeploy from the repo so
production and the shipped tree stop diverging (owner's call).

## S2 - F1: sanitize remote HTML on ingest

The worst open finding; independent of S1 but sequenced after it because S1
touches the same files.

- `activitypub/statuses.py`: run `sanitize_html()` on (a) Note `content` in
  `_mirror_status`, both the create and the update branch
  (`_apply_note_fields`), (b) Film `summary` in `_mirror_film`, (c) list
  extension `summary` in `_mirror_list` (create and `_apply_list_fields`).
  Pattern to follow: `mirrors.py` Person summary.
- Tests: inbox-deliver a Note with `<script>` and `<img onerror>` and assert
  the stored row is escaped (bleach escapes rather than strips); same for a
  Film mirror summary and a list description; assert the moderation queue
  page renders the stored value safely.

## S3 - F2 + F3: federation ingest integrity

- `_mirror_status` update branch and `handle_delete`: drop (and log) when
  `existing.user_id != sender.pk`.
- `fetch_person_document`: require `doc["id"]` to equal the requested URL
  (fragment-stripped), else `RemoteFetchError`.
- Tests: forged Update from a second verified sender is ignored; forged
  Delete is ignored; a Person doc whose id does not match the fetched URL
  is rejected; honest Update/Delete still work (regression guard).

## S4 - F6: fail-fast on SECRET_KEY

- `settings.py`: when `DEBUG` is false and `SECRET_KEY` is unset, empty, or
  equal to the fallback constant, raise `ImproperlyConfigured` pointing at
  `setup.sh`.
- Tests: boot under `DEBUG=False` without a key raises; with a real key it
  does not. Verify the documented deploy paths (setup.sh, prebuilt) still
  come up clean.
- Fold in any settings hygiene S1 surfaced (e.g. `.env.example` gaps).

## S5 - F7 + F8: inbox hardening

- App-side inbox body cap (the nginx 1 MiB cap is operator-side; the shipped
  app needs its own bound before `json.loads`).
- Per-source floor on inbox POSTs reusing the `attempts.py` table pattern
  (generous limit; the shared inbox legitimately receives from many peers).
- `DeliveredActivity` retention prune (management command; document cron).
- Require body coverage in signatures: `content-digest` among components for
  RFC 9421 POSTs with a body; `Digest` required on the legacy path.
  Check Mastodon interop first - the LAN peer is the reference; owner drives
  the live federation check before merge.

## S6 - Cleanup pass + finish the audit

- F10 invite code out of the query string, F11 CSV export formula guard
  (prefix `=` cells), F12 notes (document at-rest key risk in DEPLOYING;
  version-disclosure decision).
- Audit the surfaces the first pass did not cover: `social/admin.py`,
  `lists/models.py` + `services.py`, `mentions/parser.py`, notifications
  views, `collections.py`, `objects.py`, `broadcast.py`, `verify.py`.
  Append any findings to `SECURITY-AUDIT.md` as F13+ and extend this plan
  if material.

## Out of scope (operator infra, per the lean-scope decision)

nftables/SSH/sysctl/logrotate work, nginx realip and rate limits, TLS,
backups, certbot, host-side DB roles, Cloudflare dashboard settings.

## Open questions for the owner

1. LAN opt-in shape for `public_http` (S1 must-fix): boolean allow-private
   or a peer CIDR/domain allowlist; default deny either way. Confirm the
   env name.
2. `WEB_BIND` shipped default (S1): all interfaces, loopback via `.env`?
3. Pillow 12 to 13 major bump: accept as-is or hold for patch releases?
4. DB role split (`reeltalk_app` + bootstrap): the captured diff only
   separates the db container's env; the role split itself is host-side.
   Ship it in the default flow or document it as an ops recipe?
