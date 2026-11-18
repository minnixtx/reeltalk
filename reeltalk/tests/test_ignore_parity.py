"""The two ignore files must agree about the project's working notes.

`.gitignore` and `.dockerignore` guard two different boundaries, and it is easy to
edit one while meaning both:

* `.gitignore` filters what git sees, so a gitignored file never appears in
  `git status` and never reaches GitHub.
* `.dockerignore` filters the **build context**, which is the working directory,
  not the git index. `Dockerfile` ends with `COPY . /app/`, so a file that exists
  on disk is baked into every published image whether or not git ever tracked it.

That asymmetry is the whole failure. A planning doc that gets a `.gitignore` line
and not a `.dockerignore` line is invisible in both directions at once: clean
`git status` at commit time, and inside the baked image the file simply looks like
part of the app. `test_clean_room.py` walks the tree and would not object either,
because it only looks for provenance telltales.

This happened at `b5bb5d4` (2026-11-10), where one edit added two lines to
`.gitignore` and one to `.dockerignore`, and `REVIEW_LIMITS_PLAN.md` rode in every
image built after it.

**The invariant is deliberately narrow.** The two files differ in many legitimate
places and should: `assets/`, `.git` and `node_modules` are build-only, `htmlcov/`
is git-only, and the two spell `.qwen` and `/images/*` differently to the same
effect. Demanding identical files would be wrong. What has one answer is the
project's own markdown notes: a working note is for the operator's disk and
nowhere else, so it belongs in both lists or in neither.

The second test guards the other half of the same boundary. Only three markdown
files are public (`README.md`, `INSTALL.md`, `REWRITE.md`), and `REWRITE.md` was
once ignored by mistake and deliberately restored, so "public on purpose" is a
decision worth pinning rather than a fact that stays true by itself.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GITIGNORE = REPO_ROOT / ".gitignore"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

# The docs that are public by owner decision (2026-10-24, revisited for
# REWRITE.md). Everything else the project writes for itself is a working note.
PUBLIC_DOCS = ("README.md", "INSTALL.md", "REWRITE.md")


def _entries(path: Path) -> set[str]:
    """The literal patterns a file lists, comments and blanks dropped.

    Deliberately literal. These two files hold plain path names, not a pattern
    language worth interpreting, and a comparison that tried to resolve globs
    would be a second rule to keep correct.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    return {
        line.strip().lstrip("/")
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    }


def _top_level_markdown(entries: set[str]) -> set[str]:
    """Only the entries naming a markdown file at the repository root.

    No slash, `.md` suffix, and not a negation or a glob: those are the working
    notes this rule is about, and nothing else in either file looks like this.
    """
    return {
        entry
        for entry in entries
        if entry.endswith(".md") and "/" not in entry and not entry.startswith("!")
    }


def test_every_ignored_markdown_note_is_also_dockerignored():
    git_notes = _top_level_markdown(_entries(GITIGNORE))
    docker_notes = _top_level_markdown(_entries(DOCKERIGNORE))

    missing = sorted(git_notes - docker_notes)
    assert not missing, (
        f"{', '.join(missing)} is gitignored but not dockerignored, so "
        "`COPY . /app/` bakes it into every published image. Add the same line "
        "to .dockerignore."
    )


@pytest.mark.parametrize("doc", PUBLIC_DOCS)
def test_the_public_docs_are_ignored_by_neither_file(doc):
    """A public doc in either list is a doc that stops shipping, silently."""
    ignored_by = [
        path.name for path in (GITIGNORE, DOCKERIGNORE) if doc in _entries(path)
    ]
    assert not ignored_by, (
        f"{doc} is public by owner decision and is listed in {', '.join(ignored_by)}; "
        "it would vanish from git or from the image without anything failing."
    )
