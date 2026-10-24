"""setup.sh owns the whole .env, so its contract is mostly a list of don'ts.

The script copies .env.example to .env, asks a short set of questions, and
generates the secrets. What has to hold is mostly negative: never overwrite a
value that is set, never write a blank for a variable the app parses as a bool
or an int, and never let a scripted install come up on the template's
placeholder domain. Each case runs the real script in a throwaway directory
holding a copy of the script and its template, so the checkout's own .env is
never the file under test, and with a scrubbed environment, so nothing the
operator happens to export can decide what passes.

Reading the result back goes through environs in a subprocess whose environment
is exactly the generated file's lines, which is the chain that matters: compose
turns env_file into the container's environment, and settings.py then parses
that. Asserting against the file's text alone would pass on a value environs
refuses to parse.
"""

import json
import os
import pty
import select
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reeltalk.settings import PRIVATE_NETWORKS

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "setup.sh"
TEMPLATE = REPO_ROOT / ".env.example"
PLACEHOLDER = "your-instance.example.com"

# settings.py reads these with env.bool and env.int, and both raise on an empty
# string rather than falling back to the default. A rewrite that uncommented
# every template line and wrote KEY= for whatever the deployer skipped produces
# a file that refuses to boot, so this is the set a generated file gets checked
# against. env.list is the quieter half of the same trap: an empty value yields
# the empty list instead of the default, which for TRUSTED_PROXIES means
# "believe no proxy" with nothing printed.
BOOL_VARS = {
    "DEBUG",
    "SECURE_COOKIES",
    "COOKIES_FOLLOW_SCHEME",
    "EMAIL_USE_TLS",
    "SECURE_SSL_REDIRECT",
}
INT_VARS = {
    "WEB_PORT",
    "EMAIL_PORT",
    "POSTGRES_PORT",
    "DATA_UPLOAD_MAX_MEMORY_MiB",
    "INBOX_MAX_BODY_BYTES",
    "SECURE_HSTS_SECONDS",
}
TYPED_VARS = BOOL_VARS | INT_VARS

# The keys compose resolves through ${VAR:?required}, which is a layer env_file
# does not feed: they have to be present in the file setup.sh writes or the
# stack fails before a container starts.
COMPOSE_REQUIRED = ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD")

# The questions a fresh run asks, in order, on the https path. DOMAIN first,
# then the endpoint block, the transport pair, mail, TMDB, LAN peers, the zone,
# and the confirmation.
DEFAULT_ANSWERS = [
    "films.example.com",  # DOMAIN
    "",  # reverse proxy on this host -> no
    "",  # extra origins -> none
    "",  # WEB_PORT -> 3030
    "",  # transport -> https
    "",  # SECURE_SSL_REDIRECT -> off
    "",  # SECURE_HSTS_SECONDS -> 0
    "",  # EMAIL_HOST -> mail off
    "",  # TMDB key -> skipped
    "",  # LAN peers -> none
    "",  # TIME_ZONE -> UTC
    "y",  # write it
]


def _clean_env():
    """A scrubbed environment: PATH only, so no operator variable leaks in."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _sandbox(tmp_path):
    """A directory holding the script and its template, like a fresh clone."""
    shutil.copy(SCRIPT, tmp_path / "setup.sh")
    shutil.copy(TEMPLATE, tmp_path / ".env.example")
    return tmp_path


def _run(sandbox, *args):
    """Run the script with no TTY, which is the scripted-install shape."""
    return subprocess.run(
        ["bash", "setup.sh", *args],
        cwd=sandbox,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


def _run_tty(sandbox, answers):
    """Run the script on a real pty and answer its prompts in order.

    The script only interviews when stdin is a terminal, so a pipe would test a
    different branch. Each answer goes out once the script has printed a prompt
    ending in ": ", which keeps the exchange in step instead of racing it.
    """
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["bash", "setup.sh"],
        cwd=sandbox,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=_clean_env(),
    )
    os.close(slave)
    queue = list(answers)
    tail = ""
    chunks = []
    while True:
        ready, _, _ = select.select([master], [], [], 5.0)
        if not ready:
            break
        try:
            chunk = os.read(master, 1024)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk.decode(errors="replace"))
        tail = (tail + chunks[-1])[-64:]
        if queue and tail.endswith(": "):
            os.write(master, (queue.pop(0) + "\n").encode())
    os.close(master)
    if proc.poll() is None:
        proc.kill()
        proc.wait()
        pytest.fail(
            "setup.sh is still waiting for input with "
            f"{len(queue)} answer(s) unused. Output so far:\n{''.join(chunks)}"
        )
    return proc.returncode, "".join(chunks)


def _preset(sandbox, **values):
    """Write a .env the way a deployer's hand edit would, blanks included."""
    lines = [f"{key}={value}" for key, value in values.items()]
    (sandbox / ".env").write_text("\n".join(lines) + "\n")


