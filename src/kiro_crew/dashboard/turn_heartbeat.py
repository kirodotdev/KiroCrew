"""One gateway-wide ``heartbeat`` tick while any chat turn runs.

A dashboard tab and an app embed read the ``heartbeat`` frame as "the gateway is
alive and a turn is still working", nothing more: the dashboard's handler ignores
it and the app SDK forwards it unchanged. So one frame per socket per interval
carries everything the frame means, however many turns run. The tick holds the
slot keys of the running turns only so the hub can decide which app-token sockets
may learn that a turn runs (see ``WebSocketHub.broadcast_heartbeat``).

The tick runs on the serving loop and only while at least one turn holds it: the
first ``hold`` starts it, and it exits after the interval in which the last
``release`` landed. ``stop`` cancels it at shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection
from typing import Any

logger = logging.getLogger(__name__)

#: Seconds between two ticks while any turn runs.
HEARTBEAT_INTERVAL_S = 5.0


class TurnHeartbeat:
    """Send one heartbeat round per interval while any turn holds the tick.

    ``send`` is called with the slot keys of the turns holding the tick at that
    moment. ``sleep`` is the interval wait; a test passes a fake clock's sleep so
    nothing waits on real time.
    """

    def __init__(
        self,
        send: Callable[[Collection[str]], Any],
        *,
        interval: float = HEARTBEAT_INTERVAL_S,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._send = send
        self._interval = interval
        self._sleep = sleep
        # Keyed by the holder's identity, so two holders on one slot (a turn and
        # its successor overlapping in the queue hand-off) each release their own.
        self._holders: dict[int, str] = {}
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Whether the tick task is alive."""
        return self._task is not None and not self._task.done()

    def hold(self, holder: object, slot_key: str) -> None:
        """Keep the tick running while *holder* runs a turn on *slot_key*.

        Called on the serving loop. Off any loop it records the holder and starts
        nothing; the next ``hold`` on a loop starts the tick.
        """
        self._holders[id(holder)] = slot_key
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = self._task
        if task is not None and not task.done() and task.get_loop() is loop:
            return
        self._task = loop.create_task(self._run(), name="turn-heartbeat")

    def release(self, holder: object) -> None:
        """Drop *holder*. Releasing a holder that holds nothing is a no-op."""
        self._holders.pop(id(holder), None)

    async def _run(self) -> None:
        # No await between the emptiness check and the return, so a ``hold`` on
        # this loop either sees this task alive or sees it done and starts another.
        while self._holders:
            await self._sleep(self._interval)
            if not self._holders:
                return
            try:
                self._send(tuple(sorted(set(self._holders.values()))))
            except Exception:
                logger.warning("turn heartbeat: send failed", exc_info=True)

    async def stop(self) -> None:
        """Cancel the tick and forget every holder. Safe when nothing runs."""
        self._holders.clear()
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        if task.get_loop() is not asyncio.get_running_loop():
            return
        # ``wait`` does not re-raise the task's cancellation, and still lets a
        # cancellation of the caller itself through.
        await asyncio.wait({task})
