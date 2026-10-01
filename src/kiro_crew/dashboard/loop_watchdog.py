"""Off-loop watchdog that turns an event-loop stall into a logged stack dump.

KiroCrew's gateway runs the dashboard HTTP server, every agent turn, and all
background tasks on a *single* asyncio event loop on *one* thread.  If a
coroutine performs a blocking syscall on that thread — e.g. an un-timed-out
socket ``close()`` while tearing down a half-dead ACP/model stream, a burst of
``os.waitpid()`` reaping many kiro-cli children at once, or a ``stat`` walk of
a directory holding thousands of entries — the whole loop wedges:

* the HTTP server stops answering (``/api/status`` is itself a coroutine on the
  wedged loop, so it cannot even report "I'm sick" — it just hangs);
* the async event-loop heartbeat can no longer be scheduled, so the log goes
  **silent**; and
* that silence is the only signal, and it carries no information about *where*
  the loop is stuck.

The async heartbeat calls :meth:`LoopStallWatchdog.beat` every tick, and two
things turn the silence that follows a wedge into an actionable artifact:

1. **Authoritative dump-then-exit: the kernel's per-process alarm.**  Each beat
   re-arms ``setitimer(ITIMER_REAL)`` for ``exit_after`` seconds
   (:func:`kiro_crew.platform_compat.arm_process_alarm`), and
   ``faulthandler.register(SIGALRM, chain=True)`` on the dedicated crash-dump
   file makes the alarm dump *all* thread stacks from inside the signal
   handler — in C, with no GIL and no Python — and then hand ``SIGALRM`` to
   its default disposition, which ends the process.  The alarm needs no
   thread and no root, fires whatever the loop thread is doing (a blocking
   syscall or a long C call holding the GIL), and **pauses while the host
   sleeps**: Linux runs ``ITIMER_REAL`` on ``CLOCK_MONOTONIC`` (the kernel
   initialises the process's ``real_timer`` on that clock), and macOS
   schedules it on the absolute mach timebase, which does not advance during
   sleep.  So a laptop resume does not fire a deadline armed before the
   sleep; the heartbeat, on the same monotonic clock, missed no beat either.
   Desktop launches use a 25s exit budget near Electron's independent
   liveness window; managed services have no Electron probe, use a wider
   budget, and receive the soft dump first.

   faulthandler's own ``dump_traceback_later`` timer cannot be that decider
   on every platform: it waits on an interpreter lock whose deadline clock is
   fixed when CPython is built — ``sem_clockwait(CLOCK_MONOTONIC)`` when the
   build's libc offered it, otherwise ``sem_timedwait`` on
   ``CLOCK_REALTIME``, which jumps forward by the whole suspend on resume
   and fires any pending deadline the instant the host wakes, whatever its
   budget (portable interpreter builds and every macOS build take that
   branch).  Windows has no process alarm, so there that timer carries the
   exit at the same budget (:func:`exit_mechanism`): Windows'
   ``time.monotonic()`` counts a sleep as well, so a sleep already reads as
   silence on that platform and the timer's wall-clock deadline adds nothing
   new.

   **Journal visibility:** the dump lands in the dedicated file only (not
   stderr/journal) because faulthandler targets a single fd.  On the next
   gateway startup, ``server.py`` detects and replays the dump content to the
   logger at WARNING level (capped at 120 lines / 8 KB), so journal-only
   operators (containers, ``journalctl``) see the stacks one restart later —
   exactly when they are investigating why the gateway died.

2. **Soft observability (the daemon thread).**  A daemon thread measures the
   silence since the last beat on the **monotonic clock** and, in order:
   emits stall enrichment (the stall timestamp and this process's
   established TCP sockets) to the logger at WARNING at ``enrich_after``;
   dumps all thread stacks *without* exiting at ``stall_after``, re-arming on
   recovery.  While the exit is armed that soft dump stays on stderr, so a
   recovered stall is not classified as a fatal crash; when no exit can
   follow (``exit_after`` is ``None``, or the alarm failed to arm) it also
   goes to the dedicated file, because no later fatal capture can make it
   discoverable.  The daemon thread keeps
   running while the loop thread is blocked in the kernel — CPython releases
   the GIL around blocking syscalls such as ``close()`` / ``waitpid()`` /
   ``stat()``, the class of wedge observed in production — and is starved by
   a thread holding the GIL inside a long C call, which is one more reason
   the exit decision does not live on it.

**A suspend is not a stall.**  ``CLOCK_MONOTONIC`` does not count time the
host spends suspended, so across a laptop sleep neither the heartbeat nor
the daemon thread sees silence.  Each poll also samples the
suspend-inclusive clock :func:`kiro_crew.platform_compat.boottime_now`
(``CLOCK_BOOTTIME`` on Linux, the wall clock on macOS, ``None`` where no
such clock exists); when it advances :data:`SUSPEND_SKEW_MIN_SECS` further
than the monotonic clock did since the last poll, the host slept for that
long, and the watchdog says so once at INFO.

The class is deliberately split so the decision logic (:meth:`check`) is a
pure, synchronous step that can be driven from tests with injected clocks
and a dump callback, and the alarm arm/cancel calls are injectable too — so
tests verify the wiring without ever arming a real process-ending timer.
"""

