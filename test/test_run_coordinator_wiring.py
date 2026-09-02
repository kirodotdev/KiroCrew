"""The typed coordinator seam is injectable before it becomes authoritative."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.run_coordinator import (
    CommandOperation,
    MemoryRunCoordinator,
    OwnerLease,
    SQLiteRunCoordinator,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_command_authority import AuthorityOutcomeUncertain


def test_subagent_manager_defaults_to_durable_sqlite_coordinator() -> None:
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
    claims = await coordinator.claim_commands(
        owner=OwnerLease(owner_id="test-owner", lease_expires_at=9_999_999_999.0),
        limit=1,
    )
    assert len(claims) == 1
    command = claims[0].command
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
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

    await manager._shadow_submit_accepted_run(SubagentInfo(id="durable-run", task="task"))

    stored = await SQLiteRunCoordinator(tmp_path / "run-coordinator/coordinator.db").get_run(
        "durable-run"
    )
    assert stored is not None
    assert stored.task == "task"


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

    claims = await coordinator.claim_commands(
        owner=OwnerLease(owner_id="test-owner", lease_expires_at=9_999_999_999.0),
        limit=2,
    )
    assert len(claims) == 1
    assert claims[0].command.operation is CommandOperation.CONTINUE
    assert claims[0].command.command_id == "continue:run-2"


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
async def test_run_records_shadow_submission_before_execution() -> None:
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
    manager._claim_finalize = MagicMock(return_value=False)

    await manager._run(SubagentInfo(id="run-4", task="task"))

    assert order == ["submit", "execute"]


@pytest.mark.asyncio
async def test_stalled_shadow_submission_does_not_block_legacy_execution() -> None:
    coordinator = AsyncMock()
    never_settles = asyncio.Event()

    async def submit(_request: object) -> None:
        await never_settles.wait()

    coordinator.submit.side_effect = submit
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="durable-admission", task="task")
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)

    with patch("kiro_crew.subagent._SHADOW_SUBMIT_TIMEOUT_SECS", 0):
        await manager._run(info)

    manager._run_inner.assert_awaited_once_with(info, "subagent:durable-admission")


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
    manager.command_authority.execution_started = AsyncMock()
    info = SubagentInfo(id="run-5", task="task", _coordinator_admitted=True)

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
        _coordinator_admitted=True,
        _coordinator_waiting=True,
    )
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    assert info._coordinator_waiting is True
    assert info._coordinator_claim_uncertain is True
    assert info.done is False
    manager._run_inner.assert_not_awaited()
    manager._claim_finalize.assert_not_called()
    assert manager.command_authority.execution_started.await_count == 2
    manager.command_authority.stop_execution_heartbeat.assert_awaited_once_with("waiting-run")


@pytest.mark.asyncio
async def test_lost_start_settlement_response_reconciles_before_execution() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager.command_authority.execution_started = AsyncMock(
        side_effect=[AuthorityOutcomeUncertain("response lost"), None]
    )
    info = SubagentInfo(
        id="reconciled-start",
        task="task",
        _coordinator_admitted=True,
        _coordinator_waiting=True,
    )

    await manager._run(info)

    assert info._coordinator_waiting is False
    assert info._coordinator_claim_uncertain is False
    assert manager.command_authority.execution_started.await_count == 2
    manager._run_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_queued_cancel_stops_the_authority_lease() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._unqueue = MagicMock(return_value=[{"_preassigned_id": "queued-run"}])
    manager.command_authority.stop_execution_heartbeat = AsyncMock()

    assert await manager.cancel("queued-run") is True

    manager.command_authority.stop_execution_heartbeat.assert_awaited_once_with("queued-run")


@pytest.mark.asyncio
async def test_manager_shutdown_closes_command_authority() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager.command_authority.close = AsyncMock()

    await manager.cancel_all()

    manager.command_authority.close.assert_awaited_once_with()
