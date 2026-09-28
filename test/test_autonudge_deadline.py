"""Deadline-preserving countdown tests for AutoNudgeService.

The loop's schedule anchors on ``NudgeLoop.next_due_ts``: user turns cancel
the pending timer task but never push the deadline back, delivered fires clear
it so the next cycle starts fresh from the nudge turn's end, and restarts
resume the persisted countdown instead of resetting it.
"""

from __future__ import annotations

import json
import time

import pytest

from kiro_crew.autonudge import _OVERDUE_REARM_SECS, AutoNudgeService, NudgeLoop


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture
def svc(tmp_path):
    return AutoNudgeService(base_dir=tmp_path)


def _capture_arm_delays(svc: AutoNudgeService) -> list[float | None]:
    """Spy on ``_arm_timer`` (the raw arming mechanism) recording each delay."""
    delays: list[float | None] = []
    orig = svc._arm_timer

    def spy(loop: NudgeLoop, delay: float | None = None) -> None:
        delays.append(delay)
        orig(loop, delay)

    svc._arm_timer = spy  # type: ignore[method-assign]
    return delays


@pytest.mark.asyncio
async def test_add_sets_and_persists_deadline(svc, tmp_path):
    """add() anchors the first deadline at arm time and writes it to the store."""
    await svc.start()
    before = time.time()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=100)
    after = time.time()
    assert before + 100 <= loop.next_due_ts <= after + 100
    raw = json.loads((tmp_path / "autonudge.json").read_text())
    assert raw["loops"][0]["next_due_ts"] == loop.next_due_ts
    svc.stop()


@pytest.mark.asyncio
async def test_user_turn_resumes_remaining_time_not_full_interval(svc):
    """The core fix: chatting defers the fire but does not restart the countdown."""
    await svc.start()
    delays = _capture_arm_delays(svc)
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=100)
    loop.next_due_ts = time.time() + 37  # mid-countdown
    svc.notify_user_input("chat-1-123")
    assert loop.id not in svc._timers  # pending fire cancelled — never mid-turn
    svc.notify_turn_complete("chat-1-123")
    assert loop.id in svc._timers
    resumed = delays[-1]
    assert resumed is not None and 35 <= resumed <= 38  # remaining, NOT 100
    assert time.time() + 30 < loop.next_due_ts  # deadline itself untouched
    svc.stop()


@pytest.mark.asyncio
async def test_deadline_passed_mid_turn_fires_after_overdue_beat(svc):
    """A deadline that lapsed during a user turn fires shortly after it ends."""
    await svc.start()
    delays = _capture_arm_delays(svc)
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=100)
    loop.next_due_ts = time.time() - 50  # went due while the user was chatting
    svc.notify_turn_complete("chat-1-123")
    assert delays[-1] == float(_OVERDUE_REARM_SECS)
    svc.stop()


