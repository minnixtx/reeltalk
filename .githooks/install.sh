#!/usr/bin/env bash
#
# Point git at the versioned hooks in .githooks/ so this clone enforces the
# no-co-author-trailer rule. Idempotent -- safe to run any number of times.
#
#   bash .githooks/install.sh
#
# core.hooksPath is per-clone local config, so it has to be set once per clone.
# ./setup.sh calls this automatically.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if [ ! -d .githooks ]; then
  echo "attribution-guard: no .githooks directory here -- nothing to install." >&2
  exit 1
fi

git config core.hooksPath .githooks
chmod +x .githooks/commit-msg .githooks/pre-push 2>/dev/null || true

echo "attribution-guard installed."
echo "  core.hooksPath = $(git config --get core.hooksPath)"
echo "  commit-msg     : $([ -x .githooks/commit-msg ] && echo active || echo 'NOT EXECUTABLE')"
echo "  pre-push       : $([ -x .githooks/pre-push ] && echo active || echo 'NOT EXECUTABLE')"
