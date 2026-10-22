# Security audit handoff

> Status: **S4 is fully landed.** Part 1 closed F6 (fail-fast on `SECRET_KEY`);
> part 2 landed the **DB role split** (owner decision 4) on 2026-10-22, **full
> gate green: 2574 passed, 5 skipped** in 35:29, all four markers `0`.
>
> The next increment is **S5: F7 + F8, inbox hardening**. No open design
> questions block it. The `## NOW` block in `PROGRESS.md` is the current source
> of truth; everything below is the handoff as written at the role split's close.

Point the next session at this file. Everything needed is in the repo; no
production access is required to start.

## State (as of the role-split close, 2026-10-22)

- Closed so far: S1 (F4, F5), S2 (F1), S3 (F2, F3 + the Tombstone gap), S4 part 1
  (F6), **S4 part 2 (the DB role split)**. Open: **S5 (F7, F8)**, then S6
  (F10-F12 + the surfaces the first pass did not cover).
- **The app no longer connects as a superuser.** `db/role-split.sql` creates a
  separate login with no superuser, no `CREATEROLE`, no `CREATEDB`, no
  bypass-RLS and no replication, hands it the database and every object in it,
  and retires the bootstrap password. It ends with a verification block that
  raises if the app role still holds any of those privileges, so a half-landed
  split fails the boot instead of coming up looking healthy.
- **First boot vs upgrade.** The DDL runs from `/docker-entrypoint-initdb.d/`
  (mounted in both compose files), which the postgres image only executes against
  an empty data directory. An install whose volume already exists runs
  `db/upgrade-role-split.sh`. Leaving `POSTGRES_APP_USER` empty is the supported
  no-split posture: `settings.py` falls back to `POSTGRES_USER`.
- **The test suite has its own throwaway server** (`docker-compose.test.yml`,
  tracked). pytest-django creates and drops `test_reeltalk` on every run, which
  needs `CREATEDB`, and the app role deliberately holds none. The known limit is
  stated in that file: the suite runs as that throwaway server's superuser, so it
  does not police the app role's floor.
- **Gate invocation changed shape.** The suite now needs
  `docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm web
  pytest`. Without the second `-f` it points at the app's own server and dies on
  `permission denied to create database`. `.qwen/tmp/run-gate.sh` is gitignored,
  so **this session updated the local copy**; a fresh clone's gate script needs
  the same two-line change, and it is not something the next session can see in
  `git diff`.
- **VPS redeploy stays deferred until the audit is finished (owner).**
  reeltalk.dev still runs pre-S1 code. When it redeploys its `.env` needs
  `WEB_BIND=127.0.0.1` and `FEDERATION_ALLOWED_NETWORKS=192.168.1.0/24`.

## What the next session should do

**S5: F7 + F8, inbox hardening** (see `security-plan.md` §S5). Four pieces:

1. **An app-side bound on the inbox body**, applied before `json.loads`.
2. **A per-source floor on inbox POSTs**, reusing the `attempts.py` table
   pattern with a generous limit (the shared inbox legitimately receives from
   many peers).
3. **A `DeliveredActivity` retention prune** as a management command, with cron
   documented.
4. **Require body coverage in signatures**: `content-digest` among the covered
   components for RFC 9421 POSTs with a body, and `Digest` required on the legacy
   path. **Check Mastodon interop first** - the LAN peer is the reference, and the
   owner drives the live federation check before merge.

### Measured facts for S5 (verified 2026-10-22, this session)

Read these before designing; two of them correct a premise you might otherwise
inherit.

- **The body is not uncapped today, it is capped at the wrong number.**
  `settings.DATA_UPLOAD_MAX_MEMORY_SIZE` is `DATA_UPLOAD_MAX_MEMORY_MiB` with a
  **100 MiB** default (`reeltalk/settings.py:330`), so Django already refuses an
  oversized body - with a limit 100x too generous for an inbox POST, and global
  rather than inbox-specific. The finding is still real (the nginx 1 MiB cap is
  operator-side and the shipped app needs its own), but the fix is a
  route-appropriate bound, not "add the first bound".
