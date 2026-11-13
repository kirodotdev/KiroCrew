"""The one-slot gate every memory consolidation pass in a process holds.

``mc-membg`` bounds threads, not passes: without this gate each idle or ending
session starts its own multi-minute pass, and several of them at once in the
process that also serves HTTP and the websocket starve the event loop through
the GIL. One gate per event loop, because an asyncio primitive belongs to the
loop that first waits on it.
"""

from __future__ import annotations

import asyncio

_GATE: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def consolidation_pass_gate() -> asyncio.Semaphore:
    """Return the running loop's consolidation gate, creating it on first use."""
    global _GATE
    loop = asyncio.get_running_loop()
    if _GATE is None or _GATE[0] is not loop:
        _GATE = (loop, asyncio.Semaphore(1))
    return _GATE[1]
