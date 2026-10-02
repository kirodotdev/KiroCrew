"""Scheduled messages are one-shot AutoNudge records.

The tests pin the distinctions that ``max_cycles=1`` alone does not provide:
an absolute deadline may be more than one recurring interval away, a successful
turn frees the session's single automation slot, and restart recovery replays a
dispatched-but-unfinished turn while cleaning a durably completed record without
sending it twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import autonudge_selfarm
from kiro_crew.autonudge import (
    _OVERDUE_REARM_SECS,
    AutoNudgeService,
    MonitorUpdateConflict,
    NudgeLoop,
    scheduled_message_trust_id,
)
from kiro_crew.autonudge_service.maintenance import _maintenance_lock
from kiro_crew.autonudge_service.model import (
    _SCHEDULED_MESSAGE_MAX_ATTEMPTS,
    _START_FAILURE_BACKOFF_AFTER,
    _START_FAILURE_STANDDOWN_AFTER,
)
from kiro_crew.testing.wait import async_wait_until, until_parked

# Split so this file's own source carries no contiguous AWS key-ID literal.
_FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.fixture(autouse=True)
def _clear_process_memory(_floor_monkeypatch):
    autonudge_selfarm._reset_scheduled_messages_for_tests()
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
    yield
    autonudge_selfarm._reset_scheduled_messages_for_tests()


def _scheduled_store_row(loop_id: str, scheduled_at: float) -> dict:
    return {
        "id": loop_id,
        "slot_key": "chat-1-123",
        "message": "",
        "idle_secs": 15,
        "max_cycles": 1,
        "cycle_count": 0,
        "active": True,
        "scheduled_message": True,
        "scheduled_at": scheduled_at,
        "next_due_ts": scheduled_at,
    }


@pytest.mark.asyncio
async def test_process_memory_provenance_survives_service_load(tmp_path):
    due = time.time() + 600
    record_id = scheduled_message_trust_id("same-boot")
    autonudge_selfarm.record_scheduled_message(
        record_id, "chat-1-123", "trusted same-process text", due
    )
    (tmp_path / "autonudge.json").write_text(
        json.dumps({"version": 1, "loops": [_scheduled_store_row("same-boot", due)]}),
        encoding="utf-8",
    )
    service = AutoNudgeService(base_dir=tmp_path)

    await service.start()
    try:
        recovered = service.get_by_id("same-boot")
        assert recovered is not None
        assert recovered.message == ""
        assert recovered.scheduled_at == due
        assert autonudge_selfarm.read_scheduled_message(record_id, recovered.slot_key) == (
            autonudge_selfarm.ScheduledMessageProvenance(
                "chat-1-123", "trusted same-process text", due
            )
        )
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_caller_cannot_mutate_stored_containment_authority(tmp_path):
    record_id = scheduled_message_trust_id("immutable-containment")
    due = time.time() + 600
    admission = {
        "queued_containment": {
            "mirrored": False,
            "mirror_identity": "",
            "nested": {"values": ["original"]},
        }
    }
    autonudge_selfarm.record_scheduled_message(
        record_id,
        "chat-1-123",
        "trusted text",
        due,
        containment_meta=admission,
    )

    first = autonudge_selfarm.read_scheduled_message(record_id, "chat-1-123")
    assert first is not None and first.containment_meta is not None
    first.containment_meta["queued_containment"]["mirrored"] = True
    first.containment_meta["queued_containment"]["nested"]["values"].append("mutated")

    stored = autonudge_selfarm.read_scheduled_message(record_id, "chat-1-123")
    assert stored is not None
    assert stored.containment_meta == admission
    assert autonudge_selfarm.replace_scheduled_message(
        record_id,
        stored,
        "updated text",
        due + 60,
    )
    replaced = autonudge_selfarm.read_scheduled_message(record_id, "chat-1-123")
    assert replaced is not None
    assert replaced.containment_meta == admission


@pytest.mark.asyncio
async def test_scheduled_text_is_never_written_under_data_home(tmp_path) -> None:
    exact = "private deferred text that must remain process-local"
    due = time.time() + 600
    service = AutoNudgeService(base_dir=tmp_path)
    service._arm_timer = MagicMock()  # type: ignore[method-assign]
    loop = await service.add(
        slot_key="chat-1-123",
        message=exact,
        scheduled_at=due,
        loop_id="memory-only",
    )
    autonudge_selfarm.record_scheduled_message(
        scheduled_message_trust_id(loop.id), loop.slot_key, exact, due
    )

    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert files
    assert all(exact.encode() not in path.read_bytes() for path in files)
    stored = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
    assert stored["loops"][0]["message"] == ""


@pytest.mark.asyncio
async def test_new_process_memory_drops_stale_scheduled_metadata(tmp_path):
    due = time.time() + 600
    record_id = scheduled_message_trust_id("stale-after-restart")
    autonudge_selfarm.record_scheduled_message(
        record_id, "chat-1-123", "does not survive restart", due
    )
    (tmp_path / "autonudge.json").write_text(
        json.dumps({"version": 1, "loops": [_scheduled_store_row("stale-after-restart", due)]}),
        encoding="utf-8",
    )
    autonudge_selfarm._reset_scheduled_messages_for_tests()

    service = AutoNudgeService(base_dir=tmp_path)
    await service.start()
    try:
        assert service.get_by_id("stale-after-restart") is None
        assert autonudge_selfarm.read_scheduled_message(record_id, "chat-1-123") is None
        assert json.loads((tmp_path / "autonudge.json").read_text())["loops"] == []
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_restart_drop_warns_with_identity_only(tmp_path, caplog):
    """The drop must be operator-visible, and must leak nothing.

    The spec already requires it ("load drops every persisted scheduled metadata
    row lacking memory state and records the restart-cancellation warning") --
    without the line a Send-later message vanished across a restart with nothing
    on any surface saying why. Only the two ADDRESSING_FIELDS may appear: the
    composer text lives in the provenance that is gone, and the mutable row's own
    ``message`` is agent-writable.
    """
    due = time.time() + 600
    secret = "the exact composer bytes nobody may log"
    row = _scheduled_store_row("warns-on-restart", due)
    row["message"] = secret
    (tmp_path / "autonudge.json").write_text(
        json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
    )
    autonudge_selfarm._reset_scheduled_messages_for_tests()

    service = AutoNudgeService(base_dir=tmp_path)
    with caplog.at_level(logging.WARNING):
        await service.start()
    try:
        assert service.get_by_id("warns-on-restart") is None
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        dropped = [m for m in warnings if "warns-on-restart" in m]
        assert len(dropped) == 1, warnings
        line = dropped[0]
        assert "chat-1-123" in line
        assert "restart" in line
        # No content, from either source.
        assert secret not in caplog.text
        assert "does not survive" not in caplog.text
    finally:
        service.stop()


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    return AutoNudgeService(base_dir=tmp_path)


def _protect(loop: NudgeLoop, message: str) -> None:
    autonudge_selfarm.record_scheduled_message(
        scheduled_message_trust_id(loop.id),
        loop.slot_key,
        message,
        loop.scheduled_at,
    )


def _capture_arms(svc: AutoNudgeService) -> list[float | None]:
    arms: list[float | None] = []

    def capture(_loop: NudgeLoop, delay: float | None = None) -> None:
        arms.append(delay)

    svc._arm_timer = capture  # type: ignore[method-assign]
    return arms


def _bind_current_scheduled_turn(svc: AutoNudgeService, loop_id: str) -> asyncio.Task:
    turn_task = asyncio.current_task()
    assert turn_task is not None
    svc.note_scheduled_delivery_dispatched(loop_id)
    svc.bind_scheduled_delivery_task(loop_id, turn_task)
    return turn_task


@pytest.mark.asyncio
async def test_scheduled_add_persists_exact_deadline_and_one_cycle(svc, tmp_path):
    arms = _capture_arms(svc)
    due = time.time() + 2 * 86400

    loop = await svc.add(
        slot_key="chat-1-123",
        message="send the release update",
        idle_secs=15,
        max_cycles=99,
        scheduled_at=due,
    )

    assert loop.scheduled_at == due
    assert loop.next_due_ts == due
    assert loop.max_cycles == 1
    assert arms[-1] == 3600
    raw = json.loads((tmp_path / "autonudge.json").read_text())
    assert raw["loops"][0]["scheduled_at"] == due
    assert raw["loops"][0]["next_due_ts"] == due
    assert raw["loops"][0]["max_cycles"] == 1


@pytest.mark.asyncio
async def test_scheduled_add_clears_supplied_stop_sentinel(svc, tmp_path):
    _capture_arms(svc)
    due = time.time() + 600

    loop = await svc.add(
        slot_key="chat-1-123",
        message="send later",
        scheduled_at=due,
        stop_sentinel_path=str(tmp_path / "predictable-stop"),
    )

    assert loop.stop_sentinel_path == ""
    stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
    assert stored["stop_sentinel_path"] == ""


@pytest.mark.asyncio
async def test_present_legacy_sentinel_cannot_block_scheduled_dispatch(svc, tmp_path):
    _capture_arms(svc)
    sentinel = tmp_path / "predictable-stop"
    sentinel.write_text("stop")
    loop = await svc.add(
        slot_key="chat-1-123",
        message="send later",
        scheduled_at=time.time() - 1,
    )
    _protect(loop, "send later")
    loop.stop_sentinel_path = str(sentinel)  # Simulate a loaded legacy row.
    fired: list[str] = []

    async def on_fire(candidate):
        fired.append(candidate.id)
        return True

    svc._on_fire = on_fire
    await svc._timer(loop, delay=0.0)

    assert fired == [loop.id]
    assert loop.cycle_count == 1


@pytest.mark.asyncio
async def test_present_sentinel_still_stops_an_ordinary_loop(svc, tmp_path):
    _capture_arms(svc)
    sentinel = tmp_path / "ordinary-stop"
    sentinel.write_text("stop")
    loop = await svc.add(
        slot_key="chat-1-123",
        message="keep working",
        stop_sentinel_path=str(sentinel),
    )

    await svc._timer(loop, delay=0.0)

    # An ordinary loop's finished goal is KEPT, inactive, under its own reason.
    stopped = svc.get_by_id(loop.id)
    assert stopped is not None
    assert stopped.active is False
    assert stopped.stopped_reason == "stop_sentinel"


@pytest.mark.asyncio
async def test_scheduled_timer_rechecks_wall_clock_before_firing(svc, monkeypatch):
    """A backward clock jump must not make an absolute schedule fire early."""
    due = 2_000.0
    loop = NudgeLoop(
        id="scheduled",
        slot_key="chat-1-123",
        message="later",
        scheduled_message=True,
        scheduled_at=due,
        next_due_ts=due,
        max_cycles=1,
    )
    svc._loops[loop.id] = loop
    arms = _capture_arms(svc)
    fired: list[str] = []

    async def no_sleep(_delay):
        return None

    async def on_fire(_loop):
        fired.append(_loop.id)
        return True

    monkeypatch.setattr("kiro_crew.autonudge.asyncio.sleep", no_sleep)
    monkeypatch.setattr("kiro_crew.autonudge.time.time", lambda: 1_000.0)
    svc._on_fire = on_fire

    await svc._timer(loop, delay=0.0)

    assert fired == []
    assert arms == [1_000.0]


@pytest.mark.asyncio
async def test_scheduled_sleep_uses_a_wall_clock_beat(svc, monkeypatch):
    due = 10_000.0
    loop = NudgeLoop(
        id="scheduled",
        slot_key="chat-1-123",
        message="later",
        scheduled_message=True,
        scheduled_at=due,
        next_due_ts=due,
        max_cycles=1,
    )
    arms = _capture_arms(svc)
    monkeypatch.setattr("kiro_crew.autonudge.time.time", lambda: 1_000.0)

    svc._arm_from_deadline(loop)

    assert arms == [3600]


@pytest.mark.asyncio
async def test_scheduled_record_is_removed_after_its_turn_completes(svc, tmp_path):
    _capture_arms(svc)
    due = time.time() + 60
    loop = await svc.add(
        slot_key="chat-1-123",
        message="one turn",
        scheduled_at=due,
    )
    _protect(loop, "one turn")

    async def delivered(_loop):
        return True

    svc._on_fire = delivered
    await svc._run_fire_cycle(loop)
    assert loop.cycle_count == 1
    assert svc.get_by_slot(loop.slot_key) is loop, "exclusivity ended before the turn completed"

    _bind_current_scheduled_turn(svc, loop.id)
    svc.notify_turn_complete(loop.slot_key, turn_completed=True)
    await asyncio.gather(*tuple(svc._inflight_adds))

    assert svc.get_by_slot(loop.slot_key) is None
    raw = json.loads((tmp_path / "autonudge.json").read_text())
    assert raw["loops"] == []


@pytest.mark.asyncio
async def test_restart_removes_an_already_delivered_schedule_without_refiring(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    due = time.time() - 5
    trust_id = scheduled_message_trust_id("scheduled")
    autonudge_selfarm.record_scheduled_message(trust_id, "chat-1-123", "already sent", due)
    pending = autonudge_selfarm.read_scheduled_message(trust_id, "chat-1-123")
    assert pending is not None
    assert autonudge_selfarm.mark_scheduled_message_completed(trust_id, pending)
    (tmp_path / "autonudge.json").write_text(
        json.dumps(
            {
                "version": 1,
                "loops": [
                    {
                        "id": "scheduled",
                        "slot_key": "chat-1-123",
                        "message": "already sent",
                        "idle_secs": 15,
                        "max_cycles": 1,
                        "cycle_count": 1,
                        "active": True,
                        "scheduled_message": True,
                        "scheduled_at": due,
                        "scheduled_completed": True,
                        "next_due_ts": 0.0,
                    }
                ],
            }
        )
    )
    svc = AutoNudgeService(base_dir=tmp_path)
    arms = _capture_arms(svc)

    await svc.start()

    # A delivered row is armed for cleanup on the overdue beat, never at zero:
    # delay zero is the hot-loop cadence when the cleanup write keeps failing.
    assert arms == [float(_OVERDUE_REARM_SECS)]
    assert svc.get_by_slot("chat-1-123") is not None
    await svc._timer(svc.get_by_slot("chat-1-123"), delay=0.0)  # type: ignore[arg-type]
    assert svc.get_by_slot("chat-1-123") is None


@pytest.mark.asyncio
async def test_user_input_rearms_an_undispatched_scheduled_message(svc):
    arms = _capture_arms(svc)
    due = time.time() + 600
    loop = await svc.add(slot_key="chat-1-123", message="later", scheduled_at=due)
    arms.clear()

    svc.notify_user_input(loop.slot_key)
    svc.notify_turn_complete(loop.slot_key)

    assert loop.cycle_count == 0
    assert arms[-1] == pytest.approx(due - time.time(), abs=1)


@pytest.mark.asyncio
async def test_direct_add_cannot_replace_a_protected_scheduled_message(svc):
    _capture_arms(svc)
    scheduled = await svc.add(
        slot_key="chat-1-123",
        message="send later",
        scheduled_at=time.time() + 600,
    )

    with pytest.raises(MonitorUpdateConflict, match="authenticated dashboard user"):
        await svc.add(slot_key=scheduled.slot_key, message="agent replacement")

    assert svc.get_by_slot(scheduled.slot_key) is scheduled


@pytest.mark.asyncio
async def test_same_generation_reload_replays_a_dispatched_but_unfinished_schedule(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    due = time.time() - 5
    autonudge_selfarm.record_scheduled_message(
        scheduled_message_trust_id("scheduled"),
        "chat-1-123",
        "try this turn again",
        due,
    )
    (tmp_path / "autonudge.json").write_text(
        json.dumps(
            {
                "version": 1,
                "loops": [
                    {
                        "id": "scheduled",
                        "slot_key": "chat-1-123",
                        "message": "try this turn again",
                        "idle_secs": 15,
                        "max_cycles": 1,
                        "cycle_count": 1,
                        "active": True,
                        "scheduled_message": True,
                        "scheduled_at": due,
                        "scheduled_completed": False,
                        "next_due_ts": 0.0,
                    }
                ],
            }
        )
    )
    service = AutoNudgeService(base_dir=tmp_path)
    arms = _capture_arms(service)

    await service.start()

    recovered = service.get_by_slot("chat-1-123")
    assert recovered is not None
    assert recovered.cycle_count == 0
    assert recovered.scheduled_completed is False
    assert recovered.next_due_ts == due
    assert arms == [10.0]
    stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
    assert stored["cycle_count"] == 0
    assert stored["scheduled_completed"] is False


@pytest.mark.asyncio
async def test_interrupted_scheduled_turn_is_persisted_and_rearmed(svc, tmp_path):
    arms = _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123",
        message="finish me",
        scheduled_at=time.time() + 60,
    )

    async def delivered(_loop):
        return True

    svc._on_fire = delivered
    await svc._run_fire_cycle(loop)
    _bind_current_scheduled_turn(svc, loop.id)
    svc.notify_turn_complete(loop.slot_key, turn_completed=False)
    await asyncio.gather(*tuple(svc._inflight_adds))

    assert loop.cycle_count == 0
    assert loop.scheduled_completed is False
    assert loop.next_due_ts == loop.scheduled_at
    assert arms[-1] == pytest.approx(loop.scheduled_at - time.time(), abs=1)
    stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
    assert stored["cycle_count"] == 0
    assert stored["scheduled_completed"] is False


@pytest.mark.asyncio
async def test_direct_service_call_refuses_a_channel_schedule(svc):
    _capture_arms(svc)
    with pytest.raises(ValueError, match="dashboard session"):
        await svc.add(
            slot_key="slack:C123:1.0",
            message="later",
            scheduled_at=time.time() + 60,
        )


@pytest.mark.asyncio
async def test_schedule_and_goal_share_the_same_slot_conflict(svc):
    _capture_arms(svc)
    await svc.add(
        slot_key="chat-1-123",
        message="later",
        scheduled_at=time.time() + 60,
        replace_existing=False,
    )
    with pytest.raises(MonitorUpdateConflict, match="session already has an automation"):
        await svc.add(
            slot_key="chat-1-123",
            message="keep working",
            replace_existing=False,
        )


@pytest.mark.asyncio
async def test_goal_blocks_a_schedule_on_the_same_slot(svc):
    _capture_arms(svc)
    await svc.add(
        slot_key="chat-1-123",
        message="keep working",
        replace_existing=False,
    )
    with pytest.raises(MonitorUpdateConflict, match="session already has an automation"):
        await svc.add(
            slot_key="chat-1-123",
            message="later",
            scheduled_at=time.time() + 60,
            replace_existing=False,
        )


@pytest.mark.asyncio
async def test_scheduled_update_moves_deadline_and_rearms(svc, tmp_path):
    arms = _capture_arms(svc)
    first = time.time() + 600
    loop = await svc.add(slot_key="chat-1-123", message="before", scheduled_at=first)
    _protect(loop, "before")
    arms.clear()
    moved = time.time() + 1200

    updated = await svc.update_pending_scheduled_message(
        loop.id, message="after", scheduled_at=moved
    )

    assert updated is loop
    assert loop.message == ""
    assert loop.scheduled_at == moved
    assert loop.next_due_ts == moved
    assert loop.max_cycles == 1
    assert arms[-1] == pytest.approx(moved - time.time(), abs=1)
    stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
    assert stored["message"] == ""
    assert stored["scheduled_at"] == moved
    assert stored["next_due_ts"] == moved


@pytest.mark.asyncio
async def test_scheduled_update_preserves_deadline_when_only_message_changes(svc):
    _capture_arms(svc)
    due = time.time() + 600
    loop = await svc.add(slot_key="chat-1-123", message="before", scheduled_at=due)
    _protect(loop, "before")

    await svc.update_pending_scheduled_message(loop.id, message="after")

    assert loop.scheduled_at == due
    assert loop.next_due_ts == due


@pytest.mark.asyncio
async def test_scheduled_update_refuses_a_firing_or_completed_message(svc):
    arms = _capture_arms(svc)
    loop = await svc.add(slot_key="chat-1-123", message="before", scheduled_at=time.time() + 600)
    _protect(loop, "before")
    svc._firing.add(loop.id)
    with pytest.raises(ValueError, match="already firing or completed"):
        await svc.update_pending_scheduled_message(loop.id, message="too late")
    svc._firing.clear()
    loop.cycle_count = 1
    with pytest.raises(ValueError, match="already firing or completed"):
        await svc.update_pending_scheduled_message(loop.id, message="too late")

    loop.cycle_count = 0
    with pytest.raises(MonitorUpdateConflict, match="authenticated dashboard user"):
        await svc.update(loop.id, active=False)
    assert svc.get_by_id(loop.id) is loop
    arms.clear()
    fired, error, status = await svc.fire_now(loop.id)
    assert fired is None and status == 409
    assert "scheduled time" in error
    assert arms == []


@pytest.mark.asyncio
async def test_unschedule_refuses_after_dispatch_enters_the_fire_window(svc):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123",
        message="before",
        scheduled_at=time.time() + 600,
    )
    _protect(loop, "before")
    svc._firing.add(loop.id)

    assert await svc.remove_pending_scheduled_message(loop.id) is None
    assert svc.get_by_id(loop.id) is loop

    svc._firing.clear()
    assert await svc.remove_pending_scheduled_message(loop.id) is not None
    assert svc.get_by_id(loop.id) is None


@pytest.mark.asyncio
async def test_unschedule_refuses_while_delivery_callback_is_running(svc):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123",
        message="deliver once",
        scheduled_at=time.time() + 600,
    )
    # Drive the timer as due without arming a real long-lived task.
    loop.scheduled_at = time.time() - 1
    loop.next_due_ts = loop.scheduled_at
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delivering(_loop):
        entered.set()
        await release.wait()
        return True

    svc._on_fire = delivering
    timer = asyncio.create_task(svc._timer(loop, delay=0.0))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        assert await svc.remove_pending_scheduled_message(loop.id) is None
        assert svc.get_by_id(loop.id) is loop
    finally:
        release.set()
        await timer

    assert loop.cycle_count == 1


# --- Owner removal never consults provenance --------------------------------
#
# The fail-closed provenance check governs HONORING a scheduled message and the
# cancel-and-preserve path. Removal is the owner's exit: a row whose protected
# record is missing, malformed or names another slot can never fire, so refusing
# to remove it would trap the user with an entry nothing can clear (the gateway's
# refuse-then-drop path reaches ``remove`` for exactly such rows).

_PROVENANCE_STATES = ("valid", "missing", "foreign_slot")


def _store_provenance(loop: NudgeLoop, state: str) -> None:
    """Put process-memory provenance for *loop* into one review state."""
    trust_id = scheduled_message_trust_id(loop.id)
    autonudge_selfarm.delete_scheduled_message_record(trust_id)
    if state == "valid":
        _protect(loop, "send later")
    elif state == "foreign_slot":
        autonudge_selfarm.record_scheduled_message(
            trust_id, "chat-9-999", "not yours", loop.scheduled_at
        )
    elif state != "missing":  # pragma: no cover - parametrization typo guard
        raise AssertionError(state)
    trusted = autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key)
    assert (trusted is not None) is (state == "valid")


async def _settle_trust_revocation(loop: NudgeLoop) -> None:
    """Wait for executor-backed trust cleanup to delete process-memory state."""
    trust_id = scheduled_message_trust_id(loop.id)
    for _ in range(50):
        if autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) is None:
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", _PROVENANCE_STATES)
async def test_generic_remove_refuses_a_scheduled_message_whatever_its_provenance(
    svc, tmp_path, state
):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="send later", scheduled_at=time.time() + 600
    )
    _store_provenance(loop, state)

    with pytest.raises(MonitorUpdateConflict, match="authenticated dashboard user"):
        await svc.remove(loop.id)

    assert svc.get_by_id(loop.id) is loop
    assert svc.get_by_slot(loop.slot_key) is loop
    assert len(json.loads((tmp_path / "autonudge.json").read_text())["loops"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", _PROVENANCE_STATES)
async def test_gateway_discard_frees_a_scheduled_message_whatever_its_provenance(
    svc, tmp_path, state
):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="send later", scheduled_at=time.time() + 600
    )
    _store_provenance(loop, state)

    assert await svc.discard_scheduled_message(loop.id) is True

    assert svc.get_by_id(loop.id) is None
    assert svc.get_by_slot(loop.slot_key) is None
    assert json.loads((tmp_path / "autonudge.json").read_text())["loops"] == []
    await _settle_trust_revocation(loop)
    assert (
        autonudge_selfarm.read_scheduled_message(scheduled_message_trust_id(loop.id), loop.slot_key)
        is None
    )
    replacement = await svc.add(slot_key=loop.slot_key, message="goal after discard")
    assert svc.get_by_slot(loop.slot_key) is replacement


@pytest.mark.asyncio
@pytest.mark.parametrize("state", _PROVENANCE_STATES)
async def test_session_close_retires_a_scheduled_message_whatever_its_provenance(
    svc, tmp_path, state
):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="send later", scheduled_at=time.time() + 600
    )
    _store_provenance(loop, state)

    retired = await svc.remove_by_slot(loop.slot_key)

    assert retired is not None and retired.loop is loop
    assert (retired.scheduled_provenance is not None) is (state == "valid")
    assert svc.get_by_slot(loop.slot_key) is None
    assert json.loads((tmp_path / "autonudge.json").read_text())["loops"] == []
    await _settle_trust_revocation(loop)
    assert (
        autonudge_selfarm.read_scheduled_message(scheduled_message_trust_id(loop.id), loop.slot_key)
        is None
    )
    assert await svc.remove_by_slot(loop.slot_key) is None


@pytest.mark.asyncio
async def test_session_close_discard_does_not_read_provenance_for_authorization(svc, monkeypatch):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="send later", scheduled_at=time.time() + 600
    )
    _protect(loop, "send later")
    read = MagicMock(side_effect=OSError("transient protected-store read"))
    revoke = AsyncMock()
    monkeypatch.setattr(autonudge_selfarm, "read_scheduled_message", read)
    monkeypatch.setattr(svc, "_revoke_scheduled_provenance_before_removal", revoke)

    retired = await svc.remove_by_slot(loop.slot_key)

    assert retired is not None and retired.loop is loop
    assert svc.get_by_slot(loop.slot_key) is None
    read.assert_not_called()
    revoke.assert_awaited_once_with(loop)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ("missing", "foreign_slot"))
async def test_cancel_without_trusted_text_still_frees_the_row(svc, state):
    """The authenticated cancel path discards an unusable protected row."""
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="send later", scheduled_at=time.time() + 600
    )
    _store_provenance(loop, state)

    with pytest.raises(ValueError, match="provenance is unavailable"):
        await svc.remove_pending_scheduled_message(loop.id)

    assert svc.get_by_id(loop.id) is None
    assert svc.get_by_slot(loop.slot_key) is None
    await svc.remove(loop.id)


@pytest.mark.asyncio
async def test_remove_leaves_the_kind_guards_on_mutating_paths_intact(svc):
    """Removal opening up must not loosen update / fire / replace-by-add."""
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="send later", scheduled_at=time.time() + 600
    )
    _store_provenance(loop, "missing")

    with pytest.raises(MonitorUpdateConflict, match="authenticated dashboard user"):
        await svc.update(loop.id, active=False)
    with pytest.raises(MonitorUpdateConflict, match="authenticated dashboard user"):
        await svc.add(slot_key=loop.slot_key, message="agent replacement")
    fired, error, status = await svc.fire_now(loop.id)
    assert fired is None and status == 409 and "scheduled time" in error
    assert svc.get_by_id(loop.id) is loop


@pytest.mark.asyncio
async def test_time_only_edit_after_restart_restores_exact_protected_message(tmp_path, monkeypatch):
    """A scrubbed restart projection must never replace exact composer text."""
    from unittest.mock import MagicMock

    from kiro_crew import autonudge_authz, autonudge_selfarm
    from kiro_crew.autonudge import scheduled_message_trust_id

    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    monkeypatch.setattr(autonudge_authz, "sel", lambda: MagicMock())
    exact = f"release key {_FAKE_AWS_KEY}"
    original_at = time.time() + 600
    first = AutoNudgeService(base_dir=tmp_path)
    _capture_arms(first)
    loop = await first.add(
        slot_key="chat-1-123",
        message=exact,
        scheduled_at=original_at,
        loop_id="scheduled",
    )
    admission = {
        "queued_containment": {
            "linked": False,
            "mirrored": False,
            "workspace": "default",
        }
    }
    autonudge_selfarm.record_scheduled_message(
        scheduled_message_trust_id(loop.id),
        loop.slot_key,
        exact,
        original_at,
        containment_meta=admission,
    )
    # Model-visible loop storage is mutable and can contain a stale/scrubbed
    # candidate after a failed update, unlike the protected provenance record.
    mutable_path = tmp_path / "autonudge.json"
    stale = json.loads(mutable_path.read_text())
    stale["loops"][0].update(
        message="scrubbed mutable mirror",
        scheduled_at=original_at + 300,
        next_due_ts=original_at + 300,
    )
    mutable_path.write_text(json.dumps(stale))
    first.stop()

    restarted = AutoNudgeService(base_dir=tmp_path)
    _capture_arms(restarted)
    await restarted.start()
    restored = restarted.get_by_id(loop.id)
    assert restored is not None
    assert restored.message == ""
    assert restored.scheduled_at == original_at
    assert restored.next_due_ts == original_at

    moved_at = time.time() + 1_200
    updated, error, status = await autonudge_authz.authorize_and_update_nudge(
        svc=restarted,
        loop_id=loop.id,
        scheduled_at=moved_at,
        scheduled_user_origin=True,
        source="dashboard",
    )

    assert (error, status) == (None, 200)
    assert updated is restored
    assert restored.message == ""
    assert restored.scheduled_at == moved_at
    trusted = autonudge_selfarm.read_scheduled_message(
        scheduled_message_trust_id(loop.id), loop.slot_key
    )
    assert trusted == autonudge_selfarm.ScheduledMessageProvenance(
        slot_key=loop.slot_key,
        message=exact,
        scheduled_at=moved_at,
        containment_meta=admission,
    )
    stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
    assert stored["message"] == ""
    assert stored["scheduled_at"] == moved_at
    restarted.stop()


@pytest.mark.asyncio
async def test_concurrent_scheduled_edits_serialize_provenance_and_mutable_rollback(
    svc, tmp_path, monkeypatch
):
    """Only the CAS winner may remain in the loop, store, and trust record."""
    from unittest.mock import MagicMock

    from kiro_crew import autonudge_authz, autonudge_selfarm
    from kiro_crew.autonudge import scheduled_message_trust_id

    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    monkeypatch.setattr(autonudge_authz, "sel", lambda: MagicMock())
    _capture_arms(svc)
    original_at = time.time() + 600
    loop = await svc.add(
        slot_key="chat-1-123",
        message="original",
        scheduled_at=original_at,
        loop_id="scheduled",
    )
    record_id = scheduled_message_trust_id(loop.id)
    autonudge_selfarm.record_scheduled_message(record_id, loop.slot_key, "original", original_at)
    original_read = autonudge_selfarm.read_scheduled_message
    active_reads = 0
    maximum_concurrent_reads = 0
    first_read_entered = threading.Event()
    release_first_read = threading.Event()

    def read_while_observable(record: str, slot: str):
        nonlocal active_reads, maximum_concurrent_reads
        active_reads += 1
        maximum_concurrent_reads = max(maximum_concurrent_reads, active_reads)
        try:
            if not first_read_entered.is_set():
                first_read_entered.set()
                assert release_first_read.wait(timeout=30), "first read was never released"
            return original_read(record, slot)
        finally:
            active_reads -= 1

    monkeypatch.setattr(autonudge_selfarm, "read_scheduled_message", read_while_observable)
    times = (time.time() + 1_200, time.time() + 1_800)
    edits = [
        asyncio.ensure_future(
            autonudge_authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=loop.id,
                message=f"edit-{index}",
                scheduled_at=scheduled_at,
                scheduled_user_origin=True,
                source="dashboard",
            )
        )
        for index, scheduled_at in enumerate(times)
    ]
    try:
        await async_wait_until(first_read_entered.is_set)
        # The second edit must be parked on the store mutex, not inside a read.
        await until_parked(_maintenance_lock(svc._base_dir))
        assert active_reads == 1
    finally:
        release_first_read.set()
    results = await asyncio.gather(*edits)

    assert [result[2] for result in results] == [200, 200]
    assert maximum_concurrent_reads == 1
    trusted = autonudge_selfarm.read_scheduled_message(record_id, loop.slot_key)
    assert trusted is not None
    current = svc.get_by_id(loop.id)
    assert current is not None
    assert current.message == ""
    assert current.scheduled_at == trusted.scheduled_at
    stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
    assert (stored["message"], stored["scheduled_at"]) == ("", trusted.scheduled_at)


@pytest.mark.asyncio
async def test_restart_reconciles_failed_rollback_disk_state_from_protected_provenance(
    tmp_path, monkeypatch
):
    """A losing later candidate cannot delay the authentic scheduled turn after restart."""
    from unittest.mock import MagicMock

    from kiro_crew import autonudge_authz, autonudge_selfarm
    from kiro_crew.autonudge import scheduled_message_trust_id

    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    monkeypatch.setattr(autonudge_authz, "sel", lambda: MagicMock())
    trusted_message = f"trusted key {_FAKE_AWS_KEY}"
    trusted_at = time.time() + 600
    service = AutoNudgeService(base_dir=tmp_path)
    _capture_arms(service)
    loop = await service.add(
        slot_key="chat-1-123",
        message=trusted_message,
        scheduled_at=trusted_at,
        loop_id="scheduled",
    )
    record_id = scheduled_message_trust_id(loop.id)
    autonudge_selfarm.record_scheduled_message(
        record_id, loop.slot_key, trusted_message, trusted_at
    )

    original_write = service._write_state
    writes = 0

    def persist_candidate_but_fail_rollback(payload):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("rollback write failed")
        original_write(payload)

    monkeypatch.setattr(service, "_write_state", persist_candidate_but_fail_rollback)
    monkeypatch.setattr(autonudge_selfarm, "replace_scheduled_message", lambda *_args: False)
    losing_at = time.time() + 1_800
    updated, error, status = await autonudge_authz.authorize_and_update_nudge(
        svc=service,
        loop_id=loop.id,
        message="losing mutable text",
        scheduled_at=losing_at,
        scheduled_user_origin=True,
        source="dashboard",
    )

    assert updated is None
    assert status == 409
    assert error == "scheduled message provenance changed during update"
    stale = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
    assert stale["message"] == ""
    assert stale["scheduled_at"] == losing_at
    assert stale["next_due_ts"] == losing_at
    service.stop()

    restarted = AutoNudgeService(base_dir=tmp_path)
    arms = _capture_arms(restarted)
    before_start = time.time()
    await restarted.start()

    recovered = restarted.get_by_id(loop.id)
    assert recovered is not None
    assert recovered.message == ""
    assert recovered.scheduled_at == trusted_at
    assert recovered.next_due_ts == trusted_at
    assert arms == [pytest.approx(trusted_at - before_start, abs=1)]
    repaired = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
    assert repaired["message"] == ""
    assert repaired["scheduled_at"] == trusted_at
    assert repaired["next_due_ts"] == trusted_at
    restarted.stop()


def test_scheduled_revision_matches_browser_surrogates_without_changing_unicode():
    """Python hashes the same canonical bytes as JSON.stringify + TextEncoder."""
    import hashlib

    from kiro_crew import autonudge_authz

    scheduled_at = 1_700_000_000.125
    surrogate_message = "before\ud800after"
    # ECMAScript's well-formed JSON.stringify escapes a lone surrogate while
    # leaving ordinary Unicode intact. TextEncoder therefore hashes these ASCII
    # backslash-u bytes rather than raising while encoding the surrogate itself.
    browser_payload = '["scheduled","chat-1-123","before\\ud800after",1700000000125]'
    assert (
        autonudge_authz.scheduled_message_revision(
            "scheduled", "chat-1-123", surrogate_message, scheduled_at
        )
        == hashlib.sha256(browser_payload.encode("utf-8")).hexdigest()
    )

    unicode_message = "Déjà vu — 東京 🚀"
    prior_python_payload = json.dumps(
        ["scheduled", "chat-1-123", unicode_message, 1_700_000_000_125],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert (
        autonudge_authz.scheduled_message_revision(
            "scheduled", "chat-1-123", unicode_message, scheduled_at
        )
        == hashlib.sha256(prior_python_payload.encode("utf-8")).hexdigest()
    )


@pytest.mark.asyncio
async def test_owner_get_and_patch_accept_lone_surrogate_revision(svc, monkeypatch):
    """An escaped surrogate survives owner GET and a revision-guarded PATCH."""
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew import autonudge_authz
    from kiro_crew.dashboard.handlers import autonudge as handler

    audit = MagicMock()
    monkeypatch.setattr(autonudge_authz, "sel", lambda: audit)
    monkeypatch.setattr(handler, "sel", lambda: audit)
    monkeypatch.setattr(handler, "_autonudge_get", lambda: svc)
    _capture_arms(svc)

    message = "send this escaped edge \ud800 safely"
    due = float(int(time.time()) + 600)
    loop = await svc.add(
        slot_key="chat-1-123",
        message=message,
        scheduled_at=due,
        loop_id="scheduled-surrogate-cas",
    )
    _protect(loop, message)

    def owner_request(method, path, *, match, body=None):
        app = web.Application()
        app["state"] = SimpleNamespace(owner_id="U_OWNER")
        request = make_mocked_request(method, path, app=app, match_info=match)
        request["user"] = "U_OWNER"
        request["app"] = ""
        request["is_dashboard_user"] = True
        if body is not None:
            request.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        return request

    get_response = await handler.api_autonudge_get(
        owner_request(
            "GET",
            "/api/autonudge/slot/chat-1-123",
            match={"slot_key": "chat-1-123"},
        )
    )
    assert get_response.status == 200
    served = json.loads(get_response.body.decode("utf-8"))["loop"]
    assert served["message"] == message

    revision = autonudge_authz.scheduled_message_revision(
        served["id"], served["slot_key"], served["message"], served["scheduled_at"]
    )
    moved_at = due + 60
    patch_response = await handler.api_autonudge_update(
        owner_request(
            "PATCH",
            f"/api/autonudge/{loop.id}",
            match={"loop_id": loop.id},
            body={"message": message, "at": moved_at, "expected_revision": revision},
        )
    )
    assert patch_response.status == 200
    updated = autonudge_selfarm.read_scheduled_message(
        scheduled_message_trust_id(loop.id), loop.slot_key
    )
    assert updated is not None
    assert (updated.message, updated.scheduled_at) == (message, moved_at)


@pytest.mark.asyncio
async def test_stale_scheduled_patch_is_refused_before_audit_or_write(svc, tmp_path, monkeypatch):
    """A stale tab cannot replace the exact message or deadline another tab saved."""
    from unittest.mock import MagicMock

    from kiro_crew import autonudge_authz, autonudge_selfarm
    from kiro_crew.autonudge import scheduled_message_trust_id

    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    audit = MagicMock()
    monkeypatch.setattr(autonudge_authz, "sel", lambda: audit)
    _capture_arms(svc)
    original_at = time.time() + 600
    loop = await svc.add(
        slot_key="chat-1-123",
        message="original exact text",
        scheduled_at=original_at,
        loop_id="scheduled-cas",
    )
    record_id = scheduled_message_trust_id(loop.id)
    autonudge_selfarm.record_scheduled_message(
        record_id, loop.slot_key, "original exact text", original_at
    )
    stale_revision = autonudge_authz.scheduled_message_revision(
        loop.id, loop.slot_key, "original exact text", original_at
    )

    winner_at = time.time() + 1_200
    winner, error, status = await autonudge_authz.authorize_and_update_nudge(
        svc=svc,
        loop_id=loop.id,
        message="winner exact text",
        scheduled_at=winner_at,
        expected_scheduled_revision=stale_revision,
        scheduled_user_origin=True,
        source="dashboard",
    )
    assert winner is loop and (error, status) == (None, 200)
    audits_after_winner = audit.log_tool_invocation.call_count

    loser, error, status = await autonudge_authz.authorize_and_update_nudge(
        svc=svc,
        loop_id=loop.id,
        message="stale overwrite",
        scheduled_at=time.time() + 1_800,
        expected_scheduled_revision=stale_revision,
        scheduled_user_origin=True,
        source="dashboard",
    )

    assert loser is None and status == 409
    assert error == "The scheduled message changed in another window. Your edits were kept."
    # The refusal leaves exactly ONE non-critical ``denied`` record and no critical
    # ``invoked`` one: a stale request never invoked a mutation and must not look
    # like one in SEL, but it must not vanish from the trail either.
    stale_audits = audit.log_tool_invocation.call_args_list[audits_after_winner:]
    assert len(stale_audits) == 1
    denied = stale_audits[0].kwargs
    assert denied["outcome"] == "denied"
    assert denied["tool_name"] == "autonudge_update"
    assert denied["error"] == "scheduled revision conflict — scheduled message not updated"
    assert denied["metadata"]["loop_id"] == loop.id
    assert denied.get("critical", False) is False
    assert not [c for c in stale_audits if c.kwargs.get("outcome") == "invoked"]
    trusted = autonudge_selfarm.read_scheduled_message(record_id, loop.slot_key)
    assert trusted is not None
    assert (trusted.message, trusted.scheduled_at) == ("winner exact text", winner_at)
    stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
    assert (stored["message"], stored["scheduled_at"]) == ("", winner_at)


@pytest.mark.asyncio
async def test_valid_scheduled_revision_records_invoked_then_success_and_fails_closed(
    svc, tmp_path, monkeypatch
):
    """The revision-guarded write still audits ``invoked`` (critical) before mutating.

    Twin of the stale-revision regression above: a CURRENT revision records the
    critical ``invoked`` event and then ``success``, and when the critical audit
    cannot be written the update is denied with the store and provenance untouched.
    """
    from unittest.mock import MagicMock

    from kiro_crew import autonudge_authz, autonudge_selfarm
    from kiro_crew.autonudge import scheduled_message_trust_id

    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    audit = MagicMock()
    monkeypatch.setattr(autonudge_authz, "sel", lambda: audit)
    _capture_arms(svc)
    original_at = time.time() + 600
    loop = await svc.add(
        slot_key="chat-1-123",
        message="original exact text",
        scheduled_at=original_at,
        loop_id="scheduled-cas-valid",
    )
    record_id = scheduled_message_trust_id(loop.id)
    autonudge_selfarm.record_scheduled_message(
        record_id, loop.slot_key, "original exact text", original_at
    )
    current_revision = autonudge_authz.scheduled_message_revision(
        loop.id, loop.slot_key, "original exact text", original_at
    )

    moved_at = time.time() + 1_200
    updated, error, status = await autonudge_authz.authorize_and_update_nudge(
        svc=svc,
        loop_id=loop.id,
        message="moved exact text",
        scheduled_at=moved_at,
        expected_scheduled_revision=current_revision,
        scheduled_user_origin=True,
        source="dashboard",
    )

    assert updated is loop and (error, status) == (None, 200)
    outcomes = [c.kwargs["outcome"] for c in audit.log_tool_invocation.call_args_list]
    assert outcomes == ["invoked", "success"]
    invoked = audit.log_tool_invocation.call_args_list[0].kwargs
    assert invoked["critical"] is True
    assert invoked["tool_name"] == "autonudge_update"
    assert sorted(invoked["metadata"]["fields"]) == ["message", "scheduled_at"]
    trusted = autonudge_selfarm.read_scheduled_message(record_id, loop.slot_key)
    assert trusted is not None
    assert (trusted.message, trusted.scheduled_at) == ("moved exact text", moved_at)
    moved_revision = autonudge_authz.scheduled_message_revision(
        loop.id, loop.slot_key, "moved exact text", moved_at
    )

    # Fail closed: a current revision whose critical ``invoked`` audit cannot be
    # written is denied before the store or provenance is touched.
    audit.reset_mock()

    def _critical_unavailable(**kwargs):
        if kwargs.get("critical"):
            raise OSError("audit log unavailable")

    audit.log_tool_invocation.side_effect = _critical_unavailable
    denied, error, status = await autonudge_authz.authorize_and_update_nudge(
        svc=svc,
        loop_id=loop.id,
        message="unaudited overwrite",
        scheduled_at=time.time() + 1_800,
        expected_scheduled_revision=moved_revision,
        scheduled_user_origin=True,
        source="dashboard",
    )

    assert denied is None and status == 503
    assert error == "audit log unavailable — nudge loop not updated"
    attempted = [c.kwargs for c in audit.log_tool_invocation.call_args_list]
    assert [c["outcome"] for c in attempted] == ["invoked"]
    assert attempted[0]["critical"] is True
    trusted = autonudge_selfarm.read_scheduled_message(record_id, loop.slot_key)
    assert trusted is not None
    assert (trusted.message, trusted.scheduled_at) == ("moved exact text", moved_at)
    stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
    assert (stored["message"], stored["scheduled_at"]) == ("", moved_at)


@pytest.mark.asyncio
async def test_restart_ignores_agent_writable_completion_marker(tmp_path, monkeypatch):
    from kiro_crew import autonudge_selfarm
    from kiro_crew.autonudge import scheduled_message_trust_id

    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    trusted_at = time.time() - 60
    autonudge_selfarm.record_scheduled_message(
        scheduled_message_trust_id("scheduled"),
        "chat-1-123",
        "trusted pending text",
        trusted_at,
    )
    (tmp_path / "autonudge.json").write_text(
        json.dumps(
            {
                "version": 1,
                "loops": [
                    {
                        "id": "scheduled",
                        "slot_key": "chat-1-123",
                        "message": "forged completed text",
                        "idle_secs": 15,
                        "max_cycles": 1,
                        "cycle_count": 1,
                        "active": True,
                        "scheduled_message": True,
                        "scheduled_at": trusted_at + 3_600,
                        "scheduled_completed": True,
                        "next_due_ts": 0.0,
                    }
                ],
            }
        )
    )
    service = AutoNudgeService(base_dir=tmp_path)
    arms = _capture_arms(service)

    await service.start()

    recovered = service.get_by_id("scheduled")
    assert recovered is not None
    assert recovered.message == ""
    assert recovered.scheduled_at == trusted_at
    assert recovered.scheduled_completed is False
    assert recovered.cycle_count == 0
    assert recovered.next_due_ts == trusted_at
    assert arms == [pytest.approx(10.0)]
    service.stop()


@pytest.mark.asyncio
async def test_restart_cleans_only_signed_completed_schedule(tmp_path, monkeypatch):
    from kiro_crew import autonudge_selfarm
    from kiro_crew.autonudge import scheduled_message_trust_id

    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    trusted_at = time.time() - 60
    trust_id = scheduled_message_trust_id("scheduled")
    autonudge_selfarm.record_scheduled_message(
        trust_id, "chat-1-123", "trusted completed text", trusted_at
    )
    pending = autonudge_selfarm.read_scheduled_message(trust_id, "chat-1-123")
    assert pending is not None
    assert autonudge_selfarm.mark_scheduled_message_completed(trust_id, pending)
    (tmp_path / "autonudge.json").write_text(
        json.dumps(
            {
                "version": 1,
                "loops": [
                    {
                        "id": "scheduled",
                        "slot_key": "chat-1-123",
                        "message": "",
                        "idle_secs": 15,
                        "max_cycles": 1,
                        "cycle_count": 0,
                        "active": True,
                        "scheduled_message": True,
                        "scheduled_at": trusted_at,
                        "scheduled_completed": False,
                        "next_due_ts": trusted_at,
                    }
                ],
            }
        )
    )
    service = AutoNudgeService(base_dir=tmp_path)
    arms = _capture_arms(service)

    await service.start()

    recovered = service.get_by_id("scheduled")
    assert recovered is not None
    assert recovered.scheduled_completed is True
    assert recovered.cycle_count == 1
    assert recovered.next_due_ts == 0.0
    assert arms == [float(_OVERDUE_REARM_SECS)]
    await service._timer(recovered, delay=0.0)
    assert service.get_by_id("scheduled") is None
    service.stop()


@pytest.mark.asyncio
async def test_completed_schedule_retries_transient_removal_without_redelivery(
    svc, tmp_path, monkeypatch
):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123",
        message="deliver once",
        scheduled_at=time.time() + 60,
    )
    _protect(loop, "deliver once")
    loop.cycle_count = 1
    loop.last_fire_ts = time.time()
    original_write = svc._write_state
    writes = 0

    def fail_first_removal(payload):
        nonlocal writes
        writes += 1
        # First write persists scheduled_completed. The first removal write is
        # transiently lost; the same settlement must retry without delivery.
        if writes == 2:
            raise OSError("transient removal failure")
        original_write(payload)

    monkeypatch.setattr(svc, "_write_state", fail_first_removal)

    turn_task = asyncio.current_task()
    assert turn_task is not None
    svc._scheduled_turn_outcomes[loop.id] = (turn_task, True)
    await svc._settle_scheduled_turn(loop.id, turn_task, completed=True)

    assert writes == 3
    assert svc.get_by_id(loop.id) is None
    assert json.loads((tmp_path / "autonudge.json").read_text())["loops"] == []


@pytest.mark.asyncio
async def test_completed_schedule_retries_failed_completion_marker(svc, tmp_path, monkeypatch):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123",
        message="deliver once",
        scheduled_at=time.time() + 60,
    )
    _protect(loop, "deliver once")
    loop.cycle_count = 1
    loop.last_fire_ts = time.time()
    original_write = svc._write_state
    writes = 0

    def fail_first_marker(payload):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("transient marker failure")
        original_write(payload)

    monkeypatch.setattr(svc, "_write_state", fail_first_marker)
    monkeypatch.setattr("kiro_crew.autonudge._OVERDUE_REARM_SECS", 0)

    turn_task = asyncio.current_task()
    assert turn_task is not None
    svc._scheduled_turn_outcomes[loop.id] = (turn_task, True)
    svc._schedule_scheduled_settlement(loop.id, turn_task, completed=True)
    for _ in range(100):
        await asyncio.sleep(0.01)
        stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"]
        if svc.get_by_id(loop.id) is None and stored == []:
            break

    assert writes == 3
    assert svc.get_by_id(loop.id) is None
    assert stored == []


async def _completed_one_shot(svc: AutoNudgeService) -> NudgeLoop:
    """A delivered one-shot whose only remaining work is the cleanup write."""
    loop = await svc.add(
        slot_key="chat-1-123",
        message="deliver once",
        scheduled_at=time.time() + 60,
    )
    _protect(loop, "deliver once")
    await svc.mark_scheduled_message_completed(loop)
    loop.cycle_count = 1
    loop.last_fire_ts = time.time()
    loop.scheduled_completed = True
    loop.next_due_ts = 0.0
    return loop


@pytest.mark.asyncio
async def test_failed_cleanup_write_rearms_on_the_overdue_beat(svc, monkeypatch):
    arms = _capture_arms(svc)
    loop = await _completed_one_shot(svc)
    arms.clear()

    def wedged(_payload):
        raise OSError("store is wedged")

    monkeypatch.setattr(svc, "_write_state", wedged)

    with pytest.raises(OSError):
        await svc._timer(loop, delay=0.0)

    # The failed removal restored the row and re-armed it for another cleanup
    # attempt -- on the overdue beat, not at zero, which would retry the same
    # failing write back-to-back for as long as the store stays wedged.
    assert svc.get_by_id(loop.id) is loop
    assert loop.scheduled_completed is True
    assert arms == [float(_OVERDUE_REARM_SECS)]


@pytest.mark.asyncio
async def test_failed_cleanup_write_retries_boundedly_with_a_live_timer(svc, monkeypatch):
    loop = await _completed_one_shot(svc)
    monkeypatch.setattr("kiro_crew.autonudge._OVERDUE_REARM_SECS", 30)
    attempts = 0

    def wedged(_payload):
        nonlocal attempts
        attempts += 1
        raise OSError("store is wedged")

    monkeypatch.setattr(svc, "_write_state", wedged)

    with pytest.raises(OSError):
        await svc._timer(loop, delay=0.0)
    for _ in range(5):
        await asyncio.sleep(0.01)

    # One failed write, then ONE pending timer parked on the beat: with the
    # real ``_arm_timer`` a zero-delay re-arm would have spun through many
    # cleanup attempts inside this window.
    assert attempts == 1
    timer = svc._timers.get(loop.id)
    assert timer is not None and not timer.done()
    assert svc.get_by_id(loop.id) is loop
    svc.stop()


# --- Provenance revocation is reliable and fails closed ---------------------
#
# The mutable loop row is agent-writable, so a removal that commits while its
# protected provenance record survives lets a re-minted row replay cancelled
# text as user-authored input. Revocation therefore happens BEFORE the store
# commit, and an unverified revocation aborts the removal.


@pytest.mark.asyncio
async def test_removal_revokes_provenance_before_the_store_commit(svc, tmp_path):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="send later", scheduled_at=time.time() + 600
    )
    _protect(loop, "send later")
    trust_id = scheduled_message_trust_id(loop.id)
    assert autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) is not None

    original_write = svc._write_state
    provenance_gone_at_write: list[bool] = []

    def record_then_write(payload):
        provenance_gone_at_write.append(
            autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) is None
        )
        original_write(payload)

    svc._write_state = record_then_write  # type: ignore[method-assign]

    assert await svc.discard_scheduled_message(loop.id) is True

    # The removal's durable write observed the provenance already gone: a commit
    # can never leave replayable provenance behind.
    assert provenance_gone_at_write == [True]
    assert svc.get_by_id(loop.id) is None
    assert autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) is None


@pytest.mark.asyncio
async def test_removal_fails_closed_when_provenance_cannot_be_revoked(svc, tmp_path, monkeypatch):
    """An unrevoked protected record leaves the row in place rather than
    committing a removal a replay could survive."""
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="send later", scheduled_at=time.time() + 600
    )
    _protect(loop, "send later")

    # Model a deletion that silently fails: the record survives the revocation.
    monkeypatch.setattr(
        autonudge_selfarm,
        "delete_scheduled_message_record",
        lambda _id: None,
    )

    with pytest.raises(OSError, match="provenance survived revocation"):
        await svc.discard_scheduled_message(loop.id)

    assert svc.get_by_id(loop.id) is loop
    assert svc.get_by_slot(loop.slot_key) is loop
    assert len(json.loads((tmp_path / "autonudge.json").read_text())["loops"]) == 1
    trust_id = scheduled_message_trust_id(loop.id)
    assert autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("cancel", "discard", "remove_by_slot"))
async def test_failed_mutable_removal_restores_provenance_before_rearm(
    svc,
    tmp_path,
    monkeypatch,
    operation,
):
    arms = _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="send later", scheduled_at=time.time() + 600
    )
    _protect(loop, "send later")
    trust_id = scheduled_message_trust_id(loop.id)
    expected = autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key)
    assert expected is not None
    arms.clear()

    def fail_removal(_payload):
        assert autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) is None
        raise OSError("mutable removal failed")

    monkeypatch.setattr(svc, "_write_state", fail_removal)

    with pytest.raises(OSError, match="mutable removal failed"):
        if operation == "cancel":
            await svc.remove_pending_scheduled_message(loop.id)
        elif operation == "discard":
            await svc.discard_scheduled_message(loop.id)
        else:
            await svc.remove_by_slot(loop.slot_key)

    assert svc.get_by_id(loop.id) is loop
    assert autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) == expected
    assert arms, "the restored schedule was not rearmed"
    assert len(json.loads((tmp_path / "autonudge.json").read_text())["loops"]) == 1


@pytest.mark.asyncio
async def test_cancellation_during_provenance_revocation_restores_before_rearm(svc, monkeypatch):
    arms = _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="send later", scheduled_at=time.time() + 600
    )
    _protect(loop, "send later")
    trust_id = scheduled_message_trust_id(loop.id)
    expected = autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key)
    assert expected is not None
    arms.clear()

    entered = threading.Event()
    release = threading.Event()
    real_delete = autonudge_selfarm.delete_scheduled_message_record

    def blocking_delete(record_id: str) -> None:
        entered.set()
        if not release.wait(timeout=2.0):
            raise TimeoutError("test did not release provenance deletion")
        real_delete(record_id)

    monkeypatch.setattr(autonudge_selfarm, "delete_scheduled_message_record", blocking_delete)
    removal = asyncio.create_task(svc.discard_scheduled_message(loop.id))
    try:
        assert await asyncio.to_thread(entered.wait, 1.0)
        removal.cancel()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await removal

    assert svc.get_by_id(loop.id) is loop
    assert autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) == expected
    assert arms, "the mutable schedule rearmed before protected provenance was restored"


@pytest.mark.asyncio
async def test_generic_remove_cancellation_after_commit_stays_removed(svc, tmp_path, monkeypatch):
    loop = await svc.add(slot_key="chat-1-123", message="ordinary loop")
    entered = threading.Event()
    release = threading.Event()
    original_write = svc._write_state

    def blocking_write(payload):
        entered.set()
        assert release.wait(timeout=2.0)
        original_write(payload)

    monkeypatch.setattr(svc, "_write_state", blocking_write)
    removal = asyncio.create_task(svc.remove(loop.id))
    try:
        assert await asyncio.to_thread(entered.wait, 1.0)
        removal.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await removal
    finally:
        release.set()
        if not removal.done():
            removal.cancel()
            await asyncio.gather(removal, return_exceptions=True)

    assert svc.get_by_id(loop.id) is None
    assert json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))["loops"] == []


@pytest.mark.asyncio
async def test_failed_provenance_restoration_never_rearms_removed_metadata(
    svc, tmp_path, monkeypatch
):
    arms = _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="send later", scheduled_at=time.time() + 600
    )
    _protect(loop, "send later")
    trust_id = scheduled_message_trust_id(loop.id)
    arms.clear()

    monkeypatch.setattr(
        svc,
        "_write_state",
        MagicMock(side_effect=OSError("mutable removal failed")),
    )
    monkeypatch.setattr(
        autonudge_selfarm,
        "record_scheduled_message",
        MagicMock(side_effect=OSError("provenance restore failed")),
    )

    with pytest.raises(OSError, match="provenance restore failed"):
        await svc.discard_scheduled_message(loop.id)

    assert svc.get_by_id(loop.id) is loop
    assert autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) is None
    assert arms == []
    assert len(json.loads((tmp_path / "autonudge.json").read_text())["loops"]) == 1


# --- Local slash-command deliveries are retired, not stranded ---------------
#
# A scheduled message whose text is a local slash command runs a turn that
# returns without signalling completion, so notify_turn_complete never settles
# it. The gateway backstop retires the charged one-shot instead.


@pytest.mark.asyncio
async def test_predispatch_completion_cannot_claim_a_scheduled_delivery(svc):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="deliver later", scheduled_at=time.time() + 600
    )
    _protect(loop, "deliver later")

    svc.notify_turn_complete(loop.slot_key, turn_completed=True)
    assert loop.id not in svc._scheduled_turn_outcomes

    turn_task = _bind_current_scheduled_turn(svc, loop.id)
    loop.cycle_count = 1
    await svc._settle_scheduled_turn(loop.id, turn_task, completed=True)

    assert svc.get_by_id(loop.id) is loop
    trusted = autonudge_selfarm.read_scheduled_message(
        scheduled_message_trust_id(loop.id), loop.slot_key
    )
    assert trusted is not None and trusted.completed is False


@pytest.mark.asyncio
async def test_settle_unclaimed_scheduled_delivery_retires_a_charged_one_shot(svc, tmp_path):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="/goal status", scheduled_at=time.time() + 600
    )
    _protect(loop, "/goal status")
    _bind_current_scheduled_turn(svc, loop.id)
    loop.cycle_count = 1
    loop.last_fire_ts = time.time()

    assert await svc.settle_unclaimed_scheduled_delivery(loop.id) is True

    assert svc.get_by_id(loop.id) is None
    assert json.loads((tmp_path / "autonudge.json").read_text())["loops"] == []


@pytest.mark.asyncio
async def test_settle_unclaimed_scheduled_delivery_is_a_noop_after_completion_signal(svc):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="deliver once", scheduled_at=time.time() + 600
    )
    _protect(loop, "deliver once")
    _bind_current_scheduled_turn(svc, loop.id)
    loop.cycle_count = 1

    # An ordinary turn signals completion, which claims the pending delivery.
    svc.notify_turn_complete(loop.slot_key, turn_completed=True)

    assert await svc.settle_unclaimed_scheduled_delivery(loop.id) is False

    # The completion-signalled settlement still retires the one-shot.
    for _ in range(100):
        if svc.get_by_id(loop.id) is None:
            break
        await asyncio.sleep(0.01)
    assert svc.get_by_id(loop.id) is None


@pytest.mark.asyncio
async def test_a_turn_tail_task_settles_the_turn_it_names(svc):
    """The runner's tail runs in a task of its own and names the turn task.

    Correlation is by the task that RAN the delivery, so a hook called from any
    other task must carry it explicitly; one that names nothing is that other
    task's own completion and leaves the scheduled outcome unclaimed.
    """
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="deliver once", scheduled_at=time.time() + 600
    )
    _protect(loop, "deliver once")
    loop.cycle_count = 1
    release = asyncio.Event()

    async def scheduled_turn_body() -> None:
        await release.wait()

    scheduled_turn = asyncio.create_task(scheduled_turn_body())
    svc.note_scheduled_delivery_dispatched(loop.id)
    svc.bind_scheduled_delivery_task(loop.id, scheduled_turn)

    async def unrelated_tail() -> None:
        svc.notify_turn_complete(loop.slot_key, turn_completed=True)

    await asyncio.create_task(unrelated_tail())
    assert loop.id not in svc._scheduled_turn_outcomes

    async def the_turns_tail() -> None:
        svc.notify_turn_complete(loop.slot_key, turn_completed=True, turn_task=scheduled_turn)

    await asyncio.create_task(the_turns_tail())
    assert svc._scheduled_turn_outcomes[loop.id] == (scheduled_turn, True)
    release.set()
    await scheduled_turn
    for _ in range(100):
        if svc.get_by_id(loop.id) is None:
            break
        await asyncio.sleep(0.01)
    assert svc.get_by_id(loop.id) is None


@pytest.mark.asyncio
async def test_queued_same_slot_turn_cannot_overwrite_scheduled_outcome(svc, monkeypatch):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="deliver once", scheduled_at=time.time() + 600
    )
    _protect(loop, "deliver once")
    loop.cycle_count = 1
    loop.last_fire_ts = time.time()

    entered = threading.Event()
    release = threading.Event()
    real_write = svc._write_state

    def blocking_write(payload):
        entered.set()
        if not release.wait(timeout=2.0):
            raise TimeoutError("test did not release scheduled settlement")
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", blocking_write)
    start = asyncio.Event()

    async def scheduled_completion() -> None:
        await start.wait()
        svc.notify_turn_complete(loop.slot_key, turn_completed=True)

    scheduled_turn = asyncio.create_task(scheduled_completion())
    svc.note_scheduled_delivery_dispatched(loop.id)
    svc.bind_scheduled_delivery_task(loop.id, scheduled_turn)
    start.set()
    await scheduled_turn
    try:
        assert await asyncio.to_thread(entered.wait, 1.0)

        async def queued_completion() -> None:
            svc.notify_turn_complete(loop.slot_key, turn_completed=False)

        await asyncio.create_task(queued_completion())
        assert svc._scheduled_turn_outcomes[loop.id] == (scheduled_turn, True)
    finally:
        release.set()

    await asyncio.gather(*tuple(svc._inflight_adds))
    assert svc.get_by_id(loop.id) is None


@pytest.mark.asyncio
async def test_service_accepts_oversized_goal_budget_as_nonmutating_usage(svc):
    _capture_arms(svc)
    oversized = "9" * 5000

    loop = await svc.add(
        slot_key="chat-1-123",
        message=f"/goal --max {oversized} must not arm",
        scheduled_at=time.time() + 600,
    )

    assert loop.scheduled_message is True
    assert svc.get_by_slot(loop.slot_key) is loop


@pytest.mark.asyncio
async def test_service_rejects_a_mutating_goal_command_before_scheduled_create(svc):
    _capture_arms(svc)

    with pytest.raises(ValueError, match="cannot change session goals"):
        await svc.add(
            slot_key="chat-1-123",
            message="/goal clear",
            scheduled_at=time.time() + 600,
        )

    assert svc.get_by_slot("chat-1-123") is None


@pytest.mark.asyncio
async def test_time_only_edit_rejects_a_legacy_mutating_goal_message(svc):
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123",
        message="ordinary text",
        scheduled_at=time.time() + 600,
    )
    _protect(loop, "ordinary text")
    trust_id = scheduled_message_trust_id(loop.id)
    original = autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key)
    assert original is not None
    assert autonudge_selfarm.replace_scheduled_message(
        trust_id,
        original,
        "/goal --max 5 ship the feature",
        original.scheduled_at,
    )
    legacy = autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key)
    assert legacy is not None
    prior_deadline = loop.scheduled_at

    with pytest.raises(ValueError, match="cannot change session goals"):
        await svc.update_pending_scheduled_message(
            loop.id,
            scheduled_at=time.time() + 1_200,
        )

    assert loop.scheduled_at == prior_deadline
    assert autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) == legacy


# --- Inherited legacy controller bounds must never strand a one-shot ----------
#
# The three fields below are recorded per SLOT, not per cycle, so a scheduled
# one-shot inherits whatever the slot's HUMAN turns left behind. All three of
# ``_timer``'s terminal branches settled through the protected ``update()``,
# which refuses a scheduled record -- so the raise escaped ``_timer``'s bare
# task, the timer died, and the message was never delivered AND never retired.


def _inherit_runtime_budget(loop: NudgeLoop) -> None:
    loop.max_runtime_secs = 60
    loop.created_ts = time.time() - 9_999


def _inherit_approval_stall(loop: NudgeLoop) -> None:
    loop.approval_stalled = True


def _inherit_start_failure_standdown(loop: NudgeLoop) -> None:
    loop.consecutive_start_failures = _START_FAILURE_STANDDOWN_AFTER


def _inherit_start_failure_backoff(loop: NudgeLoop) -> None:
    loop.consecutive_start_failures = _START_FAILURE_BACKOFF_AFTER


_INHERITED_BOUNDS = [
    pytest.param(_inherit_runtime_budget, id="runtime_budget_spent"),
    pytest.param(_inherit_approval_stall, id="approval_stalled"),
    pytest.param(_inherit_start_failure_standdown, id="start_failure_standdown"),
    pytest.param(_inherit_start_failure_backoff, id="start_failure_backoff"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("inherit", _INHERITED_BOUNDS)
async def test_inherited_legacy_bound_still_dispatches_a_due_schedule(svc, inherit):
    """A due one-shot delivers past every bound it inherited from its slot."""
    arms = _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123",
        message="send the release update",
        scheduled_at=time.time() - 1,
    )
    _protect(loop, "send the release update")
    inherit(loop)
    arms.clear()
    fired: list[str] = []

    async def on_fire(candidate):
        fired.append(candidate.id)
        return True

    svc._on_fire = on_fire

    await svc._timer(loop, delay=0.0)

    # Reached its OWN dispatch, not a terminal update / expiry / backoff re-arm.
    assert fired == [loop.id]
    assert loop.cycle_count == 1
    assert loop.active is True
    assert loop.stopped_reason == ""
    # No deferral was paid: a backoff re-arm would have replaced the send.
    assert arms == []


@pytest.mark.asyncio
@pytest.mark.parametrize("inherit", _INHERITED_BOUNDS)
async def test_inherited_legacy_bound_emits_no_expiry_for_a_schedule(svc, inherit):
    """The bypass is silent on ``expired``: nothing stopped, so nothing to notify."""
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123",
        message="send later",
        scheduled_at=time.time() - 1,
    )
    _protect(loop, "send later")
    inherit(loop)
    events: list[str] = []
    svc.subscribe(lambda kind, _loop: events.append(kind))

    async def on_fire(_loop):
        return True

    svc._on_fire = on_fire

    await svc._timer(loop, delay=0.0)

    assert "expired" not in events
    assert "fired" in events


@pytest.mark.asyncio
@pytest.mark.parametrize("inherit", _INHERITED_BOUNDS)
async def test_inherited_legacy_bound_still_stops_an_ordinary_loop(svc, inherit):
    """The gate is scoped to scheduled records: an ordinary loop keeps its bound."""
    _capture_arms(svc)
    loop = await svc.add(slot_key="chat-1-123", message="keep working", idle_secs=60)
    inherit(loop)
    fired: list[str] = []

    async def on_fire(candidate):
        fired.append(candidate.id)
        return True

    svc._on_fire = on_fire

    await svc._timer(loop, delay=0.0)

    # Terminal bounds deactivate; the backoff bound defers and an approval stall
    # holds the loop active. Either way the ordinary loop does NOT reach a
    # delivery on this tick.
    assert fired == []
    if inherit is _inherit_start_failure_backoff:
        assert loop.active is True
        assert svc._start_failure_deferred[loop.id] == _START_FAILURE_BACKOFF_AFTER
    elif loop.approval_stalled:
        assert loop.active is True
        assert loop.stopped_reason == ""
    else:
        assert loop.active is False
        assert loop.stopped_reason != ""


@pytest.mark.asyncio
async def test_spent_budget_does_not_block_a_schedules_own_retirement(svc, tmp_path):
    """Post-delivery, a spent inherited budget must not refuse the one-shot.

    The post-delivery budget stop settles through ``_update_unserialized``,
    which carries the same protected-record refusal as ``update()``. Before the
    gate it raised AFTER the send had gone out, so the one-shot delivered and
    then stranded instead of retiring.
    """
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123",
        message="one turn",
        scheduled_at=time.time() + 60,
    )
    _protect(loop, "one turn")
    _inherit_runtime_budget(loop)

    async def delivered(_loop):
        return True

    svc._on_fire = delivered

    # Would raise MonitorUpdateConflict out of the post-delivery budget stop.
    await svc._run_fire_cycle(loop)

    assert loop.cycle_count == 1
    assert loop.active is True
    assert loop.stopped_reason == ""

    # And the one-shot's own completion path still retires it.
    _bind_current_scheduled_turn(svc, loop.id)
    svc.notify_turn_complete(loop.slot_key, turn_completed=True)
    await asyncio.gather(*tuple(svc._inflight_adds))

    assert svc.get_by_slot(loop.slot_key) is None
    assert json.loads((tmp_path / "autonudge.json").read_text())["loops"] == []


@pytest.mark.asyncio
async def test_delivered_schedule_still_retires_at_its_cap(svc):
    """The cap guard's own scheduled route is unchanged by the new gate."""
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123",
        message="send later",
        scheduled_at=time.time() - 1,
    )
    _protect(loop, "send later")
    loop.cycle_count = 1
    _inherit_approval_stall(loop)
    cleaned: list[str] = []

    async def cleanup(loop_id):
        cleaned.append(loop_id)
        return True

    svc.cleanup_completed_scheduled_message = cleanup  # type: ignore[method-assign]

    async def on_fire(_loop):
        raise AssertionError("a capped one-shot must not fire again")

    svc._on_fire = on_fire

    await svc._timer(loop, delay=0.0)

    assert cleaned == [loop.id]


