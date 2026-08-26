"""Idle manager for Mochi — pause the autonomous half when the user is away.

Ported from ``src/main/idleManager.ts``. Pauses the queue poller and watchlist
service on screen lock / suspend / long system idle, and resumes (triggering
catch-up) when the user comes back.

PORTING NOTES
-------------

**The port is the state machine only; the plumbing moved to the owner.** The
original registered four ``powerMonitor`` listeners and ran a 60s
``setInterval`` that polled ``getSystemIdleTime()`` — all Electron main-process
machinery. In KiroCrew the pet's shell relays power events and idle readings to
the gateway, so the owner calls :meth:`handle_power_event` for each event and
:meth:`check_idle` on its own ~60s cadence (:data:`IDLE_CHECK_INTERVAL_MS` is
exported for that loop). ``start()``/``stop()`` were pure timer wiring and have
no port. This takes the ported-modules time discipline one step further: elsewhere
a timer became ``tick(now_ms)``; here the interval leaves the module
entirely, because its body has no time-derived state at all.

**Lock and suspend share one ``locked`` flag, and the pairs CROSS.**
``suspend`` sets it, but ``unlock-screen`` clears it too (and vice versa for
``lock-screen`` / ``resume``), so an unlock event resumes a suspend-pause.
Ships that way; preserved.

**The auto-resume threshold is a literal 60, not the named constant.** Resume
fires only when paused AND not locked AND idle < 60 seconds — the lock guard
exists because the lock animation itself generates an HID event that would
otherwise instantly un-pause a locked machine. Pause is ``idle >= threshold``
(closed boundary), resume is ``idle < 60`` (open boundary).

**Pause and resume are idempotent by guard, not by accident.** Repeated lock
events call back exactly once; an unlock while never paused calls back never.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

# Idle threshold in seconds — pause after 30 minutes of inactivity.
IDLE_THRESHOLD_SECS = 30 * 60

# How often the OWNER should call check_idle (the original setInterval period).
IDLE_CHECK_INTERVAL_MS = 60_000

# Auto-resume only below this idle level (a literal in the original too).
_RESUME_IDLE_SECS = 60

# The four power events the original subscribed to, with their flag effects.
_LOCKING_EVENTS = ("lock-screen", "suspend")
_UNLOCKING_EVENTS = ("unlock-screen", "resume")


class IdleCallbacks(Protocol):
    """Sinks for pause/resume transitions. Mirrors the original callback bag."""

    def on_pause(self) -> None: ...

    def on_resume(self) -> None: ...


class IdleManager:
    """Away-detection state machine: paused/locked flags plus the transitions."""

    def __init__(self, callbacks: IdleCallbacks) -> None:
        self._callbacks = callbacks
        self._paused = False
        # Screen locked or system suspended — the idle check must not
        # auto-resume; only the explicit unlock/resume event may.
        self._locked = False

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def is_paused(self) -> bool:
        return self._paused

    def handle_power_event(self, event: str) -> None:
        """Feed a power event: lock-screen / unlock-screen / suspend / resume.

        Unknown events are ignored (the original never subscribed to them, so
        they could not arrive; the owner-fed shape can see anything).
        """
        if event in _LOCKING_EVENTS:
            self._locked = True
            self._pause(event)
        elif event in _UNLOCKING_EVENTS:
            self._locked = False
            self._resume("wake" if event == "resume" else event)

    def check_idle(self, idle_secs: int) -> None:
        """The body of the original interval — call every ~60s with the
        system idle time in seconds."""
        if not self._paused and idle_secs >= IDLE_THRESHOLD_SECS:
            self._pause(f"idle-{idle_secs}s")
        elif self._paused and not self._locked and idle_secs < _RESUME_IDLE_SECS:
            # Only auto-resume from an idle-timeout pause, not from
            # lock/suspend — those require the explicit event.
            self._resume("activity")

    # ── Internal ───────────────────────────────────────────────────────────

    def _pause(self, reason: str) -> None:
        if self._paused:
            return
        self._paused = True
        logger.info("[IdleManager] Paused — %s", reason)
        self._callbacks.on_pause()

    def _resume(self, reason: str) -> None:
        if not self._paused:
            return
        self._paused = False
        logger.info("[IdleManager] Resumed — %s", reason)
        self._callbacks.on_resume()
