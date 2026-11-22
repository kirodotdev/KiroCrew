"""A caller's own deadline around a session start, as the startup metric sees it.

A cancel that reaches a startup emitter (``AcpClient.ensure_ready``,
``_start_kiro_runtime``) is either the person stopping the start or a caller's
deadline expiring, and from inside the emitter both are the same
``CancelledError``. A caller that bounds a start with :func:`startup_deadline`
instead of ``asyncio.wait_for`` lets the emitter read which one it was.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextvars import ContextVar

_DEADLINE: ContextVar[asyncio.Timeout | None] = ContextVar(
    "kirocrew_startup_deadline", default=None
)


@contextlib.asynccontextmanager
async def startup_deadline(seconds: float) -> AsyncIterator[asyncio.Timeout]:
    """``asyncio.timeout(seconds)`` whose expiry the startup metric files as ``timeout``.

    Raises ``TimeoutError`` on expiry, exactly as ``asyncio.wait_for`` does.
    """
    async with asyncio.timeout(seconds) as deadline:
        previous = _DEADLINE.get()
        _DEADLINE.set(deadline)
        try:
            yield deadline
        finally:
            _DEADLINE.set(previous)


def cancel_outcome(exc: asyncio.CancelledError) -> str | None:
    """The startup outcome a cancel stands for, or ``None`` to keep the current one.

    A cancel raised while a failure is being handled carries that failure as its
    context, and the failure's label stands. Otherwise an expired
    :func:`startup_deadline` is ``timeout`` and any other cancel is ``cancelled``.
    """
    if isinstance(exc.__context__, Exception):
        return None
    deadline = _DEADLINE.get()
    if deadline is not None and deadline.expired():
        return "timeout"
    return "cancelled"
