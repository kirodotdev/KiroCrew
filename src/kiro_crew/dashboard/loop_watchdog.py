"""Off-loop watchdog that turns an event-loop stall into a logged stack dump.

KiroCrew's gateway runs the dashboard HTTP server, every agent turn, and all
background tasks on a *single* asyncio event loop on *one* thread.  If a
coroutine performs a blocking syscall on that thread — e.g. an un-timed-out
socket ``close()`` while tearing down a half-dead ACP/model stream, or a burst
of ``os.waitpid()`` reaping many kiro-cli children at once — the whole loop
wedges:

* the HTTP server stops answering (``/api/status`` is itself a coroutine on the
  wedged loop, so it cannot even report "I'm sick" — it just hangs);
* the async event-loop heartbeat can no longer be scheduled, so the log goes
  **silent**; and
* that silence is the only signal, and it carries no information about *where*
  the loop is stuck.

Two independent mechanisms turn that silence into an actionable artifact, both
fed by the async heartbeat calling :meth:`LoopStallWatchdog.beat` every tick:

1. **Authoritative dump-then-exit (the primary, portable path).**  Each beat
   re-arms :func:`faulthandler.dump_traceback_later` with ``exit=True``.  That
   timer lives on faulthandler's own C thread and reads thread states in C, so
   it fires even when the loop thread is wedged in a syscall *and* the
   GIL-dependent daemon check below would be starved; when it fires it dumps
   *all* thread stacks and then ``_exit()``s the process in one atomic step.
   Because the process dumps and dies on its own, there is no race with the
   external Electron liveness probe (the probe is reduced to a backstop), and
   the mechanism is fully cross-platform and needs no root — unlike an
   out-of-process ``py-spy`` capture, which macOS blocks without elevated
   ``task_for_pid`` privileges.  ``exit_after`` (25s) is kept below the external
   probe's kill window — that probe trips after ``failure_threshold`` (3)
   consecutive failed polls at a 10s cadence, i.e. ≳20s — so this in-process
   path generally wins and the dump is captured.  The two budgets are close
   (25s vs ≳20s) rather than comfortably separated, so they must be kept in sync
   if either side is tuned; see :class:`LoopStallWatchdog` for the exact timing.

   **Journal visibility:** hard-exit dumps land in the dedicated file only (not
   stderr/journal) because ``faulthandler.dump_traceback_later`` targets a single
   fd.  On the next gateway startup, ``server.py`` detects and replays the dump
   content to the logger at WARNING level (capped at 120 lines / 8 KB), so
   journal-only operators (containers, ``journalctl``) see the stacks one restart
   later — exactly when they are investigating why the gateway died.

2. **Soft observability dump (the daemon-thread fallback).**  A separate daemon
   thread compares the last beat against the clock and, once the loop has not
   beaten for ``stall_after`` seconds, logs a marker and dumps all thread stacks
   *without* exiting, re-arming on recovery.  This is the fallback used when the
   armed timer is disabled (``exit_after=None``) or could not be armed.

The daemon thread keeps running even when the loop thread is blocked in the
kernel — CPython releases the GIL around blocking syscalls such as ``close()`` /
``waitpid()``, which is exactly the class of wedge observed in production.

The class is deliberately split so the decision logic (:meth:`check`) is a
pure, synchronous step that can be driven from tests with an injected clock and
dump callback, and the armed-timer arm/cancel calls are injectable too — so
tests verify the wiring without ever arming a real process-killing timer.
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
import time
import typing
from collections.abc import Callable

logger = logging.getLogger("kiro_crew.dashboard.loop_watchdog")


def _default_dump(file: "typing.IO[str] | typing.Any | None" = None) -> None:
    """Dump every thread's stack to a dedicated file AND stderr.

    Writes to both the dedicated crash-dump file (for discoverability) and stderr
    (for journal/terminal visibility) so dumps are never lost.

    *file* can be any object with a ``fileno()`` method (including
    :class:`~kiro_crew.dashboard.crash_dump_store.DumpFile`).
    """
    target = file or sys.stderr
    faulthandler.dump_traceback(file=target, all_threads=True)
    # Also write to stderr if the target is a different file
    if target is not sys.stderr:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)


def _default_arm_later(timeout: float, file: "typing.IO[str] | typing.Any | None" = None) -> None:
    """Arm faulthandler's C-level timer.

    After ``timeout`` seconds with no re-pet, it dumps every thread's stack to
    the dedicated crash-dump file and then ``_exit(1)``.  Re-petted by every
    :meth:`LoopStallWatchdog.beat`.  Runs on faulthandler's own thread and reads
    thread states in C, so it fires even when the loop thread is wedged in a
    blocking syscall.  ``repeat=False`` because the process exits the first time
    it fires.

    *file* can be any object with a ``fileno()`` method (including
    :class:`~kiro_crew.dashboard.crash_dump_store.DumpFile`).  faulthandler's C
    code extracts the fd via ``fileno()`` at arm time and holds only the integer
    — so the fd must remain valid until fire.  :class:`DumpFile` guarantees this
    by never closing its fd.

    **Trade-off:** hard-exit dumps land ONLY in the dedicated file (not
    stderr/journal) because ``faulthandler.dump_traceback_later`` targets a
    single fd.  To ensure journal-only operators (containers, systemd) still see
    the stacks, the gateway replays the dump content into the logger on the next
    startup (see ``server.py`` startup dump surfacing).
    """
    target = file or sys.stderr
    faulthandler.dump_traceback_later(timeout, repeat=False, file=target, exit=True)


def _default_cancel_later() -> None:
    """Cancel any pending :func:`faulthandler.dump_traceback_later` timer."""
    faulthandler.cancel_dump_traceback_later()


class LoopStallWatchdog:
    """Detects a wedged asyncio loop and captures the frozen stacks.

    Two layers, both fed by :meth:`beat` (called from the async heartbeat):

    * the C-level ``faulthandler`` armed timer that dumps **and exits** at
      ``exit_after`` seconds of silence — authoritative; portable / no-root and
      it beats the external liveness probe so the dump is never lost to a race;
      and
    * the daemon-thread :meth:`check` that dumps **without** exiting at
      ``stall_after`` seconds — a soft observability fallback.

    With the default config (``exit_after=25`` < ``stall_after=30``) the armed
    timer ``_exit()``s the process before the soft dump's threshold is reached,
    so :meth:`check` only actually emits when the armed timer is **disabled or
    could not be armed** — i.e. ``exit_after=None`` (a dashboard-only process) or
    an ``arm_later`` failure that degrades to soft-only.  It is a fallback, not a
    second live layer in the gateway's default configuration.

    Args:
        stall_after: Seconds of heartbeat silence before the soft daemon-thread
            dump fires.  Only reachable when the armed timer is off/failed (see
            above).  Should comfortably exceed the heartbeat interval (default
            heartbeat is 5s, so 30s ≈ 6 missed ticks avoids false positives).
        exit_after: Seconds of silence before the authoritative
            ``dump_traceback_later(exit=True)`` fires.  The timer is re-armed
            only on each :meth:`beat`, so the real silence tolerated before
            ``_exit`` is ``exit_after`` minus up to one heartbeat interval (≈20s
            at the 5s default heartbeat).  Kept below the external liveness
            probe's kill window — the probe needs ``failure_threshold`` (3)
            consecutive failed polls at its poll interval (10s), i.e. ≳20s — so
            this in-process path generally wins and the faulthandler dump is
            captured rather than lost to the probe's SIGKILL.  The two budgets
            are close, so keep them in sync if either side is tuned.  ``None``
            disables the armed timer (only the soft dump remains).
        poll_interval: How often the daemon thread evaluates liveness.
        now: Monotonic clock, injectable for tests.
        dump: Soft stack-dump callback, injectable for tests.  Defaults to
            ``faulthandler.dump_traceback(all_threads=True)``.
        arm_later: Arms the C-level dump-then-exit timer for N seconds,
            injectable for tests so they never arm a real process-killing timer.
            Defaults to :func:`_default_arm_later`.
        cancel_later: Cancels the armed timer, injectable for tests.  Defaults
            to :func:`_default_cancel_later`.
        log: Logger, injectable for tests.
    """

    def __init__(
        self,
        *,
        stall_after: float = 30.0,
        exit_after: float | None = 25.0,
        poll_interval: float = 5.0,
        now: Callable[[], float] = time.monotonic,
        dump: Callable[[], None] | None = None,
        arm_later: Callable[[float], None] | None = None,
        cancel_later: Callable[[], None] | None = None,
        dump_file: "typing.IO[str] | typing.Any | None" = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._stall_after = stall_after
        self._exit_after = exit_after
        self._poll_interval = poll_interval
        self._now = now
        self._dump_file = dump_file
        self._dump = dump or (lambda: _default_dump(dump_file))
        self._arm_later = arm_later or (lambda t: _default_arm_later(t, dump_file))
        self._cancel_later = cancel_later or _default_cancel_later
        self._log = log or logger
        self._last_beat = now()
        self._dumped = False
        # True only between start() and stop() when exit_after is set; gates the
        # armed timer so a dashboard-only process (e.g. `kirocrew chat`, where
        # start() is never called) never arms a process-killing timer.
        self._later_active = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def beat(self) -> None:
        """Record that the event loop is alive *now* and re-pet the armed timer.

        Called from the async heartbeat each tick.  The liveness write is a
        single atomic float store (the GIL makes it safe between the loop thread
        and the daemon thread).  When the armed timer is active, each beat
        cancels and re-arms it, so it only fires after a genuine ``exit_after``
        gap with no beats — i.e. a real wedge, not a transient lag.
        """
        self._last_beat = self._now()
        if self._later_active and self._exit_after is not None:
            try:
                self._cancel_later()
                self._arm_later(self._exit_after)
            except Exception:  # pragma: no cover - never let petting crash the loop
                self._log.exception(
                    "loop watchdog failed to re-arm dump_traceback_later"
                )

    def check(self) -> bool:
        """Evaluate liveness once.  Returns ``True`` iff a soft dump was emitted.

        Pure and synchronous so tests can step it with a fake clock.  Emits at
        most one dump per stall episode and re-arms when the loop recovers.
        """
        silence = self._now() - self._last_beat
        if silence >= self._stall_after:
            if not self._dumped:
                self._dumped = True
                self._log.error(
                    "event loop STALLED for %.1fs — dumping all thread stacks. "
                    "The loop thread is almost certainly blocked in a syscall "
                    "(e.g. an un-timed-out socket close on a teardown path).",
                    silence,
                )
                try:
                    self._dump()
                except Exception:  # pragma: no cover - dump must never crash the watchdog
                    self._log.exception("loop watchdog stack dump failed")
                return True
            return False
        # Healthy / recovered.
        if self._dumped:
            self._log.warning(
                "event loop recovered after stall (last beat %.1fs ago)", silence
            )
        self._dumped = False
        return False

    def _run(self) -> None:
        # ``Event.wait`` returns True only when stopped; on timeout it returns
        # False, which is our cue to run another liveness check.
        while not self._stop.wait(self._poll_interval):
            try:
                self.check()
            except Exception:  # pragma: no cover - watchdog must outlive any error
                self._log.exception("loop watchdog check raised")

    def start(self) -> None:
        """Arm the C-level dump-then-exit timer and spawn the daemon thread (idempotent)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._last_beat = self._now()
        # Prime the authoritative dump-then-exit timer before the first beat.
        if self._exit_after is not None:
            try:
                self._cancel_later()
                self._arm_later(self._exit_after)
                self._later_active = True
            except Exception:  # pragma: no cover - degrade to soft dump only
                self._log.exception("loop watchdog failed to arm dump_traceback_later")
                self._later_active = False
        self._thread = threading.Thread(
            target=self._run, name="loop-stall-watchdog", daemon=True
        )
        self._thread.start()
        self._log.info(
            "loop stall watchdog armed (stall_after=%.0fs, poll=%.0fs, exit_after=%s)",
            self._stall_after,
            self._poll_interval,
            f"{self._exit_after:.0f}s" if self._exit_after is not None else "off",
        )

    def stop(self, timeout: float | None = 2.0) -> None:
        """Cancel the armed timer, signal the daemon thread to exit, join it (idempotent)."""
        self._stop.set()
        if self._later_active:
            self._later_active = False
            try:
                self._cancel_later()
            except Exception:  # pragma: no cover - cancel must never crash shutdown
                self._log.exception(
                    "loop watchdog failed to cancel dump_traceback_later"
                )
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def is_running(self) -> bool:
        """True while the daemon thread is started (between start() and stop()).

        A small public accessor so callers/tests can assert lifecycle state
        without reaching into the private ``_thread`` attribute.
        """
        return self._thread is not None and self._thread.is_alive()
