"""AutoNudgeService tests: what arming a loop does to the slot's existing loop or monitor."""

from __future__ import annotations

import asyncio

import pytest
from autonudge_helpers import _enable  # noqa: F401
from autonudge_helpers import svc_fixture  # noqa: F401

from kiro_crew.autonudge import (
    APPROVAL_STALL_REASON,
    AUTONUDGE_STOP_REASON,
    MonitorUpdateConflict,
    NudgeLoop,
)
from kiro_crew.monitoring.models import MONITOR_STATE_VERSION, MonitorBudgets, MonitorOutcome

#: Lost-run ceiling for synchronization the test itself must release. It is a
#: hang guard whose expiry fails at the awaited line, never a performance
#: assertion, and is at most half the repository's ``--timeout=120``.
_LOST_RUN_SECS = 30.0


@pytest.mark.asyncio
async def test_one_loop_per_slot_replaces(svc):
    await svc.start()
    l1 = await svc.add(slot_key="chat-1-123", message="first", idle_secs=15)
    l2 = await svc.add(slot_key="chat-1-123", message="second", idle_secs=15)
    assert l1.id != l2.id
    # Only the second loop should remain.
    all_loops = svc.list_all()
    assert len(all_loops) == 1
    assert all_loops[0].message == "second"


@pytest.mark.asyncio
async def test_add_monitor_refuses_to_replace_an_inflight_wake(svc):
    await svc.start()
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    assert existing.monitor is not None
    existing.monitor.wake_in_flight = True
    existing.monitor.last_wake_fingerprint = "actionable-1"

    with pytest.raises(MonitorUpdateConflict, match="wake is in flight"):
        await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#456",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )

    assert svc.get_by_slot("chat-1-123") is existing
    assert existing.monitor.wake_in_flight
    assert existing.monitor.last_wake_fingerprint == "actionable-1"


@pytest.mark.asyncio
async def test_add_monitor_can_atomically_refuse_an_occupied_slot(svc):
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    persisted_before = svc._path.read_bytes()

    with pytest.raises(MonitorUpdateConflict, match="already has an automation"):
        await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#456",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
            replace_existing=False,
        )

    assert svc.get_by_slot("chat-1-123") is existing
    assert svc._path.read_bytes() == persisted_before


@pytest.mark.asyncio
async def test_add_monitor_conditionally_replaces_one_terminal_generation(svc):
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    assert existing.monitor is not None
    existing.active = False
    existing.monitor.outcome = MonitorOutcome.USER_STOP
    expected_generation = existing.monitor.config_generation

    async def restart() -> NudgeLoop:
        return await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
            expected_existing_monitor_id=existing.id,
            expected_existing_config_generation=expected_generation,
        )

    results = await asyncio.wait_for(
        asyncio.gather(restart(), restart(), return_exceptions=True), timeout=_LOST_RUN_SECS
    )
    winners = [result for result in results if isinstance(result, NudgeLoop)]
    conflicts = [result for result in results if isinstance(result, MonitorUpdateConflict)]

    assert len(winners) == 1
    assert len(conflicts) == 1
    assert "monitor changed before restart" in str(conflicts[0])
    assert svc.get_by_slot("chat-1-123") is winners[0]


