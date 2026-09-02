"""Deterministic in-memory implementation of the run coordinator contract."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import replace

from .models import (
    CommandClaim,
    CommandOperation,
    CommandStatus,
    CoordinatorDecision,
    CoordinatorReason,
    CoordinatorResult,
    DeliveryFence,
    DeliveryState,
    DesiredState,
    ObservedState,
    OutboxEvent,
    OwnerLease,
    RunCommand,
    RunCompletion,
    RunFence,
    RunOutcome,
    RunRecord,
    SubmitReceipt,
    SubmitRun,
)

_EXECUTION_COMMANDS = frozenset({CommandOperation.SPAWN, CommandOperation.CONTINUE})
_STARTABLE_STATES = frozenset({ObservedState.ACCEPTED, ObservedState.QUEUED})
_COMPLETABLE_STATES = frozenset(
    {
        ObservedState.ACCEPTED,
        ObservedState.QUEUED,
        ObservedState.STARTING,
        ObservedState.RUNNING,
    }
)


class MemoryRunCoordinator:
    """Event-loop-affine coordinator used by tests and pre-authority wiring."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._lock = asyncio.Lock()
        self._runs: dict[str, RunRecord] = {}
        self._commands: dict[str, RunCommand] = {}
        self._command_by_key: dict[str, str] = {}
        self._outbox: dict[str, OutboxEvent] = {}
        self._outbox_by_run_type: dict[tuple[str, str], str] = {}

    @staticmethod
    def _result(
        decision: CoordinatorDecision,
        reason: CoordinatorReason,
        value: object = None,
    ) -> CoordinatorResult:
        return CoordinatorResult(decision=decision, reason=reason, value=value)

    async def submit(self, request: SubmitRun) -> CoordinatorResult[SubmitReceipt]:
        async with self._lock:
            if request.operation not in _EXECUTION_COMMANDS:
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.UNSUPPORTED_OPERATION,
                )
            existing_id = self._command_by_key.get(request.idempotency_key)
            if existing_id is not None:
                command = self._commands[existing_id]
                if (
                    command.run_id != request.run_id
                    or command.command_id != request.command_id
                    or command.operation != request.operation
                    or command.payload_hash != request.payload_hash
                ):
                    return self._result(
                        CoordinatorDecision.REJECTED,
                        CoordinatorReason.IDEMPOTENCY_CONFLICT,
                    )
                run = self._runs[command.run_id]
                return self._result(
                    CoordinatorDecision.UNCHANGED,
                    CoordinatorReason.IDEMPOTENT_REPLAY,
                    SubmitReceipt(run=run, command=command, created=False),
                )
            if request.run_id in self._runs or request.command_id in self._commands:
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.IDENTITY_CONFLICT,
                )

            now = self._clock()
            accepted = request.accepted
            run = RunRecord(
                run_id=request.run_id,
                parent_session=request.parent_session,
                agent=request.agent,
                task=request.task,
                conversation_key=request.conversation_key,
                desired_state=DesiredState.RUN,
                observed_state=(ObservedState.ACCEPTED if accepted else ObservedState.TERMINAL),
                outcome=None if accepted else RunOutcome.FAILED,
                result_path="",
                error=request.rejection_reason,
                attempt=1,
                version=1,
                owner_id="",
                lease_expires_at=0.0,
                lease_epoch=0,
                created_at=now,
                updated_at=now,
                terminal_at=None if accepted else now,
            )
            command = RunCommand(
                command_id=request.command_id,
                idempotency_key=request.idempotency_key,
                run_id=request.run_id,
                operation=request.operation,
                payload_hash=request.payload_hash,
                status=CommandStatus.PENDING if accepted else CommandStatus.REJECTED,
                attempt=0,
                owner_id="",
                lease_epoch=0,
                created_at=now,
                updated_at=now,
                rejection_reason=request.rejection_reason,
                payload_json=request.payload_json,
            )
            self._runs[run.run_id] = run
            self._commands[command.command_id] = command
            self._command_by_key[command.idempotency_key] = command.command_id
            receipt = SubmitReceipt(run=run, command=command, created=True)
            if not accepted:
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.ADMISSION_REJECTED,
                    receipt,
                )
            return self._result(CoordinatorDecision.APPLIED, CoordinatorReason.CREATED, receipt)

    async def claim_commands(self, owner: OwnerLease, limit: int) -> list[CommandClaim]:
        if limit <= 0:
            return []
        async with self._lock:
            now = self._clock()
            if owner.lease_expires_at <= now:
                return []
            claims: list[CommandClaim] = []
            for current in sorted(
                self._commands.values(),
                key=lambda item: (item.created_at, item.command_id),
            ):
                if len(claims) >= limit:
                    break
                run = self._runs.get(current.run_id)
                if run is None:
                    continue
                if not (
                    current.status is CommandStatus.PENDING
                    or (current.status is CommandStatus.CLAIMED and run.lease_expires_at <= now)
                ):
                    continue
                epoch = run.lease_epoch
                if current.operation in _EXECUTION_COMMANDS:
                    epoch += 1
                    run = replace(
                        run,
                        owner_id=owner.owner_id,
                        lease_expires_at=owner.lease_expires_at,
                        lease_epoch=epoch,
                        updated_at=now,
                    )
                    self._runs[run.run_id] = run
                command = replace(
                    current,
                    status=CommandStatus.CLAIMED,
                    attempt=current.attempt + 1,
                    owner_id=owner.owner_id,
                    lease_epoch=epoch,
                    updated_at=now,
                )
                self._commands[command.command_id] = command
                claims.append(
                    CommandClaim(
                        command=command,
                        run=run,
                        fence=RunFence(run.run_id, owner.owner_id, epoch),
                    )
                )
            return claims

    def _validate_transition(
        self, run_id: str, fence: RunFence, expected_version: int, *, now: float
    ) -> CoordinatorResult[RunRecord] | RunRecord:
        run = self._runs.get(run_id)
        if run is None:
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.NOT_FOUND)
        if (
            fence.run_id != run_id
            or run.owner_id != fence.owner_id
            or run.lease_epoch != fence.lease_epoch
            or run.lease_expires_at <= now
        ):
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.STALE_FENCE)
        if run.version != expected_version:
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.VERSION_CONFLICT)
        return run

    async def mark_starting(
        self, command: RunCommand, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]:
        async with self._lock:
            now = self._clock()
            validated = self._validate_transition(command.run_id, fence, expected_version, now=now)
            if isinstance(validated, CoordinatorResult):
                return validated
            if validated.observed_state is ObservedState.STARTING:
                return self._result(
                    CoordinatorDecision.UNCHANGED, CoordinatorReason.TRANSITIONED, validated
                )
            if validated.observed_state not in _STARTABLE_STATES:
                return self._result(
                    CoordinatorDecision.REJECTED, CoordinatorReason.INVALID_TRANSITION
                )
            updated = replace(
                validated,
                observed_state=ObservedState.STARTING,
                version=validated.version + 1,
                updated_at=now,
            )
            self._runs[updated.run_id] = updated
            return self._result(
                CoordinatorDecision.APPLIED, CoordinatorReason.TRANSITIONED, updated
            )

    async def mark_running(
        self, run_id: str, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]:
        async with self._lock:
            now = self._clock()
            validated = self._validate_transition(run_id, fence, expected_version, now=now)
            if isinstance(validated, CoordinatorResult):
                return validated
            if validated.observed_state is ObservedState.RUNNING:
                return self._result(
                    CoordinatorDecision.UNCHANGED, CoordinatorReason.TRANSITIONED, validated
                )
            if validated.observed_state is not ObservedState.STARTING:
                return self._result(
                    CoordinatorDecision.REJECTED, CoordinatorReason.INVALID_TRANSITION
                )
            updated = replace(
                validated,
                observed_state=ObservedState.RUNNING,
                version=validated.version + 1,
                updated_at=now,
            )
            self._runs[updated.run_id] = updated
            return self._result(
                CoordinatorDecision.APPLIED, CoordinatorReason.TRANSITIONED, updated
            )

    async def complete(
        self, completion: RunCompletion, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[OutboxEvent]:
        async with self._lock:
            key = (completion.run_id, completion.event_type)
            existing_id = self._outbox_by_run_type.get(key)
            if existing_id is not None:
                event = self._outbox[existing_id]
                run = self._runs[completion.run_id]
                if (
                    fence.run_id != run.run_id
                    or fence.owner_id != run.owner_id
                    or fence.lease_epoch != run.lease_epoch
                ):
                    return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.STALE_FENCE)
                if expected_version != event.run_version - 1:
                    return self._result(
                        CoordinatorDecision.REJECTED,
                        CoordinatorReason.VERSION_CONFLICT,
                    )
                if (
                    run.outcome == completion.outcome
                    and run.result_path == completion.result_path
                    and run.error == completion.error
                    and event.destination == completion.destination
                    and event.payload_json == completion.payload_json
                ):
                    return self._result(
                        CoordinatorDecision.UNCHANGED,
                        CoordinatorReason.COMPLETION_REPLAY,
                        event,
                    )
                return self._result(
                    CoordinatorDecision.REJECTED, CoordinatorReason.OUTCOME_CONFLICT
                )
            now = self._clock()
            validated = self._validate_transition(
                completion.run_id, fence, expected_version, now=now
            )
            if isinstance(validated, CoordinatorResult):
                return self._result(validated.decision, validated.reason)
            if validated.observed_state not in _COMPLETABLE_STATES:
                return self._result(
                    CoordinatorDecision.REJECTED, CoordinatorReason.INVALID_TRANSITION
                )
            event_id = self._id_factory()
            run = replace(
                validated,
                observed_state=ObservedState.TERMINAL,
                outcome=completion.outcome,
                result_path=completion.result_path,
                error=completion.error,
                version=validated.version + 1,
                updated_at=now,
                terminal_at=completion.terminal_at,
            )
            self._runs[run.run_id] = run
            for command_id, command in tuple(self._commands.items()):
                if command.run_id == run.run_id and command.status is CommandStatus.CLAIMED:
                    self._commands[command_id] = replace(
                        command, status=CommandStatus.APPLIED, updated_at=now
                    )
            event = OutboxEvent(
                event_id=event_id,
                run_id=run.run_id,
                run_version=run.version,
                destination=completion.destination,
                event_type=completion.event_type,
                payload_json=completion.payload_json,
                status=DeliveryState.PENDING,
                attempts=0,
                available_at=now,
                claim_owner="",
                claim_expires_at=0.0,
                claim_epoch=0,
                created_at=now,
                delivered_at=None,
            )
            self._outbox[event.event_id] = event
            self._outbox_by_run_type[key] = event.event_id
            return self._result(CoordinatorDecision.APPLIED, CoordinatorReason.COMPLETED, event)

    async def renew(self, run_id: str, fence: RunFence, until: float) -> bool:
        async with self._lock:
            run = self._runs.get(run_id)
            now = self._clock()
            if (
                run is None
                or fence.run_id != run_id
                or run.owner_id != fence.owner_id
                or run.lease_epoch != fence.lease_epoch
                or run.lease_expires_at <= now
                or until <= now
                or run.observed_state == ObservedState.TERMINAL
            ):
                return False
            self._runs[run_id] = replace(
                run,
                lease_expires_at=max(run.lease_expires_at, until),
                updated_at=now,
            )
            return True

    async def claim_outbox(self, owner: OwnerLease, limit: int) -> list[OutboxEvent]:
        if limit <= 0:
            return []
        async with self._lock:
            now = self._clock()
            if owner.lease_expires_at <= now:
                return []
            claimed: list[OutboxEvent] = []
            for current in sorted(
                self._outbox.values(), key=lambda item: (item.created_at, item.event_id)
            ):
                if len(claimed) >= limit:
                    break
                pending = current.status is DeliveryState.PENDING and current.available_at <= now
                expired = (
                    current.status is DeliveryState.CLAIMED and current.claim_expires_at <= now
                )
                if not (pending or expired):
                    continue
                event = replace(
                    current,
                    status=DeliveryState.CLAIMED,
                    attempts=current.attempts + 1,
                    claim_owner=owner.owner_id,
                    claim_expires_at=owner.lease_expires_at,
                    claim_epoch=current.claim_epoch + 1,
                )
                self._outbox[event.event_id] = event
                claimed.append(event)
            return claimed

    def _validate_delivery(
        self, fence: DeliveryFence
    ) -> CoordinatorResult[OutboxEvent] | OutboxEvent:
        event = self._outbox.get(fence.event_id)
        if event is None:
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.NOT_FOUND)
        matching = event.claim_owner == fence.owner_id and event.claim_epoch == fence.claim_epoch
        if event.status is DeliveryState.DELIVERED:
            if matching:
                return self._result(
                    CoordinatorDecision.UNCHANGED,
                    CoordinatorReason.ALREADY_DELIVERED,
                    event,
                )
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.STALE_FENCE)
        if (
            event.status is not DeliveryState.CLAIMED
            or not matching
            or event.claim_expires_at <= self._clock()
        ):
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.STALE_FENCE)
        return event

    async def release_outbox(
        self, fence: DeliveryFence, available_at: float
    ) -> CoordinatorResult[OutboxEvent]:
        async with self._lock:
            validated = self._validate_delivery(fence)
            if isinstance(validated, CoordinatorResult):
                return validated
            if not math.isfinite(available_at):
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.INVALID_TRANSITION,
                )
            event = replace(
                validated,
                status=DeliveryState.PENDING,
                available_at=available_at,
                claim_owner="",
                claim_expires_at=0.0,
            )
            self._outbox[event.event_id] = event
            return self._result(
                CoordinatorDecision.APPLIED, CoordinatorReason.DELIVERY_RELEASED, event
            )

    async def mark_delivered(self, fence: DeliveryFence) -> CoordinatorResult[OutboxEvent]:
        async with self._lock:
            validated = self._validate_delivery(fence)
            if isinstance(validated, CoordinatorResult):
                return validated
            event = replace(
                validated,
                status=DeliveryState.DELIVERED,
                delivered_at=self._clock(),
            )
            self._outbox[event.event_id] = event
            return self._result(CoordinatorDecision.APPLIED, CoordinatorReason.DELIVERED, event)

    async def get_run(self, run_id: str) -> RunRecord | None:
        async with self._lock:
            return self._runs.get(run_id)
