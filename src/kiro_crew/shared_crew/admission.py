"""Admission, accepted-work, runtime-ownership and capacity contracts.

The operation state machine here is the one every execution entry point
reports through. It is a projection over the durable task queue
(``kiro_crew.taskq.model``): :data:`TASKQ_STATE_MAP` names the operation state
for each queue state, so a queued chat turn and a queued subagent read the same
way in the UI and in a capacity answer.

Two rules carry the fairness guarantee and are encoded as data, not prose:

* A rejected request was never accepted. ``REJECTED`` is reachable only from
  ``SUBMITTED`` and an :class:`AcceptedWork` can never hold it.
* Capacity is released only when work leaves a :data:`CAPACITY_HOLDING` state
  for a terminal one and its runtime and descendants are proven stopped.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from kiro_crew.shared_crew.codec import (
    SCHEMA_VERSION,
    ContractError,
    enum_or,
    merge_extra,
    opt_float,
    opt_int,
    opt_str,
    read_schema_version,
    req_enum,
    req_float,
    req_int,
    req_str,
    require_mapping,
    split_extra,
)
from kiro_crew.shared_crew.contracts import EnforcementLevel

#: How long an idempotency key keeps answering with its first result.
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
_IDEMPOTENCY_KEY_RE = re.compile(r"\A[A-Za-z0-9._:\-]{8,128}\Z")


class OperationKind(str, enum.Enum):
    """Which entry point an admission request came from."""

    CHAT_TURN = "chat_turn"
    SUBAGENT = "subagent"
    WORKFLOW = "workflow"
    TASK = "task"
    CRON = "cron"
    CHANNEL_TURN = "channel_turn"
    REMOTE = "remote"
    BACKGROUND = "background"


class OperationState(str, enum.Enum):
    """Where one unit of work stands, from the browser's send to its end."""

    #: Sent by a client, not yet acknowledged by the host. Client-side only.
    SUBMITTED = "submitted"
    QUEUED = "queued"
    ADMITTED = "admitted"
    RUNNING = "running"
    CANCELLING = "cancelling"
    #: Ownership or liveness is being re-established after a restart or loss.
    RECONCILING = "reconciling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    #: The work may have run after a crash; it is surfaced, never replayed.
    UNKNOWN_OUTCOME = "unknown_outcome"


S = OperationState

TERMINAL: frozenset[OperationState] = frozenset(
    {S.COMPLETED, S.FAILED, S.CANCELLED, S.EXPIRED, S.REJECTED, S.UNKNOWN_OUTCOME}
)

#: States in which the work counts against active capacity.
CAPACITY_HOLDING: frozenset[OperationState] = frozenset(
    {S.ADMITTED, S.RUNNING, S.CANCELLING, S.RECONCILING}
)

TRANSITIONS: Mapping[OperationState, frozenset[OperationState]] = {
    S.SUBMITTED: frozenset({S.QUEUED, S.ADMITTED, S.REJECTED}),
    S.QUEUED: frozenset({S.ADMITTED, S.CANCELLED, S.EXPIRED, S.RECONCILING}),
    # ADMITTED -> QUEUED: revalidation found capacity gone before the start.
    # ADMITTED -> CANCELLED: revalidation found the grant or policy revoked.
    S.ADMITTED: frozenset({S.RUNNING, S.QUEUED, S.CANCELLED, S.EXPIRED, S.RECONCILING, S.FAILED}),
    S.RUNNING: frozenset({S.CANCELLING, S.COMPLETED, S.FAILED, S.RECONCILING}),
    S.CANCELLING: frozenset({S.CANCELLED, S.COMPLETED, S.FAILED, S.RECONCILING}),
    # RECONCILING -> QUEUED only when the work is proven never to have started.
    S.RECONCILING: frozenset(
        {S.QUEUED, S.RUNNING, S.COMPLETED, S.FAILED, S.CANCELLED, S.UNKNOWN_OUTCOME}
    ),
    **{t: frozenset() for t in TERMINAL},
}