- **F8 is confirmed on both paths, and it is the sender's own declaration that
  decides.** In `_verify_rfc9421` (`reeltalk/activitypub/signatures.py:301`) the
  covered components come from the sender's `Signature-Input`, and
  `content-digest` is only checked *if the sender listed it*. Same shape in
  `_verify_legacy` (`:347`): the `digest` branch runs only if `digest` appears in
  the sender's own `headers` list. When a sender does list it, the value is
  already checked against the real body and never trusted - so the gap is
  precisely "coverage is optional", not "the digest is believed".
- **The F7 amplification is real and sits before the signature check.**
  `_handle_inbox_post` (`reeltalk/activitypub/views.py:305`) runs
  `extract_key_id` → `resolve_sender` → `verify_request` → `json.loads`.
  `resolve_sender` is `_resolve_actor(..., fetch=True)`
  (`reeltalk/activitypub/mirrors.py:430`), so an unknown keyid triggers a
  synchronous outbound fetch. Note the order: an unauthenticated caller with any
  syntactically valid keyid makes us dial out before we know anything about them.
- **Key any floor on `client_ip()`, never a header.** `reeltalk/proxy_trust.py:219`.
  `reeltalk/social/attempts.py` is the pattern to copy, including its two
  standing lessons: count in a Postgres table, not a cache (a LocMemCache counter
  resets on deploy and disagrees between workers, which is a control that reads
  as protection and is not), and never key a limit on an unparseable address (one
  shared empty bucket lets one caller lock out every other).
- **`DeliveredActivity`** (`reeltalk/activitypub/models.py:13`) is `activity_id`
  (unique, `max_length=2048`) plus `delivered_date`. The prune keys on
  `delivered_date`. Management-command precedents to follow:
  `reeltalk/core/management/commands/check_client_ip.py` and
  `reeltalk/lists/management/commands/rebroadcast_lists.py`.
- **The dedup row and the handler run in one transaction**
  (`reeltalk/activitypub/inbox.py`), so a handler failure rolls the dedup record
  back and the sender's retry is not blocked by its own failed delivery. Any new
  floor must not accidentally break that: a floor counted *inside* the
  transaction would roll back its own counter.

## Deferred by the owner (2026-10-22): eliminate the bootstrap password entirely

Not this session. The idea came from the owner asking whether `POSTGRES_PASSWORD`
could simply stop existing, and the answer measured out to yes. It is recorded
here with its evidence so the next session does not re-derive it.

What was measured, on throwaway `postgres:17` containers:

- **The password requirement is the image's, not Postgres's.** The refusal comes
  from `docker_verify_minimum_env` in `/usr/local/bin/docker-entrypoint.sh`
  (line 106): it errors when `POSTGRES_PASSWORD` is empty **and**
  `POSTGRES_HOST_AUTH_METHOD` is not `trust`, and only when the data directory is
  uninitialised. Postgres itself creates a cluster whose superuser has no
  password without complaint.
- **Setting `POSTGRES_HOST_AUTH_METHOD=trust` satisfies the guard with no
  password** (measured: `database system is ready to accept connections`). Do
  not ship this: it writes trust rules for host connections, so any container on
  the compose network could open the bootstrap login with no credential. That is
  strictly worse than today.
- **The seed is never reachable while it exists.** The entrypoint starts its
  temporary server with `-c listen_addresses=''` (line 297), so during first boot
  there is no network listener. Measured by probing TCP from a second container
  while an init script slept: no response across 18 seconds.
- **The credential can be destroyed rather than rotated.** `ALTER ROLE <boot>
  PASSWORD NULL` leaves `pg_authid.rolpassword` NULL, after which scram refuses
  every password including the original seed, while `docker compose exec db psql`
  over the socket still works. That is stronger than what shipped: today the
  verifier exists and is merely unrecorded.

The shape of the change would be: compose supplies a fixed public literal for
`POSTGRES_PASSWORD` purely to satisfy the image guard, the init script creates the
app role over the socket, and then clears the bootstrap credential outright.
`setup.sh` and `.env.example` would stop carrying a database secret that nobody
can use.

