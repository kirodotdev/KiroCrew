"""Typed lifecycle records shared by run coordinator implementations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, Protocol, TypeVar


class DesiredState(str, Enum):
    RUN = "run"
    CANCEL = "cancel"
    RELEASE = "release"


class ObservedState(str, Enum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    TERMINAL = "terminal"


class RunOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


class CommandOperation(str, Enum):
    SPAWN = "spawn"
    CONTINUE = "continue"
    STEER = "steer"
    CANCEL = "cancel"
    RELEASE = "release"


class CommandStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    APPLIED = "applied"
    REJECTED = "rejected"


class DeliveryState(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    DELIVERED = "delivered"


class CoordinatorDecision(str, Enum):
    APPLIED = "applied"
    UNCHANGED = "unchanged"
    REJECTED = "rejected"


class CoordinatorReason(str, Enum):
    CREATED = "created"
    ADMISSION_REJECTED = "admission_rejected"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    IDENTITY_CONFLICT = "identity_conflict"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    CLAIMED = "claimed"
    TRANSITIONED = "transitioned"
    VERSION_CONFLICT = "version_conflict"
    STALE_FENCE = "stale_fence"
    INVALID_TRANSITION = "invalid_transition"
    NOT_FOUND = "not_found"
    COMPLETED = "completed"
    COMPLETION_REPLAY = "completion_replay"
    OUTCOME_CONFLICT = "outcome_conflict"
    DELIVERY_RELEASED = "delivery_released"
    DELIVERED = "delivered"
    ALREADY_DELIVERED = "already_delivered"


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    parent_session: str
    agent: str
    task: str
    conversation_key: str
    desired_state: DesiredState
    observed_state: ObservedState
    outcome: RunOutcome | None
    result_path: str
    error: str
    attempt: int
    version: int
    owner_id: str
    lease_expires_at: float
    lease_epoch: int
    created_at: float
    updated_at: float
    terminal_at: float | None


@dataclass(frozen=True)
class RunCommand:
    command_id: str
    idempotency_key: str
    run_id: str
    operation: CommandOperation
    payload_hash: str
    status: CommandStatus
    attempt: int
    owner_id: str
    lease_epoch: int
    created_at: float
    updated_at: float
    rejection_reason: str
    payload_json: str = ""
    claim_expires_at: float = 0.0
    claim_epoch: int = 0
    result_json: str = ""


@dataclass(frozen=True)
class RunFence:
    run_id: str
    owner_id: str
    lease_epoch: int


@dataclass(frozen=True)
class CommandFence:
    command_id: str
    owner_id: str
    claim_epoch: int


@dataclass(frozen=True)
class DeliveryFence:
    event_id: str
    owner_id: str
    claim_epoch: int


@dataclass(frozen=True)
class OwnerLease:
    owner_id: str
    lease_expires_at: float


@dataclass(frozen=True)
class CommandClaim:
    command: RunCommand
    run: RunRecord | None
    fence: RunFence | None
    command_fence: CommandFence


@dataclass(frozen=True)
class RunCompletion:
    run_id: str
    outcome: RunOutcome
    result_path: str
    error: str
    event_type: str
    destination: str
    payload_json: str
    terminal_at: float


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    run_id: str
    run_version: int
    destination: str
    event_type: str
    payload_json: str
    status: DeliveryState
    attempts: int
    available_at: float
    claim_owner: str
    claim_expires_at: float
    claim_epoch: int
    created_at: float
    delivered_at: float | None


@dataclass(frozen=True)
class SubmitRun:
    run_id: str
    command_id: str
    idempotency_key: str
    payload_hash: str
    parent_session: str
    agent: str
    task: str
    conversation_key: str
    operation: CommandOperation
    accepted: bool = True
    rejection_reason: str = ""
    payload_json: str = ""


@dataclass(frozen=True)
class SubmitControl:
    command_id: str
    idempotency_key: str
    run_id: str
    operation: CommandOperation
    payload_hash: str
    payload_json: str = ""
    accepted: bool = True
    rejection_reason: str = ""


@dataclass(frozen=True)
class SubmitReceipt:
    run: RunRecord
    command: RunCommand
    created: bool


@dataclass(frozen=True)
class CommandReceipt:
    run: RunRecord | None
    command: RunCommand
    created: bool


T = TypeVar("T")


@dataclass(frozen=True)
class CoordinatorResult(Generic[T]):
    decision: CoordinatorDecision
    reason: CoordinatorReason
    value: T | None


class RunCoordinator(Protocol):
    async def submit(self, request: SubmitRun) -> CoordinatorResult[SubmitReceipt]: ...

    async def submit_control(self, request: SubmitControl) -> CoordinatorResult[CommandReceipt]: ...

    async def get_command_by_key(self, idempotency_key: str) -> CommandReceipt | None: ...

    async def claim_commands(self, owner: OwnerLease, limit: int) -> list[CommandClaim]: ...

    async def claim_controls(
        self, owner: OwnerLease, limit: int, command_id: str = ""
    ) -> list[CommandClaim]: ...

    async def claim_command(self, command_id: str, owner: OwnerLease) -> CommandClaim | None: ...

    async def finish_command(
        self,
        fence: CommandFence,
        status: CommandStatus,
        rejection_reason: str = "",
        result_json: str = "",
    ) -> CoordinatorResult[RunCommand]: ...

    async def finish_control(
        self,
        fence: CommandFence,
        status: CommandStatus,
        rejection_reason: str = "",
        result_json: str = "",
    ) -> CoordinatorResult[RunCommand]: ...

    async def mark_starting(
        self, command: RunCommand, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]: ...

    async def mark_running(
        self, run_id: str, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]: ...

    async def complete(
        self, completion: RunCompletion, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[OutboxEvent]: ...

    async def renew(self, run_id: str, fence: RunFence, until: float) -> bool: ...

    async def claim_outbox(self, owner: OwnerLease, limit: int) -> list[OutboxEvent]: ...

    async def release_outbox(
        self, fence: DeliveryFence, available_at: float
    ) -> CoordinatorResult[OutboxEvent]: ...

    async def mark_delivered(self, fence: DeliveryFence) -> CoordinatorResult[OutboxEvent]: ...

    async def get_run(self, run_id: str) -> RunRecord | None: ...
