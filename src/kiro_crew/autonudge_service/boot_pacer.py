"""Release the loops due at gateway start one at a time, as fast as the host allows.

After a restart every loop that came due while the gateway was down, and every
conductor's work-ledger replay, would otherwise wake in the same second. Each wake
can start a session and its MCP servers, and a conductor's wake can start its
workers too, which is how a host with many conductors stalls. ``start()`` hands
those loops to one :class:`BootPacer` in boot order, and each loop's timer waits
its turn here (:meth:`BootPacer.wait_turn`) before its own countdown.

The gap between two releases follows the memory decision posture
``resource_status.probe`` returns (``ample`` / ``tight`` / ``critical``). Memory, not disk: what a woken
session costs at start is memory, and a full temp disk is not freed by waiting.

* ``ample``: :data:`BOOT_WAKE_GAP_SECS`, enough for the previous wake to start its
  process, so two dozen loops are all released within about a minute;
* ``tight``, or a posture nobody could read: up to
  :data:`BOOT_WAKE_TIGHT_GAP_SECS`, released sooner if the host recovers;
* ``critical``: held until the host reads ``ample`` again.

Whatever the posture, the loop at the head is released once it has waited
:data:`BOOT_WAKE_MAX_HOLD_SECS`, so a host that stays critical slows the boot
wakes down but never stops them. Nothing is dropped: a held loop only waits. A timer that is cancelled while it
waits (a user turn, a stop, a replacing arm) leaves the queue, so the loops behind
it move up, and the re-arm that follows does not come back through here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Iterable

# Logged under the service's name, as every autonudge owner is.
logger = logging.getLogger("kiro_crew.autonudge")

#: Gap after a release while the host is ``ample``.
BOOT_WAKE_GAP_SECS = 2.0
#: Longest gap after a release while the host is ``tight`` or its posture is unknown.
BOOT_WAKE_TIGHT_GAP_SECS = 30.0
#: Longest the loop at the head waits, whatever the posture.
BOOT_WAKE_MAX_HOLD_SECS = 300.0
#: How often the loop at the head of the queue re-reads the posture while it waits.
POSTURE_POLL_SECS = 1.0

_AMPLE, _CRITICAL = "ample", "critical"


async def probe_host_posture() -> str:
    """The host's memory posture, from a fresh ``resource_status.probe``.

    The probe reads ``/proc`` and the config, so it runs off the loop. The pacing is a
    decision, so it reads ``decision_posture``: the advisory ``unknown`` reported for a
    container limit out of view must not slow the wakes. A probe that fails reads
    ``unknown``, which the pacer treats as ``tight``.
    """
    from kiro_crew import resource_status

    try:
        return str((await asyncio.to_thread(resource_status.probe)).decision_posture)
    except Exception:  # noqa: BLE001 - a failed probe is an unknown posture
        logger.debug("AutoNudge boot pacer: posture probe failed", exc_info=True)
        return resource_status.POSTURE_UNKNOWN


#: The reader a pacer built without one uses, looked up at each read.
read_host_posture = probe_host_posture


class BootPacer:
    """A FIFO of loop ids; the head is released when the posture's gap has passed."""

    def __init__(
        self,
        read_posture: Callable[[], Awaitable[str]] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._read_posture = read_posture
        self._clock = clock
        self._sleep = sleep
        self._order: list[str] = []
        self._released_at: float | None = None
        self._head_since = 0.0
        self._held_critical = False
        self._last_posture = ""
        self._moved: asyncio.Event | None = None

    def queue(self, loop_ids: Iterable[str]) -> None:
        """Append *loop_ids* to the queue, in order."""
        self._order.extend(i for i in loop_ids if i not in self._order)

    def queued(self, loop_id: str) -> bool:
        return loop_id in self._order

    def forget(self, loop_id: str) -> None:
        """Drop *loop_id* without releasing it; the loops behind it move up."""
        if loop_id in self._order:
            self._order.remove(loop_id)
            self._wake_waiters()

    def _wake_waiters(self) -> None:
        if self._moved is not None:
            self._moved.set()
            self._moved = None

    async def _head_changed(self) -> None:
        if self._moved is None:
            self._moved = asyncio.Event()
        await self._moved.wait()

    async def _ready(self) -> float:
        """Seconds the head still waits, or 0 when it may go now."""
        # Looked up per read, so a caller that passed no reader gets the module's
        # current one.
        posture = await (self._read_posture or read_host_posture)()
        self._last_posture = posture
        # A critical host holds the queue until it reads ample, not merely tight.
        if posture == _CRITICAL:
            self._held_critical = True
        elif posture == _AMPLE:
            self._held_critical = False
        if self._clock() - self._head_since >= BOOT_WAKE_MAX_HOLD_SECS:
            return 0.0
        if self._held_critical:
            return POSTURE_POLL_SECS
        if self._released_at is None:
            return 0.0
        elapsed = self._clock() - self._released_at
        if posture == _AMPLE:
            return BOOT_WAKE_GAP_SECS - elapsed
        return BOOT_WAKE_TIGHT_GAP_SECS - elapsed

    async def wait_turn(self, loop_id: str) -> None:
        """Return once *loop_id* is released. A loop not queued returns at once."""
        head = False
        try:
            while loop_id in self._order:
                if self._order[0] != loop_id:
                    await self._head_changed()
                    continue
                if not head:
                    head = True
                    self._head_since = self._clock()
                wait = await self._ready()
                if wait <= 0:
                    self._released_at = self._clock()
                    logger.info(
                        "AutoNudge boot pacer: released loop %s (host %s, %d still queued)",
                        loop_id,
                        self._last_posture,
                        len(self._order) - 1,
                    )
                    break
                await self._sleep(min(wait, POSTURE_POLL_SECS))
        finally:
            self.forget(loop_id)
