"""Daily database backup (M6).

Dumps the configured Postgres database with ``pg_dump`` (custom format) into
``settings.BACKUP_DIR`` and prunes older dumps down to ``settings.BACKUP_RETENTION``.
Implemented as a management command so the logic is directly testable and can be
run by hand or from host cron; the ``backup`` compose service schedules it once a
day (see ``backup-loop.sh``). The dump reads its connection parameters from
Django's live settings, so it targets whichever database the app points at — the
real one in production, the test database under pytest.
"""

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


def dump_filename(db_name: str, when: datetime | None = None) -> str:
    """A timestamped custom-format dump name.

    Zero-padded UTC (``<db>-YYYYMMDDTHHMMSSZ.dump``), so lexicographic order of
    the names is chronological — retention can prune by sorting on the name.
    """
    when = when or datetime.now(UTC)
    return f"{db_name}-{when.strftime('%Y%m%dT%H%M%SZ')}.dump"


def dumps_to_prune(existing: list[Path], keep: int) -> list[Path]:
    """The oldest dumps to delete so that at most ``keep`` remain.

    ``existing`` is the full set of this database's dump files (the just-written
    one included); returns the surplus, ordered oldest-first. ``keep <= 0``
    retains nothing.
    """
    retain = max(0, keep)
    if len(existing) <= retain:
        return []
    ordered = sorted(existing, key=lambda path: path.name)
    return ordered[: len(existing) - retain]


class Command(BaseCommand):
    help = "Dump the database with pg_dump and prune old backups."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default=None,
            help="Directory to write dumps into (default: settings.BACKUP_DIR).",
        )
        parser.add_argument(
            "--keep",
            type=int,
            default=None,
            help=(
                "Number of dumps to retain per database "
                "(default: settings.BACKUP_RETENTION)."
            ),
        )

    def handle(self, *args, **options):
        if shutil.which("pg_dump") is None:
            raise CommandError(
                "pg_dump not found on PATH — install postgresql-client in the image."
            )

        output_dir = Path(options["output_dir"] or settings.BACKUP_DIR)
        keep = (
            options["keep"]
            if options["keep"] is not None
            else settings.BACKUP_RETENTION
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        conn = connection.settings_dict
        db_name = str(conn["NAME"])
        target = output_dir / dump_filename(db_name)

        cmd = ["pg_dump", "--format=custom", "--file", str(target)]
        if conn.get("HOST"):
            cmd += ["-h", str(conn["HOST"]), "-p", str(conn.get("PORT") or 5432)]
        cmd += ["-U", str(conn["USER"]), "-d", db_name]

        # The password goes through PGPASSWORD, never the argv (no process-list leak).
        env = {**os.environ, "PGPASSWORD": str(conn.get("PASSWORD") or "")}

        self.stdout.write(f"Dumping '{db_name}' -> {target}")
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise CommandError(
                f"pg_dump failed (exit {proc.returncode}): {proc.stderr.strip()}"
            )

        prefix = f"{db_name}-"
        existing = [p for p in output_dir.glob(f"{prefix}*.dump")]
        pruned = dumps_to_prune(existing, keep)
        for victim in pruned:
            victim.unlink()
            self.stdout.write(f"Pruned {victim.name}")

        size = target.stat().st_size
        kept = len(existing) - len(pruned)
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {target.name} ({size} bytes); "
                f"{kept} dump(s) of '{db_name}' retained."
            )
        )
