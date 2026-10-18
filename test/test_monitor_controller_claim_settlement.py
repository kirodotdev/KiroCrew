"""Tests for how a monitor's outcome and its in-flight wake claim settle.

An explicit stop records ``USER_STOP`` and never rewrites an earlier terminal outcome,
and it clears a busy or restart-recovered claim so a replacement monitor can take the
slot. A failed session close restores an accepted claim as a busy retry, an untyped
dispatch result fails closed without orphaning the claim, and a cancelled dispatch
keeps the claim for a busy retry before the cancellation propagates. Claim admission
and expiry, busy-delivery retries, budget stops and persistence-failure rollback live
in ``test_monitor_controller.py``. The shared ``_Provider`` fake and the ``_armed`` and
``_result`` builders live in ``monitor_controller_helpers.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from monitor_controller_helpers import (
    _armed,
    _Provider,
    _result,
)

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.monitoring.controller import MonitorController
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorDecision,
    MonitorDispatchResult,
    MonitorObservationStatus,
    MonitorOutcome,
)


@pytest.mark.asyncio
async def test_explicit_stop_retains_structured_outcome(tmp_path):
    service, loop, _controller = await _armed(
        tmp_path,
        result=_result(MonitorObservationStatus.PENDING),
        dispatch=lambda _loop, _envelope: True,
    )

    stopped = await service.stop_monitor(loop.id, now=125.0)

    assert stopped is loop
    assert not loop.active
    assert loop.monitor is not None
    assert loop.monitor.outcome is MonitorOutcome.USER_STOP
    assert service.get_by_slot("chat-1") is loop


@pytest.mark.asyncio
async def test_stop_does_not_rewrite_an_existing_terminal_outcome(tmp_path):
    service, loop, controller = await _armed(
        tmp_path,
        result=_result(MonitorObservationStatus.SUCCESS),
        dispatch=AsyncMock(),
    )
    await controller.tick(loop, now=120.0)

    await service.stop_monitor(loop.id, now=125.0)

    assert loop.monitor is not None
    assert loop.monitor.outcome is MonitorOutcome.SUCCESS


@pytest.mark.asyncio
async def test_restored_accepted_claim_without_delivery_retries_as_busy(tmp_path):
    dispatched = AsyncMock(return_value=MonitorDispatchResult.DISPATCHED)
    service, loop, controller = await _armed(
        tmp_path,
        result=_result(MonitorObservationStatus.PENDING),
        dispatch=dispatched,
    )
    assert await service.mark_monitor_action_in_flight(loop.id, "fp-1", now=105.0)
    service.mark_monitor_turn_accepted(loop.id, "fp-1")
    await service.retire_monitor_for_session_close(loop.id, now=110.0)

    await service.restore_monitor_after_failed_session_close(loop.id, now=120.0)

    assert loop.monitor is not None
    assert loop.monitor.wake_delivery is MonitorDispatchResult.BUSY
    retry_at = loop.monitor.next_probe_at
    assert (await controller.tick(loop, now=retry_at)).decision is MonitorDecision.WAKE_ACTIONABLE
    dispatched.assert_awaited_once()
    service.stop()


@pytest.mark.asyncio
async def test_untyped_dispatch_result_fails_closed_without_orphaning_claim(tmp_path):
    service, loop, controller = await _armed(
        tmp_path,
        result=_result(MonitorObservationStatus.ACTIONABLE),
        dispatch=AsyncMock(return_value=True),
    )

    decision = await controller.tick(loop, now=120.0)

    assert decision.decision is MonitorDecision.WAKE_ACTIONABLE
    assert loop.monitor is not None
    assert loop.monitor.outcome is MonitorOutcome.TARGET_UNAVAILABLE
    assert not loop.monitor.wake_in_flight
    service.stop()


@pytest.mark.asyncio
async def test_cancelled_dispatch_restores_busy_retry_before_propagating(tmp_path):
    service, loop, controller = await _armed(
        tmp_path,
        result=_result(MonitorObservationStatus.ACTIONABLE),
        dispatch=AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await controller.tick(loop, now=120.0)

    assert loop.monitor is not None
    assert loop.monitor.wake_in_flight
    assert loop.monitor.wake_delivery is MonitorDispatchResult.BUSY
    assert loop.monitor.next_probe_at > 120.0
    service.stop()


@pytest.mark.asyncio
async def test_busy_stop_clears_claim_and_allows_a_fresh_monitor(
    tmp_path,
):
    provider = _Provider(_result(MonitorObservationStatus.ACTIONABLE, "fp-2"))
    dispatched = AsyncMock()
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        now=100.0,
    )
    assert await service.mark_monitor_action_in_flight(loop.id, "fp-1", now=120.0)
    await service.record_monitor_dispatch_busy(loop.id, "fp-1", now=120.0)

    await service.stop_monitor(loop.id, now=125.0)

    assert loop.monitor is not None
    assert loop.monitor.outcome is MonitorOutcome.USER_STOP
    assert not loop.monitor.wake_in_flight
    assert loop.monitor.wake_delivery is None
    assert loop.monitor.agent_turns == 0
    assert loop.next_due_ts == loop.monitor.next_probe_at == 0.0

    decision = await MonitorController(
        service, dispatched, providers={"github_pull_request": provider}
    ).tick(loop, now=180.0)

    assert decision.decision is MonitorDecision.STOP_BLOCKED
    assert provider.previous == []
    dispatched.assert_not_awaited()
    assert loop.monitor.agent_turns == 0

    replacement = await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/8",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        now=181.0,
    )
    assert replacement.id != loop.id
    assert service.get_by_slot("chat-1") is replacement
    service.stop()


@pytest.mark.asyncio
async def test_stop_clears_recovered_dispatched_claim_and_allows_replacement(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        now=100.0,
    )
    assert await service.mark_monitor_action_in_flight(loop.id, "fp-1", now=120.0)
    service.mark_monitor_turn_accepted(loop.id, "fp-1")
    await service.record_monitor_dispatched(loop.id, "fp-1", now=120.0)
    service.stop()

    restarted = AutoNudgeService(base_dir=tmp_path)
    restarted._load()
    restored = restarted.get_by_slot("chat-1")
    assert restored is not None and restored.monitor is not None
    assert restored.monitor.wake_in_flight
    assert restored.monitor.wake_delivery is MonitorDispatchResult.DISPATCHED

    await restarted.stop_monitor(restored.id, now=125.0)

    assert restored.monitor.outcome is MonitorOutcome.USER_STOP
    assert not restored.monitor.wake_in_flight
    assert restored.monitor.wake_delivery is None
    replacement = await restarted.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/8",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        now=126.0,
    )
    assert replacement.id != restored.id
    assert restarted.get_by_slot("chat-1") is replacement
    restarted.stop()
