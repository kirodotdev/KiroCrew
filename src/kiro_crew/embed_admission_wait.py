"""The record an unbounded embed-pool admission wait leaves when it runs long.

:func:`kiro_crew.executors._run_admitted` waits on its per-loop semaphore with no
deadline when the caller set none. A wait past :data:`SLOW_ADMISSION_WAIT_SECS`
logs one DEBUG line when it ends, so a stalled request can be matched to the
wait. A wait that finishes sooner logs nothing, and the wait itself is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time

# The line belongs to the admission path, so it goes out under that module's logger.
logger = logging.getLogger("kiro_crew.executors")

#: An unbounded admission wait longer than this is logged when it ends.
SLOW_ADMISSION_WAIT_SECS = 5.0


async def acquire_unbounded(admission: asyncio.Semaphore, workers: int) -> None:
    """``await admission.acquire()``, then one DEBUG line if the wait ran long."""
    started = time.monotonic()
    await admission.acquire()
    waited = time.monotonic() - started
    if waited > SLOW_ADMISSION_WAIT_SECS:
        logger.debug("embed admission waited %.1fs; budget=none workers=%d", waited, workers)
