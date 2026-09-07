"""Clean-room guard (REWRITE.md rule 1): no BookWyrm code in this repo.

Scans every project file for telltale strings that may only appear in the
attribution docs. The suite runs both locally and inside the built image
(where there is no .git), so discovery is a filesystem walk with build
artifacts and local-only dirs skipped — not git. If a new file legitimately
needs to contain a telltale string, add it to WHITELISTED_FILES below:
deliberately manual, so each exception is a conscious decision.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The only files allowed to contain telltale strings: the attribution docs
# (required by REWRITE.md rule 5) and this guard itself, which must name its
# targets. Adding anything else is a conscious decision — that's the point.
WHITELISTED_FILES = {
    "README.md",
    "REWRITE.md",
    "PLAN.md",
    "PROGRESS.md",
    "reeltalk/tests/test_clean_room.py",
}

# Top-level dirs that are build artifacts or runtime volumes, not project
# source (static/ holds collectstatic output incl. Django admin assets;
# images/ is the media volume). Only matched as the FIRST path part so app
# static dirs like reeltalk/social/static/ stay in scope.
TOP_LEVEL_SKIP_DIRS = {"images", "static"}

# Skipped anywhere they appear: VCS, caches, third-party code, local state.
SKIP_DIRS = {
    ".git",
    ".pytest_cache",
    ".qwen",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}

# Local-only files (gitignored operator config).
SKIP_FILES = {".env", ".env.dev"}

TELLTALES = ("bookwyrm", "mouse reeve", "anti-capitalist")
ACRL_RE = re.compile(r"\bacrl\b", re.IGNORECASE)


def _project_files():
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT)
        if rel.parts and rel.parts[0] in TOP_LEVEL_SKIP_DIRS:
            continue
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.as_posix() in SKIP_FILES:
            continue
        yield rel


FILES = list(_project_files())

# A guard that scans nothing has silently stopped working — fail loudly.
assert FILES, "clean-room guard found no files to scan"


@pytest.mark.parametrize("rel", FILES, ids=lambda p: p.as_posix())
def test_no_bookwyrm_telltale_strings(rel):
    if rel.as_posix() in WHITELISTED_FILES:
        pytest.skip("whitelisted (attribution docs or this guard itself)")
    content = (REPO_ROOT / rel).read_text(errors="ignore")
    lowered = content.lower()
    hits = [t for t in TELLTALES if t in lowered]
    if ACRL_RE.search(content):
        hits.append("acrl")
    assert not hits, f"{rel.as_posix()} contains BookWyrm telltale(s): {hits}"