@pytest.mark.asyncio
async def test_delivered_fire_clears_deadline_then_turn_end_starts_fresh(svc, monkeypatch):
    """The loop's own cycles keep the historical semantics: full interval from turn end."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    svc._on_fire = on_fire
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=100)
    await svc._timers[loop.id]
    assert len(fired) == 1
    assert loop.next_due_ts == 0.0  # cleared on delivery
    # Persisted too — a restart during the nudge turn must not resume a spent
    # countdown.
    raw = json.loads((svc._path).read_text())
    assert raw["loops"][0]["next_due_ts"] == 0.0
    # The nudge turn's end starts the next full cycle.
    before = time.time()
    svc.notify_turn_complete("chat-1-123")
    assert loop.next_due_ts >= before + 100
    svc.stop()


@pytest.mark.asyncio
async def test_restart_resumes_persisted_countdown(tmp_path):
    """Remaining time survives a gateway restart; an overdue loop fires soon."""
    svc1 = AutoNudgeService(base_dir=tmp_path)
    await svc1.start()
    await svc1.add(slot_key="chat-1-123", message="go", idle_secs=200)
    svc1.stop()
    # Simulate elapsed downtime by rewriting the persisted deadlines.
    raw = json.loads((tmp_path / "autonudge.json").read_text())
    raw["loops"][0]["next_due_ts"] = time.time() + 42
    raw["loops"].append(
        dict(raw["loops"][0], id="deadbeef", slot_key="chat-2-456", next_due_ts=time.time() - 99)
    )
    (tmp_path / "autonudge.json").write_text(json.dumps(raw))

    svc2 = AutoNudgeService(base_dir=tmp_path)
    delays = _capture_arm_delays(svc2)
    await svc2.start()
    assert len(delays) == 2
    in_future, overdue = delays
    assert in_future is not None and 40 <= in_future <= 43
    assert overdue == float(_OVERDUE_REARM_SECS)
    svc2.stop()


@pytest.mark.asyncio
async def test_legacy_store_entry_without_deadline_starts_fresh(tmp_path):
    """A store written before the field existed loads and arms a full countdown."""
    (tmp_path / "autonudge.json").write_text(
        json.dumps(
            {
                "version": 1,
                "loops": [
                    {
                        "id": "cafe0001",
                        "slot_key": "chat-1-123",
                        "message": "go",
                        "idle_secs": 120,
                        "active": True,
                    }
                ],
            }
        )
    )
    svc = AutoNudgeService(base_dir=tmp_path)
    delays = _capture_arm_delays(svc)
    await svc.start()
    loop = next(iter(svc._loops.values()))
    assert loop.next_due_ts >= time.time() + 118
    assert delays[-1] is not None and delays[-1] == pytest.approx(120, abs=2)
    svc.stop()


@pytest.mark.asyncio
async def test_update_interval_change_resets_deadline(svc):
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=100)
    loop.next_due_ts = time.time() + 5
    before = time.time()
    updated = await svc.update(loop.id, idle_secs=200)
    assert updated is not None
    assert updated.next_due_ts >= before + 200
    svc.stop()


@pytest.mark.asyncio
async def test_update_message_only_preserves_deadline(svc):
    """monitor_update refining the instruction must not delay the next check."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=100)
    due = time.time() + 50
    loop.next_due_ts = due
    delays = _capture_arm_delays(svc)
    updated = await svc.update(loop.id, message="check harder")
    assert updated is not None and updated.next_due_ts == due
    assert delays[-1] is not None and delays[-1] <= 51  # remaining, NOT 100
    svc.stop()


@pytest.mark.asyncio
async def test_deactivate_clears_deadline_and_revival_starts_fresh(svc):
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=100)
    await svc.update(loop.id, active=False)
    assert loop.next_due_ts == 0.0
    assert loop.id not in svc._timers
    before = time.time()
    await svc.update(loop.id, active=True)
    assert loop.next_due_ts >= before + 100
    svc.stop()


@pytest.mark.asyncio
async def test_near_deadline_resumes_with_remaining_not_overdue_beat(svc):
    """A deadline closer than the overdue beat still fires at the deadline —
    the beat applies only to deadlines already in the past."""
    await svc.start()
    delays = _capture_arm_delays(svc)
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=100)
    loop.next_due_ts = time.time() + 5  # nearer than _OVERDUE_REARM_SECS
    svc.notify_turn_complete("chat-1-123")
    resumed = delays[-1]
    assert resumed is not None and 3 <= resumed <= 5
    svc.stop()


