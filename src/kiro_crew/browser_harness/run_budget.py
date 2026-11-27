"""``browser.run.max_ops``, ``max_repeats`` and ``max_minutes``: stop a browser task that runs on.

A long browser task can loop unnoticed: the agent scrolls or clicks the same control
dozens of times and every call succeeds. With a setting above 0 the gateway asks
:func:`check` before an op reaches the browser and answers the stop with a report
the model reads instead of an error it might retry around.
"""

from __future__ import annotations

import json
import time
import zlib
from dataclasses import dataclass
from typing import Any

#: The refusal's ``code``; the ``browser`` tool prints it as ``Error (run_budget): ...``.
CODE = "run_budget"

_WAY_ON = "a person can raise {setting}, take over the browser, or start a new chat"


@dataclass
class RunCount:
    """Ops run, the last op's fingerprint, its run length and when the run's first op ran."""

    ops: int = 0
    last: str = ""
    repeats: int = 0
    started: float | None = None


def _now() -> float:
    """Seconds on a monotonic clock; a test replaces this to move time by hand."""
    return time.monotonic()


def _fingerprint(op: str, args: dict[str, Any]) -> str:
    """A checksum and length of the op and its arguments, so a typed value is never kept."""
    data = json.dumps([op, args], sort_keys=True, default=str).encode()
    return f"{zlib.crc32(data):08x}:{len(data)}"


def check(
    count: RunCount,
    op: str,
    args: dict[str, Any],
    *,
    max_ops: int = 0,
    max_repeats: int = 0,
    max_minutes: int = 0,
) -> dict[str, Any] | None:
    """Count *op* for the chat, or return the refusal that stops it.

    ``None`` means run it. A refused op is not counted, so the count says what ran and
    a repeat stays refused until a different op goes first. The op cap is checked
    first, then the time cap, then the repeat run. A limit of 0 is off; with all three
    off nothing is counted and the clock is not read. The time cap runs from the run's
    first op, and a refused op leaves the run's start as it was.
    """
    if max_ops <= 0 and max_repeats <= 0 and max_minutes <= 0:
        return None
    now = _now() if max_minutes > 0 else 0.0
    if 0 < max_ops <= count.ops:
        return {
            "ok": False,
            "code": CODE,
            "error": (
                f"browser run budget reached: {count.ops} browser ops in this chat "
                f"(browser.run.max_ops = {max_ops}). This `{op}` was not run. Stop browsing and "
                f"tell the user where you got to; {_WAY_ON.format(setting='browser.run.max_ops')}."
            ),
        }
    if max_minutes > 0:
        if count.started is None:
            count.started = now
        elif max_minutes * 60 <= now - count.started:
            return {
                "ok": False,
                "code": CODE,
                "error": (
                    f"browser run time limit reached: this chat's browser run has lasted "
                    f"{int((now - count.started) // 60)} minutes (browser.run.max_minutes = "
                    f"{max_minutes}). This `{op}` was not run. Stop browsing and tell the user "
                    f"where you got to; {_WAY_ON.format(setting='browser.run.max_minutes')}."
                ),
            }
    current = _fingerprint(op, args)
    run = count.repeats + 1 if current == count.last else 1
    if 0 < max_repeats < run:
        return {
            "ok": False,
            "code": CODE,
            "error": (
                f"repeated action stopped: `{op}` was run {count.repeats} times in a row with "
                f"the same arguments (browser.run.max_repeats = {max_repeats}). This `{op}` was "
                "not run. Try something different or tell the user what is blocking you; "
                f"{_WAY_ON.format(setting='browser.run.max_repeats')}."
            ),
        }
    count.ops += 1
    count.last, count.repeats = current, run
    return None
