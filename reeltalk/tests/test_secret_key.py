"""F6: a production boot must refuse a public SECRET_KEY.

The settings module is already imported by the test session, so the guard
cannot be exercised by mutating ``os.environ`` in-process: reloading the
module would rewrite the settings every other test reads. Each case instead
boots a real interpreter with a scrubbed environment and a cwd outside the
repo (so settings.py's ``read_env(".env")`` finds no operator file), which is
what a container does at start.

Every case asserts the *reason* for the outcome, not just the exit code: a
bare non-zero exit is also produced by a typo in the import path, and the
success cases print what the module actually loaded, so a boot that died for
an unrelated reason cannot pass.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from reeltalk.settings import DEV_SECRET_KEY

REPO_ROOT = Path(__file__).resolve().parents[2]

# A key long enough to be plausible, of the kind setup.sh generates.
REAL_KEY = "Zq7Xf2mN0pLs8KdRt4VyBn6WcHj1GhTgUe5IoPaS9DkM3"

# Printed on a clean boot, followed by DEBUG and the key length, so a reader
# can see the module really ran under the posture the case asked for.
BOOT_MARKER = "BOOT_OK"

BOOT_SNIPPET = f"import reeltalk.settings as s; print('{BOOT_MARKER}', s.DEBUG)"


def _boot(**overrides: str) -> subprocess.CompletedProcess:
    """Import reeltalk.settings in a fresh interpreter with a controlled env.

    SECRET_KEY and DEBUG are stripped from the inherited environment first:
    pytest-env pins both for the suite, and a subprocess that inherited them
    would silently test the wrong posture.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"SECRET_KEY", "DEBUG", "DJANGO_SETTINGS_MODULE"}
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


def _assert_refused(result: subprocess.CompletedProcess) -> None:
    assert result.returncode != 0, f"boot succeeded, expected refusal: {result.stdout}"
    assert "ImproperlyConfigured" in result.stderr, result.stderr
    # The refusal has to be actionable: someone who has never seen this repo
    # still needs to be told where a real key comes from.
    assert "SECRET_KEY" in result.stderr, result.stderr
    assert "setup.sh" in result.stderr, result.stderr


@pytest.mark.parametrize(
    "overrides",
    [
        # The shape a real misconfiguration takes: DEBUG is simply not set, so
        # it defaults to false, and no key was ever generated.
        pytest.param({}, id="no-key-and-no-debug"),
        pytest.param({"DEBUG": "false"}, id="no-key-debug-false"),
        # `.env.example` ships SECRET_KEY= blank, so this is the
        # copy-the-template-and-skip-setup.sh path.
        pytest.param({"DEBUG": "false", "SECRET_KEY": ""}, id="empty-key"),
        pytest.param({"DEBUG": "false", "SECRET_KEY": "   "}, id="whitespace-key"),
        # The published fallback, named explicitly in an env file.
        pytest.param(
            {"DEBUG": "false", "SECRET_KEY": DEV_SECRET_KEY},
            id="fallback-key",
        ),
    ],
)
def test_production_boot_refuses_a_public_key(overrides):
    _assert_refused(_boot(**overrides))


def test_production_boot_accepts_a_real_key():
    result = _boot(DEBUG="false", SECRET_KEY=REAL_KEY)
    assert result.returncode == 0, result.stderr
    assert f"{BOOT_MARKER} False" in result.stdout


def test_debug_boot_keeps_the_dev_fallback():
    """DEBUG=true is the one posture allowed to run on the published key."""
    result = _boot(DEBUG="true")
    assert result.returncode == 0, result.stderr
    assert f"{BOOT_MARKER} True" in result.stdout


def test_the_fallback_constant_is_the_published_one():
    """Pin the constant's value, so changing it is a deliberate act."""
    assert DEV_SECRET_KEY == "insecure-dev-only-key-change-me"
