"""Per-step boot timing: one INFO line with wall and thread CPU milliseconds.

A step whose wall time is far above its CPU time, while it runs pure Python,
waited on the GIL or on I/O. Logging only: the step's exceptions pass through.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)


@contextmanager
def timed_step(name: str) -> Iterator[None]:
    """Log ``memory-boot step=<name> wall_ms=<int> cpu_ms=<int>`` when the block exits.

    The CPU figure is the calling thread's, so run it in the thread doing the work.
    The line is logged on an exception too, which then propagates unchanged.
    """
    wall = time.perf_counter()
    cpu = time.thread_time()
    try:
        yield
    finally:
        logger.info(
            "memory-boot step=%s wall_ms=%d cpu_ms=%d",
            name,
            (time.perf_counter() - wall) * 1000,
            (time.thread_time() - cpu) * 1000,
        )
