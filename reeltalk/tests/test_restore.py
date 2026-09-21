"""Database restore command tests (deploy-readiness increment 5).

Two layers. The pure helpers (argv shape, the live-target guard) need no database.
The round-trip tests shell out to the real ``pg_dump`` / ``psql`` against the
pytest database: dump it, create a scratch database in the same cluster, restore
into it, and compare the table set and per-table row counts against the source.
They skip when the Postgres client tools are absent or the role cannot create a
database, so they stay honest rather than passing vacuously.

Nothing here restores into the application's own database — the one test that
targets it asserts the command *refuses*.
"""

import os
import shutil
import subprocess
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from reeltalk.core.management.commands import restore_database as restore_module
from reeltalk.core.management.commands.restore_database import (
    check_target,
    restore_argv,
)
from reeltalk.core.models import Film

CLIENT_TOOLS = ("pg_dump", "pg_restore", "psql")
needs_client = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in CLIENT_TOOLS),
    reason="postgres client tools not installed",
)


# --- pure helpers ---------------------------------------------------------


def test_restore_argv_default_shape():
    argv = restore_argv("/tmp/a.dump", "scratch")
    assert argv[:4] == [
        "pg_restore",
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
    ]
    assert argv[-3:] == ["-d", "scratch", "/tmp/a.dump"]
    assert "-h" not in argv and "-U" not in argv


def test_restore_argv_adds_connection_flags_when_given():
    argv = restore_argv("d.dump", "scratch", host="db", port=5433, user="rt")
    assert ["-h", "db", "-p", "5433"] == argv[argv.index("-h") : argv.index("-h") + 4]
    assert ["-U", "rt"] == argv[argv.index("-U") : argv.index("-U") + 2]


def test_restore_argv_port_defaults_when_falsy():
    argv = restore_argv("d.dump", "scratch", host="db", port=None)
    assert argv[argv.index("-p") + 1] == "5432"


def test_restore_argv_clean_only_when_asked():
    assert "--clean" not in restore_argv("d.dump", "scratch")
    clean = restore_argv("d.dump", "scratch", clean=True)
    assert "--clean" in clean and "--if-exists" in clean


def test_check_target_refuses_the_live_database_without_force():
    with pytest.raises(CommandError) as exc:
        check_target("reeltalk", "reeltalk", force=False)
    assert "--force" in str(exc.value)


def test_check_target_allows_live_only_with_force():
    check_target("reeltalk", "reeltalk", force=True)


def test_check_target_allows_any_other_database():
    check_target("reeltalk_drill", "reeltalk", force=False)
    check_target("reeltalk_drill", "reeltalk", force=True)


def test_check_target_comparison_is_exact_not_case_folded():
    # The wire-level `database` parameter is resolved case-sensitively, so a
    # differently-cased name is a different database and needs no --force.
    check_target("ReelTalk", "reeltalk", force=False)


@pytest.mark.parametrize("bad", ["", "   "])
def test_check_target_refuses_an_empty_database_name(bad):
    with pytest.raises(CommandError):
        check_target(bad, "reeltalk", force=True)


# --- command-level wiring (stubbed subprocess) ----------------------------


def _stub_run(captured):
    def fake_run(cmd, env=None, capture_output=None, text=None):
        captured["argv"] = list(cmd)
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def test_command_sends_the_password_in_the_env_not_argv(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"stub")
    captured = {}
    password = str(connection.settings_dict.get("PASSWORD") or "")

    with mock.patch.object(restore_module.subprocess, "run", _stub_run(captured)):
        call_command(
            "restore_database", str(dump), "--into", "scratch_target", verbosity=0
        )

    assert captured["env"]["PGPASSWORD"] == password
    if password:
        assert password not in " ".join(captured["argv"])


def test_command_targets_the_named_database_not_the_live_one(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"stub")
    captured = {}
    live = str(connection.settings_dict["NAME"])

    with mock.patch.object(restore_module.subprocess, "run", _stub_run(captured)):
        call_command("restore_database", str(dump), "--into", "other_db", verbosity=0)

    assert captured["argv"][captured["argv"].index("-d") + 1] == "other_db"
    assert captured["argv"][captured["argv"].index("-d") + 1] != live


def test_command_refuses_the_live_database_before_running_anything(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"stub")
    captured = {}
    live = str(connection.settings_dict["NAME"])

    with (
        mock.patch.object(restore_module.subprocess, "run", _stub_run(captured)),
        pytest.raises(CommandError),
    ):
        call_command("restore_database", str(dump), "--into", live, verbosity=0)

    assert "argv" not in captured  # nothing was ever invoked


