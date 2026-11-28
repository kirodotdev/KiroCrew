#!/usr/bin/env python3
"""Fail CI if old docs, version-stamped tests or build artifacts return."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "build", "dist", "__pycache__"}
VERSIONED_TEST = re.compile(r"test_0\d{2}(?:_|\.py)")
# Canonical documentation lives in exactly two places: the project root
# (README.md + CHANGELOG.md) and the docs/ tree. Anything else at the root is a
# stray legacy report (old audits, scratch notes) and is rejected, which is what
# the one-document policy was created to prevent from creeping back.
CANONICAL_ROOT_DOCS = {"README.md", "CHANGELOG.md"}


def problems(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if any(part in IGNORED for part in rel.parts):
            continue
        posix = rel.as_posix()
        if posix in CANONICAL_ROOT_DOCS or rel.parts[0] == "docs":
            continue
        errors.append(f"extra Markdown outside canonical docs (root README/CHANGELOG or docs/): {posix}")
    for path in (root / "tests").glob("test_*.py"):
        if VERSIONED_TEST.match(path.name):
            errors.append(f"version-prefixed test file: tests/{path.name}")
    # This runs in Actions checkout; inspect the index, not the (ignored) local build output.
    try:
        result = subprocess.run(["git", "ls-files", "--", "build", "dist"], cwd=root,
                                capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass  # Source archives intentionally have no .git/index.
    else:
        for item in result.stdout.splitlines():
            errors.append(f"tracked build artifact: {item}")
    return sorted(errors)


def main() -> int:
    errors = problems()
    if errors:
        for error in errors:
            print("FAIL:", error, file=sys.stderr)
        return 1
    print("PASS: one current README, feature-named tests and no tracked build outputs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