@pytest.mark.asyncio
async def test_a_scheduled_record_always_carries_the_one_shot_cap(svc, tmp_path):
    """``max_cycles == 1`` is the ONE bound the gate leaves in place.

    Gating the other four on ``is_scheduled_message`` is only safe because a
    scheduled record cannot reach ``_timer`` with any other cap: ``add`` forces
    it, and ``_load`` repairs a stored row that disagrees. If either entrance
    ever stopped forcing it, the bypass would turn a one-shot into a loop with
    no bound at all -- so the invariant is asserted here rather than assumed.
    """
    _capture_arms(svc)
    due = time.time() + 600
    loop = await svc.add(
        slot_key="chat-1-123",
        message="send later",
        max_cycles=0,  # "unbounded" for an ordinary loop
        scheduled_at=due,
    )
    _protect(loop, "send later")
    assert loop.max_cycles == 1

    # And a stored row that disagrees is repaired on load, not honoured.
    raw = json.loads((tmp_path / "autonudge.json").read_text())
    raw["loops"][0]["max_cycles"] = 0
    (tmp_path / "autonudge.json").write_text(json.dumps(raw))
    reloaded = AutoNudgeService(base_dir=tmp_path)
    _capture_arms(reloaded)
    try:
        await reloaded.start()
        restored = reloaded.get_by_id(loop.id)
        assert restored is not None
        assert restored.max_cycles == 1
    finally:
        reloaded.stop()