from __future__ import annotations

import faulthandler
import logging
import signal
import sys
import threading
import time
import typing
from collections.abc import Callable

from kiro_crew.dashboard.stall_enrichment import collect_stall_enrichment
from kiro_crew.platform_compat import arm_process_alarm, boottime_now, process_alarm_available

logger = logging.getLogger("kiro_crew.dashboard.loop_watchdog")

#: Minimum divergence, in seconds across one poll, between the suspend-inclusive
#: clock and the monotonic clock that is reported as a suspend.  On Linux the
#: two clocks otherwise advance in lock-step; on the wall-clock fallback a
#: small NTP slew stays far below this floor.
SUSPEND_SKEW_MIN_SECS: float = 2.0

#: The mechanism the last :func:`_default_arm_later` armed the dump-then-exit
#: on — ``"alarm"`` or ``"faulthandler"`` — or ``None`` while it holds no
#: pending deadline.  Process-wide like the two deadlines it names
#: (``ITIMER_REAL`` and faulthandler's single timer); written only by the
#: default arm/cancel pair, and read by the cancel so it targets the deadline
#: that was actually started, not the one :func:`exit_mechanism` would pick now.
_armed_mechanism: str | None = None


def _default_dump(file: "typing.IO[str] | typing.Any | None" = None) -> None:
    """Dump every thread's stack to a dedicated file and stderr.

    The caller passes ``dump_file`` only when no exit can follow the soft
    dump. That keeps soft-only failures discoverable by ``doctor`` while a
    recoverable pre-exit dump in a managed service remains journal-only and
    cannot masquerade as a fatal crash at the next clean startup.
    """
    target = file or sys.stderr
    faulthandler.dump_traceback(file=target, all_threads=True)
    if target is not sys.stderr:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)


def exit_mechanism() -> str:
    """Which mechanism carries the dump-then-exit here: ``alarm`` or ``faulthandler``.

    The kernel's process alarm where it exists (POSIX) **and** ``SIGALRM`` is
    still at its default disposition.  A process that already handles
    ``SIGALRM`` from Python — pytest-timeout in a test process, an embedding
    host with its own alarm — owns ``ITIMER_REAL`` too, and arming or
    cancelling it from here would silently remove that owner's deadline; the
    watchdog then leaves both alone and uses faulthandler's own
    ``dump_traceback_later`` thread at the same budget.  Windows has no such
    timer, so faulthandler's thread stands in there as well.  Its wall-clock
    deadline is no new false exit on Windows: ``time.monotonic()`` counts a
    sleep there too, so a sleep already reads as silence on that platform.
    faulthandler's own ``SIGALRM`` registration is not a Python-level handler,
    so the check keeps answering ``alarm`` once the watchdog holds the signal.
    Asked once per arm by :func:`_default_arm_later`, which latches the answer
    for the matching :func:`_default_cancel_later`; an owner that appears or
    leaves between two beats therefore moves the exit at the next re-arm and
    never has the cancel aimed at a deadline that was not the one started.
    """
    if not process_alarm_available():
        return "faulthandler"
    return "alarm" if signal.getsignal(signal.SIGALRM) == signal.SIG_DFL else "faulthandler"