**The hazard that has to be closed in the same change, or the change is a
regression.** The no-split posture (`POSTGRES_APP_USER` empty) makes the app
connect as the bootstrap login over TCP using `POSTGRES_PASSWORD`. If that value
becomes a published literal, the app's own database login is a publicly known
superuser password. So the change is only safe if that posture is either dropped
or made to fail loudly when `POSTGRES_APP_USER` is empty and `POSTGRES_PASSWORD`
is the shipped literal. This is the standing pattern of a widening change
silently deleting a guard that was doing real work by accident.

Also load-bearing and easy to break later: the db container publishing **no port**
is what makes "no password" safe. Anything that publishes 5432 turns this from a
hardening into an exposure.

## Ground rules (full list in security-plan.md)

- Rebuild the Docker image before running pytest; the gate runs the baked
  image. A docs-only edit needs it too (the clean-room guard scans the baked
  tree, and ruff formats Python inside markdown fences).
- Targeted pytest + ruff per increment. The full gate (~36 min) only on the
  owner's explicit signal.
- One commit per increment, no co-author trailer. Rewrite the `PROGRESS.md`
  NOW block as the increment's last step.
- Deployer-agnostic: safe no-config defaults in shipped code; host infra stays
  out of the repo.
- The VPS is production: read-only unless the owner approves a change, and all
  VPS work is deferred until the audit is complete.
- No em dashes in user-facing text.
- The suite must not inherit operator env: any setting `.env` may legitimately
  set that tests assume a default of belongs pinned in pyproject's pytest-env
  block. **The role split added two such settings**: `POSTGRES_APP_USER` and
  `POSTGRES_APP_PASSWORD`. `test_db_role_split.py` scrubs every `POSTGRES_*`
  name from the inherited env in its subprocess boots, which is the pattern for
  any future settings-level test.

## Traps learned landing the role split, so you do not re-derive them

- **`ALTER DATABASE ... OWNER TO` does not re-owner existing objects.** The
  per-object `ALTER ... OWNER TO` loop is required, not an optimization; without
  it migrations fail on the first `ALTER TABLE`.
- **Django's auto-column sequences are *internally* dependent on their table**
  (`pg_depend.deptype = 'i'`, not `'a'`). They move with `ALTER TABLE`, and
  trying to re-own one on its own fails outright. A probe that queries only
  `deptype = 'a'` reports a misleading count.
- **The exec bit on `db/init-app-role.sh` is load-bearing.** The postgres
  entrypoint *sources* a non-executable `.sh`, and the script's early `exit 0`
  (the no-split path) would then abort Postgres's own first boot.
- **`\getenv` needs a guaranteed exporter.** If the env var is unset the psql
  variable stays unset, is not substituted, and the literal `:'name'` reaches the
  server as a syntax error.
- **`gen_random_bytes()` needs pgcrypto**, which this project installs nowhere.
  The rotation uses two dash-stripped `gen_random_uuid()`s from pg_catalog.
- **`pg_roles` columns are all `rol`-prefixed** (`rolsuper`, `rolcreaterole`,
  `rolcreatedb`, `rolbypassrls`, `rolreplication`).
- **Rebuild before recreating after a credential rotation.** The image bakes the
  source, so a running container keeps the old `settings.py`; that is what
  produced a `password authentication failed` unhealthy container here.

## Read in this order

1. `security-plan.md` - ground rules first, then §S5.
2. `SECURITY-AUDIT.md` - F7 and F8 as written, plus F9 (the replay window,
   documented as accepted, which F7 makes cheap).
3. `reeltalk/activitypub/views.py` (`_handle_inbox_post`), `signatures.py`
   (`_verify_rfc9421`, `_verify_legacy`), `inbox.py`, `models.py`
   (`DeliveredActivity`), and `reeltalk/social/attempts.py` as the throttle
   pattern.
4. `db/role-split.sql` and `docker-compose.test.yml` if the gate command or the
   DB identity comes up.
