"""Session-start admission for the shared ACP runtime.

Owns the gateway-wide cold-start admission every runtime spawn enters, the
``SessionStartGate`` that bounds concurrent ``session/new`` requests per event loop,
and the ``StartCollector`` that keeps a timed-out ``session/new`` alive until its late
answer is adopted, torn down or abandoned, with the start timeouts they read.
``AcpRuntime`` drives all of them.

``kiro_crew.acp.runtime`` re-exports every name defined here.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from collections import deque
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.config import live

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

# The functions read ``time``, ``weakref``, ``AcpSessionHandle`` and ``AcpRuntimeDead``
# through ``kiro_crew.acp.runtime``, imported inside the function: a test that replaces
# one of those bindings there -- most often with a clock it steps -- reaches this code
# at call time, as it did when the code lived there. The ``weakref`` bound here builds
# the two registries below once, at import. Circular import: ``kiro_crew.acp.runtime``
# imports this module while it loads, so this code cannot import it at the top.

# Logged under the runtime's name: start admission is the runtime's, and operator log
# filters and level settings key on that name.
logger = logging.getLogger("kiro_crew.acp.runtime")


# One gateway event loop owns many independent SessionManager and worker-pool
# callers. Keep their expensive subprocess spawn + initialize handshakes behind
# one low process-wide-per-loop bound; worker pools use the same default.
_COLD_START_MAX_CONCURRENT = 2


class _ColdStartAdmission:
    """Loop-affine admission state for runtime spawn + initialize."""

    def __init__(self, limit: int) -> None:
        self.semaphore = asyncio.Semaphore(limit)
        self.active = 0
        self.queued = 0

    async def acquire(self) -> float:
        from kiro_crew.acp.runtime import time

        started = time.monotonic()
        self.queued += 1
        acquired = False
        try:
            await self.semaphore.acquire()
            acquired = True
        finally:
            self.queued -= 1
        if acquired:
            self.active += 1
        return (time.monotonic() - started) * 1000.0

    def release(self) -> None:
        self.active = max(0, self.active - 1)
        self.semaphore.release()


# asyncio synchronization primitives are loop-affine. Gateways normally have one
# loop, while tests and embedded callers can create several; keying by loop keeps
# the production bound gateway-wide without binding a semaphore to the wrong loop.
_cold_start_admissions: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[_ColdStartAdmission]
] = weakref.WeakKeyDictionary()
_cold_start_admissions_lock = threading.Lock()


def _cold_start_admission() -> _ColdStartAdmission:
    from kiro_crew.acp.runtime import weakref

    loop = asyncio.get_running_loop()
    with _cold_start_admissions_lock:
        admission_ref = _cold_start_admissions.get(loop)
        admission = admission_ref() if admission_ref is not None else None
        if admission is None:
            admission = _ColdStartAdmission(_COLD_START_MAX_CONCURRENT)
            _cold_start_admissions[loop] = weakref.ref(admission)
        return admission


def _cold_start_counts() -> tuple[int, int]:
    """Current-loop active and queued starts for bounded diagnostics."""
    admission = _cold_start_admission()
    return admission.active, admission.queued


# ── Session-start gate (RFC §4.4) ─────────────────────────────────────────────
#
# ``_ColdStartAdmission`` above bounds runtime spawn + initialize. This bounds
# the OTHER expensive start: ``session/new`` on an already-running runtime,
# which blocks while kiro-cli initializes the session's MCP servers. Under a
# burst of subagent starts every session/new competes for the same process,
# each one gets slower, and the 90s budget is hit by requests that would have
# completed in isolation -- a timeout that says nothing about the runtime's
# health. The gate keeps at most ``agent.session_start_concurrency`` (default 2)
# session/new requests outstanding per event loop; waiters queue in FIFO order.
# It is a FIXED semaphore on purpose: the adaptive loop lives in the gatewayd
# spawn gate and the execution-cap controller, and two adapting loops on one
# resource oscillate. The same gate serves every harness (kiro-cli, KAS, a
# later Claude host): it wraps ``create_session``, which every backend's
# session start runs through.
_SESSION_START_CONCURRENCY_DEFAULT = 2
_SESSION_START_CONCURRENCY_FLOOR = 1

# How many of the gate's permits are RESERVED for a ``session/new`` that has not
# gone out yet, expressed as a shortfall from the limit: a
# :class:`StartCollector` may hold at most ``limit - this`` permits.
#
# Without the reservation the gate starves. A timed-out start does not release
# its permit -- it hands it to a collector that keeps it for
# ``agent.start_collect_timeout_secs`` (default 300 s) -- so at the default
# limit of 2, two slow starts park BOTH permits for five minutes and every
# session/new on the whole gateway queues behind them, including the retries
# those failures produce, whose own timeouts create more collectors. Observed on
# an operator host: one member slot spent 15 consecutive auto-nudge cycles on
# ``session/new timed out after 90s (0/10 MCP server(s) reported)`` while no new
# agent process was ever spawned -- every attempt was waiting in this queue.
#
# Reserving one permit bounds what the collecting population can claim instead
# of letting it become the whole gate. A collector denied the hand-off still
# runs and still owns its request (the session it may yet receive is still
# adopted or torn down); it simply does not hold back-pressure it cannot
# release. At ``limit == 1`` the ceiling is 0, so no collector holds a permit --
# which is the only reading of "always keep one free" that a single-permit gate
# admits.
_COLLECTOR_PERMIT_HEADROOM = 1


def _resolve_session_start_concurrency() -> int:
    """Snapshot ``agent.session_start_concurrency`` from config (off-loop caller)."""
    try:
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        return max(_SESSION_START_CONCURRENCY_FLOOR, int(cfg.agent.session_start_concurrency))
    except Exception:
        logger.debug("session_start_concurrency unreadable -- using default", exc_info=True)
        return _SESSION_START_CONCURRENCY_DEFAULT


def _record_session_start(start_t0: float, *, ok: bool, attributable_timeout: bool = False) -> None:
    """Feed one ``session/new`` outcome to the adaptive controller, when one runs.

    The controller is process-wide (``adaptive.controller.current``); without
    one this is a no-op. ``key`` groups the samples by the start kind so a slow
    ACP handshake reads apart from a slow MCP backend spawn.
    """
    from kiro_crew.acp.runtime import time

    try:
        from kiro_crew.adaptive.controller import current as _current_controller

        controller = _current_controller()
        if controller is None:
            return
        controller.record_start(
            (time.monotonic() - start_t0) * 1000.0,
            ok=ok,
            attributable_timeout=attributable_timeout,
            key="acp:session/new",
        )
    except Exception:
        logger.debug("session start sample not recorded", exc_info=True)


class SessionStartGate:
    """Loop-affine FIFO semaphore around ``session/new``.

    ``acquire()`` returns a :class:`StartPermit` carrying the queue wait in
    milliseconds so the caller can set the run's start clock at gate EXIT --
    time spent waiting here is queue time and must not count against the
    session-start budget or the startup watchdog. ``StartPermit.release()`` is
    idempotent, which is what makes "released exactly once on every path"
    checkable: ``releases`` counts real releases.
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(_SESSION_START_CONCURRENCY_FLOOR, int(limit))
        self._semaphore = asyncio.Semaphore(self.limit)
        self.active = 0
        self.queued = 0
        self.releases = 0
        # Permits currently held by a StartCollector rather than by a live
        # ``session/new``. Bounded by ``collector_hold_ceiling`` so a fresh start
        # always has somewhere to go -- see _COLLECTOR_PERMIT_HEADROOM.
        self.collector_holds = 0

    @property
    def collector_hold_ceiling(self) -> int:
        """How many permits :class:`StartCollector` instances may hold at once."""
        return max(0, self.limit - _COLLECTOR_PERMIT_HEADROOM)

    async def acquire(self) -> "StartPermit":
        from kiro_crew.acp.runtime import time

        started = time.monotonic()
        self.queued += 1
        try:
            await self._semaphore.acquire()
        finally:
            self.queued -= 1
        self.active += 1
        return StartPermit(self, (time.monotonic() - started) * 1000.0)

    def _reserve_collector_hold(self) -> bool:
        """Claim one collector hold, or refuse when the ceiling is reached."""
        if self.collector_holds >= self.collector_hold_ceiling:
            return False
        self.collector_holds += 1
        return True

    def _release(self, *, collector_held: bool = False) -> None:
        if collector_held:
            self.collector_holds = max(0, self.collector_holds - 1)
        self.active = max(0, self.active - 1)
        self.releases += 1
        self._semaphore.release()


