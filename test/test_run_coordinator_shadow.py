"""Shadow coordinator preserves the primary decision under every divergence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.run_coordinator import (
    CommandOperation,
    CommandStatus,
    CoordinatorDecision,
    CoordinatorResult,
    DeliveryState,
    MemoryRunCoordinator,
    OwnerLease,
    RunCompletion,
    RunFence,
    RunOutcome,
    ShadowRunCoordinator,
    SubmitControl,
    SubmitRun,
)


def _request(operation: CommandOperation = CommandOperation.SPAWN) -> SubmitRun:
    return SubmitRun(
        run_id="run-1",
        command_id="command-1",
        idempotency_key="key-1",
        payload_hash="hash-1",
        parent_session="dashboard:parent",
        agent="researcher",
        task="private task payload",
        conversation_key="",
        operation=operation,
    )


@pytest.mark.asyncio
async def test_shadow_failure_cannot_change_primary_success() -> None:
    primary = MemoryRunCoordinator()
    shadow = AsyncMock()
    shadow.submit.side_effect = RuntimeError("database unavailable")
    coordinator = ShadowRunCoordinator(primary, shadow)

    result = await coordinator.submit(_request())

    assert result.decision is CoordinatorDecision.APPLIED
    shadow.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_shadow_cancellation_cannot_discard_committed_primary_success() -> None:
    primary = MemoryRunCoordinator()
    shadow = AsyncMock()
    shadow.submit.side_effect = asyncio.CancelledError
    coordinator = ShadowRunCoordinator(primary, shadow)

    result = await coordinator.submit(_request())

    assert result.decision is CoordinatorDecision.APPLIED


@pytest.mark.asyncio
async def test_shadow_comparison_failure_cannot_discard_committed_primary_success() -> None:
    class ExplodingEquality:
        def __eq__(self, _other: object) -> bool:
            raise RuntimeError("comparison failed")

    primary = MemoryRunCoordinator()
    shadow = AsyncMock()
    shadow.submit.return_value = ExplodingEquality()
    coordinator = ShadowRunCoordinator(primary, shadow)

    result = await coordinator.submit(_request())

    assert result.decision is CoordinatorDecision.APPLIED


@pytest.mark.asyncio
async def test_primary_failure_prevents_shadow_mutation() -> None:
    primary = AsyncMock()
    primary.submit.side_effect = RuntimeError("primary failed")
    shadow = AsyncMock()
    coordinator = ShadowRunCoordinator(primary, shadow)

    with pytest.raises(RuntimeError, match="primary failed"):
        await coordinator.submit(_request())

    shadow.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_primary_typed_rejection_prevents_shadow_mutation() -> None:
    primary = MemoryRunCoordinator()
    shadow = AsyncMock()
    coordinator = ShadowRunCoordinator(primary, shadow)

    result = await coordinator.submit(_request(CommandOperation.STEER))

    assert result.decision is CoordinatorDecision.REJECTED
    shadow.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_primary_false_renew_prevents_shadow_mutation() -> None:
    primary = MemoryRunCoordinator()
    shadow = AsyncMock()
    coordinator = ShadowRunCoordinator(primary, shadow)

    renewed = await coordinator.renew(
        "missing-run",
        RunFence(run_id="missing-run", owner_id="gateway", lease_epoch=1),
        until=20.0,
    )

    assert renewed is False
    shadow.renew.assert_not_awaited()


@pytest.mark.asyncio
async def test_mismatch_reports_field_classes_without_payload_values() -> None:
    mismatches: list[tuple[str, frozenset[str]]] = []
    primary = MemoryRunCoordinator(clock=lambda: 10.0)
    shadow = MemoryRunCoordinator(clock=lambda: 20.0)
    coordinator = ShadowRunCoordinator(
        primary,
        shadow,
        on_mismatch=lambda operation, fields: mismatches.append((operation, fields)),
    )

    result = await coordinator.submit(_request())

    assert result.decision is CoordinatorDecision.APPLIED
    # Clock-only divergence is intentionally excluded from parity.
    assert mismatches == []


@pytest.mark.asyncio
async def test_mismatch_reports_bounded_nested_field_classes_without_values() -> None:
    mismatches: list[tuple[str, frozenset[str]]] = []
    primary = MemoryRunCoordinator()
    shadow_store = MemoryRunCoordinator()

    async def mismatch_submit(request: SubmitRun) -> CoordinatorResult:
        result = await shadow_store.submit(request)
        assert result.value is not None
        return replace(
            result,
            value=replace(
                result.value,
                run=replace(
                    result.value.run,
                    agent="different-agent",
                    task="different private task",
                ),
            ),
        )

    shadow = AsyncMock()
    shadow.submit.side_effect = mismatch_submit
    coordinator = ShadowRunCoordinator(
        primary,
        shadow,
        on_mismatch=lambda operation, fields: mismatches.append((operation, fields)),
    )

    result = await coordinator.submit(_request())

    assert result.decision is CoordinatorDecision.APPLIED
    assert mismatches == [("submit", frozenset({"value.run.agent", "value.run.task"}))]
    assert all("different" not in field for _, fields in mismatches for field in fields)


@pytest.mark.asyncio
async def test_generated_event_id_does_not_create_shadow_parity_mismatch() -> None:
    mismatches: list[tuple[str, frozenset[str]]] = []

    def clock() -> float:
        return 10.0

    def record_mismatch(operation: str, fields: frozenset[str]) -> None:
        mismatches.append((operation, fields))

    coordinator = ShadowRunCoordinator(
        MemoryRunCoordinator(clock=clock),
        MemoryRunCoordinator(clock=clock),
        on_mismatch=record_mismatch,
    )

    submitted = await coordinator.submit(_request())
    assert submitted.value is not None
    claim = (
        await coordinator.claim_commands(OwnerLease("gateway", lease_expires_at=20.0), limit=1)
    )[0]
    starting = await coordinator.mark_starting(claim.command, claim.fence, claim.run.version)
    assert starting.value is not None
    running = await coordinator.mark_running("run-1", claim.fence, starting.value.version)
    assert running.value is not None
    completed = await coordinator.complete(
        RunCompletion(
            run_id="run-1",
            outcome=RunOutcome.COMPLETED,
            result_path="/tmp/result.txt",
            error="",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"summary":"done"}',
            terminal_at=10.0,
        ),
        claim.fence,
        running.value.version,
    )

    assert completed.value is not None
    assert completed.value.status is DeliveryState.PENDING
    assert mismatches == []


@pytest.mark.asyncio
async def test_mismatch_observer_failure_cannot_change_primary_result() -> None:
    primary = MemoryRunCoordinator(clock=lambda: 10.0)
    shadow = AsyncMock()
    shadow.submit.return_value = None

    def broken_observer(_operation: str, _fields: frozenset[str]) -> None:
        raise RuntimeError("metrics unavailable")

    coordinator = ShadowRunCoordinator(primary, shadow, on_mismatch=broken_observer)

    result = await coordinator.submit(_request())

    assert result.decision is CoordinatorDecision.APPLIED


@pytest.mark.asyncio
async def test_shadow_mirrors_control_claim_finish_and_current_lookup() -> None:
    primary = MemoryRunCoordinator(clock=lambda: 10.0)
    shadow = MemoryRunCoordinator(clock=lambda: 10.0)
    coordinator = ShadowRunCoordinator(primary, shadow)
    request = SubmitControl(
        command_id="control-1",
        idempotency_key="control-key-1",
        run_id="legacy-run",
        operation=CommandOperation.STEER,
        payload_hash="control-hash-1",
        payload_json='{"message":"focus"}',
    )

    submitted = await coordinator.submit_control(request)
    claim = await coordinator.claim_command(
        "control-1", OwnerLease("controller", lease_expires_at=20.0)
    )
    assert claim is not None
    finished = await coordinator.finish_command(
        claim.command_fence,
        CommandStatus.APPLIED,
        "",
        '{"ok":true}',
    )
    queried = await coordinator.get_command_by_key("control-key-1")

    assert submitted.value is not None
    assert submitted.value.run is None
    assert finished.value is not None
    assert finished.value.result_json == '{"ok":true}'
    assert queried is not None
    assert queried.command == finished.value
