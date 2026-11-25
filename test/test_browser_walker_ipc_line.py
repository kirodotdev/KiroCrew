"""The walker call fits the Browser Harness daemon's IPC line.

Every page call sends ``WALKER_SOURCE`` plus one method call as a CDP
``Runtime.evaluate``, and Harness writes that request as ONE JSON line to its
daemon (``browser_harness._ipc.request``), which reads it with asyncio's default
64 KiB line limit (``_ipc.serve`` -> ``start_unix_server``). A longer line fails
every walker call -- ``navigate`` times out and ``snapshot`` answers ``Separator
is found, but chunk is longer than limit`` -- so the walker's size is pinned here,
with room left for a call's own arguments.
"""

from __future__ import annotations

import json

from kiro_crew.browser_harness import ops

#: asyncio's default ``StreamReader`` limit, which the Harness daemon reads with.
HARNESS_LINE_LIMIT = 2**16
#: Kept free under the limit for a call's arguments: a ref or a short string. A
#: long ``paste`` can still pass the line inside its own cap.
ARGUMENT_ROOM = 8 * 1024


def _harness_line(expression: str) -> bytes:
    """The bytes Harness writes for ``cdp("Runtime.evaluate", expression=...)``."""
    request = {
        "method": "Runtime.evaluate",
        "params": {
            "expression": expression,
            "contextId": 2**31 - 1,
            "returnByValue": True,
            "awaitPromise": True,
        },
        "session_id": "F" * 32,
    }
    return (json.dumps(request) + "\n").encode()


def test_the_walker_call_leaves_room_under_the_harness_line_limit() -> None:
    line = _harness_line(f"{ops.WALKER_SOURCE}.snapshot()")
    assert len(line) <= HARNESS_LINE_LIMIT - ARGUMENT_ROOM, (
        f"the walker call is {len(line)} bytes on the daemon's IPC line; keep it at most "
        f"{HARNESS_LINE_LIMIT - ARGUMENT_ROOM} so every walker op still reaches the page"
    )


def test_dropping_comment_lines_and_indents_cannot_change_the_walker() -> None:
    # Only text that spans lines (a template, a `\` continuation, a block comment)
    # could hold a `//` line or an indent that matters; ASCII keeps `splitlines()`
    # and `strip()` to plain newlines and spaces.
    walker = ops.WALKER_PATH.read_text(encoding="utf-8")
    assert walker.isascii(), "snapshot_walker.js: keep it ASCII (use \\u escapes)"
    for number, line in enumerate(walker.splitlines(), 1):
        assert line.count("`") % 2 == 0, f"snapshot_walker.js:{number}: multi-line template"
        assert not line.endswith("\\"), f"snapshot_walker.js:{number}: string continued by \\"
        assert "/*" not in line or "*/" in line, f"snapshot_walker.js:{number}: block comment"
    assert not any(
        line.startswith(("//", " ", "\t")) or not line for line in ops.WALKER_SOURCE.split("\n")
    )