def _written(env_file):
    """The uncommented assignments in a .env, as a dict."""
    out = {}
    for line in Path(env_file).read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            out[key] = value
    return out


# Run in a process whose environment is the generated file, so the assertion is
# about what the app would actually resolve. Each spec entry carries the same
# default settings.py passes, because "absent falls back" is only meaningful
# against that default; a probe with no default would just report a raise for
# every key the script correctly left alone. JSON in and out keeps the probe
# free of quoting tricks.
PROBE = """
import environs, json, os
env = environs.Env()
resolved = {}
for key, kind, default in json.loads(os.environ["RT_PROBE"]):
    try:
        if default is None:
            value = getattr(env, kind)(key)
        else:
            value = getattr(env, kind)(key, default=default)
        resolved[key] = repr(value)
    except Exception as exc:
        resolved[key] = "RAISES " + type(exc).__name__
print(json.dumps(resolved))
"""


def _resolve(env_file, spec):
    """What environs makes of the file's own values, in a clean interpreter.

    spec is a list of (key, kind, default) triples; default None means the key
    is expected to be present.
    """
    probe_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "RT_PROBE": json.dumps([list(entry) for entry in spec]),
    }
    probe_env.update(_written(env_file))
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        env=probe_env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_no_tty_refuses_the_placeholder_domain(tmp_path):
    """The regression that actually bit: a scripted install must not mint on a
    domain nobody owns. The placeholder is non-empty, so emptiness was not the
    check that caught it."""
    sandbox = _sandbox(tmp_path)
    result = _run(sandbox)
    assert result.returncode != 0, result.stdout
    assert "DOMAIN" in result.stderr
    # The file it created is left behind with the placeholder intact, because
    # that is the file the deployer now has to edit.
    assert _written(sandbox / ".env")["DOMAIN"] == PLACEHOLDER


def test_no_tty_fills_every_blank_secret(tmp_path):
    sandbox = _sandbox(tmp_path)
    _preset(sandbox, DOMAIN="films.example.com", SECRET_KEY="", POSTGRES_PASSWORD="")
    result = _run(sandbox)
    assert result.returncode == 0, result.stderr
    written = _written(sandbox / ".env")
    for key in ("SECRET_KEY", "POSTGRES_PASSWORD"):
        assert len(written[key]) > 30, f"{key} was not generated: {written[key]!r}"


def test_app_password_only_when_the_login_is_named(tmp_path):
    """Inventing a password for a role that does not exist points the app at a
    login that cannot authenticate, so an install without POSTGRES_APP_USER
    gets no app password at all."""
    sandbox = _sandbox(tmp_path)
    _preset(sandbox, DOMAIN="films.example.com", POSTGRES_PASSWORD="")
    assert _run(sandbox).returncode == 0
    assert "POSTGRES_APP_PASSWORD" not in _written(sandbox / ".env")


def test_rerunning_changes_not_one_byte(tmp_path):
    """This is what makes .env hand-editable and the script safe to re-run. A
    re-run once destroyed a deployer's file by regenerating every blank."""
    sandbox = _sandbox(tmp_path)
    _preset(sandbox, DOMAIN="films.example.com", SECRET_KEY="", POSTGRES_PASSWORD="")
    assert _run(sandbox).returncode == 0
    before = (sandbox / ".env").read_bytes()
    assert _run(sandbox).returncode == 0
    assert (sandbox / ".env").read_bytes() == before


def test_a_value_that_is_set_is_never_overwritten(tmp_path):
    sandbox = _sandbox(tmp_path)
    _preset(
        sandbox,
        DOMAIN="other.example.com",
        SECRET_KEY="a-key-somebody-kept",
        POSTGRES_PASSWORD="a-password-somebody-kept",
    )
    assert _run(sandbox).returncode == 0
    written = _written(sandbox / ".env")
    assert written["DOMAIN"] == "other.example.com"
    assert written["SECRET_KEY"] == "a-key-somebody-kept"
    assert written["POSTGRES_PASSWORD"] == "a-password-somebody-kept"


