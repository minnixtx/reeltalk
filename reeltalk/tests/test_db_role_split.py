"""S4: the app connects as its own database role, not the bootstrap superuser.

Two things are pinned here, and both fail silently if they break.

The first is the precedence in ``settings.DATABASES``. ``POSTGRES_APP_USER`` has
to win when it is set and fall back to ``POSTGRES_USER`` when it is not, because
that fallback is what keeps an install predating the split booting. The blank
case is the one that bites: ``.env.example`` ships ``POSTGRES_APP_PASSWORD=``
empty, and an env library that hands back the empty string rather than the
default would point the app at a login with no password. That is the same shape
F6 found for ``SECRET_KEY``, so it is tested rather than assumed.

The second is that both shipped compose files actually mount the DDL. A dropped
mount is invisible: the init script is what creates the app role, and with it
missing, a fresh install comes up healthy and connects as the superuser, which
is the exact state the split exists to remove.

Like ``test_secret_key.py``, each settings case boots a real interpreter: the
settings module is already imported by the test session, and reloading it would
rewrite the settings every other test reads.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

BOOT_MARKER = "BOOT_OK"

BOOT_SNIPPET = (
    "import reeltalk.settings as s;"
    f"print('{BOOT_MARKER}',"
    " s.DATABASES['default']['USER'],"
    " s.DATABASES['default']['PASSWORD'])"
)

# The postgres image creates POSTGRES_USER as a superuser. Naming it in a case
# below is the point: the app must not end up using it when an app role is given.
BOOTSTRAP = "reeltalk"
APP_ROLE = "reeltalk_app"


def _boot(**overrides: str) -> subprocess.CompletedProcess:
    """Import settings in a fresh interpreter with a controlled DB env.

    Every POSTGRES_* name is stripped from the inherited environment first: the
    gate container inherits the operator's env_file, and a subprocess that
    inherited a real app role would silently test the wrong posture.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("POSTGRES_") and k != "DJANGO_SETTINGS_MODULE"
    }
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(overrides)
    return subprocess.run(
        [sys.executable, "-c", BOOT_SNIPPET],
        capture_output=True,
        text=True,
        env=env,
        # Outside the repo: settings.py calls read_env(".env"), which is a
        # no-op only where no such file exists.
        cwd=REPO_ROOT.parent,
    )


def _identity(result: subprocess.CompletedProcess) -> tuple[str, str]:
    """Pull (USER, PASSWORD) out of a clean boot, asserting the boot was clean."""
    assert result.returncode == 0, result.stderr
    assert BOOT_MARKER in result.stdout, result.stdout
    _, user, password = result.stdout.split()
    return user, password


def test_the_app_role_wins_when_it_is_named():
    """The whole point of the split: the app does not use the superuser login."""
    result = _boot(
        POSTGRES_USER=BOOTSTRAP,
        POSTGRES_PASSWORD="bootstrap-seed",
        POSTGRES_APP_USER=APP_ROLE,
        POSTGRES_APP_PASSWORD="app-secret",
    )
    assert _identity(result) == (APP_ROLE, "app-secret")


@pytest.mark.parametrize(
    "overrides",
    [
        # An install that predates the split: no app role line at all.
        pytest.param({}, id="app-role-absent"),
        # The line exists but was never filled in, which is how a half-edited
        # .env or a template copy looks in the wild.
        pytest.param({"POSTGRES_APP_USER": ""}, id="app-role-blank"),
    ],
)
def test_the_bootstrap_login_is_the_fallback(overrides):
    """Falling back is what keeps a pre-split install booting after an upgrade."""
    result = _boot(
        POSTGRES_USER=BOOTSTRAP,
        POSTGRES_PASSWORD="bootstrap-seed",
        **overrides,
    )
    assert _identity(result) == (BOOTSTRAP, "bootstrap-seed")


def test_a_blank_app_password_falls_back_rather_than_connecting_with_none():
    """The F6 shape: an empty value must not become the password the app uses.

    Only the password is blank here, with the app role named, so a naive
    implementation would authenticate as reeltalk_app with an empty secret
    instead of falling back.
    """
    result = _boot(
        POSTGRES_USER=BOOTSTRAP,
        POSTGRES_PASSWORD="bootstrap-seed",
        POSTGRES_APP_USER=APP_ROLE,
        POSTGRES_APP_PASSWORD="",
    )
    user, password = _identity(result)
    assert user == APP_ROLE, "the app role is named, so it should still be used"
    assert password == "bootstrap-seed", (
        "a blank app password must fall back, not authenticate with an empty secret"
    )


# The DDL is only executed because the compose files mount it. Both shipped
# paths have to do so: a deployer who pulls the prebuilt image is just as
# entitled to the privilege floor as one who builds.
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prebuilt.yml")


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_compose_mounts_the_role_split(compose_file):
    text = (REPO_ROOT / compose_file).read_text()
    assert "/docker-entrypoint-initdb.d/" in text, (
        f"{compose_file} no longer mounts the init script, so a fresh install "
        "silently runs as the bootstrap superuser"
    )
    assert "role-split.sql" in text, f"{compose_file} no longer mounts the DDL"
    # The vars have to reach the db container's own environment: the init script
    # reads them there, and env_file is not the interpolation layer.
    assert "POSTGRES_APP_USER" in text, compose_file
    assert "POSTGRES_APP_PASSWORD" in text, compose_file


def test_the_ddl_scripts_are_present_and_executable():
    """The postgres entrypoint *sources* a non-executable .sh, where the init
    script's early `exit 0` would abort Postgres's own first boot instead of
    skipping the split. The exec bit is therefore load-bearing."""
    init = REPO_ROOT / "db" / "init-app-role.sh"
    ddl = REPO_ROOT / "db" / "role-split.sql"
    assert init.exists() and ddl.exists()
    assert os.access(init, os.X_OK), f"{init} must stay executable"