# --- The attempt cap on a dispatched-but-unlanded one-shot -------------------
#
# ``max_cycles == 1`` bounds DELIVERED sends only. A turn that starts and never
# lands is settled back to pending and replayed on the record's own deadline, so
# without ``scheduled_attempts`` a slot whose turns keep failing would redispatch
# the same send every overdue beat forever. Each test below is a negative
# control for one branch: remove the cap (or the fence) and the assertion on the
# dispatch count fails.


async def _tick_then_fail_the_turn(svc: AutoNudgeService, loop: NudgeLoop) -> int:
    """Drive one timer tick; if it dispatched, settle that turn as not landed.

    Returns how many turns the tick dispatched (0 or 1).
    """
    fired: list[str] = []

    async def on_fire(candidate):
        fired.append(candidate.id)
        return True

    svc._on_fire = on_fire
    await svc._timer(loop, delay=0.0)
    if fired:
        _bind_current_scheduled_turn(svc, loop.id)
        svc.notify_turn_complete(loop.slot_key, turn_completed=False)
        await asyncio.gather(*tuple(svc._inflight_adds))
    return len(fired)


@pytest.mark.asyncio
async def test_unlanded_turns_replay_until_the_cap_then_stand_down(svc, tmp_path, caplog):
    """The table every other branch is read against, one row per tick.

    tick | dispatched | after settlement
    -----+------------+------------------------------------------------------
     1   | yes        | pending again, attempts=1, re-armed on its deadline
     2   | yes        | pending again, attempts=2, re-armed on its deadline
     3   | yes        | RETIRED: row gone, provenance gone, no re-arm
     4   | no         | nothing left to dispatch
    """
    arms = _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="keeps failing", scheduled_at=time.time() - 1
    )
    _protect(loop, "keeps failing")
    arms.clear()
    cap = _SCHEDULED_MESSAGE_MAX_ATTEMPTS
    assert cap >= 2  # the table below needs at least one replay before the stand-down

    dispatched: list[int] = []
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        for tick in range(1, cap + 2):
            dispatched.append(await _tick_then_fail_the_turn(svc, loop))
            stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"]
            if tick < cap:
                # Replayed: back to pending, the attempt charged in the SAME write.
                assert loop.cycle_count == 0
                assert loop.scheduled_completed is False
                assert loop.next_due_ts == loop.scheduled_at
                assert loop.scheduled_attempts == tick
                assert stored[0]["scheduled_attempts"] == tick
                assert stored[0]["cycle_count"] == 0
                assert arms[-1] == 10.0, "an overdue deadline re-arms on the overdue beat"
                assert svc.get_by_id(loop.id) is loop
            else:
                # Stood down, through the delivered record's own retirement path.
                assert svc.get_by_id(loop.id) is None
                assert stored == []
                assert (
                    autonudge_selfarm.read_scheduled_message(
                        scheduled_message_trust_id(loop.id), loop.slot_key
                    )
                    is None
                )

    assert dispatched == [1] * cap + [0], "exactly the cap, then nothing"
    standing_down = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "standing down" in r.getMessage()
    ]
    assert len(standing_down) == 1
    assert loop.id in standing_down[0] and "chat-1-123" in standing_down[0]
    assert "keeps failing" not in caplog.text, "identity only, never the composer text"