def test_blanking_one_secret_rotates_only_that_one(tmp_path):
    """Blank means fill, set means leave alone. That is the whole repair
    vocabulary, and a rotation has to be something the deployer asked for."""
    sandbox = _sandbox(tmp_path)
    _preset(
        sandbox, DOMAIN="films.example.com", SECRET_KEY="", POSTGRES_PASSWORD="keep-me"
    )
    assert _run(sandbox).returncode == 0
    first = _written(sandbox / ".env")
    assert first["POSTGRES_PASSWORD"] == "keep-me"

    (sandbox / ".env").write_text(
        "\n".join(f"{k}={'' if k == 'SECRET_KEY' else v}" for k, v in first.items())
        + "\n"
    )
    assert _run(sandbox).returncode == 0
    second = _written(sandbox / ".env")
    assert second["SECRET_KEY"] != first["SECRET_KEY"]
    assert second["POSTGRES_PASSWORD"] == "keep-me"


def test_no_typed_variable_is_present_but_empty(tmp_path):
    """The constraint that shapes the design: a blank line is a value to
    environs, and for the typed settings that value does not parse."""
    sandbox = _sandbox(tmp_path)
    assert _run_tty(sandbox, DEFAULT_ANSWERS)[0] == 0
    written = _written(sandbox / ".env")
    assert [key for key in TYPED_VARS if key in written and written[key] == ""] == []


def test_the_values_written_are_the_values_the_app_resolves(tmp_path):
    sandbox = _sandbox(tmp_path)
    assert _run_tty(sandbox, DEFAULT_ANSWERS)[0] == 0
    env_file = sandbox / ".env"
    written = _written(env_file)
    spec = [
        (key, "bool" if key in BOOL_VARS else "int", None)
        for key in written
        if key in TYPED_VARS
    ]
    resolved = _resolve(env_file, spec)
    for key, kind, _default in spec:
        parsed = resolved[key]
        raw = written[key]
        assert not parsed.startswith("RAISES"), f"{key}={raw!r} -> {parsed}"
        # The probe hands back repr(), so the comparison has to be against what
        # the typed string means, not against the string itself: environs turns
        # "true" into True and "3030" into 3030.
        if kind == "int":
            expected = repr(int(raw))
        else:
            expected = repr(raw.lower() in ("true", "1", "yes", "on"))
        assert parsed == expected, f"{key}: wrote {raw!r}, app reads {parsed}"


def test_a_skipped_question_leaves_the_line_commented(tmp_path):
    """An unanswered optional must stay absent, not become an empty assignment,
    so the app's own default applies. TRUSTED_PROXIES is the case worth
    naming: present-but-empty means "trust no proxy" and prints nothing."""
    sandbox = _sandbox(tmp_path)
    assert _run_tty(sandbox, DEFAULT_ANSWERS)[0] == 0
    written = _written(sandbox / ".env")
    for key in ("TRUSTED_PROXIES", "FEDERATION_ALLOWED_NETWORKS", "EMAIL_HOST"):
        assert key not in written, (
            f"{key}={written[key]!r} should have stayed commented"
        )
    # And absent really does fall back to the shipped default, which the empty
    # value would not. The default comes from settings rather than being
    # restated here, so this cannot drift from the ranges the app ships.
    resolved_proxies = _resolve(
        sandbox / ".env", [("TRUSTED_PROXIES", "list", PRIVATE_NETWORKS)]
    )
    assert resolved_proxies["TRUSTED_PROXIES"] == repr(PRIVATE_NETWORKS)


def test_answers_land_in_the_file(tmp_path):
    sandbox = _sandbox(tmp_path)
    answers = [
        "https://films.example.com/",  # a pasted URL, not a bare host
        "y",  # proxy on this host -> WEB_BIND
        "http://192.168.1.50:3030,https://www.example.com",
        "3031",
        "https",
        "y",  # SECURE_SSL_REDIRECT
        "31536000",
        "mail.example.com",  # EMAIL_HOST
        "587",
        "reeltalk@mail.example.com",
        "p@ss\\word$1",
        "y",
        "reeltalk",
        "",
        "",
        "",
        "Europe/Berlin",
        "y",
    ]
    assert _run_tty(sandbox, answers)[0] == 0
    written = _written(sandbox / ".env")
    assert written["DOMAIN"] == "films.example.com"
    assert written["WEB_BIND"] == "127.0.0.1"
    assert written["WEB_PORT"] == "3031"
    assert written["SECURE_SSL_REDIRECT"] == "true"
    assert written["SECURE_HSTS_SECONDS"] == "31536000"
    assert written["EMAIL_HOST"] == "mail.example.com"
    assert written["EMAIL_HOST_PASSWORD"] == "p@ss\\word$1"
    assert written["EMAIL_SENDER_NAME"] == "reeltalk"
    assert written["TIME_ZONE"] == "Europe/Berlin"
    # The origins answer both halves of "may this request be answered".
    assert (
        written["CSRF_TRUSTED_ORIGINS"]
        == "http://192.168.1.50:3030,https://www.example.com"
    )
    assert (
        written["ALLOWED_HOSTS"]
        == "films.example.com,localhost,192.168.1.50,www.example.com"
    )