#: Operation state for each ``kiro_crew.taskq.model`` state.
TASKQ_STATE_MAP: Mapping[str, OperationState] = {
    "queued": S.QUEUED,
    "admitted": S.ADMITTED,
    "starting": S.ADMITTED,
    "running": S.RUNNING,
    "waiting_children": S.RUNNING,
    "waiting_permission": S.RUNNING,
    "waiting_dependency": S.RUNNING,
    "waiting_input": S.RUNNING,
    "waiting_infra": S.QUEUED,
    "retry_wait": S.QUEUED,
    "recovering": S.RECONCILING,
    "done": S.COMPLETED,
    "failed": S.FAILED,
    "cancelled": S.CANCELLED,
    "unknown_side_effect": S.UNKNOWN_OUTCOME,
}


def is_legal_transition(old: OperationState, new: OperationState) -> bool:
    return new in TRANSITIONS[old]


class ReasonCode(str, enum.Enum):
    """Machine-readable reason on a refusal, a terminal state or a conflict."""

    PRINCIPAL_ACTIVE_LIMIT = "principal_active_limit"
    PRINCIPAL_QUEUE_FULL = "principal_queue_full"
    CREW_ACTIVE_LIMIT = "crew_active_limit"
    CREW_QUEUE_FULL = "crew_queue_full"
    SESSION_CREATE_RATE = "session_create_rate"
    REQUEST_TOO_LARGE = "request_too_large"
    STORAGE_LIMIT = "storage_limit"
    CONTAINMENT_UNAVAILABLE = "containment_unavailable"
    QUEUE_EXPIRED = "queue_expired"
    RUN_DEADLINE = "run_deadline"
    #: The work ran and ended in an error (a raise, or a turn its runtime failed).
    RUNTIME_ERROR = "runtime_error"
    GRANT_REVOKED = "grant_revoked"
    POLICY_CHANGED = "policy_changed"
    CANCELLED_BY_USER = "cancelled_by_user"
    #: The owner removed the person who asked for this work: it was stopped.
    ACCESS_REMOVED = "access_removed"
    STALE_REVISION = "stale_revision"
    ALREADY_RESOLVED = "already_resolved"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_TRANSITION = "invalid_transition"
    NOT_FOUND = "not_found"
    #: A send lost a first-submit-wins race (``send_race``): nothing was accepted.
    SEND_RACE = "send_race"
    #: Someone else holds the session's typing lease (``typing_lease``): the
    #: send was refused and the sender keeps the draft.
    COMPOSER_LOCKED = "composer_locked"


#: HTTP status a handler answers for each refusal reason.
REASON_HTTP_STATUS: Mapping[ReasonCode, int] = {
    ReasonCode.PRINCIPAL_ACTIVE_LIMIT: 429,
    ReasonCode.PRINCIPAL_QUEUE_FULL: 429,
    ReasonCode.CREW_ACTIVE_LIMIT: 429,
    ReasonCode.CREW_QUEUE_FULL: 429,
    ReasonCode.SESSION_CREATE_RATE: 429,
    ReasonCode.REQUEST_TOO_LARGE: 413,
    ReasonCode.STORAGE_LIMIT: 507,
    ReasonCode.CONTAINMENT_UNAVAILABLE: 503,
    ReasonCode.QUEUE_EXPIRED: 409,
    ReasonCode.RUN_DEADLINE: 409,
    ReasonCode.RUNTIME_ERROR: 409,
    ReasonCode.GRANT_REVOKED: 403,
    ReasonCode.POLICY_CHANGED: 409,
    ReasonCode.CANCELLED_BY_USER: 409,
    ReasonCode.ACCESS_REMOVED: 403,
    ReasonCode.STALE_REVISION: 409,
    ReasonCode.ALREADY_RESOLVED: 409,
    ReasonCode.IDEMPOTENCY_CONFLICT: 409,
    ReasonCode.INVALID_TRANSITION: 409,
    ReasonCode.NOT_FOUND: 404,
    ReasonCode.SEND_RACE: 409,
    ReasonCode.COMPOSER_LOCKED: 409,
}


class InvalidTransition(ContractError):
    def __init__(self, old: OperationState, new: OperationState) -> None:
        super().__init__(
            ReasonCode.INVALID_TRANSITION.value, "state", f"{old.value} -> {new.value} is not legal"
        )