# --- The stand-down at the cap is explained, not just logged -------------------
#
# ``notify_scheduled_message_dropped`` is the gateway's loss notice bound into the
# service. Branch table, one test per row:
#
# settlement                       | retirement commit | hook called | row after
# ---------------------------------+-------------------+-------------+-----------
# unlanded, below the cap          | none (replayed)   | no          | pending
# unlanded, reaches the cap        | durable           | once, after | removed
# unlanded at the cap, write fails | rolled back       | no          | retryable
# at the cap, notice not durable   | durable           | until True  | kept, then removed
# delivered                        | durable           | no          | removed


def _recording_drop_hook(svc: AutoNudgeService, tmp_path, *, answers: list[bool] | None = None):
    """Install a hook that snapshots what the service had committed when it was called."""
    seen: list[dict] = []
    replies = list(answers or [])

    async def hook(loop: NudgeLoop) -> bool:
        seen.append(
            {
                "loop_id": loop.id,
                "row_present": svc.get_by_id(loop.id) is loop,
                "completed": loop.scheduled_completed,
                "attempts": loop.scheduled_attempts,
                "stored": json.loads((tmp_path / "autonudge.json").read_text())["loops"],
                "provenance": autonudge_selfarm.read_scheduled_message(
                    scheduled_message_trust_id(loop.id), loop.slot_key
                ),
            }
        )
        return replies.pop(0) if replies else True

    svc._notify_scheduled_message_dropped = hook
    return seen


