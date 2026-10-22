-- Land the ReelTalk application database role.
--
-- Two roles, per HARDENING.md:
--
--   bootstrap (POSTGRES_USER)   the login the postgres image creates. Superuser,
--                               retained for maintenance. Never used by the app.
--   app       (POSTGRES_APP_USER) a plain login with no superuser, no CREATEROLE
--                               and no CREATEDB. Owns the ReelTalk database and
--                               every object in it, because that is what lets it
--                               run migrations without elevated rights.
--
-- Idempotent: safe to re-run, and it is run twice in practice (first boot via
-- /docker-entrypoint-initdb.d, and again by db/upgrade-role-split.sh on an
-- install that predates the split).
--
-- Run through db/init-app-role.sh or db/upgrade-role-split.sh, which set the
-- three environment variables this reads. The password travels by environment
-- rather than by psql -v so it never appears in a process listing.

\set ON_ERROR_STOP on

-- Every one of these is exported by both wrappers (db/init-app-role.sh and
-- db/upgrade-role-split.sh). That is load-bearing, not tidiness: psql's \getenv
-- leaves the variable *unset* when the environment has no such name, and an
-- unset psql variable is not substituted -- the literal text :'name' reaches
-- the server and fails as a syntax error. A wrapper that forgets one gets a
-- confusing parse failure instead of a clear "you did not set X".
\getenv app_user      REELTALK_APP_USER
\getenv app_password  REELTALK_APP_PASSWORD
\getenv boot_user     REELTALK_BOOTSTRAP_USER
\getenv rotate        REELTALK_ROTATE_BOOTSTRAP

SELECT set_config('reeltalk.app_user',     :'app_user',     false);
SELECT set_config('reeltalk.app_password', :'app_password', false);
SELECT set_config('reeltalk.boot_user',    :'boot_user',    false);
SELECT set_config('reeltalk.rotate',       :'rotate',       false);

-- The app login. Created if missing; if it already exists its password is
-- re-set, so re-running after a password rotation in .env converges instead of
-- erroring out.
DO $do$
DECLARE
    app      text := current_setting('reeltalk.app_user');
    secret   text := current_setting('reeltalk.app_password');
    boot     text := current_setting('reeltalk.boot_user');
BEGIN
    IF app = boot THEN
        -- Collapsing the two roles back together is never the intent, and it
        -- would hand a superuser login to the app one ALTER later.
        RAISE EXCEPTION 'REELTALK_APP_USER and REELTALK_BOOTSTRAP_USER must differ';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app) THEN
        EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', app, secret);
    ELSE
        EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L', app, secret);
    END IF;

    -- The privilege floor, asserted rather than assumed: an install that got
    -- this far with a privileged app role gets demoted, not skipped.
    EXECUTE format('ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE', app);
    EXECUTE format('ALTER ROLE %I NOBYPASSRLS NOREPLICATION', app);
END
$do$;

-- Ownership. Schema `public` is owned by pg_database_owner, whose only implicit
-- member is the owner of the current database, so transferring the database is
-- what gives the app role CREATE on the schema for its migrations.
DO $do$
DECLARE
    app text := current_setting('reeltalk.app_user');
    rel record;
