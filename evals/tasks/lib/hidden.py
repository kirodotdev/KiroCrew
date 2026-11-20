"""Shared driver for a case's ``hidden/check.py``.

``python hidden/check.py ORIGIN`` clones ORIGIN's ``trunk`` into a temporary
directory, runs the case's ``test_*`` functions there in a child interpreter
(the agent's code is imported only in that child, with a time limit), and
prints one JSON object::

    {"pass": bool, "parts": {"test_name": bool, ...}, "counters": {}, "notes": [...]}

A case's check.py defines its ``test_*`` functions and ends with
``main(globals())``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from typing import Any, Callable

#: Seconds the child running the tests may take.
TEST_TIMEOUT_S = 120


def _run_inside(tests: dict[str, Callable[[], Any]]) -> dict[str, Any]:
    parts: dict[str, bool] = {}
    notes: list[str] = []
    for name in sorted(tests):
        try:
            tests[name]()
            parts[name] = True
        except Exception:  # noqa: BLE001 - every failure is a failed part
            parts[name] = False
            last = traceback.format_exc().strip().splitlines()[-1]
            notes.append(f"{name}: {last[:300]}")
    return {
        "pass": bool(parts) and all(parts.values()),
        "parts": parts,
        "counters": {},
        "notes": notes,
    }


def _export_main(origin: str, dest: str) -> str:
    """Clone ORIGIN's trunk into DEST; return an error line, or "" on success."""
    proc = subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--branch", "trunk", origin, dest],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return "" if proc.returncode == 0 else f"clone failed: {proc.stderr.strip()[:300]}"


def main(namespace: dict[str, Any]) -> None:
    tests = {k: v for k, v in namespace.items() if k.startswith("test_") and callable(v)}
    if len(sys.argv) == 3 and sys.argv[1] == "--inside":
        sys.path.insert(0, sys.argv[2])
        os.chdir(sys.argv[2])
        print(json.dumps(_run_inside(tests)))
        return
    if len(sys.argv) != 2:
        print("usage: check.py ORIGIN", file=sys.stderr)
        sys.exit(2)
    with tempfile.TemporaryDirectory(prefix="eval-check-") as tmp:
        tree = os.path.join(tmp, "main")
        error = _export_main(sys.argv[1], tree)
        if error:
            print(json.dumps({"pass": False, "parts": {}, "counters": {}, "notes": [error]}))
            return
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
        try:
            proc = subprocess.run(
                # -B and a private pycache prefix: no cache is written, and none in the
                # suite or the checked tree is read.
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-X",
                    "pycache_prefix=" + os.path.join(tmp, "pyc"),
                    namespace["__file__"],
                    "--inside",
                    tree,
                ],
                capture_output=True,
                text=True,
                timeout=TEST_TIMEOUT_S,
                cwd=tree,
                env=env,
            )
            result = json.loads(proc.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            result = {"pass": False, "parts": {}, "counters": {}, "notes": ["tests timed out"]}
        except (ValueError, IndexError):
            tail = (proc.stderr or "").strip()[-300:]
            result = {"pass": False, "parts": {}, "counters": {}, "notes": [f"no result: {tail}"]}
    print(json.dumps(result))
