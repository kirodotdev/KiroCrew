#!/usr/bin/env python3
"""Regenerate ``test/fixtures/acp_launch_goldens.json``.

The golden test is strictly read-only, because a test must not create files in the
repo that outlive the run (AUTOSDE ``no-test-side-effects``). This script is the
writer: it drives ``AcpClient._spawn`` for every id in ``ACP_BACKENDS_KNOWN`` through
the same capture the test uses, and rewrites the fixture.

    python3 scripts/update_acp_launch_goldens.py

Run it when you have deliberately changed what a harness is LAUNCHED as, and commit
the rewritten fixture in the SAME commit as that change: the fixture diff is what
shows a reviewer which harness moved and how. Never regenerate one to make a red test
green without saying in the review why the launch moved -- and never regenerate it to
clear a kiro-cli row, which is the row harness-parity H13 exists to protect.

There is no ``--check`` mode: ``test/test_acp_launch_goldens.py`` already fails on a
stale fixture, so a second checker here would be a flag with no caller.

Exit codes: 0 written (or already matching) · 2 an environment error.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# The capture drives the production spawn path, and that path records telemetry.
# Under pytest the root conftest pins this for the same reason; this script runs
# outside pytest, so on a host with telemetry enabled a capture would land in the
# real histogram. Set before any ``kiro_crew`` import because ``metrics.provider``
# resolves consent on its first build and the env var outranks the config flag.
os.environ["KIROCREW_TELEMETRY"] = "0"

REPO_ROOT = Path(__file__).resolve().parents[1]
# The capture lives under test/ (not src/) so that importing kiro_crew.acp.client
# does not add an edge to the agent-sdk boundary baseline. Both this script and the
# test import it from there.
sys.path.insert(0, str(REPO_ROOT / "test"))
sys.path.insert(0, str(REPO_ROOT / "src"))

import acp_launch_capture as capture_mod  # noqa: E402


def main(argv: list[str]) -> int:
    if argv:
        print(
            f"unexpected argument(s): {' '.join(argv)}\n"
            "This script takes no options; it always rewrites the fixture. To CHECK "
            "without writing, run `python3 -m pytest test/test_acp_launch_goldens.py`, "
            "which fails on a stale fixture.",
            file=sys.stderr,
        )
        return 2

    # A real temp dir outside the repository: the capture builds each client against
    # a work dir, and the whole point of this script existing is that the run leaves
    # no file behind in the checkout except the fixture it is here to write.
    with tempfile.TemporaryDirectory(prefix="acp-launch-goldens-") as tmp:
        try:
            snapshot = capture_mod.capture_all(Path(tmp))
        except Exception as exc:  # pragma: no cover - operator-facing diagnostic
            print(f"error: could not capture a launch: {exc}", file=sys.stderr)
            return 2

    rendered = capture_mod.render(snapshot)
    previous = (
        capture_mod.GOLDEN_PATH.read_text(encoding="utf-8")
        if capture_mod.GOLDEN_PATH.exists()
        else ""
    )
    if rendered == previous:
        print(f"{capture_mod.GOLDEN_PATH.relative_to(REPO_ROOT)} already matches")
        return 0

    capture_mod.GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    capture_mod.GOLDEN_PATH.write_text(rendered, encoding="utf-8")
    print(
        f"rewrote {capture_mod.GOLDEN_PATH.relative_to(REPO_ROOT)} "
        f"({len(snapshot)} harness(es))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
