"""The one admission call every execution entry point makes.

Entry points use :func:`current_service`, then ``admit`` or ``admit_child``:

* ``passthrough`` -- fair admission is off; :meth:`Admission.run` awaits the work.
* ``rejected`` -- nothing accepted; :meth:`Admission.refusal_response` answers HTTP.
* ``admitted`` or ``queued`` -- durable before acknowledgment. The caller owns
  the reservation until handoff; interrupted acceptance cancels its own row and
  releases its ticket. :meth:`Admission.run` revalidates, runs within containment
  and a deadline, then releases capacity once the work and descendants stop.

Budget: the INITIATOR's principal (``ActorContext`` published by the auth
middleware under ``ACTOR_REQUEST_KEY``; Unknown when absent, so every
unattributed caller shares one bounded bucket). Never the session creator.
Children inherit the budget and root execution of the session that spawned
them (:meth:`AdmissionService.budget_for_session`). Automation with no recorded
initiator is charged to :data:`AUTOMATION_PRINCIPAL`, which has the same
limits as anyone.

Restart: :meth:`AdmissionService.ensure_ready` reconciles every non-terminal
record before the first admission of a process. Work proven never started is
re-queued only when a relauncher for its operation is registered, otherwise it
ends ``cancelled``; work that may have run ends ``unknown_outcome`` and is never
replayed; work whose runtime is still alive keeps its slot in ``reconciling``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Coroutine, Mapping, TypeVar

from kiro_crew.config.sections_shared_crew import EffectiveLimits
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.shared_crew import admission_parked as _parked
from kiro_crew.shared_crew import admission_runtime as _runtime
from kiro_crew.shared_crew import admission_settle as _settle
from kiro_crew.shared_crew import containment as _containment
from kiro_crew.shared_crew.accounting import SessionCreateRate, StorageAccount
from kiro_crew.shared_crew.admission import (
    CAPACITY_HOLDING,
    TERMINAL,
    AcceptedWork,
    AdmissionRequest,
    CapacityResponse,
    CapacityScope,
    ContainmentKind,
    InvalidTransition,
    Liveness,
    OperationKind,
    OperationState,
    ReasonCode,
    ResourceEstimate,
    RuntimeOwnershipRef,
    ScopeUsage,
    StaleRevision,
    error_body,
    http_status,
    retry_after_header,
)
from kiro_crew.shared_crew.admission_http import (
    IDEMPOTENCY_HEADER,
    coded_json_response,
    idempotency_key_of,
    request_digest,
)
from kiro_crew.shared_crew.admission_parked import MERGED_INTO_KEY
from kiro_crew.shared_crew.admission_runtime import (
    SLICE_IDLE_SECONDS,
)
from kiro_crew.shared_crew.admission_runtime import systemd_reap as _systemd_reap
from kiro_crew.shared_crew.admission_runtime import systemd_unit_liveness as _systemd_unit_liveness
from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.contracts import EnforcementLevel, Principal, Transport
from kiro_crew.shared_crew.identity import actor_of
from kiro_crew.shared_crew.internal_caller import (  # noqa: F401 -- public import path
    ATTESTED_SESSION_KEY,
    CALLER_SESSION_HEADER,
    CALLER_TOKEN_HEADER,
    attest_internal_session,
)
from kiro_crew.shared_crew.scheduler import (
    Decision,
    FairScheduler,
    SchedulerLimits,
    Ticket,
    WorkClass,
)
from kiro_crew.shared_crew.work_store import IdempotencyConflict, WorkStore, new_work_id

logger = logging.getLogger(__name__)

T = TypeVar("T")
S = OperationState

#: The bounded owner of automation that records no initiator.
AUTOMATION_PRINCIPAL = Principal.system("automation")

#: Refusal code when the durable store is unavailable (no ``ReasonCode`` names it).
TASK_STORE_UNAVAILABLE_CODE = "task_store_unavailable"

#: ``extra`` key naming why reconciliation ended a record, for operators.
RECONCILE_DETAIL_KEY = "reconcile_detail"
#: ``extra`` key naming why ``failed`` work failed (bounded, no prompt text).
FAILURE_DETAIL_KEY = "failure_detail"
RECONCILE_RESTARTED_BEFORE_START = "host_restarted_before_start"
RECONCILE_RAN_BEFORE_RESTART = "host_restarted_while_running"
#: ``reconcile_detail`` of work the host's own stop ended.
STOPPED_BEFORE_START = "host_stopped_before_start"
STOPPED_WHILE_RUNNING = "host_stopped_while_running"
#: ``extra`` key counting the pre-restart runtime scopes reconciliation stopped.
ORPHANS_REAPED_KEY = "orphans_reaped"
#: ``extra`` key marking a message parked behind its session's running turn.
PARKED_KEY = "parked"

#: Operations whose work is a child of running work.
CHILD_OPERATIONS = frozenset({OperationKind.SUBAGENT})

#: Polling hints sent as ``Retry-After`` with each refusal.
_RETRY_AFTER: Mapping[ReasonCode, int] = {
    ReasonCode.PRINCIPAL_QUEUE_FULL: 10,
    ReasonCode.CREW_QUEUE_FULL: 10,
    ReasonCode.PRINCIPAL_ACTIVE_LIMIT: 10,
    ReasonCode.CREW_ACTIVE_LIMIT: 10,
    ReasonCode.STORAGE_LIMIT: 300,
    ReasonCode.CONTAINMENT_UNAVAILABLE: 30,
}

_SESSION_BINDINGS_MAX = 20000
#: Run namespaces whose workers use ``<bound key>:<suffix>`` session keys; an
#: unbound worker key inherits its run's binding (``AdmissionService._binding``).
_WORKER_SUBKEY_NAMESPACES = (
    "taskrunner:",
    "cron:",
    "wf:",
    "wf-pool:",
    "wf-unpooled:",
    "wf-worker:",
)
_QUEUE_IDS_MAX = 4096
#: Unattached work ended before its execution started, remembered so the late
#: start can be stopped (bounded, in memory).
_ENDED_UNATTACHED_MAX = 4096
#: How long a containment probe answers before it is taken again.
_REPORT_TTL_SECONDS = 60.0
#: Bound compare-and-set retries while persisting a stopped turn's terminal state.
_SETTLE_ATTEMPTS = 3


class AdmissionEnded(Exception):
    """The work ended before it ran (expired, cancelled, revalidation refused)."""

    def __init__(self, work: AcceptedWork) -> None:
        super().__init__(work.state.value)
        self.work = work


@dataclass
class _Live:
    work: AcceptedWork
    ticket: Ticket
    request: AdmissionRequest
    admitted: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_cb: Callable[[], Any] | None = None
    cancel_reason: ReasonCode | None = None
    root_work_id: str = ""
    children: set[str] = field(default_factory=set)
    #: The work's own execution finished; capacity may still wait on children.
    self_stopped: bool = False
    #: Reconciliation: runtime ownership not yet proven stopped.
    awaiting_proof: bool = False
    #: Set by :meth:`AdmissionService.note_failure`: the work returned, but failed.
    failure: str = ""
    #: Queued messages a session drain merged into this work's turn.
    followers: list[str] = field(default_factory=list)
    #: This work is a queued message merged into another work's turn.
    merged_into: str = ""
    #: Reconciliation: pre-restart runtime scopes asked to stop.
    reaped: int = 0
    #: Reconciliation: the runtime scopes found alive under the work's slice.
    orphan_scopes: tuple[str, ...] = ()
    #: A terminal write the store refused: (last durable record, target, reason,
    #: extra). The slot stays held until ``admission_settle`` lands it.
    unpersisted: tuple[AcceptedWork, OperationState, ReasonCode | None, dict[str, Any]] | None = (
        None
    )
    #: The execution a failed write asked to stop; the slot stays held until the
    #: child probe reports it ended (``admission_settle.confirm_stopped``).
    stopping_child: str = ""


@dataclass
class Admission:
    """The answer to one admission call. See the module docstring."""

    service: AdmissionService | None
    decision: str
    work: AcceptedWork | None = None
    reason: ReasonCode | None = None
    capacity: CapacityResponse | None = None
    detail: str = ""
    store_unavailable: bool = False
    replayed: bool = False

    @property
    def passthrough(self) -> bool:
        return self.decision == "passthrough"

    @property
    def rejected(self) -> bool:
        return self.decision == Decision.REJECTED.value

    @property
    def queued(self) -> bool:
        return self.decision == Decision.QUEUED.value

    @property
    def admitted(self) -> bool:
        return self.decision == Decision.ADMITTED.value

    def refusal_body(self) -> dict[str, Any]:
        if self.store_unavailable:
            return {
                "error": "The durable work queue is unavailable.",
                "code": TASK_STORE_UNAVAILABLE_CODE,
            }
        reason = self.reason or ReasonCode.CREW_ACTIVE_LIMIT
        body = error_body(
            reason,
            self.detail or reason.value.replace("_", " "),
            capacity=self.capacity,
            current=self.work if reason is ReasonCode.IDEMPOTENCY_CONFLICT else None,
        )
        return body

    def refusal_status(self) -> int:
        if self.store_unavailable:
            return 503
        return http_status(self.reason or ReasonCode.CREW_ACTIVE_LIMIT)

    def refusal_headers(self) -> dict[str, str]:
        return retry_after_header(self.capacity)

    def refusal_response(self) -> Any:
        return coded_json_response(
            self.refusal_body(), status=self.refusal_status(), headers=self.refusal_headers()
        )

    def public_work(self) -> dict[str, Any] | None:
        return None if self.work is None else self.work.to_public_dict()

    async def wait_admitted(self) -> AcceptedWork:
        """Wait until the work holds a slot. :class:`AdmissionEnded` if it ended first."""
        if self.passthrough or self.service is None or self.work is None:
            raise RuntimeError("no accepted work to wait for")
        return await self.service.wait_admitted(self.work.work_id)

    async def run(
        self,
        coro: Coroutine[Any, Any, T],
        *,
        revalidate: Callable[[], Awaitable[ReasonCode | None]] | None = None,
    ) -> T:
        """Run *coro* as this work. See :meth:`AdmissionService.run`."""
        if self.passthrough or self.service is None:
            return await coro
        if self.work is None or self.rejected:
            coro.close()
            raise RuntimeError("refused work cannot run")
        if self.replayed:
            # A retry of accepted work answers the first result; it never runs twice.
            coro.close()
            current = await self.service.current_work(self.work.work_id)
            raise AdmissionEnded(current or self.work)
        return await self.service.run(self.work.work_id, coro, revalidate=revalidate)

    async def attach_child(
        self, execution_id: str, *, session_key: str = "", also: tuple[str, ...] = ()
    ) -> None:
        """Mark an admitted child as started under *execution_id*.

        *session_key* and every key in *also* are bound to the child's principal.
        """
        if self.passthrough or self.service is None or self.work is None:
            return
        await self.service.attach_child(
            self.work.work_id, execution_id, session_key=session_key, also=also
        )

    async def abandon(self, *, reason: ReasonCode | None = None) -> None:
        """An admitted child never started (the spawn failed): end it and free its slot."""
        if self.passthrough or self.service is None or self.work is None:
            return
        await self.service.abandon(self.work.work_id, reason=reason)


Relauncher = Callable[[Mapping[str, Any], Admission], Awaitable[None]]
LivenessProbe = Callable[[RuntimeOwnershipRef], Liveness]
ChildProbe = Callable[[str], "OperationState | None"]
CancelHook = Callable[[AcceptedWork], Any]


#: Told of a record reconciliation ended, with the payload it was accepted with
#: (read before the terminal write clears it), or None when none was stored.
ReconcileListener = Callable[[AcceptedWork, "Mapping[str, Any] | None"], Any]
Reaper = Callable[[RuntimeOwnershipRef], int]


class AdmissionService:
    """Principal- and Crew-level fair admission over durable accepted work."""

    def __init__(
        self,
        store: WorkStore | None,
        *,
        limits: EffectiveLimits,
        clock: Callable[[], float] = time.time,
        containment_probe: Callable[[], _containment.ContainmentReport] | None = None,
        slices: _containment.SliceManager | None = None,
        liveness: LivenessProbe = _systemd_unit_liveness,
        reaper: Reaper | None = None,
        child_probe: ChildProbe | None = None,
        storage: StorageAccount | None = None,
        slice_busy: Callable[[str], "list[str] | None"] | None = None,
    ) -> None:
        self._store = store
        self._limits = limits
        self._clock = clock
        self._scheduler = FairScheduler(_scheduler_limits(limits), clock=clock)
        self._containment_probe = containment_probe or (
            lambda: _containment.probe_containment(
                hard_limits=self._limits.hard_limits,
                slices_enabled=self._limits.containment_enabled,
            )
        )
        self._report: _containment.ContainmentReport | None = None
        self._report_at = 0.0
        self._slices = slices
        self._liveness = liveness
        # The real reaper only pairs with the real liveness probe: an injected
        # probe (a test, a host without systemd) never stops a host unit.
        self._reaper: Reaper = reaper or (
            _systemd_reap if liveness is _systemd_unit_liveness else (lambda _ref: 0)
        )
        self._slice_busy = slice_busy or (
            lambda name: _containment.populated_scopes(
                name, runner=getattr(slices, "_runner", _containment._run)
            )
        )
        self._slice_idle: dict[str, float] = {}
        self._slice_lock = asyncio.Lock()
        self._shutting_down: Callable[[], bool] = lambda: False
        self._parked_present: Callable[[str], bool] | None = None
        self._reconcile_listener: ReconcileListener | None = None
        self._queue_ids: OrderedDict[str, str] = OrderedDict()
        self._ended_unattached: OrderedDict[str, tuple[AcceptedWork, CancelHook | None]] = (
            OrderedDict()
        )
        self._child_probe = child_probe
        self._storage = storage or (StorageAccount(store.tasks, clock=clock) if store else None)
        self._create_rate = SessionCreateRate(limits.session_creates_per_minute)
        self._live: dict[str, _Live] = {}
        self._sessions: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._relaunchers: dict[OperationKind, Relauncher] = {}
        self._probes: dict[OperationKind, ChildProbe] = {}
        self._cancels: dict[OperationKind, CancelHook] = {}
        self._lock = asyncio.Lock()
        self._ready = False
        self._promotions: set[asyncio.Task[None]] = set()
        self._relaunches: set[asyncio.Future[None]] = set()

    # -- configuration --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._limits.enabled

    @property
    def limits(self) -> EffectiveLimits:
        return self._limits

    @property
    def scheduler(self) -> FairScheduler:
        return self._scheduler

    async def set_limits(self, limits: EffectiveLimits) -> None:
        async with self._slice_lock:
            await _containment.reset_disabled_instance_quota(self._slices, limits)
            self._limits = limits
        self._create_rate.set_rate(limits.session_creates_per_minute)
        self._report = None
        if _SERVICE is self:
            _containment.publish_report(self.published_report())
        self._schedule_promotions(self._scheduler.set_limits(_scheduler_limits(limits)))

    def set_child_probe(self, probe: ChildProbe | None) -> None:
        self._child_probe = probe

    def set_shutdown_probe(self, probe: Callable[[], bool] | None) -> None:
        """Whether the host process is stopping; work it interrupts is not ``completed``."""
        self._shutting_down = probe or (lambda: False)

    def set_parked_probe(self, probe: Callable[[str], bool] | None) -> None:
        """Whether a parked message's queue entry still exists (in memory, on-loop)."""
        self._parked_present = probe

    def set_reconcile_listener(self, listener: ReconcileListener | None) -> None:
        """Told of each record reconciliation ended, so its session can say so."""
        self._reconcile_listener = listener

    def stopping(self) -> bool:
        try:
            return bool(self._shutting_down())
        except Exception:  # noqa: BLE001 -- a broken probe never ends work
            return False

    def register_probe(self, operation: OperationKind, probe: ChildProbe | None) -> None:
        """Answer the state of *operation*'s external runs by execution id (off-loop)."""
        if probe is None:
            self._probes.pop(operation, None)
        else:
            self._probes[operation] = probe

    def _probe_for(self, live: _Live) -> ChildProbe | None:
        if live.ticket.work_class is WorkClass.CHILD:
            return self._child_probe
        return self._probes.get(live.work.operation)

    def register_relauncher(self, operation: OperationKind, fn: Relauncher) -> None:
        self._relaunchers[operation] = fn

    def policy_version(self) -> str:
        """Changes when a setting that decides whether queued work may start changes."""
        lim = self._limits
        return request_digest(lim.enabled, lim.hard_limits, lim.containment_enabled)[:16]

    async def containment_report(self, *, refresh: bool = False) -> _containment.ContainmentReport:
        stale = time.monotonic() - self._report_at >= _REPORT_TTL_SECONDS
        if self._report is None or refresh or stale:
            try:
                self._report = await asyncio.to_thread(self._containment_probe)
            except Exception as exc:  # noqa: BLE001 -- a raising probe proves nothing
                logger.warning("shared crew: containment probe raised", exc_info=True)
                self._report = _containment.failed_report(
                    hard_limits=self._limits.hard_limits,
                    detail=f"containment probe raised {type(exc).__name__}",
                )
            self._report_at = time.monotonic()
            if _SERVICE is self:
                _containment.publish_report(self.published_report())
        return self._report

    def published_report(self) -> _containment.ContainmentReport | None:
        """The report ``runtime_start``'s backstop reads; None when it refuses nothing."""
        if self.enabled:
            if self._report is None and self._limits.hard_limits:
                # Not probed yet: hard mode refuses until containment is proven.
                return _containment.failed_report(
                    hard_limits=True, detail="containment not probed yet"
                )
            return self._report
        if self._limits.hard_limits:
            return _containment.admission_off_report()
        return None

    def cached_containment_report(self) -> _containment.ContainmentReport | None:
        return self._report

    # -- session bindings (budget inheritance) -------------------------------

    def bind_session(self, session_key: str, budget_key: str, root_work_id: str = "") -> None:
        if not session_key:
            return
        self._sessions[session_key] = (budget_key, root_work_id)
        self._sessions.move_to_end(session_key)
        while len(self._sessions) > _SESSION_BINDINGS_MAX:
            self._sessions.popitem(last=False)

    def _binding(self, session_key: str) -> tuple[str, str] | None:
        """*session_key*'s binding, else the nearest bound ``<key>:`` ancestor's.

        A run's workers use sub-keys of the key it was bound under
        (``taskrunner:<id>:runtime``, a stateless cron fire's ``cron:<id>:<run>``),
        so they inherit its principal and root instead of reading as unbound.
        """
        bound = self._sessions.get(session_key)
        if bound is not None or not session_key.startswith(_WORKER_SUBKEY_NAMESPACES):
            return bound
        key = session_key
        while bound is None and key.count(":") > 1:
            key = key.rsplit(":", 1)[0]
            bound = self._sessions.get(key)
        return bound

    def budget_for_session(self, session_key: str) -> str | None:
        """The principal whose work is (or last was) running in *session_key*. In memory."""
        bound = self._binding(session_key)
        return bound[0] if bound else None

    def root_for_session(self, session_key: str) -> str:
        bound = self._binding(session_key)
        return bound[1] if bound else ""

    # -- admission --------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """Reconcile durable work once per process, before anything new is admitted."""
        if self._ready or not self.enabled:
            return
        async with self._lock:
            if self._ready:
                return
            if self._store is not None:
                await self._reconcile_locked()
            self._ready = True

    def _refusal(
        self,
        reason: ReasonCode,
        budget_key: str,
        *,
        scope: CapacityScope | None = None,
        detail: str = "",
        work: AcceptedWork | None = None,
        retry_after: int | None = None,
    ) -> Admission:
        return Admission(
            self,
            Decision.REJECTED.value,
            work=work,
            reason=reason,
            capacity=self.capacity(
                budget_key,
                reason=reason,
                limiting_scope=scope,
                retry_after=retry_after if retry_after is not None else _RETRY_AFTER.get(reason),
            ),
            detail=detail,
        )

    async def admit(
        self,
        *,
        budget_key: str,
        session_key: str,
        operation: OperationKind,
        idempotency_key: str,
        digest: str = "",
        request_bytes: int = 0,
        payload: Mapping[str, Any] | None = None,
        parent_execution_id: str = "",
        estimate: ResourceEstimate | None = None,
        allow_queue: bool = True,
        park: bool = False,
    ) -> Admission:
        """Admit, queue or refuse one root execution charged to *budget_key*.

        *park* accepts work that waits on its own session's running turn rather
        than on capacity (a message sent to a busy session): it is bounded by
        the same queue depths and request size and counts as queued, but it is
        not dispatched until that session's drain calls :meth:`start_parked`.
        """
        if not self.enabled:
            if self._limits.hard_limits:
                # Hard-limit mode without the admission that applies the limits.
                return self._refusal(
                    ReasonCode.CONTAINMENT_UNAVAILABLE,
                    budget_key,
                    detail="containment unavailable: " + _containment.REASON_ADMISSION_OFF,
                )
            return Admission(self, "passthrough")
        await self.ensure_ready()
        work_class = WorkClass.CHILD if operation in CHILD_OPERATIONS else WorkClass.ROOT
        now = self._clock()
        lim = self._limits
        expires_at = now + lim.queue_expiry_seconds
        try:
            request = AdmissionRequest(
                idempotency_key=idempotency_key,
                budget_key=budget_key,
                session_key=session_key,
                operation=operation,
                submitted_at=now,
                expires_at=expires_at,
                policy_version=self.policy_version(),
                parent_execution_id=parent_execution_id,
                estimate=estimate or ResourceEstimate(),
                request_digest=digest,
            )
        except ContractError as exc:
            return Admission(
                self, Decision.REJECTED.value, reason=ReasonCode.REQUEST_TOO_LARGE, detail=str(exc)
            )
        if self._store is None:
            return Admission(self, Decision.REJECTED.value, store_unavailable=True)
        async with self._lock:
            existing = await self._existing(request)
            if existing is not None:
                return existing
            if request_bytes > lim.max_request_bytes:
                return self._refusal(ReasonCode.REQUEST_TOO_LARGE, budget_key)
            report = await self.containment_report()
            if report.refusal_reason is not None:
                return self._refusal(
                    ReasonCode.CONTAINMENT_UNAVAILABLE,
                    budget_key,
                    detail=f"containment unavailable: {report.refusal_reason}",
                )
            if self._storage is not None and await self._storage.would_exceed(
                budget_key, request_bytes, lim.principal_storage_bytes
            ):
                return self._refusal(ReasonCode.STORAGE_LIMIT, budget_key)
            work_id = new_work_id()
            ticket = Ticket(
                work_id=work_id,
                budget_key=budget_key,
                session_key=session_key,
                work_class=work_class,
                slots=request.estimate.slots,
                weight=request.estimate.weight,
                expires_at=expires_at,
            )
            if park:
                result = self._scheduler.park(ticket)
            else:
                result = self._scheduler.submit(ticket, allow_queue=allow_queue)
            if result.decision is Decision.REJECTED:
                return self._refusal(
                    result.reason or ReasonCode.CREW_QUEUE_FULL,
                    budget_key,
                    scope=result.limiting_scope,
                )
            state = S.ADMITTED if result.decision is Decision.ADMITTED else S.QUEUED
            root = self.root_for_session(session_key) if work_class is WorkClass.CHILD else ""
            accept_extra: dict[str, Any] = {"root_work_id": root} if root else {}
            if park:
                accept_extra[PARKED_KEY] = True
            handed_off = False
            try:
                if (
                    lim.hard_limits
                    and not park
                    and not await self.prepare_principal_slice(budget_key)
                ):
                    return self._refusal(ReasonCode.CONTAINMENT_UNAVAILABLE, budget_key)
                try:
                    work, inserted = await self._store.accept(
                        request,
                        state=state,
                        payload=payload,
                        work_id=work_id,
                        deadline_at=(
                            self._deadline_for(work_class, now) if state is S.ADMITTED else None
                        ),
                        extra=accept_extra or None,
                    )
                except IdempotencyConflict as exc:
                    return Admission(
                        self,
                        Decision.REJECTED.value,
                        work=exc.current,
                        reason=ReasonCode.IDEMPOTENCY_CONFLICT,
                        capacity=self.capacity(budget_key, reason=ReasonCode.IDEMPOTENCY_CONFLICT),
                    )
                except Exception:
                    logger.warning("shared crew: accepting work failed", exc_info=True)
                    return Admission(self, Decision.REJECTED.value, store_unavailable=True)
                if not inserted:
                    return self._replay(work)
                live = _Live(work=work, ticket=ticket, request=request, root_work_id=root)
                self._live[work_id] = live
                if root and root in self._live:
                    self._live[root].children.add(work_id)
                if state is S.ADMITTED:
                    live.admitted.set()
                if request_bytes > 0 and self._storage is not None:
                    try:
                        await self._storage.charge(budget_key, request_bytes)
                    except Exception:
                        logger.debug("shared crew: storage charge failed", exc_info=True)
                handed_off = True
            finally:
                if not handed_off:
                    from kiro_crew.dashboard.chat_utils import run_to_completion

                    try:
                        await run_to_completion(self._abandon_reservation(ticket))
                    finally:
                        self._undo(ticket)
                self._schedule_promotions(result.also_admitted)
        return Admission(
            self,
            Decision.ADMITTED.value if state is S.ADMITTED else Decision.QUEUED.value,
            work=work,
            reason=result.reason,
            capacity=self.capacity(
                budget_key, reason=result.reason, limiting_scope=result.limiting_scope
            ),
        )

    async def _abandon_reservation(self, ticket: Ticket) -> None:
        """Settle this reservation's write before returning its unclaimed capacity."""
        assert self._store is not None
        # The same writer serializes this read after an interrupted accept.
        work = await self._store.get(ticket.work_id)
        if work is not None and work.state not in TERMINAL:
            live = self._live.get(ticket.work_id)
            if live is not None:
                await self._end(live, S.CANCELLED, reason=None)
            else:
                await self._store.advance(work, S.CANCELLED)

    async def admit_child(
        self,
        *,
        parent_session_key: str,
        fallback_budget: str,
        operation: OperationKind = OperationKind.SUBAGENT,
        idempotency_key: str | None = None,
        digest: str = "",
    ) -> Admission:
        """Admit a child of the work running in *parent_session_key*, charged to its initiator."""
        budget = self.budget_for_session(parent_session_key) or fallback_budget
        return await self.admit(
            budget_key=budget,
            session_key=parent_session_key or "subagent",
            operation=operation,
            idempotency_key=idempotency_key or "srv-" + new_work_id(),
            digest=digest,
            parent_execution_id=self.root_for_session(parent_session_key),
        )

    def check_session_create(self, budget_key: str) -> Admission | None:
        """A refusal when *budget_key* is over its session-creation rate, else None."""
        if not self.enabled:
            return None
        allowed, retry = self._create_rate.allow(budget_key)
        if allowed:
            return None
        return self._refusal(ReasonCode.SESSION_CREATE_RATE, budget_key, retry_after=retry)

    async def charge_storage(self, budget_key: str, nbytes: int) -> None:
        if self.enabled and self._storage is not None and nbytes:
            await self._storage.charge(budget_key, nbytes)

    async def _existing(self, request: AdmissionRequest) -> Admission | None:
        return await self._existing_scope(request.scope, request.request_digest, request.budget_key)

    async def _existing_scope(self, scope: str, digest: str, budget_key: str) -> Admission | None:
        assert self._store is not None
        rows = await self._store.tasks.run(self._store.tasks.work_by_scope, scope)
        if rows is None:
            return None
        work = await self._store.get(rows)
        if work is None:
            return None
        if work.request_digest != digest:
            return Admission(
                self,
                Decision.REJECTED.value,
                work=work,
                reason=ReasonCode.IDEMPOTENCY_CONFLICT,
                capacity=self.capacity(budget_key, reason=ReasonCode.IDEMPOTENCY_CONFLICT),
            )
        return self._replay(work)

    async def lookup(
        self, *, budget_key: str, session_key: str, idempotency_key: str, digest: str
    ) -> Admission | None:
        """The replay (or conflict) a send under this key gets, or None when it is new.

        Reads only; nothing is accepted. A send handler asks this before it
        writes anything, so a retry of accepted work writes no second row.
        """
        if not self.enabled or self._store is None:
            return None
        await self.ensure_ready()
        from kiro_crew.shared_crew.admission import idempotency_scope

        try:
            scope = idempotency_scope(budget_key, session_key, idempotency_key)
        except ContractError:
            return None
        try:
            return await self._existing_scope(scope, digest, budget_key)
        except Exception:
            logger.warning("shared crew: idempotency lookup failed", exc_info=True)
            return None

    def note_queue_id(self, work_id: str, queue_id: str) -> None:
        """Remember which session-queue entry carries parked *work_id* (bounded, in memory)."""
        if not work_id or not queue_id:
            return
        self._queue_ids[work_id] = queue_id
        self._queue_ids.move_to_end(work_id)
        while len(self._queue_ids) > _QUEUE_IDS_MAX:
            self._queue_ids.popitem(last=False)

    def queue_id_for(self, work_id: str) -> str:
        return self._queue_ids.get(work_id, "")

    async def start_parked(self, work_ids: list[str], key: tuple = ()) -> Admission | None:
        """Hand a session drain's parked messages to the fair queue as one turn.

        The first live parked id becomes the turn's work; merged ones end with its
        outcome. None when none is still parked here, or the turn's (digest, budget) *key* differs.
        """
        if not self.enabled or self._store is None:
            return None
        refused = await self._cancelled_parked(work_ids)
        if refused is not None:
            for work_id in work_ids:
                if (live := self._live.get(work_id)) is not None:
                    await self._end(live, S.CANCELLED, reason=refused.reason)
            return refused
        primary: _Live | None = None
        followers: list[_Live] = []
        for work_id in work_ids:
            live = self._live.get(work_id)
            if live is None or live.work.state is not S.QUEUED:
                continue
            if not self._scheduler.is_parked(work_id):
                continue
            if primary is None:
                primary = live
            else:
                followers.append(live)
        if primary is None:
            return await self._cancelled_parked(work_ids)
        if await _parked.dropped_as_changed(self, [primary, *followers], key):
            return None
        now = self._clock()
        result = self._scheduler.submit_parked(
            primary.work.work_id, expires_at=now + self._limits.queue_expiry_seconds
        )
        if result is None:
            return None
        for follower in followers:
            self._scheduler.cancel_queued(follower.work.work_id)
            follower.merged_into = primary.work.work_id
            primary.followers.append(follower.work.work_id)
        if result.decision is Decision.REJECTED:
            await self._end(primary, S.CANCELLED, reason=result.reason)
            return Admission(
                self,
                Decision.REJECTED.value,
                work=primary.work,
                reason=result.reason,
                capacity=self.capacity(primary.work.budget_key, reason=result.reason),
            )
        if result.decision is Decision.ADMITTED:
            self._schedule_promotions((result.ticket,) + tuple(result.also_admitted))
        else:
            self._schedule_promotions(result.also_admitted)
        return Admission(
            self,
            result.decision.value,
            work=primary.work,
            reason=result.reason,
            capacity=self.capacity(primary.work.budget_key, reason=result.reason),
        )

    async def _cancelled_parked(self, work_ids: list[str]) -> Admission | None:
        """See :func:`admission_parked.cancelled_parked`."""
        return await _parked.cancelled_parked(self, work_ids)

    async def _settle_followers(self, live: _Live) -> None:
        """See :func:`admission_parked.settle_followers`."""
        await _parked.settle_followers(self, live)

    def _replay(self, work: AcceptedWork) -> Admission:
        live = self._live.get(work.work_id)
        current = live.work if live is not None else work
        decision = Decision.QUEUED.value if current.state is S.QUEUED else Decision.ADMITTED.value
        return Admission(
            self,
            decision,
            work=current,
            capacity=self.capacity(current.budget_key),
            replayed=True,
        )

    def _undo(self, ticket: Ticket) -> None:
        if self._scheduler.cancel_queued(ticket.work_id) is None:
            self._schedule_promotions(self._scheduler.release(ticket.work_id).admitted)

    def _deadline_for(self, work_class: WorkClass, now: float) -> float:
        lim = self._limits
        span = (
            lim.child_deadline_seconds
            if work_class is WorkClass.CHILD
            else lim.root_deadline_seconds
        )
        return now + span

    # -- promotion of queued work ------------------------------------------------

    def _schedule_promotions(self, tickets: tuple[Ticket, ...] | list[Ticket]) -> None:
        for ticket in tickets:
            try:
                task = asyncio.get_running_loop().create_task(self._promote(ticket.work_id))
            except RuntimeError:
                continue
            self._promotions.add(task)
            task.add_done_callback(self._promotions.discard)

    async def _promote(self, work_id: str) -> None:
        live = self._live.get(work_id)
        if live is None or self._store is None:
            return
        if live.work.state is not S.QUEUED:
            live.admitted.set()
            return
        if self._clock() >= live.ticket.expires_at:
            await self._end(live, S.EXPIRED, reason=ReasonCode.QUEUE_EXPIRED)
            return
        try:
            live.work = await self._store.advance(
                live.work,
                S.ADMITTED,
                deadline_at=self._deadline_for(live.ticket.work_class, self._clock()),
            )
        except (StaleRevision, InvalidTransition):
            current = await self._store.get(work_id)
            if current is not None:
                live.work = current
        except Exception:
            self._fail_closed(live, "promotion")
            return
        live.admitted.set()

    async def drain_promotions(self) -> None:
        """Await every in-flight promotion write (tests and shutdown)."""
        while self._promotions:
            await asyncio.gather(*list(self._promotions), return_exceptions=True)

    async def drain_relaunches(self) -> None:
        """Await every relaunched execution (tests and shutdown)."""
        while self._relaunches:
            await asyncio.gather(*list(self._relaunches), return_exceptions=True)

    async def wait_admitted(self, work_id: str) -> AcceptedWork:
        live = self._live.get(work_id)
        if live is None:
            raise KeyError(work_id)
        while True:
            if live.work.state in TERMINAL:
                raise AdmissionEnded(live.work)
            if live.admitted.is_set() and live.work.state is not S.QUEUED:
                return live.work
            waiters = [
                asyncio.ensure_future(live.admitted.wait()),
                asyncio.ensure_future(live.ended.wait()),
            ]
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()

    # -- running ---------------------------------------------------------------

    async def run(
        self,
        work_id: str,
        coro: Coroutine[Any, Any, T],
        *,
        revalidate: Callable[[], Awaitable[ReasonCode | None]] | None = None,
    ) -> T:
        """Wait for a slot, revalidate, run *coro* in its principal's slice, settle the record.

        Raises :class:`AdmissionEnded` (with *coro* closed, never started) when
        the work expired, was cancelled or failed revalidation before it ran.
        """
        live = self._live.get(work_id)
        if live is None or self._store is None:
            coro.close()
            current = await self._store.get(work_id) if self._store is not None else None
            if current is None:
                raise KeyError(work_id)
            # A replayed handle for work that already ran: never run it twice.
            raise AdmissionEnded(current)
        if live.work.state not in (S.QUEUED, S.ADMITTED):
            coro.close()
            raise AdmissionEnded(live.work)
        try:
            await self.wait_admitted(work_id)
            if self.stopping():
                # The host is going away: work that never started is cancelled, not run.
                await self._end(
                    live,
                    S.CANCELLED,
                    reason=None,
                    extra={RECONCILE_DETAIL_KEY: STOPPED_BEFORE_START},
                )
                raise AdmissionEnded(live.work)
            refusal = await self._revalidate(live, revalidate)
            if refusal is not None:
                terminal = S.EXPIRED if refusal is ReasonCode.QUEUE_EXPIRED else S.CANCELLED
                await self._end(live, terminal, reason=refusal)
                raise AdmissionEnded(live.work)
            unit = await self.prepare_principal_slice(live.work.budget_key)
            if self._limits.hard_limits and not unit:
                await self._end(live, S.CANCELLED, reason=ReasonCode.CONTAINMENT_UNAVAILABLE)
                raise AdmissionEnded(live.work)
            runtime = RuntimeOwnershipRef(
                execution_id=work_id,
                root_execution_id=live.root_work_id or work_id,
                parent_execution_id=live.request.parent_execution_id,
                containment=ContainmentKind.SYSTEMD_SCOPE if unit else ContainmentKind.NONE,
                containment_unit=unit,
                liveness=Liveness.ALIVE,
                reconciled_at=None,
            )
            try:
                live.work = await self._store.advance(live.work, S.RUNNING, runtime=runtime)
            except (StaleRevision, InvalidTransition):
                current = await self._store.get(work_id)
                if current is not None:
                    live.work = current
                raise AdmissionEnded(live.work) from None
        except BaseException as exc:
            # Every path that does not start the work closes its coroutine here.
            coro.close()
            await _parked.end_unstarted(self, live, exc)
            raise
        self.bind_session(live.work.session_key, live.work.budget_key, work_id)
        from kiro_crew.shared_crew.turn_gate import mark_admitted

        with mark_admitted(work_id):
            inner = _containment.create_task_in_scope(coro, live.work.budget_key)
        live.cancel_cb = inner.cancel
        outcome: OperationState = S.COMPLETED
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            if not inner.done():
                inner.cancel()
                await asyncio.gather(inner, return_exceptions=True)
            outcome = S.CANCELLED
            raise
        except BaseException:
            outcome = S.FAILED
            raise
        finally:
            live.cancel_cb = None
            live.self_stopped = True
            await self._settle(live, outcome)

    async def _revalidate(
        self,
        live: _Live,
        revalidate: Callable[[], Awaitable[ReasonCode | None]] | None,
    ) -> ReasonCode | None:
        if live.cancel_reason is not None:
            return live.cancel_reason
        if live.work.state is S.QUEUED and self._clock() >= live.ticket.expires_at:
            return ReasonCode.QUEUE_EXPIRED
        if live.request.policy_version != self.policy_version():
            return ReasonCode.POLICY_CHANGED
        report = await self.containment_report()
        if report.refusal_reason is not None:
            return ReasonCode.CONTAINMENT_UNAVAILABLE
        from kiro_crew.shared_crew import access_set

        if access_set.principal_removed(live.work.budget_key):
            return ReasonCode.GRANT_REVOKED
        if revalidate is not None:
            try:
                return await revalidate()
            except Exception:
                logger.warning("shared crew: revalidation raised; refusing", exc_info=True)
                return ReasonCode.GRANT_REVOKED
        return None

    async def prepare_principal_slice(self, budget_key: str) -> str:
        """Size *budget_key*'s slice off-loop so its spawns land there; "" when not applied."""
        async with self._slice_lock:
            return await _containment.prepare_slices(
                self._slices, budget_key, self._limits, await self.containment_report()
            )

    def note_failure(self, work_id: str, detail: str) -> bool:
        """Mark running work *work_id* as failed when it settles, with *detail*.

        For work that reports its own failure and returns normally (a chat turn
        whose runtime errored writes an error row and returns). No I/O; the
        settle that follows records ``failed`` with ``runtime_error``.
        """
        live = self._live.get(work_id)
        if live is None or live.work.state is not S.RUNNING:
            return False
        live.failure = (detail or "failed")[:200]
        return True

    def note_cancelled(self, work_id: str) -> None:
        """Record a turn's acknowledged user stop without cancelling its cleanup."""
        live = self._live.get(work_id)
        if live is not None and live.work.state is S.RUNNING and live.cancel_reason is None:
            live.cancel_reason = ReasonCode.CANCELLED_BY_USER

    async def _settle(self, live: _Live, outcome: OperationState) -> None:
        """Record *live*'s terminal state and release its slot once allowed."""
        assert self._store is not None
        state = live.work.state
        target = outcome
        reason: ReasonCode | None = None
        extra: dict[str, Any] | None = None
        if target is S.COMPLETED and live.failure:
            target = S.FAILED
        if target is S.FAILED:
            reason = ReasonCode.RUNTIME_ERROR
            extra = {FAILURE_DETAIL_KEY: live.failure or "raised"}
        if live.cancel_reason is not None:
            target = S.CANCELLED
            reason = live.cancel_reason
        elif target is S.COMPLETED and state is S.RUNNING and self.stopping():
            # The host's stop ended this turn: it was interrupted, not completed.
            target = S.CANCELLED
            extra = {RECONCILE_DETAIL_KEY: STOPPED_WHILE_RUNNING}
        try:
            for _attempt in range(_SETTLE_ATTEMPTS):
                try:
                    if live.work.state is S.RUNNING and target is S.CANCELLED:
                        live.work = await self._store.advance(
                            live.work, S.CANCELLING, reason=reason
                        )
                    if live.work.state not in TERMINAL:
                        live.work = await self._store.advance(
                            live.work,
                            target,
                            reason=reason,
                            runtime=self._stopped_ref(live),
                            extra=extra,
                        )
                    break
                except (StaleRevision, InvalidTransition):
                    current = await self._store.get(live.work.work_id)
                    if current is None:
                        raise
                    live.work = current
                    if current.state in TERMINAL:
                        break
            else:
                raise RuntimeError("terminal work revision did not stabilize")
        except Exception:
            self._fail_closed(live, "settle")
        await self._settle_followers(live)
        live.ended.set()
        self._maybe_release(live)

    def _stopped_ref(self, live: _Live) -> RuntimeOwnershipRef:
        base = live.work.runtime or RuntimeOwnershipRef(execution_id=live.work.work_id)
        alive_children = sum(1 for c in live.children if c in self._live)
        return replace(
            base,
            liveness=Liveness.STOPPED,
            descendants_alive=alive_children,
            reconciled_at=self._clock(),
        )

    def _maybe_release(self, live: _Live) -> None:
        """Free *live*'s slot if it is terminal and nothing it started still runs."""
        work_id = live.work.work_id
        if (
            live.work.state not in TERMINAL
            or live.awaiting_proof
            or live.unpersisted
            or live.stopping_child
        ):
            return
        if any(c in self._live for c in live.children):
            return
        self._live.pop(work_id, None)
        self._schedule_promotions(self._scheduler.release(work_id).admitted)
        root = self._live.get(live.root_work_id) if live.root_work_id else None
        if root is not None:
            root.children.discard(work_id)
            self._maybe_release(root)

    async def _end(
        self,
        live: _Live,
        state: OperationState,
        *,
        reason: ReasonCode | None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """Move unstarted or child work straight to a terminal *state*."""
        assert self._store is not None
        work_id = live.work.work_id
        unattached = live.work.state in (S.QUEUED, S.ADMITTED) and not live.work.extra.get(
            "execution_id"
        )
        if live.work.state is S.QUEUED:
            self._scheduler.cancel_queued(work_id)
        try:
            if live.work.state is S.RUNNING and state is S.CANCELLED:
                live.work = await self._store.advance(live.work, S.CANCELLING, reason=reason)
            if live.work.state not in TERMINAL:
                stopped = self._stopped_ref(live) if live.work.runtime is not None else None
                live.work = await self._store.advance(
                    live.work, state, reason=reason, runtime=stopped, extra=extra
                )
        except (StaleRevision, InvalidTransition):
            current = await self._store.get(work_id)
            if current is not None:
                live.work = current
        except Exception:
            self._fail_closed(live, "end")
        if unattached and live.work.state in TERMINAL:
            self._ended_unattached[work_id] = (live.work, self._cancel_hook_for(live))
            while len(self._ended_unattached) > _ENDED_UNATTACHED_MAX:
                self._ended_unattached.popitem(last=False)
        await self._settle_followers(live)
        live.self_stopped = True
        live.ended.set()
        self._maybe_release(live)

    # -- children ----------------------------------------------------------------

    async def attach_child(
        self,
        work_id: str,
        execution_id: str,
        *,
        session_key: str = "",
        also: tuple[str, ...] = (),
    ) -> None:
        """Mark admitted work as started under *execution_id*.

        Work cancelled (or otherwise ended) while its start was in flight holds
        no capacity any more, so the execution that start produced is stopped
        through the operation's cancel hook rather than left running outside
        admission.
        """
        if self._store is None:
            return
        live = self._live.get(work_id)
        if live is None or live.work.state in TERMINAL:
            _parked.stop_late_start(self, work_id, execution_id)
            return
        runtime = RuntimeOwnershipRef(
            execution_id=execution_id,
            parent_execution_id=live.request.parent_execution_id,
            root_execution_id=live.root_work_id,
            liveness=Liveness.ALIVE,
        )
        try:
            live.work = await self._store.advance(
                live.work, S.RUNNING, runtime=runtime, extra={"execution_id": execution_id}
            )
        except (StaleRevision, InvalidTransition):
            if live.work.state in TERMINAL or work_id not in self._live:
                _parked.stop_late_start(self, work_id, execution_id)
            return
        except Exception:
            self._fail_closed(live, "attach", execution_id)
            raise
        for key in (session_key, *also):
            if key:
                self.bind_session(key, live.work.budget_key, live.root_work_id or work_id)

    async def abandon(self, work_id: str, *, reason: ReasonCode | None = None) -> None:
        live = self._live.get(work_id)
        if live is None:
            return
        await self._end(live, S.FAILED, reason=reason)

    # -- cancellation ------------------------------------------------------------

    async def cancel(
        self, work_id: str, *, expected_revision: int, reason: ReasonCode | None = None
    ) -> AcceptedWork:
        """Cancel *work_id* at *expected_revision* for *reason*, a user stop by default."""
        why = reason or ReasonCode.CANCELLED_BY_USER
        if self._store is None:
            raise KeyError(work_id)
        live = self._live.get(work_id)
        current = live.work if live is not None else await self._store.get(work_id)
        if current is None:
            raise KeyError(work_id)
        if current.revision != expected_revision:
            raise StaleRevision(expected_revision, current.revision)
        if current.state in TERMINAL or current.state is S.CANCELLING:
            raise InvalidTransition(current.state, S.CANCELLED)
        if live is None:
            # A durable record this process does not run (reconciling after a
            # restart). Only a legal direct move is taken.
            return await self._store.advance(current, S.CANCELLED, reason=why)
        if current.state is S.RECONCILING:
            # Being resolved against runtime ownership; its outcome is not ours to pick.
            raise InvalidTransition(current.state, S.CANCELLED)
        live.cancel_reason = why
        if live.merged_into and (primary := self._live.get(live.merged_into)) is not None:
            await self.cancel(primary.work.work_id, expected_revision=primary.work.revision)
            return live.work
        if current.state in (S.QUEUED, S.ADMITTED):
            await self._end(live, S.CANCELLED, reason=why)
            return live.work
        try:
            live.work = await self._store.advance(live.work, S.CANCELLING, reason=why)
        finally:
            self._stop_child(live)
        return live.work

    _child_cancel: CancelHook | None = None

    def set_child_cancel(self, hook: CancelHook | None) -> None:
        self._child_cancel = hook

    def register_cancel(self, operation: OperationKind, hook: CancelHook | None) -> None:
        if hook is None:
            self._cancels.pop(operation, None)
        else:
            self._cancels[operation] = hook

    def _cancel_hook_for(self, live: _Live) -> CancelHook | None:
        if live.ticket.work_class is WorkClass.CHILD:
            return self._child_cancel
        return self._cancels.get(live.work.operation)

    def _stop_child(self, live: _Live, execution_id: str = "") -> None:
        """Stop *live*'s running execution: its task, else its operation's cancel hook."""
        if live.cancel_cb is not None:
            live.cancel_cb()
            return
        hook = self._cancel_hook_for(live)
        exec_id = execution_id or str(live.work.extra.get("execution_id") or "")
        if hook is not None and exec_id:
            hook(replace(live.work, extra={**live.work.extra, "execution_id": exec_id}))

    def _fail_closed(self, live: _Live, what: str, execution_id: str = "") -> None:
        """A store write failed: end *live* in memory now, keep its slot until the write lands.

        The ticker retries the terminal write (``admission_settle``); capacity is
        released only once the store records it.
        """
        logger.warning(
            "shared crew: %s write for %s failed; ending it", what, live.work.work_id, exc_info=True
        )
        self._scheduler.cancel_queued(live.work.work_id)
        exec_id = execution_id or str(live.work.extra.get("execution_id") or "")
        _settle.mark_unpersisted(
            live,
            S.FAILED,
            ReasonCode.RUNTIME_ERROR,
            {"execution_id": exec_id} if exec_id else None,
        )
        if exec_id and self._probe_for(live) is not None:
            live.stopping_child = exec_id
        self._stop_child(live, execution_id)
        live.self_stopped = True
        live.admitted.set()
        live.ended.set()
        self._maybe_release(live)

    # -- maintenance -------------------------------------------------------------

    async def tick(self, now: float | None = None) -> dict[str, int]:
        """Expire queued work, enforce deadlines, settle finished children. Idempotent."""
        if self._store is None:
            return {}
        if not self.enabled:
            await _parked.cancel_disabled_queue(self)
        at = self._clock() if now is None else now
        counts = {"expired": 0, "deadline": 0, "children": 0, "proven": 0, "parked": 0}
        counts["parked"] = await self._sweep_parked(at)
        for ticket in self._scheduler.expire(at):
            live = self._live.get(ticket.work_id)
            if live is not None:
                await self._end(live, S.EXPIRED, reason=ReasonCode.QUEUE_EXPIRED)
                counts["expired"] += 1
        for live in list(self._live.values()):
            work = live.work
            if (
                work.state is S.RUNNING
                and work.deadline_at is not None
                and at >= work.deadline_at
                and live.cancel_reason is None
            ):
                live.cancel_reason = ReasonCode.RUN_DEADLINE
                counts["deadline"] += 1
                if live.cancel_cb is None and self._cancel_hook_for(live) is None:
                    continue
                try:
                    live.work = await self._store.advance(
                        live.work, S.CANCELLING, reason=ReasonCode.RUN_DEADLINE
                    )
                except (StaleRevision, InvalidTransition):
                    pass
                except Exception:  # noqa: BLE001 -- the child is stopped regardless
                    logger.warning(
                        "shared crew: deadline write for %s failed", work.work_id, exc_info=True
                    )
                self._stop_child(live)
        for live in list(self._live.values()):
            probe = self._probe_for(live)
            if probe is None or live.cancel_cb is not None:
                continue
            exec_id = str(live.work.extra.get("execution_id") or "")
            if live.work.state not in (S.RUNNING, S.CANCELLING, S.RECONCILING) or not exec_id:
                continue
            state = await asyncio.to_thread(probe, exec_id)
            if state is not None and state in TERMINAL:
                await self._settle_child(live, state)
                counts["children"] += 1
        for live in list(self._live.values()):
            if live.awaiting_proof and live.work.runtime is not None:
                liveness = await asyncio.to_thread(self._proof, live)
                if liveness is Liveness.STOPPED:
                    await self._finish_reconcile(live)
                    counts["proven"] += 1
        counts["persisted"] = await _settle.persist_pending(self)
        counts["stopped"] = await _settle.confirm_stopped(self)
        counts["slices"] = await self.sweep_slices(at)
        return counts

    async def _sweep_parked(self, at: float) -> int:
        """End parked messages past their expiry whose queue entry is gone."""
        return await _parked.sweep_parked(self, at)

    async def sweep_slices(self, at: float | None = None, *, force: bool = False) -> int:
        """Release per-principal slices with no live work; see ``admission_runtime``."""
        if self._slices is None:
            return 0
        return await _runtime.sweep_idle_slices(
            self._slices,
            budgets={live.work.budget_key for live in self._live.values()},
            idle=self._slice_idle,
            now=self._clock() if at is None else at,
            busy=self._slice_busy,
            force=force,
        )

    async def release_stale_slices(self) -> int:
        """Release per-principal slices an earlier gateway process left behind."""
        if self._slices is None or not self._limits.containment_enabled:
            return 0
        return await _runtime.release_stale_slices(self._slices, busy=self._slice_busy)

    async def _settle_child(self, live: _Live, state: OperationState) -> None:
        assert self._store is not None
        target = S.UNKNOWN_OUTCOME if state is S.EXPIRED else state
        try:
            if target is S.UNKNOWN_OUTCOME and live.work.state in (S.RUNNING, S.CANCELLING):
                live.work = await self._store.advance(live.work, S.RECONCILING)
            if target is S.CANCELLED and live.work.state is S.RUNNING:
                live.work = await self._store.advance(live.work, S.CANCELLING)
            live.work = await self._store.advance(
                live.work, target, reason=live.cancel_reason, runtime=self._stopped_ref(live)
            )
        except (StaleRevision, InvalidTransition):
            current = await self._store.get(live.work.work_id)
            if current is not None:
                live.work = current
        live.awaiting_proof = False
        live.self_stopped = True
        live.ended.set()
        self._maybe_release(live)

    # -- reconciliation ---------------------------------------------------------

    async def _reconcile_locked(self) -> None:
        assert self._store is not None
        rows = await self._store.list(states=[s for s in S if s not in TERMINAL], limit=100000)
        for work in rows:
            try:
                await self._reconcile_one(work)
            except Exception:
                logger.warning("shared crew: reconciling %s failed", work.work_id, exc_info=True)
        try:
            await self._store.tasks.run(self._store.purge_sync)
        except Exception:
            logger.debug("shared crew: purge failed", exc_info=True)

    async def _reconcile_one(self, work: AcceptedWork) -> None:
        assert self._store is not None
        detail_started = {RECONCILE_DETAIL_KEY: RECONCILE_RAN_BEFORE_RESTART}
        work_class = WorkClass.CHILD if work.operation in CHILD_OPERATIONS else WorkClass.ROOT
        ticket = Ticket(
            work_id=work.work_id,
            budget_key=work.budget_key,
            session_key=work.session_key,
            work_class=work_class,
            expires_at=work.expires_at,
        )
        request = AdmissionRequest(
            idempotency_key="reconcile-" + work.work_id[-24:],
            budget_key=work.budget_key,
            session_key=work.session_key,
            operation=work.operation,
            submitted_at=work.accepted_at,
            expires_at=max(work.expires_at, work.accepted_at),
            policy_version=self.policy_version(),
            request_digest=work.request_digest,
        )
        if work.state in (S.QUEUED, S.ADMITTED):
            # Proven never started: ``running`` is written before any launch.
            work = await self._store.advance(work, S.RECONCILING)
            relaunch = self._relaunchers.get(work.operation)
            payload = await self._store.tasks.run(self._store.payload_sync, work.work_id)
            if relaunch is not None and payload is not None and work.expires_at > self._clock():
                work = await self._store.advance(work, S.QUEUED)
                result = self._scheduler.submit(ticket)
                live = _Live(work=work, ticket=ticket, request=request)
                self._live[work.work_id] = live
                if result.decision is Decision.REJECTED:
                    await self._end(live, S.CANCELLED, reason=result.reason)
                    return
                if result.decision is Decision.ADMITTED:
                    self._schedule_promotions((ticket,))
                admission = Admission(self, result.decision.value, work=work)
                task: asyncio.Future[None] = asyncio.ensure_future(relaunch(payload, admission))
                self._relaunches.add(task)
                task.add_done_callback(self._relaunches.discard)
                return
            ended = await self._store.advance(
                work,
                S.CANCELLED,
                extra={RECONCILE_DETAIL_KEY: RECONCILE_RESTARTED_BEFORE_START},
            )
            self._tell_reconciled(ended, payload)
            return
        if work.state in (S.RUNNING, S.CANCELLING):
            work = await self._store.advance(work, S.RECONCILING, extra=detail_started)
        # RECONCILING: holds capacity until proven stopped.
        self._scheduler.hold(ticket)
        live = _Live(work=work, ticket=ticket, request=request, awaiting_proof=True)
        live.root_work_id = str(work.extra.get("root_work_id") or "")
        self._live[work.work_id] = live
        if work_class is WorkClass.CHILD or (
            work.operation in self._probes and work.extra.get("execution_id")
        ):
            # Its owner (the subagent queue, the task runner, the workflow
            # registry) reconciles the run; the probe settles this record.
            live.awaiting_proof = False
            return
        runtime = work.runtime or RuntimeOwnershipRef(execution_id=work.work_id)
        liveness = await asyncio.to_thread(self._liveness, runtime)
        if liveness is Liveness.STOPPED:
            await self._finish_reconcile(live)
            return
        if liveness is Liveness.ALIVE:
            # The previous process's runtime outlived it and has no reader: stop
            # it. The slot stays held until the stop is proven (``tick``), and
            # the proof watches only the scopes found now, so a later runtime of
            # the same person in the same slice does not hold this record open.
            unit = runtime.containment_unit
            if unit.endswith(".slice"):
                found = await asyncio.to_thread(self._slice_busy, unit)
                live.orphan_scopes = tuple(found or ())
            live.reaped = await asyncio.to_thread(self._reaper, runtime)
            logger.warning(
                "shared crew: %s's runtime outlived the restart; stopped %d scope(s)",
                work.work_id,
                live.reaped,
            )
            if await asyncio.to_thread(self._proof, live) is Liveness.STOPPED:
                await self._finish_reconcile(live)

    def _proof(self, live: _Live) -> Liveness:
        """Liveness of a reconciling record's runtime. Blocking: call off-loop."""
        runtime = live.work.runtime or RuntimeOwnershipRef(execution_id=live.work.work_id)
        if not live.orphan_scopes:
            return self._liveness(runtime)
        answers = [
            self._liveness(replace(runtime, containment_unit=scope)) for scope in live.orphan_scopes
        ]
        if any(a is Liveness.ALIVE for a in answers):
            return Liveness.ALIVE
        if any(a is Liveness.UNKNOWN for a in answers):
            return Liveness.UNKNOWN
        return Liveness.STOPPED

    def _tell_reconciled(
        self, work: AcceptedWork, payload: Mapping[str, Any] | None = None
    ) -> None:
        listener = self._reconcile_listener
        if listener is None:
            return
        try:
            listener(work, payload)
        except Exception:  # noqa: BLE001 -- the note is best effort
            logger.debug("shared crew: reconcile listener failed", exc_info=True)

    async def _finish_reconcile(self, live: _Live) -> None:
        """A reconciling root whose runtime is proven stopped: it may have run; never replay."""
        assert self._store is not None
        prior_cancel = live.work.reason is ReasonCode.CANCELLED_BY_USER
        target = S.CANCELLED if prior_cancel else S.UNKNOWN_OUTCOME
        try:
            # The terminal write clears the payload, so it is read first.
            payload = await self._store.tasks.run(self._store.payload_sync, live.work.work_id)
        except Exception:  # noqa: BLE001 -- the note then carries no prompt
            payload = None
        try:
            live.work = await self._store.advance(
                live.work,
                target,
                runtime=self._stopped_ref(live),
                extra={ORPHANS_REAPED_KEY: live.reaped} if live.reaped else None,
            )
            self._tell_reconciled(live.work, payload)
        except (StaleRevision, InvalidTransition):
            pass
        live.awaiting_proof = False
        live.self_stopped = True
        live.ended.set()
        self._maybe_release(live)

    # -- reads ---------------------------------------------------------------------

    def capacity(
        self,
        budget_key: str,
        *,
        reason: ReasonCode | None = None,
        limiting_scope: CapacityScope | None = None,
        retry_after: int | None = None,
    ) -> CapacityResponse:
        lim = self._limits
        if not lim.enabled:
            return CapacityResponse(
                principal=ScopeUsage(), crew=ScopeUsage(), enforcement=EnforcementLevel.NONE
            )
        principal, crew = self._scheduler.usage(budget_key)
        return CapacityResponse(
            principal=principal,
            crew=crew,
            enforcement=EnforcementLevel.BEST_EFFORT,
            limiting_scope=limiting_scope,
            reason=reason,
            retry_after_seconds=retry_after,
            queue_expiry_seconds=lim.queue_expiry_seconds,
            run_deadline_seconds=lim.root_deadline_seconds,
        )

    def live_work(self, work_id: str) -> AcceptedWork | None:
        live = self._live.get(work_id)
        return live.work if live is not None else None

    async def current_work(self, work_id: str) -> AcceptedWork | None:
        live = self.live_work(work_id)
        if live is not None or self._store is None:
            return live
        return await self._store.get(work_id)

    async def list_work(
        self, *, session_key: str | None = None, budget_key: str | None = None, limit: int = 200
    ) -> list[AcceptedWork]:
        if self._store is None:
            return []
        # Open work first, so a long finished history never hides a queued turn.
        rows = await self._store.list(
            states=set(S) - TERMINAL, session_key=session_key, budget_key=budget_key, limit=limit
        )
        room = limit - len(rows)
        if room > 0:
            rows += await self._store.list(
                states=TERMINAL, session_key=session_key, budget_key=budget_key, limit=room
            )
        return [self._live[w.work_id].work if w.work_id in self._live else w for w in rows]

    def holding_states(self) -> frozenset[OperationState]:
        return CAPACITY_HOLDING


def _scheduler_limits(lim: EffectiveLimits) -> SchedulerLimits:
    return SchedulerLimits(
        principal_active=max(1, lim.principal_active),
        principal_children=max(1, lim.principal_children),
        principal_queue=max(0, lim.principal_queue),
        crew_active=max(1, lim.crew_active),
        crew_children=max(1, lim.crew_children),
        crew_queue=max(0, lim.crew_queue),
    )


# -- the gateway's service --------------------------------------------------------

_SERVICE: AdmissionService | None = None
_SERVICE_LOCK = LoopBoundLock()
_SERVICE_LOOP: asyncio.AbstractEventLoop | None = None
_CONFIG_TTL_SECONDS = 5.0
_last_config_at = 0.0
_ticker: asyncio.Task[None] | None = None


def install_service(service: AdmissionService | None) -> None:
    """Install the process's service (gateway startup, tests). None removes it."""
    global _SERVICE, _SERVICE_LOOP, _last_config_at
    _SERVICE = service
    _last_config_at = time.monotonic()
    from kiro_crew.taskq import lanes

    if service is not None and _HOOKS != HostHooks():
        _apply_hooks(service)
    if service is None:
        _SERVICE_LOOP = None
        lanes.set_lane_resolver(None)
        _containment.install_slice_manager(None)
        _containment.publish_report(None)
        return
    _containment.publish_report(service.published_report())
    lanes.set_lane_resolver(service.budget_for_session if service.enabled else None)
    _containment.install_slice_manager(
        service._slices if service.enabled and service.limits.containment_enabled else None
    )


@dataclass
class HostHooks:
    """What the hosting gateway tells every admission service it builds."""

    shutting_down: Callable[[], bool] | None = None
    parked_present: Callable[[str], bool] | None = None
    reconciled: ReconcileListener | None = None
    #: The subagent manager's run probe and stop hook, so a service rebuilt
    #: after a restart settles and cancels recovered children before any spawn.
    child_probe: ChildProbe | None = None
    child_cancel: CancelHook | None = None


_HOOKS = HostHooks()


def install_host_hooks(hooks: HostHooks | None) -> None:
    """Install the gateway's hooks (``shared_crew.host_hooks``); None removes them."""
    global _HOOKS
    _HOOKS = hooks or HostHooks()
    if _SERVICE is not None:
        _apply_hooks(_SERVICE)


def _apply_hooks(service: AdmissionService) -> None:
    service.set_shutdown_probe(_HOOKS.shutting_down)
    service.set_parked_probe(_HOOKS.parked_present)
    service.set_reconcile_listener(_HOOKS.reconciled)
    if _HOOKS.child_probe is not None:
        service.set_child_probe(_HOOKS.child_probe)
    if _HOOKS.child_cancel is not None:
        service.set_child_cancel(_HOOKS.child_cancel)


def installed_service() -> AdmissionService | None:
    """The installed service without building one; None before first use. No I/O."""
    return _SERVICE


def _load_limits() -> EffectiveLimits:
    from kiro_crew.agent_sdk import session_start_concurrency
    from kiro_crew.config.sections_shared_crew import effective_limits, load_shared_crew_config

    return effective_limits(
        load_shared_crew_config(), auto_crew_concurrency=session_start_concurrency()
    )


def _open_default_store() -> WorkStore | None:
    from kiro_crew.config.paths import data_home
    from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable

    try:
        tasks = TaskStore(TaskStore.default_path(data_home())).open()
    except TaskStoreUnavailable:
        logger.warning("shared crew: the durable work store is unavailable", exc_info=True)
        return None
    return WorkStore(tasks)


async def current_service() -> AdmissionService:
    """Serialize first use and off-loop config refresh; disabled admission opens no store."""
    global _last_config_at, _SERVICE_LOOP
    async with _SERVICE_LOCK:
        service = _SERVICE
        loop = asyncio.get_running_loop()
        if service is not None and _SERVICE_LOOP is not loop:
            # Built on another event loop (a restarted loop, a test): its locks and
            # events belong there, so this loop gets its own service.
            service = None
        now = time.monotonic()
        if service is not None and now - _last_config_at < _CONFIG_TTL_SECONDS:
            return service
        _last_config_at = now
        try:
            limits = await asyncio.to_thread(_load_limits)
        except Exception:
            logger.warning(
                "shared crew: config unreadable; keeping the current limits", exc_info=True
            )
            if service is not None:
                return service
            raise
        if service is None:
            store = await asyncio.to_thread(_open_default_store) if limits.enabled else None
            service = AdmissionService(store, limits=limits, slices=_containment.SliceManager())
            install_service(service)
            _SERVICE_LOOP = loop
            _start_ticker(service)
            return service
        if limits != service.limits:
            if limits.enabled and service._store is None:
                store = await asyncio.to_thread(_open_default_store)
                service = AdmissionService(store, limits=limits, slices=service._slices)
                install_service(service)
                _SERVICE_LOOP = loop
                _start_ticker(service)
                return service
            await service.set_limits(limits)
            install_service(service)
            _SERVICE_LOOP = loop
            if _ticker is None or _ticker.done():
                _start_ticker(service)
        return service


def _configured_hard_limits() -> bool:
    """Whether config asks for hard limits; unreadable configuration refuses work."""
    try:
        from kiro_crew.config.sections_shared_crew import load_shared_crew_config

        cfg = load_shared_crew_config()
    except Exception:  # noqa: BLE001 -- unknown configuration cannot authorize work
        return True
    return bool(cfg.hard_limits)


def _start_ticker(service: AdmissionService, *, period: float = 1.0) -> None:
    global _ticker
    if _ticker is not None and not _ticker.done():
        _ticker.cancel()
    _ticker = None
    if not service.enabled and not service._live:
        return

    async def _loop() -> None:
        while _SERVICE is service and (service.enabled or service._live):
            try:
                await service.tick()
            except Exception:
                logger.debug("shared crew: tick failed", exc_info=True)
            await asyncio.sleep(period)

    try:
        _ticker = asyncio.get_running_loop().create_task(_loop())
    except RuntimeError:
        _ticker = None


class AdmissionRefused(Exception):
    """:func:`admit_and_run` was refused; ``admission`` carries the reason and body."""

    def __init__(self, admission: Admission) -> None:
        super().__init__((admission.reason or ReasonCode.CREW_ACTIVE_LIMIT).value)
        self.admission = admission


async def admit_and_run(
    coro: Coroutine[Any, Any, T],
    *,
    budget_key: str,
    session_key: str,
    operation: OperationKind,
    idempotency_key: str | None = None,
    digest: str = "",
    revalidate: Callable[[], Awaitable[ReasonCode | None]] | None = None,
) -> T:
    """Admit and run *coro* as one root execution; the shape a background caller needs.

    With fair admission off this is ``await coro``. A refusal closes *coro*
    unstarted and raises :class:`AdmissionRefused`; work that ends before it
    runs raises :class:`AdmissionEnded`.
    """
    service = await current_service()
    admission = await service.admit(
        budget_key=budget_key,
        session_key=session_key,
        operation=operation,
        idempotency_key=idempotency_key or "srv-" + new_work_id(),
        digest=digest,
    )
    if admission.rejected:
        coro.close()
        raise AdmissionRefused(admission)
    return await admission.run(coro, revalidate=revalidate)


def recorded_principal(value: object) -> str:
    """*value* when it is a well-formed principal id a record stored, else automation's."""
    if isinstance(value, str) and value.startswith("p_"):
        try:
            return Principal(id=value, kind=AUTOMATION_PRINCIPAL.kind).id
        except ContractError:
            pass
    return AUTOMATION_PRINCIPAL.id


def scope_principal_for_session(session_key: str) -> str | None:
    """The principal a spawn from *session_key* runs as, for the containment slice. In memory."""
    service = _SERVICE
    if service is None or not service.enabled or not session_key:
        return None
    return service.budget_for_session(session_key)


def internal_initiator(request: Any) -> str | None:
    """The person whose work runs in an internal request's proven calling session, or None.

    The internal secret proves the call came from a runtime this gateway
    started; it does not prove which session. Only a session
    :func:`attest_internal_session` proved is read, and the principal is the
    one admission bound to that session server-side (``budget_for_session``).
    """
    service = _SERVICE
    if service is None or not service.enabled:
        return None
    try:
        session = request.get(ATTESTED_SESSION_KEY)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(session, str) or not session:
        return None
    return service.budget_for_session(session)


def actor_budget(request: Any, *, automation: bool = False) -> str:
    """The budget key for *request*: its actor's principal, its initiator's, or Unknown.

    An internal request (work an agent starts from inside a turn) is charged
    to the person whose turn runs in its proven calling session, so automation
    never adds a second budget to anyone. An internal request whose session is
    unproven or holds no work is charged to the bounded Unknown bucket, never
    to automation. Automation with no actor (cron, loopback) is automation.
    """
    actor = actor_of(request)
    if actor is not None and not actor.principal.is_unknown:
        if automation and actor.transport is Transport.INTERNAL:
            return internal_initiator(request) or Principal.unknown().id
        return actor.principal.id
    if automation:
        return AUTOMATION_PRINCIPAL.id
    return Principal.unknown().id


# Start and stop checks live in their own module; this stays their import path.
from kiro_crew.shared_crew.admission_startup import (  # noqa: E402
    SHUTDOWN_SWEEP_INTERVAL_SECONDS,
    SHUTDOWN_SWEEP_ROUNDS,
    shutdown_cleanup,
    startup_check,
)

__all__ = [
    "ATTESTED_SESSION_KEY",
    "AUTOMATION_PRINCIPAL",
    "Admission",
    "AdmissionEnded",
    "AdmissionRefused",
    "AdmissionService",
    "FAILURE_DETAIL_KEY",
    "SLICE_IDLE_SECONDS",
    "HostHooks",
    "install_host_hooks",
    "MERGED_INTO_KEY",
    "ORPHANS_REAPED_KEY",
    "PARKED_KEY",
    "STOPPED_BEFORE_START",
    "STOPPED_WHILE_RUNNING",
    "IDEMPOTENCY_HEADER",
    "RECONCILE_DETAIL_KEY",
    "TASK_STORE_UNAVAILABLE_CODE",
    "actor_budget",
    "CALLER_SESSION_HEADER",
    "coded_json_response",
    "internal_initiator",
    "SHUTDOWN_SWEEP_INTERVAL_SECONDS",
    "SHUTDOWN_SWEEP_ROUNDS",
    "shutdown_cleanup",
    "actor_of",
    "attest_internal_session",
    "admit_and_run",
    "current_service",
    "idempotency_key_of",
    "install_service",
    "installed_service",
    "recorded_principal",
    "request_digest",
    "scope_principal_for_session",
    "startup_check",
]