@pytest.mark.asyncio
async def test_the_cap_stand_down_writes_one_notice_after_the_durable_commit(svc, tmp_path):
    """Below the cap nothing is explained; the attempt that reaches it is, exactly once,
    after the retirement is on disk and while the row still exists."""
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="keeps failing", scheduled_at=time.time() - 1
    )
    _protect(loop, "keeps failing")
    seen = _recording_drop_hook(svc, tmp_path)
    cap = _SCHEDULED_MESSAGE_MAX_ATTEMPTS

    for tick in range(1, cap + 1):
        assert await _tick_then_fail_the_turn(svc, loop) == 1
        if tick < cap:
            assert seen == [], "a replayed attempt is not a drop"
            assert svc.get_by_id(loop.id) is loop

    assert len(seen) == 1
    (call,) = seen
    assert call["loop_id"] == loop.id
    # Called AFTER the durable commit: the record and the store both read as
    # retired with the final attempt charged, and the provenance is marked...
    assert call["completed"] is True
    assert call["attempts"] == cap
    assert call["stored"][0]["scheduled_completed"] is True
    assert call["stored"][0]["scheduled_attempts"] == cap
    assert call["provenance"] is not None and call["provenance"].completed is True
    # ...but BEFORE the removal that takes the banner away.
    assert call["row_present"] is True
    assert svc.get_by_id(loop.id) is None
    assert json.loads((tmp_path / "autonudge.json").read_text())["loops"] == []


