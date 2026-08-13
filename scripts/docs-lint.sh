#!/usr/bin/env sh
# docs-lint — make "every doc is indexed, every link resolves" a real rule.
#
# Thin wrapper so CI and humans invoke this the same way scrub-lint.sh is invoked.
# Run from anywhere: --test self-tests the checks, no args lints the trees.
# Exit 0 = clean, 1 = findings, 2 = usage/environment error.
set -eu
cd "$(dirname "$0")/.."
exec python3 scripts/docs_lint.py "$@"
