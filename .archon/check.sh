#!/usr/bin/env bash
# Fast, repeatable checks for Archon's dev-cycle loop.
set -euo pipefail

git diff --check HEAD
bash -n .archon/check.sh
python3 scripts/docs_lint.py --test
./scripts/docs-lint.sh
archon validate workflows dev-cycle