class StartPermit:
    """One acquired gate slot; ``release()`` is a no-op after the first call."""

    def __init__(self, gate: SessionStartGate, queue_wait_ms: float) -> None:
        self._gate = gate
        self.queue_wait_ms = queue_wait_ms
        self.released = False
        # True once a StartCollector owns this permit for the rest of its life,
        # which is what the gate counts against ``collector_hold_ceiling``.
        self.collector_held = False

    def hold_for_collector(self) -> bool:
        """Let a :class:`StartCollector` keep this permit, if the gate allows it.

        False when the permit is already released or already collector-held, or
        when collectors hold the gate's whole collector budget. The caller then
        releases the permit itself and gives the collector none: the collector is
        still created and still owns its request, but a start that has not gone
        out yet is never made to queue behind one that already gave up.
        """
        if self.released or self.collector_held:
            return False
        if not self._gate._reserve_collector_hold():
            return False
        self.collector_held = True
        return True

    def release(self) -> bool:
        if self.released:
            return False
        self.released = True
        self._gate._release(collector_held=self.collector_held)
        return True


_session_start_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, SessionStartGate] = (
    weakref.WeakKeyDictionary()
)
_session_start_gates_lock = threading.Lock()


async def session_start_gate() -> SessionStartGate:
    """The current loop's gate, sized from config the first time it is asked for.

    Config is resolved off-loop (``KiroCrewConfig.load`` is disk I/O) unless the
    live watcher's snapshot is armed. The size is fixed for the loop's lifetime;
    ``agent.session_start_concurrency`` is ``restart=True``. The gate is a
    strong value keyed weakly by loop, so it lives exactly as long as its loop.
    """
    loop = asyncio.get_running_loop()
    with _session_start_gates_lock:
        gate = _session_start_gates.get(loop)
    if gate is not None:
        return gate
    snap = live.snapshot()
    limit: int | None = None
    if snap is not None:
        try:
            limit = int(snap.agent.session_start_concurrency)
        except Exception:
            limit = None
    if limit is None:
        limit = await asyncio.to_thread(_resolve_session_start_concurrency)
    with _session_start_gates_lock:
        gate = _session_start_gates.get(loop)
        if gate is None:
            gate = SessionStartGate(limit)
            _session_start_gates[loop] = gate
    return gate