@pytest.mark.asyncio
async def test_a_failed_retirement_commit_emits_no_notice_and_stays_retryable(
    svc, tmp_path, monkeypatch
):
    """No durable retirement, no notice: a notice must never claim a drop that
    the store does not record. The same settlement then succeeds on retry."""
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="keeps failing", scheduled_at=time.time() - 1
    )
    _protect(loop, "keeps failing")
    seen = _recording_drop_hook(svc, tmp_path)
    cap = _SCHEDULED_MESSAGE_MAX_ATTEMPTS
    for _ in range(cap - 1):
        assert await _tick_then_fail_the_turn(svc, loop) == 1
    assert loop.scheduled_attempts == cap - 1

    async def delivered(_loop):
        return True

    svc._on_fire = delivered
    await svc._timer(loop, delay=0.0)
    assert loop.cycle_count == 1
    turn_task = _bind_current_scheduled_turn(svc, loop.id)
    svc._scheduled_delivery_pending.pop(loop.id, None)
    svc._scheduled_turn_outcomes[loop.id] = (turn_task, False)
    real_write = svc._write_state

    def refuse_write(_payload):
        raise OSError("disk full")

    monkeypatch.setattr(svc, "_write_state", refuse_write)
    with pytest.raises(OSError):
        await svc._settle_scheduled_turn(loop.id, turn_task, completed=False)

    assert seen == [], "the commit failed, so there is no drop to explain"
    # Rolled back and still retryable on the same outcome.
    assert svc.get_by_id(loop.id) is loop
    assert loop.scheduled_completed is False
    assert loop.scheduled_attempts == cap - 1
    assert (
        json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]["scheduled_attempts"]
        == cap - 1
    )
    assert svc._scheduled_turn_outcomes.get(loop.id) == (turn_task, False)

    monkeypatch.setattr(svc, "_write_state", real_write)
    await svc._settle_scheduled_turn(loop.id, turn_task, completed=False)

    assert len(seen) == 1 and seen[0]["completed"] is True and seen[0]["row_present"] is True
    assert svc.get_by_id(loop.id) is None