def _default_arm_later(timeout: float, file: "typing.IO[str] | typing.Any | None" = None) -> None:
    """Arm the dump-then-exit: dump every thread from C after ``timeout``, then end.

    Where the process alarm exists, registers faulthandler's ``SIGALRM``
    handler on the dedicated crash-dump file with ``chain=True`` and starts the
    kernel's ``ITIMER_REAL`` countdown.  When it fires, faulthandler writes
    every thread's stack to the file inside the signal handler — no GIL, no
    Python — and then re-raises ``SIGALRM`` under its default disposition,
    which terminates the process.  ``SIGALRM`` and ``ITIMER_REAL`` belong to
    the watchdog in the gateway process.  Where the alarm is unavailable or
    another Python handler already owns ``SIGALRM`` (see
    :func:`exit_mechanism`) it arms ``faulthandler.dump_traceback_later``
    with ``exit=True``, which dumps and ``_exit(1)``s from faulthandler's own
    thread, and touches neither the signal nor the interval timer.  Re-armed
    by every :meth:`LoopStallWatchdog.beat`, so it fires only after a genuine
    ``exit_after`` gap with no beats.  The ``SIGALRM`` registration is released
    and renewed on every arm: ``faulthandler.register`` reinstalls nothing while
    it believes it still holds the signal, so a temporary owner that handed the
    signal back with ``SIG_DFL`` would otherwise leave the next alarm to end the
    process without a dump.

    The mechanism is decided here, once per arm, and latched in
    :data:`_armed_mechanism` so :func:`_default_cancel_later` cancels the
    deadline this arm started rather than re-deriving which one that was: a
    foreign ``signal.signal(SIGALRM, ...)`` installed between an arm and the
    next beat flips :func:`exit_mechanism`, and a cancel that followed the
    new answer would cancel faulthandler's idle timer and leave the pending
    itimer to fire into the foreign handler beside the exit the re-arm then
    starts.  With the latch, that beat cancels the itimer and the re-arm moves
    the exit onto faulthandler's timer, so exactly one exit is pending on the
    mechanism the foreign owner leaves free.

    *file* can be any object with a ``fileno()`` method (including
    :class:`~kiro_crew.dashboard.crash_dump_store.DumpFile`).  faulthandler
    extracts the fd via ``fileno()`` at registration and holds only the
    integer — so the fd must remain valid until fire.  :class:`DumpFile`
    guarantees this by never closing its fd.

    **Trade-off:** this dump lands ONLY in the dedicated file (not
    stderr/journal) because faulthandler targets a single fd.  To ensure
    journal-only operators (containers, systemd) still see the stacks, the
    gateway replays the dump content into the logger on the next startup (see
    ``server.py`` startup dump surfacing).
    """
    global _armed_mechanism
    target = file or sys.stderr
    mechanism = exit_mechanism()
    if mechanism == "alarm":
        # ``faulthandler.register`` installs its C handler only while it holds
        # no registration for the signal; a repeat call updates the file and
        # flags and leaves the disposition alone.  A temporary Python owner of
        # ``SIGALRM`` that hands the signal back with ``SIG_DFL`` has overwritten
        # that handler, so a bare re-register would arm an alarm whose delivery
        # ends the process with no dump.  Releasing the registration first makes
        # every arm install the handler afresh.  Every caller cancels the pending
        # deadline before it arms, so no alarm of ours can land in the gap.
        faulthandler.unregister(signal.SIGALRM)
        faulthandler.register(signal.SIGALRM, file=target, all_threads=True, chain=True)
        arm_process_alarm(timeout)
    else:
        faulthandler.dump_traceback_later(timeout, repeat=False, file=target, exit=True)
    _armed_mechanism = mechanism


def _default_cancel_later() -> None:
    """Cancel the pending dump-then-exit on the mechanism the last arm latched.

    Nothing latched means this module armed nothing, so nothing is cancelled:
    the alternative — deriving a mechanism to cancel — would reach for a timer
    another owner may hold.  The latch is cleared only once the cancel has
    happened, so a cancel that raises leaves the pending deadline recorded for
    the next beat to retry.
    """
    global _armed_mechanism
    if _armed_mechanism == "alarm":
        arm_process_alarm(0.0)
    elif _armed_mechanism == "faulthandler":
        faulthandler.cancel_dump_traceback_later()
    _armed_mechanism = None