def session_start_gate_counts() -> tuple[int, int]:
    """``(active, queued)`` for the current loop's gate; ``(0, 0)`` when none exists."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return (0, 0)
    with _session_start_gates_lock:
        gate = _session_start_gates.get(loop)
    return (gate.active, gate.queued) if gate is not None else (0, 0)


# Bounds every init-frame holder: ``_split_init_frames`` and ``StartCollector`` below,
# and ``AcpRuntime``'s staging deque. A frame is staged only while the session
# id that would claim it is still unknown, so one that nobody ever claims must
# not accumulate for the runtime's life.
_INIT_NOTIFICATION_BUFFER_LIMIT = 100


def _split_init_frames(
    staged: "deque[JsonRpcMessage]", session_id: str
) -> tuple[list[JsonRpcMessage], "deque[JsonRpcMessage]"]:
    """Partition staged init frames into *session_id*'s and everyone else's.

    An empty *session_id* claims NOTHING: a start whose request never answered
    has no id to match on, and matching everything there would hand it a
    concurrent start's registrations.
    """
    matched: list[JsonRpcMessage] = []
    retained: deque[JsonRpcMessage] = deque(maxlen=_INIT_NOTIFICATION_BUFFER_LIMIT)
    for msg in staged:
        params = msg.params if isinstance(msg.params, dict) else {}
        if session_id and str(params.get("sessionId") or "") == session_id:
            matched.append(msg)
        else:
            retained.append(msg)
    return matched, retained


StartAdopter = Callable[[str, dict[str, Any]], Awaitable[bool]]

START_OUTCOME_ADOPTED = "adopted"
START_OUTCOME_TORN_DOWN = "torn_down"
START_OUTCOME_ABANDONED = "abandoned"
START_OUTCOME_RUNTIME_DEAD = "runtime_dead"
START_OUTCOME_ERROR = "error"


class StartCollector:
    """Owns a ``session/new`` whose answer outlived the caller's budget.

    Created by :meth:`AcpRuntime.create_session` on timeout. Holds the adopted
    request future and the gate permit, and waits up to ``timeout``
    (``agent.start_collect_timeout_secs``) for the answer:

    * late result + an adopter registered -> the adopter decides; ``True``
      means the session continues under its original owner (``adopted``);
    * late result, no adopter (or the adopter declined) -> the session is torn
      down through the runtime's normal per-session teardown (``torn_down``),
      never by killing the shared runtime;
    * the runtime dies -> nothing to tear down (``runtime_dead``);
    * the cleanup deadline passes -> the request is dropped (``abandoned``).

    Whichever path settles it releases the gate permit exactly once and
    unregisters the collector. ``settled`` is an ``asyncio.Event`` for callers
    that want to wait for the verdict.

    It also holds the MCP-init frames its session may still send, because those
    frames name a session id nobody can claim until this request answers -- see
    :meth:`stage_init_frame`.
    """

    def __init__(
        self,
        runtime: "AcpRuntime",
        req_id: int,
        future: "asyncio.Future[dict[str, Any]]",
        *,
        permit: "StartPermit | None",
        timeout: float,
        context: dict[str, Any] | None = None,
        memory_mode: str = "persistent",
    ) -> None:
        from kiro_crew.acp.runtime import time

        self._runtime = runtime
        self.req_id = req_id
        self._future = future
        self._permit = permit
        self.timeout = float(timeout)
        self.context = dict(context or {})
        self.memory_mode = memory_mode
        self._adopter: StartAdopter | None = None
        self.outcome: str | None = None
        self.session_id: str = ""
        self.settled = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.created_at = time.monotonic()
        self._staged_init: deque[JsonRpcMessage] = deque(maxlen=_INIT_NOTIFICATION_BUFFER_LIMIT)

    def start(self) -> "StartCollector":
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())
        return self

    @property
    def is_settled(self) -> bool:
        return self.settled.is_set()

    def adopt(self, adopter: StartAdopter) -> bool:
        """Register who takes the session if it arrives late; False once settled."""
        if self.is_settled:
            return False
        self._adopter = adopter
        return True

    def gate_released(self) -> bool:
        return self._permit is None or self._permit.released

    def seed_init_frames(self, staged: "deque[JsonRpcMessage]") -> None:
        """Copy the frames the timed-out start already staged into this collector.

        A copy, not a move: a CONCURRENT start's frames sit in the same runtime
        deque and only the id inside a frame says whose it is, so both holders
        keep every candidate and each claims by id (:meth:`take_init_frames`).
        """
        self._staged_init.extend(staged)

    def stage_init_frame(self, msg: JsonRpcMessage) -> None:
        """Hold one MCP-init frame that may belong to this start's late session.

        Held HERE rather than in the runtime's own staging deque because
        ``_mcp_init_progress`` reads that one un-keyed, by server NAME: a frame
        left there past its own init scope would be reported as the next
        session-start timeout's progress, which is the one diagnostic that has to
        stay attributable.
        """
        self._staged_init.append(msg)

    def take_init_frames(self, session_id: str) -> list[JsonRpcMessage]:
        """Take the staged frames that name *session_id*, leaving the rest."""
        matched, self._staged_init = _split_init_frames(self._staged_init, session_id)
        return matched

    def drop_init_frames(self) -> None:
        """Forget the staged frames: no claimant is left."""
        self._staged_init.clear()

    async def _run(self) -> None:
        from kiro_crew.acp.runtime import AcpRuntimeDead, AcpSessionHandle, time

        outcome = START_OUTCOME_ERROR
        try:
            try:
                resp = await asyncio.wait_for(asyncio.shield(self._future), timeout=self.timeout)
            except asyncio.TimeoutError:
                self._runtime._pending_requests.pop(self.req_id, None)
                if not self._future.done():
                    self._future.cancel()
                # A dead runtime is dead whichever clock fired first. ``_mark_dead``
                # resolves this future with ``AcpRuntimeDead``, so the arm below
                # names the death only when that resolution WON the race against
                # this timeout -- and on a slow host it loses, which would report
                # the one outcome an operator can act on ("the process is gone")
                # as the one they cannot ("it never answered"). The runtime's own
                # flag is not a clock, so read that instead of ordering two.
                if self._runtime._dead:
                    outcome = START_OUTCOME_RUNTIME_DEAD
                    logger.warning(
                        "start collector: session/new req_id=%d unanswered after %gs and the "
                        "runtime is dead; settling as runtime_dead",
                        self.req_id,
                        self.timeout,
                    )
                    return
                outcome = START_OUTCOME_ABANDONED
                logger.warning(
                    "start collector: session/new req_id=%d never answered within %gs; "
                    "attempt abandoned",
                    self.req_id,
                    self.timeout,
                )
                return
            except AcpRuntimeDead:
                outcome = START_OUTCOME_RUNTIME_DEAD
                return
            except Exception:
                logger.debug(
                    "start collector: session/new req_id=%d failed late",
                    self.req_id,
                    exc_info=True,
                )
                return
            session_id = str((resp or {}).get("sessionId") or "")
            self.session_id = session_id
            if not session_id:
                return
            adopted = False
            if self._adopter is not None:
                try:
                    adopted = bool(await self._adopter(session_id, resp))
                except Exception:
                    logger.warning(
                        "start collector: adopter for late session %s raised; tearing down",
                        session_id,
                        exc_info=True,
                    )
                    adopted = False
            if adopted:
                outcome = START_OUTCOME_ADOPTED
                return
            try:
                await self._runtime._teardown_late_session(session_id)
            finally:
                if self.memory_mode != "persistent":
                    await asyncio.to_thread(AcpSessionHandle.cleanup_transcript_files, session_id)
            outcome = START_OUTCOME_TORN_DOWN
        finally:
            self.outcome = outcome
            # Settled on EVERY outcome: an adopted session already took its own
            # frames, and on any other outcome nothing will ever claim them.
            # Holding them would keep one attempt's registrations alive for the
            # runtime's life.
            self.drop_init_frames()
            released = self._permit.release() if self._permit is not None else False
            self._runtime._start_collectors.pop(self.req_id, None)
            logger.info(
                "start collector settled: req_id=%d outcome=%s session=%s gate_released=%s "
                "after %.1fs",
                self.req_id,
                outcome,
                self.session_id or "-",
                released,
                time.monotonic() - self.created_at,
            )
            self.settled.set()


# Session start (session/new, session/load) gets its own budget because kiro-cli
# blocks the response while it initializes the session's MCP servers, and a
# remote server pending OAuth holds that initialization for its FULL 30s
# authorization wait. _REQUEST_TIMEOUT is also 30s, so sharing it turns session
# start into a race the client usually loses: kiro-cli creates the session, the
# client gives up a beat earlier, and the slot dies. This must stay comfortably
# ABOVE the backend's 30s OAuth wait plus the initialization tail that follows
# it (observed: remaining servers register within ~1s after the wait; a
# 71-server agent with no pending OAuth completes in ~14s) — do NOT "tidy" it
# back down to _REQUEST_TIMEOUT.
# This is the built-in default AND floor; ``agent.session_start_timeout_secs``
# raises it for agents whose MCP fleet legitimately needs longer (see
# _resolve_session_start_timeout below).
_SESSION_NEW_TIMEOUT = 90.0


#: ``agent.start_collect_timeout_secs`` built-in default: how long a
#: StartCollector keeps a timed-out session/new before abandoning the attempt.
_START_COLLECT_TIMEOUT_DEFAULT = 300.0


def _resolve_start_collect_timeout() -> float:
    """Snapshot ``agent.start_collect_timeout_secs`` from config (off-loop caller)."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        return max(10.0, float(cfg.agent.start_collect_timeout_secs))
    except Exception:
        logger.debug("start_collect_timeout_secs unreadable -- using default", exc_info=True)
        return _START_COLLECT_TIMEOUT_DEFAULT


def _resolve_session_start_timeout() -> float:
    """Snapshot ``agent.session_start_timeout_secs`` from config.

    Function-level import (mirrors ``_load_watchdog_settings`` in
    session_handle.py) avoids the config -> dashboard -> acp import cycle;
    any failure falls back to the built-in default rather than breaking a
    runtime. The loader clamps the on-disk value to
    [SESSION_START_TIMEOUT_MIN, SESSION_START_TIMEOUT_MAX]; the ``max`` here
    is belt-and-braces so a degraded load can never shrink the budget below
    the built-in floor — a session-start budget under the backend's 30s OAuth
    wait recreates the race that floor exists to prevent.
    """
    try:
        # circular import: config.loader -> dashboard -> session -> acp
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        return max(_SESSION_NEW_TIMEOUT, float(cfg.agent.session_start_timeout_secs))
    except Exception:
        logger.debug("session-start timeout load failed — using default", exc_info=True)
        return _SESSION_NEW_TIMEOUT
