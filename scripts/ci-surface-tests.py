#!/usr/bin/env python3
"""Select the test files that must still run when a diff touches ONE surface.

Why this exists
---------------
Most of this repo's tests only exercise their own surface, so a backend-only
diff has no way to affect the frontend specs (and vice versa). Skipping the
opposite surface's tests on a single-surface diff is a large CI saving.

The catch is that a minority of tests are *cross-surface parity guards*: they
live in one suite but assert against the OTHER surface's source (e.g. a pytest
test that reads ``website/src/utils/sanitize.ts`` to prove the redaction mirror
still matches, or an Electron test that reads
``src/kiro_crew/computer_use/permissions.py``). Skipping one of those is how a
drift bug ships green.

So this script does NOT try to enumerate the guards (a list that must be
complete to be safe). It inverts the question and enumerates only the files it
can *positively prove* are single-surface. Everything else -- every guard, every
file it cannot classify, every file added tomorrow -- lands in the must-run set
and keeps running. A mistake in the heuristic therefore costs CI time, not a
skipped guard.

Usage
-----
    ci-surface-tests.py --surface backend    # pytest files that must still run
    ci-surface-tests.py --surface frontend   # vitest/electron specs that must run

Prints one repo-relative path per line (empty output = nothing needs to run).
``--summary`` adds counts on stderr. Exit 2 on a usage/environment error, in
which case the caller MUST fall back to running the full suite.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# What counts as "the other surface" for a BACKEND (pytest) file.
#
# Deliberately broad: a bare mention of the frontend tree, the Electron shell,
# the packaging/signing inputs, the skills tree or a TS/TSX filename is enough
# to keep the file in the must-run set. Over-matching only costs CI time.
# ---------------------------------------------------------------------------
_BACKEND_FOREIGN = re.compile(
    r"""
      website              # the frontend tree, in any quoting/joining style
    | electron
    | packaging            # build-desktop.sh, signing entitlements, ...
    | node_modules
    | \bskills\b           # repo-root skills/ tree (read by some guards)
    | \bdocker\b
    | \.tsx?\b             # a TypeScript filename appearing in an assertion
    | \.mjs\b
    | \.plist\b
    | entitlements
    | \bnpm\b
    | vitest
    | playwright
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ---------------------------------------------------------------------------
# What counts as "the other surface" for a FRONTEND (vitest / electron) file.
#
# Two distinct escape styles both have to be caught, which is exactly what an
# eyeball grep kept missing:
#   1. string literals   -- '../../../src/kiro_crew/connections/registry.json'
#   2. path segments     -- path.resolve(__dirname, '..', '..', '..', 'test')
# ---------------------------------------------------------------------------
_FRONTEND_FOREIGN = re.compile(
    r"""
      (?:\.\./){3,}                                  # ../../../ escape upward
    | \.\.[\\/]\.\.[\\/]\.\.                          # ../../.. joined form
    | (?:['"]\.\.['"]\s*,\s*){2,}                     # '..', '..', ... segments
    | kiro_crew                                       # backend package by name
    | \bpackaging\b
    | \bskills\b
    | test[\\/]fixtures                               # shared parity fixtures
    | \bdocker\b
    | \.py\b                                          # a Python file in an assertion
    """,
    re.VERBOSE,
)

# Backend test roots. MUST cover every entry in setup.cfg `testpaths` --
# test_ci_surface_tests.py asserts exactly that, because an unenumerated root is
# worse than an unclassified file: the reduced run passes explicit paths, so a
# root that is never walked never runs at all (it does not fall back to
# "keep running" the way an unclassifiable file does).
_BACKEND_ROOTS = ("test", "transfer", "src/kiro_crew/apps/builtins")
_BACKEND_GLOBS = ("test_*.py",)

# Frontend spec roots. MUST cover every root in vitest's `test.include`
# (website/vite.config.ts) -- test_ci_surface_tests.py asserts that, for the same
# reason as _BACKEND_ROOTS: an unenumerated root is never walked, so it never
# runs at all rather than falling back to "keep running".
# website/electron is scanned too (its guards belong to the always-on
# electron-test job); the frontend-test scope step filters it out before handing
# paths to vitest.
_FRONTEND_SPECS = (
    ("website/src", ("*.test.ts", "*.test.tsx")),
    ("website/integration", ("*.test.ts", "*.test.tsx")),
    ("website/electron", ("*.test.js",)),
)


