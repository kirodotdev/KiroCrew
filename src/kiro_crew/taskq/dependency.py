"""Dependency signals and the per-scope retry coordinator.

An external dependency -- the GitHub API, an HTTP host, a model provider -- can
refuse work for a while. Without a coordinator every entry point notices that on its own and
run its own retry loop, so five sessions hitting the same GitHub rate limit
produced five independent backoff timers and, when the limit lifted, five
simultaneous retries. This module replaces that with two things:

* :class:`DependencySignal` -- ONE shape every adapter translates a service
  error into. :func:`classify_exception` walks the registered adapters (GitHub,
  generic HTTP, the ACP/provider stream) and returns the first match.
* :class:`DependencyCoordinator` -- ONE retry schedule per ``dependency_scope``.
  It honours a server-supplied ``retry_at`` exactly, otherwise retries on the
  shared recovery ladder's schedule (``recovery/policy.py``: capped exponential
  backoff, equal jitter, ``agent.recovery_backoff_*``); it bounds the wait (attempts and a
  wall-clock deadline) so nothing retries forever; and when the scope's
  ``retry_at`` arrives it wakes waiters BY CAPACITY through admission -- one
  probe first, then ``wake_per_tick`` per ``wake_spacing_secs`` -- so a
  recovered dependency never replays every waiter at once. Scopes are
  independent: one outage never delays another scope's wake.

The coordinator keeps its schedule in memory and persists every entry and exit
as ``task_events`` (``dependency_wait`` / ``dependency_wake`` /
``dependency_failed``), so :meth:`DependencyCoordinator.rebuild` reconstructs
the schedule after a gateway restart from the rows alone.

Two threads reach it, which is why the schedule lock and the store writes are
kept apart (see :class:`DependencyCoordinator`): :meth:`report` and :meth:`tick`
run on the task store's writer thread, while the gateway loop reads and edits
the same schedule (``forget``, ``recovered``, ``waiters``, ``next_deadline``,
``shared_retry_at``).

Specification: ``docs/system-specs/modules/taskq.md`` (Dependency waits).
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from kiro_crew.recovery.policy import LayerPolicy, RecoveryPolicy

from .model import (
    FAILED,
    QUEUED,
    RETRY_WAIT,
    RUNNING,
    WAITING_DEPENDENCY,
    WAITING_INPUT,
    TaskRecord,
)
from .store import TaskStore, TaskStoreUnavailable
from .waits import EVIDENCE_DEPENDENCY_ADAPTER, WaitLedger, WaitRecord

logger = logging.getLogger(__name__)

# ── signal taxonomy ───────────────────────────────────────────────────────────

#: The dependency is down or unreachable (5xx, connection refused, DNS).
KIND_DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
#: The dependency is up but asked us to slow down (429, abuse detection).
KIND_RATE_LIMITED = "rate_limited"
#: Credentials rejected or expired. Terminal: no retry can sign us in.
KIND_AUTH_FAILED = "auth_failed"
#: The request itself is wrong (404, 422, malformed). Terminal: identical
#: retries are rejected identically.
KIND_PERMANENT_PARAM_ERROR = "permanent_param_error"
#: Too many concurrent requests from us (a concurrency ceiling, not a rate).
KIND_CONCURRENCY_EXCEEDED = "concurrency_exceeded"
#: An allowance is spent until it resets (monthly limit, quota). Retryable
#: only when the dependency told us WHEN it resets.
KIND_QUOTA_EXHAUSTED = "quota_exhausted"

SIGNAL_KINDS: frozenset[str] = frozenset(
    {
        KIND_DEPENDENCY_UNAVAILABLE,
        KIND_RATE_LIMITED,
        KIND_AUTH_FAILED,
        KIND_PERMANENT_PARAM_ERROR,
        KIND_CONCURRENCY_EXCEEDED,
        KIND_QUOTA_EXHAUSTED,
    }
)

#: Kinds that are never retried by the coordinator, whatever the adapter said.
TERMINAL_KINDS: frozenset[str] = frozenset({KIND_AUTH_FAILED, KIND_PERMANENT_PARAM_ERROR})

#: ``task_events`` kinds this module writes.
EVENT_WAIT = "dependency_wait"
EVENT_WAKE = "dependency_wake"
EVENT_FAILED = "dependency_failed"

#: What one durable write did. ``COMMITTED`` landed; ``REFUSED`` means the store
#: is fine and said no (a fenced generation, a missing row, a state the
#: transition table does not admit); ``UNAVAILABLE`` means nothing landed and the
#: same call may land later. The last two are kept apart because a boolean
#: cannot tell them apart and they need OPPOSITE answers: a refusal is another
#: owner's decision to respect, an outage is this waiter's place to keep.
WRITE_COMMITTED = "committed"
WRITE_REFUSED = "refused"
WRITE_UNAVAILABLE = "unavailable"

#: :attr:`Verdict.outcome` values. ``UNPERSISTED`` is the one a caller must not
#: park on: the schedule holds a retry instant but NO row records the wait, so
#: neither :meth:`DependencyCoordinator.rebuild` nor a boot sweep can find it.
OUTCOME_WAIT = "wait"
OUTCOME_TERMINAL = "terminal"
OUTCOME_DEADLINE = "deadline"
OUTCOME_UNPERSISTED = "unpersisted"

#: Where a retryable waiter is parked. A LIVE run (row ``running``) enters
#: ``waiting_dependency`` with a ``WaitRecord`` and keeps its runtime resident;
#: a row that is not running yet (``starting``) is parked in ``retry_wait``
#: instead, where the dispatcher re-claims it once the coordinator wakes it.
WAIT_STATE: str = WAITING_DEPENDENCY
PARK_STATE: str = RETRY_WAIT
#: Where an ``auth_failed`` waiter goes: the user must sign in, which is real
#: input; a row that cannot enter that wait (not running) is ``failed``.
AUTH_STATE: str = WAITING_INPUT

# Defaults mirror ``agent.dependency_*`` in ``config/sections.py``.
DEFAULT_MAX_ATTEMPTS = 20


def dependency_backoff(policy: RecoveryPolicy | None = None) -> LayerPolicy:
    """The retry schedule a scope without a server ``retry_at`` follows.

    The shared recovery schedule (``agent.recovery_backoff_*``: capped
    exponential backoff with equal jitter) with no layer specialisation -- a
    dependency probe retries on the ladder's clock, never on a schedule of its
    own. ``None`` takes the ladder's defaults.
    """
    base = policy if policy is not None else RecoveryPolicy()
    return LayerPolicy(
        layer="dependency",
        trigger="dependency scope retry with no server retry_at",
        max_attempts=1,
        base_secs=base.base_secs,
        max_secs=base.max_secs,
        jitter=base.jitter,
    )


#: Scopes whose retry budget is the WALL-CLOCK deadline alone, never the probe
#: count: an outage of our own infrastructure (the MCP gateway daemon, its
#: spawn gate) is survived for ``dependency_wait_deadline_secs``, one probe per
#: backoff step for the whole scope. Twenty probes at a 2 s cadence would fail
#: every waiter a minute into a twenty-minute outage the deadline was sized
#: for; a remote provider's budget stays probe-counted because its failures
#: are the provider's answer, not our own outage.
INFRA_SCOPE_PREFIXES: tuple[str, ...] = ("mcp_gateway:",)


def is_infra_scope(scope: str) -> bool:
    """Whether *scope* is one of our own infrastructure scopes (deadline-bounded)."""
    return str(scope or "").startswith(INFRA_SCOPE_PREFIXES)


DEFAULT_WAIT_DEADLINE_SECS = 3600.0
DEFAULT_WAKE_PER_TICK = 0  # 0 = the current effective admission capacity
DEFAULT_WAKE_SPACING_SECS = 1.0


@dataclass(frozen=True)
class DependencySignal:
    """One dependency error, in the vocabulary the coordinator schedules on.

    ``retry_at`` is an absolute epoch second when the dependency named the
    moment it will accept work again (``Retry-After``, ``X-RateLimit-Reset``);
    ``None`` means "back off". ``retryable`` is forced ``False`` for the
    terminal kinds and for a quota exhaustion without a known reset.
    ``dependency_scope`` names the shared budget: every task that reports the
    same scope waits on the same schedule. ``source`` names the adapter.
    """

    kind: str
    dependency_scope: str
    source: str
    retry_at: float | None = None
    retryable: bool = True
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in SIGNAL_KINDS:
            raise ValueError(f"unknown dependency signal kind {self.kind!r}")
        if not self.dependency_scope:
            raise ValueError("a dependency signal needs a non-empty dependency_scope")
        if self.kind in TERMINAL_KINDS and self.retryable:
            object.__setattr__(self, "retryable", False)
        if self.kind == KIND_QUOTA_EXHAUSTED and self.retry_at is None and self.retryable:
            object.__setattr__(self, "retryable", False)
        if self.retry_at is not None:
            object.__setattr__(self, "retry_at", float(self.retry_at))
        if len(self.detail) > 500:
            object.__setattr__(self, "detail", self.detail[:500])

    @property
    def terminal(self) -> bool:
        return not self.retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "dependency_scope": self.dependency_scope,
            "source": self.source,
            "retry_at": self.retry_at,
            "retryable": self.retryable,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DependencySignal | None":
        try:
            retry_at = data.get("retry_at")
            return cls(
                kind=str(data["kind"]),
                dependency_scope=str(data["dependency_scope"]),
                source=str(data.get("source", "")),
                retry_at=float(retry_at) if retry_at is not None else None,
                retryable=bool(data.get("retryable", True)),
                detail=str(data.get("detail", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ── adapter registry ──────────────────────────────────────────────────────────

#: An adapter reads one exception (and the caller's scope hint) and answers a
#: signal when it recognises the shape, ``None`` otherwise.
Adapter = Callable[[BaseException, str], "DependencySignal | None"]

#: Attribute an adapter or seam may set on an exception to pre-classify it;
#: :func:`classify_exception` honours it before consulting the registry.
SIGNAL_ATTR = "dependency_signal"

_registry_lock = threading.Lock()
_adapters: list[tuple[str, Adapter]] = []
_builtin_adapters_loaded = False


def register_adapter(name: str, adapter: Adapter, *, first: bool = False) -> None:
    """Register *adapter* under *name*; a same-named adapter is replaced."""
    with _registry_lock:
        remaining = [(n, a) for n, a in _adapters if n != name]
        if first:
            remaining.insert(0, (name, adapter))
        else:
            remaining.append((name, adapter))
        _adapters[:] = remaining


def unregister_adapter(name: str) -> bool:
    with _registry_lock:
        before = len(_adapters)
        _adapters[:] = [(n, a) for n, a in _adapters if n != name]
        return len(_adapters) != before


def registered_adapters() -> list[str]:
    _ensure_builtin_adapters()
    with _registry_lock:
        return [n for n, _ in _adapters]


def _ensure_builtin_adapters() -> None:
    global _builtin_adapters_loaded
    if _builtin_adapters_loaded:
        return
    with _registry_lock:
        if _builtin_adapters_loaded:
            return
        _builtin_adapters_loaded = True
    # Imported here, not at module top: the adapters import this module.
    from . import adapters as _adapters_pkg

    _adapters_pkg.install()


def classify_exception(exc: BaseException, scope: str = "") -> DependencySignal | None:
    """Translate *exc* into a :class:`DependencySignal`, or ``None`` if no adapter knows it.

    A signal already attached to the exception (``exc.dependency_signal``) wins.
    Otherwise the adapters run in registration order and the first non-``None``
    answer is returned; an adapter that raises is skipped, never fatal. *scope*
    is the caller's scope hint (``"github:api"``); adapters that can derive a
    narrower scope from the error (a host, a provider id) may override it.
    """
    pre = getattr(exc, SIGNAL_ATTR, None)
    if isinstance(pre, DependencySignal):
        return pre
    _ensure_builtin_adapters()
    with _registry_lock:
        snapshot = list(_adapters)
    for name, adapter in snapshot:
        try:
            signal = adapter(exc, scope)
        except Exception:  # noqa: BLE001 - one broken adapter must not hide the rest
            logger.debug("dependency adapter %s raised", name, exc_info=True)
            continue
        if signal is not None:
            return signal
    return None


# ── coordinator ───────────────────────────────────────────────────────────────

PHASE_WAITING = "waiting"
PHASE_PROBE = "probe"
PHASE_RAMP = "ramp"


@dataclass
class ScopeSchedule:
    """The single retry schedule for one ``dependency_scope``."""

    scope: str
    since: float
    retry_at: float
    attempts: int = 1
    phase: str = PHASE_WAITING
    last_kind: str = KIND_DEPENDENCY_UNAVAILABLE
    last_source: str = ""
    server_retry_at: float | None = None
    # task_id -> generation the waiter reported under (None = unfenced).
    waiters: dict[str, int | None] = field(default_factory=dict)
    #: Woken waiters that have not yet reported again or completed; a fresh
    #: report from the scope while these are in flight means the probe failed.
    in_flight: set[str] = field(default_factory=set)

    def public(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "since": self.since,
            "retry_at": self.retry_at,
            "attempts": self.attempts,
            "phase": self.phase,
            "last_kind": self.last_kind,
            "last_source": self.last_source,
            "server_retry_at": self.server_retry_at,
            "waiters": len(self.waiters),
            "in_flight": len(self.in_flight),
        }

    def snapshot(self) -> "ScopeInstant":
        return ScopeInstant(
            scope=self.scope,
            since=self.since,
            retry_at=self.retry_at,
            server_retry_at=self.server_retry_at,
            attempts=self.attempts,
            phase=self.phase,
        )


@dataclass(frozen=True)
class ScopeInstant:
    """One scope's schedule as of a report or a wake -- the values a store write records.

    Taken under the schedule lock and used outside it, so the write never runs
    with the lock held. The persisted instants are therefore the schedule's as of
    the decision that produced the write, which is what the ``dependency_wait``
    and ``dependency_wake`` events have always claimed to carry.
    """

    scope: str
    since: float
    retry_at: float
    server_retry_at: float | None
    attempts: int
    phase: str


@dataclass(frozen=True)
class Verdict:
    """What :meth:`DependencyCoordinator.report` did with a signal."""

    #: ``wait`` (parked on the scope schedule, ``state`` says WHERE),
    #: ``terminal`` (task failed or moved to ``waiting_input``), ``deadline``
    #: (scope budget spent, failed), ``unpersisted`` (no row records the wait --
    #: the caller must not park on it).
    outcome: str
    state: str
    scope: str
    retry_at: float | None = None
    attempts: int = 0
    reason: str = ""


class DependencyCoordinator:
    """One retry schedule per dependency scope, wakes by capacity through admission.

    ``store`` may be ``None`` for callers that only want the schedule (the
    controller's signal path); every store write is then skipped. ``capacity``
    returns the current effective admission capacity and is consulted on each
    wake tick when ``wake_per_tick`` is 0. ``on_wake`` is called once per woken
    task after its row is ``queued`` again, so the dispatcher's pump can be
    armed; a wake never bypasses admission -- the row is merely eligible.
    ``clock`` and ``rng`` are injectable for deterministic tests.

    ``wake_through`` is the run loop's seam: asked ``(task_id, generation)``
    BEFORE the coordinator writes a wake. A ``True`` answer means the waiter is
    a LIVE run that yielded its lane slot and will re-enter through admission
    (which writes ``wake_wait`` under the run's own generation when the slot
    is granted); the coordinator then records the ``dependency_wake`` event
    only, so one wake is one store write. ``False`` (or no seam) keeps the
    parked-row path: the coordinator wakes the row itself. ``on_fail`` is told
    ``(task_id, reason)`` for every waiter a scope deadline or attempts cap
    fails from :meth:`tick` or :meth:`report`, so a run blocked on its wake can
    end instead of waiting for a grant that will never come.

    **Two threads, one schedule.** :meth:`report` and :meth:`tick` are called on
    the task store's writer thread (``store.run``), while the gateway loop edits
    and reads the same schedule (:meth:`forget`, :meth:`recovered`,
    :meth:`waiters`, :meth:`next_deadline`, :func:`shared_retry_at`). So
    ``_lock`` covers the in-memory schedule ONLY and is never held across a store
    write: one ``BEGIN IMMEDIATE`` waits up to ``TaskStore.BUSY_TIMEOUT_SECS`` for
    a competing writer, and a batch of them under the lock would stall the loop
    for that many timeouts with nothing in ``store.loop_thread_calls`` to show it
    (the loop thread never reaches the store here). Each write therefore takes a
    :class:`ScopeInstant` snapshot under the lock and runs outside it, the same
    separation ``TaskStore._executor_lock`` keeps between the executor slot and
    the connection lock, and the same order :meth:`_terminal` and :meth:`rebuild`
    already use. The wake / give-up hooks set loop-affine
    ``asyncio.Event``s, so they are handed to the loop they belong to
    (:meth:`_on_hook_loop`).
    """

    def __init__(
        self,
        store: TaskStore | None,
        *,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        backoff: LayerPolicy | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        wait_deadline_secs: float = DEFAULT_WAIT_DEADLINE_SECS,
        wake_per_tick: int = DEFAULT_WAKE_PER_TICK,
        wake_spacing_secs: float = DEFAULT_WAKE_SPACING_SECS,
        capacity: Callable[[], int] | None = None,
        on_wake: Callable[[str], None] | None = None,
        wake_through: Callable[[str, int | None], bool] | None = None,
        on_fail: Callable[[str, str], None] | None = None,
    ) -> None:
        self._store = store
        self._ledger = WaitLedger(store, clock=clock) if store is not None else None
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        self._backoff = backoff if backoff is not None else dependency_backoff()
        self._max_attempts = max(1, int(max_attempts))
        self._wait_deadline = max(0.0, float(wait_deadline_secs))
        self._wake_per_tick = max(0, int(wake_per_tick))
        self._wake_spacing = max(0.0, float(wake_spacing_secs))
        self._capacity = capacity or (lambda: 1)
        self._on_wake = on_wake
        self._wake_through = wake_through
        self._on_fail = on_fail
        self._wake_listeners: list[Callable[[str], None]] = []
        self._fail_listeners: list[Callable[[str, str], None]] = []
        self._lock = threading.RLock()
        self._scopes: dict[str, ScopeSchedule] = {}
        #: The loop the hooks' ``asyncio.Event``s live on; see
        #: :meth:`_on_hook_loop`. Recorded here for a coordinator built ON the
        #: loop, and in :meth:`subscribe` for the gateway's own, which is built
        #: on the writer thread and subscribed from the loop.
        self._hook_loop: asyncio.AbstractEventLoop | None = None
        self._remember_hook_loop()

    def subscribe(
        self,
        *,
        on_wake: Callable[[str], None] | None = None,
        on_fail: Callable[[str, str], None] | None = None,
    ) -> None:
        """Add a second consumer of the wake / give-up hooks.

        The subagent manager builds the coordinator with its own ``on_wake``
        (arm the admission pump); the runner adapters (TaskRunner, workflow
        agent calls) park their coroutines on the same scopes and need the
        same wake -- one schedule per scope, several waiters' owners. Every
        listener is called for every woken or failed task id; a listener that
        does not own the id ignores it.
        """
        # The wiring subscribes FROM the loop, which is where every hook's
        # ``asyncio.Event`` lives -- including the ones passed to __init__ on the
        # writer thread, since both belong to the one gateway loop.
        self._remember_hook_loop()
        if on_wake is not None:
            self._wake_listeners.append(on_wake)
        if on_fail is not None:
            self._fail_listeners.append(on_fail)

    def _remember_hook_loop(self) -> asyncio.AbstractEventLoop | None:
        """Record the loop the hooks belong to when called ON one; answer it."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._hook_loop = running
        return running

    def _on_hook_loop(self, fire: Callable[[], None]) -> None:
        """Run *fire* -- one task's wake or give-up hooks -- on the loop they own.

        Both hooks end in a loop-affine ``asyncio.Event.set()``: a run's
        ``_resume_event`` and the runner admission's ``_wakes`` entry. Made from
        the store's writer thread, that set is not delivered to the loop -- which
        never wakes to see it -- so the released run stays parked on a wake nothing
        will hand it until its own timeout ends the turn, and after a give-up the
        schedule is already cleared, which leaves these hooks as the ONLY releaser.
        The cross-thread door is ``call_soon_threadsafe``, captured the way
        ``apps/event_bus.build_broadcast_fn`` captures the gateway loop. A loop
        that is closed or not running would swallow the callback instead of
        delivering it, so *fire* runs here in that case: a hook that raises is
        already tolerated, a lost give-up is not.
        """
        running = self._remember_hook_loop()
        home = self._hook_loop
        if home is None or home is running or home.is_closed() or not home.is_running():
            fire()
            return
        try:
            home.call_soon_threadsafe(fire)
        except RuntimeError:
            fire()

    def _emit_wake(self, task_id: str) -> None:
        hooks: list[Callable[[str], None]] = []
        if self._on_wake is not None:
            hooks.append(self._on_wake)
        hooks.extend(self._wake_listeners)

        def _fire() -> None:
            for hook in hooks:
                try:
                    hook(task_id)
                except Exception:  # noqa: BLE001 - the pump hook is advisory
                    logger.debug("dependency on_wake hook failed for %s", task_id, exc_info=True)

        self._on_hook_loop(_fire)

    def _wake_through_or_requeue(self, task_id: str, generation: int | None, scope: str) -> bool:
        """Run the delegated wake; put the waiter back on its scope if it refuses.

        The worker path commits to delegation before the seam can answer, because
        the answer needs the loop. So the refusal lands HERE, one tick later, on a
        waiter its own caller already popped: without the requeue the row is never
        woken and no schedule holds it, so the live run sits on a resume event that
        nothing will set until its deadline cancels it. Requeueing is the same
        answer the inline arm reaches by testing the boolean, one tick later.
        """
        try:
            if bool(self._wake_through(task_id, generation)):  # type: ignore[misc]
                return True
        except Exception:  # noqa: BLE001 - a broken seam owes the waiter its place back
            logger.debug("dependency wake_through seam failed for %s", task_id, exc_info=True)
        self._requeue_waiter(task_id, generation, scope, why="refused its delegated wake")
        return False

    def _requeue_waiter(
        self, task_id: str, generation: int | None, scope: str, *, why: str
    ) -> None:
        """Return a popped waiter to its scope so the NEXT tick wakes it again.

        Only for a scope that still exists: one deleted while this callback was in
        flight means the last waiter left and the dependency recovered, and
        re-creating it here would resurrect a schedule nothing is waiting on.
        """
        with self._lock:
            sched = self._scopes.get(scope)
            if sched is None:
                logger.warning(
                    "dependency coordinator: %s lost its wake and its scope %s is gone",
                    task_id,
                    scope,
                )
                return
            sched.in_flight.discard(task_id)
            sched.waiters[task_id] = generation
            # Due on the next tick rather than instantly: the wake failed for a
            # reason that is unlikely to have changed within one pass.
            sched.retry_at = max(sched.retry_at, self.now() + self._wake_spacing)
        logger.info("dependency coordinator: %s %s; requeued on %s", task_id, why, scope)

    def _drop_waiter(self, task_id: str, scope: str) -> None:
        """Take *task_id* off *scope*: nobody is waiting on this schedule for it.

        For a waiter whose wait was never persisted (its caller owns the failure)
        and for a wake the store REFUSED (another owner holds the row). Keeping it
        would have the next tick wake a row this coordinator does not own.
        """
        with self._lock:
            sched = self._scopes.get(scope)
            if sched is None:
                return
            sched.waiters.pop(task_id, None)
            sched.in_flight.discard(task_id)
            if not sched.waiters and not sched.in_flight:
                self._scopes.pop(scope, None)

    def _emit_fail(self, task_id: str, reason: str) -> None:
        hooks: list[Callable[[str, str], None]] = []
        if self._on_fail is not None:
            hooks.append(self._on_fail)
        hooks.extend(self._fail_listeners)

        def _fire() -> None:
            for hook in hooks:
                try:
                    hook(task_id, reason)
                except Exception:  # noqa: BLE001 - the run-loop hook is advisory
                    logger.debug("dependency on_fail hook failed for %s", task_id, exc_info=True)

        self._on_hook_loop(_fire)

    # -- introspection -------------------------------------------------------

    def now(self) -> float:
        return float(self._clock())

    @property
    def wait_deadline_secs(self) -> float:
        """The wall-clock bound on one scope's wait (0 = attempts cap only)."""
        return self._wait_deadline

    @property
    def backoff_max_secs(self) -> float:
        return float(self._backoff.max_secs)

    def schedule(self, scope: str) -> ScopeSchedule | None:
        with self._lock:
            return self._scopes.get(scope)

    def scopes(self) -> list[str]:
        with self._lock:
            return sorted(self._scopes)

    def waiters(self, scope: str) -> list[str]:
        with self._lock:
            sched = self._scopes.get(scope)
            return list(sched.waiters) if sched else []

    def next_deadline(self) -> float | None:
        """Earliest ``retry_at`` across scopes -- when the pump should call :meth:`tick`."""
        with self._lock:
            if not self._scopes:
                return None
            return min(s.retry_at for s in self._scopes.values())

    def public(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.public() for s in sorted(self._scopes.values(), key=lambda s: s.scope)]

    # -- backoff -------------------------------------------------------------

    def backoff_ceiling(self, attempts: int) -> float:
        """The ladder's undithered ``min(max, base * 2**(attempts-1))`` -- the delay's upper bound."""
        return self._backoff.raw_backoff_secs(attempts)

    def backoff_delay(self, attempts: int) -> float:
        """The ladder's equal-jitter delay: uniform in ``[ceiling/2, ceiling]``."""
        return self._backoff.backoff_secs(attempts, rng=self._rng)

    def _wake_batch(self) -> int:
        if self._wake_per_tick > 0:
            return self._wake_per_tick
        try:
            return max(1, int(self._capacity()))
        except Exception:  # noqa: BLE001 - a broken capacity probe wakes one
            return 1

    def _park_until(self, sched: ScopeInstant) -> float:
        """``next_run_at`` for a parked row: the scope deadline, never the retry.

        The dispatcher must not pick a waiter up on its own at ``retry_at`` --
        that would bypass the staged wake -- so the row's own eligibility is
        the scope's wall-clock deadline: a safety net if this process dies
        before :meth:`rebuild` runs, not the normal path.
        """
        if self._wait_deadline > 0:
            return sched.since + self._wait_deadline
        return sched.retry_at + self.backoff_max_secs

    # -- reporting -----------------------------------------------------------

    def report(
        self,
        task_id: str,
        signal: DependencySignal,
        *,
        generation: int | None = None,
        from_state: str | None = None,
    ) -> Verdict:
        """A task hit a dependency error: park it on its scope's schedule.

        Terminal signals (auth, permanent parameter error, quota with no reset)
        end the task immediately -- ``auth_failed`` goes to the sign-in wait
        state when the machine has one, else ``failed``. Retryable signals join
        the scope's ONE schedule: the first report creates it, later reports
        join it without a second timer, and a report from a waiter the scope
        WOKE counts as that wake failing (attempts += 1, new backoff -- never
        earlier than an unexpired server ``retry_at``), once for the wave
        however many of its members report. A newcomer that arrives mid-wave
        joins the wait and spends none of the scope's probe budget.
        A server-supplied ``retry_at`` is honoured exactly and only ever
        extends the schedule. The attempts cap and the wall-clock deadline
        both end every waiter in the scope as ``failed``.

        Answers ``unpersisted`` when no row records the wait (the store could not
        take it): the schedule has a retry instant but nothing durable holds the
        waiter, so the caller must NOT park on it -- see :meth:`_persist_wait`.
        The schedule is decided under ``_lock``; every store write and every hook
        runs after it is released.
        """
        now = self.now()
        scope = signal.dependency_scope
        if signal.terminal:
            return self._terminal(task_id, signal, generation, now)
        with self._lock:
            sched = self._scopes.get(scope)
            if sched is None:
                sched = ScopeSchedule(
                    scope=scope,
                    since=now,
                    retry_at=now,
                    attempts=1,
                    last_kind=signal.kind,
                    last_source=signal.source,
                )
                sched.retry_at = self._next_retry_at(sched, signal, now)
                self._scopes[scope] = sched
            else:
                sched.last_kind = signal.kind
                sched.last_source = signal.source
                if task_id in sched.in_flight and sched.phase != PHASE_WAITING:
                    # A waiter this scope WOKE hit the wall again: the scope is
                    # still down, and the wake wave costs it ONE attempt --
                    # ``max_attempts`` bounds probe cycles, not arrivals. Both
                    # halves say that. ``in_flight`` is who was woken, so a task
                    # the scope never woke is a newcomer reporting its OWN first
                    # failure, not this scope's probe answering; and ``phase``
                    # is the wave's uncounted bit, since only :meth:`tick` moves
                    # it off ``waiting`` and the count below moves it back. The
                    # rest of the batch therefore joins the schedule like any
                    # other waiter rather than each spending a probe of its own.
                    sched.in_flight.discard(task_id)
                    sched.attempts += 1
                    sched.phase = PHASE_WAITING
                    sched.retry_at = self._next_retry_at(sched, signal, now)
                elif signal.retry_at is not None and signal.retry_at > sched.retry_at:
                    # A later server-stated reset extends the shared schedule.
                    sched.server_retry_at = signal.retry_at
                    sched.retry_at = float(signal.retry_at)
            sched.waiters[task_id] = generation
            sched.in_flight.discard(task_id)
            over_attempts = self._attempts_exhausted(sched)
            give_up = over_attempts or self._deadline_passed(sched, now)
            reason = ""
            failed: list[tuple[str, int | None]] = []
            if give_up:
                reason = (
                    f"dependency {scope} unavailable after {sched.attempts} attempts"
                    if over_attempts
                    else f"dependency {scope} unavailable for {now - sched.since:.0f}s"
                )
                failed = self._take_scope_failure(sched)
            instant = sched.snapshot()
        if give_up:
            self._write_scope_failure(failed, instant, reason)
            return Verdict(
                outcome=OUTCOME_DEADLINE,
                state=FAILED,
                scope=scope,
                attempts=instant.attempts,
                reason=reason,
            )
        persisted = self._persist_wait(task_id, signal, instant, generation, from_state)
        if persisted is None:
            # The caller owns the failure, so the schedule must stop holding a
            # waiter that is not waiting: the next tick would wake a row whose
            # state this coordinator never wrote.
            self._drop_waiter(task_id, scope)
            return Verdict(
                outcome=OUTCOME_UNPERSISTED,
                state=str(from_state or ""),
                scope=scope,
                retry_at=instant.retry_at,
                attempts=instant.attempts,
                reason=f"dependency wait on {scope} did not persist",
            )
        return Verdict(
            outcome=OUTCOME_WAIT,
            state=persisted,
            scope=scope,
            retry_at=instant.retry_at,
            attempts=instant.attempts,
        )

    def _next_retry_at(self, sched: ScopeSchedule, signal: DependencySignal, now: float) -> float:
        """The scope's next retry instant; an UNEXPIRED server statement is a floor.

        One scope's ramp batch fails item by item and not every failure carries
        a header: a headerless 503 from one waiter must not move the shared
        instant back before the reset another waiter's 429 already stated, or
        the scope retries against a dependency that said it would refuse. The
        instant passing is the only implicit exit from the floor;
        :meth:`recovered` is the explicit one.
        """
        floor = sched.server_retry_at if (sched.server_retry_at or 0.0) > now else None
        if signal.retry_at is not None:
            stated = max(now, float(signal.retry_at))
            if floor is not None:
                stated = max(stated, floor)
            sched.server_retry_at = stated
            return stated
        if floor is not None:
            return max(floor, now + self.backoff_delay(sched.attempts))
        sched.server_retry_at = None
        return now + self.backoff_delay(sched.attempts)

    def _deadline_passed(self, sched: ScopeSchedule, now: float) -> bool:
        return self._wait_deadline > 0 and (now - sched.since) > self._wait_deadline

    def _attempts_exhausted(self, sched: ScopeSchedule) -> bool:
        """The probe-count cap; our own infrastructure scopes have none.

        An ``mcp_gateway:*`` outage is bounded by :meth:`_deadline_passed`
        only (with a zero deadline the scope waits until the gateway is back);
        every other scope keeps ``max_attempts`` probes.
        """
        if is_infra_scope(sched.scope):
            return False
        return sched.attempts > self._max_attempts

    def _terminal(
        self, task_id: str, signal: DependencySignal, generation: int | None, now: float
    ) -> Verdict:
        reason = f"{signal.kind}: {signal.detail or signal.dependency_scope}"
        state = AUTH_STATE if signal.kind == KIND_AUTH_FAILED else FAILED
        if self._store is not None and self._ledger is not None:
            if state != FAILED:
                record = WaitRecord.input(
                    f"auth:{signal.dependency_scope}",
                    since=now,
                    reason=f"sign-in required for {signal.dependency_scope}: {signal.detail}",
                    source=EVIDENCE_DEPENDENCY_ADAPTER,
                )
                if self._enter(task_id, record, generation) != WRITE_COMMITTED:
                    state = FAILED
            if state == FAILED:
                self._store_finish(task_id, generation, reason)
            self._append_event(task_id, EVENT_FAILED, {**signal.to_dict(), "reason": reason})
        with self._lock:
            for sched in self._scopes.values():
                sched.waiters.pop(task_id, None)
                sched.in_flight.discard(task_id)
        return Verdict(
            outcome=OUTCOME_TERMINAL, state=state, scope=signal.dependency_scope, reason=reason
        )

    def _persist_wait(
        self,
        task_id: str,
        signal: DependencySignal,
        sched: ScopeInstant,
        generation: int | None,
        from_state: str | None,
    ) -> str | None:
        """Write the wait; answers WHERE it landed, or ``None`` when nowhere.

        Three distinct outcomes, because two of them are a wait and one is not:
        ``WAIT_STATE`` (a live run entered ``waiting_dependency`` with the
        record), ``PARK_STATE`` (the row was not live -- ``starting``, or already
        parked -- so it is re-dispatched later), and ``None`` -- NOTHING durable
        holds the wait. An outage is never answered with a park: ``retry_wait``
        has an edge to neither ``running`` nor ``done``, so parking a live run
        there would have its own terminal write refused too. ``None`` also appends
        no ``dependency_wait`` event: an event claiming a wait no row is in would
        be the one thing :meth:`rebuild` trusts.
        """
        if self._store is None:
            return WAIT_STATE  # store-less by construction: the schedule is the record
        deadline_at = sched.since + self._wait_deadline if self._wait_deadline > 0 else None
        record = WaitRecord.dependency(
            sched.scope,
            since=sched.since,
            retry_at=sched.retry_at,
            reason=f"{signal.kind} from {signal.source}: {signal.detail or sched.scope}",
            deadline_at=deadline_at,
        )
        entered = self._enter(task_id, record, generation)
        if entered == WRITE_UNAVAILABLE:
            return None
        state = WAIT_STATE
        if entered == WRITE_REFUSED:
            # Not a live run (``starting``, or already parked): re-dispatch
            # later instead. ``next_run_at`` is the scope DEADLINE, not the
            # retry: the dispatcher must not pick the row up on its own at
            # ``retry_at`` and bypass the staged wake. It is a safety net for
            # a process that dies before :meth:`rebuild` runs.
            state = PARK_STATE
            parked = self._transition(
                task_id,
                PARK_STATE,
                generation,
                next_run_at=self._park_until(sched),
                detail={"dependency_scope": sched.scope, "retry_at": sched.retry_at},
            )
            if parked == WRITE_UNAVAILABLE:
                return None
            if parked == WRITE_REFUSED and not self._already_parked(task_id):
                return None
        self._append_event(
            task_id,
            EVENT_WAIT,
            {
                **signal.to_dict(),
                "retry_at": sched.retry_at,
                "server_retry_at": sched.server_retry_at,
                "attempts": sched.attempts,
                "since": sched.since,
                "state": state,
                "from_state": from_state,
            },
        )
        return state

    def _already_parked(self, task_id: str) -> bool:
        """Whether the row is ALREADY in ``retry_wait``: a refused park changed nothing.

        A woken probe that fails again reports from ``retry_wait``, which the
        transition table gives an edge to neither ``waiting_dependency`` nor
        itself -- both writes are refused although the row IS where a parked
        waiter belongs, and its ``dependency_wait`` event is what :meth:`rebuild`
        restores it from. Any other state (queued, admitted, terminal, gone) means
        nothing durable holds this wait, and an unreadable store means we do not
        know that it does.
        """
        assert self._store is not None
        try:
            return self._store.state_of(task_id) == PARK_STATE
        except TaskStoreUnavailable:
            logger.warning("dependency coordinator: state of %s is unreadable", task_id)
            return False

    # -- waking --------------------------------------------------------------

    def recovered(self, scope: str) -> bool:
        """An external recovery signal for *scope*: wake now, still staged."""
        with self._lock:
            sched = self._scopes.get(scope)
            if sched is None:
                return False
            sched.retry_at = self.now()
            sched.server_retry_at = None
            return True

    def forget(self, task_id: str) -> None:
        """A waiter completed or was cancelled elsewhere: drop it from every scope."""
        with self._lock:
            for scope in list(self._scopes):
                sched = self._scopes[scope]
                sched.waiters.pop(task_id, None)
                if task_id in sched.in_flight:
                    sched.in_flight.discard(task_id)
                    # A woken task finishing is the probe SUCCEEDING.
                    if sched.phase == PHASE_PROBE:
                        sched.phase = PHASE_RAMP
                        sched.retry_at = min(sched.retry_at, self.now())
                if not sched.waiters and not sched.in_flight:
                    del self._scopes[scope]

    def tick(
        self,
        *,
        callbacks: list[Callable[[], Any]] | None = None,
        live_waiters: frozenset[tuple[str, int | None]] = frozenset(),
    ) -> list[str]:
        """Wake due scopes; returns the task ids made eligible this tick.

        Per due scope: in ``waiting`` phase exactly ONE waiter is woken as the
        probe and the scope moves to ``probe``; ``wake_spacing_secs`` later,
        if no report came back, the scope ramps and wakes ``wake_per_tick``
        waiters per spacing until none remain. Scopes past their deadline
        fail instead. Scopes are handled independently so a due scope is
        never delayed by a throttled one. A worker caller supplies ``callbacks``
        and a loop-owned snapshot of ``live_waiters``; store work stays here,
        while admission and listener callbacks are applied back on the loop.

        The whole pass is DECIDED under ``_lock`` and then executed without it:
        one contended scope's store writes must not hold the schedule the loop
        reads (see the class docstring).
        """
        from functools import partial

        now = self.now()
        woken: list[str] = []
        plan: list[tuple[str, int | None, ScopeInstant]] = []
        failures: list[tuple[list[tuple[str, int | None]], ScopeInstant, str]] = []
        with self._lock:
            for scope in sorted(self._scopes):
                sched = self._scopes[scope]
                if sched.retry_at > now:
                    continue
                if self._deadline_passed(sched, now) and sched.phase == PHASE_WAITING:
                    reason = f"dependency {scope} unavailable for {now - sched.since:.0f}s"
                    failures.append((self._take_scope_failure(sched), sched.snapshot(), reason))
                    continue
                if not sched.waiters:
                    # Only woken tasks remain. They are kept so a late probe
                    # failure still counts against the scope, but a task that
                    # finished without calling forget() must not pin the
                    # scope forever: once the backoff cap has passed since the
                    # last wake, the scope is considered recovered.
                    if not sched.in_flight or (now - sched.retry_at) > self.backoff_max_secs:
                        del self._scopes[scope]
                    continue
                if sched.phase == PHASE_WAITING:
                    batch = 1
                    sched.phase = PHASE_PROBE
                else:
                    batch = self._wake_batch()
                    sched.phase = PHASE_RAMP
                instant = sched.snapshot()
                for task_id in list(sched.waiters)[:batch]:
                    generation = sched.waiters.pop(task_id)
                    sched.in_flight.add(task_id)
                    plan.append((task_id, generation, instant))
                if sched.waiters:
                    sched.retry_at = now + self._wake_spacing
                elif not sched.in_flight:
                    del self._scopes[scope]
        for failed, instant, reason in failures:
            self._write_scope_failure(failed, instant, reason, callbacks=callbacks)
        for task_id, generation, instant in plan:
            if self._wake_one(
                task_id, generation, instant, now, callbacks=callbacks, live_waiters=live_waiters
            ):
                woken.append(task_id)
        for task_id in woken:
            if callbacks is None:
                self._emit_wake(task_id)
            else:
                callbacks.append(partial(self._emit_wake, task_id))
        return woken

    def _wake_one(
        self,
        task_id: str,
        generation: int | None,
        sched: ScopeInstant,
        now: float,
        *,
        callbacks: list[Callable[[], Any]] | None = None,
        live_waiters: frozenset[tuple[str, int | None]] = frozenset(),
    ) -> bool:
        """Wake one waiter; ``False`` when the wake did NOT happen.

        A ``False`` waiter is never reported as woken and gets no
        ``dependency_wake`` event: it was either put back on its scope (the store
        could not take the wake -- an outage is transient and the waiter keeps its
        place) or dropped (the store REFUSED it -- the row is gone, terminal, or
        held under another generation, so no tick of ours can wake it).
        """
        if self._store is None or self._ledger is None:
            return True  # store-less by construction: the schedule alone owns the wake
        reason = f"dependency {sched.scope} retry ({sched.phase}, attempt {sched.attempts})"
        if self._wake_through is not None:
            delegated = False
            if callbacks is not None:
                if (task_id, generation) in live_waiters:
                    from functools import partial

                    # The deferred seam cannot answer here, so its refusal is
                    # handled where it arrives: the caller already popped this
                    # waiter, and a `False` (the manager does not own the run, or
                    # the resume was refused) or a raise would otherwise leave the
                    # row unwoken with nobody holding it -- the run then waits on
                    # a resume event until its deadline cancels it. `_requeue_waiter`
                    # is what the INLINE arm gets for free by testing the boolean.
                    callbacks.append(
                        partial(self._wake_through_or_requeue, task_id, generation, sched.scope)
                    )
                    delegated = True
            else:
                try:
                    delegated = bool(self._wake_through(task_id, generation))
                except Exception:  # noqa: BLE001 - a broken seam falls back to the row path
                    logger.debug(
                        "dependency wake_through seam failed for %s", task_id, exc_info=True
                    )
            if delegated:
                # A live run owns this wake: admission writes ``wake_wait``
                # under the run's generation when the lane slot is granted,
                # so the coordinator records the wake and writes nothing else.
                self._append_event(
                    task_id,
                    EVENT_WAKE,
                    {
                        "dependency_scope": sched.scope,
                        "phase": sched.phase,
                        "attempts": sched.attempts,
                        "to": RUNNING,
                        "via": "admission",
                        "generation": generation,
                    },
                )
                return True
        try:
            new_gen = self._ledger.wake(task_id, reason=reason, generation=generation)
        except TaskStoreUnavailable:
            # An outage is not an answer about the ROW: reading it as "the row was
            # parked" attempts a ``waiting_dependency -> queued`` the transition
            # table forbids, and the waiter -- already popped by the caller -- is
            # then held by nobody at all. It keeps its place instead.
            logger.warning("dependency coordinator: wake %s failed", task_id)
            self._requeue_waiter(
                task_id, generation, sched.scope, why="could not be woken (store unavailable)"
            )
            return False
        # ``WaitLedger.wake`` lands a live waiter in ``retry_wait`` (claimable, new
        # generation) and answers None for a row that is in no wait at all.
        woke_to = RETRY_WAIT
        if new_gen is None:
            # Parked in ``retry_wait``: make it claimable now; admission meters it.
            woke_to = QUEUED
            moved = self._transition(
                task_id,
                QUEUED,
                generation,
                next_run_at=now,
                detail={"dependency_scope": sched.scope, "phase": sched.phase},
            )
            if moved == WRITE_UNAVAILABLE:
                self._requeue_waiter(
                    task_id, generation, sched.scope, why="could not be woken (store unavailable)"
                )
                return False
            if moved == WRITE_REFUSED:
                logger.warning(
                    "dependency coordinator: %s is in neither a wait nor retry_wait; "
                    "dropped from %s",
                    task_id,
                    sched.scope,
                )
                self._drop_waiter(task_id, sched.scope)
                return False
        self._append_event(
            task_id,
            EVENT_WAKE,
            {
                "dependency_scope": sched.scope,
                "phase": sched.phase,
                "attempts": sched.attempts,
                "to": woke_to,
                "generation": new_gen,
            },
        )
        return True

    def _take_scope_failure(self, sched: ScopeSchedule) -> list[tuple[str, int | None]]:
        """Give up on *sched*: clear the schedule and hand back who was on it.

        The in-memory half, under ``_lock``; :meth:`_write_scope_failure` does the
        rows and the hooks outside it. Split because the caller holds ``_lock`` and
        each waiter costs one ``finish`` plus one event -- two busy waits per waiter
        the gateway loop would otherwise queue behind.
        """
        failed = list(sched.waiters.items())
        sched.waiters.clear()
        if not sched.in_flight:
            self._scopes.pop(sched.scope, None)
        return failed

    def _write_scope_failure(
        self,
        failed: list[tuple[str, int | None]],
        sched: ScopeInstant,
        reason: str,
        *,
        callbacks: list[Callable[[], Any]] | None = None,
    ) -> None:
        """Fail every waiter :meth:`_take_scope_failure` handed back, then release them.

        The give-up hook is each waiter's LAST releaser -- the schedule that would
        have woken it is gone -- so it is emitted for every one of them, whether
        the row's own ``failed`` write committed or not.
        """
        from functools import partial

        for task_id, generation in failed:
            if self._store is not None:
                self._store_finish(task_id, generation, reason)
                self._append_event(
                    task_id,
                    EVENT_FAILED,
                    {
                        "dependency_scope": sched.scope,
                        "reason": reason,
                        "attempts": sched.attempts,
                        "since": sched.since,
                    },
                )
        logger.warning("dependency scope %s gave up: %s", sched.scope, reason)
        for task_id, _generation in failed:
            if callbacks is None:
                self._emit_fail(task_id, reason)
            else:
                callbacks.append(partial(self._emit_fail, task_id, reason))

    # -- restart -------------------------------------------------------------

    def rebuild(self) -> int:
        """Rebuild the schedule from the rows after a restart.

        Every ``waiting_dependency`` row rejoins its ``WaitRecord.dependency_scope``;
        every ``retry_wait`` row whose newest ``dependency_wait`` event is newer
        than its newest ``dependency_wake`` / ``dependency_failed`` event
        rejoins the scope that event names. A scope's ``retry_at`` / ``attempts``
        / ``since`` are the latest / max / earliest across its waiters, so a
        scope that was mid-backoff resumes it instead of retrying at once. The
        server floor (``server_retry_at``, latest across the waiters) rides in
        the same event, so a restart cannot turn an authoritative deadline back
        into a ladder delay; a row written without the key carries no floor.
        Returns the number of waiters restored.
        """
        if self._store is None:
            return 0
        restored = 0
        candidates: list[tuple[TaskRecord, dict[str, Any]]] = []
        try:
            for row in self._store.waiting_rows(state=WAIT_STATE):
                record = WaitRecord.from_dict(row.wait)
                wait = self._last_wait_event(row.id) or {}
                if record is not None and record.dependency_scope:
                    wait.setdefault("dependency_scope", record.dependency_scope)
                    wait.setdefault("since", record.since)
                    if record.resume_condition.at is not None:
                        wait.setdefault("retry_at", record.resume_condition.at)
                if wait.get("dependency_scope"):
                    candidates.append((row, wait))
            for row in self._store.list_rows(state=PARK_STATE, limit=100_000):
                parked = self._last_wait_event(row.id)
                if parked is not None and parked.get("dependency_scope"):
                    candidates.append((row, parked))
        except TaskStoreUnavailable:
            logger.warning("dependency rebuild: cannot read waiting rows", exc_info=True)
            return restored
        with self._lock:
            for row, wait in candidates:
                scope = str(wait["dependency_scope"])
                signal = DependencySignal.from_dict(wait)
                retry_at = float(wait.get("retry_at") or self.now())
                attempts = int(wait.get("attempts") or 1)
                since = float(wait.get("since") or row.updated_at or self.now())
                stated = wait.get("server_retry_at")
                floor = float(stated) if stated is not None else None
                sched = self._scopes.get(scope)
                if sched is None:
                    sched = ScopeSchedule(
                        scope=scope,
                        since=since,
                        retry_at=retry_at,
                        attempts=attempts,
                        last_kind=signal.kind if signal else KIND_DEPENDENCY_UNAVAILABLE,
                        last_source=signal.source if signal else "",
                        server_retry_at=floor,
                    )
                    self._scopes[scope] = sched
                else:
                    sched.since = min(sched.since, since)
                    sched.retry_at = max(sched.retry_at, retry_at)
                    sched.attempts = max(sched.attempts, attempts)
                    if floor is not None:
                        sched.server_retry_at = max(sched.server_retry_at or 0.0, floor)
                sched.waiters[row.id] = row.generation
                restored += 1
        return restored

    def _last_wait_event(self, task_id: str) -> dict[str, Any] | None:
        assert self._store is not None
        try:
            events = self._store.events(task_id, limit=500)
        except TaskStoreUnavailable:
            return None
        for ev in reversed(events):
            if ev.kind in (EVENT_WAKE, EVENT_FAILED):
                return None
            if ev.kind == EVENT_WAIT:
                return dict(ev.data)
        return None

    # -- store helpers -------------------------------------------------------

    def _enter(self, task_id: str, record: WaitRecord, generation: int | None) -> str:
        """Enter the wait; one of :data:`WRITE_COMMITTED` / ``REFUSED`` / ``UNAVAILABLE``.

        The three are kept apart all the way up: a REFUSED enter means the row is
        not a live run and is parked instead, while an UNAVAILABLE one says nothing
        about the row and must not be answered with a park.
        """
        assert self._ledger is not None
        try:
            entered = self._ledger.enter(task_id, record, generation=generation)
        except TaskStoreUnavailable:
            logger.warning("dependency coordinator: enter wait for %s failed", task_id)
            return WRITE_UNAVAILABLE
        return WRITE_COMMITTED if entered else WRITE_REFUSED

    def _transition(
        self,
        task_id: str,
        state: str,
        generation: int | None,
        *,
        next_run_at: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> str:
        """Move the row; one of :data:`WRITE_COMMITTED` / ``REFUSED`` / ``UNAVAILABLE``."""
        assert self._store is not None
        try:
            moved = self._store.transition(
                task_id, state, generation=generation, next_run_at=next_run_at, detail=detail
            )
        except TaskStoreUnavailable:
            logger.warning("dependency coordinator: transition %s -> %s failed", task_id, state)
            return WRITE_UNAVAILABLE
        return WRITE_COMMITTED if moved else WRITE_REFUSED

    def _store_finish(self, task_id: str, generation: int | None, reason: str) -> bool:
        assert self._store is not None
        try:
            return self._store.finish(task_id, FAILED, generation=generation, error=reason)
        except TaskStoreUnavailable:
            logger.warning("dependency coordinator: finish %s failed", task_id)
            return False

    def _append_event(self, task_id: str, kind: str, data: dict[str, Any]) -> None:
        assert self._store is not None
        try:
            self._store.append_event(task_id, kind, data)
        except TaskStoreUnavailable:
            logger.warning("dependency coordinator: event %s for %s lost", kind, task_id)


_current_lock = threading.Lock()
_current: DependencyCoordinator | None = None


def register_coordinator(coordinator: DependencyCoordinator | None) -> None:
    """Publish the process's ONE coordinator (the subagent manager's) for readers
    that hold no task row -- the main chat consults its scope schedule so a
    throttle it hits waits out the same cooldown the sub-agents are already on."""
    global _current
    with _current_lock:
        _current = coordinator


def current_coordinator() -> DependencyCoordinator | None:
    with _current_lock:
        return _current


def shared_retry_at(scope: str) -> float | None:
    """The registered coordinator's ``retry_at`` for *scope*, or None when no
    schedule exists for it. Read-only: a caller without a task row must not
    join the schedule (that would persist events for a row that does not exist)."""
    coordinator = current_coordinator()
    if coordinator is None:
        return None
    sched = coordinator.schedule(scope)
    return None if sched is None else float(sched.retry_at)


def coordinator_from_config(
    store: TaskStore | None,
    agent_config: Any,
    *,
    capacity: Callable[[], int] | None = None,
    on_wake: Callable[[str], None] | None = None,
    wake_through: Callable[[str, int | None], bool] | None = None,
    on_fail: Callable[[str, str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> DependencyCoordinator:
    """Build a coordinator from an ``AgentConfig``: the ``dependency_*`` keys and
    the shared ``recovery_backoff_*`` schedule."""

    def _get(name: str, default: Any) -> Any:
        return getattr(agent_config, name, default)

    schedule = RecoveryPolicy.from_config(SimpleNamespace(agent=agent_config))
    return DependencyCoordinator(
        store,
        clock=clock,
        backoff=dependency_backoff(schedule),
        max_attempts=_get("dependency_max_attempts", DEFAULT_MAX_ATTEMPTS),
        wait_deadline_secs=_get("dependency_wait_deadline_secs", DEFAULT_WAIT_DEADLINE_SECS),
        wake_per_tick=_get("dependency_wake_per_tick", DEFAULT_WAKE_PER_TICK),
        wake_spacing_secs=_get("dependency_wake_spacing_secs", DEFAULT_WAKE_SPACING_SECS),
        capacity=capacity,
        on_wake=on_wake,
        wake_through=wake_through,
        on_fail=on_fail,
    )


__all__ = [
    "AUTH_STATE",
    "Adapter",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_WAIT_DEADLINE_SECS",
    "DEFAULT_WAKE_PER_TICK",
    "DEFAULT_WAKE_SPACING_SECS",
    "DependencyCoordinator",
    "DependencySignal",
    "EVENT_FAILED",
    "EVENT_WAIT",
    "EVENT_WAKE",
    "KIND_AUTH_FAILED",
    "KIND_CONCURRENCY_EXCEEDED",
    "KIND_DEPENDENCY_UNAVAILABLE",
    "KIND_PERMANENT_PARAM_ERROR",
    "KIND_QUOTA_EXHAUSTED",
    "KIND_RATE_LIMITED",
    "OUTCOME_DEADLINE",
    "OUTCOME_TERMINAL",
    "OUTCOME_UNPERSISTED",
    "OUTCOME_WAIT",
    "PARK_STATE",
    "PHASE_PROBE",
    "PHASE_RAMP",
    "PHASE_WAITING",
    "SIGNAL_ATTR",
    "SIGNAL_KINDS",
    "ScopeInstant",
    "ScopeSchedule",
    "TERMINAL_KINDS",
    "Verdict",
    "WRITE_COMMITTED",
    "WRITE_REFUSED",
    "WRITE_UNAVAILABLE",
    "WAIT_STATE",
    "classify_exception",
    "coordinator_from_config",
    "dependency_backoff",
    "current_coordinator",
    "register_adapter",
    "register_coordinator",
    "registered_adapters",
    "shared_retry_at",
    "unregister_adapter",
]
