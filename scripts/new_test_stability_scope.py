#!/usr/bin/env python3
"""List test files a non-leaf PR adds or modifies for the stability repeat gate.

The existing leaf-test path (ci.yml LEAF_REPEAT loop + leaf_test_scope.py)
already repeats test-only diffs.  This script covers the gap: a PR that adds
source changes alongside new or modified test files.

Outputs one repo-relative path per line, e.g. ``test/test_foo.py``.
Exits with nothing when the set is empty.

Consumes STABILITY_BASE (the PR's base.sha from the CI event payload)
or the ``--base`` argument.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

# leaf_test_scope, ratchet_scope and platform_collect_ignore_helpers live
# in scripts/ and test/ respectively -- both paths added to sys.path once.
_scripts = str(Path(__file__).resolve().parent)
_test = str(Path(__file__).resolve().parent.parent / "test")
for _p in (_scripts, _test):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from leaf_test_scope import _LEAF_NAME, REPO_ROOT, corpus_gates  # noqa: E402
from platform_collect_ignore_helpers import platform_ignore_names  # noqa: E402
from ratchet_scope import changed_paths_at, resolve_base  # noqa: E402


def filter_stability_scope(paths: list[str], gates: set[str]) -> list[str]:
    """Return paths that are leaf test modules, not corpus gates or helpers.

    A leaf test module matches ``test_*.py`` directly under ``test/``.
    ``conftest.py``, shared helpers, data files and corpus-scanning gates are
    all excluded.  Pure function: testable without git or disk I/O.
    """
    result = []
    for path in paths:
        if not path.startswith("test/"):
            continue
        name = path[len("test/") :]
        if not _LEAF_NAME.match(name):
            continue
        if path in gates:
            continue
        result.append(path)
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="",
        help="base SHA (default: $STABILITY_BASE)",
    )
    args = parser.parse_args(argv)

    base = args.base or os.environ.get("STABILITY_BASE", "")
    if not base:
        print("error: pass --base <sha> or set STABILITY_BASE", file=sys.stderr)
        return 2

    # resolve_base prints ::notice:: / ::warning:: workflow commands to stdout;
    # redirect them to stderr so they do not contaminate the file-list output
    # the caller captures with `> $TARGETS_FILE`.
    with contextlib.redirect_stdout(sys.stderr):
        resolved = resolve_base(base)

    gates = set(corpus_gates(REPO_ROOT))

    # Named paths bypass pytest_ignore_collect; apply the Windows exclusion
    # list so Linux-only suites never reach the Windows stability job.
    if sys.platform == "win32":
        gates |= {f"test/{name}" for name in platform_ignore_names("win32")}

    for path in filter_stability_scope(changed_paths_at(resolved), gates):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