def _repo_root() -> Path:
    """Resolve the repo root from this file's location (scripts/<me>)."""
    return Path(__file__).resolve().parents[1]


def _iter_files(root: Path, rel_dir: str, globs: tuple[str, ...]) -> list[Path]:
    base = root / rel_dir
    if not base.is_dir():
        return []
    found: list[Path] = []
    for pattern in globs:
        found.extend(base.rglob(pattern))
    return [p for p in found if p.is_file() and "node_modules" not in p.parts]


def _is_cross_surface(path: Path, pattern: re.Pattern[str]) -> bool:
    """True when the file mentions the other surface -- or cannot be read.

    Fail CLOSED: an unreadable/undecodable file is treated as cross-surface so
    it keeps running. Never let an IO error silently drop a guard.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return bool(pattern.search(text))


def _is_windows() -> bool:
    """Whether the emitted paths will be consumed by a Windows pytest run.

    A named seam rather than an inline ``os.name`` read so tests can exercise the
    Windows branch without simulating the platform: patching ``os.name`` globally
    also switches ``pathlib.Path`` to ``WindowsPath``, which cannot be
    instantiated on POSIX.
    """
    return os.name == "nt"


def _windows_collect_ignore(root: Path) -> frozenset[str]:
    """Bare test filenames that ``test/conftest.py`` refuses to collect on Windows.

    Read from the same file conftest reads, so the two cannot drift.
    """
    listfile = root / "test" / "windows-collect-ignore.txt"
    try:
        text = listfile.read_text(encoding="utf-8")
    except OSError:
        # Fail OPEN here, deliberately. A missing list must not silently empty
        # the target set (that would skip real coverage); emitting the unfiltered
        # list at worst reproduces today's behaviour.
        return frozenset()
    names = (ln.split("#", 1)[0].strip() for ln in text.splitlines())
    return frozenset(n for n in names if n)


def collect(surface: str) -> list[str]:
    """Repo-relative paths that must still run for a single-surface diff."""
    root = _repo_root()
    must_run: list[str] = []

    if surface == "backend":
        for rel_dir in _BACKEND_ROOTS:
            for path in _iter_files(root, rel_dir, _BACKEND_GLOBS):
                if _is_cross_surface(path, _BACKEND_FOREIGN):
                    must_run.append(path.relative_to(root).as_posix())
    else:
        for rel_dir, globs in _FRONTEND_SPECS:
            for path in _iter_files(root, rel_dir, globs):
                if _is_cross_surface(path, _FRONTEND_FOREIGN):
                    must_run.append(path.relative_to(root).as_posix())

    if _is_windows():
        # These paths are consumed as EXPLICIT pytest arguments, and an explicit
        # argument bypasses both `collect_ignore` and the `pytest_ignore_collect`
        # hook -- verified, not assumed. So conftest's Windows exclusion does not
        # protect this path, and emitting a POSIX-only suite here collects it and
        # fails the Windows shards on any diff that takes the reduced scope.
        #
        # Split the string rather than going through pathlib: these are already
        # `as_posix()` forms, so the separator is known, and it keeps the filter
        # independent of which Path flavour the host provides.
        ignored = _windows_collect_ignore(root)
        must_run = [rel for rel in must_run if rel.rsplit("/", 1)[-1] not in ignored]

    return sorted(set(must_run))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--surface",
        required=True,
        choices=("backend", "frontend"),
        help="which suite to select files from",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="also print counts to stderr",
    )
    args = parser.parse_args(argv)

    paths = collect(args.surface)

    if args.summary:
        root = _repo_root()
        if args.surface == "backend":
            total = sum(
                len(_iter_files(root, d, _BACKEND_GLOBS)) for d in _BACKEND_ROOTS
            )
        else:
            total = sum(len(_iter_files(root, d, g)) for d, g in _FRONTEND_SPECS)
        print(
            f"{args.surface}: {len(paths)} must-run / {total} total "
            f"({total - len(paths)} provably single-surface, skippable)",
            file=sys.stderr,
        )

    for rel in paths:
        print(rel)
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    sys.exit(main())
