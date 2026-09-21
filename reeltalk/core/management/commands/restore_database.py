"""Restore a database dump (M6).

The counterpart to ``backup_database``: restores a ``pg_dump`` custom-format dump
into the database named by ``--into``. Deliberately stricter than its backup half,
because the two operations are not symmetric — dumping the live database is
harmless, restoring over it destroys the owner's data. So the target is never
defaulted to the app's own database: it must be named, and naming the live one
additionally requires ``--force``.

Connection parameters come from Django's live settings exactly as in
``backup_database``, so a drill runs against whatever cluster the app points at,
and the password travels in ``PGPASSWORD`` rather than argv (no process-list
leak). The target database must already exist — this command never creates it, so
a typo in ``--into`` is a loud connection failure rather than a fresh database
nobody is looking at.
"""

import os
import shutil
import subprocess
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


def restore_argv(
    dump_path: str | Path,
    target_db: str,
    *,
    host: str | None = None,
    port: int | str | None = None,
    user: str | None = None,
    clean: bool = False,
) -> list[str]:
    """Build the ``pg_restore`` argv for a restore.

    ``--exit-on-error`` keeps a failure loud instead of pg_restore's default of
    carrying on and exiting 0. ``--single-transaction`` makes the restore
    all-or-nothing, so a mid-way failure leaves the target empty rather than
    half-loaded. ``--no-owner`` drops the dump's ``ALTER OWNER`` statements so a
    restore under a different role than the one that dumped still works; every
    object ends up owned by the restoring user.

    The password is never part of the returned list — it goes through the
    environment.
    """
    cmd = ["pg_restore", "--exit-on-error", "--single-transaction", "--no-owner"]
    if clean:
        cmd += ["--clean", "--if-exists"]
    if host:
        cmd += ["-h", str(host), "-p", str(port or 5432)]
    if user:
        cmd += ["-U", str(user)]
    cmd += ["-d", target_db, str(dump_path)]
    return cmd


def check_target(target_db: str, live_db: str, *, force: bool) -> None:
    """Refuse a target that would clobber the database the app runs against.

    Comparison is exact and case-sensitive, matching how the wire-level ``database``
    startup parameter is resolved against ``pg_database`` — ``--into ReelTalk``
    cannot silently resolve onto a live ``reeltalk``.
    """
    if not target_db or not target_db.strip():
        raise CommandError("--into must name a database to restore into.")
    if target_db == live_db and not force:
        raise CommandError(
            f"'{target_db}' is the database this application is running against. "
            "Restoring over it destroys the live data. Pass --force to do that anyway."
        )


class Command(BaseCommand):
    help = "Restore a pg_dump custom-format dump into an explicitly named database."

    def add_arguments(self, parser):
        parser.add_argument(
            "dump",
            help="Path to a pg_dump custom-format .dump file.",
        )
        parser.add_argument(
            "--into",
            required=True,
            help=(
                "Target database name. Required, with no default: this command "
                "will not guess that you meant the app's own database. The "
                "database must already exist."
            ),
        )
        parser.add_argument(
            "--clean",
            action="store_true",
            help=(
                "Drop existing objects in the target before recreating them "
                "(pg_restore --clean --if-exists). For restoring over a database "
                "that already holds a schema; leave it off for an empty one."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow the target to be the application's own live database.",
        )

    def handle(self, *args, **options):
        if shutil.which("pg_restore") is None:
            raise CommandError(
                "pg_restore not found on PATH — install postgresql-client in the image."
            )

        dump_path = Path(options["dump"])
        if not dump_path.is_file():
            raise CommandError(f"Dump file not found: {dump_path}")

        target_db = str(options["into"])
        conn = connection.settings_dict
        check_target(target_db, str(conn["NAME"]), force=options["force"])

        cmd = restore_argv(
            dump_path,
            target_db,
            host=conn.get("HOST"),
            port=conn.get("PORT"),
            user=conn.get("USER"),
            clean=options["clean"],
        )
        env = {**os.environ, "PGPASSWORD": str(conn.get("PASSWORD") or "")}

        size = dump_path.stat().st_size
        self.stdout.write(
            f"Restoring {dump_path.name} ({size} bytes) into '{target_db}'"
        )
        # Safe to echo: the password lives in the environment, not in argv.
        self.stdout.write(" ".join(cmd))
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise CommandError(
                f"pg_restore failed (exit {proc.returncode}): {proc.stderr.strip()}"
            )

        self.stdout.write(
            self.style.SUCCESS(f"Restored {dump_path.name} into '{target_db}'.")
        )
