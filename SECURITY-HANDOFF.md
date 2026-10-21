# Security audit handoff

> Status: S2 landed 2026-10-21 (F1 closed) with targeted pytest and ruff green
> on the rebuilt baked image. The **full gate has not been run for S2** - it
> runs only on the owner's explicit signal. The `## NOW` block in
> `PROGRESS.md` is the current source of truth; everything below is the
> handoff as written at S2's close.

Point the next session at this file. Everything needed is in the repo; no
production access is required to start S3.

## State (as of the S2 close, 2026-10-21)

- S2 closed **F1 (stored XSS from any peer)**. `reeltalk/activitypub/statuses.py`
  now runs every remote HTML field through `core.utils.sanitize_html` at the
  single place it is read off the wire, via a module-level `_remote_html`
  (line 142) beside the other wire normalizers: Note `content` in
  `_mirror_status`, Film `summary` in `_mirror_film`, list extension
  `summary` in `_mirror_list`. Because each field is sanitized once at its
  read, the create branch and the update helpers (`_apply_note_fields`,
  `_apply_list_fields`) store the identical checked bytes - the audit's
  "create and update branches alike" is satisfied by construction, not by
  two call sites that can drift.
- Two consequences worth knowing before touching this file again. **One:**
  bleach escapes what it refuses rather than stripping it, so a hostile body
  stays readable as inert text and a non-empty field stays non-empty - which
  matters because the shape mapping in `_mirror_status` reads `content` to
  choose the status type. **Two:** remote bodies now pass the local
  outbound-link rule, so an `href` to a domain outside `LinkDomain` loses
  its href exactly as a local member's would. Both are pinned by tests.
- Tests: `reeltalk/tests/test_federation_sanitize.py` (12 collected), every
  one delivered through the real signed `/inbox/` route, including the
  moderation-queue render. Non-vacuity was checked by reverting the guard:
  8 go red, the 4 controls stay green. Targeted set with the change in
  place: **447 passed** (435 existing federation/moderation + 12 new).
- **VPS redeploy is deferred until the whole audit is finished (owner,
  2026-10-21).** reeltalk.dev still runs pre-S1 code. When it eventually
  redeploys from the repo, its `.env` needs `WEB_BIND=127.0.0.1` and
  `FEDERATION_ALLOWED_NETWORKS=192.168.1.0/24`.
- Gate baseline: **2528 passed, 5 skipped** (2026-10-21, measured on S1).
  S2 adds 12 collected items, so a green full gate on S2 should read 2540.
- Open findings: **F2 + F3 (S3, next)**, F6 (S4), F7-F9 (S5), F10-F12 (S6).
  The DB role split ships in the default deploy flow and lands with S4
  (owner decision).

## What the next session should do

Execute increment **S3** from `security-plan.md`: F2 + F3, federation ingest
integrity. Both are small diffs in two files that S2 also touched, so re-read
the current state of those files rather than the audit's line numbers.

- **F2 - `Update`/`Delete` do not check that the sender owns the object.**
  `reeltalk/activitypub/statuses.py` (line refs verified 2026-10-21):
  the update branch of `_mirror_status` starts at line 529
  (`existing = Status.objects.filter(local=False, remote_url=home_url)`),
  and `handle_delete` is at line 917. In both, drop the activity and log it
  when `existing.user_id != sender.pk` - the same graceful-ignore posture
  the module already uses for other unforged-but-unusable shapes. Note the
  module's attribution rule protects who *created* a row and says nothing
  about who may *mutate* one; that is the gap. `handle_update` (line 878)
  dispatches into `_mirror_status`, so the guard belongs in the update
  branch, not in the dispatcher.
- **F3 - a fetched Person document's `id` is never checked against the URL
  it was fetched from.** `reeltalk/activitypub/mirrors.py`:
  `fetch_person_document` (line 71) already refuses a non-Person type and a
  missing `publicKeyPem`, so the id check belongs beside those existing
  guards and raises `RemoteFetchError`. Compare against the requested URL
  with the fragment stripped - `mirror_user_from_person` (line 189) keys the
  mirror on `doc["id"]` at line 198, which is how a mismatched document
  would otherwise mint a mirror impersonating somebody else's actor URL and
  occupy the unique `actor_url` slot against the real identity.
- Tests (from the plan): a forged `Update` from a second verified sender is
  ignored; a forged `Delete` is ignored; a Person doc whose `id` does not
  match the fetched URL is rejected; and **honest `Update`/`Delete` still
  work** - that last pair is the regression guard, and it is the one that
  keeps this increment from being a fix that silently breaks federation.
  Deliver through the real signed `/inbox/` route, as
  `test_federation_inbound.py` and `test_federation_sanitize.py` do.

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

- **S2 live check: DONE 2026-10-21.** A real signed payload (its own keypair,
  five RFC 9421 activities POSTed to the live `/inbox/`) was delivered to
  this box and rendered inert on all three surfaces, in the moderation
  queue, and on the edited-note update path. No alert fired. The same
  session closed S1's outstanding live item too: this box and the LAN
  Mastodon peer follow each other and posts arrive, so the SSRF guard and
  its LAN opt-in have now met a real peer.
- **The link consequence is settled, not open (owner, 2026-10-21):** with no
  allowed link domains, a link inside a remote post arrives as plain text.
  The owner confirmed that is what they want. Do not re-open it as a defect
  in a later increment.
- The full gate has not been run since S1. Say the word and a session runs
  it; expect 2540 passed, 5 skipped if S2 is clean.

## Read in this order

1. `security-plan.md` - ground rules first, then S3.
2. `SECURITY-AUDIT.md` - F2 and F3 evidence (its line numbers predate S2's
   edit to `statuses.py`; use the ones above).
3. `reeltalk/activitypub/statuses.py` (the update branch and
   `handle_delete`) and `reeltalk/activitypub/mirrors.py`
   (`fetch_person_document`, `mirror_user_from_person`).

No open questions for the owner going into S3.
