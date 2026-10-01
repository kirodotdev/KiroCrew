"""Run a synchronous crew-log call off the event-loop thread, from a test.

A crew-log read or append takes the unit's append lock, and an acquire on the
event-loop thread makes one attempt: it is refused at once when any other thread
holds the lock. The eager folder reads a unit on its own thread right after every
entry, so a test that calls the store directly from an ``async def`` body can land
on that read and fail on a refusal no production caller sees -- the dashboard's
handlers reach the store through ``asyncio.to_thread``. This runs the call on a
worker thread and blocks for it, which is what those handlers do, while keeping the
test's own call sites synchronous.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def off_loop(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Call ``fn(*args, **kwargs)`` on a fresh thread and return or raise its outcome."""
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # re-raised on the caller's thread below
            box["error"] = exc

    worker = threading.Thread(target=_run, name="test-off-loop", daemon=True)
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    return box["value"]
