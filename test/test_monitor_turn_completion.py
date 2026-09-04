"""Completed-turn accounting for structured monitors."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy

import pytest

from kiro_crew.acp.types import TurnUsage
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.monitoring import models
from kiro_crew.monitoring.models import MonitorBudgets, MonitorOutcome, MonitorState


def _structured_loop() -> NudgeLoop:
    return NudgeLoop(
        id="monitor1",
        slot_key="chat-1-123",
        message="inspect the changed pull request",
        monitor=MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
        ),
    )


def test_monitor_completion_contract_is_typed() -> None:
    """An untyped completion payload could bypass correlation and validation."""
    assert hasattr(models, "MonitorActionCompletion")
    assert hasattr(models, "MonitorActionDisposition")
    completion = models.MonitorActionCompletion(
        monitor_id="monitor1",
        fingerprint="failure-a",
        disposition=models.MonitorActionDisposition.SUCCESS,
        completed_ts=1_120.0,
        input_tokens=1_200,
        output_tokens=None,
    )

    assert completion.monitor_id == "monitor1"
    assert completion.disposition is models.MonitorActionDisposition.SUCCESS
    assert completion.input_tokens == 1_200
    assert completion.output_tokens is None

    with pytest.raises(ValueError, match="input_tokens"):
        models.MonitorActionCompletion(
            monitor_id="monitor1",
            fingerprint="failure-a",
            disposition=models.MonitorActionDisposition.FAILURE,
            completed_ts=1_120.0,
            input_tokens=-1,
        )


@pytest.mark.asyncio
async def test_dispatch_charges_nothing_until_the_action_turn_completes(tmp_path) -> None:
    """Moving completed-turn accounting into dispatch would spend budget early."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop

    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_100.0)
    await service.record_monitor_dispatched(loop.id, "failure-a", now=1_105.0)
    assert loop.monitor is not None
    assert loop.monitor.agent_turns == 0
    assert loop.monitor.total_tokens == 0
    assert loop.monitor.wake_in_flight
    assert loop.monitor.wake_delivery is models.MonitorDispatchResult.DISPATCHED

    await service.record_monitor_turn_completion(
        models.MonitorActionCompletion(
            monitor_id=loop.id,
            fingerprint="failure-a",
            disposition=models.MonitorActionDisposition.SUCCESS,
            completed_ts=1_120.0,
            input_tokens=1_200,
            output_tokens=300,
        )
    )

    assert loop.monitor.agent_turns == 1
    assert loop.monitor.input_tokens == 1_200
    assert loop.monitor.output_tokens == 300
    assert not loop.monitor.wake_in_flight
    assert loop.monitor.wake_delivery is None


@pytest.mark.asyncio
async def test_completion_persistence_failure_leaves_live_accounting_unchanged(
    tmp_path, monkeypatch
) -> None:
    """A failed completion write cannot charge counters that restart will forget."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    assert loop.monitor is not None
    loop.monitor.budgets = MonitorBudgets(max_agent_turns=1)
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_100.0)
    before = deepcopy(loop)
    persisted_before = service._path.read_bytes()

    async def fail_snapshot(_payload=None):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", fail_snapshot)

    with pytest.raises(OSError, match="disk full"):
        await service.record_monitor_turn_completion(
            models.MonitorActionCompletion(
                monitor_id=loop.id,
                fingerprint="failure-a",
                disposition=models.MonitorActionDisposition.SUCCESS,
                completed_ts=1_120.0,
                input_tokens=1_200,
                output_tokens=300,
            )
        )

    assert loop == before
    assert service._path.read_bytes() == persisted_before


@pytest.mark.asyncio
async def test_claim_persistence_failure_leaves_live_monitor_unchanged(
    tmp_path, monkeypatch
) -> None:
    """A claim is not live until the snapshot that suppresses duplicates is durable."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop
    before = deepcopy(loop)

    async def fail_snapshot(_payload=None):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", fail_snapshot)

    with pytest.raises(OSError, match="disk full"):
        await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_100.0)

    assert loop == before