@pytest.mark.asyncio
async def test_add_monitor_persistence_failure_keeps_existing_monitor_running(svc, monkeypatch):
    async def on_monitor_tick(_loop):
        return None

    svc._on_monitor_tick = on_monitor_tick
    await svc.start()
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    persisted_before = svc._path.read_bytes()

    async def fail_snapshot(_payload, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", fail_snapshot)

    with pytest.raises(OSError, match="disk full"):
        await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#456",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )

    assert svc.get_by_slot("chat-1-123") is existing
    assert existing.id in svc._timers
    assert not svc._timers[existing.id].done()
    assert svc._path.read_bytes() == persisted_before


@pytest.mark.asyncio
async def test_add_legacy_loop_refuses_to_replace_an_inflight_monitor(svc):
    await svc.start()
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    assert existing.monitor is not None
    existing.monitor.wake_in_flight = True
    existing.monitor.last_wake_fingerprint = "actionable-1"

    with pytest.raises(MonitorUpdateConflict, match="wake is in flight"):
        await svc.add(slot_key="chat-1-123", message="legacy replacement", idle_secs=60)

    assert svc.get_by_slot("chat-1-123") is existing
    assert existing.monitor.wake_in_flight
    assert existing.monitor.last_wake_fingerprint == "actionable-1"


@pytest.mark.asyncio
async def test_add_legacy_loop_create_only_preserves_an_existing_monitor(svc):
    """A browser legacy create racing a monitor arm cannot replace the winner."""
    await svc.start()
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    persisted_before = svc._path.read_bytes()

    with pytest.raises(MonitorUpdateConflict, match="session already has an automation"):
        await svc.add(
            slot_key="chat-1-123",
            message="legacy replacement",
            idle_secs=60,
            replace_existing=False,
        )

    assert svc.get_by_slot("chat-1-123") is existing
    assert svc._path.read_bytes() == persisted_before


@pytest.mark.asyncio
async def test_create_only_add_replaces_an_inactive_approval_stalled_loop(svc):
    """The approval-stall deadlock: ``monitor_update`` refuses to revive an
    approval-stalled loop and names ``monitor_start`` as the remedy, so the
    directive re-arm (``replace_stopped=True``) must not read that retained
    INACTIVE row as an occupying automation. Observed live: a babysit re-arm
    bounced off its own predecessor's approval-stall tombstone with "session
    already has an automation", leaving the session with no working re-arm
    path at all."""
    await svc.start()
    stalled = await svc.add(slot_key="chat-1-123", message="old babysit", idle_secs=60)
    await svc.update(stalled.id, active=False, stopped_reason=APPROVAL_STALL_REASON)

    fresh = await svc.add(
        slot_key="chat-1-123",
        message="new babysit",
        idle_secs=60,
        replace_existing=False,
        replace_stopped=True,
    )

    assert fresh.id != stalled.id
    assert svc.get_by_slot("chat-1-123") is fresh
    # The tombstone is gone, not merely shadowed: exactly one loop remains.
    assert [lp.id for lp in svc.list_all()] == [fresh.id]
    svc.stop()


@pytest.mark.asyncio
async def test_create_only_add_still_refuses_an_active_legacy_loop(svc):
    """``replace_stopped`` must not widen create-only into replace: a LIVE
    legacy loop still refuses a second arm even on the directive path."""
    await svc.start()
    live = await svc.add(slot_key="chat-1-123", message="live babysit", idle_secs=60)

    with pytest.raises(MonitorUpdateConflict, match="session already has an automation"):
        await svc.add(
            slot_key="chat-1-123",
            message="usurper",
            idle_secs=60,
            replace_existing=False,
            replace_stopped=True,
        )

    assert svc.get_by_slot("chat-1-123") is live
    svc.stop()


@pytest.mark.asyncio
async def test_dashboard_create_only_add_preserves_a_stopped_row(svc):
    """Dashboard REST creates pass ``replace_existing=False`` WITHOUT the
    directive opt-in, and their documented contract is any-record 409: a
    retained stopped row must survive byte-identically, never be silently
    deleted by a create (GPT security finding on the first cut of this fix)."""
    await svc.start()
    stalled = await svc.add(slot_key="chat-1-123", message="old babysit", idle_secs=60)
    await svc.update(stalled.id, active=False, stopped_reason=APPROVAL_STALL_REASON)
    persisted_before = svc._path.read_bytes()

    with pytest.raises(MonitorUpdateConflict, match="session already has an automation"):
        await svc.add(
            slot_key="chat-1-123",
            message="dashboard create",
            idle_secs=60,
            replace_existing=False,
        )

    assert svc.get_by_slot("chat-1-123") is stalled
    assert svc._path.read_bytes() == persisted_before
    svc.stop()


@pytest.mark.asyncio
async def test_replace_stopped_never_deletes_a_future_version_monitor(svc):
    """A future-version record belongs to the newer gateway that wrote it:
    ``_load()`` retains it inactive across a downgrade so an upgrade can resume
    the watch. The directive re-arm must refuse it — deleting opaque state this
    gateway cannot read is data loss, not a re-arm."""
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    assert existing.monitor is not None
    existing.active = False
    existing.monitor.version = MONITOR_STATE_VERSION + 1

    with pytest.raises(MonitorUpdateConflict, match="written by a newer gateway"):
        await svc.add(
            slot_key="chat-1-123",
            message="directive re-arm",
            idle_secs=60,
            replace_existing=False,
            replace_stopped=True,
        )
    with pytest.raises(MonitorUpdateConflict, match="written by a newer gateway"):
        await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#456",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
            replace_existing=False,
            replace_stopped=True,
        )

    assert svc.get_by_slot("chat-1-123") is existing
    assert existing.monitor.version == MONITOR_STATE_VERSION + 1


