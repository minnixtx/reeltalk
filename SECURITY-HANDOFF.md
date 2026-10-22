# Security audit handoff

> Status: S3 landed 2026-10-21 (F2 + F3 closed, plus the Tombstone delete
> gap its live check caught) and the **targeted suite is green on it: 654
> passed** on the rebuilt image (the federation / activitypub / moderation
> set). The full gate has not been run since S2 (**2541 passed, 5
> skipped**, all four markers 0); when it next runs, expect **2553 passed,
> 5 skipped**. The `## NOW` block in `PROGRESS.md` is the current source of
> truth; everything below is the handoff as written at S3's close.

Point the next session at this file. Everything needed is in the repo; no
production access is required to start S4.

## State (as of the S3 close, 2026-10-21)

- S2 closed **F1 (stored XSS from any peer)**. Every remote HTML field
  (Note `content`, Film `summary`, list extension `summary`) runs through
  `core.utils.sanitize_html` via `_remote_html`, called once where the
  field is read off the wire, so create and update branches store identical
  checked bytes. bleach escapes what it refuses, so a hostile body stays
  readable as inert text. The link consequence is **settled, not open**
  (owner): with no allowed link domains, a link inside a remote post
  arrives as plain text. Do not re-open it as a defect.
- S3 closed **F2 (forged `Update`/`Delete`)** and **F3 (a fetched Person
  document's `id` was never checked against the URL it was fetched from)**.
  In `reeltalk/activitypub/statuses.py`, the update branch of
  `_mirror_status` and `handle_delete` now drop (and log) when the verified
  sender is not the row's author - the attribution rule credits a *new* row
  to the signer and said nothing about who may *mutate* an existing one.
  The drop is the module's graceful-ignore posture: 202, the peer's queue
  drains, the row stays exactly as its author left it. The guard sits above
  the list-extension read, so a forged list update is dropped by the same
  check. In `reeltalk/activitypub/mirrors.py`, `fetch_person_document`
  refuses a document whose `id` is not the URL it was fetched from
  (fragment-stripped), beside its other unusable-document guards - that id
  is what `mirror_user_from_person` keys the mirror on, so a mismatch was
  an impersonation lever against the unique `actor_url` slot. The live
  check that closed S3 also caught a **pre-existing interop gap**: Mastodon
  4.7.2 spells a deleted status as a `Delete` whose object is a `Tombstone`
  stub (`DeleteNoteSerializer` emits `{"type": "Tombstone", "id": <status
  url>}`), and `handle_delete` matched only `"Note"` - a real delete
  arrived, logged as handled, and changed nothing. The type gate now
  accepts `Note` or `Tombstone`; the id lookup and the F2 ownership check
  underneath are shared, so the widened gate widens nothing else.
- Tests: `reeltalk/tests/test_federation_integrity.py` (9 collected), every
  one delivered through the real signed `/inbox/` route, plus two unit
  tests beside the existing `fetch_person_document` guards in
  `test_activitypub_inbox.py`. The honest `Update`/`Delete` pair is the
  regression guard that keeps this from being a fix that silently breaks
  federation, in both the Note and the Tombstone spelling of a delete.
  Non-vacuity was checked by stashing the guards: with the F2/F3 guards
  stashed exactly the 4 attack tests go red; with the Tombstone gate
  reverted exactly the 2 tombstone tests go red; the controls stay green
  both times. Targeted set with the change in place: **654 passed**
  (federation / activitypub / moderation / mentions / public_http / flag /
  shelf files).
- **S3 live check DONE (owner, 2026-10-21):** the honest edit arrived as an
  update and a new peer's first contact minted its mirror - the F3 id check
  met a real Mastodon Person document and held. The honest delete failed at
  first, and that failure surfaced the Tombstone gap above; the fix is
  deployed and the owner reruns a delete with a fresh post. The one stale
  mirror the failed delete left behind (status 87, upstream post already
  gone) was tombstoned by hand. (Owner drives live federation checks.)
- **VPS redeploy is deferred until the whole audit is finished (owner,
  2026-10-21).** reeltalk.dev still runs pre-S1 code. When it eventually
  redeploys from the repo, its `.env` needs `WEB_BIND=127.0.0.1` and
  `FEDERATION_ALLOWED_NETWORKS=192.168.1.0/24`.
- Gate baseline: full-suite **2541 passed, 5 skipped** (2026-10-21, on S2,
  all four markers 0). S3 ran targeted only (654). Count collected items,
  never `def test` lines: `test_clean_room.py` parametrizes one case per
  file, so a new test file adds a collected item beyond its own tests.
- Open findings: **F6 (S4, next)**, F7-F9 (S5), F10-F12 (S6). The DB role
  split ships in the default deploy flow and lands with S4 (owner
  decision).

## What the next session should do

Execute increment **S4** from `security-plan.md`: F6, fail-fast on
`SECRET_KEY`, plus the deploy-hygiene pass.

- **F6.** `reeltalk/settings.py:19` reads
  `SECRET_KEY = env.str("SECRET_KEY", default="insecure-dev-only-key-change-me")` -
  a production boot with the variable unset silently gets a world-known
  key. When `DEBUG` is false and `SECRET_KEY` is unset, empty, or equal to
  that fallback constant, raise `ImproperlyConfigured` pointing at
  `setup.sh`. Tests (from the plan): boot under `DEBUG=False` without a key
  raises; with a real key it does not. Verify the documented deploy paths
  (`setup.sh`, the prebuilt compose) still come up clean. Watch the
  pytest-env block: the suite must not inherit an operator `.env`, and the
  test settings boot must keep working.
- Fold in settings hygiene S1 surfaced (e.g. `.env.example` gaps).
- **DB role split (owner decision 4 in `security-plan.md`):** the role
  creation, the ownership migration, and the upgrade path for existing
  installs land here, as deploy-flow scripts and docs (`setup.sh`,
  `DEPLOYING.md`, the compose files). Host-side infra stays out of the repo
  per the lean-scope decision.
- S3's live check is folded in (above): it passed for the honest edit and
  the first contact, and its one failure produced the Tombstone fix.

## Ground rules (full list in security-plan.md)

- Rebuild the Docker image before running pytest; the gate runs the baked
  image.
- Targeted pytest + ruff per increment. The full gate (~36 min) only on the
  owner's explicit signal.
- One commit per increment, no co-author trailer. Rewrite the
  `PROGRESS.md` NOW block as the increment's last step.
- Deployer-agnostic: safe no-config defaults in shipped code; host infra
  stays out of the repo.
- The VPS is production: read-only unless the owner approves a change, and
  all VPS work is deferred until the audit is complete.
- No em dashes in user-facing text.
- The suite must not inherit operator env: any setting `.env` may
  legitimately set that tests assume a default of belongs pinned in
  pyproject's pytest-env block.

## Open items for the owner

- **S3 delete rerun:** with the Tombstone fix deployed, delete one LAN
  Mastodon post and watch the tombstone arrive. The earlier failure's
  record is above; the edit and first-contact checks already passed.
- The full gate has not been run since S2. Say the word and a session runs
  it; expect **2553 passed, 5 skipped** if S3 is clean.
- S1's, S2's, and S3's live checks are DONE (2026-10-21): the LAN peer
  federates both ways through the SSRF guard, S2's hostile payload
  rendered inert on every surface including the moderation queue, and
  S3's edit and first-contact checks passed on the real wire.

## Read in this order

1. `security-plan.md` - ground rules first, then S4.
2. `SECURITY-AUDIT.md` - F6 evidence.
3. `reeltalk/settings.py` (the `SECRET_KEY` line and how `DEBUG` is read),
   `setup.sh`, `DEPLOYING.md`, `docker-compose.prebuilt.yml`.

No open questions for the owner going into S4.