class StaleRevision(ContractError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            ReasonCode.STALE_REVISION.value, "revision", f"expected {expected}, current {actual}"
        )


def validate_idempotency_key(key: str) -> str:
    """Return *key* if it is a well-formed client idempotency key."""
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY_RE.match(key):
        raise ContractError("invalid_idempotency_key", "idempotency_key", "malformed key")
    return key


def idempotency_scope(budget_key: str, session_key: str, idempotency_key: str) -> str:
    """The deduplication key: one principal, one session, one client key.

    Scoped to the principal so one person's key cannot collide with or probe
    another's, and to the session so a key reused in another chat is new work.
    """
    return f"{budget_key}\x1f{session_key}\x1f{validate_idempotency_key(idempotency_key)}"


@dataclass(frozen=True)
class ResourceEstimate:
    """The scheduler's cost assumption for one unit of work."""

    slots: int = 1
    weight: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"slots": self.slots, "weight": self.weight}


@dataclass(frozen=True)
class AdmissionRequest:
    """A request to run work, built by the server at the entry point.

    ``budget_key`` is the INITIATOR's principal id, never the session creator
    and never a per-device token, so tabs, devices and child tasks of one
    person share one budget.
    """

    idempotency_key: str
    budget_key: str
    session_key: str
    operation: OperationKind
    submitted_at: float
    expires_at: float
    policy_version: str = ""
    parent_execution_id: str = ""
    estimate: ResourceEstimate = field(default_factory=ResourceEstimate)
    #: Digest of the request payload. A replay under the same scope with a
    #: different digest is ``IDEMPOTENCY_CONFLICT``, never new work.
    request_digest: str = ""

    def __post_init__(self) -> None:
        validate_idempotency_key(self.idempotency_key)
        if self.expires_at < self.submitted_at:
            raise ContractError("malformed_record", "expires_at", "expires before it was submitted")

    @property
    def scope(self) -> str:
        return idempotency_scope(self.budget_key, self.session_key, self.idempotency_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "idempotency_key": self.idempotency_key,
            "budget_key": self.budget_key,
            "session_key": self.session_key,
            "operation": self.operation.value,
            "submitted_at": self.submitted_at,
            "expires_at": self.expires_at,
            "policy_version": self.policy_version,
            "parent_execution_id": self.parent_execution_id,
            "estimate": self.estimate.to_dict(),
            "request_digest": self.request_digest,
        }


class ContainmentKind(str, enum.Enum):
    SYSTEMD_SCOPE = "systemd_scope"
    NONE = "none"


class Liveness(str, enum.Enum):
    ALIVE = "alive"
    STOPPED = "stopped"
    #: Not yet reconciled; never counts as stopped.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuntimeOwnershipRef:
    """A pointer into the existing ownership registry, not a second registry.

    ``lease`` is ``runtime_ownership`` ``Acquisition.lease``; ``tenancy_epoch``
    is the ``RuntimeTenancy`` epoch; ``fencing_generation`` is the taskq
    ``generation``; ``containment_unit`` is the systemd scope unit name when one
    exists. ``liveness`` is what the reconciler last proved, never what a
    client reported.
    """

    execution_id: str
    lease: str = ""
    tenancy_epoch: int = 0
    fencing_generation: int = 0
    parent_execution_id: str = ""
    root_execution_id: str = ""
    containment: ContainmentKind = ContainmentKind.NONE
    containment_unit: str = ""
    liveness: Liveness = Liveness.UNKNOWN
    descendants_alive: int = 0
    reconciled_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "lease": self.lease,
            "tenancy_epoch": self.tenancy_epoch,
            "fencing_generation": self.fencing_generation,
            "parent_execution_id": self.parent_execution_id,
            "root_execution_id": self.root_execution_id,
            "containment": self.containment.value,
            "containment_unit": self.containment_unit,
            "liveness": self.liveness.value,
            "descendants_alive": self.descendants_alive,
            "reconciled_at": self.reconciled_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> RuntimeOwnershipRef:
        data = require_mapping(raw, "runtime")
        return cls(
            execution_id=req_str(data, "execution_id"),
            lease=opt_str(data, "lease") or "",
            tenancy_epoch=opt_int(data, "tenancy_epoch") or 0,
            fencing_generation=opt_int(data, "fencing_generation") or 0,
            parent_execution_id=opt_str(data, "parent_execution_id") or "",
            root_execution_id=opt_str(data, "root_execution_id") or "",
            containment=enum_or(data, "containment", ContainmentKind, ContainmentKind.NONE),
            containment_unit=opt_str(data, "containment_unit") or "",
            liveness=enum_or(data, "liveness", Liveness, Liveness.UNKNOWN),
            descendants_alive=opt_int(data, "descendants_alive") or 0,
            reconciled_at=opt_float(data, "reconciled_at"),
        )

    @property
    def proven_stopped(self) -> bool:
        return self.liveness is Liveness.STOPPED and self.descendants_alive == 0


