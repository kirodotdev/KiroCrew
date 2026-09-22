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
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import autonudge_selfarm
from kiro_crew.autonudge import (
    AutoNudgeService,
    MonitorUpdateConflict,
    NudgeLoop,
    scheduled_message_trust_id,
)

# Split so this file's own source carries no contiguous AWS key-ID literal.
_FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.fixture(autouse=True)
def _clear_process_memory(monkeypatch):
    autonudge_selfarm._reset_scheduled_messages_for_tests()
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
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

    assert svc.get_by_id(loop.id) is None


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

    assert arms == [0.0]
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

    def read_while_observable(record: str, slot: str):
        nonlocal active_reads, maximum_concurrent_reads
        active_reads += 1
        maximum_concurrent_reads = max(maximum_concurrent_reads, active_reads)
        try:
            time.sleep(0.02)
            return original_read(record, slot)
        finally:
            active_reads -= 1

    monkeypatch.setattr(autonudge_selfarm, "read_scheduled_message", read_while_observable)
    times = (time.time() + 1_200, time.time() + 1_800)
    results = await asyncio.gather(
        *(
            autonudge_authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=loop.id,
                message=f"edit-{index}",
                scheduled_at=scheduled_at,
                scheduled_user_origin=True,
                source="dashboard",
            )
            for index, scheduled_at in enumerate(times)
        )
    )

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
    assert arms == [0.0]
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