@pytest.mark.asyncio
async def test_malformed_persisted_deadline_degrades_to_fresh(tmp_path):
    """Non-numeric, negative, or non-finite stored timer fields must not crash
    start() or poison emitted JSON — each degrades to a sane value."""
    (tmp_path / "autonudge.json").write_text(
        json.dumps(
            {
                "version": 1,
                "loops": [
                    {
                        "id": "cafe0001",
                        "slot_key": "chat-1-123",
                        "message": "go",
                        "idle_secs": 120,
                        "active": True,
                        "next_due_ts": None,
                    },
                    {
                        "id": "cafe0002",
                        "slot_key": "chat-2-456",
                        "message": "go",
                        "idle_secs": 120,
                        "active": True,
                        "next_due_ts": "soon",
                    },
                    {
                        "id": "cafe0003",
                        "slot_key": "chat-3-789",
                        "message": "go",
                        "idle_secs": 120,
                        "active": True,
                        "next_due_ts": -7.5,
                    },
                    {
                        "id": "cafe0004",
                        "slot_key": "chat-4-012",
                        "message": "go",
                        "idle_secs": 120,
                        "active": True,
                        "next_due_ts": 1e309,  # parses to inf — invalid JSON on re-emit
                    },
                    {
                        "id": "cafe0005",
                        "slot_key": "chat-5-345",
                        "message": "go",
                        "idle_secs": "soon",  # string interval — arm arithmetic input
                        "active": True,
                    },
                    {
                        "id": "cafe0006",
                        "slot_key": "chat-6-678",
                        "message": "go",
                        "idle_secs": 120,
                        "active": True,
                        # JSON ints are arbitrary-precision: float(10**400) raises
                        # OverflowError instead of returning inf. The repair must
                        # swallow it — a skipped entry would be DELETED by the
                        # next persist, not just mis-timed.
                        "next_due_ts": 10**400,
                    },
                ],
            }
        )
    )
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()  # must not raise
    assert len(svc._loops) == 6  # no entry skipped — a skip would delete it on next persist
    now = time.time()
    for loop in svc._loops.values():
        if loop.id == "cafe0005":
            assert loop.idle_secs == 15  # repaired to _MIN_IDLE_SECS
            assert loop.next_due_ts >= now + 13
        else:
            assert loop.idle_secs == 120
            assert loop.next_due_ts >= now + 118  # degraded to a fresh countdown
        assert loop.next_due_ts < now + 200  # finite — inf repaired, not kept
        assert loop.id in svc._timers
    # The repaired store must round-trip as strictly valid JSON.
    json.loads(json.dumps(svc._serialize_state(), allow_nan=False))
    svc.stop()


@pytest.mark.asyncio
async def test_update_during_nudge_turn_leaves_arm_to_turn_end(svc, monkeypatch):
    """A monitor_update landing while the nudge turn is still running (deadline
    cleared by the delivered fire) must not anchor the next interval mid-turn:
    the deadline stays unset and unarmed until notify_turn_complete."""
    import asyncio as _aio

    async def on_fire(loop):
        return True

    svc._on_fire = on_fire
    import kiro_crew.autonudge as _an

    real_sleep = _aio.sleep

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=100)
    await svc._timers[loop.id]  # delivered fire; deadline cleared, turn "running"
    monkeypatch.setattr(_an.asyncio, "sleep", real_sleep)
    updated = await svc.update(loop.id, message="check harder")
    assert updated is not None and updated.next_due_ts == 0.0
    assert loop.id not in svc._timers  # turn end owns the next arm
    # An interval change mid-turn also keeps the deadline unset — the new
    # interval applies when the turn's end starts the next full countdown.
    updated = await svc.update(loop.id, idle_secs=200)
    assert updated is not None and updated.next_due_ts == 0.0
    assert loop.id not in svc._timers
    before = time.time()
    svc.notify_turn_complete("chat-1-123")
    assert loop.next_due_ts >= before + 200  # fresh countdown at the NEW interval
    assert loop.id in svc._timers
    svc.stop()


@pytest.mark.asyncio
async def test_turn_complete_fresh_deadline_is_persisted(svc, monkeypatch):
    """A deadline assigned by the turn-lifecycle hook reaches the store, so a
    restart resumes this countdown instead of restarting the interval."""
    import asyncio as _aio

    async def on_fire(loop):
        return True

    svc._on_fire = on_fire
    import kiro_crew.autonudge as _an

    real_sleep = _aio.sleep

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=100)
    await svc._timers[loop.id]  # fire once; persists next_due_ts = 0
    monkeypatch.setattr(_an.asyncio, "sleep", real_sleep)  # keep the re-arm parked
    before = time.time()
    svc.notify_turn_complete("chat-1-123")  # assigns fresh deadline + schedules persist
    assert loop.next_due_ts >= before + 100
    for task in list(svc._inflight_adds):
        await task
    raw = json.loads((svc._path).read_text())
    assert raw["loops"][0]["next_due_ts"] == loop.next_due_ts
    svc.stop()