@dataclass(frozen=True)
class AcceptedWork:
    """Durable record of work the host accepted. Revisioned for compare-and-set."""

    work_id: str
    scope: str
    budget_key: str
    session_key: str
    operation: OperationKind
    state: OperationState
    revision: int
    accepted_at: float
    expires_at: float
    deadline_at: float | None = None
    host_ack: str = ""
    reason: ReasonCode | None = None
    runtime: RuntimeOwnershipRef | None = None
    request_digest: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False)

    _KNOWN = frozenset(
        {
            "schema_version",
            "work_id",
            "scope",
            "budget_key",
            "session_key",
            "operation",
            "state",
            "revision",
            "accepted_at",
            "expires_at",
            "deadline_at",
            "host_ack",
            "reason",
            "runtime",
            "request_digest",
        }
    )

    def __post_init__(self) -> None:
        if self.state in (S.SUBMITTED, S.REJECTED):
            raise ContractError(
                "malformed_record", "state", f"{self.state.value} work was never accepted"
            )

    def advance(
        self,
        new_state: OperationState,
        *,
        expected_revision: int,
        reason: ReasonCode | None = None,
        runtime: RuntimeOwnershipRef | None = None,
    ) -> AcceptedWork:
        """Return the next revision, refusing a stale or illegal transition."""
        if expected_revision != self.revision:
            raise StaleRevision(expected_revision, self.revision)
        if not is_legal_transition(self.state, new_state):
            raise InvalidTransition(self.state, new_state)
        return replace(
            self,
            state=new_state,
            revision=self.revision + 1,
            reason=reason if reason is not None else self.reason,
            runtime=runtime if runtime is not None else self.runtime,
        )

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema_version": SCHEMA_VERSION,
            "work_id": self.work_id,
            "scope": self.scope,
            "budget_key": self.budget_key,
            "session_key": self.session_key,
            "operation": self.operation.value,
            "state": self.state.value,
            "revision": self.revision,
            "accepted_at": self.accepted_at,
            "expires_at": self.expires_at,
            "deadline_at": self.deadline_at,
            "host_ack": self.host_ack,
            "reason": None if self.reason is None else self.reason.value,
            "runtime": None if self.runtime is None else self.runtime.to_dict(),
            "request_digest": self.request_digest,
        }
        return merge_extra(body, self.extra)

    def to_public_dict(self) -> dict[str, Any]:
        """The browser shape: no idempotency scope, no lease or fencing ids."""
        return {
            "work_id": self.work_id,
            "session_key": self.session_key,
            "operation": self.operation.value,
            "state": self.state.value,
            "revision": self.revision,
            "accepted_at": self.accepted_at,
            "expires_at": self.expires_at,
            "deadline_at": self.deadline_at,
            "reason": None if self.reason is None else self.reason.value,
        }

    @classmethod
    def from_dict(cls, raw: object) -> AcceptedWork:
        data = require_mapping(raw, "accepted_work")
        read_schema_version(data)
        reason_raw = data.get("reason")
        runtime_raw = data.get("runtime")
        return cls(
            work_id=req_str(data, "work_id"),
            scope=req_str(data, "scope"),
            budget_key=req_str(data, "budget_key"),
            session_key=req_str(data, "session_key"),
            operation=req_enum(data, "operation", OperationKind),
            state=req_enum(data, "state", OperationState),
            revision=req_int(data, "revision"),
            accepted_at=req_float(data, "accepted_at"),
            expires_at=req_float(data, "expires_at"),
            deadline_at=opt_float(data, "deadline_at"),
            host_ack=opt_str(data, "host_ack") or "",
            reason=None if reason_raw is None else req_enum(data, "reason", ReasonCode),
            runtime=None if runtime_raw is None else RuntimeOwnershipRef.from_dict(runtime_raw),
            request_digest=opt_str(data, "request_digest") or "",
            extra=split_extra(data, cls._KNOWN),
        )