@pytest.mark.asyncio
async def test_budget_stop_persistence_failure_leaves_live_monitor_armed(
    tmp_path, monkeypatch
) -> None:
    """A failed terminal write cannot stop only the in-memory monitor."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    assert loop.monitor is not None
    loop.monitor.budgets = MonitorBudgets(max_runtime_secs=50)
    service._loops[loop.id] = loop
    before = deepcopy(loop)

    async def fail_snapshot(_payload=None):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", fail_snapshot)

    with pytest.raises(OSError, match="disk full"):
        await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_100.0)

    assert loop == before


@pytest.mark.asyncio
async def test_completion_winning_dispatch_race_counts_the_wake_once(tmp_path) -> None:
    """A synchronous channel completion can arrive before DISPATCHED is persisted."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_100.0)
    completion = models.MonitorActionCompletion(
        monitor_id=loop.id,
        fingerprint="failure-a",
        disposition=models.MonitorActionDisposition.SUCCESS,
        completed_ts=1_120.0,
    )

    await service.record_monitor_turn_completion(completion)
    await service.record_monitor_dispatched(loop.id, "failure-a", now=1_121.0)
    await service.record_monitor_turn_completion(completion)

    assert loop.monitor is not None
    assert loop.monitor.wake_count == 1
    assert loop.monitor.agent_turns == 1
    service.stop()


@pytest.mark.asyncio
async def test_dispatch_failure_before_turn_start_does_not_charge(tmp_path) -> None:
    """A failed handoff is uncharged and cannot duplicate its fingerprint."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_100.0)

    await service.record_monitor_dispatch_failure(loop.id, "failure-a")

    assert loop.monitor is not None
    assert loop.monitor.agent_turns == 0
    assert loop.monitor.total_tokens == 0
    assert not loop.monitor.wake_in_flight
    assert loop.monitor.outcome is models.MonitorOutcome.TARGET_UNAVAILABLE
    assert not await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_101.0)


@pytest.mark.asyncio
async def test_late_dispatch_failure_preserves_terminal_stop(tmp_path) -> None:
    """A delivery callback cannot replace a stop accepted during dispatch."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_100.0)
    await service.stop_monitor(loop.id, now=1_105.0)

    await service.record_monitor_dispatch_failure(loop.id, "failure-a", now=1_110.0)

    assert loop.monitor is not None
    assert loop.monitor.outcome is MonitorOutcome.USER_STOP
    assert loop.monitor.stopped_reason == "user_stop"


@pytest.mark.asyncio
async def test_late_completion_expiry_preserves_terminal_stop(tmp_path) -> None:
    """Missing-evidence recovery cannot replace a stop accepted during a turn."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_100.0)
    service.mark_monitor_turn_accepted(loop.id, "failure-a")
    await service.record_monitor_dispatched(loop.id, "failure-a", now=1_101.0)
    assert loop.monitor is not None
    evidence_deadline = loop.monitor.completion_evidence_deadline
    await service.stop_monitor(loop.id, now=1_105.0)

    assert loop.monitor.wake_in_flight
    assert loop.monitor.completion_evidence_deadline == evidence_deadline
    assert loop.next_due_ts == evidence_deadline
    assert loop.id in service._timers

    await service.record_monitor_completion_evidence_unavailable(
        loop.id,
        "failure-a",
        now=evidence_deadline,
    )

    assert loop.monitor.outcome is MonitorOutcome.USER_STOP
    assert loop.monitor.stopped_reason == "user_stop"
    assert not loop.monitor.wake_in_flight
    assert loop.monitor.completion_evidence_deadline == 0.0
    assert loop.next_due_ts == 0.0
    assert loop.id not in service._accepted_monitor_turns


@pytest.mark.asyncio
async def test_budget_stop_preserves_accepted_completion_deadline(tmp_path) -> None:
    """A terminal budget result cannot orphan an accepted completion claim."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_100.0)
    service.mark_monitor_turn_accepted(loop.id, "failure-a")
    await service.record_monitor_dispatched(loop.id, "failure-a", now=1_101.0)
    assert loop.monitor is not None
    evidence_deadline = loop.monitor.completion_evidence_deadline

    service._apply_monitor_budget_stop(loop, "runtime_budget", stopped_at=1_105.0)

    assert loop.monitor.outcome is MonitorOutcome.BUDGET
    assert loop.monitor.wake_in_flight
    assert loop.monitor.completion_evidence_deadline == evidence_deadline
    assert loop.next_due_ts == evidence_deadline
    service.stop()


