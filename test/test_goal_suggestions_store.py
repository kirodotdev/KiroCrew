"""Suggested goals stay durable and inert until a fenced owner Start."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import autonudge
from kiro_crew.autonudge_service import model, mutations, timers
from kiro_crew.goal import GOAL_CONTINUATION_DELAY_SECS, GoalState, continuation_message
from kiro_crew.goal_actions import goal_snapshot

SLOT = "chat-1-123"


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1_000.0)
    fake_time = SimpleNamespace(time=lambda: clock.now, strftime=time.strftime, gmtime=time.gmtime)
    # Bind the readers without changing stdlib time or asyncio's monotonic clock.
    for owner in (autonudge, model, mutations, timers):
        monkeypatch.setattr(owner, "time", fake_time)
    return clock


@pytest.fixture
def stores(tmp_path, monkeypatch, event_loop):
    services = []
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")

    def make(base_dir=None):
        service = autonudge.AutoNudgeService(
            base_dir=base_dir or tmp_path / str(len(services)), on_fire=AsyncMock(return_value=True)
        )
        services.append(service)
        return service

    yield make
    tasks = []
    for service in services:
        tasks.extend(service._timers.values())
        tasks.extend(service._inflight_adds)
        if service._reconciler is not None:
            tasks.append(service._reconciler)
        service.stop()
    if tasks:
        event_loop.run_until_complete(
            asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        )


def goal(status="suggested"):
    return GoalState.from_dict(
        {
            "objective": "Verify keyboard navigation",
            "criteria": ["Arrow keys move focus"],
            "status": status,
            "evidence": ["Keyboard checks passed"] if status == "complete" else [],
        }
    )


async def add_goal(service, status="suggested", **kwargs):
    intent = goal(status)
    return await service.add(
        SLOT,
        continuation_message(intent),
        goal=intent,
        max_cycles=5,
        max_runtime_secs=60,
        **kwargs,
    )


async def stored_rows(service):
    return json.loads(await asyncio.to_thread(service._path.read_text, encoding="utf-8"))["loops"]


@pytest.mark.asyncio
async def test_suggestion_is_durable_without_a_deadline_timer_or_cycle(stores, clock):
    service = stores()
    loop = await add_goal(service)
    service.notify_turn_complete(SLOT)
    clock.now += loop.max_runtime_secs * 2
    assert not autonudge.runtime_budget_exceeded(loop)
    assert loop.goal.status == "suggested"
    assert not loop.active and loop.next_due_ts == 0 and loop.cycle_count == 0
    assert not service._timers
    rejected, _, status = await service.fire_now(loop.id)
    assert rejected is None and status == 409
    service._on_fire.assert_not_awaited()
    row = (await stored_rows(service))[0]
    assert row["goal"]["status"] == "suggested"
    assert not row["active"] and row["next_due_ts"] == 0 and row["cycle_count"] == 0


@pytest.mark.asyncio
async def test_owner_start_anchors_budget_once_and_resume_does_not_renew_it(stores, clock):
    service = stores()
    loop = await add_goal(service)
    clock.now += loop.max_runtime_secs * 3
    started_at = clock.now
    result = await service.update(loop.id, active=True, expected_generation=loop.config_generation)
    assert result is loop and loop.active and loop.goal.status == "working"
    assert loop.created_ts == started_at
    assert loop.next_due_ts == started_at + GOAL_CONTINUATION_DELAY_SECS
    assert loop.id in service._timers
    service.notify_user_input(SLOT)
    row = (await stored_rows(service))[0]
    assert row["created_ts"] == started_at and row["next_due_ts"] == loop.next_due_ts

    # Model a run that has already delivered two cycles; Resume must retain them.
    loop.cycle_count = 2
    clock.now += 10
    assert await service.pause_goal(loop.id)
    budgets = (loop.created_ts, loop.max_runtime_secs, loop.max_cycles, loop.cycle_count)
    clock.now += 10
    await service.update(loop.id, active=True, expected_generation=loop.config_generation)
    service.notify_user_input(SLOT)
    assert (loop.created_ts, loop.max_runtime_secs, loop.max_cycles, loop.cycle_count) == budgets
    assert loop.goal.status == "working"
    assert await service.pause_goal(loop.id)
    clock.now = started_at + loop.max_runtime_secs
    with pytest.raises(autonudge.GoalUpdateConflict, match="reached a limit"):
        await service.update(loop.id, active=True, expected_generation=loop.config_generation)
    assert loop.created_ts == started_at and not loop.active


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["working", "waiting", "paused", "blocked", "needs_input"])
@pytest.mark.parametrize("active", [None, False])
async def test_native_metadata_cannot_promote_an_unarmed_suggestion(stores, status, active):
    service = stores()
    loop = await add_goal(service)
    before = goal_snapshot(loop)
    with pytest.raises(autonudge.GoalUpdateConflict, match="remain unarmed"):
        await service.update(
            loop.id, goal=goal(status), active=active, expected_generation=loop.config_generation
        )
    assert goal_snapshot(loop) == before
    assert not service._timers


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["suggested", "working", "complete", "ended"])
async def test_a_supplied_goal_cannot_authorize_first_activation(stores, status):
    service = stores()
    loop = await add_goal(service)
    before = goal_snapshot(loop)
    with pytest.raises(autonudge.GoalUpdateConflict, match="owner control"):
        await service.update(
            loop.id, goal=goal(status), active=True, expected_generation=loop.config_generation
        )
    assert goal_snapshot(loop) == before
    assert not service._timers


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["suggested", "complete", "ended"])
async def test_native_suggestion_revision_and_completion_do_not_arm(stores, clock, status):
    service = stores()
    loop = await add_goal(service)
    created = loop.created_ts
    clock.now += loop.max_runtime_secs * 2
    revision = goal(status).revised({"progress": "Keyboard behavior checked"})
    await service.update(
        loop.id, goal=revision, active=False, expected_generation=loop.config_generation
    )
    assert loop.goal == revision
    assert not loop.active and not service._timers
    assert loop.next_due_ts == loop.cycle_count == 0
    assert loop.created_ts == created
    assert (await stored_rows(service))[0]["goal"]["status"] == status
    if status != "suggested":
        with pytest.raises(autonudge.GoalUpdateConflict, match="finished goal"):
            await service.update(loop.id, active=True, expected_generation=loop.config_generation)


@pytest.mark.asyncio
@pytest.mark.parametrize("write_fails", [False, True])
async def test_stop_preserves_suggestion_and_refuses_a_stale_start(
    stores, clock, monkeypatch, write_fails
):
    service = stores()
    loop = await add_goal(service)
    observed = loop.config_generation
    clock.now += loop.max_runtime_secs * 2

    def fail_write(_payload):
        raise OSError("full")

    with monkeypatch.context() as failing:
        if write_fails:
            failing.setattr(service, "_write_state", fail_write)
        assert await service.pause_goal(loop.id) is not write_fails
    assert loop.goal.status == "suggested" and not loop.active
    assert loop.config_generation > observed
    with pytest.raises(autonudge.GoalUpdateConflict, match="changed"):
        await service.update(loop.id, active=True, expected_generation=observed)
    assert not service._timers and loop.next_due_ts == 0


@pytest.mark.asyncio
async def test_start_requires_revision_and_retains_generic_edit_guards(stores):
    service = stores()
    loop = await add_goal(service)
    before = goal_snapshot(loop)
    for changes in (
        {"active": True},
        {"max_cycles": loop.max_cycles + 1},
        {"max_runtime_secs": loop.max_runtime_secs + 1},
        {"message": "Replace the proposed outcome"},
    ):
        with pytest.raises(autonudge.GoalUpdateConflict):
            await service.update(loop.id, **changes)
        assert goal_snapshot(loop) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["working", "paused"])
async def test_an_armed_goal_cannot_become_a_fresh_suggestion(stores, status):
    service = stores()
    loop = await add_goal(service, "working")
    service.notify_user_input(SLOT)
    if status == "paused":
        assert await service.pause_goal(loop.id)
    before = goal_snapshot(loop)
    with pytest.raises(autonudge.GoalUpdateConflict, match="cannot become a suggestion"):
        await service.update(loop.id, goal=goal(), active=False)
    assert goal_snapshot(loop) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("incoming", [None, "suggested", "waiting", "working"])
async def test_only_explicit_working_goal_can_replace_a_suggestion(stores, incoming):
    service = stores()
    old = await add_goal(service)
    if incoming == "working":
        new = await add_goal(service, incoming)
        assert new.id in service._timers
        service.notify_user_input(SLOT)
        assert new is not old and new.active and new.goal.status == "working"
        assert service.get_by_id(old.id) is None
    else:
        with pytest.raises(autonudge.MonitorUpdateConflict, match="unfinished goal"):
            if incoming is None:
                await service.add(SLOT, "Watch unrelated work")
            else:
                await add_goal(service, incoming)
        assert service.get_by_slot(SLOT) is old and not old.active


@pytest.mark.asyncio
async def test_suggestions_cannot_be_observation_gated(stores):
    service = stores()
    with pytest.raises(ValueError, match="ungated"):
        await add_goal(service, gate=True)
    assert service.get_by_slot(SLOT) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_active", [False, True])
async def test_reload_keeps_suggestions_inactive_even_with_a_live_deadline(
    stores, clock, stored_active
):
    service = stores()
    loop = await add_goal(service)
    raw = json.loads(await asyncio.to_thread(service._path.read_text, encoding="utf-8"))
    raw["loops"][0].update(active=stored_active, next_due_ts=clock.now + 20)
    await asyncio.to_thread(service._path.write_text, json.dumps(raw), encoding="utf-8")
    restored = stores(service._base_dir)
    await asyncio.wait_for(restored.start(), 10)
    current = restored.get_by_id(loop.id)
    assert current.goal.status == "suggested"
    assert not current.active and current.next_due_ts == current.cycle_count == 0
    assert not restored._timers
    restored._on_fire.assert_not_awaited()
    row = (await stored_rows(restored))[0]
    assert not row["active"] and row["next_due_ts"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "start", "manual-replacement"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_suggestion_transitions_publish_only_after_durable_write(
    stores, clock, monkeypatch, operation, write_fails
):
    service = stores()
    old = None if operation == "create" else await add_goal(service)
    before = goal_snapshot(old) if old is not None else None
    old_created = old.created_ts if old is not None else None
    clock.now += 300
    entered, release = asyncio.Event(), threading.Event()
    running_loop = asyncio.get_running_loop()
    write = service._write_state

    def held_write(payload):
        running_loop.call_soon_threadsafe(entered.set)
        if not release.wait(10):
            raise TimeoutError("test did not release the store write")
        if write_fails:
            raise OSError("full")
        write(payload)

    monkeypatch.setattr(service, "_write_state", held_write)
    task = asyncio.create_task(
        service.update(old.id, active=True, expected_generation=old.config_generation)
        if operation == "start"
        else add_goal(service, "working" if operation == "manual-replacement" else "suggested")
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        visible = service.get_by_slot(SLOT)
        assert visible is old
        assert (goal_snapshot(visible) if visible is not None else None) == before
        assert not service._timers
        if old is not None:
            assert old.created_ts == old_created and old.next_due_ts == old.cycle_count == 0
    finally:
        release.set()
        result = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10))[0]
        service.notify_user_input(SLOT)
    if write_fails:
        assert isinstance(result, OSError)
        assert service.get_by_slot(SLOT) is old
        assert (goal_snapshot(old) if old is not None else None) == before
        if old is not None:
            assert old.created_ts == old_created
            assert (await stored_rows(service))[0]["goal"]["status"] == "suggested"
        else:
            assert not service._path.exists()
    else:
        live = service.get_by_slot(SLOT)
        assert result is live
        assert (live is old) is (operation == "start")
        assert live.active is (operation != "create")
        assert live.goal.status == ("suggested" if operation == "create" else "working")
        row = (await stored_rows(service))[0]
        assert row["active"] == live.active and row["goal"]["status"] == live.goal.status
        if operation != "create":
            assert row["created_ts"] == clock.now
            assert row["next_due_ts"] == clock.now + GOAL_CONTINUATION_DELAY_SECS