BEGIN
    IF (SELECT datdba::regrole::text FROM pg_database
         WHERE datname = current_database()) <> app THEN
        EXECUTE format('ALTER DATABASE %I OWNER TO %I', current_database(), app);
    END IF;

    -- The database owner does not carry the objects with it: on an install that
    -- predates the split every table still belongs to the bootstrap role, and a
    -- role that does not own a table cannot ALTER it. Migrations would fail on
    -- the first ALTER TABLE. Indexes and constraints follow their table, so
    -- they are not listed.
    --
    -- Sequences need care. A sequence created for an auto column is *internally*
    -- dependent on that table (pg_depend deptype 'i', not 'a') and already moves
    -- with its ALTER TABLE; trying to re-own one on its own fails outright with
    -- "cannot change owner of sequence ... is linked to table". Only a sequence
    -- with no such link -- a hand-made one, or one a migration left behind --
    -- needs its own ALTER. Every sequence Django creates here is table-linked,
    -- so in practice this loop transfers zero rows and exists for the case where
    -- one does not.
    FOR rel IN
        SELECT c.oid, c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'S', 't', 'v', 'm', 'f')
          AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = app)
          AND NOT (
              c.relkind = 'S'
              AND EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.objid = c.oid
                    AND d.refclassid = 'pg_class'::regclass
                    AND d.deptype IN ('a', 'i')
              )
          )
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO %I',
            CASE rel.relkind
                WHEN 'S'          THEN 'SEQUENCE'
                WHEN 'v'          THEN 'VIEW'
                WHEN 'm'          THEN 'MATERIALIZED VIEW'
                WHEN 'f'          THEN 'FOREIGN TABLE'
                ELSE                   'TABLE'
            END,
            rel.oid::regclass,
            app);
    END LOOP;

    -- The search-vector trigger function is created by a migration and is owned
    -- by whoever ran it. A trigger cannot fire a function its owner cannot
    -- execute, so this has to move with the tables.
    FOR rel IN
        SELECT p.oid
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_language l ON l.oid = p.prolang
        WHERE n.nspname = 'public'
          AND l.lanname <> 'internal'
          AND p.proowner <> (SELECT oid FROM pg_roles WHERE rolname = app)
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO %I', rel.oid::regprocedure, app);
    END LOOP;
END
$do$;

-- The bootstrap password is a first-boot seed: the postgres image needs it to
-- create its superuser, and leaving it live would mean the app's own .env
-- carries a credential that reaches a superuser login. Rotating it to a value
-- nobody recorded closes that path. Maintenance does not need it: the db
-- container publishes no port, and `docker compose exec db psql -U <bootstrap>`
-- connects over the unix socket, which pg_hba trusts.
DO $do$
DECLARE
    boot text := current_setting('reeltalk.boot_user');
BEGIN
    IF current_setting('reeltalk.rotate') = 'true' THEN
        -- gen_random_uuid() is pg_catalog, so this needs no extension: the
        -- project installs none (no CREATE EXTENSION anywhere) and the app role
        -- could not create one even if it wanted to. Two UUIDs with the dashes
        -- stripped is 64 hex characters of entropy, which is far past what a
        -- scram hash needs.
        EXECUTE format('ALTER ROLE %I PASSWORD %L', boot,
                       replace(gen_random_uuid()::text, '-', '') ||
                       replace(gen_random_uuid()::text, '-', ''));
        RAISE NOTICE 'bootstrap role % password rotated; it is not recorded anywhere', boot;
    END IF;
END
$do$;

-- Fail loudly rather than come up subtly wrong: every check here is the
-- property the whole point of the file was to establish.
DO $do$
DECLARE
    app text := current_setting('reeltalk.app_user');
    bad text;
BEGIN
    SELECT string_agg(priv, ', ' ORDER BY priv) INTO bad
    FROM (
        SELECT 'superuser' AS priv, rolsuper AS holds FROM pg_roles WHERE rolname = app
        UNION ALL
        SELECT 'createrole', rolcreaterole FROM pg_roles WHERE rolname = app
        UNION ALL
        SELECT 'createdb', rolcreatedb FROM pg_roles WHERE rolname = app
        UNION ALL
        SELECT 'bypassrls', rolbypassrls FROM pg_roles WHERE rolname = app
        UNION ALL
        SELECT 'replication', rolreplication FROM pg_roles WHERE rolname = app
    ) held
    WHERE held.holds;

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'app role % still holds: %', app, bad;
    END IF;

    IF (SELECT datdba::regrole::text FROM pg_database
         WHERE datname = current_database()) <> app THEN
        RAISE EXCEPTION 'database % is not owned by %', current_database(), app;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'S')
          AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = app)
    ) THEN
        RAISE EXCEPTION 'objects in public are still owned by someone else than %', app;
    END IF;
END
$do$;
