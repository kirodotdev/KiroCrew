"""``browser.run.max_repeats``: stop one browser op repeated in a row.

A long browser task can loop unnoticed: the agent scrolls or clicks the same control
dozens of times and every call succeeds. With the setting above 0 the gateway asks
:func:`check` before an op reaches the browser and answers the repeat with a report
the model reads instead of an error it might retry around.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from typing import Any

#: The refusal's ``code``; the ``browser`` tool prints it as ``Error (run_budget): ...``.
CODE = "run_budget"

_WAY_ON = "a person can raise {setting}, take over the browser, or start a new chat"


@dataclass
class RunCount:
    """The last op's fingerprint and how many times in a row it has run."""

    last: str = ""
    repeats: int = 0


def _fingerprint(op: str, args: dict[str, Any]) -> str:
    """A checksum and length of the op and its arguments, so a typed value is never kept."""
    data = json.dumps([op, args], sort_keys=True, default=str).encode()
    return f"{zlib.crc32(data):08x}:{len(data)}"


def check(
    count: RunCount, op: str, args: dict[str, Any], *, max_repeats: int
) -> dict[str, Any] | None:
    """Count *op* for the chat, or return the refusal that stops it.

    ``None`` means run it. A refused op is not counted, so the same op stays refused
    until a different one goes first. A limit of 0 is off and counts nothing.
    """
    if max_repeats <= 0:
        return None
    current = _fingerprint(op, args)
    run = count.repeats + 1 if current == count.last else 1
    if run > max_repeats:
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
    count.last, count.repeats = current, run
    return None
