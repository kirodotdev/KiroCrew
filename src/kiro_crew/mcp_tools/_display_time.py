"""UTC display stamps for the session-control MCP replies (moved out of mcp_dashboard)."""

from __future__ import annotations

import time


def _summary_time(value: object) -> str:
    """The sidecar's ``generated_at`` (a ``time.time()`` float) as UTC ISO-8601."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return "at an unknown time"


def _closed_at_display(value: object) -> str:
    """A gone row's ``closed_at`` (crew-log epoch MILLISECONDS) as UTC ISO-8601.

    The close edge stamps ``entry.time`` from ``now_ms()``, so the value is in
    milliseconds; render it human-readable rather than as a raw epoch number. Falls
    back to the value's own string form when it is not a finite millisecond count,
    so a malformed stamp still shows something rather than raising in the renderer.
    """
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value) / 1000.0))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value)