@pytest.mark.asyncio
async def test_dashboard_create_only_add_monitor_preserves_a_terminal_record(svc):
    """Structured twin of the preservation pin: a terminal monitor retained
    for inspection survives a dashboard create-only arm untouched."""
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    stopped = await svc.stop_monitor(existing.id)
    assert stopped is not None
    persisted_before = svc._path.read_bytes()

    with pytest.raises(MonitorUpdateConflict, match="session already has an automation"):
        await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#456",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
            replace_existing=False,
        )

    assert svc.get_by_slot("chat-1-123") is existing
    assert svc._path.read_bytes() == persisted_before


@pytest.mark.asyncio
async def test_create_only_add_monitor_replaces_a_merged_subject_monitor(svc):
    """A subject-terminal stop is system-imposed (the watched pull request
    merged; there is nothing left to observe), so the directive re-arm may
    displace the record and start a watch on a NEW subject."""
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    assert existing.monitor is not None
    existing.active = False
    existing.monitor.outcome = MonitorOutcome.SUCCESS
    existing.monitor.stopped_reason = "merged"

    fresh = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#456",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        replace_existing=False,
        replace_stopped=True,
    )

    assert fresh.id != existing.id
    assert svc.get_by_slot("chat-1-123") is fresh
    assert [lp.id for lp in svc.list_all()] == [fresh.id]


@pytest.mark.asyncio
async def test_replace_stopped_preserves_a_quarantined_monitor_record(svc):
    """``_load()`` quarantines a malformed monitor payload as BLOCKED precisely
    to retain the raw record for inspection — a directive re-arm deleting it
    would destroy the only copy of the corrupt state."""
    from kiro_crew.monitoring.models import MONITOR_STOP_INVALID_RECORD

    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    assert existing.monitor is not None
    existing.active = False
    existing.monitor.outcome = MonitorOutcome.BLOCKED
    existing.monitor.stopped_reason = MONITOR_STOP_INVALID_RECORD

    with pytest.raises(MonitorUpdateConflict, match="retained as evidence"):
        await svc.add(
            slot_key="chat-1-123",
            message="directive re-arm",
            idle_secs=60,
            replace_existing=False,
            replace_stopped=True,
        )

    assert svc.get_by_slot("chat-1-123") is existing


