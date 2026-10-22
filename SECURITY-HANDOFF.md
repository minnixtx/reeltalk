# Security audit handoff

> Status: **S4 is half landed.** F6 (fail-fast on `SECRET_KEY`) plus the
> settings hygiene S1 left are committed and verified, **full gate green: 2562
> passed, 5 skipped**, all five markers `0`. The remaining S4 piece is the **DB
> role split**, and the one question that blocked its shape is now **settled**
> (see `## Settled`): the test suite gets its own throwaway Postgres server,
> so the app role is restricted with nothing traded away. No open questions
> remain. The `## NOW` block in `PROGRESS.md` is the current source of truth;
> everything below is the handoff as written at F6's close.

Point the next session at this file. Everything needed is in the repo; no
production access is required to start.

## State (as of the F6 close, 2026-10-21)

- **S4 part 1 closed F6.** `reeltalk/settings.py` now raises
  `ImproperlyConfigured` when `DEBUG` is false and `SECRET_KEY` is unset,
  blank, whitespace-only, or equal to the published fallback, naming
  `setup.sh`. **The finding as written understated the bug:** `.env.example`
  ships `SECRET_KEY=` blank and `environs` returns the empty string rather
  than the default (measured in the image), so the copy-the-template-and-skip-
  `setup.sh` path was booting on an **empty** key, not the known constant. The
  audit described a leaked constant; the likeliest real path was worse.
- **Tests:** `reeltalk/tests/test_secret_key.py` (8). Each case boots a real
  interpreter with a scrubbed env and a cwd outside the repo, because
  `settings.py` is already imported in-process and reloading it would rewrite
  the settings every other test reads. Non-vacuity: with the guard stashed
  exactly the 5 refusal cases go red and all 3 controls stay green.
- **Both documented deploy paths verified on the rebuilt image:** a
  template-only env refuses with a message naming `setup.sh`, and
  `setup.sh`'s own output passes `manage.py check`.
- **Hygiene folded in:** `POSTGRES_PORT` and the five `*_LOG_LEVEL` knobs were
  read by `settings.py` and documented nowhere. `.env.example` now lists them.
- **Gate state: the full suite has NOT been run on this change.** Targeted set
  was 441 passed, 5 skipped. Last full baseline is **2553 passed, 5 skipped**
  (on S3), so a full gate here should land at **2562** (2553 + 8 new tests + 1
  `test_clean_room.py` item for the new file). Count collected items, never
  `def test` lines.
- Closed so far: S1 (F4, F5), S2 (F1), S3 (F2, F3 + the Tombstone gap), S4
  part 1 (F6). Open: **the DB role split (S4 part 2, next)**, then F7-F9 (S5),
  F10-F12 (S6).
- **VPS redeploy stays deferred until the audit is finished (owner).**
  reeltalk.dev still runs pre-S1 code. When it redeploys its `.env` needs
  `WEB_BIND=127.0.0.1` and `FEDERATION_ALLOWED_NETWORKS=192.168.1.0/24`.

## What the next session should do

Land the **DB role split** (owner decision 4 in `security-plan.md`): role
creation, the ownership migration, and the upgrade path for existing installs,
as deploy-flow scripts and docs (`setup.sh`, `DEPLOYING.md`, the compose
files). Host-side infra stays out of the repo per the lean-scope decision.

### The spec, and where it actually lives

`security-plan.md` cites `HARDENING.md` as the input. **It is not in the repo
and never was** (`git log --all -- HARDENING.md` is empty). The owner's copy is
at `~/design-elements/HARDENING.md`. The load-bearing lines:

> App DB role `reeltalk_app` has no superuser, create-role or create-database
> privilege. Ownership/migration DDL was rehearsed against a restored copy.
> The maintenance bootstrap password was rotated after the app cutover.

> The bootstrap role is retained for maintenance; the application uses a
> separate restricted login and owns its database objects. **Never inject
> maintenance credentials into web or worker.**

**Read this before designing: the VPS already has the split and the repo does
not.** This is the same divergence S1 existed to close, one layer down. The
first move is a **read-only** capture of the VPS's real role DDL and grants
(`\du`, `\l`, per-object owners), so the repo ships what production already
runs rather than a second, different invention. VPS access is read-only unless
the owner approves a change.

### Measured facts (local dev stack, 2026-10-21, Postgres 17.11)

These were measured, not assumed, and they decide the shape:

- **The app role is a full superuser today**: `reeltalk` has
  `rolsuper`, `rolcreaterole`, `rolcreatedb`, and owns every database
  including `postgres` and `template1`. There is real privilege to remove.
- **Ownership is already clean, so the migration is cheap.** `reeltalk` owns
  the database and every table, and schema `public` is owned by
  `pg_database_owner` (whose only implicit member is the database owner). So
  `ALTER DATABASE ... OWNER TO reeltalk_app` transfers effective control with
  **no per-object `ALTER TABLE ... OWNER TO` churn**.
- **Nothing needs superuser-only DDL**: no `CREATE EXTENSION` anywhere,
  `tsvector` is built in, and there is exactly one non-internal trigger
  (`core_film_search_vector_trigger`, owned by `reeltalk`).
- **A `CREATEDB`-only role can create a database** (proven with a throwaway
  role, since dropped). So the test suite does not need superuser.
- **No backup/restore management commands exist in this rewrite** (they were
  legacy-project work), so the runtime role's privilege floor is migrations
  plus the test suite, nothing more.
- **`entrypoint.sh` runs `python manage.py migrate` with the app's own
  credentials**, and pytest-django creates `test_reeltalk`. Both are the
  constraints below.

## Settled: how the test suite gets its scratch database

**Owner's call (2026-10-22): the test suite gets its own throwaway Postgres
server.** Of the three shapes, this is the only one that satisfies the
hardening rule instead of trading a piece of it away, and it is what the owner
already did when validating the hardening: `HARDENING.md` records the 2,524
tests running "against a disposable PostgreSQL container, with no production
secrets in the test container".

So the app role keeps **no** `CREATEDB`, no `CREATEROLE` and no `SUPERUSER`,
and no second login needs to exist in the app's config at all, because the
suite never talks to the app's server.

Design sketch for the next session, with the two traps already spotted:

- The throwaway server belongs in a **tracked compose override** (something
  like `docker-compose.test.yml`) defining a second Postgres service with
  throwaway credentials, and overriding the app's `POSTGRES_HOST` to point at
  it. Tracked, because `.qwen/tmp/run-gate.sh` is gitignored: a change that
  lives only there is invisible to every other deployer and to the next
  session.
- The throwaway container's own admin role *may* hold `CREATEDB` (pytest-django
  creates `test_reeltalk` from scratch and runs the migrations into it). That
  is not a hole in the rule: it is a disposable container with no production
  secrets, which is exactly the posture `HARDENING.md` describes.
- **Deployer-agnostic still applies.** `docker compose up` must keep working
  with no manual psql and no knowledge of the test override. The override is
  opt-in for the test run only.
- The gate invocation changes shape: from `docker compose run --rm web pytest`
  to `docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm
  web pytest` (or equivalent). Worth confirming the `depends_on` healthcheck
  still gates correctly when a second db service is in play.


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
  block.

## Read in this order

1. `security-plan.md` - ground rules first, then S4.
2. `~/design-elements/HARDENING.md` - the role-split spec (outside the repo).
3. `reeltalk/settings.py` (the `SECRET_KEY` guard), `setup.sh`, `entrypoint.sh`,
   `DEPLOYING.md`, both compose files.