def test_command_requires_the_dump_to_exist(tmp_path):
    with pytest.raises(CommandError) as exc:
        call_command(
            "restore_database", str(tmp_path / "absent.dump"), "--into", "scratch"
        )
    assert "not found" in str(exc.value)


def test_command_requires_into():
    # Django's OptionParser turns argparse's "required" failure into CommandError
    # instead of exiting, so the requirement bites before handle() ever runs.
    with pytest.raises(CommandError) as exc:
        call_command("restore_database", "/tmp/whatever.dump")
    assert "--into" in str(exc.value)


# --- real round trip ------------------------------------------------------


def _client_env():
    conn = connection.settings_dict
    return {**os.environ, "PGPASSWORD": str(conn.get("PASSWORD") or "")}


def _psql(dbname, sql):
    conn = connection.settings_dict
    cmd = ["psql", "-v", "ON_ERROR_STOP=1", "-At", "-d", dbname]
    if conn.get("HOST"):
        cmd += ["-h", str(conn["HOST"]), "-p", str(conn.get("PORT") or 5432)]
    if conn.get("USER"):
        cmd += ["-U", str(conn["USER"])]
    cmd += ["-c", sql]
    return subprocess.run(cmd, env=_client_env(), capture_output=True, text=True)


def _admin_sql(sql):
    """Run one statement against the cluster's maintenance database."""
    return _psql("postgres", sql)


def _table_row_counts(dbname):
    """``{table: row_count}`` for every public table, in one round trip."""
    listing = _psql(
        dbname,
        "SELECT tablename FROM pg_tables "
        "WHERE schemaname = 'public' ORDER BY tablename",
    )
    assert listing.returncode == 0, listing.stderr
    tables = [line for line in listing.stdout.splitlines() if line.strip()]
    if not tables:
        return {}
    union = " UNION ALL ".join(
        f"SELECT '{name}' AS tbl, count(*) AS rows FROM public.\"{name}\""
        for name in tables
    )
    counts = _psql(dbname, union)
    assert counts.returncode == 0, counts.stderr
    result = {}
    for line in counts.stdout.splitlines():
        if not line.strip():
            continue
        table, _, rows = line.partition("|")
        result[table] = int(rows)
    return result


@pytest.fixture
def scratch_db():
    """A throwaway database in the same cluster, dropped on teardown."""
    live = str(connection.settings_dict["NAME"])
    name = f"{live}_drill_probe"

    _admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    created = _admin_sql(f'CREATE DATABASE "{name}"')
    if created.returncode != 0:
        pytest.skip(f"role cannot create a scratch database: {created.stderr.strip()}")
    try:
        yield name
    finally:
        _admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@needs_client
@pytest.mark.django_db(transaction=True)
def test_dump_restores_into_a_scratch_database_with_matching_row_counts(
    tmp_path, scratch_db
):
    live = str(connection.settings_dict["NAME"])
    marker = "Restore Round Trip Probe"
    Film.objects.create(title=marker, year=1982)

    call_command("backup_database", "--output-dir", str(tmp_path), verbosity=0)
    dumps = list(tmp_path.glob(f"{live}-*.dump"))
    assert len(dumps) == 1

    call_command("restore_database", str(dumps[0]), "--into", scratch_db, verbosity=0)

    assert _table_row_counts(scratch_db) == _table_row_counts(live)
    probe = _psql(
        scratch_db, f"SELECT count(*) FROM core_film WHERE title = '{marker}'"
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "1"


@needs_client
def test_restore_into_a_database_that_does_not_exist_fails(tmp_path):
    # A real dump, so pg_restore gets past its own file check and the failure is
    # genuinely about the target database not existing.
    call_command("backup_database", "--output-dir", str(tmp_path), verbosity=0)
    live = str(connection.settings_dict["NAME"])
    real = list(tmp_path.glob(f"{live}-*.dump"))
    assert real, "backup produced no dump to restore from"

    with pytest.raises(CommandError):
        call_command(
            "restore_database",
            str(real[0]),
            "--into",
            "reeltalk_target_that_was_never_created",
            verbosity=0,
        )


@needs_client
def test_scratch_database_does_not_survive_the_suite():
    """Teardown is real: no probe database is left behind in the cluster."""
    names = _admin_sql(
        "SELECT datname FROM pg_database WHERE datname LIKE '%\\_drill\\_probe'"
    )
    assert names.returncode == 0, names.stderr
    assert names.stdout.strip() == ""