@pytest.mark.asyncio
async def test_create_only_add_replaces_a_target_unavailable_monitor(svc):
    """``TARGET_UNAVAILABLE`` is system-imposed (a vanished or undeliverable
    subject) — no consumer authored it, so the directive re-arm must displace
    it rather than re-create the deadlock (Opus advisory on the ruling)."""
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    assert existing.monitor is not None
    existing.active = False
    existing.monitor.outcome = MonitorOutcome.TARGET_UNAVAILABLE
    existing.monitor.stopped_reason = "session_unavailable"

    fresh = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#456",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        replace_existing=False,
        replace_stopped=True,
    )

    assert fresh.id != existing.id
    assert svc.get_by_slot("chat-1-123") is fresh


@pytest.mark.asyncio
async def test_replace_stopped_preserves_a_user_stopped_monitor(svc):
    """Owner ruling (option A): a USER_STOP record is a consumer-recorded stop
    — retained for inspection — so even the directive re-arm refuses it. The
    dashboard restart route (conditional replace) is the sanctioned path."""
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    stopped = await svc.stop_monitor(existing.id)
    assert stopped is not None
    assert stopped.monitor is not None
    assert stopped.monitor.outcome is MonitorOutcome.USER_STOP

    with pytest.raises(MonitorUpdateConflict, match="retained as evidence"):
        await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#456",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
            replace_existing=False,
            replace_stopped=True,
        )

    assert svc.get_by_slot("chat-1-123") is existing


@pytest.mark.asyncio
async def test_replace_stopped_preserves_a_research_tombstone(svc):
    """The auto_research watchdog reads a retained ``AUTONUDGE_STOP_REASON``
    row to tell deliberate completion from crash cleanup; a directive re-arm
    deleting it would leave the campaign running."""
    await svc.start()
    worker = await svc.add(slot_key="chat-1-123", message="research worker", idle_secs=60)
    await svc.update(worker.id, active=False, stopped_reason=AUTONUDGE_STOP_REASON)

    with pytest.raises(MonitorUpdateConflict, match="retained as evidence"):
        await svc.add(
            slot_key="chat-1-123",
            message="directive re-arm",
            idle_secs=60,
            replace_existing=False,
            replace_stopped=True,
        )

    survivor = svc.get_by_slot("chat-1-123")
    assert survivor is not None and survivor.id == worker.id
    assert survivor.stopped_reason == AUTONUDGE_STOP_REASON
    svc.stop()


@pytest.mark.asyncio
async def test_replace_stopped_preserves_a_manual_pause(svc):
    """A manually paused loop (empty stop reason) is the user's decision, not a
    system-imposed stop — the re-arm must not silently discard its instruction."""
    await svc.start()
    paused = await svc.add(slot_key="chat-1-123", message="paused by hand", idle_secs=60)
    await svc.update(paused.id, active=False)

    with pytest.raises(MonitorUpdateConflict, match="retained as evidence"):
        await svc.add(
            slot_key="chat-1-123",
            message="directive re-arm",
            idle_secs=60,
            replace_existing=False,
            replace_stopped=True,
        )

    survivor = svc.get_by_slot("chat-1-123")
    assert survivor is not None and survivor.id == paused.id
    svc.stop()


@pytest.mark.asyncio
async def test_create_only_add_still_refuses_a_terminal_row_with_an_inflight_wake(svc):
    """A terminal record whose accepted wake still awaits completion evidence
    owns a live correlation; replacing it would orphan the claim. The inactive
    path must fall through to the wake-in-flight refusal, never proceed."""
    existing = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    assert existing.monitor is not None
    existing.active = False
    existing.monitor.outcome = MonitorOutcome.BUDGET
    existing.monitor.wake_in_flight = True
    existing.monitor.last_wake_fingerprint = "actionable-1"
    existing.monitor.completion_evidence_deadline = 2_000_000.0

    with pytest.raises(MonitorUpdateConflict, match="wake is in flight"):
        await svc.add(
            slot_key="chat-1-123",
            message="legacy replacement",
            idle_secs=60,
            replace_existing=False,
            replace_stopped=True,
        )

    assert svc.get_by_slot("chat-1-123") is existing
    assert existing.monitor.wake_in_flight
