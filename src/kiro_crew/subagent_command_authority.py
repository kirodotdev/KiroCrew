"""Coordinator-backed command admission for the synchronous subagent facade.

The manager remains the local executor.  This boundary makes a keyed command
durable before calling it and consumes coordinator replays without repeating
the manager side effect.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from .run_coordinator.models import (
    CommandClaim,
    CommandFence,
    CommandOperation,
    CommandStatus,
    CoordinatorDecision,
    DeliveryState,
    ObservedState,
    OwnerLease,
    RunCompletion,
    RunCoordinator,
    RunFence,
    RunOutcome,
    RunRecord,
    SubmitControl,
    SubmitRun,
)
from .security import redact_credentials, redact_exfiltration_urls

_CONTROL_LEASE_SECS = 30.0
_SHUTDOWN_SETTLEMENT_RETRY_SECS = 1.0
_SHUTDOWN_SETTLEMENT_MAX_ATTEMPTS = 3
# Cancellation includes the manager's bounded 20-minute parent-delivery wait.
# The extra two minutes cover termination and the durable finish write without
# forcing unrelated control commands to remain claimed after a gateway crash.
_CANCEL_CONTROL_LEASE_SECS = 22.0 * 60.0
EXECUTION_LEASE_SECONDS = 90.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandIdentity:
    """Stable identity generated before a mutation crosses a transport boundary."""

    run_id: str
    command_id: str
    idempotency_key: str


@dataclass(frozen=True)
class AdmittedExecution:
    """Durable replay view when no live manager record is available."""

    id: str
    task: str
    done: bool = False
    error: str = ""
    queued: bool = False
    counted: bool = True
    batch_id: str = ""
    batch_total: int = 0
    silent: bool = False


@dataclass(frozen=True)
class _PendingExecutionFailure:
    result: Any
    claim: CommandClaim
    run: RunRecord
    batch_id: str
    batch_total: int


class AuthorityError(RuntimeError):
    """Base error for a mutation that cannot safely reach the local executor."""


class AuthorityConflict(AuthorityError):
    """The stable key was reused for a different canonical payload."""


class AuthorityUnavailable(AuthorityError):
    """The coordinator could not prove that this caller owns the command."""


class AuthorityOutcomeUncertain(AuthorityUnavailable):
    """The local side effect ran but its durable command result is uncertain."""


_T = TypeVar("_T")


def _redact(value: str) -> str:
    value, _ = redact_exfiltration_urls(value)
    value, _ = redact_credentials(value)
    return value


def _redact_result(value: Any) -> Any:
    """Redact every string in a provider-owned control result."""

    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, tuple):
        return tuple(_redact_result(item) for item in value)
    if isinstance(value, list):
        return [_redact_result(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_result(item) for key, item in value.items()}
    return value


class SubagentCommandAuthority:
    """Admit keyed mutations before invoking the existing manager methods.

    Execution methods deliberately remain synchronous on the manager.  The
    authority awaits only the durable admission, then calls the manager without
    another yield, keeping its event-loop-affine scheduler and registries atomic.
    """

    def __init__(
        self,
        coordinator: RunCoordinator,
        manager: Any,
        *,
        owner_id: str | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._coordinator = coordinator
        self._manager = manager
        self._owner_id = owner_id or f"gateway:{uuid.uuid4().hex}"
        self._clock = clock
        self._sleep = sleep
        self._inflight: dict[str, tuple[str, asyncio.Task[Any]]] = {}
        # Queued records are absent from manager.get() until the stagger pump
        # starts them. Retain the accepted facade result for keyed replays.
        self._execution_results: dict[str, Any] = {}
        self._waiting_executions: dict[str, tuple[CommandFence, str]] = {}
        self._waiting_execution_keys: dict[str, str] = {}
        self._pending_execution_failures: dict[str, _PendingExecutionFailure] = {}
        self._lease_tasks: dict[str, asyncio.Task[None]] = {}

    @staticmethod
    def _payload(operation: str, **values: Any) -> tuple[str, str]:
        payload_json = json.dumps(
            {"operation": operation, **values},
            separators=(",", ":"),
            sort_keys=True,
        )
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        return payload_json, payload_hash

    async def _coalesce(
        self,
        identity: CommandIdentity,
        payload_hash: str,
        operation: Callable[[], Coroutine[Any, Any, _T]],
    ) -> _T:
        existing = self._inflight.get(identity.idempotency_key)
        if existing is not None:
            existing_hash, task = existing
            if existing_hash != payload_hash:
                raise AuthorityConflict("idempotency_conflict")
            return cast(_T, await asyncio.shield(task))

        task = asyncio.create_task(operation())
        self._inflight[identity.idempotency_key] = (payload_hash, task)
        try:
            return cast(_T, await asyncio.shield(task))
        finally:
            current = self._inflight.get(identity.idempotency_key)
            if current is not None and current[1] is task and task.done():
                self._inflight.pop(identity.idempotency_key, None)

    @staticmethod
    def _reason(result: Any) -> str:
        reason = getattr(result, "reason", "")
        return str(getattr(reason, "value", reason) or "coordinator_rejected")

    @staticmethod
    async def _before_side_effect(operation: Awaitable[_T], boundary: str) -> _T:
        """Keep coordinator uncertainty distinct from a definite rejection."""

        try:
            return await operation
        except Exception as exc:
            raise AuthorityUnavailable(f"coordinator {boundary} failed") from exc

    async def _finish_failed_side_effect(
        self,
        fence: CommandFence,
        failure: Exception,
        boundary: str,
        *,
        result_json: str = "",
    ) -> None:
        """Fail closed when a provider failure cannot be durably settled."""

        try:
            finished = await self._coordinator.finish_command(
                fence,
                CommandStatus.REJECTED,
                rejection_reason=type(failure).__name__,
                result_json=result_json,
            )
        except Exception as exc:
            raise AuthorityOutcomeUncertain(f"{boundary} failure was not durably finished") from exc
        if finished.decision is CoordinatorDecision.REJECTED:
            raise AuthorityOutcomeUncertain(
                f"{boundary} failure was not durably finished: {self._reason(finished)}"
            ) from failure

    async def lookup_response(self, idempotency_key: str) -> dict[str, object] | None:
        """Reconstruct the transport response without reapplying the command."""

        receipt = await self._before_side_effect(
            self._coordinator.get_command_by_key(idempotency_key),
            "lookup",
        )
        if receipt is None:
            return None
        try:
            payload = json.loads(receipt.command.payload_json)
        except (TypeError, ValueError) as exc:
            raise AuthorityUnavailable("stored command payload is invalid") from exc
        if not isinstance(payload, dict):
            raise AuthorityUnavailable("stored command payload has an invalid shape")

        operation = receipt.command.operation
        if operation in (CommandOperation.SPAWN, CommandOperation.CONTINUE):
            return self._lookup_execution_response(receipt, payload)
        return self._lookup_control_response(receipt, payload)

    @classmethod
    def _lookup_execution_response(cls, receipt: Any, payload: dict[str, Any]) -> dict[str, object]:
        command = receipt.command
        run_id = str(payload.get("run_id") or command.run_id)
        task = _redact(str(payload.get("task") or ""))
        conversation_id = str(payload.get("conversation_id") or "")
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}

        stored: AdmittedExecution | None = None
        if command.result_json:
            stored = cls._decode_execution_result(command.result_json, run_id, task)
        run = receipt.run
        if (
            not command.result_json
            and run is not None
            and run.observed_state is ObservedState.TERMINAL
            and run.outcome is not None
            and run.outcome is not RunOutcome.COMPLETED
        ):
            return {
                "found": True,
                "id": run_id,
                "error": run.error or f"run ended {run.outcome.value}",
                "code": f"run_{run.outcome.value}",
                "counted": True,
            }
        if stored is not None and stored.done and stored.error:
            code = (
                cls._continue_error_code(stored.error)
                if command.operation is CommandOperation.CONTINUE
                else "spawn_rejected"
            )
            stored_response: dict[str, object] = {
                "found": True,
                "id": run_id,
                "error": stored.error,
                "code": code,
            }
            if stored.counted:
                stored_response["counted"] = True
            return stored_response
        if command.status is CommandStatus.REJECTED:
            error = (
                stored.error
                if stored is not None and stored.error
                else command.rejection_reason or "command rejected"
            )
            error = _redact(error)
            if command.operation is CommandOperation.CONTINUE:
                code = cls._continue_error_code(error)
            else:
                code = "spawn_rejected"
            rejected_response: dict[str, object] = {
                "found": True,
                "id": run_id,
                "error": error,
                "code": code,
            }
            if stored is None or stored.counted:
                rejected_response["counted"] = True
            return rejected_response
        if not command.result_json and command.status in (
            CommandStatus.PENDING,
            CommandStatus.CLAIMED,
        ):
            return {
                "found": True,
                "id": run_id,
                "error": "command outcome is still pending",
                "status": "pending",
                "code": "command_pending",
                "command_status": command.status.value,
            }

        if command.operation is CommandOperation.CONTINUE:
            return {
                "found": True,
                "id": run_id,
                "conversation": conversation_id,
                "status": "spawned",
            }
        response: dict[str, object] = {
            "found": True,
            "id": run_id,
            "task": task,
            "status": "spawned",
        }
        if bool(arguments.get("keep")):
            response["conversation"] = run_id
        return response

    @classmethod
    def _lookup_control_response(cls, receipt: Any, payload: dict[str, Any]) -> dict[str, object]:
        command = receipt.command
        target = str(payload.get("run_id") or command.run_id)
        if not command.result_json:
            if command.status in (CommandStatus.PENDING, CommandStatus.CLAIMED):
                return {
                    "found": True,
                    "id": target,
                    "error": "command outcome is still pending",
                    "status": "pending",
                    "code": "command_pending",
                    "command_status": command.status.value,
                }
            return {
                "found": True,
                "id": target,
                "error": command.rejection_reason or "command rejected",
                "code": "command_rejected",
            }

        result = cls._decode_control_result(command.operation, command.result_json)
        if command.operation is CommandOperation.CANCEL:
            return {"found": True, "ok": True, "cancelled": bool(result)}
        ok, detail = cast(tuple[bool, str], result)
        if command.operation is CommandOperation.RELEASE:
            if ok:
                return {
                    "found": True,
                    "conversation": target,
                    "status": "released",
                }
            return {
                "found": True,
                "id": target,
                "error": detail,
                "code": (
                    "conversation_busy"
                    if detail.startswith("conversation_busy")
                    else "conversation_gone"
                ),
            }

        mode = str(payload.get("mode") or "interrupt")
        if ok:
            return {
                "found": True,
                "id": target,
                "status": "follow_up_queued" if mode == "follow_up" else "steered",
            }
        return {
            "found": True,
            "id": target,
            "error": detail,
            "code": cls._steer_error_code(detail),
        }

    @staticmethod
    def _continue_error_code(error: str) -> str:
        if error.startswith("conversation_busy"):
            return "conversation_busy"
        if error.startswith("conversation_gone"):
            return "conversation_gone"
        return "spawn_rejected"

    @staticmethod
    def _steer_error_code(error: str) -> str:
        if error == "not_found":
            return "not_found"
        if error.startswith("not_running"):
            return "not_running"
        if error.startswith("session_starting"):
            return "session_starting"
        return "steer_failed"

    async def spawn(self, identity: CommandIdentity, task: str, **kwargs: Any) -> Any:
        """Durably admit a spawn, then invoke the synchronous manager once."""

        return await self._execution(
            identity,
            CommandOperation.SPAWN,
            task,
            conversation_id="",
            kwargs=kwargs,
        )

    async def continue_conversation(
        self,
        identity: CommandIdentity,
        conversation_id: str,
        task: str,
        **kwargs: Any,
    ) -> Any:
        """Durably admit a continuation, preserving its preassigned run id."""

        return await self._execution(
            identity,
            CommandOperation.CONTINUE,
            task,
            conversation_id=conversation_id,
            kwargs=kwargs,
        )

    async def _execution(
        self,
        identity: CommandIdentity,
        operation: CommandOperation,
        task: str,
        *,
        conversation_id: str,
        kwargs: dict[str, Any],
    ) -> Any:
        if "_preassigned_id" in kwargs:
            raise ValueError("CommandIdentity owns _preassigned_id")
        payload_json, payload_hash = self._payload(
            operation.value,
            run_id=identity.run_id,
            conversation_id=conversation_id,
            task=task,
            arguments=kwargs,
        )
        persisted_payload_json = json.dumps(
            _redact_result(json.loads(payload_json)),
            separators=(",", ":"),
            sort_keys=True,
        )

        async def admit() -> Any:
            existing = await self._before_side_effect(
                self._coordinator.get_command_by_key(identity.idempotency_key),
                "lookup",
            )
            matching_pending = (
                existing is not None
                and existing.command.status is CommandStatus.PENDING
                and existing.command.operation is operation
                and existing.command.payload_hash == payload_hash
            )
            if existing is not None and not matching_pending:
                return await admit_reserved()
            reserve = getattr(self._manager, "reserve_coordinator_run_id", None)
            if callable(reserve):
                reserved = bool(reserve(identity.run_id))
            else:
                queued_collision = any(
                    str(entry.get("_preassigned_id") or "") == identity.run_id
                    for entry in getattr(self._manager, "_queue", ())
                )
                reserved = self._manager.get(identity.run_id) is None and not queued_collision
            if not reserved:
                return AdmittedExecution(
                    identity.run_id,
                    task,
                    done=True,
                    error="run_id_conflict: an active legacy run already owns this id",
                    counted=False,
                )
            try:
                return await admit_reserved()
            finally:
                release = getattr(self._manager, "release_coordinator_run_id", None)
                if callable(release):
                    release(identity.run_id)

        async def admit_reserved() -> Any:
            conversation_key = f"subagent:{conversation_id}" if conversation_id else ""
            result = await self._before_side_effect(
                self._coordinator.submit(
                    SubmitRun(
                        run_id=identity.run_id,
                        command_id=identity.command_id,
                        idempotency_key=identity.idempotency_key,
                        payload_hash=payload_hash,
                        payload_json=persisted_payload_json,
                        parent_session=str(kwargs.get("parent_session_key") or ""),
                        agent=_redact(str(kwargs.get("agent") or "")),
                        task=_redact(task),
                        conversation_key=conversation_key,
                        operation=operation,
                    )
                ),
                "submission",
            )
            if result.decision is CoordinatorDecision.REJECTED:
                raise AuthorityConflict(self._reason(result))
            receipt = result.value
            if receipt is None:
                raise AuthorityUnavailable("coordinator omitted the submission receipt")
            if receipt.run is None:
                raise AuthorityUnavailable("execution receipt omitted the run record")
            if not receipt.created:
                if (
                    not receipt.command.result_json
                    and receipt.run.observed_state is ObservedState.TERMINAL
                    and receipt.run.outcome is not None
                    and receipt.run.outcome is not RunOutcome.COMPLETED
                ):
                    try:
                        replay_payload = json.loads(receipt.command.payload_json)
                    except (TypeError, ValueError):
                        replay_payload = {}
                    replay_arguments = replay_payload.get("arguments", {})
                    if not isinstance(replay_arguments, dict):
                        replay_arguments = {}
                    self._pending_execution_failures.pop(receipt.command.command_id, None)
                    self._execution_results.pop(receipt.run.run_id, None)
                    return AdmittedExecution(
                        receipt.run.run_id,
                        _redact(receipt.run.task),
                        done=True,
                        error=receipt.run.error or f"run ended {receipt.run.outcome.value}",
                        batch_id=str(replay_arguments.get("batch_id") or ""),
                        batch_total=int(replay_arguments.get("batch_total") or 0),
                        silent=bool(replay_arguments.get("silent")),
                    )
                pending_failure = self._pending_execution_failures.get(receipt.command.command_id)
                if pending_failure is not None:
                    return await self._persist_execution_failure(
                        receipt.command.command_id,
                        pending_failure,
                    )
                if receipt.run.run_id in self._execution_results:
                    return self._execution_results[receipt.run.run_id]
                replay = self._manager.get(receipt.run.run_id)
                if replay is not None:
                    return replay
                if receipt.command.result_json:
                    return self._decode_execution_result(
                        receipt.command.result_json, receipt.run.run_id, receipt.run.task
                    )
                if receipt.command.status is CommandStatus.REJECTED:
                    raise AuthorityConflict(
                        receipt.command.rejection_reason or self._reason(result)
                    )
                # A PENDING command has never been claimed, so no executor can
                # have crossed the manager side-effect boundary.  Reclaiming it
                # is the recovery path for a crash after durable submission but
                # before the first claim.  CLAIMED remains uncertain: the prior
                # owner may already have invoked the manager.
                if receipt.command.status is not CommandStatus.PENDING:
                    raise AuthorityUnavailable("command outcome is still pending")

            claim = await self._before_side_effect(
                self._coordinator.claim_command(
                    identity.command_id,
                    self._owner_lease(execution=True),
                ),
                "claim",
            )
            if claim is None:
                raise AuthorityUnavailable("command outcome is still pending")
            if claim.fence is None or claim.run is None:
                raise AuthorityUnavailable("execution claim omitted its run fence")
            queued_legacy_collision = any(
                str(entry.get("_preassigned_id") or "") == receipt.run.run_id
                for entry in getattr(self._manager, "_queue", ())
            )
            if self._manager.get(receipt.run.run_id) is not None or queued_legacy_collision:
                local_result = AdmittedExecution(
                    receipt.run.run_id,
                    receipt.run.task,
                    done=True,
                    error="run_id_conflict: an active legacy run already owns this id",
                    counted=False,
                )
                result_json = self._encode_execution_result(local_result, receipt.run.run_id)
                try:
                    finished = await self._coordinator.finish_command(
                        claim.command_fence,
                        CommandStatus.REJECTED,
                        rejection_reason="run_id_conflict",
                        result_json=result_json,
                    )
                except Exception as exc:
                    raise AuthorityOutcomeUncertain(
                        "run id conflict was not durably rejected"
                    ) from exc
                if finished.decision is CoordinatorDecision.REJECTED:
                    raise AuthorityOutcomeUncertain(
                        f"run id conflict was not durably rejected: {self._reason(finished)}"
                    )
                return local_result
            call_kwargs = {
                **kwargs,
                "_preassigned_id": receipt.run.run_id,
                "_coordinator_admitted": True,
                "_coordinator_command": claim.command,
                "_coordinator_fence": claim.fence,
                "_coordinator_version": claim.run.version,
            }
            try:
                if operation is CommandOperation.CONTINUE:
                    local_result = self._manager.continue_conversation(
                        conversation_id, task, **call_kwargs
                    )
                else:
                    local_result = self._manager.spawn(task, **call_kwargs)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    raise
                try:
                    registered = self._manager.get(receipt.run.run_id)
                except Exception as lookup_exc:
                    raise AuthorityOutcomeUncertain(
                        "manager acceptance could not be determined"
                    ) from lookup_exc
                if registered is not None:
                    if not bool(getattr(registered, "done", False)) or not str(
                        getattr(registered, "error", "") or ""
                    ):
                        raise AuthorityOutcomeUncertain(
                            "manager accepted execution before reporting a local failure"
                        ) from exc
                    local_result = registered
                else:
                    local_result = AdmittedExecution(
                        receipt.run.run_id,
                        receipt.run.task,
                        done=True,
                        error=_redact(str(exc) or type(exc).__name__),
                        batch_id=str(kwargs.get("batch_id") or ""),
                        batch_total=int(kwargs.get("batch_total") or 0),
                        silent=bool(kwargs.get("silent")),
                    )
                    pending_failure = _PendingExecutionFailure(
                        local_result,
                        claim,
                        receipt.run,
                        local_result.batch_id,
                        local_result.batch_total,
                    )
                    self._pending_execution_failures[receipt.command.command_id] = pending_failure
                    return await self._persist_execution_failure(
                        receipt.command.command_id,
                        pending_failure,
                    )
            waiting = bool(
                getattr(
                    local_result,
                    "_coordinator_waiting",
                    getattr(local_result, "queued", False),
                )
            )
            result_json = self._encode_execution_result(local_result, receipt.run.run_id)
            if waiting:
                if claim.fence is None:
                    raise AuthorityUnavailable("execution claim omitted its run fence")
                self._execution_results[receipt.run.run_id] = local_result
                self._waiting_executions[receipt.run.run_id] = (
                    claim.command_fence,
                    result_json,
                )
                self._waiting_execution_keys[receipt.run.run_id] = identity.idempotency_key
                self._start_execution_heartbeat(receipt.run.run_id, claim.fence)
                return local_result
            if not self._execution_succeeded(local_result):
                pending_failure = _PendingExecutionFailure(
                    local_result,
                    claim,
                    receipt.run,
                    str(getattr(local_result, "batch_id", "") or kwargs.get("batch_id") or ""),
                    int(getattr(local_result, "batch_total", 0) or kwargs.get("batch_total") or 0),
                )
                self._pending_execution_failures[receipt.command.command_id] = pending_failure
                return await self._persist_execution_failure(
                    receipt.command.command_id,
                    pending_failure,
                )
            await self._finish_execution_result(claim, result_json)
            return local_result

        return await self._coalesce(identity, payload_hash, admit)

    async def _persist_execution_failure(
        self,
        command_id: str,
        pending: _PendingExecutionFailure,
    ) -> Any:
        event_id = await self._complete_execution_failure(
            pending.claim,
            pending.run,
            pending.result,
            batch_id=pending.batch_id,
            batch_total=pending.batch_total,
        )
        safe_result = AdmittedExecution(
            id=str(getattr(pending.result, "id", pending.run.run_id) or pending.run.run_id),
            task=_redact(pending.run.task),
            done=bool(getattr(pending.result, "done", True)),
            error=_redact(
                str(getattr(pending.result, "error", "") or "manager rejected execution")
            ),
            queued=bool(getattr(pending.result, "queued", False)),
            batch_id=pending.batch_id,
            batch_total=pending.batch_total,
            silent=bool(getattr(pending.result, "silent", False)),
            counted=bool(getattr(pending.result, "counted", True)),
        )
        result_json = self._encode_execution_result(safe_result, pending.run.run_id)
        try:
            await self._finish_execution_result(pending.claim, result_json)
        except Exception:
            # The terminal run and its outbox event are already durable.  A
            # command-result fill is useful for exact response reconstruction,
            # but it cannot turn that counted terminal outcome back into an
            # uncounted transport failure.  Replays fall back to the terminal
            # run when this best-effort fill is unavailable.
            logger.exception(
                "Failed to store the facade result for terminal run %s",
                pending.run.run_id,
            )
        self._execution_results.pop(pending.run.run_id, None)
        self._pending_execution_failures.pop(command_id, None)
        if pending.batch_id:
            try:
                await self._deliver_execution_failure(event_id)
            except Exception:
                logger.exception(
                    "Durable execution rejection delivery failed for event %s",
                    event_id,
                )
        return safe_result

    async def _finish_execution_result(self, claim: Any, result_json: str) -> None:
        try:
            finished = await self._coordinator.finish_command(
                claim.command_fence,
                CommandStatus.APPLIED,
                result_json=result_json,
            )
        except Exception as exc:
            raise AuthorityOutcomeUncertain("execution result was not durably finished") from exc
        if finished.decision is CoordinatorDecision.REJECTED:
            raise AuthorityOutcomeUncertain(
                f"execution result was not durably finished: {self._reason(finished)}"
            )

    async def _complete_execution_failure(
        self,
        claim: CommandClaim,
        run: RunRecord,
        local_result: Any,
        *,
        batch_id: str,
        batch_total: int,
    ) -> str:
        if claim.fence is None:
            raise AuthorityUnavailable("execution failure omitted its run fence")
        try:
            latest_run = await self._coordinator.get_run(run.run_id)
        except Exception as exc:
            raise AuthorityOutcomeUncertain(
                "execution rejection lifecycle could not be refreshed"
            ) from exc
        if latest_run is not None and latest_run.observed_state is not ObservedState.TERMINAL:
            run = latest_run
        error = _redact(str(getattr(local_result, "error", "") or "manager rejected execution"))
        task = _redact(run.task)
        try:
            stored_payload = json.loads(claim.command.payload_json)
        except (TypeError, ValueError):
            stored_payload = {}
        stored_arguments = stored_payload.get("arguments", {})
        if not isinstance(stored_arguments, dict):
            stored_arguments = {}
        requested_model = _redact(
            str(getattr(local_result, "requested_model", "") or stored_arguments.get("model") or "")
        )
        payload_json = json.dumps(
            {
                "id": run.run_id,
                "parent_session_key": run.parent_session,
                "agent": _redact(run.agent),
                "task": task[:1000],
                "outcome": RunOutcome.FAILED.value,
                "error": error[:2000],
                "result_path": str(getattr(local_result, "result_path", "") or ""),
                "result_summary": "",
                "result_truncated": False,
                "user_stopped": False,
                "silent": bool(getattr(local_result, "silent", False)),
                "batch_id": batch_id,
                "batch_total": batch_total,
                "elapsed": float(getattr(local_result, "elapsed", 0.0) or 0.0),
                "conversation_key": run.conversation_key,
                "resolved_model": _redact(str(getattr(local_result, "resolved_model", ""))),
                "requested_model": requested_model,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            completed = await self._coordinator.complete(
                RunCompletion(
                    run_id=run.run_id,
                    outcome=RunOutcome.FAILED,
                    result_path=str(getattr(local_result, "result_path", "") or ""),
                    error=error,
                    event_type="subagent_completion",
                    destination=run.parent_session,
                    payload_json=payload_json,
                    terminal_at=self._clock(),
                    delivery_state=(DeliveryState.PENDING if batch_id else DeliveryState.DELIVERED),
                ),
                claim.fence,
                run.version,
            )
        except Exception as exc:
            raise AuthorityOutcomeUncertain(
                "execution rejection was not durably completed"
            ) from exc
        if completed.value is None or completed.decision is CoordinatorDecision.REJECTED:
            raise AuthorityOutcomeUncertain(
                f"execution rejection was not durably completed: {self._reason(completed)}"
            )
        if batch_id:
            self._manager.prepare_coordinator_rejection(
                run.run_id,
                batch_id=batch_id,
                batch_total=batch_total,
            )
        return completed.value.event_id

    async def _deliver_execution_failure(self, event_id: str) -> None:
        await self._manager.deliver_coordinator_event(event_id)

    async def steer(self, identity: CommandIdentity, run_id: str, message: str) -> tuple[bool, str]:
        payload = {"message": message, "mode": "interrupt"}
        return cast(
            tuple[bool, str],
            await self._control(
                identity,
                run_id,
                CommandOperation.STEER,
                payload,
                lambda: self._manager.steer_run(run_id, message),
            ),
        )

    async def follow_up(
        self, identity: CommandIdentity, run_id: str, message: str
    ) -> tuple[bool, str]:
        payload = {"message": message, "mode": "follow_up"}
        return cast(
            tuple[bool, str],
            await self._control(
                identity,
                run_id,
                CommandOperation.STEER,
                payload,
                lambda: self._manager.follow_up_run(run_id, message),
            ),
        )

    async def cancel(self, identity: CommandIdentity, run_id: str) -> bool:
        return cast(
            bool,
            await self._control(
                identity,
                run_id,
                CommandOperation.CANCEL,
                {},
                lambda: self._manager.cancel(run_id),
            ),
        )

    async def release(self, identity: CommandIdentity, conversation_id: str) -> tuple[bool, str]:
        async def invoke() -> tuple[bool, str]:
            return await self._manager.release_conversation_async(conversation_id)

        return cast(
            tuple[bool, str],
            await self._control(
                identity,
                conversation_id,
                CommandOperation.RELEASE,
                {},
                invoke,
            ),
        )

    async def _control(
        self,
        identity: CommandIdentity,
        run_id: str,
        operation: CommandOperation,
        payload: dict[str, Any],
        invoke: Callable[[], Awaitable[Any]],
    ) -> Any:
        payload_json, payload_hash = self._payload(
            operation.value,
            run_id=run_id,
            **payload,
        )

        async def admit_and_apply() -> Any:
            result = await self._before_side_effect(
                self._coordinator.submit_control(
                    SubmitControl(
                        command_id=identity.command_id,
                        idempotency_key=identity.idempotency_key,
                        run_id=run_id,
                        operation=operation,
                        payload_hash=payload_hash,
                        payload_json=payload_json,
                    )
                ),
                "control submission",
            )
            if result.decision is CoordinatorDecision.REJECTED and result.value is None:
                raise AuthorityConflict(self._reason(result))
            receipt = result.value
            if receipt is None:
                raise AuthorityUnavailable("coordinator omitted the control receipt")
            command = receipt.command
            if command.result_json:
                return self._decode_control_result(operation, command.result_json)
            if command.status is CommandStatus.REJECTED:
                raise AuthorityConflict(command.rejection_reason or self._reason(result))
            if command.status is not CommandStatus.PENDING:
                raise AuthorityOutcomeUncertain(
                    "control outcome is uncertain and cannot be replayed safely"
                )

            claim = await self._before_side_effect(
                self._coordinator.claim_command(
                    identity.command_id,
                    self._owner_lease(
                        control_seconds=(
                            _CANCEL_CONTROL_LEASE_SECS
                            if operation is CommandOperation.CANCEL
                            else _CONTROL_LEASE_SECS
                        )
                    ),
                ),
                "control claim",
            )
            if claim is None:
                latest = await self._before_side_effect(
                    self._coordinator.get_command_by_key(identity.idempotency_key),
                    "control lookup",
                )
                if latest is not None and latest.command.result_json:
                    return self._decode_control_result(operation, latest.command.result_json)
                raise AuthorityUnavailable("control command is owned by another claimant")

            try:
                legacy_result = await invoke()
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    raise
                if isinstance(exc, AuthorityOutcomeUncertain):
                    raise
                await self._finish_failed_side_effect(
                    claim.command_fence,
                    exc,
                    "control",
                )
                raise

            safe_result = _redact_result(legacy_result)
            result_json = json.dumps(safe_result, separators=(",", ":"))
            status = (
                CommandStatus.APPLIED
                if self._control_succeeded(safe_result)
                else CommandStatus.REJECTED
            )
            try:
                finished = await self._coordinator.finish_command(
                    claim.command_fence,
                    status,
                    rejection_reason=("" if status is CommandStatus.APPLIED else "legacy_rejected"),
                    result_json=result_json,
                )
            except Exception as exc:
                raise AuthorityOutcomeUncertain("control result was not durably finished") from exc
            if finished.decision is CoordinatorDecision.REJECTED:
                raise AuthorityOutcomeUncertain(
                    f"control result was not durably finished: {self._reason(finished)}"
                )
            return safe_result

        return await self._coalesce(identity, payload_hash, admit_and_apply)

    def _start_execution_heartbeat(self, run_id: str, fence: RunFence) -> None:
        if run_id in self._lease_tasks:
            return

        async def renew() -> None:
            cadence = EXECUTION_LEASE_SECONDS / 3
            while True:
                await self._sleep(cadence)
                try:
                    renewed = await self._coordinator.renew(
                        run_id,
                        fence,
                        self._clock() + EXECUTION_LEASE_SECONDS,
                    )
                except Exception:
                    continue
                if not renewed:
                    return

        task = asyncio.create_task(renew())
        self._lease_tasks[run_id] = task

        def forget(done: asyncio.Task[None]) -> None:
            if self._lease_tasks.get(run_id) is done:
                self._lease_tasks.pop(run_id, None)

        task.add_done_callback(forget)

    async def close(self) -> None:
        """Settle accepted queue work before stopping its lease renewals."""

        for _attempt in range(_SHUTDOWN_SETTLEMENT_MAX_ATTEMPTS):
            if not self._pending_execution_failures:
                break
            settlement_failed = False
            for command_id, pending in tuple(self._pending_execution_failures.items()):
                if await self._release_superseded_execution_failure(
                    command_id,
                    pending,
                ):
                    continue
                try:
                    await self._persist_execution_failure(command_id, pending)
                except Exception:
                    if await self._release_superseded_execution_failure(
                        command_id,
                        pending,
                    ):
                        continue
                    settlement_failed = True
                    logger.warning(
                        "Execution rejection %s was not durable during shutdown; retrying",
                        pending.run.run_id,
                        exc_info=True,
                    )
            if self._pending_execution_failures:
                await self._sleep(_SHUTDOWN_SETTLEMENT_RETRY_SECS if settlement_failed else 0.0)
        if self._pending_execution_failures:
            logger.error(
                "Leaving %d execution rejections for durable lease takeover during shutdown",
                len(self._pending_execution_failures),
            )
        for _attempt in range(_SHUTDOWN_SETTLEMENT_MAX_ATTEMPTS):
            if not self._waiting_executions:
                break
            settlement_failed = False
            for run_id in tuple(self._waiting_executions):
                try:
                    unqueue = getattr(self._manager, "_unqueue", None)
                    if callable(unqueue):
                        unqueue(run_id)
                    await self.reject_waiting_execution(
                        run_id,
                        "gateway shut down before execution",
                    )
                except Exception:
                    if await self._release_superseded_waiting_execution(run_id):
                        continue
                    settlement_failed = True
                    logger.warning(
                        "Waiting execution %s was not durably rejected during shutdown; retrying",
                        run_id,
                        exc_info=True,
                    )
            if settlement_failed:
                await self._sleep(_SHUTDOWN_SETTLEMENT_RETRY_SECS)
        if self._waiting_executions:
            logger.error(
                "Leaving %d waiting executions for durable lease takeover during shutdown",
                len(self._waiting_executions),
            )
        tasks = list(self._lease_tasks.values())
        self._lease_tasks.clear()
        self._execution_results.clear()
        self._waiting_executions.clear()
        self._waiting_execution_keys.clear()
        self._pending_execution_failures.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _release_superseded_execution_failure(
        self,
        command_id: str,
        pending: _PendingExecutionFailure,
    ) -> bool:
        """Release local rejection debt after durable state owns the outcome."""

        try:
            receipt = await self._coordinator.get_command_by_key(
                pending.claim.command.idempotency_key
            )
        except Exception:
            return False
        if receipt is None or receipt.command.command_id != command_id:
            return False
        command = receipt.command
        fence = pending.claim.command_fence
        terminal_run = (
            receipt.run is not None and receipt.run.observed_state is ObservedState.TERMINAL
        )
        terminal_command = command.status in (
            CommandStatus.APPLIED,
            CommandStatus.REJECTED,
        )
        newer_claim = command.owner_id != fence.owner_id or command.claim_epoch != fence.claim_epoch
        if not terminal_run and not terminal_command and not newer_claim:
            return False
        if self._pending_execution_failures.get(command_id) is pending:
            self._pending_execution_failures.pop(command_id, None)
        self._execution_results.pop(pending.run.run_id, None)
        logger.info(
            "Execution rejection %s was settled or superseded by durable command claim %s",
            pending.run.run_id,
            command.claim_epoch,
        )
        return True

    async def stop_execution_heartbeat(self, run_id: str) -> None:
        """Stop the queue lease after the manager starts or terminals the run."""

        self._execution_results.pop(run_id, None)
        self._waiting_executions.pop(run_id, None)
        self._waiting_execution_keys.pop(run_id, None)
        task = self._lease_tasks.pop(run_id, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _release_superseded_waiting_execution(self, run_id: str) -> bool:
        """Release local shutdown debt after another durable fence owns it."""

        waiting = self._waiting_executions.get(run_id)
        idempotency_key = self._waiting_execution_keys.get(run_id)
        if waiting is None or not idempotency_key:
            return False
        command_fence, _result_json = waiting
        try:
            receipt = await self._coordinator.get_command_by_key(idempotency_key)
        except Exception:
            return False
        if receipt is None or receipt.command.command_id != command_fence.command_id:
            return False
        command = receipt.command
        terminal = command.status in (CommandStatus.APPLIED, CommandStatus.REJECTED)
        newer_claim = (
            command.owner_id != command_fence.owner_id
            or command.claim_epoch != command_fence.claim_epoch
        )
        if not terminal and not newer_claim:
            return False
        logger.info(
            "Waiting execution %s was settled or superseded by durable command claim %s",
            run_id,
            command.claim_epoch,
        )
        await self.stop_execution_heartbeat(run_id)
        return True

    async def _waiting_execution_is_applied(self, run_id: str) -> bool:
        """Confirm that this authority's exact start settlement committed."""

        waiting = self._waiting_executions.get(run_id)
        idempotency_key = self._waiting_execution_keys.get(run_id)
        if waiting is None or not idempotency_key:
            return False
        command_fence, result_json = waiting
        try:
            receipt = await self._coordinator.get_command_by_key(idempotency_key)
        except Exception:
            return False
        if receipt is None:
            return False
        command = receipt.command
        return (
            command.command_id == command_fence.command_id
            and command.owner_id == command_fence.owner_id
            and command.claim_epoch == command_fence.claim_epoch
            and command.status is CommandStatus.APPLIED
            and command.result_json == result_json
        )

    async def reject_waiting_execution(
        self,
        run_id: str,
        error: str,
        *,
        stop_heartbeat: bool = True,
    ) -> None:
        """Reject waiting work, optionally retaining its lease through terminal commit."""

        waiting = self._waiting_executions.get(run_id)
        finish_error = "waiting execution claim not found" if waiting is None else ""
        if waiting is not None:
            command_fence, _result_json = waiting
            safe_error = _redact(error)
            result_json = self._encode_execution_result(
                AdmittedExecution(run_id, "", done=True, error=safe_error),
                run_id,
            )
            try:
                finished = await self._coordinator.finish_command(
                    command_fence,
                    CommandStatus.REJECTED,
                    rejection_reason=safe_error,
                    result_json=result_json,
                )
            except Exception as exc:
                finish_error = str(exc) or type(exc).__name__
            else:
                if finished.decision is CoordinatorDecision.REJECTED:
                    finish_error = self._reason(finished)
        if finish_error:
            raise AuthorityOutcomeUncertain(
                f"waiting execution rejection was not durably finished: {finish_error}"
            )
        if stop_heartbeat:
            await self.stop_execution_heartbeat(run_id)

    async def execution_started(self, run_id: str) -> None:
        """Commit a waiting command only when its manager task actually starts."""

        waiting = self._waiting_executions.get(run_id)
        if waiting is not None:
            command_fence, result_json = waiting
            try:
                finished = await self._coordinator.finish_command(
                    command_fence,
                    CommandStatus.APPLIED,
                    result_json=result_json,
                )
            except Exception as exc:
                if not await self._waiting_execution_is_applied(run_id):
                    raise AuthorityOutcomeUncertain(
                        "execution start was not durably finished"
                    ) from exc
            else:
                if finished.decision is CoordinatorDecision.REJECTED:
                    if not await self._waiting_execution_is_applied(run_id):
                        reason = self._reason(finished)
                        raise AuthorityOutcomeUncertain(
                            f"execution start was not durably finished: {reason}"
                        )
        await self.stop_execution_heartbeat(run_id)

    def _owner_lease(
        self,
        *,
        execution: bool = False,
        control_seconds: float = _CONTROL_LEASE_SECS,
    ) -> OwnerLease:
        return OwnerLease(
            owner_id=self._owner_id,
            lease_expires_at=self._clock()
            + (EXECUTION_LEASE_SECONDS if execution else control_seconds),
        )

    @staticmethod
    def _execution_succeeded(result: Any) -> bool:
        return result is not None and not (
            bool(getattr(result, "done", False)) and bool(getattr(result, "error", ""))
        )

    @staticmethod
    def _encode_execution_result(result: Any, run_id: str) -> str:
        if result is None:
            payload: dict[str, Any] = {"has_info": False, "id": run_id}
        else:
            payload = {
                "has_info": True,
                "id": str(getattr(result, "id", run_id) or run_id),
                "done": bool(getattr(result, "done", False)),
                "error": _redact(str(getattr(result, "error", "") or "")),
                "queued": bool(getattr(result, "queued", False)),
                "counted": bool(getattr(result, "counted", True)),
                "batch_id": str(getattr(result, "batch_id", "") or ""),
                "batch_total": int(getattr(result, "batch_total", 0) or 0),
                "silent": bool(getattr(result, "silent", False)),
            }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _decode_execution_result(
        result_json: str, run_id: str, task: str
    ) -> AdmittedExecution | None:
        payload = json.loads(result_json)
        if not isinstance(payload, dict):
            raise AuthorityUnavailable("stored execution result has an invalid shape")
        if not payload.get("has_info"):
            return None
        return AdmittedExecution(
            id=str(payload.get("id") or run_id),
            task=task,
            done=bool(payload.get("done")),
            error=_redact(str(payload.get("error") or "")),
            queued=bool(payload.get("queued")),
            counted=bool(payload.get("counted", True)),
            batch_id=str(payload.get("batch_id") or ""),
            batch_total=int(payload.get("batch_total") or 0),
            silent=bool(payload.get("silent")),
        )

    @staticmethod
    def _control_succeeded(result: Any) -> bool:
        if isinstance(result, tuple) and result:
            return bool(result[0])
        return bool(result)

    @staticmethod
    def _decode_control_result(operation: CommandOperation, result_json: str) -> Any:
        result = json.loads(result_json)
        if operation in (CommandOperation.STEER, CommandOperation.RELEASE):
            if not isinstance(result, list) or len(result) != 2:
                raise AuthorityUnavailable("stored control result has an invalid shape")
            return bool(result[0]), str(result[1])
        return bool(result)
