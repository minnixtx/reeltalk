# Security audit handoff

> Status: **S6 is landed on two commits (Half 1 the fixes, Half 2 this record)
> and FULL-GATE GREEN: 2619 passed, 5 skipped (all four markers `0`, 36:36).**
> Half 1 closed **F10** (invite code now rides the minter's session, never the
> query string), **F11** (CSV export formula guard on the title cell), and
> **F12** (a: DEPLOYING.md at-rest key note; b: version-disclosure decision
> recorded - keep publishing; c: the mirror profile throttle moved off
> LocMemCache to a `MirrorProfileAttempt` table, migration `0003`). Half 2
> audited the surfaces the first pass missed and appended **F13-F18** to
> `SECURITY-AUDIT.md`.
>
> The first gate run caught one real defect in Half 1 - the F10 edit left
> `reverse` imported but unused in `social/views.py` (ruff F401), which the
> targeted pass had skipped because it did not name that file. It was removed and
> folded into the Half 1 commit, and the gate was re-run green. The two commits
> are local, **not yet pushed**.
>
> The next increment is **S7: fix F13-F18** (see `security-plan.md` and the
> second-pass section of `SECURITY-AUDIT.md`). No open design questions block it.
> The `## NOW` block in `PROGRESS.md` is the current source of truth; everything
> below is the handoff as written at S6's close.

Point the next session at this file. Everything needed is in the repo; no
production access is required to start.

## State (as of the S6 close, 2026-10-22)

- Closed so far: S1 (F4, F5), S2 (F1), S3 (F2, F3 + the Tombstone gap), S4 part 1
  (F6), S4 part 2 (the DB role split), S5 (F7, F8), **S6 (F10, F11, F12 + the
  second-pass audit)**. Open: **S7** (fix F13-F18).
- **The full gate has not been run since S4 part 2.** S6 landed on targeted runs
  only (106 green across the three touched suites). The next full gate should
  re-count from the S5 baseline of 2600 passed, 5 skipped and add S6's new items:
  `test_mirror_profile_throttle.py` collects 6, `test_import_export.py` grew by
  11 (10 parametrized guard cases + 1 export case), and `test_invites.py` is
  net-flat (one test replaced by one). Plus the clean-room guard's items for the
  two new files. **Count collected items, never `def test` lines.**
- **VPS redeploy stays deferred until the audit is finished (owner).**
  reeltalk.dev still runs pre-S1 code. When it redeploys its `.env` needs
  `WEB_BIND=127.0.0.1` and `FEDERATION_ALLOWED_NETWORKS=192.168.1.0/24`.

## What landed in S6, and where

- **F10** - `reeltalk/social/views.py`: `invite_create` stashes the minted code
  in `request.session[INVITE_SESSION_KEY]` and redirects to the bare profile;
  `_invite_context` pops it from the session and never reads `?invite=`. Two
  tests rewritten to the session carry (the mint redirect is the bare profile
  path; a code handed in via the URL renders nothing).
- **F11** - `reeltalk/core/import_export.py`: `guard_formula` prefixes a leading
  `=`, `+`, `-`, `@`, tab or CR with `'`, applied to `film.title` in
  `export_film_csv`. Fires only on the payload, so ordinary titles stay
  byte-identical and the export still re-imports as a no-op.
- **F12a** - `DEPLOYING.md`: new subsection "What the role split does not protect:
  keys at rest", stating that `social_user.private_key` is plaintext and a DB or
  backup compromise is a signing-key compromise.
- **F12b** - `SECURITY-AUDIT.md` "F12 resolutions": decision to keep publishing
  the version, citing the live peer (`mastodon 4.7.3` in nodeinfo) and `/about`.
  No code change.
- **F12c** - `reeltalk/activitypub/models.py` `MirrorProfileAttempt` (one row per
  actor, `actor_url` unique, upserted) + migration `0003_mirrorprofileattempt.py`
  + `mirrors.py::_profile_refresh_throttled` (marks before fetching, so a failing
  remote still counts). New suite `test_mirror_profile_throttle.py` (6 items).
- **Half 2** - `SECURITY-AUDIT.md` "Second pass (S6)": F13 (High, admin
  self-promotion), F14-F17 (Medium), F18 (minor bucket a-k).

## What the next session should do