class CapacityScope(str, enum.Enum):
    PRINCIPAL = "principal"
    CREW = "crew"


@dataclass(frozen=True)
class ScopeUsage:
    """Limits and usage for one scope. ``None`` limit means not enforced."""

    active: int = 0
    queued: int = 0
    active_limit: int | None = None
    queue_limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "queued": self.queued,
            "active_limit": self.active_limit,
            "queue_limit": self.queue_limit,
        }


@dataclass(frozen=True)
class CapacityResponse:
    """The answer to "can this run now", attached to every admission outcome.

    There is no wait-time estimate: the scheduler cannot back one, so the
    contract has no field for it. ``retry_after_seconds`` is a polling hint for
    a refusal, sent as the HTTP ``Retry-After`` header too.
    """

    principal: ScopeUsage
    crew: ScopeUsage
    enforcement: EnforcementLevel
    limiting_scope: CapacityScope | None = None
    reason: ReasonCode | None = None
    retry_after_seconds: int | None = None
    queue_expiry_seconds: int | None = None
    run_deadline_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "principal": self.principal.to_dict(),
            "crew": self.crew.to_dict(),
            "enforcement": self.enforcement.value,
            "limiting_scope": None if self.limiting_scope is None else self.limiting_scope.value,
            "reason": None if self.reason is None else self.reason.value,
            "retry_after_seconds": self.retry_after_seconds,
            "queue_expiry_seconds": self.queue_expiry_seconds,
            "run_deadline_seconds": self.run_deadline_seconds,
        }


def error_body(
    reason: ReasonCode,
    message: str,
    *,
    capacity: CapacityResponse | None = None,
    current: AcceptedWork | None = None,
) -> dict[str, Any]:
    """The JSON body for a refusal or conflict, in the dashboard's envelope.

    ``error`` and ``code`` match the existing ``{"error", "code"}`` handler
    shape. ``current`` carries the server's present state on a conflict so a
    stale UI action can reconcile instead of guessing.
    """
    body: dict[str, Any] = {"error": message, "code": reason.value}
    if capacity is not None:
        body["capacity"] = capacity.to_dict()
    if current is not None:
        body["current"] = current.to_public_dict()
    return body


def http_status(reason: ReasonCode) -> int:
    return REASON_HTTP_STATUS[reason]


def retry_after_header(capacity: CapacityResponse | None) -> dict[str, str]:
    """The ``Retry-After`` header for a refusal, or no header."""
    if capacity is None or capacity.retry_after_seconds is None:
        return {}
    return {"Retry-After": str(capacity.retry_after_seconds)}


__all__ = [
    "AcceptedWork",
    "AdmissionRequest",
    "CAPACITY_HOLDING",
    "CapacityResponse",
    "CapacityScope",
    "ContainmentKind",
    "IDEMPOTENCY_TTL_SECONDS",
    "InvalidTransition",
    "Liveness",
    "OperationKind",
    "OperationState",
    "REASON_HTTP_STATUS",
    "ReasonCode",
    "ResourceEstimate",
    "RuntimeOwnershipRef",
    "ScopeUsage",
    "StaleRevision",
    "TASKQ_STATE_MAP",
    "TERMINAL",
    "TRANSITIONS",
    "error_body",
    "http_status",
    "idempotency_scope",
    "is_legal_transition",
    "retry_after_header",
    "validate_idempotency_key",
]
