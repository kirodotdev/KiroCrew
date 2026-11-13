"""At most one refresh frame per window for one kind: ``turn_end``, or the listing's ``history``.

A push on the serving loop goes out at once when none went out in the last
window, otherwise one frame is sent when that window ends; any other caller
sends at once. ``KIROCREW_REFRESH_COALESCE_S`` sets the window, and 0 turns it off.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState

TURN_END_KIND = "turn_end"
HISTORY_KIND = "history"

#: The ``DashboardState`` field holding each kind's coalescer.
_STATE_ATTR = {TURN_END_KIND: "_refresh_coalescer", HISTORY_KIND: "_history_coalescer"}

_DEFAULT_WINDOW_S = 1.5


def _window_from_env() -> float:
    raw = os.environ.get("KIROCREW_REFRESH_COALESCE_S", "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_WINDOW_S
    except ValueError:
        return _DEFAULT_WINDOW_S
    if not math.isfinite(value):
        return _DEFAULT_WINDOW_S
    return max(0.0, value)


#: The window in seconds, and the longest a coalesced push waits.
REFRESH_COALESCE_WINDOW_S = _window_from_env()

#: Patchable clock, so tests can drive the window without sleeping.
_clock: Callable[[], float] = time.monotonic


class RefreshCoalescer:
    def __init__(self, state: "DashboardState", kind: str = TURN_END_KIND) -> None:
        self._state = state
        self._kind = kind
        self._timer: asyncio.TimerHandle | None = None
        self._last_sent = float("-inf")
        self.pushes_offered = 0
        self.frames_sent = 0

    def push(self) -> None:
        self.pushes_offered += 1
        loop = self._state.serving_loop
        window = REFRESH_COALESCE_WINDOW_S
        if window <= 0 or loop is None or self._state._running_loop() is not loop:
            self._send()
            return
        if self._timer is not None:
            return
        wait = self._last_sent + window - _clock()
        if wait <= 0:
            self._send()
        else:
            self._timer = loop.call_later(wait, self._flush)

    def _flush(self) -> None:
        self._timer = None
        self._send()

    def _send(self) -> None:
        self._last_sent = _clock()
        self.frames_sent += 1
        self._state._broadcast({"_type": "refresh", "kinds": self._kind})

    def shutdown(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._flush()


def coalescer_for(state: "DashboardState", kind: str = TURN_END_KIND) -> RefreshCoalescer:
    attr = _STATE_ATTR[kind]
    found = getattr(state, attr)
    if found is None:
        found = RefreshCoalescer(state, kind)
        setattr(state, attr, found)
    return found
