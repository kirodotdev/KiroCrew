"""A child that writes a real crew log, parks at a named failpoint, and waits to die.

Every other crash test in this suite hands the repair a file IT wrote by hand, so
what those prove is "given this file state, the repair does X" -- never "a real
kill produces that file state". This child closes that gap: it drives the
PRODUCTION writer, stops at a point named on the command line, and then blocks
forever, so the parent's ``SIGKILL`` is the only thing that ends it.

The failpoint is announced by writing a TERMINAL marker value to the marker file.
The parent waits for that exact value rather than for the file to exist: a prefix
of a longer word would otherwise let the parent kill while the child was still
mid-setup, which is a race that reads as a flaky test rather than as the bug it
would hide.

Usage: crew_log_crash_child.py <failpoint> <marker-path>

Failpoints:
  open-turn        a turn is started and left open -- what a crash mid-turn leaves
  open-tool        a turn with an unfinished tool call inside it
  orphan-chunks    chunk entries whose citing entry is not written
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import Ledger, emit

SESSION = "crash-child-session"


def _park(marker: Path, value: str) -> None:
    """Announce the failpoint, then wait to be killed.

    The marker is written and flushed BEFORE parking, and the value is written in
    one call so a reader cannot see half of it.
    """
    marker.write_text(value, encoding="utf-8")
    while True:  # only SIGKILL ends this
        time.sleep(3600)


def main() -> int:
    failpoint, marker_path = sys.argv[1], Path(sys.argv[2])
    Ledger.create(lg.KIND_SESSION, SESSION, owner="crash-crew", agent="kirocrew")

    if failpoint == "open-turn":
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_message_received(SESSION, 1, role="user", text="do the thing")
        assert emit.flush(timeout=20.0), "the child could not durably write its setup"
        _park(marker_path, "turn-open")

    elif failpoint == "open-tool":
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_write", call_id="tc-crash")
        assert emit.flush(timeout=20.0), "the child could not durably write its setup"
        _park(marker_path, "tool-open")

    elif failpoint == "orphan-chunks":
        # The citing entry is deliberately never written, which is what a torn
        # group write leaves behind. Written through the store directly: the
        # emitter always writes the group as one batch, so the shape the repair
        # has to handle cannot be produced through it -- which is the point.
        handle = Ledger.open(lg.KIND_SESSION, SESSION)
        handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
        handle.append_many(
            [
                {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True},
                {"type": "message/chunk", "data": {"turn": 1, "delta": "bb"}, "ignorable": True},
            ],
            src="acp",
        )
        _park(marker_path, "chunks-orphaned")

    else:  # pragma: no cover - a typo in the test, not a code path
        raise SystemExit(f"unknown failpoint {failpoint!r}")

    return 0  # pragma: no cover - unreachable, the child is killed


if __name__ == "__main__":
    raise SystemExit(main())
