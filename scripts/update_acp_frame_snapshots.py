#!/usr/bin/env python3
"""Regenerate the ACP frame-replay snapshots under ``test/fixtures/acp_frames/``.

The replay test is strictly read-only, because a test must not create files in
the repo that outlive the run (AUTOSDE ``no-test-side-effects``). This script is
the writer: it replays every fixture through the same harness the test uses and
rewrites each ``<name>.expected.json``.

    python3 scripts/update_acp_frame_snapshots.py

Run it when you have deliberately changed what the dispatch layer makes of a
frame, and commit the rewritten snapshots in the SAME commit as that change: the
snapshot diff is what shows a reviewer the behaviour change. Never regenerate one
to make a red test green without saying in the review why the events moved.

There is no ``--check`` mode: ``test/test_acp_frame_replay.py`` already fails on a
stale snapshot, so a second checker here would be a flag with no caller.

Exit codes: 0 written (or already matching) · 2 an environment or fixture error.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Replay is offline, but the parsers it drives are the production ones, and
# ``_dispatch`` records a ``kirocrew.tool.call.duration`` sample for every
# ``tool_call`` -> terminal ``tool_call_update`` pair it sees. Under pytest the
# root conftest pins ``KIROCREW_TELEMETRY=0`` for the same reason; this script
# runs outside pytest, so on a host with telemetry enabled the fixtures' tool
# calls would land in the real histogram as sub-millisecond samples. The pin is
# set before any ``kiro_crew`` import because ``metrics.provider`` resolves
# consent on its first build and the env var outranks the config flag.
os.environ["KIROCREW_TELEMETRY"] = "0"

REPO_ROOT = Path(__file__).resolve().parents[1]
# The harness lives under test/ (not src/) so that importing
# kiro_crew.acp._dispatch does not add an edge to the agent-sdk boundary
# baseline. Both this script and the test import it from there.
sys.path.insert(0, str(REPO_ROOT / "test"))
sys.path.insert(0, str(REPO_ROOT / "src"))

import acp_frame_replay_harness as harness  # noqa: E402


def main(argv: list[str]) -> int:
    if argv:
        # This script took --check and --test until the flags were removed for
        # having no caller. Accepting and ignoring them would be worse than not
        # having them: someone who reads an older doc and types --check expecting
        # a read-only report would get their snapshots rewritten instead.
        print(
            f"unexpected argument(s): {' '.join(argv)}\n"
            "This script takes no options; it always rewrites stale snapshots. "
            "To CHECK without writing, run `python3 -m pytest "
            "test/test_acp_frame_replay.py`, which fails on a stale snapshot.",
            file=sys.stderr,
        )
        return 2

    fixtures = harness.fixture_files()
    if not fixtures:
        print(f"no fixtures under {harness.CORPUS}", file=sys.stderr)
        return 2

    rewritten: list[str] = []
    for fixture in fixtures:
        try:
            _meta, frames = harness.read_fixture(fixture)
        except (harness.FixtureError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        rendered = harness.snapshot_json(harness.replay_frames(frames))
        target = harness.expected_path(fixture)
        if target.exists() and target.read_text(encoding="utf-8") == rendered:
            continue
        target.write_text(rendered, encoding="utf-8")
        rewritten.append(f"{fixture.parent.name}/{target.name}")

    if not rewritten:
        print(f"all {len(fixtures)} snapshot(s) already match")
        return 0
    print(f"rewrote {len(rewritten)} snapshot(s):")
    for name in rewritten:
        print(f"  {name}")
    print("commit these with the change that moved the events")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
