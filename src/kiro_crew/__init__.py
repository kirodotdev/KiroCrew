"""KiroCrew — open-source personal AI agent."""

from __future__ import annotations

import asyncio

__version__ = "0.2.0"


class _LazyShutdownEvent:
    """Proxy for ``asyncio.Event`` that defers construction until first use.

    On Python 3.9, ``asyncio.Event()`` captures the *current* event loop at
    construction time.  Creating it at module-import time binds it to
    whatever loop ``get_event_loop()`` implicitly returns (typically a
    throwaway loop created during import), which is NOT the loop that
    ``asyncio.run()`` later spins up for the gateway.  Awaiting such an
    Event from the gateway loop raises::

        RuntimeError: Task ... got Future ... attached to a different loop

    Python 3.10+ made ``asyncio.Event`` loop-less, but we still target 3.9.

    This proxy constructs the underlying Event lazily, **inside the running
    loop**, on first method access.  The real Event therefore binds to the
    correct loop.  The proxy also rebinds transparently if the running
    loop changes (tests, repeated ``asyncio.run()`` cycles).

    Why ``get_running_loop()`` and not ``get_event_loop()``:
        On Python 3.9 in the main thread, ``get_event_loop()`` never raises
        ``RuntimeError`` when no loop is running — it silently creates (or
        returns) a default loop.  Using it here would reintroduce the
        original cross-loop bug whenever the first access happens outside
        a running loop.  ``get_running_loop()`` raises cleanly when no
        loop is running, which is what we want so we can fall back to
        pending state without accidentally binding to a stray loop.

    For sync callers without a running loop (rare — in practice every
    caller in this codebase is inside an async context or signal handler
    registered on the running loop), we maintain a pending-set flag so
    the intent is preserved until a loop is available.
    """

    __slots__ = ("_event", "_loop", "_pending_set")

    def __init__(self) -> None:
        self._event: "asyncio.Event | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        # Tracks set/clear calls made with no running loop, so the state
        # is applied to the real Event once one becomes available.
        self._pending_set: bool = False

    def _get_or_none(self) -> "asyncio.Event | None":
        """Return the Event bound to the current running loop, or None.

        Creates / rebinds the Event if needed.  Returns ``None`` when
        there is no running loop (caller must handle the pending state).
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        if self._event is None or self._loop is not current_loop:
            new_event = asyncio.Event()
            if self._pending_set:
                new_event.set()
            self._event = new_event
            self._loop = current_loop
        return self._event

    def _get(self) -> asyncio.Event:
        """Return the Event, requiring a running loop."""
        event = self._get_or_none()
        if event is None:
            raise RuntimeError(
                "shutdown_event accessed without a running event loop; "
                "call from within an async context."
            )
        return event

    # ── asyncio.Event API ────────────────────────────────────────────
    def is_set(self) -> bool:
        event = self._get_or_none()
        if event is None:
            return self._pending_set
        return event.is_set()

    def set(self) -> None:
        self._pending_set = True
        event = self._get_or_none()
        if event is not None:
            event.set()

    def clear(self) -> None:
        self._pending_set = False
        event = self._get_or_none()
        if event is not None:
            event.clear()

    async def wait(self) -> bool:
        # Always runs from inside a coroutine, so a running loop exists.
        return await self._get().wait()

    def __repr__(self) -> str:
        state = self.is_set()
        return f"<_LazyShutdownEvent set={state}>"


# Process-wide shutdown signal.  Any background loop should use
# ``await shutdown_event.wait()`` (with a timeout) instead of plain
# ``asyncio.sleep()`` so it wakes instantly on Ctrl-C.
shutdown_event: _LazyShutdownEvent = _LazyShutdownEvent()
