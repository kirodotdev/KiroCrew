"""Command-authority boundary tests for coordinator-backed subagent mutations."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.run_coordinator import (
    CommandOperation,
    CommandStatus,
    CoordinatorDecision,
    CoordinatorReason,
    CoordinatorResult,
    MemoryRunCoordinator,
    ObservedState,
    OwnerLease,
    RunCompletion,
    RunOutcome,
    SubmitRun,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_command_authority import (
    AdmittedExecution,
    AuthorityConflict,
    AuthorityOutcomeUncertain,
    AuthorityUnavailable,
    CommandIdentity,
    SubagentCommandAuthority,
)

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.mark.asyncio
async def test_durable_batch_rejection_normalizes_before_completion_callback() -> None:
    coordinator = MemoryRunCoordinator()
    submitted = await coordinator.submit(
        SubmitRun(
            run_id="rejected-run",
            command_id="rejected-command",
            idempotency_key="rejected-key",
            payload_hash="rejected-hash",
            payload_json="{}",
            parent_session="dashboard:parent",
            agent="reviewer",
            task="reject before registration",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    delivered: list[SubagentInfo] = []

    async def on_done(info: SubagentInfo) -> None:
        assert info.user_stopped is False
        assert info.parent_session_key == "dashboard:parent"
        delivered.append(info)

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=on_done,
        coordinator=coordinator,
    )

    await manager.announce_durable_rejection(
        AdmittedExecution(
            id="rejected-run",
            task="reject before registration",
            done=True,
            error="platform policy unavailable",
            batch_id="batch-1",
            batch_total=2,
        )
    )

    assert [info.id for info in delivered] == ["rejected-run"]


class _FinishUnavailableCoordinator(MemoryRunCoordinator):
    async def finish_command(self, *args: Any, **kwargs: Any):
        raise OSError("coordinator write failed")


class _FirstFinishUnavailableCoordinator(MemoryRunCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.finish_attempts = 0

    async def finish_command(self, *args: Any, **kwargs: Any):
        self.finish_attempts += 1
        if self.finish_attempts == 1:
            raise OSError("coordinator write failed once")
        return await super().finish_command(*args, **kwargs)


class _PostCommitSubmitUnavailableCoordinator(MemoryRunCoordinator):
    async def submit(self, request: SubmitRun):
        await super().submit(request)
        raise OSError("coordinator response was lost")


class _PostCommitFinishUnavailableCoordinator(MemoryRunCoordinator):
    async def finish_command(self, *args: Any, **kwargs: Any):
        await super().finish_command(*args, **kwargs)
        raise OSError("coordinator response was lost")


@dataclass
class _Info:
    id: str
    done: bool = False
    error: str = ""
    queued: bool = False
    batch_id: str = ""
    batch_total: int = 0
    _coordinator_waiting: bool = False
    silent: bool = False


class _Manager:
    def __init__(self, *, register_spawn: bool = True) -> None:
        self.register_spawn = register_spawn
        self.spawn_calls: list[tuple[str, dict[str, Any]]] = []
        self.continue_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.steer_calls: list[tuple[str, str]] = []
        self.followup_calls: list[tuple[str, str]] = []
        self.cancel_calls: list[str] = []
        self.release_calls: list[str] = []
        self.delivered_events: list[str] = []
        self.delivered_batches: list[tuple[str, int]] = []
        self.prepared_batches: list[tuple[str, str, int]] = []
        self.infos: dict[str, _Info] = {}
        self._queue: list[dict[str, Any]] = []
        self.reserved_run_ids: set[str] = set()

    def reserve_coordinator_run_id(self, run_id: str) -> bool:
        if (
            run_id in self.reserved_run_ids
            or run_id in self.infos
            or any(str(entry.get("_preassigned_id") or "") == run_id for entry in self._queue)
        ):
            return False
        self.reserved_run_ids.add(run_id)
        return True

    def release_coordinator_run_id(self, run_id: str) -> None:
        self.reserved_run_ids.discard(run_id)

    def queue_legacy_run(self, run_id: str) -> bool:
        if run_id in self.reserved_run_ids:
            return False
        self._queue.append({"task": "concurrent legacy work", "_preassigned_id": run_id})
        return True

    def _unqueue(self, run_id: str) -> list[dict[str, Any]]:
        dropped = [
            entry for entry in self._queue if str(entry.get("_preassigned_id") or "") == run_id
        ]
        self._queue = [entry for entry in self._queue if entry not in dropped]
        return dropped

    def spawn(self, task: str, **kwargs: Any) -> _Info:
        self.spawn_calls.append((task, kwargs))
        info = _Info(
            kwargs["_preassigned_id"],
            queued=not self.register_spawn,
            _coordinator_waiting=not self.register_spawn,
        )
        if self.register_spawn:
            self.infos[info.id] = info
        return info

    def continue_conversation(self, conversation_id: str, task: str, **kwargs: Any) -> _Info:
        self.continue_calls.append((conversation_id, task, kwargs))
        info = _Info(kwargs["_preassigned_id"])
        self.infos[info.id] = info
        return info

    def get(self, run_id: str) -> _Info | None:
        return self.infos.get(run_id)

    async def steer_run(self, run_id: str, message: str) -> tuple[bool, str]:
        self.steer_calls.append((run_id, message))
        return True, "ok"

    async def follow_up_run(self, run_id: str, message: str) -> tuple[bool, str]:
        self.followup_calls.append((run_id, message))
        return True, "queued"

    async def cancel(self, run_id: str) -> bool:
        self.cancel_calls.append(run_id)
        return True

    def release_conversation(self, conversation_id: str) -> tuple[bool, str]:
        self.release_calls.append(conversation_id)
        return True, "released"

    async def release_conversation_async(self, conversation_id: str) -> tuple[bool, str]:
        return await asyncio.to_thread(self.release_conversation, conversation_id)

    async def announce_durable_rejection(self, info: _Info) -> None:
        self.announced.append(info)

    async def deliver_coordinator_event(
        self,
        event_id: str,
        *,
        batch_id: str = "",
        batch_total: int = 0,
    ) -> None:
        self.delivered_events.append(event_id)
        self.delivered_batches.append((batch_id, batch_total))

    def prepare_coordinator_rejection(
        self,
        run_id: str,
        *,
        batch_id: str = "",
        batch_total: int = 0,
    ) -> None:
        self.prepared_batches.append((run_id, batch_id, batch_total))


class _RegisteredThenRaisesManager(_Manager):
    def spawn(self, task: str, **kwargs: Any) -> _Info:
        super().spawn(task, **kwargs)
        raise RuntimeError("post-registration audit failed")


class _UnclaimableCoordinator(MemoryRunCoordinator):
    async def claim_command(self, command_id, owner):
        return None


class _FirstClaimUnavailableCoordinator(MemoryRunCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.claim_attempts = 0

    async def claim_command(self, command_id, owner):
        self.claim_attempts += 1
        if self.claim_attempts == 1:
            return None
        return await super().claim_command(command_id, owner)


class _RejectingManager(_Manager):
    def spawn(self, task: str, **kwargs: Any) -> _Info:
        self.spawn_calls.append((task, kwargs))
        return _Info(
            kwargs["_preassigned_id"],
            done=True,
            error="spawn refused by governance",
            batch_id=str(kwargs.get("batch_id") or ""),
            batch_total=int(kwargs.get("batch_total") or 0),
            silent=bool(kwargs.get("silent")),
        )

    def continue_conversation(self, conversation_id: str, task: str, **kwargs: Any) -> _Info:
        self.continue_calls.append((conversation_id, task, kwargs))
        return _Info(
            kwargs["_preassigned_id"],
            done=True,
            error="conversation_busy: existing run",
        )


class _SlowCancelManager(_Manager):
    def __init__(self, clock: list[float]) -> None:
        super().__init__()
        self._clock = clock

    async def cancel(self, run_id: str) -> bool:
        self._clock[0] += 31.0
        return await super().cancel(run_id)


class _RaisesBeforeRegistrationManager(_Manager):
    def spawn(self, task: str, **kwargs: Any) -> _Info:
        self.spawn_calls.append((task, kwargs))
        raise RuntimeError("pre-registration admission failed")


class _PlatformRejectingManager(_Manager):
    def spawn(self, task: str, **kwargs: Any) -> _Info:
        self.spawn_calls.append((task, kwargs))
        raise PlatformCompositionError("platform policy unavailable")


class _QueuedManager(_Manager):
    def spawn(self, task: str, **kwargs: Any) -> _Info:
        info = super().spawn(task, **kwargs)
        self._queue.append({"task": task, **kwargs})
        return info


class _RaisingControlManager(_Manager):
    async def steer_run(self, run_id: str, message: str) -> tuple[bool, str]:
        self.steer_calls.append((run_id, message))
        raise RuntimeError("provider rejected steering")


class _RejectFinishCoordinator(MemoryRunCoordinator):
    async def finish_command(self, *args: Any, **kwargs: Any):
        return CoordinatorResult(
            CoordinatorDecision.REJECTED,
            CoordinatorReason.VERSION_CONFLICT,
            None,
        )


class _RaisingManager(_Manager):
    def spawn(self, task: str, **kwargs: Any) -> _Info:
        self.spawn_calls.append((task, kwargs))
        raise RuntimeError("provider refused startup")


class _FailFirstCompletionCoordinator(MemoryRunCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.completion_calls = 0

    async def complete(self, completion, fence, expected_version):
        self.completion_calls += 1
        if self.completion_calls == 1:
            return CoordinatorResult(
                CoordinatorDecision.REJECTED,
                CoordinatorReason.VERSION_CONFLICT,
                None,
            )
        return await super().complete(completion, fence, expected_version)


class _RejectEveryCompletionCoordinator(MemoryRunCoordinator):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.completion_calls = 0

    async def complete(self, completion, fence, expected_version):
        self.completion_calls += 1
        return CoordinatorResult(
            CoordinatorDecision.REJECTED,
            CoordinatorReason.VERSION_CONFLICT,
            None,
        )


class _RaiseAfterCompletionCoordinator(MemoryRunCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.completion_calls = 0

    async def complete(self, completion, fence, expected_version):
        self.completion_calls += 1
        result = await super().complete(completion, fence, expected_version)
        if self.completion_calls == 1:
            raise OSError("coordinator response was lost")
        return result


class _FailFirstFinishCoordinator(MemoryRunCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.finish_calls = 0

    async def finish_command(self, fence, status, rejection_reason="", result_json=""):
        self.finish_calls += 1
        if self.finish_calls == 1:
            return CoordinatorResult(
                CoordinatorDecision.REJECTED,
                CoordinatorReason.VERSION_CONFLICT,
                None,
            )
        return await super().finish_command(fence, status, rejection_reason, result_json)


def _identity(suffix: str, *, key: str | None = None) -> CommandIdentity:
    return CommandIdentity(
        run_id=f"run-{suffix}",
        command_id=f"command-{suffix}",
        idempotency_key=key or f"key-{suffix}",
    )


async def _coordinator_with_target(
    run_id: str,
    *,
    clock: Any = None,
) -> MemoryRunCoordinator:
    coordinator = MemoryRunCoordinator(clock=clock) if clock is not None else MemoryRunCoordinator()
    result = await coordinator.submit(
        SubmitRun(
            run_id=run_id,
            command_id=f"seed:{run_id}",
            idempotency_key=f"seed:{run_id}",
            payload_hash="seed",
            payload_json="{}",
            parent_session="",
            agent="",
            task="seed",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert result.value is not None
    return coordinator


@pytest.mark.asyncio
async def test_keyed_spawn_replay_invokes_sync_manager_once() -> None:
    manager = _Manager()
    authority = SubagentCommandAuthority(MemoryRunCoordinator(), manager)
    identity = _identity("spawn")

    first = await authority.spawn(
        identity,
        "inspect the tree",
        parent_session_key="dashboard:one",
        agent="reviewer",
    )
    replay = await authority.spawn(
        identity,
        "inspect the tree",
        parent_session_key="dashboard:one",
        agent="reviewer",
    )

    assert replay is first
    assert len(manager.spawn_calls) == 1
    called_task, called_kwargs = manager.spawn_calls[0]
    assert called_task == "inspect the tree"
    assert called_kwargs["parent_session_key"] == "dashboard:one"
    assert called_kwargs["agent"] == "reviewer"
    assert called_kwargs["_preassigned_id"] == "run-spawn"
    assert called_kwargs["_coordinator_admitted"] is True
    assert called_kwargs["_coordinator_command"].command_id == "command-spawn"
    assert called_kwargs["_coordinator_fence"].run_id == "run-spawn"
    assert called_kwargs["_coordinator_version"] == 1


@pytest.mark.asyncio
async def test_keyed_spawn_payload_conflict_fails_before_second_execution() -> None:
    manager = _Manager()
    authority = SubagentCommandAuthority(MemoryRunCoordinator(), manager)
    identity = _identity("spawn-conflict")
    await authority.spawn(identity, "first payload")

    with pytest.raises(AuthorityConflict, match="idempotency_conflict"):
        await authority.spawn(identity, "different payload")

    assert len(manager.spawn_calls) == 1


@pytest.mark.asyncio
async def test_keyed_spawn_rejects_active_legacy_run_id_before_manager_call() -> None:
    manager = _Manager()
    existing = _Info("run-id-collision")
    manager.infos[existing.id] = existing
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("id-collision")

    result = await authority.spawn(identity, "must not overwrite")

    assert result.done is True
    assert result.counted is False
    assert "run_id_conflict" in result.error
    assert manager.spawn_calls == []
    assert manager.infos[existing.id] is existing
    assert await coordinator.get_run(identity.run_id) is None
    assert await coordinator.get_command_by_key(identity.idempotency_key) is None


@pytest.mark.asyncio
async def test_keyed_spawn_rejects_queued_legacy_run_id_before_manager_call() -> None:
    manager = _Manager()
    identity = _identity("queued-id-collision")
    queued = {"task": "legacy queued work", "_preassigned_id": identity.run_id}
    manager._queue.append(queued)
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)

    result = await authority.spawn(identity, "must not overwrite queued work")

    assert result.done is True
    assert result.counted is False
    assert "run_id_conflict" in result.error
    assert manager.spawn_calls == []
    assert manager._queue == [queued]
    assert await coordinator.get_run(identity.run_id) is None
    assert await coordinator.get_command_by_key(identity.idempotency_key) is None

    legacy = await coordinator.submit(
        SubmitRun(
            run_id=identity.run_id,
            command_id=f"spawn:{identity.run_id}",
            idempotency_key=f"spawn:{identity.run_id}",
            payload_hash="legacy-payload",
            parent_session="parent",
            agent="kirocrew",
            task="legacy queued work",
            conversation_key="",
            operation=CommandOperation.SPAWN,
            payload_json="{}",
        )
    )
    assert legacy.decision is CoordinatorDecision.APPLIED


@pytest.mark.asyncio
async def test_keyed_spawn_reserves_run_id_across_coordinator_submission() -> None:
    manager = _Manager()

    class RacingCoordinator(MemoryRunCoordinator):
        async def submit(self, request: SubmitRun):
            assert manager.queue_legacy_run(request.run_id) is False
            return await super().submit(request)

    authority = SubagentCommandAuthority(RacingCoordinator(), manager)
    identity = _identity("admission-race")

    result = await authority.spawn(identity, "keyed owner arrives first")

    assert result.done is False
    assert manager._queue == []
    assert len(manager.spawn_calls) == 1
    assert manager.reserved_run_ids == set()


@pytest.mark.asyncio
async def test_pending_replay_reserves_run_id_across_coordinator_submission() -> None:
    manager = _Manager()

    class ReplayRaceCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.submit_calls = 0

        async def submit(self, request: SubmitRun):
            result = await super().submit(request)
            self.submit_calls += 1
            if self.submit_calls == 1:
                raise OSError("coordinator response was lost")
            assert manager.queue_legacy_run(request.run_id) is False
            return result

    coordinator = ReplayRaceCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("pending-replay-race")

    with pytest.raises(AuthorityUnavailable, match="coordinator submission failed"):
        await authority.spawn(identity, "recover the pending command")

    replay = await authority.spawn(identity, "recover the pending command")

    assert replay.done is False
    assert manager._queue == []
    assert len(manager.spawn_calls) == 1
    assert manager.reserved_run_ids == set()


@pytest.mark.asyncio
async def test_post_commit_submission_failure_remains_lookup_worthy() -> None:
    manager = _Manager()
    coordinator = _PostCommitSubmitUnavailableCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("lost-submit-response")

    with pytest.raises(AuthorityUnavailable, match="coordinator submission failed"):
        await authority.spawn(identity, "persist before execution")

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.PENDING
    assert manager.spawn_calls == []


@pytest.mark.asyncio
async def test_keyed_queued_spawn_remains_claimed_until_manager_starts_it() -> None:
    manager = _Manager(register_spawn=False)
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("queued")

    first = await authority.spawn(identity, "wait for capacity")
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.CLAIMED

    replay_authority = SubagentCommandAuthority(coordinator, manager)
    with pytest.raises(AuthorityUnavailable, match="outcome is still pending"):
        await replay_authority.spawn(identity, "wait for capacity")

    await authority.execution_started(identity.run_id)
    replay = await replay_authority.spawn(identity, "wait for capacity")

    assert first.id == replay.id == identity.run_id
    assert len(manager.spawn_calls) == 1
    await authority.close()
    await replay_authority.close()


@pytest.mark.asyncio
async def test_execution_started_reconciles_a_lost_post_commit_response() -> None:
    manager = _Manager(register_spawn=False)
    coordinator = _PostCommitFinishUnavailableCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("lost-start-response")

    await authority.spawn(identity, "start exactly once")

    await authority.execution_started(identity.run_id)

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.APPLIED
    assert identity.run_id not in authority._waiting_executions
    assert identity.run_id not in authority._lease_tasks
    await authority.close()


@pytest.mark.asyncio
async def test_rejected_execution_exact_retry_replays_stored_result() -> None:
    manager = _RejectingManager()
    coordinator = MemoryRunCoordinator()
    identity = _identity("rejected-replay")

    first = await SubagentCommandAuthority(coordinator, manager).spawn(
        identity, "reject this spawn"
    )
    replay = await SubagentCommandAuthority(coordinator, manager).spawn(
        identity, "reject this spawn"
    )

    assert replay.id == first.id
    assert replay.task == "reject this spawn"
    assert replay.done is True
    assert replay.error == "spawn refused by governance"
    assert len(manager.spawn_calls) == 1


@pytest.mark.asyncio
async def test_keyed_batch_rejection_announces_after_durable_settlement() -> None:
    coordinator = MemoryRunCoordinator()
    identity = _identity("batch-rejection")
    observed_statuses: list[CommandStatus] = []

    class ObservingManager(_RejectingManager):
        async def deliver_coordinator_event(
            self,
            event_id: str,
            *,
            batch_id: str = "",
            batch_total: int = 0,
        ) -> None:
            receipt = await coordinator.get_command_by_key(identity.idempotency_key)
            assert receipt is not None
            observed_statuses.append(receipt.command.status)
            await super().deliver_coordinator_event(
                event_id,
                batch_id=batch_id,
                batch_total=batch_total,
            )

    manager = ObservingManager()
    authority = SubagentCommandAuthority(coordinator, manager)

    result = await authority.spawn(
        identity,
        "reject this batch spawn",
        batch_id="wave3",
        batch_total=2,
    )

    assert result.error == "spawn refused by governance"
    assert len(manager.delivered_events) == 1
    assert observed_statuses == [CommandStatus.APPLIED]


@pytest.mark.asyncio
async def test_keyed_batch_rejection_completion_failure_is_not_delivered() -> None:
    manager = _RejectingManager()
    authority = SubagentCommandAuthority(_FailFirstCompletionCoordinator(), manager)

    with pytest.raises(AuthorityOutcomeUncertain, match="not durably completed"):
        await authority.spawn(
            _identity("batch-rejection-unsettled"),
            "reject this batch spawn",
            batch_id="wave4",
            batch_total=2,
        )

    assert manager.delivered_events == []
    assert manager.prepared_batches == []


@pytest.mark.asyncio
async def test_rejected_execution_redacts_error_before_return_and_persistence() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"

    class CredentialRejectingManager(_RejectingManager):
        def spawn(self, task: str, **kwargs: Any) -> _Info:
            self.spawn_calls.append((task, kwargs))
            return _Info(
                kwargs["_preassigned_id"],
                done=True,
                error=f"unknown agent {secret}",
            )

    manager = CredentialRejectingManager()
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("redacted-rejection")

    result = await authority.spawn(identity, "reject this spawn")
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    lookup = await authority.lookup_response(identity.idempotency_key)

    assert secret not in result.error
    assert receipt is not None
    assert secret not in receipt.command.result_json
    assert lookup is not None
    assert secret not in str(lookup)


@pytest.mark.asyncio
async def test_keyed_queued_spawn_renews_lease_before_manager_registration() -> None:
    clock = [100.0]
    heartbeat_ticks: asyncio.Queue[None] = asyncio.Queue()

    async def controlled_sleep(_delay: float) -> None:
        await heartbeat_ticks.get()

    manager = _Manager(register_spawn=False)
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0])
    authority = SubagentCommandAuthority(
        coordinator,
        manager,
        clock=lambda: clock[0],
        sleep=controlled_sleep,
    )
    identity = _identity("queued-heartbeat")

    await authority.spawn(identity, "wait for capacity")
    clock[0] = 180.0
    heartbeat_ticks.put_nowait(None)
    for _ in range(10):
        await asyncio.sleep(0)
        run = await coordinator.get_run(identity.run_id)
        if run is not None and run.lease_expires_at > 180.0:
            break
    clock[0] = 200.0

    assert (
        await coordinator.claim_recovery(
            OwnerLease("recovery", clock[0] + 90.0),
            1,
        )
        == []
    )
    await authority.close()


@pytest.mark.asyncio
async def test_finished_registered_spawn_does_not_remain_in_replay_cache() -> None:
    authority = SubagentCommandAuthority(MemoryRunCoordinator(), _Manager())
    identity = _identity("cache-eviction")

    await authority.spawn(identity, "complete admission")

    assert identity.run_id not in authority._execution_results


@pytest.mark.asyncio
async def test_post_effect_finish_failure_is_reported_as_transport_uncertainty() -> None:
    manager = _Manager()
    authority = SubagentCommandAuthority(_FinishUnavailableCoordinator(), manager)
    identity = _identity("finish-unavailable")

    with pytest.raises(AuthorityOutcomeUncertain, match="not durably finished"):
        await authority.spawn(identity, "child has started")

    assert [task for task, _kwargs in manager.spawn_calls] == ["child has started"]


@pytest.mark.asyncio
async def test_manager_exception_result_fill_failure_keeps_durable_failure() -> None:
    manager = _RaisesBeforeRegistrationManager()
    coordinator = _FinishUnavailableCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("exception-settlement-unavailable")

    result = await authority.spawn(identity, "child may have started")
    run = await coordinator.get_run(identity.run_id)
    lookup = await authority.lookup_response(identity.idempotency_key)

    assert [task for task, _kwargs in manager.spawn_calls] == ["child may have started"]
    assert result.done is True
    assert result.error == "pre-registration admission failed"
    assert run is not None
    assert run.observed_state is ObservedState.TERMINAL
    assert run.outcome is RunOutcome.FAILED
    assert lookup is not None
    assert lookup["code"] == "run_failed"
    assert lookup["counted"] is True


@pytest.mark.asyncio
async def test_settled_manager_exception_returns_counted_rejection() -> None:
    manager = _RaisesBeforeRegistrationManager()
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("exception-settled")

    result = await authority.spawn(identity, "count this failed child")
    replay = await authority.spawn(identity, "count this failed child")
    lookup = await authority.lookup_response(identity.idempotency_key)

    assert result.done is True
    assert result.error == "pre-registration admission failed"
    assert result.counted is True
    assert replay == result
    assert [task for task, _kwargs in manager.spawn_calls] == ["count this failed child"]
    assert lookup == {
        "found": True,
        "id": identity.run_id,
        "error": "pre-registration admission failed",
        "code": "spawn_rejected",
        "counted": True,
    }


@pytest.mark.asyncio
async def test_platform_failure_before_registration_closes_batch_member() -> None:
    coordinator = MemoryRunCoordinator()
    manager = _PlatformRejectingManager()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("batch-platform-failure")

    result = await authority.spawn(
        identity,
        "start",
        batch_id="batchplatform",
        batch_total=2,
    )

    assert result.done is True
    assert result.error == "platform policy unavailable"
    assert result.batch_id == "batchplatform"
    assert result.batch_total == 2
    assert len(manager.delivered_events) == 1
    events = await coordinator.claim_outbox(
        OwnerLease("delivery", 10**12),
        1,
        event_id=manager.delivered_events[0],
    )
    assert len(events) == 1
    payload = json.loads(events[0].payload_json)
    assert payload["batch_id"] == "batchplatform"
    assert payload["batch_total"] == 2
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.APPLIED
    assert receipt.run is not None
    assert receipt.run.outcome is RunOutcome.FAILED


@pytest.mark.asyncio
async def test_manager_exception_after_registration_keeps_command_claimed() -> None:
    manager = _RegisteredThenRaisesManager()
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("registered-exception")

    with pytest.raises(AuthorityOutcomeUncertain, match="manager accepted execution"):
        await authority.spawn(identity, "registered child keeps running")

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert manager.get(identity.run_id) is not None
    assert receipt is not None
    assert receipt.command.status is CommandStatus.CLAIMED
    assert receipt.command.rejection_reason == ""


@pytest.mark.asyncio
async def test_control_exception_rejected_settlement_is_outcome_uncertain() -> None:
    manager = _RaisingControlManager()
    authority = SubagentCommandAuthority(_RejectFinishCoordinator(), manager)
    identity = _identity("control-exception-settlement-rejected")

    with pytest.raises(AuthorityOutcomeUncertain, match="failure was not durably finished"):
        await authority.steer(identity, "run-target", "change course")

    assert manager.steer_calls == [("run-target", "change course")]


@pytest.mark.asyncio
async def test_uncertain_control_exception_keeps_command_claimed() -> None:
    class _UncertainCancelManager(_Manager):
        async def cancel(self, run_id: str) -> bool:
            self.cancel_calls.append(run_id)
            raise AuthorityOutcomeUncertain("cancel settlement is uncertain")

    manager = _UncertainCancelManager()
    coordinator = await _coordinator_with_target("target-run")
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("uncertain-cancel")

    with pytest.raises(AuthorityOutcomeUncertain, match="cancel settlement"):
        await authority.cancel(identity, "target-run")

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.CLAIMED
    assert receipt.command.rejection_reason == ""
    assert receipt.command.result_json == ""

    restarted = SubagentCommandAuthority(coordinator, manager)
    with pytest.raises(AuthorityOutcomeUncertain, match="control outcome"):
        await restarted.cancel(identity, "target-run")
    assert manager.cancel_calls == ["target-run"]


@pytest.mark.asyncio
async def test_post_registration_manager_failure_preserves_live_execution() -> None:
    manager = _RegisteredThenRaisesManager()
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("registered-then-raised")

    with pytest.raises(AuthorityOutcomeUncertain, match="manager accepted"):
        await authority.spawn(identity, "child may already be running")

    run = await coordinator.get_run(identity.run_id)
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert run is not None
    assert run.observed_state is not ObservedState.TERMINAL
    assert run.outcome is None
    assert receipt is not None
    assert receipt.command.status is CommandStatus.CLAIMED
    assert manager.prepared_batches == []


@pytest.mark.asyncio
async def test_post_registration_exception_preserves_terminal_manager_rejection() -> None:
    class TerminalRegisteredThenRaisesManager(_Manager):
        def spawn(self, task: str, **kwargs: Any) -> _Info:
            info = super().spawn(task, **kwargs)
            info.done = True
            info.error = "spawn refused by governance"
            raise RuntimeError("post-registration audit failed")

    manager = TerminalRegisteredThenRaisesManager()
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("terminal-registered-then-raised")

    result = await authority.spawn(identity, "preserve the manager rejection")
    replay = await authority.spawn(identity, "preserve the manager rejection")
    lookup = await authority.lookup_response(identity.idempotency_key)

    assert result.error == "spawn refused by governance"
    assert replay.error == "spawn refused by governance"
    assert lookup is not None
    assert lookup["error"] == "spawn refused by governance"


@pytest.mark.asyncio
async def test_post_registration_rejection_uses_latest_run_version() -> None:
    class AdvancingCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.claim = None
            self.advance_on_lookup = False
            self.advanced = False

        async def claim_command(self, command_id, owner):
            claim = await super().claim_command(command_id, owner)
            self.claim = claim
            return claim

        async def get_run(self, run_id):
            if self.advance_on_lookup and not self.advanced:
                self.advanced = True
                assert self.claim is not None
                assert self.claim.run is not None
                assert self.claim.fence is not None
                started = await super().mark_starting(
                    self.claim.command,
                    self.claim.fence,
                    self.claim.run.version,
                )
                assert started.value is not None
            return await super().get_run(run_id)

    coordinator = AdvancingCoordinator()

    class TerminalRegisteredThenRaisesManager(_Manager):
        def spawn(self, task: str, **kwargs: Any) -> _Info:
            info = super().spawn(task, **kwargs)
            info.done = True
            info.error = "spawn refused after start"
            coordinator.advance_on_lookup = True
            raise RuntimeError("post-registration audit failed")

    authority = SubagentCommandAuthority(coordinator, TerminalRegisteredThenRaisesManager())
    identity = _identity("terminal-rejection-after-start")

    result = await authority.spawn(identity, "use the latest lifecycle version")

    run = await coordinator.get_run(identity.run_id)
    assert coordinator.advanced is True
    assert result.error == "spawn refused after start"
    assert run is not None
    assert run.observed_state is ObservedState.TERMINAL
    assert run.outcome is RunOutcome.FAILED


@pytest.mark.asyncio
async def test_post_registration_exception_never_becomes_clean_success() -> None:
    class CleanTerminalThenRaisesManager(_Manager):
        def spawn(self, task: str, **kwargs: Any) -> _Info:
            info = super().spawn(task, **kwargs)
            info.done = True
            raise RuntimeError("post-registration audit failed")

    manager = CleanTerminalThenRaisesManager()
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("clean-terminal-then-raised")

    with pytest.raises(AuthorityOutcomeUncertain, match="manager accepted"):
        await authority.spawn(identity, "do not report a clean result")

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.CLAIMED


@pytest.mark.asyncio
async def test_close_retries_terminal_manager_rejection_until_durable() -> None:
    class TerminalRegisteredThenRaisesManager(_Manager):
        def spawn(self, task: str, **kwargs: Any) -> _Info:
            info = super().spawn(task, **kwargs)
            info.done = True
            info.error = "spawn refused by governance"
            raise RuntimeError("post-registration audit failed")

    coordinator = _FailFirstCompletionCoordinator()
    authority = SubagentCommandAuthority(coordinator, TerminalRegisteredThenRaisesManager())
    identity = _identity("shutdown-terminal-rejection")

    with pytest.raises(AuthorityOutcomeUncertain, match="not durably completed"):
        await authority.spawn(identity, "preserve the manager rejection")

    await authority.close()

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert coordinator.completion_calls == 2
    assert receipt is not None
    assert receipt.command.status is CommandStatus.APPLIED
    assert receipt.run is not None
    assert receipt.run.observed_state is ObservedState.TERMINAL
    assert receipt.run.outcome is RunOutcome.FAILED
    assert authority._pending_execution_failures == {}


@pytest.mark.asyncio
async def test_close_releases_rejection_after_lost_completion_response() -> None:
    class TerminalRegisteredThenRaisesManager(_Manager):
        def spawn(self, task: str, **kwargs: Any) -> _Info:
            info = super().spawn(task, **kwargs)
            info.done = True
            info.error = "spawn refused by governance"
            raise RuntimeError("post-registration audit failed")

    coordinator = _RaiseAfterCompletionCoordinator()
    authority = SubagentCommandAuthority(coordinator, TerminalRegisteredThenRaisesManager())
    identity = _identity("shutdown-committed-rejection")

    with pytest.raises(AuthorityOutcomeUncertain, match="not durably completed"):
        await authority.spawn(identity, "preserve the committed rejection")

    await authority.close()

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert coordinator.completion_calls == 1
    assert receipt is not None
    assert receipt.run is not None
    assert receipt.run.observed_state is ObservedState.TERMINAL
    assert receipt.run.outcome is RunOutcome.FAILED
    assert authority._pending_execution_failures == {}


@pytest.mark.asyncio
async def test_close_releases_rejection_after_command_terminalizes() -> None:
    coordinator = _RejectEveryCompletionCoordinator()
    authority = SubagentCommandAuthority(coordinator, _RejectingManager())
    identity = _identity("shutdown-terminal-command")

    with pytest.raises(AuthorityOutcomeUncertain, match="not durably completed"):
        await authority.spawn(identity, "preserve the command outcome")
    pending = authority._pending_execution_failures[identity.command_id]
    await coordinator.finish_command(pending.claim.command_fence, CommandStatus.APPLIED)

    await authority.close()

    assert coordinator.completion_calls == 1
    assert authority._pending_execution_failures == {}


@pytest.mark.asyncio
async def test_close_releases_rejection_after_command_claim_takeover() -> None:
    clock = [100.0]
    coordinator = _RejectEveryCompletionCoordinator(clock=lambda: clock[0])
    authority = SubagentCommandAuthority(
        coordinator,
        _RejectingManager(),
        clock=lambda: clock[0],
    )
    identity = _identity("shutdown-newer-claim")

    with pytest.raises(AuthorityOutcomeUncertain, match="not durably completed"):
        await authority.spawn(identity, "defer to the new owner")
    clock[0] = 200.0
    replacement = await coordinator.claim_command(
        identity.command_id,
        OwnerLease("replacement", 290.0),
    )
    assert replacement is not None

    await authority.close()

    assert coordinator.completion_calls == 1
    assert authority._pending_execution_failures == {}


@pytest.mark.asyncio
async def test_close_bounds_pending_failure_settlement_during_coordinator_outage() -> None:
    async def yielding_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    coordinator = _RejectEveryCompletionCoordinator()
    authority = SubagentCommandAuthority(
        coordinator,
        _RejectingManager(),
        sleep=yielding_sleep,
    )
    identity = _identity("shutdown-pending-outage")

    with pytest.raises(AuthorityOutcomeUncertain, match="not durably completed"):
        await authority.spawn(identity, "bound shutdown retries")

    await asyncio.wait_for(authority.close(), timeout=0.2)

    assert coordinator.completion_calls == 1 + 3
    assert authority._pending_execution_failures == {}
    assert authority._lease_tasks == {}


@pytest.mark.asyncio
async def test_close_yields_while_superseded_failure_cache_changes() -> None:
    """Shutdown settlement cannot monopolize the loop on a replaced cache entry."""
    authority = SubagentCommandAuthority(MemoryRunCoordinator(), _Manager())
    authority._pending_execution_failures["command-1"] = object()  # type: ignore[assignment]
    marker_ran = False
    observations: list[bool] = []

    async def run_marker() -> None:
        nonlocal marker_ran
        await asyncio.sleep(0)
        marker_ran = True

    async def release_superseded(command_id: str, _pending: object) -> bool:
        observations.append(marker_ran)
        if len(observations) == 3:
            authority._pending_execution_failures.pop(command_id, None)
        return True

    authority._release_superseded_execution_failure = release_superseded  # type: ignore[method-assign]
    marker_task = asyncio.create_task(run_marker())

    await authority.close()
    await marker_task

    assert any(observations)


@pytest.mark.asyncio
async def test_manager_base_exception_is_never_converted_to_spawn_failure() -> None:
    class GatewayAbort(BaseException):
        pass

    class InterruptingManager(_Manager):
        def spawn(self, task: str, **kwargs: Any) -> _Info:
            self.spawn_calls.append((task, kwargs))
            raise GatewayAbort("operator interrupted gateway")

    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, InterruptingManager())
    identity = _identity("keyboard-interrupt")

    with pytest.raises(GatewayAbort, match="operator interrupted gateway"):
        await authority.spawn(identity, "do not swallow interrupts")

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.CLAIMED


@pytest.mark.asyncio
async def test_waiting_execution_rejection_finishes_durable_command() -> None:
    manager = _Manager(register_spawn=False)
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("waiting-rejected")

    result = await authority.spawn(identity, "wait for approval")
    assert result.queued is True

    await authority.reject_waiting_execution(identity.run_id, "spawn rejected")

    assert await authority.lookup_response(identity.idempotency_key) == {
        "found": True,
        "id": identity.run_id,
        "error": "spawn rejected",
        "code": "spawn_rejected",
        "counted": True,
    }
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.REJECTED
    assert identity.run_id not in authority._waiting_executions
    assert identity.run_id not in authority._execution_results


@pytest.mark.asyncio
async def test_waiting_execution_rejection_redacts_durable_reason() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, _Manager(register_spawn=False))
    identity = _identity("waiting-redacted")

    await authority.spawn(identity, "wait for approval")
    await authority.reject_waiting_execution(identity.run_id, f"unknown agent {secret}")

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    lookup = await authority.lookup_response(identity.idempotency_key)
    assert receipt is not None
    assert secret not in receipt.command.rejection_reason
    assert lookup is not None
    assert secret not in str(lookup)


@pytest.mark.asyncio
async def test_failed_waiting_rejection_retains_fence_and_heartbeat() -> None:
    manager = _Manager(register_spawn=False)
    authority = SubagentCommandAuthority(_FinishUnavailableCoordinator(), manager)
    identity = _identity("waiting-rejection-unavailable")

    await authority.spawn(identity, "wait for approval")

    with pytest.raises(AuthorityOutcomeUncertain, match="not durably finished"):
        await authority.reject_waiting_execution(identity.run_id, "spawn rejected")

    assert identity.run_id in authority._waiting_executions
    assert identity.run_id in authority._execution_results
    assert identity.run_id in authority._lease_tasks
    await authority.stop_execution_heartbeat(identity.run_id)


@pytest.mark.asyncio
async def test_waiting_rejection_can_retain_lease_until_terminal_commit() -> None:
    manager = _Manager(register_spawn=False)
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("waiting-rejected-with-lease")

    await authority.spawn(identity, "wait for approval")
    await authority.reject_waiting_execution(
        identity.run_id,
        "spawn rejected",
        stop_heartbeat=False,
    )

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.REJECTED
    assert identity.run_id in authority._waiting_executions
    assert identity.run_id in authority._lease_tasks
    await authority.stop_execution_heartbeat(identity.run_id)


@pytest.mark.asyncio
async def test_close_rejects_waiting_execution_before_dropping_lease() -> None:
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, _Manager(register_spawn=False))
    identity = _identity("waiting-shutdown")

    await authority.spawn(identity, "wait for capacity")
    await authority.close()

    assert await authority.lookup_response(identity.idempotency_key) == {
        "found": True,
        "id": identity.run_id,
        "error": "gateway shut down before execution",
        "code": "spawn_rejected",
        "counted": True,
    }
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.REJECTED


@pytest.mark.asyncio
async def test_close_bounds_waiting_settlement_when_coordinator_stays_unavailable() -> None:
    async def yielding_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    manager = _Manager(register_spawn=False)
    coordinator = _FinishUnavailableCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager, sleep=yielding_sleep)
    identity = _identity("shutdown-persistent-outage")
    await authority.spawn(identity, "wait durably")

    await asyncio.wait_for(authority.close(), timeout=0.2)

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.CLAIMED
    assert authority._waiting_executions == {}
    assert authority._pending_execution_failures == {}
    assert authority._lease_tasks == {}


@pytest.mark.asyncio
async def test_close_unqueues_waiting_execution_before_durable_rejection() -> None:
    manager = _QueuedManager(register_spawn=False)
    queue_present_at_finish: list[bool] = []

    class ObservingCoordinator(MemoryRunCoordinator):
        async def finish_command(self, *args: Any, **kwargs: Any):
            queue_present_at_finish.append(bool(manager._queue))
            return await super().finish_command(*args, **kwargs)

    coordinator = ObservingCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("waiting-shutdown-queued")

    await authority.spawn(identity, "wait for capacity")
    assert manager._queue

    await authority.close()

    assert queue_present_at_finish == [False]
    assert manager._queue == []
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.REJECTED


@pytest.mark.asyncio
async def test_close_retries_waiting_settlement_before_stopping_heartbeat() -> None:
    coordinator = _FirstFinishUnavailableCoordinator()
    authority = SubagentCommandAuthority(
        coordinator,
        _Manager(register_spawn=False),
    )
    identity = _identity("waiting-shutdown-unsettled")

    await authority.spawn(identity, "wait for capacity")
    await authority.close()

    assert coordinator.finish_attempts == 2
    assert identity.run_id not in authority._waiting_executions
    assert identity.run_id not in authority._execution_results
    assert identity.run_id not in authority._lease_tasks


@pytest.mark.asyncio
async def test_close_releases_waiting_state_after_command_takeover() -> None:
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0])
    authority = SubagentCommandAuthority(
        coordinator,
        _Manager(register_spawn=False),
        clock=lambda: clock[0],
    )
    identity = _identity("waiting-shutdown-taken-over")

    await authority.spawn(identity, "wait for capacity")
    clock[0] = 200.0
    replacement = await coordinator.claim_command(
        identity.command_id,
        OwnerLease("replacement", 290.0),
    )
    assert replacement is not None
    await coordinator.finish_command(
        replacement.command_fence,
        CommandStatus.APPLIED,
    )

    await asyncio.wait_for(authority.close(), timeout=0.1)

    assert identity.run_id not in authority._waiting_executions
    assert identity.run_id not in authority._execution_results
    assert identity.run_id not in authority._lease_tasks


@pytest.mark.asyncio
async def test_keyed_approval_wait_renews_lease_until_manager_takes_over() -> None:
    clock = [100.0]
    heartbeat_ticks: asyncio.Queue[None] = asyncio.Queue()

    async def controlled_sleep(_delay: float) -> None:
        await heartbeat_ticks.get()

    manager = _Manager()
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0])
    authority = SubagentCommandAuthority(
        coordinator,
        manager,
        clock=lambda: clock[0],
        sleep=controlled_sleep,
    )
    identity = _identity("approval-heartbeat")
    original_spawn = manager.spawn

    def awaiting_approval(task: str, **kwargs: Any) -> _Info:
        info = original_spawn(task, **kwargs)
        info._coordinator_waiting = True
        return info

    manager.spawn = awaiting_approval  # type: ignore[method-assign]

    await authority.spawn(identity, "wait for approval")
    clock[0] = 180.0
    heartbeat_ticks.put_nowait(None)
    for _ in range(10):
        await asyncio.sleep(0)
        run = await coordinator.get_run(identity.run_id)
        if run is not None and run.lease_expires_at > 180.0:
            break
    clock[0] = 200.0

    assert await coordinator.claim_recovery(OwnerLease("recovery", 290.0), 1) == []
    await authority.stop_execution_heartbeat(identity.run_id)


@pytest.mark.asyncio
async def test_lookup_response_returns_none_for_unknown_key() -> None:
    authority = SubagentCommandAuthority(MemoryRunCoordinator(), _Manager())

    assert await authority.lookup_response("unknown") is None


@pytest.mark.asyncio
async def test_lookup_response_reconstructs_spawn_and_continuation() -> None:
    manager = _Manager()
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    spawn_identity = _identity("lookup-spawn")
    continue_identity = _identity("lookup-continue")

    await authority.spawn(spawn_identity, "inspect", keep=True)
    await authority.continue_conversation(continue_identity, "conversation-one", "follow up")

    assert await authority.lookup_response(spawn_identity.idempotency_key) == {
        "found": True,
        "id": spawn_identity.run_id,
        "task": "inspect",
        "status": "spawned",
        "conversation": spawn_identity.run_id,
    }
    assert await authority.lookup_response(continue_identity.idempotency_key) == {
        "found": True,
        "id": continue_identity.run_id,
        "conversation": "conversation-one",
        "status": "spawned",
    }


@pytest.mark.asyncio
async def test_lookup_response_redacts_stored_spawn_task() -> None:
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, _Manager())
    identity = _identity("redacted-task")
    secret = "AKIAIOSFODNN7EXAMPLE"

    await authority.spawn(identity, f"inspect {secret}")

    response = await authority.lookup_response(identity.idempotency_key)
    assert response is not None
    assert secret not in str(response["task"])


@pytest.mark.asyncio
async def test_lookup_response_reports_pending_without_invoking_manager() -> None:
    manager = _Manager()
    coordinator = _UnclaimableCoordinator()
    authority = SubagentCommandAuthority(coordinator, manager)
    spawn_identity = _identity("pending-spawn")
    steer_identity = _identity("pending-steer")

    with pytest.raises(AuthorityUnavailable):
        await authority.spawn(spawn_identity, "wait durably")
    with pytest.raises(AuthorityUnavailable):
        await authority.steer(steer_identity, "legacy-target", "adjust")

    assert await authority.lookup_response(spawn_identity.idempotency_key) == {
        "found": True,
        "id": spawn_identity.run_id,
        "error": "command outcome is still pending",
        "status": "pending",
        "code": "command_pending",
        "command_status": "pending",
    }
    assert await authority.lookup_response(steer_identity.idempotency_key) == {
        "found": True,
        "id": "legacy-target",
        "error": "command outcome is still pending",
        "status": "pending",
        "code": "command_pending",
        "command_status": "pending",
    }
    assert manager.spawn_calls == []
    assert manager.steer_calls == []


@pytest.mark.asyncio
async def test_exact_replay_of_unstarted_spawn_remains_pending() -> None:
    manager = _Manager()
    coordinator = _UnclaimableCoordinator()
    identity = _identity("pending-replay")

    with pytest.raises(AuthorityUnavailable, match="still pending"):
        await SubagentCommandAuthority(coordinator, manager).spawn(
            identity,
            "wait durably",
        )
    with pytest.raises(AuthorityUnavailable, match="still pending"):
        await SubagentCommandAuthority(coordinator, manager).spawn(
            identity,
            "wait durably",
        )
    assert manager.spawn_calls == []


@pytest.mark.asyncio
async def test_exact_replay_reclaims_never_claimed_spawn() -> None:
    manager = _Manager()
    coordinator = _FirstClaimUnavailableCoordinator()
    identity = _identity("pending-reclaim")

    with pytest.raises(AuthorityUnavailable, match="still pending"):
        await SubagentCommandAuthority(coordinator, manager).spawn(identity, "wait durably")

    replay = await SubagentCommandAuthority(coordinator, manager).spawn(
        identity,
        "wait durably",
    )

    assert replay.id == identity.run_id
    assert [task for task, _kwargs in manager.spawn_calls] == ["wait durably"]


@pytest.mark.asyncio
async def test_lookup_response_reconstructs_rejected_spawn() -> None:
    coordinator = MemoryRunCoordinator()
    manager = _RejectingManager()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("rejected-spawn")

    await authority.spawn(identity, "denied")

    assert await authority.lookup_response(identity.idempotency_key) == {
        "found": True,
        "id": identity.run_id,
        "error": "spawn refused by governance",
        "code": "spawn_rejected",
        "counted": True,
    }
    run = await coordinator.get_run(identity.run_id)
    assert run is not None
    assert run.observed_state.value == "terminal"
    assert run.outcome is RunOutcome.FAILED
    events = await coordinator.claim_outbox(OwnerLease("delivery", 10**12), 1)
    assert events == []
    assert manager.delivered_events == []


@pytest.mark.asyncio
async def test_non_batch_rejection_is_already_delivered() -> None:
    class DeliveryFailingManager(_RejectingManager):
        async def deliver_coordinator_event(
            self,
            event_id: str,
            *,
            batch_id: str = "",
            batch_total: int = 0,
        ) -> None:
            raise OSError("parent delivery unavailable")

    coordinator = MemoryRunCoordinator()
    manager = DeliveryFailingManager()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("non-batch-delivery-failure")

    result = await authority.spawn(identity, "denied")
    replay = await authority.spawn(identity, "denied")
    events = await coordinator.claim_outbox(OwnerLease("redrive", 10**12), 1)

    assert result.error == "spawn refused by governance"
    assert replay.error == result.error
    assert len(manager.spawn_calls) == 1
    assert events == []


@pytest.mark.asyncio
async def test_lookup_response_reconstructs_rejected_continuation() -> None:
    coordinator = MemoryRunCoordinator()
    authority = SubagentCommandAuthority(coordinator, _RejectingManager())
    identity = _identity("rejected-continue")

    await authority.continue_conversation(identity, "conversation-one", "denied")

    assert await authority.lookup_response(identity.idempotency_key) == {
        "found": True,
        "id": identity.run_id,
        "error": "conversation_busy: existing run",
        "code": "conversation_busy",
        "counted": True,
    }


@pytest.mark.asyncio
async def test_batch_rejection_preserves_wave_metadata_and_routes_one_event() -> None:
    coordinator = MemoryRunCoordinator()
    manager = _RejectingManager()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("batch-rejection")
    agent_secret = "AKIAIOSFODNN7EXAMPLE"
    complete = coordinator.complete

    async def complete_after_batch_registration(*args, **kwargs):
        assert manager.prepared_batches == []
        return await complete(*args, **kwargs)

    coordinator.complete = complete_after_batch_registration  # type: ignore[method-assign]

    result = await authority.spawn(
        identity,
        "denied",
        batch_id="batchone",
        batch_total=3,
        silent=True,
        agent=agent_secret,
    )

    assert result.done is True
    assert result.error == "spawn refused by governance"
    assert len(manager.delivered_events) == 1
    assert manager.prepared_batches == [(identity.run_id, "batchone", 3)]
    event_id = manager.delivered_events[0]
    events = await coordinator.claim_outbox(
        OwnerLease("delivery", 10**12),
        1,
        event_id=event_id,
    )
    assert len(events) == 1
    assert '"batch_id":"batchone"' in events[0].payload_json
    assert '"batch_total":3' in events[0].payload_json
    assert '"silent":true' in events[0].payload_json
    assert agent_secret not in json.loads(events[0].payload_json)["agent"]


@pytest.mark.asyncio
async def test_continuation_rejection_preserves_conversation_and_model_identity() -> None:
    coordinator = MemoryRunCoordinator()
    manager = _RejectingManager()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("continuation-rejection-identity")

    await authority.continue_conversation(
        identity,
        "conversation-one",
        "denied",
        batch_id="batch-continuation",
        batch_total=2,
        model="review-model",
    )

    events = await coordinator.claim_outbox(
        OwnerLease("delivery", 10**12),
        1,
        event_id=manager.delivered_events[0],
    )
    assert len(events) == 1
    payload = json.loads(events[0].payload_json)
    assert payload["conversation_key"] == "subagent:conversation-one"
    assert payload["requested_model"] == "review-model"
    assert payload["resolved_model"] == ""


@pytest.mark.asyncio
async def test_manager_exception_returns_counted_batch_failure_without_rethrow() -> None:
    coordinator = MemoryRunCoordinator()
    manager = _RaisingManager()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("batch-exception")

    result = await authority.spawn(
        identity,
        "start",
        batch_id="batchtwo",
        batch_total=2,
        silent=True,
    )

    assert result.done is True
    assert result.error == "provider refused startup"
    assert result.batch_id == "batchtwo"
    assert result.batch_total == 2
    assert len(manager.spawn_calls) == 1
    assert len(manager.delivered_events) == 1
    events = await coordinator.claim_outbox(
        OwnerLease("delivery", 10**12),
        1,
        event_id=manager.delivered_events[0],
    )
    assert len(events) == 1
    assert json.loads(events[0].payload_json)["silent"] is True


@pytest.mark.asyncio
async def test_transient_completion_failure_resumes_without_reinvoking_manager() -> None:
    coordinator = _FailFirstCompletionCoordinator()
    manager = _RejectingManager()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("retry-rejection")

    with pytest.raises(AuthorityOutcomeUncertain, match="not durably completed"):
        await authority.spawn(identity, "denied")
    result = await authority.spawn(identity, "denied")

    assert result.done is True
    assert result.error == "spawn refused by governance"
    assert coordinator.completion_calls == 2
    assert len(manager.spawn_calls) == 1
    assert manager.delivered_events == []
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.APPLIED
    assert receipt.command.result_json
    assert receipt.run is not None
    assert receipt.run.outcome is RunOutcome.FAILED
    events = await coordinator.claim_outbox(OwnerLease("delivery", 10**12), 1)
    assert events == []


@pytest.mark.asyncio
async def test_post_commit_completion_failure_stays_counted_and_replays_without_manager() -> None:
    coordinator = _RaiseAfterCompletionCoordinator()
    manager = _RejectingManager()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("lost-completion-response")

    with pytest.raises(AuthorityOutcomeUncertain, match="not durably completed"):
        await authority.spawn(identity, "denied", batch_id="batchlost", batch_total=2)
    result = await authority.spawn(
        identity,
        "denied",
        batch_id="batchlost",
        batch_total=2,
    )

    assert result.done is True
    assert result.error == "spawn refused by governance"
    assert len(manager.spawn_calls) == 1
    assert coordinator.completion_calls == 1


@pytest.mark.asyncio
async def test_terminal_replay_preserves_batch_metadata_and_redacts_task() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    coordinator = _RaiseAfterCompletionCoordinator()
    manager = _RejectingManager()
    identity = _identity("terminal-replay-metadata")

    with pytest.raises(AuthorityOutcomeUncertain, match="not durably completed"):
        await SubagentCommandAuthority(coordinator, manager).spawn(
            identity,
            f"inspect credential {secret}",
            batch_id="batch-replay",
            batch_total=4,
            silent=True,
        )
    replay = await SubagentCommandAuthority(coordinator, _Manager()).spawn(
        identity,
        f"inspect credential {secret}",
        batch_id="batch-replay",
        batch_total=4,
        silent=True,
    )

    assert secret not in replay.task
    assert replay.batch_id == "batch-replay"
    assert replay.batch_total == 4
    assert replay.silent is True


@pytest.mark.asyncio
async def test_execution_admission_never_persists_raw_task_or_arguments() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    coordinator = MemoryRunCoordinator()
    manager = _Manager()
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("redacted-admission")

    await authority.spawn(
        identity,
        f"inspect credential {secret}",
        model=f"served-{secret}",
    )

    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.run is not None
    assert secret not in receipt.run.task
    assert secret not in receipt.command.payload_json
    assert secret in manager.spawn_calls[0][0]
    assert secret in manager.spawn_calls[0][1]["model"]


@pytest.mark.asyncio
async def test_restart_reconstructs_failure_after_completion_before_result_fill() -> None:
    coordinator = _FailFirstFinishCoordinator()
    first_manager = _RejectingManager()
    identity = _identity("restart-failure")

    first = await SubagentCommandAuthority(coordinator, first_manager).spawn(identity, "denied")
    assert first.done is True
    assert first.error == "spawn refused by governance"
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.status is CommandStatus.APPLIED
    assert receipt.command.result_json == ""
    assert receipt.run is not None
    assert receipt.run.outcome is RunOutcome.FAILED

    replay_manager = _Manager()
    replay = await SubagentCommandAuthority(coordinator, replay_manager).spawn(
        identity,
        "denied",
    )

    assert replay.done is True
    assert replay.error == "spawn refused by governance"
    assert replay_manager.spawn_calls == []


@pytest.mark.asyncio
async def test_batch_failure_remains_counted_when_result_fill_fails() -> None:
    coordinator = _FailFirstFinishCoordinator()
    manager = _RejectingManager()
    identity = _identity("batch-fill-failure")

    result = await SubagentCommandAuthority(coordinator, manager).spawn(
        identity,
        "denied",
        batch_id="batchthree",
        batch_total=2,
    )

    assert result.done is True
    assert result.error == "spawn refused by governance"
    assert len(manager.delivered_events) == 1
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert receipt.command.result_json == ""
    assert receipt.run is not None
    assert receipt.run.outcome is RunOutcome.FAILED


@pytest.mark.asyncio
async def test_exact_spawn_replay_claims_a_submission_left_pending() -> None:
    coordinator = _FirstClaimUnavailableCoordinator()
    identity = _identity("preclaim-recovery")
    with pytest.raises(AuthorityUnavailable, match="still pending"):
        await SubagentCommandAuthority(coordinator, _Manager()).spawn(
            identity,
            "wait durably",
        )
    replay_manager = _Manager()

    replay = await SubagentCommandAuthority(coordinator, replay_manager).spawn(
        identity,
        "wait durably",
    )

    assert replay.done is False
    assert replay.error == ""
    assert len(replay_manager.spawn_calls) == 1


@pytest.mark.asyncio
async def test_exact_spawn_replay_preserves_stored_acceptance_after_later_failure() -> None:
    coordinator = MemoryRunCoordinator()
    first_manager = _Manager()
    identity = _identity("accepted-then-failed")
    first = await SubagentCommandAuthority(coordinator, first_manager).spawn(
        identity,
        "start normally",
    )
    call_kwargs = first_manager.spawn_calls[0][1]
    completed = await coordinator.complete(
        RunCompletion(
            run_id=identity.run_id,
            outcome=RunOutcome.FAILED,
            result_path="",
            error="runtime failed later",
            event_type="subagent_completion",
            destination="",
            payload_json="{}",
            terminal_at=10**6,
        ),
        call_kwargs["_coordinator_fence"],
        call_kwargs["_coordinator_version"],
    )
    assert completed.decision is CoordinatorDecision.APPLIED
    replay_manager = _Manager()

    replay = await SubagentCommandAuthority(coordinator, replay_manager).spawn(
        identity,
        "start normally",
    )

    assert replay.done is False
    assert replay.error == ""
    assert replay.id == first.id
    assert replay_manager.spawn_calls == []


@pytest.mark.asyncio
async def test_keyed_continuation_replay_preserves_conversation_and_run_identity() -> None:
    manager = _Manager()
    authority = SubagentCommandAuthority(MemoryRunCoordinator(), manager)
    identity = _identity("continue")

    first = await authority.continue_conversation(
        identity,
        "conversation-one",
        "follow up",
        parent_session_key="dashboard:one",
    )
    replay = await authority.continue_conversation(
        identity,
        "conversation-one",
        "follow up",
        parent_session_key="dashboard:one",
    )

    assert replay is first
    assert len(manager.continue_calls) == 1
    conversation, called_task, called_kwargs = manager.continue_calls[0]
    assert conversation == "conversation-one"
    assert called_task == "follow up"
    assert called_kwargs["parent_session_key"] == "dashboard:one"
    assert called_kwargs["_preassigned_id"] == "run-continue"
    assert called_kwargs["_coordinator_admitted"] is True
    assert called_kwargs["_coordinator_command"].command_id == "command-continue"
    assert called_kwargs["_coordinator_fence"].run_id == "run-continue"
    assert called_kwargs["_coordinator_version"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "argument", "expected", "calls_attr"),
    [
        ("steer", "course correct", (True, "ok"), "steer_calls"),
        ("follow_up", "do this next", (True, "queued"), "followup_calls"),
        ("cancel", "", True, "cancel_calls"),
        ("release", "", (True, "released"), "release_calls"),
    ],
)
async def test_keyed_control_replay_invokes_manager_once(
    method: str, argument: str, expected: object, calls_attr: str
) -> None:
    manager = _Manager()
    target = "target-run"
    coordinator = await _coordinator_with_target(target)
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity(method)

    args = (identity, target, argument) if argument else (identity, target)
    first = await getattr(authority, method)(*args)
    replay = await getattr(authority, method)(*args)

    assert first == expected
    assert replay == expected
    assert len(getattr(manager, calls_attr)) == 1
    expected_lookup = {
        "steer": {"found": True, "id": target, "status": "steered"},
        "follow_up": {
            "found": True,
            "id": target,
            "status": "follow_up_queued",
        },
        "cancel": {"found": True, "ok": True, "cancelled": True},
        "release": {
            "found": True,
            "conversation": target,
            "status": "released",
        },
    }[method]
    assert await authority.lookup_response(identity.idempotency_key) == expected_lookup


@pytest.mark.asyncio
async def test_keyed_release_runs_blocking_manager_cleanup_off_the_event_loop() -> None:
    loop_thread = threading.get_ident()
    release_threads: list[int] = []

    class _ThreadRecordingManager(_Manager):
        def release_conversation(self, conversation_id: str) -> tuple[bool, str]:
            release_threads.append(threading.get_ident())
            return super().release_conversation(conversation_id)

    manager = _ThreadRecordingManager()
    authority = SubagentCommandAuthority(MemoryRunCoordinator(), manager)

    assert await authority.release(_identity("release-thread"), "conversation") == (
        True,
        "released",
    )
    assert release_threads and release_threads[0] != loop_thread


@pytest.mark.asyncio
async def test_control_result_is_redacted_before_return_and_persistence() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"

    class _SecretManager(_Manager):
        async def steer_run(self, run_id: str, message: str) -> tuple[bool, str]:
            self.steer_calls.append((run_id, message))
            return False, f"provider rejected credential {secret}"

    manager = _SecretManager()
    coordinator = await _coordinator_with_target("target-run")
    authority = SubagentCommandAuthority(coordinator, manager)
    identity = _identity("redacted-control")

    first = await authority.steer(identity, "target-run", "course correct")
    replay = await authority.steer(identity, "target-run", "course correct")

    assert secret not in first[1]
    assert replay == first
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)
    assert receipt is not None
    assert secret not in receipt.command.result_json


@pytest.mark.asyncio
async def test_slow_control_result_is_durable_without_replaying_the_side_effect() -> None:
    clock = [100.0]
    manager = _SlowCancelManager(clock)
    coordinator = await _coordinator_with_target("target-run", clock=lambda: clock[0])
    authority = SubagentCommandAuthority(
        coordinator,
        manager,
        clock=lambda: clock[0],
    )
    identity = _identity("slow-cancel")

    first = await authority.cancel(identity, "target-run")
    replay = await authority.cancel(identity, "target-run")

    assert first is True
    assert replay is True
    assert manager.cancel_calls == ["target-run"]


@pytest.mark.asyncio
async def test_control_finish_failure_is_uncertain_and_never_replays_side_effect() -> None:
    clock = [100.0]
    manager = _Manager()
    coordinator = _FinishUnavailableCoordinator(clock=lambda: clock[0])
    authority = SubagentCommandAuthority(coordinator, manager, clock=lambda: clock[0])
    identity = _identity("uncertain-steer")

    with pytest.raises(AuthorityOutcomeUncertain, match="control result"):
        await authority.steer(identity, "target-run", "course correct")
    clock[0] += 31.0
    restarted = SubagentCommandAuthority(coordinator, manager, clock=lambda: clock[0])
    with pytest.raises(AuthorityOutcomeUncertain, match="control outcome"):
        await restarted.steer(identity, "target-run", "course correct")

    assert manager.steer_calls == [("target-run", "course correct")]


@pytest.mark.asyncio
async def test_cancel_claim_covers_the_bounded_parent_delivery_wait() -> None:
    now = [100.0]
    manager = _Manager()

    async def slow_cancel(run_id: str) -> bool:
        manager.cancel_calls.append(run_id)
        now[0] += 60.0
        return True

    manager.cancel = slow_cancel  # type: ignore[method-assign]
    coordinator = await _coordinator_with_target("target-run", clock=lambda: now[0])
    authority = SubagentCommandAuthority(
        coordinator,
        manager,
        clock=lambda: now[0],
    )
    identity = _identity("slow-cancel")

    assert await authority.cancel(identity, "target-run") is True
    receipt = await coordinator.get_command_by_key(identity.idempotency_key)

    assert receipt is not None
    assert receipt.command.status is CommandStatus.APPLIED
    assert manager.cancel_calls == ["target-run"]


@pytest.mark.asyncio
async def test_lookup_reports_recovered_claimed_execution_as_interrupted() -> None:
    now = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: now[0])
    identity = _identity("recovered-claimed")
    submitted = await coordinator.submit(
        SubmitRun(
            run_id=identity.run_id,
            command_id=identity.command_id,
            idempotency_key=identity.idempotency_key,
            payload_hash="hash",
            payload_json=(
                '{"arguments":{},"operation":"spawn","run_id":"'
                + identity.run_id
                + '","task":"inspect"}'
            ),
            parent_session="dashboard:parent",
            agent="reviewer",
            task="inspect",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    claim = await coordinator.claim_command(
        identity.command_id,
        OwnerLease("dead-gateway", 110.0),
    )
    assert claim is not None
    now[0] = 111.0
    recovery_claims = await coordinator.claim_recovery(
        OwnerLease("recovery", 200.0),
        1,
    )
    assert len(recovery_claims) == 1
    recovery_claim = recovery_claims[0]
    completed = await coordinator.complete(
        RunCompletion(
            run_id=identity.run_id,
            outcome=RunOutcome.INTERRUPTED,
            result_path="",
            error="interrupted by gateway restart",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json="{}",
            terminal_at=now[0],
        ),
        recovery_claim.fence,
        recovery_claim.run.version,
    )
    assert completed.value is not None
    authority = SubagentCommandAuthority(coordinator, _Manager())

    assert await authority.lookup_response(identity.idempotency_key) == {
        "found": True,
        "id": identity.run_id,
        "error": "interrupted by gateway restart",
        "code": "run_interrupted",
        "counted": True,
    }
