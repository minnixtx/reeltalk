"""Database backup command tests (M6 increment 1).

The pure helpers (filename shape, retention pruning) run without any database or
binary. The end-to-end tests shell out to the real ``pg_dump`` against the pytest
database and verify the result is a valid custom-format dump; they skip when the
Postgres client tools are absent (they ship in the image via postgresql-client).
"""

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection

from reeltalk.core.management.commands.backup_database import (
    dump_filename,
    dumps_to_prune,
)


def test_dump_filename_shape():
    name = dump_filename("reeltalk", datetime(2026, 9, 14, 3, 17, 0, tzinfo=UTC))
    assert name == "reeltalk-20260914T031700Z.dump"


def test_dump_filename_defaults_to_now():
    # Shape only: <db>-<YYYYMMDDTHHMMSSZ>.dump (a 16-char UTC stamp).
    name = dump_filename("reeltalk")
    assert name.startswith("reeltalk-") and name.endswith(".dump")
    stamp = name[len("reeltalk-") : -len(".dump")]
    assert len(stamp) == 16


def test_dumps_to_prune_empty_and_under_keep():
    assert dumps_to_prune([], 7) == []
    files = [Path(f"d-{i}.dump") for i in range(3)]
    assert dumps_to_prune(files, 7) == []
    assert dumps_to_prune(files, 3) == []


def test_dumps_to_prune_keeps_newest_by_name():
    # Names are zero-padded timestamps, so lexicographic == chronological.
    files = [Path(f"d-2026{i:02d}T000000Z.dump") for i in range(1, 5)]  # 4 dumps
    assert dumps_to_prune(files, 2) == [files[0], files[1]]  # the two oldest


def test_dumps_to_prune_zero_keep_retains_nothing():
    files = [Path(f"d-{i}.dump") for i in range(3)]
    assert dumps_to_prune(files, 0) == sorted(files, key=lambda p: p.name)


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
@pytest.mark.django_db
def test_backup_command_produces_a_valid_custom_dump(tmp_path):
    call_command("backup_database", "--output-dir", str(tmp_path), verbosity=0)

    db_name = str(connection.settings_dict["NAME"])
    dumps = list(tmp_path.glob(f"{db_name}-*.dump"))
    assert len(dumps) == 1
    dump = dumps[0]
    assert dump.stat().st_size > 0

    # A custom-format dump is a tar of TOC + data; pg_restore --list reads it and
    # fails on a non-custom / corrupt file, so exit 0 is the validity check.
    listing = subprocess.run(
        ["pg_restore", "--list", str(dump)], capture_output=True, text=True
    )
    assert listing.returncode == 0
    assert listing.stdout.strip()  # a table of contents was printed


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
@pytest.mark.django_db
def test_backup_command_prunes_old_dumps(tmp_path):
    db_name = str(connection.settings_dict["NAME"])

    # Seed three older fake dumps (2020 timestamps sort before any real 2026 run);
    # a single run with keep=2 must prune the two oldest and retain the newest.
    for day in ("20200101", "20200102", "20200103"):
        (tmp_path / f"{db_name}-{day}T000000Z.dump").write_bytes(b"fake")

    call_command("backup_database", "--output-dir", str(tmp_path), verbosity=0, keep=2)

    remaining = sorted(p.name for p in tmp_path.glob(f"{db_name}-*.dump"))
    assert len(remaining) == 2
    assert not any(n.startswith(f"{db_name}-20200101") for n in remaining)
    assert not any(n.startswith(f"{db_name}-20200102") for n in remaining)
    assert any(n.startswith(f"{db_name}-20200103") for n in remaining)
