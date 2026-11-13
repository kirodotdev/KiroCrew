#!/usr/bin/env python3
"""Regenerate the chat turn golden files under ``test/fixtures/turn_golden/``.

``test/test_turn_golden.py`` compares what a replayed turn sends to each socket
and writes to its transcript with these files, and never writes into the
repository. This script is the writer. It runs that test under pytest, so the
turn gets the suite's isolation (its own data home, no real ``~/.kiro``), with
``KIROCREW_TURN_GOLDEN_OUT`` pointing at a temporary directory outside the
repository. The test writes each rendered result there, and the script copies
every file that differs into ``test/fixtures/turn_golden/``.

    python3 scripts/update_turn_golden_snapshots.py

Run it when a change alters the frames or transcript of a turn on purpose, and
commit the rewritten files in the same commit as that change, so the review
shows the frame diff. Never run it to turn a red test green without saying in
the review why the frames moved. To check without writing, run the test.

Exit codes: 0 written (or already matching) · 1 the test run failed ·
2 an unexpected argument.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = REPO_ROOT / "test" / "fixtures" / "turn_golden"
TEST_FILE = "test/test_turn_golden.py"


def main(argv: list[str]) -> int:
    if argv:
        print(
            f"unexpected argument(s): {' '.join(argv)}\n"
            "This script takes no options; it always rewrites stale golden files. "
            f"To CHECK without writing, run `python3 -m pytest {TEST_FILE}`.",
            file=sys.stderr,
        )
        return 2
    with tempfile.TemporaryDirectory(prefix="turn-golden-") as out:
        env = {**os.environ, "KIROCREW_TURN_GOLDEN_OUT": out}
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            f"{TEST_FILE}::test_turn_matches_golden",
            "-n0",
            "-q",
            "-p",
            "no:cacheprovider",
        ]
        run = subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=False)
        if run.returncode != 0:
            print("the golden test run failed; nothing was written", file=sys.stderr)
            return 1
        rendered = sorted(Path(out).glob("*.golden.json"))
        if not rendered:
            print(f"the test wrote no golden files into {out}", file=sys.stderr)
            return 1
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        rewritten: list[str] = []
        for source in rendered:
            target = GOLDEN_DIR / source.name
            text = source.read_text(encoding="utf-8")
            if target.exists() and target.read_text(encoding="utf-8") == text:
                continue
            target.write_text(text, encoding="utf-8")
            rewritten.append(target.name)
    if not rewritten:
        print(f"all {len(rendered)} golden file(s) already match")
        return 0
    print(f"rewrote {len(rewritten)} golden file(s):")
    for name in rewritten:
        print(f"  {name}")
    print("commit these with the change that moved the frames")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
