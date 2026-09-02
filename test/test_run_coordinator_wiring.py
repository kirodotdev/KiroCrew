"""The typed coordinator seam is injectable before it becomes authoritative."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.run_coordinator import (
    CommandOperation,
    CoordinatorDecision,
    MemoryRunCoordinator,
    OwnerLease,
    RunFence,
    RunOutcome,
    SQLiteRunCoordinator,
    SubmitRun,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_command_authority import AuthorityOutcomeUncertain


def test_subagent_manager_defaults_to_durable_sqlite_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kiro_crew.subagent.SQLiteRunCoordinator",
        SQLiteRunCoordinator,
    )
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    assert isinstance(manager._coordinator, SQLiteRunCoordinator)


def test_subagent_manager_preserves_injected_coordinator_identity() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    assert manager._coordinator is coordinator
    assert manager.command_authority._coordinator is coordinator
    assert manager.command_authority._manager is manager


def test_subagent_manager_preserves_a_falsy_injected_coordinator() -> None:
    class FalsyCoordinator(MemoryRunCoordinator):
        def __bool__(self) -> bool:
            return False

    coordinator = FalsyCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )

    assert manager._coordinator is coordinator
    assert manager.command_authority._coordinator is coordinator


@pytest.mark.asyncio
async def test_execution_heartbeat_reacquires_an_expired_command_lease(monkeypatch) -> None:
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0])
    submitted = await coordinator.submit(
        SubmitRun(
            run_id="run-1",
            command_id="command-1",
            idempotency_key="key-1",
            payload_hash="hash-1",
            parent_session="dashboard:parent",
            agent="reviewer",
            task="inspect",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    claim = await coordinator.claim_command("command-1", OwnerLease("executor", 101.0))
    assert claim is not None and claim.fence is not None and claim.run is not None
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="run-1", task="inspect")
    info._coordinator_command = claim.command
    info._coordinator_fence = claim.fence
    info._coordinator_version = claim.run.version
    reacquired = asyncio.Event()
    original_claim = coordinator.claim_command

    async def observed_claim(command_id, owner):
        result = await original_claim(command_id, owner)
        if result is not None:
            reacquired.set()
        return result

    monkeypatch.setattr(coordinator, "claim_command", observed_claim)
    monkeypatch.setattr("kiro_crew.subagent.EXECUTION_LEASE_SECONDS", 0.03)
    clock[0] = 102.0

    manager._start_coordinator_heartbeat(info)
    try:
        await asyncio.wait_for(reacquired.wait(), timeout=1)
    finally:
        await manager._stop_coordinator_heartbeat("run-1")

    assert info._coordinator_fence is not None
    assert info._coordinator_fence.lease_epoch == claim.fence.lease_epoch + 1


@pytest.mark.asyncio
async def test_accepted_run_is_submitted_after_legacy_admission() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(
        id="run-1",
        task="redacted task",
        parent_session_key="parent-1",
        agent="researcher",
        model="served-model",
        reasoning_effort="high",
        allowed_tools=["Read"],
        bare=True,
        cwd="/tmp/project",
        approval_mode="auto",
        silent=True,
        max_turns=7,
        keep=True,
        include_memory=False,
        include_lessons=True,
        include_project=False,
        requested_model="requested-model",
        resolved_model="resolved-model",
    )
    info._raw_task = "raw task \ud800 AKIAIOSFODNN7EXAMPLE"

    await manager._shadow_submit_accepted_run(info)

    run = await coordinator.get_run("run-1")
    assert run is not None
    assert "AKIAIOSFODNN7EXAMPLE" not in run.task
    assert "\ud800" not in run.task
    assert info._coordinator_fence is not None
    command = info._coordinator_command
    assert command is not None
    assert command.operation is CommandOperation.SPAWN
    payload = json.loads(command.payload_json)
    assert payload == {
        "agent": "researcher",
        "allowed_tools": ["Read"],
        "app": "",
        "approval_mode": "auto",
        "bare": True,
        "batch_id": "",
        "batch_total": 0,
        "conversation_key": "",
        "cwd": "/tmp/project",
        "include_lessons": True,
        "include_memory": False,
        "include_project": False,
        "keep": True,
        "max_turns": 7,
        "model": "served-model",
        "operation": "spawn",
        "parent_session": "parent-1",
        "reasoning_effort": "high",
        "requested_model": "requested-model",
        "resolved_model": "resolved-model",
        "run_id": "run-1",
        "silent": True,
        "task": run.task,
    }


@pytest.mark.asyncio
async def test_default_shadow_persists_accepted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kiro_crew.subagent.SQLiteRunCoordinator",
        SQLiteRunCoordinator,
    )
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

    await manager._shadow_submit_accepted_run(SubagentInfo(id="durable-run", task="task"))

    stored = await SQLiteRunCoordinator(tmp_path / "run-coordinator/coordinator.db").get_run(
        "durable-run"
    )
    assert stored is not None
    assert stored.task == "task"


@pytest.mark.asyncio
async def test_manager_persists_process_identity_through_its_execution_fence() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    await coordinator.submit(
        SubmitRun(
            run_id="process-run",
            command_id="process-command",
            idempotency_key="process-key",
            payload_hash="process-hash",
            parent_session="parent-1",
            agent="researcher",
            task="task",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    claim = await coordinator.claim_command(
        "process-command",
        OwnerLease("executor", 9_999_999_999.0),
    )
    assert claim is not None
    info = SubagentInfo(id="process-run", task="task")
    info._coordinator_command = claim.command
    info._coordinator_fence = claim.fence
    info._coordinator_version = claim.run.version

    await manager._coordinator_mark_starting(info)
    await manager._coordinator_mark_running(info)
    await manager._coordinator_record_process(info, 4321, "start-1", True)

    stored = await coordinator.get_run("process-run")
    assert stored is not None
    assert stored.process_id == 4321
    assert stored.process_start_id == "start-1"
    assert stored.process_owned is True
    assert info._coordinator_version == stored.version


@pytest.mark.asyncio
async def test_continuation_submission_is_stable_and_idempotent() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(
        id="run-2",
        task="follow up",
        parent_session_key="parent-1",
        conversation_key="subagent:original",
    )
    info._raw_task = "follow up"

    await manager._shadow_submit_accepted_run(info)
    await manager._shadow_submit_accepted_run(info)

    receipt = await coordinator.get_command_by_key("continue:run-2")
    assert receipt is not None
    assert receipt.command.operation is CommandOperation.CONTINUE
    assert receipt.command.command_id == "continue:run-2"
    assert info._coordinator_fence is not None


@pytest.mark.asyncio
async def test_shadow_submission_failure_preserves_legacy_execution() -> None:
    coordinator = AsyncMock()
    coordinator.submit.side_effect = RuntimeError("database unavailable")
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="run-3", task="task")

    await manager._shadow_submit_accepted_run(info)

    coordinator.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_shadow_request_construction_failure_is_contained() -> None:
    coordinator = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="run-invalid", task="task")
    info.allowed_tools = [{"not-json-serializable"}]  # type: ignore[list-item]

    await manager._shadow_submit_accepted_run(info)

    coordinator.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_starting_transition_retries_a_lost_commit_response() -> None:
    coordinator = AsyncMock()
    committed = MagicMock()
    committed.decision = CoordinatorDecision.UNCHANGED
    committed.value.version = 4
    coordinator.mark_starting.side_effect = [OSError("response lost"), committed]
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="starting-response-lost", task="task")
    info._coordinator_command = MagicMock()
    info._coordinator_fence = MagicMock()
    info._coordinator_version = 3

    await manager._coordinator_mark_starting(info)

    assert coordinator.mark_starting.await_count == 2
    assert info._coordinator_version == 4
    assert info._coordinator_started is True


@pytest.mark.asyncio
async def test_running_transition_retries_a_lost_commit_response() -> None:
    coordinator = AsyncMock()
    committed = MagicMock()
    committed.decision = CoordinatorDecision.UNCHANGED
    committed.value.version = 5
    coordinator.mark_running.side_effect = [OSError("response lost"), committed]
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="running-response-lost", task="task")
    info._coordinator_fence = MagicMock()
    info._coordinator_version = 4

    await manager._coordinator_mark_running(info)

    assert coordinator.mark_running.await_count == 2
    assert info._coordinator_version == 5
    assert info._coordinator_running is True


@pytest.mark.asyncio
async def test_unfenced_shadow_submission_aborts_before_execution() -> None:
    order: list[str] = []
    coordinator = AsyncMock()

    async def submit(_request: object) -> object:
        order.append("submit")
        return None

    coordinator.submit.side_effect = submit
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )

    async def run_inner(_info: SubagentInfo, _session_key: str) -> None:
        order.append("execute")

    manager._run_inner = AsyncMock(side_effect=run_inner)
    manager._teardown_run_session = AsyncMock()
    manager._record_cost = MagicMock()
    manager._spawn_terminal_report = MagicMock()

    info = SubagentInfo(id="run-4", task="task")
    with patch(
        "kiro_crew.subagent.clear_tombstone_for_recovery",
        return_value=True,
    ):
        await manager._run(info)

    assert order == ["submit"]
    assert info.done is True
    assert info.error == "run coordinator submission failed after timeout"
    assert info._legacy_delivery_tombstone is True
    manager._run_inner.assert_not_awaited()
    manager._spawn_terminal_report.assert_called_once()


@pytest.mark.asyncio
async def test_slow_shadow_submit_waits_without_cancelling_accepted_run() -> None:
    class DelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_submit = asyncio.Event()
            self.submit_started = asyncio.Event()
            self.submit_cancelled = False

        async def submit(self, request):
            self.submit_started.set()
            try:
                await self.release_submit.wait()
            except asyncio.CancelledError:
                self.submit_cancelled = True
                raise
            return await super().submit(request)

    coordinator = DelayedSubmitCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    info = SubagentInfo(id="late-submit", task="task")
    manager._agents[info.id] = info

    run_task = asyncio.create_task(manager._run(info))
    await coordinator.submit_started.wait()
    await asyncio.sleep(0)

    assert run_task.done() is False
    assert coordinator.submit_cancelled is False
    manager._run_inner.assert_not_awaited()
    manager._fire_event.assert_not_awaited()
    on_done.assert_not_awaited()

    coordinator.release_submit.set()
    await run_task

    assert info._coordinator_claim_uncertain is False
    assert info._coordinator_fence is not None
    manager._run_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_shadow_submit_failure_reports_one_terminal_failure() -> None:
    class FailingDelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_submit = asyncio.Event()
            self.submit_started = asyncio.Event()

        async def submit(self, request):
            self.submit_started.set()
            await self.release_submit.wait()
            raise RuntimeError("sqlite write failed")

    coordinator = FailingDelayedSubmitCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    info = SubagentInfo(id="failed-late-submit", task="task")
    manager._agents[info.id] = info

    run_task = asyncio.create_task(manager._run(info))
    await coordinator.submit_started.wait()
    await asyncio.sleep(0)

    assert run_task.done() is False
    on_done.assert_not_awaited()

    with patch(
        "kiro_crew.subagent.clear_tombstone_for_recovery",
        return_value=True,
    ):
        coordinator.release_submit.set()
        await run_task
        if manager._report_tasks:
            await asyncio.gather(*manager._report_tasks)

    assert info._coordinator_claim_uncertain is False
    assert info._finalized is True
    assert info.error == "run coordinator submission failed after timeout"
    assert info._legacy_delivery_tombstone is True
    manager._fire_event.assert_awaited_once()
    on_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_admission_awaits_coordinator_submit_and_claim() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="durable-admission-fence", task="task")

    await manager._shadow_submit_accepted_run(info)

    assert info._coordinator_claim_uncertain is False
    assert info._coordinator_fence is not None
    assert info._coordinator_command is not None


@pytest.mark.asyncio
async def test_late_shadow_submit_failure_reopens_recovery_off_loop() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    info = SubagentInfo(id="late-shadow-failure", task="task")
    info._coordinator_claim_uncertain = True
    manager._claim_finalize = MagicMock(return_value=True)
    manager._record_cost = MagicMock()
    manager._spawn_terminal_report = MagicMock()
    clear_threads: list[int] = []

    with patch(
        "kiro_crew.subagent.clear_tombstone_for_recovery",
        side_effect=lambda _agent_id: (
            clear_threads.append(threading.get_ident()),
            True,
        )[1],
    ):
        settlement = manager._resume_legacy_terminal_after_failed_shadow_submit(info)
        if inspect.isawaitable(settlement):
            await settlement

    assert clear_threads
    assert clear_threads != [threading.get_ident()]
    assert info._legacy_delivery_tombstone is True
    manager._spawn_terminal_report.assert_called_once()
    report_call = manager._spawn_terminal_report.call_args
    assert report_call.args == (info,)
    assert report_call.kwargs["tombstone_error_on_success"] is True


@pytest.mark.asyncio
async def test_late_shadow_submit_failure_retries_before_consuming_terminal_claim() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    info = SubagentInfo(id="blocked-shadow-failure", task="task")
    info._coordinator_claim_uncertain = True
    events: list[str] = []
    clear_attempts = 0

    def clear_for_recovery(_agent_id: str) -> bool:
        nonlocal clear_attempts
        clear_attempts += 1
        events.append("clear")
        return clear_attempts > 1

    async def retry_sleep(_delay: float) -> None:
        events.append("retry")
        assert info._coordinator_claim_uncertain is True
        manager._claim_finalize.assert_not_called()

    manager._claim_finalize = MagicMock(
        side_effect=lambda _info, **_kwargs: (events.append("finalize"), True)[1]
    )
    manager._record_cost = MagicMock()
    manager._spawn_terminal_report = MagicMock()

    with (
        patch(
            "kiro_crew.subagent.clear_tombstone_for_recovery",
            side_effect=clear_for_recovery,
        ),
        patch("kiro_crew.subagent.asyncio.sleep", side_effect=retry_sleep) as sleep,
    ):
        await manager._resume_legacy_terminal_after_failed_shadow_submit(info)

    assert events == ["clear", "retry", "clear", "finalize"]
    sleep.assert_awaited_once()
    assert info._coordinator_claim_uncertain is False
    assert info._legacy_delivery_tombstone is True
    manager._claim_finalize.assert_called_once_with(info, supersede_recovery=True)
    manager._record_cost.assert_called_once_with(info)
    manager._spawn_terminal_report.assert_called_once()


@pytest.mark.asyncio
async def test_late_shadow_retry_stops_before_clearing_new_terminal_marker() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    info = SubagentInfo(id="stopped-shadow-retry", task="task")
    info._coordinator_claim_uncertain = True
    clear_attempts = 0

    def blocked_clear(_agent_id: str) -> bool:
        nonlocal clear_attempts
        clear_attempts += 1
        assert clear_attempts == 1, "a newer terminal owner must stop tombstone retries"
        return False

    async def user_stop_wins(_delay: float) -> None:
        info.done = True
        info.user_stopped = True
        info._finalized = True

    manager._spawn_terminal_report = MagicMock()
    with (
        patch(
            "kiro_crew.subagent.clear_tombstone_for_recovery",
            side_effect=blocked_clear,
        ),
        patch("kiro_crew.subagent.asyncio.sleep", side_effect=user_stop_wins),
    ):
        await manager._resume_legacy_terminal_after_failed_shadow_submit(info)

    assert clear_attempts == 1
    assert info.error == ""
    manager._spawn_terminal_report.assert_not_called()


@pytest.mark.asyncio
async def test_late_shadow_clear_restores_terminal_marker_when_stop_wins_in_worker() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    info = SubagentInfo(id="stopped-during-shadow-clear", task="task")
    info._coordinator_claim_uncertain = True
    clear_started = threading.Event()
    clear_release = threading.Event()

    def delayed_clear(_agent_id: str) -> bool:
        clear_started.set()
        assert clear_release.wait(timeout=2)
        return True

    manager._write_tombstone = MagicMock()
    manager._spawn_terminal_report = MagicMock()
    with patch(
        "kiro_crew.subagent.clear_tombstone_for_recovery",
        side_effect=delayed_clear,
    ):
        settlement = asyncio.create_task(
            manager._resume_legacy_terminal_after_failed_shadow_submit(info)
        )
        assert await asyncio.to_thread(clear_started.wait, 2)
        info.done = True
        info.user_stopped = True
        info._finalized = True
        clear_release.set()
        await settlement

    assert info.error == ""
    manager._write_tombstone.assert_called_once_with(info, "cancelled")
    manager._spawn_terminal_report.assert_not_called()


@pytest.mark.asyncio
async def test_late_shadow_submit_failure_supersedes_pending_cancel_recovery() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    info = SubagentInfo(id="recovering-shadow-failure", task="task")
    info._coordinator_claim_uncertain = True
    info._recovering = True
    manager._record_cost = MagicMock()
    manager._spawn_terminal_report = MagicMock()

    with patch(
        "kiro_crew.subagent.clear_tombstone_for_recovery",
        return_value=True,
    ):
        await manager._resume_legacy_terminal_after_failed_shadow_submit(info)

    assert info._finalized is True
    assert info._recovering is False
    assert info.done is True
    assert info.error
    manager._spawn_terminal_report.assert_called_once()


@pytest.mark.asyncio
async def test_stale_shadow_settlement_defers_to_launched_cancel_recovery() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    info = SubagentInfo(id="respawned-shadow-failure", task="task")
    info._coordinator_claim_uncertain = True
    info._coordinator_shadow_generation = 2
    info._cancel_retry_used = True
    manager._record_cost = MagicMock()
    manager._spawn_terminal_report = MagicMock()

    origin_task = asyncio.create_task(asyncio.sleep(0))
    await origin_task
    replacement_release = asyncio.Event()
    replacement_task = asyncio.create_task(replacement_release.wait())
    manager._tasks[info.id] = replacement_task

    try:
        with patch(
            "kiro_crew.subagent.clear_tombstone_for_recovery",
            return_value=True,
        ):
            await manager._resume_legacy_terminal_after_failed_shadow_submit(
                info,
                generation=1,
                origin_task=origin_task,
            )

        assert info._coordinator_claim_uncertain is True
        assert info._finalized is False
        assert info.done is False
        manager._spawn_terminal_report.assert_not_called()
    finally:
        replacement_release.set()
        await replacement_task


@pytest.mark.asyncio
async def test_cancel_recovery_shadow_attempt_clears_stale_uncertainty_before_execution() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    info = SubagentInfo(id="respawned-shadow-execution", task="task")
    info._coordinator_claim_uncertain = True
    info._coordinator_shadow_generation = 1
    info._coordinator_fence = MagicMock()
    info._cancel_retry_used = True
    info._slot_released = True
    manager._coordinator_mark_starting = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._record_cost = MagicMock()
    manager._spawn_terminal_report = MagicMock(return_value=None)

    await manager._run(info)

    assert info._coordinator_shadow_generation == 2
    assert info._coordinator_claim_uncertain is False
    manager._run_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_shadow_settlement_defers_when_durable_fence_appears() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    info = SubagentInfo(id="durable-shadow-settlement", task="task")
    info._coordinator_claim_uncertain = True
    manager._record_cost = MagicMock()
    manager._spawn_terminal_report = MagicMock()

    def clear_after_recovery_wins(_agent_id: str) -> bool:
        info._coordinator_fence = MagicMock()
        return True

    with patch(
        "kiro_crew.subagent.clear_tombstone_for_recovery",
        side_effect=clear_after_recovery_wins,
    ):
        await manager._resume_legacy_terminal_after_failed_shadow_submit(info)

    assert info._coordinator_claim_uncertain is True
    assert info._finalized is False
    assert info.done is False
    manager._spawn_terminal_report.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_fallback_tombstones_only_after_parent_report() -> None:
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=on_done,
    )
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    info = SubagentInfo(id="reported-shadow-fallback", task="task")
    info.done = True
    info.error = "coordinator submission failed"

    await manager._report_terminal(
        info,
        source="Subagent",
        injection_timeout_reason="timed out",
        mark_delivered_on_success=False,
        tombstone_error_on_success=True,
    )

    on_done.assert_awaited_once_with(info)
    assert info._reported_to_parent is True
    manager._write_tombstone.assert_called_once_with(info, "error")


@pytest.mark.asyncio
async def test_shadow_fallback_defers_tombstone_when_parent_queues() -> None:
    async def queue_completion(info: SubagentInfo) -> None:
        info._delivery_queued = True

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=queue_completion,
    )
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    info = SubagentInfo(id="queued-shadow-fallback", task="task")
    info.done = True
    info.error = "coordinator submission failed"

    await manager._report_terminal(
        info,
        source="Subagent",
        injection_timeout_reason="timed out",
        mark_delivered_on_success=False,
        tombstone_error_on_success=True,
    )

    assert info._reported_to_parent is True
    manager._write_tombstone.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_fallback_route_failure_stays_recoverable() -> None:
    async def fail_delivery(info: SubagentInfo) -> None:
        info._delivery_failed = True

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=fail_delivery,
    )
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    manager._settle_digest_holds = AsyncMock()
    info = SubagentInfo(id="failed-shadow-fallback", task="task")
    info.done = True
    info.error = "coordinator submission failed"

    await manager._report_terminal(
        info,
        source="Subagent",
        injection_timeout_reason="timed out",
        mark_delivered_on_success=False,
        settle_digest=True,
        tombstone_error_on_success=True,
    )

    assert info._reported_to_parent is False
    manager._write_tombstone.assert_not_called()
    manager._settle_digest_holds.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_shadow_claim_defers_terminal_delivery_to_recovery() -> None:
    class DelayedClaimCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            self.now = time.time()
            super().__init__(clock=lambda: self.now)
            self.release_claim = asyncio.Event()
            self.lookup_started = asyncio.Event()
            self.claim_task: asyncio.Task | None = None

        async def claim_command(self, command_id, owner):
            async def commit_claim():
                await self.release_claim.wait()
                return await super(DelayedClaimCoordinator, self).claim_command(
                    command_id,
                    owner,
                )

            self.claim_task = asyncio.create_task(commit_claim())
            raise asyncio.TimeoutError

        async def get_command_by_key(self, idempotency_key):
            self.lookup_started.set()
            return await super().get_command_by_key(idempotency_key)

    coordinator = DelayedClaimCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    info = SubagentInfo(
        id="late-claim",
        task="task",
        batch_id="batch-late",
        batch_total=2,
    )
    manager._agents[info.id] = info

    run_task = asyncio.create_task(manager._run(info))
    await asyncio.wait_for(coordinator.lookup_started.wait(), timeout=1)
    await run_task
    coordinator.release_claim.set()
    assert coordinator.claim_task is not None
    await coordinator.claim_task

    receipt = await coordinator.get_command_by_key("spawn:late-claim")
    assert receipt is not None
    assert receipt.command.status.value == "claimed"
    assert info._coordinator_claim_uncertain is True
    assert info._finalized is False
    assert manager.batch_members_pending("batch-late") is True
    manager._run_inner.assert_not_awaited()
    manager._fire_event.assert_not_awaited()
    on_done.assert_not_awaited()

    coordinator.now += 1_000
    claims = await coordinator.claim_recovery(
        OwnerLease("recovery", coordinator.now + 100),
        limit=1,
    )
    assert len(claims) == 1
    completion = manager._run_recovery.completion_for(claims[0].run)
    completed = await coordinator.complete(
        completion,
        claims[0].fence,
        claims[0].run.version,
    )
    assert completed.value is not None
    await manager._outbox_delivery.drain_once()

    manager._fire_event.assert_awaited_once()
    on_done.assert_awaited_once()
    delivered = on_done.await_args.args[0]
    assert delivered.batch_id == "batch-late"
    assert delivered.batch_total == 2


@pytest.mark.asyncio
async def test_failed_shadow_claim_defers_terminal_delivery_to_recovery() -> None:
    class FailedClaimCoordinator(MemoryRunCoordinator):
        async def claim_command(self, command_id, owner):
            raise OSError("claim unavailable")

    coordinator = FailedClaimCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    info = SubagentInfo(id="failed-claim", task="task")

    await manager._run(info)

    receipt = await coordinator.get_command_by_key("spawn:failed-claim")
    assert receipt is not None
    assert receipt.command.status.value == "pending"
    assert info._coordinator_claim_uncertain is True
    assert info._finalized is False
    manager._run_inner.assert_not_awaited()
    manager._fire_event.assert_not_awaited()
    on_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_shadow_claim_resumes_from_stable_committed_fence() -> None:
    class ClaimBeforeLookupCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_claim = asyncio.Event()
            self.claim_task: asyncio.Task | None = None

        async def claim_command(self, command_id, owner):
            async def commit_claim():
                await self.release_claim.wait()
                return await super(ClaimBeforeLookupCoordinator, self).claim_command(
                    command_id,
                    owner,
                )

            self.claim_task = asyncio.create_task(commit_claim())
            raise asyncio.TimeoutError

        async def get_command_by_key(self, idempotency_key):
            self.release_claim.set()
            assert self.claim_task is not None
            await self.claim_task
            return await super().get_command_by_key(idempotency_key)

    coordinator = ClaimBeforeLookupCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager._start_coordinator_heartbeat = MagicMock()
    info = SubagentInfo(id="resolved-claim", task="task")

    await manager._run(info)

    assert info._coordinator_claim_uncertain is False
    assert info._coordinator_fence is not None
    assert info._coordinator_fence.owner_id == manager._coordinator_owner_id
    manager._run_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_shadow_claim_lookup_cannot_overwrite_replacement_fence() -> None:
    class DelayedLookupCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.lookup_started = asyncio.Event()
            self.release_lookup = asyncio.Event()

        async def claim_command(self, command_id, owner):
            await super().claim_command(command_id, owner)
            raise asyncio.TimeoutError

        async def get_command_by_key(self, idempotency_key):
            self.lookup_started.set()
            await self.release_lookup.wait()
            return await super().get_command_by_key(idempotency_key)

    coordinator = DelayedLookupCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="stale-claim-lookup", task="task")
    info._coordinator_shadow_generation = 1
    old_attempt = asyncio.create_task(manager._shadow_submit_accepted_run(info, generation=1))
    await coordinator.lookup_started.wait()

    replacement_fence = MagicMock()
    info._coordinator_shadow_generation = 2
    info._coordinator_fence = replacement_fence
    coordinator.release_lookup.set()
    await old_attempt

    assert info._coordinator_fence is replacement_fence
    assert info._coordinator_claim_uncertain is False


@pytest.mark.asyncio
async def test_replacement_adopts_claim_committed_by_stale_shadow_attempt() -> None:
    class DelayedFirstClaimCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.first_claim_committed = asyncio.Event()
            self.release_first_claim = asyncio.Event()
            self.claim_calls = 0

        async def claim_command(self, command_id, owner):
            self.claim_calls += 1
            claim = await super().claim_command(command_id, owner)
            if self.claim_calls == 1:
                self.first_claim_committed.set()
                await self.release_first_claim.wait()
            return claim

    coordinator = DelayedFirstClaimCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="stale-claim-response", task="task")
    info._coordinator_shadow_generation = 1
    first_attempt = asyncio.create_task(manager._shadow_submit_accepted_run(info, generation=1))
    await coordinator.first_claim_committed.wait()

    info._coordinator_shadow_generation = 2
    replacement_durable = await manager._shadow_submit_accepted_run(info, generation=2)

    assert replacement_durable is True
    assert info._coordinator_fence is not None
    assert info._coordinator_fence.owner_id == manager._coordinator_owner_id
    assert info._coordinator_claim_uncertain is False

    coordinator.release_first_claim.set()
    await first_attempt


@pytest.mark.asyncio
async def test_slow_shadow_submission_waits_for_durable_admission_before_execution() -> None:
    class DelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.submit_started = asyncio.Event()
            self.release_submit = asyncio.Event()

        async def submit(self, request):
            self.submit_started.set()
            await self.release_submit.wait()
            return await super().submit(request)

    coordinator = DelayedSubmitCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="durable-admission", task="task")
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager._start_coordinator_heartbeat = MagicMock()

    run_task = asyncio.create_task(manager._run(info))
    await coordinator.submit_started.wait()
    await asyncio.sleep(0)

    assert run_task.done() is False
    manager._run_inner.assert_not_awaited()
    assert manager._coordinator_shadow_submits

    coordinator.release_submit.set()
    await run_task

    receipt = await coordinator.get_command_by_key("spawn:durable-admission")
    assert receipt is not None
    assert receipt.command.status.value == "claimed"
    assert info._coordinator_claim_uncertain is False
    manager._run_inner.assert_awaited_once_with(info, "subagent:durable-admission")


@pytest.mark.asyncio
async def test_user_stop_waits_for_retained_admission_and_reports_once() -> None:
    class DelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.submit_started = asyncio.Event()
            self.release_submit = asyncio.Event()

        async def submit(self, request):
            self.submit_started.set()
            await self.release_submit.wait()
            return await super().submit(request)

    coordinator = DelayedSubmitCoordinator()
    sessions = MagicMock()
    sessions.reset = AsyncMock()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    info = SubagentInfo(id="stopped-admission", task="task")
    manager._agents[info.id] = info
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager._write_tombstone = MagicMock()
    manager._fire_event = AsyncMock()

    run_task = asyncio.create_task(manager._run(info))
    manager._tasks[info.id] = run_task
    await coordinator.submit_started.wait()

    cancel_task = asyncio.create_task(manager.cancel(info.id))
    for _ in range(10):
        await asyncio.sleep(0)

    assert cancel_task.done() is False
    on_done.assert_not_awaited()

    coordinator.release_submit.set()
    assert await cancel_task is True
    await asyncio.gather(run_task, return_exceptions=True)

    receipt = await coordinator.get_command_by_key("spawn:stopped-admission")
    assert receipt is not None
    assert receipt.run.outcome is RunOutcome.STOPPED
    manager._run_inner.assert_not_awaited()
    on_done.assert_awaited_once()

    claims = await coordinator.claim_recovery(
        OwnerLease("recovery", time.time() + 100),
        limit=1,
    )
    assert claims == []


@pytest.mark.asyncio
async def test_user_stop_resolves_uncertain_claim_before_reporting() -> None:
    class UncertainClaimCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.claim_calls = 0
            self.lookup_started = asyncio.Event()
            self.release_lookup = asyncio.Event()

        async def claim_command(self, command_id, owner):
            self.claim_calls += 1
            if self.claim_calls == 1:
                raise asyncio.TimeoutError
            return await super().claim_command(command_id, owner)

        async def get_command_by_key(self, idempotency_key):
            self.lookup_started.set()
            await self.release_lookup.wait()
            return await super().get_command_by_key(idempotency_key)

    coordinator = UncertainClaimCoordinator()
    sessions = MagicMock()
    sessions.reset = AsyncMock()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    info = SubagentInfo(id="stopped-uncertain-claim", task="task")
    manager._agents[info.id] = info
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager._write_tombstone = MagicMock()
    manager._fire_event = AsyncMock()

    run_task = asyncio.create_task(manager._run(info))
    manager._tasks[info.id] = run_task
    await coordinator.lookup_started.wait()

    cancel_task = asyncio.create_task(manager.cancel(info.id))
    for _ in range(10):
        await asyncio.sleep(0)

    assert cancel_task.done() is False
    on_done.assert_not_awaited()

    coordinator.release_lookup.set()
    assert await cancel_task is True
    await asyncio.gather(run_task, return_exceptions=True)

    receipt = await coordinator.get_command_by_key("spawn:stopped-uncertain-claim")
    assert receipt is not None
    assert receipt.run.outcome is RunOutcome.STOPPED
    assert coordinator.claim_calls == 2
    manager._run_inner.assert_not_awaited()
    on_done.assert_awaited_once()

    claims = await coordinator.claim_recovery(
        OwnerLease("recovery", time.time() + 100),
        limit=1,
    )
    assert claims == []


@pytest.mark.asyncio
async def test_user_stop_can_settle_admission_that_already_returned_uncertain() -> None:
    class UncertainAdmissionCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.claim_calls = 0

        async def claim_command(self, command_id, owner):
            self.claim_calls += 1
            if self.claim_calls == 1:
                raise asyncio.TimeoutError
            return await super().claim_command(command_id, owner)

    coordinator = UncertainAdmissionCoordinator()
    sessions = MagicMock()
    sessions.reset = AsyncMock()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    info = SubagentInfo(id="stopped-after-uncertain-admission", task="task")
    manager._agents[info.id] = info
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager._write_tombstone = MagicMock()
    manager._fire_event = AsyncMock()

    await manager._run(info)

    assert info._coordinator_claim_uncertain is True
    assert info.done is False
    assert info.error == ""
    manager._run_inner.assert_not_awaited()
    on_done.assert_not_awaited()

    assert await manager.cancel(info.id) is True

    receipt = await coordinator.get_command_by_key("spawn:stopped-after-uncertain-admission")
    assert receipt is not None
    assert receipt.run.outcome is RunOutcome.STOPPED
    assert coordinator.claim_calls == 2
    on_done.assert_awaited_once()

    claims = await coordinator.claim_recovery(
        OwnerLease("recovery", time.time() + 100),
        limit=1,
    )
    assert claims == []


@pytest.mark.asyncio
async def test_user_stop_yields_when_recovery_terminalized_uncertain_claim() -> None:
    now = time.time()
    coordinator = MemoryRunCoordinator(clock=lambda: now)
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="stopped-after-recovery", task="task")
    manager._agents[info.id] = info

    assert await manager._shadow_submit_accepted_run(info) is True
    info._coordinator_fence = None
    info._coordinator_claim_uncertain = True
    now += 1_000
    claims = await coordinator.claim_recovery(
        OwnerLease("recovery", now + 100),
        limit=1,
    )
    assert len(claims) == 1
    claim = claims[0]
    completion = manager._run_recovery.completion_for(claim.run)
    await coordinator.complete(completion, claim.fence, claim.run.version)
    manager._force_reap = AsyncMock()
    manager._fire_event = AsyncMock()

    assert await asyncio.wait_for(manager.cancel(info.id), timeout=1) is True

    assert info._coordinator_claim_uncertain is True
    manager._force_reap.assert_not_awaited()
    attempts = await manager._outbox_delivery.drain_once()
    assert attempts
    recovered = manager._agents[info.id]
    assert recovered is not info
    assert recovered.done is True
    assert recovered.outcome == "interrupted"


@pytest.mark.asyncio
async def test_user_stop_yields_to_live_recovery_owner() -> None:
    now = time.time()
    coordinator = MemoryRunCoordinator(clock=lambda: now)
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="stopped-during-recovery", task="task")
    manager._agents[info.id] = info

    assert await manager._shadow_submit_accepted_run(info) is True
    info._coordinator_fence = None
    info._coordinator_claim_uncertain = True
    now += 1_000
    claims = await coordinator.claim_recovery(
        OwnerLease("recovery", now + 100),
        limit=1,
    )
    assert len(claims) == 1
    manager._force_reap = AsyncMock()

    assert await asyncio.wait_for(manager.cancel(info.id), timeout=1) is True

    assert info._coordinator_claim_uncertain is True
    manager._force_reap.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_cancel_recovery_does_not_respawn_after_user_stop() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._run = AsyncMock()
    info = SubagentInfo(id="stopped-before-respawn", task="task")
    info._recovering = True
    armed = asyncio.Event()
    release_original = asyncio.Event()

    async def original_run() -> None:
        manager._schedule_cancel_recovery(info)
        armed.set()
        await release_original.wait()

    original = asyncio.create_task(original_run())
    await armed.wait()
    recovery = manager._tasks[f"{info.id}:recovery"]
    info.user_stopped = True
    release_original.set()
    await original
    await recovery

    assert info._recovering is False
    assert info._coordinator_shadow_generation == 0
    manager._run.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_stop_uses_legacy_report_after_definite_submit_failure() -> None:
    sessions = MagicMock()
    sessions.reset = AsyncMock()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
        on_done=on_done,
    )
    info = SubagentInfo(id="stopped-failed-submit", task="task")
    info._coordinator_claim_uncertain = True
    manager._agents[info.id] = info
    manager._write_tombstone = MagicMock()
    manager._fire_event = AsyncMock()
    manager._resolve_stopped_shadow_claim = AsyncMock()

    failed_submit = asyncio.create_task(asyncio.sleep(0, result=False))
    manager._coordinator_shadow_submits.add(failed_submit)
    manager._coordinator_shadow_submit_owners[failed_submit] = info

    def forget_failed_submit(done: asyncio.Task[bool]) -> None:
        manager._coordinator_shadow_submits.discard(done)
        manager._coordinator_shadow_submit_owners.pop(done, None)

    failed_submit.add_done_callback(forget_failed_submit)

    assert await manager.cancel(info.id) is True

    assert info._coordinator_claim_uncertain is False
    manager._resolve_stopped_shadow_claim.assert_not_awaited()
    on_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_submit_outcome_survives_settlement_task_handoff() -> None:
    class FailedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.submit_started = asyncio.Event()
            self.release_submit = asyncio.Event()

        async def submit(self, request):
            self.submit_started.set()
            await self.release_submit.wait()
            raise OSError("submit failed")

    coordinator = FailedSubmitCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="failed-submit-handoff", task="task")

    admission_task = asyncio.create_task(manager._await_retained_shadow_submit(info))
    await coordinator.submit_started.wait()
    admission_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await admission_task
    info.user_stopped = True

    coordinator.release_submit.set()
    assert await manager._drain_retained_shadow_submits(info) is False
    assert not manager._coordinator_shadow_submits
    assert await manager._drain_retained_shadow_submits(info) is False


@pytest.mark.asyncio
async def test_retained_submit_drain_yields_finished_task_callbacks(monkeypatch) -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    info = SubagentInfo(id="finished-submit-callback", task="task")
    info._coordinator_shadow_submission_durable = False

    finished = asyncio.create_task(asyncio.sleep(0, result=False))
    await finished
    manager._coordinator_shadow_submits.add(finished)
    manager._coordinator_shadow_submit_owners[finished] = info

    def cleanup(done: asyncio.Task[bool]) -> None:
        manager._coordinator_shadow_submits.discard(done)
        manager._coordinator_shadow_submit_owners.pop(done, None)

    finished.add_done_callback(cleanup)
    gather_calls = 0

    async def bounded_gather(*tasks, **kwargs):
        nonlocal gather_calls
        gather_calls += 1
        assert gather_calls == 1, "finished retained task was polled without yielding"
        assert kwargs == {"return_exceptions": True}
        return [task.result() for task in tasks]

    monkeypatch.setattr(asyncio, "gather", bounded_gather)

    assert await manager._drain_retained_shadow_submits(info) is False
    assert not manager._coordinator_shadow_submit_owners


@pytest.mark.asyncio
async def test_authoritatively_admitted_run_does_not_submit_twice() -> None:
    coordinator = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager._coordinator_mark_starting = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock()
    info = SubagentInfo(id="run-5", task="task", _coordinator_admitted=True)
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    coordinator.submit.assert_not_awaited()
    manager._run_inner.assert_awaited_once()
    manager.command_authority.execution_started.assert_awaited_once_with("run-5")


@pytest.mark.asyncio
async def test_failed_start_settlement_keeps_waiting_run_retryable() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=True)
    manager._coordinator_mark_starting = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock(
        side_effect=AuthorityOutcomeUncertain("write failed")
    )
    manager.command_authority.stop_execution_heartbeat = AsyncMock()
    info = SubagentInfo(
        id="waiting-run",
        task="task",
        batch_id="waiting-wave",
        batch_total=3,
        _coordinator_admitted=True,
        _coordinator_waiting=True,
    )
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    assert info._coordinator_waiting is True
    assert info._coordinator_claim_uncertain is True
    assert info.done is False
    assert manager._outbox_live_run_batches[info.id] == ("waiting-wave", 3)
    manager._run_inner.assert_not_awaited()
    manager._claim_finalize.assert_not_called()
    assert manager.command_authority.execution_started.await_count == 2
    manager.command_authority.stop_execution_heartbeat.assert_awaited_once_with("waiting-run")


@pytest.mark.asyncio
async def test_failed_lifecycle_start_transition_never_applies_command() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=True)
    manager._coordinator_mark_starting = AsyncMock(side_effect=OSError("write failed"))
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock()
    info = SubagentInfo(
        id="uncommitted-start",
        task="task",
        batch_id="starting-wave",
        batch_total=4,
        _coordinator_admitted=True,
        _coordinator_waiting=True,
    )
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    assert info._coordinator_waiting is True
    assert info._coordinator_claim_uncertain is True
    assert info.done is False
    assert manager._outbox_live_run_batches[info.id] == ("starting-wave", 4)
    manager.command_authority.execution_started.assert_not_awaited()
    manager._run_inner.assert_not_awaited()
    manager._claim_finalize.assert_not_called()


@pytest.mark.asyncio
async def test_lost_start_settlement_response_reconciles_before_execution() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager._coordinator_mark_starting = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock(
        side_effect=[AuthorityOutcomeUncertain("response lost"), None]
    )
    info = SubagentInfo(
        id="reconciled-start",
        task="task",
        _coordinator_admitted=True,
        _coordinator_waiting=True,
    )
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    assert info._coordinator_waiting is False
    assert info._coordinator_claim_uncertain is False
    assert manager.command_authority.execution_started.await_count == 2
    manager._run_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_queued_cancel_keeps_authority_lease_until_terminal_commit() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    queued = {"_preassigned_id": "queued-run"}
    manager._unqueue = MagicMock(return_value=[queued])
    manager._finalize_queued_cancel = AsyncMock()
    manager.command_authority.stop_execution_heartbeat = AsyncMock()

    assert await manager.cancel("queued-run") is True

    manager.command_authority.stop_execution_heartbeat.assert_not_awaited()
    manager._finalize_queued_cancel.assert_awaited_once_with(queued)


@pytest.mark.asyncio
async def test_queued_cancel_preserves_silent_delivery_setting() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), on_done=AsyncMock())
    manager._safe_announce = AsyncMock()

    await manager._finalize_queued_cancel(
        {
            "_preassigned_id": "silent-queued-run",
            "task": "task",
            "conversation_key": "subagent:continued-run",
            "batch_id": "silent-wave",
            "batch_total": 1,
            "silent": True,
        }
    )

    manager._safe_announce.assert_awaited_once()
    announced = manager._safe_announce.await_args.args[0]
    assert announced.silent is True
    assert announced.conversation_key == "subagent:continued-run"


@pytest.mark.asyncio
async def test_manager_shutdown_closes_command_authority() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager.command_authority.close = AsyncMock()

    await manager.cancel_all()

    manager.command_authority.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cancelled_shutdown_readmits_unreported_terminal_owner() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._cancel_all_impl = AsyncMock(side_effect=asyncio.CancelledError)
    report_task = asyncio.create_task(asyncio.Event().wait())
    info = SubagentInfo(id="unreported-shadow-fallback", task="task")
    manager._lifecycle.pending_reports = MagicMock(return_value={report_task})
    manager._lifecycle.owner_for = MagicMock(return_value=info)
    event_loop_thread = threading.get_ident()
    cleared: list[str] = []

    def _clear_tombstone(agent_id: str) -> bool:
        assert threading.get_ident() != event_loop_thread
        cleared.append(agent_id)
        return True

    try:
        with patch(
            "kiro_crew.subagent.clear_tombstone_for_recovery",
            side_effect=_clear_tombstone,
        ) as clear:
            with pytest.raises(asyncio.CancelledError):
                await manager.cancel_all()
        clear.assert_called_once_with(info.id)
        assert cleared == [info.id]
    finally:
        report_task.cancel()
        await asyncio.gather(report_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_manager_shutdown_retains_active_shadow_submission_for_recovery() -> None:
    class DelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_submit = asyncio.Event()
            self.submit_started = asyncio.Event()
            self.submit_cancelled = asyncio.Event()

        async def submit(self, request):
            self.submit_started.set()
            try:
                await self.release_submit.wait()
            except asyncio.CancelledError:
                self.submit_cancelled.set()
                raise
            return await super().submit(request)

    coordinator = DelayedSubmitCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager.command_authority.close = AsyncMock()
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    info = SubagentInfo(id="shutdown-shadow-submit", task="task")
    manager._agents[info.id] = info
    run_task = asyncio.create_task(manager._run(info))
    manager._tasks[info.id] = run_task
    await coordinator.submit_started.wait()

    shutdown_task = asyncio.create_task(manager.cancel_all())
    try:
        await run_task

        assert coordinator.submit_cancelled.is_set() is False
        assert shutdown_task.done() is False
    finally:
        coordinator.release_submit.set()
        await shutdown_task

    assert info._coordinator_fence is not None
    assert info._coordinator_claim_uncertain is True
    assert manager._coordinator_shadow_submits == set()
    on_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_shutdown_readmits_unsettled_shadow_submission() -> None:
    class DelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.submit_started = asyncio.Event()
            self.submit_cancelled = asyncio.Event()

        async def submit(self, request):
            self.submit_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.submit_cancelled.set()
                raise

    coordinator = DelayedSubmitCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=AsyncMock(),
    )
    manager.command_authority.close = AsyncMock()
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    info = SubagentInfo(id="cancelled-shutdown-submit", task="task")
    manager._agents[info.id] = info
    run_task = asyncio.create_task(manager._run(info))
    manager._tasks[info.id] = run_task
    await coordinator.submit_started.wait()

    shutdown_task = asyncio.create_task(manager.cancel_all())
    await run_task
    for _ in range(100):
        if not manager._coordinator_shadow_submits:
            break
        await asyncio.sleep(0)
    assert manager._coordinator_shadow_submits == set()
    assert shutdown_task.done() is False

    with patch(
        "kiro_crew.subagent.clear_tombstone_for_recovery",
        return_value=True,
    ) as clear:
        shutdown_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown_task

    assert coordinator.submit_cancelled.is_set()
    clear.assert_any_call(info.id)


@pytest.mark.asyncio
async def test_cancelled_shutdown_readmits_submit_before_drain_starts() -> None:
    class DelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_submit = asyncio.Event()
            self.submit_started = asyncio.Event()

        async def submit(self, request):
            self.submit_started.set()
            await self.release_submit.wait()
            return await super().submit(request)

    coordinator = DelayedSubmitCoordinator()
    teardown_started = asyncio.Event()

    async def delayed_teardown(*_args) -> None:
        teardown_started.set()
        await asyncio.Event().wait()

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=AsyncMock(),
    )
    manager.command_authority.close = AsyncMock()
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock(side_effect=delayed_teardown)
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    info = SubagentInfo(id="pre-drain-shutdown-submit", task="task")
    manager._agents[info.id] = info
    run_task = asyncio.create_task(manager._run(info))
    manager._tasks[info.id] = run_task
    await coordinator.submit_started.wait()

    shutdown_task = asyncio.create_task(manager.cancel_all())
    await teardown_started.wait()

    with patch(
        "kiro_crew.subagent.clear_tombstone_for_recovery",
        return_value=True,
    ) as clear:
        shutdown_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown_task

    clear.assert_called_once_with(info.id)
    assert run_task.done()

    coordinator.release_submit.set()
    retained = list(manager._coordinator_shadow_submits)
    if retained:
        await asyncio.gather(*retained, return_exceptions=True)


@pytest.mark.asyncio
async def test_manager_shutdown_drains_retained_shadow_submission() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager.command_authority.close = AsyncMock()
    started = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()

    async def retained_submit() -> None:
        started.set()
        await release.wait()
        settled.set()

    submit_task = asyncio.create_task(retained_submit())
    manager._coordinator_shadow_submits.add(submit_task)
    await started.wait()

    shutdown_task = asyncio.create_task(manager.cancel_all())
    await asyncio.sleep(0)

    assert submit_task.cancelled() is False
    assert shutdown_task.done() is False

    release.set()
    await shutdown_task

    assert settled.is_set()
    assert manager._coordinator_shadow_submits == set()
    manager.command_authority.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_manager_shutdown_drains_report_created_by_late_shadow_settlement() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager.command_authority.close = AsyncMock()
    submit_release = asyncio.Event()
    report_started = asyncio.Event()
    report_release = asyncio.Event()
    info = SubagentInfo(id="late-shadow-report", task="task")

    async def retained_submit() -> None:
        await submit_release.wait()

    async def late_report() -> None:
        report_started.set()
        await report_release.wait()

    submit_task = asyncio.create_task(retained_submit())
    manager._coordinator_shadow_submits.add(submit_task)

    def schedule_report(_done: asyncio.Task[None]) -> None:
        manager._lifecycle.spawn_report(info, late_report)

    submit_task.add_done_callback(schedule_report)
    shutdown_task = asyncio.create_task(manager.cancel_all())
    await asyncio.sleep(0)
    submit_release.set()
    await report_started.wait()

    assert shutdown_task.done() is False

    report_release.set()
    await shutdown_task


@pytest.mark.asyncio
async def test_process_identity_persistence_failure_aborts_before_execution(monkeypatch) -> None:
    sessions = MagicMock()
    sessions.get_pid.return_value = 4321
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    info = SubagentInfo(id="run-process", task="inspect")
    info._coordinator_fence = RunFence(info.id, "executor", 1)
    manager._coordinator_record_process = AsyncMock(side_effect=OSError("write failed"))
    monkeypatch.setattr(
        "kiro_crew.subagent.platform_compat.process_identity_token",
        lambda _pid: "s1",
    )
    monkeypatch.setattr("kiro_crew.subagent.update_state", MagicMock())

    with pytest.raises(OSError, match="write failed"):
        await manager._record_process_identity(info, "subagent:run-process")

    assert info._pid == 4321


@pytest.mark.asyncio
async def test_process_identity_mirrors_legacy_state_off_loop(monkeypatch) -> None:
    sessions = MagicMock()
    sessions.get_pid.return_value = 4321
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    manager._coordinator_record_process = AsyncMock()
    info = SubagentInfo(id="run-process-off-loop", task="inspect")
    info._coordinator_fence = RunFence(info.id, "executor", 1)
    event_loop_thread = threading.get_ident()
    update_threads: list[int] = []
    monkeypatch.setattr(
        "kiro_crew.subagent.platform_compat.process_identity_token",
        lambda _pid: "s1",
    )
    monkeypatch.setattr(
        "kiro_crew.subagent.update_state",
        lambda *_args, **_kwargs: update_threads.append(threading.get_ident()),
    )

    await manager._record_process_identity(info, "subagent:run-process-off-loop")

    assert update_threads
    assert update_threads != [event_loop_thread]
    manager._coordinator_record_process.assert_awaited_once_with(
        info,
        4321,
        "s1",
        True,
    )


@pytest.mark.asyncio
async def test_missing_process_start_identity_records_unowned_process(monkeypatch) -> None:
    sessions = MagicMock()
    sessions.get_pid.return_value = 4321
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    manager._coordinator_record_process = AsyncMock()
    manager._write_state_off_loop = AsyncMock()
    info = SubagentInfo(id="run-process-unidentified", task="inspect")
    info._coordinator_fence = RunFence(info.id, "executor", 1)
    monkeypatch.setattr(
        "kiro_crew.subagent.platform_compat.process_identity_token",
        lambda _pid: None,
    )

    await manager._record_process_identity(info, "subagent:run-process-unidentified")

    manager._write_state_off_loop.assert_awaited_once()
    state_args = manager._write_state_off_loop.await_args
    assert state_args.args == (info, "PID record")
    assert state_args.kwargs["pid"] == 4321
    assert state_args.kwargs["pid_start_id"] == ""
    assert state_args.kwargs["process_owned"] is False
    manager._coordinator_record_process.assert_awaited_once_with(
        info,
        4321,
        "",
        False,
    )


@pytest.mark.asyncio
async def test_process_identity_requires_an_execution_fence() -> None:
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )

    with pytest.raises(RuntimeError, match="execution fence is missing"):
        await manager._coordinator_record_process(
            SubagentInfo(id="unfenced-run", task="inspect"),
            4321,
            "start-1",
            True,
        )