@pytest.mark.asyncio
async def test_a_notice_that_is_not_yet_durable_keeps_the_row_until_the_retry_lands(
    svc, tmp_path, monkeypatch, caplog
):
    """``False`` from the hook holds the removal: the row stays retired-but-present,
    the settlement supervisor retries, and the retry neither charges a second time
    nor dispatches again. The hook dedupes its own notice, so being asked twice
    is one notice on the session."""
    monkeypatch.setattr("kiro_crew.autonudge._OVERDUE_REARM_SECS", 0)
    _capture_arms(svc)
    loop = await svc.add(
        slot_key="chat-1-123", message="keeps failing", scheduled_at=time.time() - 1
    )
    _protect(loop, "keeps failing")
    seen = _recording_drop_hook(svc, tmp_path, answers=[False, True])
    cap = _SCHEDULED_MESSAGE_MAX_ATTEMPTS
    for _ in range(cap - 1):
        assert await _tick_then_fail_the_turn(svc, loop) == 1

    fired: list[str] = []

    async def on_fire(candidate):
        fired.append(candidate.id)
        return True

    svc._on_fire = on_fire
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        await svc._timer(loop, delay=0.0)
        assert fired == [loop.id]
        _bind_current_scheduled_turn(svc, loop.id)
        svc.notify_turn_complete(loop.slot_key, turn_completed=False)
        # Drain the settlement AND the supervisor's retry it schedules. The first
        # run fails by design (the hook said the notice is not durable); the
        # supervisor logs it and re-runs the same settlement.
        while svc._inflight_adds:
            await asyncio.gather(*tuple(svc._inflight_adds), return_exceptions=True)
            await asyncio.sleep(0)

    assert [call["completed"] for call in seen] == [True, True]
    assert [call["row_present"] for call in seen] == [True, True], "held until the notice landed"
    assert [call["attempts"] for call in seen] == [cap, cap], "the retry charges nothing more"
    assert fired == [loop.id], "the retry is a settlement, never another dispatch"
    assert svc.get_by_id(loop.id) is None
    assert json.loads((tmp_path / "autonudge.json").read_text())["loops"] == []
    assert "settlement failed" in caplog.text and "not yet durable" in caplog.text