@pytest.mark.asyncio
async def test_dispatch_failure_persistence_failure_keeps_live_claim(tmp_path, monkeypatch) -> None:
    """A failed release write cannot expose a claim that restart still considers held."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_100.0)
    before = deepcopy(loop)
    persisted_before = service._path.read_bytes()

    async def fail_snapshot(_payload=None):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", fail_snapshot)

    with pytest.raises(OSError, match="disk full"):
        await service.record_monitor_dispatch_failure(loop.id, "failure-a")

    assert loop == before
    assert service._path.read_bytes() == persisted_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budgets", "completed_ts", "input_tokens", "output_tokens", "reason"),
    [
        (MonitorBudgets(max_runtime_secs=100), 1_100.0, 1, 1, "runtime_budget"),
        (MonitorBudgets(max_agent_turns=1), 1_050.0, 1, 1, "agent_turn_budget"),
        (MonitorBudgets(max_tokens=100), 1_050.0, 75, 25, "token_budget"),
    ],
)
async def test_completion_stops_on_the_first_exhausted_budget(
    tmp_path,
    budgets: MonitorBudgets,
    completed_ts: float,
    input_tokens: int,
    output_tokens: int,
    reason: str,
) -> None:
    """Runtime, then turn, then token precedence gives one stable stop reason."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    assert loop.monitor is not None
    loop.monitor.budgets = budgets
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_050.0)

    await service.record_monitor_turn_completion(
        models.MonitorActionCompletion(
            monitor_id=loop.id,
            fingerprint="failure-a",
            disposition=models.MonitorActionDisposition.FAILURE,
            completed_ts=completed_ts,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )

    assert not loop.active
    assert loop.monitor.outcome is MonitorOutcome.BUDGET
    assert loop.monitor.stopped_reason == reason
    assert loop.monitor.stopped_at == completed_ts


def test_agent_turn_budget_above_universal_bound_is_rejected() -> None:
    """Persisted configuration and enforcement must expose the same ceiling."""
    with pytest.raises(ValueError, match="max_agent_turns"):
        MonitorBudgets(max_agent_turns=9)


@pytest.mark.asyncio
async def test_duplicate_and_mismatched_completions_are_idempotent(tmp_path) -> None:
    """A retry or stale surface callback cannot charge a second turn."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_050.0)
    stale = models.MonitorActionCompletion(
        monitor_id=loop.id,
        fingerprint="failure-b",
        disposition=models.MonitorActionDisposition.FAILURE,
        completed_ts=1_055.0,
        input_tokens=500,
        output_tokens=100,
    )
    completion = models.MonitorActionCompletion(
        monitor_id=loop.id,
        fingerprint="failure-a",
        disposition=models.MonitorActionDisposition.SUCCESS,
        completed_ts=1_060.0,
        input_tokens=10,
        output_tokens=5,
    )

    await service.record_monitor_turn_completion(stale)
    await service.record_monitor_turn_completion(completion)
    await service.record_monitor_turn_completion(completion)

    assert loop.monitor is not None
    assert loop.monitor.agent_turns == 1
    assert loop.monitor.total_tokens == 15
    assert loop.monitor.last_completion_fingerprint == "failure-a"


@pytest.mark.asyncio
async def test_missing_usage_charges_turn_without_inventing_tokens(tmp_path) -> None:
    """Unavailable authoritative counts remain unknown rather than becoming zero."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_050.0)

    await service.record_monitor_turn_completion(
        models.MonitorActionCompletion(
            monitor_id=loop.id,
            fingerprint="failure-a",
            disposition=models.MonitorActionDisposition.CANCELLATION,
            completed_ts=1_060.0,
        )
    )

    assert loop.monitor is not None
    assert loop.monitor.agent_turns == 1
    assert loop.monitor.total_tokens == 0
    assert not loop.monitor.token_usage_known
    assert loop.monitor.last_completion_disposition is models.MonitorActionDisposition.CANCELLATION


@pytest.mark.asyncio
async def test_approval_stall_is_a_completed_turn_and_terminal_blocker(tmp_path) -> None:
    """An unattended approval stall must not wake the same monitor again."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_050.0)

    await service.record_monitor_turn_completion(
        models.MonitorActionCompletion(
            monitor_id=loop.id,
            fingerprint="failure-a",
            disposition=models.MonitorActionDisposition.APPROVAL_STALL,
            completed_ts=1_060.0,
            input_tokens=10,
            output_tokens=5,
        )
    )

    assert loop.monitor is not None
    assert loop.monitor.agent_turns == 1
    assert not loop.active
    assert loop.monitor.outcome is MonitorOutcome.BLOCKED
    assert loop.monitor.stopped_reason == "approval_stall"


@pytest.mark.asyncio
async def test_surface_approval_evidence_overrides_a_nominal_success_frame(tmp_path) -> None:
    """A provider end-turn after approval timeout remains an approval stall."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = _structured_loop()
    service._loops[loop.id] = loop
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=1_050.0)
    loop.approval_stalled = True

    await service.record_monitor_turn_completion(
        models.MonitorActionCompletion(
            monitor_id=loop.id,
            fingerprint="failure-a",
            disposition=models.MonitorActionDisposition.SUCCESS,
            completed_ts=1_060.0,
            input_tokens=10,
            output_tokens=5,
        )
    )

    assert loop.monitor is not None
    assert loop.monitor.last_completion_disposition is (
        models.MonitorActionDisposition.APPROVAL_STALL
    )
    assert loop.monitor.outcome is MonitorOutcome.BLOCKED