def disarm_inherited_alarm() -> bool:
    """Cancel a process alarm this image inherited across ``execv``; True if one was cancelled.

    ``execve`` preserves ``ITIMER_REAL`` and resets a caught ``SIGALRM`` to its
    default disposition, so an in-app restart or update could hand this process
    the predecessor's pending dump-then-exit deadline as a lethal signal it never
    armed -- during boot, before :meth:`LoopStallWatchdog.start` runs and
    replaces the deadline with its own.  The predecessor cancels its alarm inside
    the exec seam (:func:`kiro_crew.platform_compat.reexec_launcher`,
    :func:`kiro_crew.platform_compat.reexec_python_module`); this is the
    successor's own half, called once from the gateway entrypoint as soon as
    faulthandler is enabled.  Same ownership rule as :func:`exit_mechanism`:
    the timer is ours to clear only while ``SIGALRM`` is at its default
    disposition -- a process that already handles ``SIGALRM`` from Python
    (pytest-timeout in a test worker, an embedding host) owns ``ITIMER_REAL``
    too, and its deadline is left alone.  Nothing is armed here.
    """
    if exit_mechanism() != "alarm":
        return False
    return arm_process_alarm(0.0)


class LoopStallWatchdog:
    """Detects a wedged asyncio loop and captures the frozen stacks.

    Two layers, both fed by :meth:`beat` (called from the async heartbeat):

    * the kernel's per-process alarm, re-armed by every beat for ``exit_after``
      seconds with faulthandler's ``SIGALRM`` handler registered on the
      crash-dump file, which dumps **and exits** at ``exit_after`` seconds of
      silence — authoritative, GIL-free, no-root, and paused by a host suspend
      (faulthandler's own timer stands in where no alarm exists, see
      :func:`exit_mechanism`); and
    * the daemon-thread :meth:`check` that enriches at ``enrich_after`` and
      dumps **without** exiting at ``stall_after`` — a soft observability
      layer on the monotonic clock, which also reports a resume once at INFO.

    When ``exit_after`` is below ``stall_after`` (the desktop default), the
    alarm exits before the soft threshold.  When it is above ``stall_after``
    (the managed-service default), the soft dump records the transient stall
    and the alarm exits only if recovery never arrives.

    Args:
        stall_after: Seconds of heartbeat silence before the soft daemon-thread
            dump fires. Should comfortably exceed the heartbeat interval
            (default heartbeat is 5s, so 30s ≈ 6 missed ticks avoids false
            positives).
        exit_after: Seconds of silence before the alarm dumps every thread's
            stack and ends the process.  The alarm is re-armed only on each
            :meth:`beat`, so the real silence tolerated is ``exit_after`` minus
            up to one heartbeat interval.  ``None`` disables the exit (only
            the soft dump remains).
        poll_interval: How often the daemon thread evaluates liveness.
        now: Monotonic clock, injectable for tests.
        suspend_clock: Suspend-inclusive clock read beside ``now`` on every
            poll to recognise a resume, ``None`` where the platform has none;
            injectable for tests.  Defaults to
            :func:`kiro_crew.platform_compat.boottime_now`.
        dump: Soft stack-dump callback, injectable for tests. By default a
            soft dump is stderr-only while the alarm is armed, otherwise it is
            written to both ``dump_file`` and stderr.
        arm_later: Arms the dump-then-exit for N seconds, injectable for
            tests so they never arm a real process-ending timer.  Defaults to
            :func:`_default_arm_later`.
        cancel_later: Cancels the armed dump-then-exit, injectable for tests.
            Defaults to :func:`_default_cancel_later`.
        dump_file: The crash-dump file (any object with ``fileno()``) the
            alarm and the soft-only dump write into.
        enrich_after: Seconds of heartbeat silence before the daemon thread
            emits stall enrichment (stall UTC timestamp + this process's
            established TCP sockets with rx/tx queue depths) to the logger at
            WARNING.  Must sit below ``exit_after`` so the capture lands
            *before* the alarm's dump-then-exit — with the 5s poll cadence,
            15s triggers on the 15–20s tick, ahead of the 25s exit.
            Never written into ``dump_file``: that file is the boot-time
            crash sentinel (line-count classified), so an append would make a
            recovered stall read as a fatal crash on the next startup.  Both
            production stalls to date froze the loop inside websocket frame
            parsing; this records *which* socket without needing a repro.
        enrich: Enrichment collector ``silence_secs -> lines``, injectable for
            tests.  Defaults to
            :func:`kiro_crew.dashboard.stall_enrichment.collect_stall_enrichment`.
        log: Logger, injectable for tests.
    """

    # Floor between heartbeat-lag captures, across episodes.
    _LAG_CAPTURE_COOLDOWN_SECS = 60.0

    def __init__(
        self,
        *,
        stall_after: float = 30.0,
        exit_after: float | None = 25.0,
        poll_interval: float = 5.0,
        now: Callable[[], float] = time.monotonic,
        suspend_clock: Callable[[], float | None] | None = None,
        dump: Callable[[], None] | None = None,
        arm_later: Callable[[float], None] | None = None,
        cancel_later: Callable[[], None] | None = None,
        dump_file: "typing.IO[str] | typing.Any | None" = None,
        enrich_after: float = 15.0,
        enrich: "Callable[[float], list[str]] | None" = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._stall_after = stall_after
        self._exit_after = exit_after
        self._poll_interval = poll_interval
        self._now = now
        self._suspend_clock = suspend_clock or boottime_now
        self._dump_file = dump_file
        self._dump = dump
        self._arm_later = arm_later or (lambda t: _default_arm_later(t, dump_file))
        self._cancel_later = cancel_later or _default_cancel_later
        self._enrich_after = enrich_after
        self._enrich = enrich or collect_stall_enrichment
        self._enriched = False
        self._lag_enriched = False
        self._lag_inflight = False
        self._lag_next_at = 0.0
        self._log = log or logger
        self._last_beat = now()
        self._dumped = False
        # True only between start() and stop() when exit_after is set; gates the
        # alarm so a dashboard-only process (e.g. `kirocrew chat`, where
        # start() is never called) never arms a process-ending timer.
        self._later_active = False
        # The two clock readings of the last poll, for the suspend comparison.
        self._last_poll_mono: float | None = None
        self._last_poll_suspend: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def beat(self) -> None:
        """Record that the event loop is alive *now* and re-pet the alarm.

        Called from the async heartbeat each tick.  The liveness write is a
        single atomic float store (the GIL makes it safe between the loop thread
        and the daemon thread).  While the alarm is armed, each beat cancels and
        re-arms it, so it only fires after a genuine ``exit_after`` gap with no
        beats — i.e. a real wedge, not a transient lag.
        """
        self._last_beat = self._now()
        if self._later_active and self._exit_after is not None:
            try:
                self._cancel_later()
            except Exception:  # pragma: no cover - never let petting crash the loop
                # Cancellation failed, so the previous alarm may still be
                # pending and still owns the crash file.  Keep the active flag
                # rather than creating a competing soft sentinel on that
                # uncertain path.
                self._log.exception("loop watchdog failed to cancel the alarm before re-arm")
                return
            try:
                self._arm_later(self._exit_after)
            except Exception:  # pragma: no cover - never let petting crash the loop
                # Cancellation succeeded but no replacement alarm exists, and
                # with the flag cleared no later beat arms one: the exit is off
                # for the rest of this run, so the soft watchdog writes the
                # discoverable file as well as stderr from here on.
                self._later_active = False
                self._log.exception(
                    "loop watchdog failed to re-arm the alarm; the stall exit stays off "
                    "for the rest of this run"
                )

    def check(self) -> bool:
        """Evaluate liveness once.  Returns ``True`` iff a soft dump was emitted.

        Pure and synchronous so tests can step it with fake clocks.  Emits at
        most one dump per stall episode and re-arms when the loop recovers.

        Stages by silence duration on the monotonic clock (defaults):
        **enrichment** at ``enrich_after`` (15s) — stall timestamp + socket
        table emitted to the logger at WARNING while the armed 25s alarm is
        still pending — then the **soft dump** at ``stall_after`` (30s),
        reachable only when the alarm is off, failed, or set later.

        A poll first compares the two clocks' advance since the previous poll:
        the suspend-inclusive clock running ahead of the monotonic one by
        :data:`SUSPEND_SKEW_MIN_SECS` or more means the host was suspended for
        that long.  That is reported once at INFO and changes no decision —
        the silence measured here is monotonic, and the alarm pauses during a
        suspend, so a suspend never counts as a stall on either layer.

        Enrichment deliberately never touches ``dump_file``: that file is the
        boot-time crash sentinel (``crash_dump_store._is_header_only`` counts
        lines), so a watchdog-side append would make a *recovered* 15–25s
        stall read as a fatal crash on the next startup.  Only faulthandler
        writes stacks into it.  The journal WARNING survives both outcomes —
        the process lives to keep logging on recovery, and journald has
        already persisted the line when the alarm ends a fatal stall.
        """
        mono = self._now()
        self._note_resume(mono)
        silence = mono - self._last_beat
        if silence >= self._enrich_after and not self._enriched:
            self._enriched = True
            try:
                lines = self._enrich(silence)
            except Exception:  # pragma: no cover - collector already degrades; belt & braces
                self._log.exception("loop watchdog stall enrichment failed")
                lines = ["=== STALL ENRICHMENT FAILED (collector raised) ==="]
            self._log.warning(
                "event loop silent %.1fs — stall enrichment captured:\n%s",
                silence,
                "\n".join(lines),
            )
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
                    if self._dump is not None:
                        self._dump()
                    elif self._later_active:
                        # A managed service may recover before its wider alarm
                        # budget. Keep that diagnostic in the journal; the
                        # alarm owns the fatal crash-sentinel file.
                        _default_dump()
                    else:
                        # No alarm can create a discoverable artifact, so
                        # retain the soft-only dump in the dedicated file too.
                        _default_dump(self._dump_file)
                except Exception:  # pragma: no cover - dump must never crash the watchdog
                    self._log.exception("loop watchdog stack dump failed")
                return True
            return False
        if silence >= self._enrich_after:
            # Mid-episode: enriched but below the soft-dump threshold.  Keep the
            # episode flags so neither capture repeats within one stall.
            return False
        # Healthy / recovered — silence is back below the first threshold.
        if self._dumped:
            self._log.warning(
                "event loop recovered after stall (last beat %.1fs ago)", silence
            )
        if self._enriched and not self._dumped:
            self._log.warning(
                "event loop recovered after stall enrichment (last beat %.1fs ago)",
                silence,
            )
        self._dumped = False
        self._enriched = False
        return False

    def claim_lag_enrichment(self, lag: float) -> bool:
        """Return ``True`` at most once per episode of heartbeat lag.

        Cheap and non-blocking, so the heartbeat may call it on the loop every
        tick, before :meth:`beat`. A lag at or below the heartbeat's 1s warning
        threshold ends the episode. A stall :meth:`check` already captured, a
        capture still running, or one within the cooldown is not captured. On
        ``True`` the caller runs :meth:`log_lag_enrichment` off the loop.
        """
        if lag <= 1.0:
            self._lag_enriched = False
            return False
        now = self._now()
        if self._lag_enriched or self._enriched or self._lag_inflight or now < self._lag_next_at:
            return False
        self._lag_next_at = now + self._LAG_CAPTURE_COOLDOWN_SECS
        self._lag_enriched = True
        self._lag_inflight = True
        return True

    def log_lag_enrichment(self, lag: float) -> None:
        """Log the ``enrich`` capture for a recovered heartbeat lag.

        Logger only, never ``dump_file``, for the crash-sentinel reason that
        :meth:`check` gives. The collector's own header describes a capture
        taken during a stall, so it is replaced. Reads procfs: run off the loop.
        """
        try:
            lines = self._enrich(lag)[1:]
        except Exception:  # pragma: no cover - collector already degrades
            self._log.exception("loop watchdog lag enrichment failed")
            lines = ["(collector raised)"]
        finally:
            self._lag_inflight = False
        self._log.warning(
            "event-loop heartbeat lag %.1fs — socket snapshot after recovery:\n%s",
            lag,
            "\n".join(lines),
        )

    def _note_resume(self, mono: float) -> None:
        """Compare the two clocks' advance since the last poll; log a suspend once.

        On a live host the suspend-inclusive clock and the monotonic clock
        advance together.  Time the host spent suspended advances only the
        former, so their difference across one poll is the length of a sleep
        that ended since the last poll.  The comparison is between consecutive
        polls, not since the last beat, so one resume produces one line.
        """
        try:
            suspend_now = self._suspend_clock()
        except Exception:  # pragma: no cover - a clock read must never stop the check
            self._log.debug("loop watchdog suspend clock unreadable", exc_info=True)
            return
        if suspend_now is None:
            return
        prev_mono, prev_suspend = self._last_poll_mono, self._last_poll_suspend
        self._last_poll_mono, self._last_poll_suspend = mono, suspend_now
        if prev_mono is None or prev_suspend is None:
            return
        mono_delta = mono - prev_mono
        skew = (suspend_now - prev_suspend) - mono_delta
        if skew >= SUSPEND_SKEW_MIN_SECS:
            self._log.info(
                "host was suspended (or its clock stepped forward) for about %.0fs: the "
                "suspend-inclusive clock advanced %.0fs while the monotonic clock advanced "
                "%.1fs since the last poll; the event loop's heartbeat silence is %.1fs on "
                "the monotonic clock and the stall alarm paused with the host, so this is "
                "not a stall and nothing exits",
                skew,
                skew + mono_delta,
                mono_delta,
                mono - self._last_beat,
            )

    def _run(self) -> None:
        # ``Event.wait`` returns True only when stopped; on timeout it returns
        # False, which is our cue to run another liveness check.
        while not self._stop.wait(self._poll_interval):
            try:
                self.check()
            except Exception:  # pragma: no cover - watchdog must outlive any error
                self._log.exception("loop watchdog check raised")

    def start(self) -> None:
        """Arm the dump-then-exit alarm and spawn the daemon thread (idempotent)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._last_beat = self._now()
        self._last_poll_mono = None
        self._last_poll_suspend = None
        # Prime the authoritative dump-then-exit before the first beat.
        if self._exit_after is not None:
            try:
                self._cancel_later()
                self._arm_later(self._exit_after)
                self._later_active = True
            except Exception:  # pragma: no cover - degrade to soft dump only
                self._log.exception("loop watchdog failed to arm the stall alarm")
                self._later_active = False
        self._thread = threading.Thread(
            target=self._run, name="loop-stall-watchdog", daemon=True
        )
        self._thread.start()
        if self._exit_after is not None and self._enrich_after >= self._exit_after:
            # Not fatal — enrichment just never lands before the exit.  Flag it
            # so a tuned exit budget doesn't silently disable the capture.
            self._log.warning(
                "loop watchdog enrich_after (%.0fs) >= exit_after (%.0fs); "
                "stall enrichment will not be captured before dump-then-exit",
                self._enrich_after,
                self._exit_after,
            )
        if self._exit_after is not None and self._later_active:
            exit_desc = f"{self._exit_after:.0f}s ({exit_mechanism()})"
        else:
            exit_desc = "off"
        self._log.info(
            "loop stall watchdog armed (stall_after=%.0fs, poll=%.0fs, exit_after=%s, "
            "enrich_after=%.0fs)",
            self._stall_after,
            self._poll_interval,
            exit_desc,
            self._enrich_after,
        )

    def stop(self, timeout: float | None = 2.0) -> None:
        """Cancel the alarm, signal the daemon thread to exit, join it (idempotent)."""
        self._stop.set()
        if self._later_active:
            self._later_active = False
            try:
                self._cancel_later()
            except Exception:  # pragma: no cover - cancel must never crash shutdown
                self._log.exception("loop watchdog failed to cancel the stall alarm")
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def is_running(self) -> bool:
        """True while the daemon thread is started (between start() and stop()).

        A small public accessor so callers/tests can assert lifecycle state
        without reaching into the private ``_thread`` attribute.
        """
        return self._thread is not None and self._thread.is_alive()