@pytest.mark.asyncio
async def test_a_delivered_send_retires_without_a_drop_notice(svc, tmp_path):
    _capture_arms(svc)
    loop = await svc.add(slot_key="chat-1-123", message="lands", scheduled_at=time.time() - 1)
    _protect(loop, "lands")
    seen = _recording_drop_hook(svc, tmp_path)

    async def delivered(_loop):
        return True

    svc._on_fire = delivered
    await svc._timer(loop, delay=0.0)
    _bind_current_scheduled_turn(svc, loop.id)
    svc.notify_turn_complete(loop.slot_key, turn_completed=True)
    await asyncio.gather(*tuple(svc._inflight_adds))

    assert seen == []
    assert svc.get_by_id(loop.id) is None


@pytest.mark.asyncio
async def test_a_build_without_the_hook_stands_down_as_before(svc, tmp_path):
    """``None`` is the pre-hook behaviour: the retirement completes with no notice."""
    _capture_arms(svc)
    assert svc._notify_scheduled_message_dropped is None
    loop = await svc.add(
        slot_key="chat-1-123", message="keeps failing", scheduled_at=time.time() - 1
    )
    _protect(loop, "keeps failing")
    for _ in range(_SCHEDULED_MESSAGE_MAX_ATTEMPTS):
        assert await _tick_then_fail_the_turn(svc, loop) == 1
    assert svc.get_by_id(loop.id) is None


@pytest.mark.asyncio
async def test_refused_fires_consume_no_attempt_and_a_landed_send_still_retires(svc, tmp_path):
    """Pre-dispatch refusals (busy slot, provenance not due) are not attempts.

    Any number of them leaves the record pending with ``scheduled_attempts == 0``,
    and the ordinary delivered-and-completed send then retires it exactly as
    before -- the cap changes nothing on the path that works.
    """
    _capture_arms(svc)
    loop = await svc.add(slot_key="chat-1-123", message="busy first", scheduled_at=time.time() - 1)
    _protect(loop, "busy first")

    async def refused(_loop):
        return False

    svc._on_fire = refused
    for _ in range(_SCHEDULED_MESSAGE_MAX_ATTEMPTS + 2):
        await svc._timer(loop, delay=0.0)
    assert loop.cycle_count == 0
    assert loop.scheduled_attempts == 0
    assert (
        json.loads((tmp_path / "autonudge.json").read_text())["loops"][0].get(
            "scheduled_attempts", 0
        )
        == 0
    )

    async def delivered(_loop):
        return True

    svc._on_fire = delivered
    await svc._timer(loop, delay=0.0)
    assert loop.cycle_count == 1
    _bind_current_scheduled_turn(svc, loop.id)
    svc.notify_turn_complete(loop.slot_key, turn_completed=True)
    await asyncio.gather(*tuple(svc._inflight_adds))

    assert svc.get_by_id(loop.id) is None
    assert json.loads((tmp_path / "autonudge.json").read_text())["loops"] == []


@pytest.mark.asyncio
async def test_a_failed_replay_write_rolls_the_charge_back_with_the_reset(svc, monkeypatch):
    """The charge and the pending reset are one transaction, both ways."""
    _capture_arms(svc)
    loop = await svc.add(slot_key="chat-1-123", message="roll back", scheduled_at=time.time() - 1)
    _protect(loop, "roll back")

    async def delivered(_loop):
        return True

    svc._on_fire = delivered
    await svc._timer(loop, delay=0.0)
    assert loop.cycle_count == 1
    turn_task = _bind_current_scheduled_turn(svc, loop.id)
    svc._scheduled_delivery_pending.pop(loop.id, None)
    svc._scheduled_turn_outcomes[loop.id] = (turn_task, False)

    def refuse_write(_payload):
        raise OSError("disk full")

    monkeypatch.setattr(svc, "_write_state", refuse_write)
    with pytest.raises(OSError):
        await svc._settle_scheduled_turn(loop.id, turn_task, completed=False)

    assert loop.cycle_count == 1
    assert loop.scheduled_attempts == 0
    assert loop.scheduled_completed is False


@pytest.mark.asyncio
async def test_reload_charges_an_interrupted_turn_and_the_fence_refuses_the_next_dispatch(
    tmp_path, monkeypatch, caplog
):
    """Restart persistence of the cap, and the dispatch-site fence.

    A record interrupted mid-turn at ``cap - 1`` attempts reloads as pending with
    the interrupted turn charged, so it is AT the cap. The timer must then retire
    it without starting another turn: the fire callback is never called.
    """
    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    due = time.time() - 5
    autonudge_selfarm.record_scheduled_message(
        scheduled_message_trust_id("scheduled"), "chat-1-123", "interrupted again", due
    )
    row = _scheduled_store_row("scheduled", due)
    row.update(
        cycle_count=1,
        next_due_ts=0.0,
        scheduled_attempts=_SCHEDULED_MESSAGE_MAX_ATTEMPTS - 1,
    )
    (tmp_path / "autonudge.json").write_text(json.dumps({"version": 1, "loops": [row]}))
    service = AutoNudgeService(base_dir=tmp_path)
    _capture_arms(service)
    try:
        await service.start()
        recovered = service.get_by_slot("chat-1-123")
        assert recovered is not None
        assert recovered.cycle_count == 0, "normalised back to pending"
        assert recovered.scheduled_attempts == _SCHEDULED_MESSAGE_MAX_ATTEMPTS
        stored = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
        assert stored["scheduled_attempts"] == _SCHEDULED_MESSAGE_MAX_ATTEMPTS

        fired: list[str] = []

        async def on_fire(candidate):
            fired.append(candidate.id)
            return True

        service._on_fire = on_fire
        with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
            await service._timer(recovered, delay=0.0)

        assert fired == [], "the fence stands the record down instead of dispatching"
        assert service.get_by_id("scheduled") is None
        assert json.loads((tmp_path / "autonudge.json").read_text())["loops"] == []
        assert (
            autonudge_selfarm.read_scheduled_message(
                scheduled_message_trust_id("scheduled"), "chat-1-123"
            )
            is None
        )
        assert "standing down before dispatch" in caplog.text
        assert "interrupted again" not in caplog.text
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_the_fence_never_dispatches_even_when_it_cannot_retire(svc, caplog):
    """Without provenance the stand-down cannot be authorized -- and the record
    still does not fire. It stays pending on the overdue beat, which is the
    cadence a failed settlement already retries at."""
    arms = _capture_arms(svc)
    loop = await svc.add(slot_key="chat-1-123", message="no trust", scheduled_at=time.time() - 1)
    # No ``_protect``: provenance is absent, so ``mark_scheduled_message_completed`` refuses.
    loop.scheduled_attempts = _SCHEDULED_MESSAGE_MAX_ATTEMPTS
    arms.clear()
    fired: list[str] = []

    async def on_fire(candidate):
        fired.append(candidate.id)
        return True

    svc._on_fire = on_fire
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        await svc._timer(loop, delay=0.0)

    assert fired == []
    assert svc.get_by_id(loop.id) is loop
    assert loop.cycle_count == 0
    assert arms == [10.0]
    assert "standing down scheduled message" in caplog.text and "failed" in caplog.text


@pytest.mark.asyncio
async def test_an_ordinary_loop_ignores_the_scheduled_attempt_cap(svc):
    """Opposite-mode control: the fence and the charge are scheduled-only."""
    _capture_arms(svc)
    loop = await svc.add(slot_key="chat-1-123", message="keep nudging", idle_secs=60, max_cycles=5)
    loop.scheduled_attempts = _SCHEDULED_MESSAGE_MAX_ATTEMPTS  # forged; must mean nothing here
    fired: list[str] = []

    async def on_fire(candidate):
        fired.append(candidate.id)
        return True

    svc._on_fire = on_fire
    await svc._timer(loop, delay=0.0)

    assert fired == [loop.id]
    assert loop.cycle_count == 1
    assert loop.active is True
    assert svc.get_by_id(loop.id) is loop


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("three", 0),  # non-numeric -> fallback
        (None, 0),  # null -> fallback
        (-4, 0),  # below the floor -> floor
        (10**6, _SCHEDULED_MESSAGE_MAX_ATTEMPTS),  # past the cap -> clamped to the cap
        (1.0, 1),  # a float that is a whole number -> that int
    ],
)
async def test_load_repairs_a_malformed_attempt_count(tmp_path, monkeypatch, stored, expected):
    """The field is compared with ``>=`` on every scheduled wake, from an
    agent-writable store, so it is normalised at the boundary like
    ``consecutive_start_failures`` -- never raised on inside ``_timer``."""
    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    due = time.time() + 600
    autonudge_selfarm.record_scheduled_message(
        scheduled_message_trust_id("scheduled"), "chat-1-123", "repair me", due
    )
    row = _scheduled_store_row("scheduled", due)
    row["scheduled_attempts"] = stored
    (tmp_path / "autonudge.json").write_text(json.dumps({"version": 1, "loops": [row]}))
    service = AutoNudgeService(base_dir=tmp_path)
    _capture_arms(service)
    try:
        await service.start()
        recovered = service.get_by_id("scheduled")
        assert recovered is not None
        assert recovered.scheduled_attempts == expected
        assert type(recovered.scheduled_attempts) is int
        persisted = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
        assert persisted["scheduled_attempts"] == expected
    finally:
        service.stop()
