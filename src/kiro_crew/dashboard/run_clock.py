"""The clock a chat run is measured on, for the ``run_seconds`` of its final ``chat_done``.

Every reading of a run goes through :func:`now`: the ``_ChatSlot.task`` setter stamps
a run's first turn, the dispatch gates restamp a person's message, and
``queue_cycle._report_run_seconds`` reads the end. This module reads ``time`` and
nothing else, so a test installs a manual clock here and every reading of a run
moves only when the test advances it, while the event loop keeps real time.
"""

from __future__ import annotations

import time


def now() -> float:
    """The current reading of the run clock, in seconds."""
    return time.monotonic()


def seconds_since(started: float) -> int:
    """Whole seconds from *started* (a :func:`now` reading) to now, never negative.

    Rounded to the nearest second, not truncated: two readings of a fractional
    clock differ by a float that can fall just short of the whole seconds between
    them.
    """
    return max(0, round(now() - started))
