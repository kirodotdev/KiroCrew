"""Daemon-global admission around backend spawn + initialize.

Before this module the gateway daemon had no bound on how many backend
processes it would fork and hand an ``initialize`` at the same time. A pooled key
was deduplicated per key and capped as a RESIDENT count by ``max_backends``; a
private backend was bounded by nothing. A wide session fan-out therefore turned
into hundreds of concurrent ``create_subprocess_exec`` calls on one event loop,
every one of them reading the same interpreter tree off a saturated disk, and
the daemon's own liveness ping missed while it was doing so.

:class:`SpawnGate` is the one bound: a daemon-wide count of spawns in flight,
where "in flight" runs from just before the fork until the backend's first
``initialize`` has resolved (or provably will never arrive). Callers past the
count wait in FIFO order. It does not replace ``max_backends`` (resident
ceiling, still enforced by the pool) or the circuit breaker (per-key crash
loop); it sits INSIDE both -- acquired after the per-key spawn lock and after
``CircuitBreaker.allow`` -- so a permit is never held during a breaker cooldown
and no pool lock is held while queued.

Two things about a :class:`Permit` are deliberately separate:

* ``settle(outcome)`` records what the spawn told us about the host --
  ``success`` (initialize completed), ``failure`` (fork or initialize failed
  for a reason that looks like congestion) or ``neutral`` (nothing learned: the
  caller was cancelled, a prewarmed backend nobody initialised, a client that
  never sent ``initialize``). It is exactly-once: the first outcome sticks.
* ``release()`` frees the slot. Idempotent, and it settles ``neutral`` first
  if nothing was recorded, so a permit can never leak a slot or a verdict.

Capacity is FIXED in this module: ``set_capacity`` is the seam the adaptive
controller plugs into later, clamped to ``[floor, ceiling]``. Raising capacity
wakes queued waiters; lowering it lets in-flight spawns finish and simply admits
fewer afterwards. Outcomes are counted so the controller has its signal.

``close()`` is the drain hook: every queued waiter is failed with
:class:`SpawnGateClosed`, every initialize watcher is cancelled (releasing its
permit as neutral), and later ``acquire`` calls fail immediately. Cancellation
at any boundary is neutral: a caller cancelled after its grant but before it
resumed gives the slot straight back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .host_budget import HostBudget, HostCharge

logger = logging.getLogger(__name__)

#: Fixed capacity shipped by the gateway's config defaults; the adaptive
#: controller moves the live value between the floor and ceiling later.
DEFAULT_CAPACITY = 4
DEFAULT_FLOOR = 1
DEFAULT_CEILING = 8

#: How often a queued waiter's ``on_queued`` callback fires while it waits.
#: This is the ``queued`` keepalive cadence a new stub renews its silence
#: timer on; it must stay comfortably below the stub's 25 s silence window.
QUEUED_KEEPALIVE_SECS = 5.0

#: Permit outcomes. Strings rather than an Enum so they serialise into the
#: ``stats`` frame and the audit row unchanged.
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_NEUTRAL = "neutral"
_OUTCOMES = frozenset({OUTCOME_SUCCESS, OUTCOME_FAILURE, OUTCOME_NEUTRAL})

#: Extra time the initialize watcher grants beyond the backend's own deadline
#: once a handshake is known to be in flight: the backend's timer fires at the
#: deadline and sets the done event; this only has to outlast that.
_INIT_WATCH_GRACE_SECS = 1.0


class SpawnGateClosed(RuntimeError):
    """The gate was closed for drain; nothing will be admitted again."""


class SpawnGateTimeout(RuntimeError):
    """A waiter's own wait budget ran out before a slot came free.

    ``position`` is where the waiter stood (1-based) when it gave up, which is
    what the rejection frame reports back to the stub.
    """

    def __init__(self, position: int, capacity: int, waited_secs: float) -> None:
        self.position = position
        self.capacity = capacity
        self.waited_secs = waited_secs
        super().__init__(
            f"spawn gate wait budget exhausted after {waited_secs:.0f}s "
            f"(position {position}, capacity {capacity})"
        )


@dataclass(frozen=True)
class QueuePosition:
    """What a waiter is told each keepalive tick."""

    position: int
    capacity: int
    in_flight: int
    waited_secs: float

    def frame(self) -> dict[str, Any]:
        return {
            "type": "queued",
            "position": self.position,
            "capacity": self.capacity,
            "in_flight": self.in_flight,
            "waited_secs": round(self.waited_secs, 1),
        }


class Permit:
    """One admitted spawn. See the module docstring for settle vs release."""

    __slots__ = ("_gate", "label", "granted_at", "_outcome", "_released")

    def __init__(self, gate: "SpawnGate", label: str, granted_at: float) -> None:
        self._gate = gate
        self.label = label
        self.granted_at = granted_at
        self._outcome: Optional[str] = None
        self._released = False

    @property
    def outcome(self) -> Optional[str]:
        return self._outcome

    @property
    def released(self) -> bool:
        return self._released

    def settle(self, outcome: str) -> None:
        """Record the outcome exactly once. A second call is ignored."""
        if outcome not in _OUTCOMES:
            raise ValueError(f"unknown permit outcome {outcome!r}")
        if self._outcome is not None:
            return
        self._outcome = outcome
        self._gate._note_outcome(outcome)

    def release(self) -> None:
        """Free the slot. Idempotent; settles ``neutral`` if nothing was recorded."""
        if self._released:
            return
        if self._outcome is None:
            self.settle(OUTCOME_NEUTRAL)
        self._released = True
        self._gate._release_slot()


class _Waiter:
    __slots__ = ("future", "label", "enqueued_at")

    def __init__(self, future: "asyncio.Future[None]", label: str, enqueued_at: float) -> None:
        self.future = future
        self.label = label
        self.enqueued_at = enqueued_at


OnQueued = Callable[[QueuePosition], Awaitable[None]]
OnSettle = Callable[[str], None]


class SpawnGate:
    """FIFO admission with a movable fixed capacity. Single event loop."""

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        *,
        floor: int = DEFAULT_FLOOR,
        ceiling: int = DEFAULT_CEILING,
        clock: Callable[[], float] = time.monotonic,
        on_settle: Optional[OnSettle] = None,
    ) -> None:
        if floor < 1:
            raise ValueError(f"floor must be >= 1, got {floor}")
        if ceiling < floor:
            raise ValueError(f"ceiling {ceiling} must be >= floor {floor}")
        self._floor = floor
        self._ceiling = ceiling
        self._capacity = min(max(int(capacity), floor), ceiling)
        self._clock = clock
        self._on_settle = on_settle
        self._in_flight = 0
        self._waiters: deque[_Waiter] = deque()
        self._closed = False
        self._watchers: set[asyncio.Task[None]] = set()
        self._outcomes: dict[str, int] = {o: 0 for o in _OUTCOMES}
        self._granted = 0
        self._timeouts = 0
        self._cancelled = 0

    # -- capacity ------------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def floor(self) -> int:
        return self._floor

    @property
    def ceiling(self) -> int:
        return self._ceiling

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def queued(self) -> int:
        return sum(1 for w in self._waiters if not w.future.done())

    @property
    def closed(self) -> bool:
        return self._closed

    def set_capacity(self, capacity: int) -> int:
        """Move the live capacity, clamped to ``[floor, ceiling]``. Returns it.

        A raise admits queued waiters immediately; a cut takes effect as the
        spawns already in flight release. Nothing in flight is ever revoked.
        """
        new = min(max(int(capacity), self._floor), self._ceiling)
        if new != self._capacity:
            logger.info("spawn gate capacity %d -> %d", self._capacity, new)
        self._capacity = new
        self._wake()
        return new

    # -- acquire / release ---------------------------------------------------

    async def acquire(
        self,
        *,
        label: str,
        deadline: Optional[float] = None,
        on_queued: Optional[OnQueued] = None,
        keepalive_secs: float = QUEUED_KEEPALIVE_SECS,
    ) -> Permit:
        """Wait for a slot in FIFO order and return its :class:`Permit`.

        ``deadline`` is an absolute time on this gate's clock; ``None`` waits
        until admitted or closed. ``on_queued`` runs every ``keepalive_secs``
        while queued and receives the waiter's current position; an exception
        from it abandons the wait (the caller's connection is what usually fails
        there, and there is nobody left to admit).
        """
        if self._closed:
            raise SpawnGateClosed("spawn gate closed")
        now = self._clock()
        if not self._waiters and self._in_flight < self._capacity:
            self._in_flight += 1
            self._granted += 1
            return Permit(self, label, now)

        loop = asyncio.get_running_loop()
        waiter = _Waiter(loop.create_future(), label, now)
        self._waiters.append(waiter)
        try:
            while True:
                now = self._clock()
                remaining: Optional[float] = None
                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        self._timeouts += 1
                        raise SpawnGateTimeout(
                            self._position_of(waiter), self._capacity, now - waiter.enqueued_at
                        )
                tick = keepalive_secs if remaining is None else min(keepalive_secs, remaining)
                try:
                    # ``shield`` keeps a timeout from cancelling the grant
                    # future itself: a grant that lands during the timeout
                    # window is observed on the next loop iteration (or handed
                    # back in the except below), never lost.
                    await asyncio.wait_for(asyncio.shield(waiter.future), timeout=tick)
                    break
                except asyncio.TimeoutError:
                    if waiter.future.done():
                        break
                    if on_queued is not None:
                        await on_queued(self._queue_position(waiter))
        except BaseException:
            if (
                waiter.future.done()
                and not waiter.future.cancelled()
                and waiter.future.exception() is None
            ):
                # Granted while we were leaving: the slot was counted for us,
                # so hand it straight back and wake whoever is next.
                self._cancelled += 1
                self._release_slot()
            else:
                self._cancelled += 1
                self._drop_waiter(waiter)
            raise
        # The grant future may carry SpawnGateClosed.
        waiter.future.result()
        self._granted += 1
        return Permit(self, label, self._clock())

    def _queue_position(self, waiter: _Waiter) -> QueuePosition:
        return QueuePosition(
            position=self._position_of(waiter),
            capacity=self._capacity,
            in_flight=self._in_flight,
            waited_secs=self._clock() - waiter.enqueued_at,
        )

    def _position_of(self, waiter: _Waiter) -> int:
        position = 0
        for w in self._waiters:
            if w.future.done():
                continue
            position += 1
            if w is waiter:
                return position
        return max(position, 1)

    def _drop_waiter(self, waiter: _Waiter) -> None:
        with contextlib.suppress(ValueError):
            self._waiters.remove(waiter)
        if not waiter.future.done():
            waiter.future.cancel()

    def _release_slot(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        self._wake()

    def _wake(self) -> None:
        if self._closed:
            return
        while self._waiters and self._in_flight < self._capacity:
            waiter = self._waiters.popleft()
            if waiter.future.done():
                continue
            self._in_flight += 1
            waiter.future.set_result(None)

    def _note_outcome(self, outcome: str) -> None:
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1
        if self._on_settle is not None:
            try:
                self._on_settle(outcome)
            except Exception:  # pragma: no cover -- a controller bug must not leak a permit
                logger.exception("spawn gate on_settle hook failed")

    # -- initialize watcher --------------------------------------------------

    def watch_initialize(
        self,
        permit: Permit,
        *,
        init_done: asyncio.Event,
        init_state: Callable[[], str],
        process_exited: Callable[[], Awaitable[Any]],
        timeout: float,
    ) -> asyncio.Task[None]:
        """Hold ``permit`` through the backend's first ``initialize``.

        ``ready`` is sent to the stub BEFORE the handshake is forwarded (the
        stub forwards kiro-cli's first frame), so the spawn path cannot await
        the handshake inline. This detached task does it: it waits ``timeout``
        for ``init_done``; if the handshake is still in flight at that point the
        backend's own deadline is about to fire, so it waits one more window.
        Outcomes: ``ready`` settles ``success``; a failed handshake settles
        ``failure`` and, because a failed backend is being reaped and may
        survive SIGKILL, holds the permit until ``process_exited`` returns or
        one more ``timeout`` passes; no handshake at all settles ``neutral``
        (an unused prewarm or a client that never initialised is not
        congestion). The permit is released on every exit, cancellation
        included.
        """
        task = asyncio.create_task(
            self._watch_initialize(permit, init_done, init_state, process_exited, timeout),
            name=f"mcp-gateway-spawn-gate-watch-{permit.label[:24]}",
        )
        self._watchers.add(task)
        # A task cancelled before its first step never reaches its ``finally``
        # (a drain racing the create_task above), so the release is also a
        # done-callback; ``release`` is idempotent, so the common path pays
        # nothing for it.
        task.add_done_callback(lambda _t: permit.release())
        task.add_done_callback(self._watchers.discard)
        return task

    async def _watch_initialize(
        self,
        permit: Permit,
        init_done: asyncio.Event,
        init_state: Callable[[], str],
        process_exited: Callable[[], Awaitable[Any]],
        timeout: float,
    ) -> None:
        try:
            done = await self._wait_event(init_done, timeout)
            if not done and init_state() == "in_flight":
                done = await self._wait_event(init_done, timeout + _INIT_WATCH_GRACE_SECS)
            state = init_state()
            if done and state == "ready":
                permit.settle(OUTCOME_SUCCESS)
                return
            if done and state == "failed":
                permit.settle(OUTCOME_FAILURE)
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(process_exited(), timeout=timeout)
                return
            permit.settle(OUTCOME_NEUTRAL)
        finally:
            permit.release()

    @staticmethod
    async def _wait_event(event: asyncio.Event, timeout: float) -> bool:
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    # -- drain ---------------------------------------------------------------

    async def close(self) -> None:
        """Refuse new admissions, fail every waiter, release every watcher."""
        self._closed = True
        waiters = list(self._waiters)
        self._waiters.clear()
        for waiter in waiters:
            if not waiter.future.done():
                waiter.future.set_exception(SpawnGateClosed("spawn gate closed for drain"))
        watchers = list(self._watchers)
        for task in watchers:
            task.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
        self._watchers.clear()

    # -- observability -------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "capacity": self._capacity,
            "floor": self._floor,
            "ceiling": self._ceiling,
            "in_flight": self._in_flight,
            "queued": self.queued,
            "watchers": len(self._watchers),
            "granted": self._granted,
            "timeouts": self._timeouts,
            "cancelled": self._cancelled,
            "outcomes": dict(self._outcomes),
            "closed": self._closed,
        }


class Admission:
    """The daemon's admission state, built once in ``run_gatewayd``.

    Bundles the host budget, the spawn gate and the initialize deadline every
    backend is spawned with, plus the reap watchers that release a host charge
    once its process is really gone. ``None`` in place of this object means
    "no admission" -- the shape unit tests that drive ``_acquire_backend``
    directly still use.
    """

    def __init__(
        self,
        *,
        gate: SpawnGate,
        budget: HostBudget,
        initialize_timeout_secs: float,
        spawn_queue_wait_secs: float,
    ) -> None:
        self.gate = gate
        self.budget = budget
        self.initialize_timeout_secs = float(initialize_timeout_secs)
        self.spawn_queue_wait_secs = float(spawn_queue_wait_secs)
        self._reapers: set[asyncio.Task[None]] = set()

    def track_process(
        self, charge: HostCharge, process_exited: Callable[[], Awaitable[Any]]
    ) -> None:
        """Release ``charge`` when ``process_exited`` returns -- and not before.

        ``Backend.shutdown`` can return with the process still alive after a
        SIGKILL; ``process.wait()`` cannot, so that is what the charge follows.
        """

        async def _reap() -> None:
            try:
                await process_exited()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover -- wait() on a dead child never raises
                logger.debug("host budget reap watcher failed", exc_info=True)
            finally:
                charge.release()

        task = asyncio.create_task(
            _reap(), name=f"mcp-gateway-host-budget-reap-{charge.label[:24]}"
        )
        self._reapers.add(task)
        # Same reasoning as the initialize watcher: a cancel before the first
        # step skips ``finally``, and a charge must never outlive its reaper.
        task.add_done_callback(lambda _t: charge.release())
        task.add_done_callback(self._reapers.discard)

    async def close(self) -> None:
        """Drain: close the gate, cancel reap watchers, drop every charge."""
        await self.gate.close()
        reapers = list(self._reapers)
        for task in reapers:
            task.cancel()
        if reapers:
            await asyncio.gather(*reapers, return_exceptions=True)
        self._reapers.clear()
        self.budget.release_all()

    def snapshot(self) -> dict[str, Any]:
        return {
            "spawn_gate": self.gate.snapshot(),
            "host_budget": self.budget.snapshot(),
            "initialize_timeout_secs": self.initialize_timeout_secs,
            "spawn_queue_wait_secs": self.spawn_queue_wait_secs,
        }
