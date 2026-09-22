"""M0 smoke test: the skeleton serves a page and Django's checks pass."""

from pathlib import Path

import pytest
from django.test import Client

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.django_db
def test_index_redirects_to_setup_on_fresh_instance():
    # A fresh instance with no admin bounces to the first-run wizard (R12).
    response = Client().get("/")
    assert response.status_code == 302
    assert response.url == "/setup/"


def test_no_template_has_an_unterminated_hash_comment():
    # ``{# … #}`` is single-line only. Written across a line break, Django
    # does not treat it as a comment at all — it renders the markers and the
    # prose between them straight onto the page (R82 shipped one this way and
    # it showed on every admin's own profile). ``{% comment %}`` is the
    # multi-line form; this project uses one ``{# … #}`` per line instead.
    offenders = []
    for path in REPO_ROOT.rglob("*.html"):
        if {".git", "node_modules", ".venv"} & set(path.parts):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if "{#" in line and "#}" not in line:
                where = path.relative_to(REPO_ROOT)
                offenders.append(f"{where}:{lineno}: {line.strip()}")
    assert not offenders, (
        "unterminated hash comment would print as page text:\n" + "\n".join(offenders)
    )