**S7: fix F13-F18** (see the second-pass section of `SECURITY-AUDIT.md` for each
finding's file:line and suggested fix). Suggested order:

1. **F13 (High) first - the admin privilege escalation.** `social/admin.py`
   `UserAdmin` is a plain `ModelAdmin` with no per-object guards, and `is_superuser`
   sits in the editable "Roles" fieldset. Any staff account with change-on-users
   can self-promote or overwrite the admin's password. Mirror stock
   `contrib.auth.admin.UserAdmin`: override `has_change_permission`/
   `has_delete_permission(request, obj)` to refuse a non-superuser on any
   superuser/staff/moderator row, and drop the role fields from the form unless
   `request.user.is_superuser`. This is the one finding worth a whole session on
   its own.
2. **F14-F17 (Medium)** - the status-Delete audience (`broadcast_status_delete`
   should use `_status_targets`, matching `broadcast_list_delete`); the password
   hash render (gate `password_hash` on change permission or truncate it); the
   mention-parser ReDoS (atomic quantifiers or a line-scan, and/or a markdown
   length cap); the resend-throttle TOCTOU (serialize check+mint per key).
3. **F18 (minor bucket a-k)** - each is small and independent; batch them. Note
   F18(f) is a one-word spec fix (`openRegistration` -> `openRegistrations` in
   `activitypub/views.py:148`), and F18(a) (clear `rating` in `Status.delete()`)
   touches a soft-delete path with federation implications, so check the
   tombstone tests.

## Deferred by the owner (from S4, still open): eliminate the bootstrap password

Not yet done. Recorded in full in `PROGRESS.md`'s NOW block and in the S4
handoff history: the password requirement comes from the postgres image's
`docker_verify_minimum_env`, not from Postgres, and `ALTER ROLE ... PASSWORD
NULL` destroys the verifier while socket maintenance keeps working. **The hazard
that must be closed in the same change:** the no-split posture (`POSTGRES_APP_USER`
empty) makes the app connect as the bootstrap login with `POSTGRES_PASSWORD`, so
turning that value into a published literal would hand out a known superuser
password. Also load-bearing: the db container publishes **no port**, which is what
makes "no password" safe at all.

## Ground rules (full list in security-plan.md)

- Rebuild the Docker image before running pytest; the gate runs the baked
  image. A docs-only edit needs it too (the clean-room guard scans the baked
  tree, and ruff formats Python inside markdown fences).
- Targeted pytest + ruff per increment. The full gate (~36 min) only on the
  owner's explicit signal.
- One commit per increment, no co-author trailer. Rewrite the `PROGRESS.md`
  NOW block as the increment's last step.
- Deployer-agnostic: safe no-config defaults in shipped code; host infra stays
  out of the repo. The prune's cron lives on the host for exactly this reason,
  in keeping with backups.
- The VPS is production: read-only unless the owner approves a change, and all
  VPS work is deferred until the audit is complete.
- No em dashes in user-facing text.
- The suite must not inherit operator env: any setting `.env` may legitimately
  set that tests assume a default of belongs pinned in pyproject's pytest-env
  block. **S5 added one:** `INBOX_MAX_BODY_BYTES`. (`POSTGRES_APP_USER` and
  `POSTGRES_APP_PASSWORD` are the S4 examples; `test_db_role_split.py` scrubs
  every `POSTGRES_*` name in its subprocess boots.)

## Traps learned landing S6, so you do not re-derive them

- **A hand-written migration must match what `makemigrations` would emit**, or
  `makemigrations --check` fails the gate. Write it, then run `--check --dry-run`
  in the container to confirm "No changes detected" before trusting it.
- **`migrations/` is ruff-excluded** (`extend-exclude` in pyproject). Do not name
  a migration file in a `ruff check` path - naming it overrides the exclude and
  you get a spurious E501 on Django's own unformatted `BigAutoField` line.
- **A formula guard must fire on the payload, not every cell.** The CSV export is
  documented as byte-identical across runs and re-importing as a no-op; a guard
  that prefixes unconditionally breaks both. `guard_formula` only touches a cell
  that leads with a dangerous character.
- **A per-actor throttle is an upsert, not an append-only log.** `MirrorProfileAttempt`
  is one row per actor updated in place. Copying `InboxAttempt`'s append-only shape
  here would grow a row per page view of an un-fillable mirror forever - the exact
  unbounded table F7 just closed.
- **A check-then-mint throttle is a TOCTOU** (this is F17, to be fixed): the
  resend cooldown reads the token table and then mints with no lock, so
  concurrent POSTs all pass. Do not trust a read-then-write throttle to bound a
  concurrent flood until it serializes the two.

## Ground rules carried from S5 (still true)

- **`ruff` on the bind-mounted host tree needs `--no-cache`**, or it dies with
  `Failed to initialize cache at /host/.ruff_cache: Permission denied`. Same for
  `makemigrations`, which must run against the host tree because the image bakes
  the source.
- **Never key a throttle on an empty address.** `client_ip()` can come back empty;
  an empty key is one shared bucket for every caller that gets one.

## Read in this order

1. `security-plan.md` - ground rules first, then the S7 fix list.
2. `SECURITY-AUDIT.md` - the "Second pass (S6)" section (F13-F18) is the whole
   S7 work list, each with file:line and a suggested fix.
3. `reeltalk/social/admin.py` for F13, and stock `django.contrib.auth.admin`
   `UserAdmin` for the guard pattern to copy.
4. `reeltalk/activitypub/broadcast.py` for F14 (compare `broadcast_status_delete`
   with `broadcast_list_delete`), `reeltalk/mentions/parser.py` for F16,
   `reeltalk/social/verify.py` for F17.
5. `reeltalk/social/attempts.py` and `inbox_throttle.py` before touching any
   throttle.
