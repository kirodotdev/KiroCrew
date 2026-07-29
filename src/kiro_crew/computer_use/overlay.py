"""Gateway-side SUPERVISOR for the Cursor Motion overlay process.

This is the only module the rest of computer use talks to about the fake cursor.
It owns exactly three things:

1. deciding whether an overlay should exist at all (macOS + the ``cursor_motion``
   opt-in, default OFF);
2. the lifecycle of the ``kiro_crew.computer_use.overlay_proc`` child — lazy
   spawn, bounded retry, reap;
3. turning a "the agent is about to click here" event into a
   :class:`~kiro_crew.computer_use.cursor_motion.MotionPlan` and shipping it down
   the child's stdin.

**The overlay is PURELY COSMETIC and this module's whole design follows from
that.** It has two absolute properties, and both are asserted by tests:

* **It never raises into a caller.** Every public method swallows every
  exception. A tool call that would have succeeded must not fail because AppKit
  was unavailable, the child died, or the pipe was full — a failed overlay
  degrades to "no visual cursor", never to a failed tool call.
* **It never blocks the event loop.** The spawn is
  ``asyncio.create_subprocess_exec``; the only wait is a bounded
  ``asyncio.wait_for`` on the readiness line. The ANIMATION itself is
  fire-and-forget: the command is written and the coroutine returns, because the
  child draws on its own run loop and making a caller await ~1.4s of decoration
  would put a cosmetic subsystem on the latency path of a real action.

It is also a **no-op by construction** off macOS and when disabled: the enable
check runs before anything else in every method, so a Linux CI shard exercises
these bodies and observes that nothing is spawned.

Why a separate process at all: AppKit requires a main-thread run loop and the
gateway's main thread IS the asyncio loop. See ``overlay_proc``'s docstring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Sequence

from kiro_crew import platform_compat
from kiro_crew.computer_use.cursor_motion import MotionPlan, plan_motion
from kiro_crew.computer_use.types import (
    DEFAULT_CURVE_SCALE,
    MAX_CLICK_COUNT,
    OVERLAY_CMD_CLICK,
    OVERLAY_CMD_HIDE,
    OVERLAY_CMD_KEY,
    OVERLAY_CMD_MOVE,
    OVERLAY_CMD_QUIT,
    OVERLAY_KEY_COUNT,
    OVERLAY_KEY_MS,
    OVERLAY_KEY_POINTS,
    OVERLAY_KEY_X,
    OVERLAY_KEY_Y,
    OVERLAY_MAX_FAILURES,
    OVERLAY_MODULE,
    OVERLAY_READY_LINE,
    OVERLAY_SPAWN_TIMEOUT_SECS,
    OVERLAY_STOP_TIMEOUT_SECS,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CursorOverlay",
    "bind_gateway_loop",
    "cursor_motion_enabled",
    "get_shared_overlay",
    "reset_shared_overlay",
    "show_pointer_motion",
]


def cursor_motion_enabled() -> bool:
    """Whether the desktop cursor overlay may run.

    Two independent conditions, both required:

    * the platform is macOS — the overlay is an AppKit window and there is no
      cross-platform equivalent, so every other OS degrades to "no visual cursor"
      exactly the way ``UnsupportedBackend`` degrades the driver;
    * ``config.json``'s ``computer_use.cursor_motion`` is true — the typed
      ``ComputerUseConfig.cursor_motion`` field, default OFF, matching the
      reference implementation's opt-in.

    Still read through ``getattr`` despite the field now being declared: this
    module is import-safe against a partially-installed build, and a missing
    attribute must fall back to OFF rather than raise inside a tool call.
    Defaulting to OFF is also the safe direction — an unreadable setting can only
    ever mean "no decoration", never "start drawing on the user's screen".
    """
    if not platform_compat.IS_MACOS:
        return False
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        section = getattr(KiroCrewConfig.load(), "computer_use", None)
    except Exception:
        logger.debug("cursor-motion config unavailable; overlay disabled", exc_info=True)
        return False
    value = getattr(section, "cursor_motion", False)
    return value is True


class CursorOverlay:
    """Supervises at most one overlay child process.

    Serialized by a single :class:`asyncio.Lock`: the child's stdin is an ordered
    byte stream and two concurrent writers would interleave half-lines, so every
    public method takes the lock for the duration of its spawn-and-write. The lock
    is created lazily because this object is constructed at import-adjacent time
    (the shared singleton) and an ``asyncio.Lock`` built outside a running loop is
    the cross-loop hazard ``kiro_crew.__init__`` documents at length.
    """

    def __init__(self) -> None:
        self._proc: "asyncio.subprocess.Process | None" = None
        self._lock: "asyncio.Lock | None" = None
        self._failures = 0
        # Last tip position, so a move starts where the cursor actually is rather
        # than teleporting from a fixed corner. ``None`` means "never drawn".
        self._last_point: "tuple[float, float] | None" = None

    # ── public surface ──

    async def move_to(
        self,
        x: float,
        y: float,
        *,
        curve_scale: float = DEFAULT_CURVE_SCALE,
    ) -> bool:
        """Animate the fake cursor to the TOP-LEFT screen point ``(x, y)``.

        Returns whether a command was actually shipped — useful to a test and to
        the SEL audit trail, and ignored by every real caller, because the answer
        "no" is a perfectly acceptable outcome for a decoration.
        """
        if not cursor_motion_enabled():
            return False
        try:
            start = self._last_point or (float(x), float(y))
            plan = plan_motion(start, (float(x), float(y)), curve_scale=curve_scale)
            sent = await self._send(_move_command(plan))
            if sent:
                self._last_point = (float(x), float(y))
            return sent
        except Exception:
            # Belt and braces: ``_send`` already swallows, so reaching here means a
            # bug in the planner. Still not allowed to reach the caller.
            logger.debug("cursor-motion move failed", exc_info=True)
            return False

    async def pulse_click(self, x: float, y: float, count: int = 1) -> bool:
        """Draw *count* click pulses at ``(x, y)`` (top-left screen coordinates)."""
        if not cursor_motion_enabled():
            return False
        try:
            pulses = min(max(int(count), 1), MAX_CLICK_COUNT)
            command = {
                OVERLAY_CMD_KEY: OVERLAY_CMD_CLICK,
                OVERLAY_KEY_X: float(x),
                OVERLAY_KEY_Y: float(y),
                OVERLAY_KEY_COUNT: pulses,
            }
            sent = await self._send(command)
            if sent:
                self._last_point = (float(x), float(y))
            return sent
        except Exception:
            logger.debug("cursor-motion click failed", exc_info=True)
            return False

    async def hide(self) -> bool:
        """Order the fake cursor off screen, keeping the child alive for reuse.

        Does NOT spawn: hiding a cursor that was never drawn is a no-op, and
        starting a process in order to hide nothing would be absurd.
        """
        if self._proc is None:
            return False
        try:
            return await self._send({OVERLAY_CMD_KEY: OVERLAY_CMD_HIDE}, spawn=False)
        except Exception:
            logger.debug("cursor-motion hide failed", exc_info=True)
            return False

    async def stop(self) -> None:
        """Tear the child down: ``quit``, close stdin, reap, then force-kill.

        Three escalating steps, all bounded:

        1. a ``quit`` command, which lets the child order its window out cleanly;
        2. closing stdin — EOF is the child's primary exit path and the one that
           also covers a gateway crash, where step 1 never happened;
        3. after :data:`OVERLAY_STOP_TIMEOUT_SECS`, ``kill_process_tree`` via
           ``platform_compat`` (never a raw ``os.killpg``).

        Idempotent and never raises, so it is safe from a shutdown handler.
        """
        async with self._get_lock():
            proc = self._proc
            self._proc = None
            self._last_point = None
        if proc is None:
            return
        try:
            await self._write_line(proc, {OVERLAY_CMD_KEY: OVERLAY_CMD_QUIT})
        except Exception:
            logger.debug("cursor-motion quit write failed", exc_info=True)
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            logger.debug("cursor-motion stdin close failed", exc_info=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=OVERLAY_STOP_TIMEOUT_SECS)
            return
        # BOTH exception names, deliberately: on Python 3.11+ they are the same
        # class, but on 3.10 (which CI gates on) ``asyncio.TimeoutError`` does NOT
        # inherit from the builtin. Catching only one would let a timeout fall into
        # the ``except Exception`` below, which RETURNS — skipping the kill and
        # leaving the overlay child alive with a fake cursor on the user's screen.
        # Same reasoning at the other two wait_for sites in this module.
        except (asyncio.TimeoutError, TimeoutError):
            logger.debug("cursor-motion child ignored EOF; killing")
        except Exception:
            logger.debug("cursor-motion child wait failed", exc_info=True)
            return
        # Route the kill through platform_compat: a raw ``os.killpg`` is a POSIX-only
        # call and ``os.kill(pid, 0)`` TERMINATES on Windows. This code path cannot
        # run off macOS today, but the shim is the repo-wide contract.
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        except Exception:
            logger.debug("cursor-motion kill failed", exc_info=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=OVERLAY_STOP_TIMEOUT_SECS)
        except Exception:
            # A zombie we cannot reap is still better than raising from shutdown.
            logger.debug("cursor-motion child did not exit after kill", exc_info=True)

    @property
    def running(self) -> bool:
        """Whether a live child process is currently supervised."""
        proc = self._proc
        return proc is not None and proc.returncode is None

    # ── internals ──

    def _get_lock(self) -> asyncio.Lock:
        """The write lock, created inside the running loop on first use."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _send(self, command: "dict[str, Any]", *, spawn: bool = True) -> bool:
        """Ship one command, spawning the child first if needed.

        Returns False on every failure and logs at debug. The failure counter is
        what stops a broken AppKit from becoming a respawn loop: after
        :data:`OVERLAY_MAX_FAILURES` consecutive failures the supervisor gives up
        for the life of the process, because a cursor that cannot be drawn is
        cosmetic but a spawn loop is a real resource leak.
        """
        if self._failures >= OVERLAY_MAX_FAILURES:
            return False
        async with self._get_lock():
            proc = self._proc
            if proc is not None and proc.returncode is not None:
                # The child exited on its own (crash, or a manual kill). Forget it
                # so the next command spawns a fresh one instead of writing into a
                # dead pipe.
                logger.debug("cursor-motion child exited (rc=%s)", proc.returncode)
                proc = None
                self._proc = None
                self._last_point = None
            if proc is None:
                if not spawn:
                    return False
                proc = await self._spawn()
                if proc is None:
                    return False
            ok = await self._write_line(proc, command)
            if ok:
                self._failures = 0
            else:
                self._failures += 1
                # A failed write means the pipe is unusable; drop the child so the
                # next attempt starts clean rather than writing into it again.
                self._proc = None
                await self._reap(proc)
            return ok

    async def _spawn(self) -> "asyncio.subprocess.Process | None":
        """Start the overlay child and wait (bounded) for its readiness line.

        The argv is fixed: ``<this interpreter> -m
        kiro_crew.computer_use.overlay_proc``. Nothing agent-supplied enters it —
        the only agent-influenced values in this whole subsystem are the numeric
        coordinates, and those travel as JSON on stdin, never as argv.

        ``start_new_session`` / ``creationflags`` are passed explicitly per the
        repo's spawn-isolation contract, so the child sits in its own process group
        and :func:`platform_compat.kill_process_tree` can reap it.
        """
        argv = [sys.executable, "-m", OVERLAY_MODULE]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
        except Exception:
            logger.debug("cursor-motion spawn failed", exc_info=True)
            self._failures += 1
            return None
        if not await self._await_ready(proc):
            self._failures += 1
            await self._reap(proc)
            return None
        self._proc = proc
        return proc

    async def _await_ready(self, proc: "asyncio.subprocess.Process") -> bool:
        """Wait (bounded) for the child's ``KIROCREW_OVERLAY_READY`` line.

        Bounded because the alternative is a coroutine that hangs forever on a
        child that wedged before it printed anything — and this coroutine is
        awaited from a tool-call path, so an unbounded wait here would stall a real
        action for a decoration.

        A ``ready 0`` line (the child started but could not build a window) is
        treated as a failure so the supervisor's give-up counter advances rather
        than the gateway shipping commands into a process that will never draw.
        """
        if proc.stdout is None:
            return False
        try:
            line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=OVERLAY_SPAWN_TIMEOUT_SECS
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.debug("cursor-motion child never reported ready")
            return False
        except Exception:
            logger.debug("cursor-motion readiness read failed", exc_info=True)
            return False
        text = line.decode("utf-8", errors="replace").strip() if line else ""
        if not text.startswith(OVERLAY_READY_LINE):
            logger.debug("cursor-motion child said %r instead of ready", text[:80])
            return False
        return text.split()[-1] != "0"

    async def _write_line(
        self, proc: "asyncio.subprocess.Process", command: "dict[str, Any]"
    ) -> bool:
        """Write one NDJSON command and drain. Never raises."""
        stdin = proc.stdin
        if stdin is None or stdin.is_closing():
            return False
        try:
            payload = json.dumps(command, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            logger.debug("cursor-motion command not serializable", exc_info=True)
            return False
        try:
            stdin.write(f"{payload}\n".encode("utf-8"))
            await stdin.drain()
            return True
        except Exception:
            # BrokenPipeError / ConnectionResetError when the child is gone.
            logger.debug("cursor-motion write failed", exc_info=True)
            return False

    async def _reap(self, proc: "asyncio.subprocess.Process") -> None:
        """Best-effort teardown of a child we are abandoning.

        Always runs to completion so an abandoned child cannot survive as an orphan
        with a window on the user's screen: close stdin (its EOF exit), wait, and
        force-kill through ``platform_compat`` if it ignores both.
        """
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            logger.debug("cursor-motion reap stdin close failed", exc_info=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=OVERLAY_STOP_TIMEOUT_SECS)
            return
        except (asyncio.TimeoutError, TimeoutError):
            pass
        except Exception:
            logger.debug("cursor-motion reap wait failed", exc_info=True)
            return
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        except Exception:
            logger.debug("cursor-motion reap kill failed", exc_info=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=OVERLAY_STOP_TIMEOUT_SECS)
        except Exception:
            logger.debug("cursor-motion reap did not complete", exc_info=True)


def _move_command(plan: MotionPlan) -> "dict[str, Any]":
    """Serialize a :class:`MotionPlan` into the ``move`` wire command.

    Points go as ``[[x, y], ...]`` pairs rather than ``{"x":..,"y":..}`` objects:
    a 96-point path is the largest thing this protocol ever carries, and the pair
    form is roughly a third of the bytes for the same information.
    """
    return {
        OVERLAY_CMD_KEY: OVERLAY_CMD_MOVE,
        OVERLAY_KEY_POINTS: [[point[0], point[1]] for point in plan.points],
        OVERLAY_KEY_MS: plan.duration_ms,
    }


def points_payload(points: Sequence[tuple[float, float]]) -> "list[list[float]]":
    """Wire form for an arbitrary point sequence (used by tests and diagnostics)."""
    return [[float(x), float(y)] for x, y in points]


# ── Process-wide shared supervisor ──
# One overlay per process, for the same reason there is one backend and one
# snapshot cache: a second supervisor would spawn a second child and two fake
# cursors would fight over the same screen.

_shared_overlay: "CursorOverlay | None" = None


def get_shared_overlay() -> CursorOverlay:
    """Process-wide :class:`CursorOverlay` singleton.

    No lock: construction is a handful of attribute assignments with no I/O, and
    the gateway's callers all live on the one event loop. The ``asyncio.Lock``
    that actually matters is created inside the instance, in the running loop.
    """
    global _shared_overlay
    if _shared_overlay is None:
        _shared_overlay = CursorOverlay()
    return _shared_overlay


def reset_shared_overlay() -> None:
    """Drop the shared supervisor WITHOUT reaping its child (tests only).

    Deliberately does not stop the process: this is sync, ``stop`` is async, and a
    sync function that spawned a task to kill a process would be a worse hazard
    than the leak it avoided. Production shutdown calls ``await stop()``; tests
    that spawned a real child must do the same before resetting.
    """
    global _shared_overlay
    _shared_overlay = None


def show_pointer_motion(x: float, y: float, count: int = 1) -> None:
    """Animate the visible cursor to ``(x, y)`` and pulse it. **Sync, fire-and-forget.**

    This is the seam the BLOCKING dispatcher calls (``tools._perform``, on a
    worker thread) immediately before a real-pointer click or drag. Three
    properties make it safe from there:

    * **it never blocks the caller.** The animation is scheduled onto the gateway's
      event loop with ``run_coroutine_threadsafe`` and the future is NOT awaited.
      Waiting for the glide would add its duration to every pointer click's latency
      for a purely cosmetic effect, and a wedged AppKit child would then stall the
      tool call itself;
    * **it never raises.** A decoration must not be able to turn a successful click
      into a failed tool call, so every failure — no running loop, a dead child, a
      full pipe — is swallowed at debug;
    * **it is not a permit.** By the time this runs the click has already been
      authorized upstream and its method resolved to a pointer-moving one. Drawing
      a cursor grants nothing, and skipping the drawing denies nothing.

    Ordering is best-effort by design: the click may land a few milliseconds before
    the drawn cursor finishes its glide. Making it strictly-before would mean
    awaiting an animation on the critical path, which is the trade this rejects.
    """
    if not cursor_motion_enabled():
        return
    try:
        loop = _gateway_loop()
        if loop is None:
            return
        overlay = get_shared_overlay()

        async def _animate() -> None:
            await overlay.move_to(x, y)
            await overlay.pulse_click(x, y, count)

        asyncio.run_coroutine_threadsafe(_animate(), loop)
    except Exception:
        logger.debug("cursor-motion pre-click animation could not be scheduled", exc_info=True)


def _gateway_loop() -> "asyncio.AbstractEventLoop | None":
    """The gateway's event loop, or ``None`` when there is not one.

    Recorded by :func:`bind_gateway_loop` at gateway start rather than discovered:
    this is called from a worker thread, where ``get_running_loop`` raises and
    ``get_event_loop`` would either create a fresh unrun loop or fail depending on
    the Python version — and a coroutine scheduled onto a loop nobody runs would
    simply never execute.
    """
    loop = _bound_loop
    if loop is None or loop.is_closed():
        return None
    return loop


_bound_loop: "asyncio.AbstractEventLoop | None" = None


def bind_gateway_loop(loop: "asyncio.AbstractEventLoop | None" = None) -> None:
    """Record the loop that :func:`show_pointer_motion` should schedule onto.

    Called from the async invoke handler (which runs ON that loop) rather than from
    a startup hook, so the binding cannot go stale across a gateway restart and no
    lifecycle wiring is needed for a feature that is off by default.
    """
    global _bound_loop
    if loop is not None:
        _bound_loop = loop
        return
    try:
        _bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        _bound_loop = None