def test_answering_no_at_the_end_writes_nothing(tmp_path):
    sandbox = _sandbox(tmp_path)
    code, output = _run_tty(sandbox, ["films.example.com"] + [""] * 10 + ["n"])
    assert code == 0
    assert "Nothing written" in output
    assert _written(sandbox / ".env")["DOMAIN"] == PLACEHOLDER


def test_a_backslash_survives_an_answer(tmp_path):
    """set_var used awk -v, which eats backslash escapes. The generated secrets
    are URL-safe base64 so they were never at risk; a typed answer is."""
    sandbox = _sandbox(tmp_path)
    _preset(sandbox, DOMAIN="C:\\Users\\films", SECRET_KEY="x", POSTGRES_PASSWORD="y")
    assert _run(sandbox).returncode == 0
    assert _written(sandbox / ".env")["DOMAIN"] == "C:\\Users\\films"


def test_the_file_carries_what_compose_interpolation_needs(tmp_path):
    """env_file and interpolation are different layers: ${VAR:?required} in the
    compose files resolves from the project .env, never from env_file, so a
    generated file missing one of these fails before any container starts."""
    sandbox = _sandbox(tmp_path)
    assert _run_tty(sandbox, DEFAULT_ANSWERS)[0] == 0
    written = _written(sandbox / ".env")
    for key in COMPOSE_REQUIRED:
        assert written.get(key), f"{key} is missing from the generated file"


def _compose_probe(env_file, compose_file):
    """`docker compose config -q` with the generated file as the project env.

    The subprocess gets PATH and HOME and nothing else, on purpose. Compose
    resolves ${VAR:?required} from the process environment *before* any env
    file, so a host that happens to export POSTGRES_* satisfies the guard from
    the shell and passes a file that cannot boot. That is not hypothetical: the
    shell this suite runs under exports six of them, and the check silently
    passed on a file with POSTGRES_PASSWORD stripped until they were scrubbed.
    """
    return subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "rtsetup",
            "-f",
            str(compose_file),
            "--env-file",
            str(env_file),
            "config",
            "-q",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
        },
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the compose CLI")
@pytest.mark.parametrize(
    "compose_file",
    ["docker-compose.yml", "docker-compose.prebuilt.yml"],
)
def test_compose_accepts_the_generated_file(tmp_path, compose_file):
    """The only check that catches the interpolation variables being wrong. It
    runs on a host with docker and skips inside the gate container, which has
    no docker CLI, so the assertion above is what covers this in the gate."""
    sandbox = _sandbox(tmp_path)
    assert _run_tty(sandbox, DEFAULT_ANSWERS)[0] == 0
    result = _compose_probe(sandbox / ".env", REPO_ROOT / compose_file)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the compose CLI")
def test_the_compose_check_actually_bites(tmp_path):
    """The negative control for the check above.

    A green config -q is only worth something if the same command fails on a
    file that cannot boot, so strip one of the ${VAR:?required} keys and assert
    it does. This is what would have caught the version of this test that
    inherited POSTGRES_* from the shell and passed on a broken file."""
    sandbox = _sandbox(tmp_path)
    assert _run_tty(sandbox, DEFAULT_ANSWERS)[0] == 0
    env_path = sandbox / ".env"
    kept = [
        line
        for line in env_path.read_text().splitlines()
        if not line.startswith("POSTGRES_PASSWORD=")
    ]
    env_path.write_text("\n".join(kept) + "\n")
    assert "POSTGRES_PASSWORD" not in _written(env_path)

    result = _compose_probe(env_path, REPO_ROOT / "docker-compose.yml")
    assert result.returncode != 0, "config -q accepted a file with no POSTGRES_PASSWORD"
    assert "POSTGRES_PASSWORD" in result.stderr + result.stdout
