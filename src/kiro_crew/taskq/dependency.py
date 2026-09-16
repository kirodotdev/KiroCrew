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

Specification: ``docs/system-specs/modules/taskq.md`` (Dependency waits).
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class Verdict:
    """What :meth:`DependencyCoordinator.report` did with a signal."""

    #: ``wait`` (parked on the scope schedule), ``terminal`` (task failed or
    #: moved to ``waiting_input``), ``deadline`` (scope budget spent, failed).
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
    fails from :meth:`tick`, so a run blocked on its wake can end instead of
    waiting for a grant that will never come.
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
        if on_wake is not None:
            self._wake_listeners.append(on_wake)
        if on_fail is not None:
            self._fail_listeners.append(on_fail)

    def _emit_wake(self, task_id: str) -> None:
        hooks: list[Callable[[str], None]] = []
        if self._on_wake is not None:
            hooks.append(self._on_wake)
        hooks.extend(self._wake_listeners)
        for hook in hooks:
            try:
                hook(task_id)
            except Exception:  # noqa: BLE001 - the pump hook is advisory
                logger.debug("dependency on_wake hook failed for %s", task_id, exc_info=True)

    def _emit_fail(self, task_id: str, reason: str) -> None:
        hooks: list[Callable[[str, str], None]] = []
        if self._on_fail is not None:
            hooks.append(self._on_fail)
        hooks.extend(self._fail_listeners)
        for hook in hooks:
            try:
                hook(task_id, reason)
            except Exception:  # noqa: BLE001 - the run-loop hook is advisory
                logger.debug("dependency on_fail hook failed for %s", task_id, exc_info=True)

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

    def _park_until(self, sched: ScopeSchedule) -> float:
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
        join it without a second timer, and a report that arrives while a probe
        is in flight counts as the probe failing (attempts += 1, new backoff --
        never earlier than an unexpired server ``retry_at``).
        A server-supplied ``retry_at`` is honoured exactly and only ever
        extends the schedule. The attempts cap and the wall-clock deadline
        both end every waiter in the scope as ``failed``.
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
                if task_id in sched.in_flight or sched.phase != PHASE_WAITING:
                    # The probe (or a ramp batch) hit the wall again: the scope
                    # is still down. One attempt for the whole scope, not one
                    # per woken task.
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
            if over_attempts or self._deadline_passed(sched, now):
                reason = (
                    f"dependency {scope} unavailable after {sched.attempts} attempts"
                    if over_attempts
                    else f"dependency {scope} unavailable for {now - sched.since:.0f}s"
                )
                self._fail_scope(sched, reason, now)
                return Verdict(
                    outcome="deadline",
                    state=FAILED,
                    scope=scope,
                    attempts=sched.attempts,
                    reason=reason,
                )
            self._persist_wait(task_id, signal, sched, generation, from_state)
            return Verdict(
                outcome="wait",
                state=WAIT_STATE,
                scope=scope,
                retry_at=sched.retry_at,
                attempts=sched.attempts,
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
            moved = False
            if state != FAILED:
                record = WaitRecord.input(
                    f"auth:{signal.dependency_scope}",
                    since=now,
                    reason=f"sign-in required for {signal.dependency_scope}: {signal.detail}",
                    source=EVIDENCE_DEPENDENCY_ADAPTER,
                )
                moved = self._enter(task_id, record, generation)
                if not moved:
                    state = FAILED
            if state == FAILED:
                self._store_finish(task_id, generation, reason)
            self._append_event(task_id, EVENT_FAILED, {**signal.to_dict(), "reason": reason})
        with self._lock:
            for sched in self._scopes.values():
                sched.waiters.pop(task_id, None)
                sched.in_flight.discard(task_id)
        return Verdict(
            outcome="terminal", state=state, scope=signal.dependency_scope, reason=reason
        )

    def _persist_wait(
        self,
        task_id: str,
        signal: DependencySignal,
        sched: ScopeSchedule,
        generation: int | None,
        from_state: str | None,
    ) -> None:
        if self._store is None:
            return
        deadline_at = sched.since + self._wait_deadline if self._wait_deadline > 0 else None
        record = WaitRecord.dependency(
            sched.scope,
            since=sched.since,
            retry_at=sched.retry_at,
            reason=f"{signal.kind} from {signal.source}: {signal.detail or sched.scope}",
            deadline_at=deadline_at,
        )
        state = WAIT_STATE
        if not self._enter(task_id, record, generation):
            # Not a live run (``starting``, or already parked): re-dispatch
            # later instead. ``next_run_at`` is the scope DEADLINE, not the
            # retry: the dispatcher must not pick the row up on its own at
            # ``retry_at`` and bypass the staged wake. It is a safety net for
            # a process that dies before :meth:`rebuild` runs.
            state = PARK_STATE
            self._transition(
                task_id,
                PARK_STATE,
                generation,
                next_run_at=self._park_until(sched),
                detail={"dependency_scope": sched.scope, "retry_at": sched.retry_at},
            )
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
        """
        from functools import partial

        now = self.now()
        woken: list[str] = []
        with self._lock:
            for scope in sorted(self._scopes):
                sched = self._scopes[scope]
                if sched.retry_at > now:
                    continue
                if self._deadline_passed(sched, now) and sched.phase == PHASE_WAITING:
                    self._fail_scope(
                        sched,
                        f"dependency {scope} unavailable for {now - sched.since:.0f}s",
                        now,
                        callbacks=callbacks,
                    )
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
                for task_id in list(sched.waiters)[:batch]:
                    generation = sched.waiters.pop(task_id)
                    sched.in_flight.add(task_id)
                    self._wake_one(
                        task_id,
                        generation,
                        sched,
                        now,
                        callbacks=callbacks,
                        live_waiters=live_waiters,
                    )
                    woken.append(task_id)
                if sched.waiters:
                    sched.retry_at = now + self._wake_spacing
                elif not sched.in_flight:
                    del self._scopes[scope]
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
        sched: ScopeSchedule,
        now: float,
        *,
        callbacks: list[Callable[[], Any]] | None = None,
        live_waiters: frozenset[tuple[str, int | None]] = frozenset(),
    ) -> None:
        if self._store is None or self._ledger is None:
            return
        reason = f"dependency {sched.scope} retry ({sched.phase}, attempt {sched.attempts})"
        if self._wake_through is not None:
            delegated = False
            if callbacks is not None:
                if (task_id, generation) in live_waiters:
                    from functools import partial

                    callbacks.append(partial(self._wake_through, task_id, generation))
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
                return
        new_gen: int | None = None
        try:
            new_gen = self._ledger.wake(task_id, reason=reason, generation=generation)
        except TaskStoreUnavailable:
            logger.warning("dependency coordinator: wake %s failed", task_id)
        woke_to = RUNNING
        if new_gen is None:
            # Parked in ``retry_wait``: make it claimable now; admission meters it.
            woke_to = QUEUED
            self._transition(
                task_id,
                QUEUED,
                generation,
                next_run_at=now,
                detail={"dependency_scope": sched.scope, "phase": sched.phase},
            )
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

    def _fail_scope(
        self,
        sched: ScopeSchedule,
        reason: str,
        now: float,
        *,
        callbacks: list[Callable[[], Any]] | None = None,
    ) -> None:
        from functools import partial

        failed = list(sched.waiters.items())
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
        sched.waiters.clear()
        if not sched.in_flight:
            self._scopes.pop(sched.scope, None)
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

    def _enter(self, task_id: str, record: WaitRecord, generation: int | None) -> bool:
        assert self._ledger is not None
        try:
            return self._ledger.enter(task_id, record, generation=generation)
        except TaskStoreUnavailable:
            logger.warning("dependency coordinator: enter wait for %s failed", task_id)
            return False

    def _transition(
        self,
        task_id: str,
        state: str,
        generation: int | None,
        *,
        next_run_at: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        assert self._store is not None
        try:
            return self._store.transition(
                task_id, state, generation=generation, next_run_at=next_run_at, detail=detail
            )
        except TaskStoreUnavailable:
            logger.warning("dependency coordinator: transition %s -> %s failed", task_id, state)
            return False

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
    "PARK_STATE",
    "PHASE_PROBE",
    "PHASE_RAMP",
    "PHASE_WAITING",
    "SIGNAL_ATTR",
    "SIGNAL_KINDS",
    "ScopeSchedule",
    "TERMINAL_KINDS",
    "Verdict",
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