def test_restart_fails_closed_when_completion_evidence_is_unavailable(tmp_path) -> None:
    """An acknowledged in-flight fingerprint cannot redispatch after restart."""
    loop = _structured_loop()
    assert loop.monitor is not None
    loop.monitor.last_wake_fingerprint = "failure-a"
    loop.monitor.wake_in_flight = True
    payload = {
        "version": 1,
        "loops": [AutoNudgeService._serialize_loop(loop)],
    }
    (tmp_path / "autonudge.json").write_text(json.dumps(payload), encoding="utf-8")

    restored_service = AutoNudgeService(base_dir=tmp_path)
    restored_service._load()
    restored = restored_service._loops[loop.id]

    assert restored.monitor is not None
    assert not restored.active
    assert not restored.monitor.wake_in_flight
    assert restored.monitor.last_wake_fingerprint == "failure-a"
    assert restored.monitor.outcome is MonitorOutcome.BLOCKED
    assert restored.monitor.stopped_reason == "completion_evidence_unavailable"


@pytest.mark.asyncio
async def test_completion_hook_delivers_one_typed_record_with_explicit_unknown_usage() -> None:
    """A credits-only usage result must not fabricate authoritative token counts."""
    assert importlib.util.find_spec("kiro_crew.monitoring.completion") is not None
    from kiro_crew.monitoring.completion import MonitorCompletionHook

    completions: list[models.MonitorActionCompletion] = []

    async def _capture(completion: models.MonitorActionCompletion) -> None:
        completions.append(completion)

    hook = MonitorCompletionHook("monitor1", "failure-a", _capture)
    await hook.complete(
        models.MonitorActionDisposition.SUCCESS,
        TurnUsage(credits=1.0),
        completed_ts=1_100.0,
    )

    assert completions == [
        models.MonitorActionCompletion(
            monitor_id="monitor1",
            fingerprint="failure-a",
            disposition=models.MonitorActionDisposition.SUCCESS,
            completed_ts=1_100.0,
            input_tokens=None,
            output_tokens=None,
        )
    ]
