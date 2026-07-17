"""Tests for AutoNudgeService — reactive idle timer, persistence, kill switch."""

from __future__ import annotations

import pytest

from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as _autonudge_mod
from kiro_crew.dashboard.handlers.autonudge import render_nudge_message


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture
def svc(tmp_path):
    return AutoNudgeService(base_dir=tmp_path)


@pytest.mark.asyncio
async def test_add_and_fire_on_idle(svc, monkeypatch):
    """Arming a timer and letting it elapse triggers the fire callback."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    svc._on_fire = on_fire
    # Patch asyncio.sleep inside the service's _timer to a no-op so the
    # test exercises the real fire path without waiting _MIN_IDLE_SECS.
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    # The timer task was created on add(); await it to completion.
    await svc._timers[loop.id]
    assert len(fired) == 1
    assert fired[0].id == loop.id
    # cycle_count should have been bumped by _timer.
    assert svc._loops[loop.id].cycle_count == 1


@pytest.mark.asyncio
async def test_user_input_cancels_timer(svc):
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)

    svc._on_fire = on_fire
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    assert loop.id in svc._timers
    svc.notify_user_input("chat-1-123")
    assert loop.id not in svc._timers


@pytest.mark.asyncio
async def test_notify_turn_complete_rearms(svc):
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    svc._cancel_timer(loop.id)
    assert loop.id not in svc._timers
    svc.notify_turn_complete("chat-1-123")
    assert loop.id in svc._timers


@pytest.mark.asyncio
async def test_persistence_across_restart(tmp_path):
    svc1 = AutoNudgeService(base_dir=tmp_path)
    await svc1.start()
    loop = await svc1.add(slot_key="chat-1-123", message="go", idle_secs=15, max_cycles=5)
    svc1.stop()

    # New instance reads the same file and restores loops.
    svc2 = AutoNudgeService(base_dir=tmp_path)
    await svc2.start()
    restored = svc2.get_by_slot("chat-1-123")
    assert restored is not None
    assert restored.id == loop.id
    assert restored.message == "go"
    assert restored.max_cycles == 5
    assert loop.id in svc2._timers  # timer re-armed
    svc2.stop()


@pytest.mark.asyncio
async def test_max_cycles_deactivates(svc, monkeypatch):
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15, max_cycles=2)
    loop.cycle_count = 2  # simulate cap reached
    svc._save()
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    # _timer with cycle_count==max deactivates the loop (doesn't remove it).
    refreshed = svc._loops[loop.id]
    assert not refreshed.active


@pytest.mark.asyncio
async def test_stop_sentinel_removes_loop(svc, tmp_path, monkeypatch):
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    sentinel = tmp_path / "STOP"
    loop = await svc.add(
        slot_key="chat-1-123", message="go", idle_secs=15, stop_sentinel_path=str(sentinel)
    )
    sentinel.write_text("halt")
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    assert svc.get_by_slot("chat-1-123") is None


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
async def test_disabled_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "0")
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    # Service is a no-op when flag is off — add/remove still work on the in-memory
    # dict but timers never arm. Verify via the enabled() helper.
    from kiro_crew.autonudge import enabled

    assert not enabled()


@pytest.mark.asyncio
async def test_update_changes_message_and_idle(svc):
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="old", idle_secs=30)
    updated = await svc.update(loop.id, message="new", idle_secs=60)
    assert updated is not None
    assert updated.message == "new"
    assert updated.idle_secs == 60


@pytest.mark.asyncio
async def test_idle_secs_clamped(svc):
    """Verify add() clamps idle_secs to [_MIN_IDLE_SECS, _MAX_IDLE_SECS]."""
    await svc.start()
    # Below min → clamped up to 15.
    loop_low = await svc.add(slot_key="s1", message="m", idle_secs=5)
    assert loop_low.idle_secs == 15
    # Above max → clamped down to 86400.
    loop_high = await svc.add(slot_key="s2", message="m", idle_secs=100_000)
    assert loop_high.idle_secs == 86400


@pytest.mark.asyncio
async def test_skip_when_delivery_returns_false(svc, monkeypatch):
    """A skipped delivery (slot mid-turn) must NOT bump cycle_count, and must
    re-arm the timer with a backoff so the loop self-heals (Mesh-2147)."""
    import asyncio

    import kiro_crew.autonudge as _an

    sleep_calls: list[float] = []
    gate = asyncio.Event()  # never set — blocks the re-armed timer's sleep

    async def _sleep(secs):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 2:
            await gate.wait()  # halt the re-arm chain so the test is bounded
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)

    fired: list[NudgeLoop] = []

    async def on_fire_skip(loop):
        fired.append(loop)
        return False  # delivery skipped (e.g. slot busy)

    svc._on_fire = on_fire_skip
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=60)
    t1 = svc._timers[loop.id]
    await t1
    # Callback ran, delivery skipped → cycle_count must not bump, loop alive.
    assert len(fired) == 1
    assert svc._loops[loop.id].cycle_count == 0
    assert svc._loops[loop.id].last_fire_ts == 0.0
    assert svc._loops[loop.id].active is True
    # Self-heal: a NEW timer task is armed (not the completed t1).
    assert loop.id in svc._timers
    assert svc._timers[loop.id] is not t1
    assert not svc._timers[loop.id].done()
    # First sleep used the full idle; the re-arm used the shorter backoff.
    assert sleep_calls[0] == 60
    assert _an._REARM_BACKOFF_SECS in sleep_calls
    svc._cancel_timer(loop.id)  # cleanup


@pytest.mark.asyncio
async def test_fire_callback_exception_does_not_deactivate(svc, monkeypatch):
    """An exception in _on_fire is swallowed (treated as not-delivered):
    cycle_count unchanged, loop stays active, AND the timer self-heals by
    re-arming with a backoff (Mesh-2147)."""
    import asyncio

    import kiro_crew.autonudge as _an

    sleep_calls: list[float] = []
    gate = asyncio.Event()  # never set — blocks the re-armed timer's sleep

    async def _sleep(secs):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 2:
            await gate.wait()
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)

    async def on_fire_raise(loop):
        raise RuntimeError("kaboom")

    svc._on_fire = on_fire_raise
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=60)
    t1 = svc._timers[loop.id]
    await t1
    refreshed = svc._loops[loop.id]
    assert refreshed.cycle_count == 0  # exception treated as not-delivered
    assert refreshed.active is True  # loop still alive
    # Self-heal: timer re-armed with a fresh task (not the completed t1).
    assert loop.id in svc._timers
    assert svc._timers[loop.id] is not t1
    assert not svc._timers[loop.id].done()
    svc._cancel_timer(loop.id)  # cleanup


@pytest.mark.asyncio
async def test_rearm_backoff_escalates_on_consecutive_failures(svc, monkeypatch):
    """Consecutive non-deliveries escalate the re-arm delay (15 → 30 → 60 …),
    so a never-delivering loop backs off instead of hammering (Mesh-2147)."""
    import asyncio

    import kiro_crew.autonudge as _an

    sleep_calls: list[float] = []

    async def _sleep(secs):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 5:
            raise asyncio.CancelledError  # halt the chain; _timer returns cleanly
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)

    async def on_fire_skip(loop):
        return False

    svc._on_fire = on_fire_skip
    await svc.start()
    # idle_secs large so neither the 300s ceiling nor idle_secs clamps the ramp.
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=10000)
    task = svc._timers[loop.id]
    for _ in range(12):
        try:
            await task
        except asyncio.CancelledError:
            break
        nxt = svc._timers.get(loop.id)
        if nxt is None or nxt is task:
            break
        task = nxt
    # First sleep = full idle; then exponential backoff per failure.
    assert sleep_calls == [10000, 15, 30, 60, 120]
    assert svc._loops[loop.id].active is True
    assert svc._rearm_fail_count[loop.id] == 4
    svc._cancel_timer(loop.id)


@pytest.mark.asyncio
async def test_failure_log_rate_limited_to_once_per_streak(svc, monkeypatch):
    """A permanently-failing callback logs a full traceback only on the first
    failure of a streak, not every re-arm (Mesh-2147 log-spam fix)."""
    import asyncio

    import kiro_crew.autonudge as _an

    sleep_calls: list[float] = []

    async def _sleep(secs):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 4:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)
    exc_calls: list[tuple] = []
    monkeypatch.setattr(_an.logger, "exception", lambda *a, **k: exc_calls.append(a))

    async def on_fire_raise(loop):
        raise RuntimeError("kaboom")

    svc._on_fire = on_fire_raise
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=10000)
    task = svc._timers[loop.id]
    for _ in range(12):
        try:
            await task
        except asyncio.CancelledError:
            break
        nxt = svc._timers.get(loop.id)
        if nxt is None or nxt is task:
            break
        task = nxt
    # 3 fires raised (calls 1-3); only the first emitted a full traceback.
    assert len(exc_calls) == 1
    assert svc._rearm_fail_count[loop.id] == 3
    svc._cancel_timer(loop.id)


@pytest.mark.asyncio
async def test_failure_streak_resets_on_delivery(svc, monkeypatch):
    """A delivered fire clears the failure streak so the next skip starts the
    backoff ramp fresh (Mesh-2147)."""
    import asyncio

    import kiro_crew.autonudge as _an

    sleep_calls: list[float] = []

    async def _sleep(secs):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 5:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)

    results = [False, False, True]  # third fire delivers
    idx = {"i": 0}

    async def on_fire(loop):
        i = idx["i"]
        idx["i"] += 1
        return results[i] if i < len(results) else True

    svc._on_fire = on_fire
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=10000)
    task = svc._timers[loop.id]
    for _ in range(12):
        try:
            await task
        except asyncio.CancelledError:
            break
        nxt = svc._timers.get(loop.id)
        if nxt is None or nxt is task:
            break
        task = nxt
    # 2 skips escalated (15, 30), then delivery bumped cycle_count and the
    # delivered happy-path does not re-arm, so the chain stops at 3 sleeps.
    assert sleep_calls == [10000, 15, 30]
    assert svc._loops[loop.id].cycle_count == 1
    assert loop.id not in svc._rearm_fail_count  # streak cleared on delivery


@pytest.mark.asyncio
async def test_fire_removed_loop_does_not_rearm_orphan(svc, monkeypatch):
    """If _on_fire removes the loop (e.g. slot missing) and returns False, the
    re-arm path must NOT resurrect it with a fresh timer (Mesh-2147 orphan)."""
    import kiro_crew.autonudge as _an

    sleep_calls: list[float] = []

    async def _sleep(secs):
        sleep_calls.append(secs)
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _sleep)

    async def on_fire_self_remove(loop):
        await svc.remove(loop.id)  # slot gone — fire path drops the loop
        return False

    svc._on_fire = on_fire_self_remove
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=60)
    await svc._timers[loop.id]
    # Loop was removed by the fire path and must stay gone — no resurrection.
    assert loop.id not in svc._loops
    assert loop.id not in svc._timers
    assert loop.id not in svc._rearm_fail_count
    # Only the initial idle sleep ran; no backoff re-arm fired.
    assert sleep_calls == [60]


@pytest.mark.asyncio
async def test_delivered_bumps_cycle_count(svc, monkeypatch):
    """When _on_fire returns True, cycle_count bumps and 'fired' event emits."""
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)

    events: list[tuple[str, str]] = []
    svc.subscribe(lambda ev, lp: events.append((ev, lp.id if lp else "")))

    async def on_fire_ok(loop):
        return True

    svc._on_fire = on_fire_ok
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    await svc._timers[loop.id]
    assert svc._loops[loop.id].cycle_count == 1
    assert svc._loops[loop.id].last_fire_ts > 0.0
    assert ("fired", loop.id) in events


@pytest.mark.asyncio
async def test_resolve_stop_sentinel(tmp_path, monkeypatch):
    """resolve_stop_sentinel computes per-slot path from workspace."""
    monkeypatch.setattr(_autonudge_mod, "workspace_dir_for", lambda ws="default": tmp_path)
    path = _autonudge_mod.resolve_stop_sentinel("chat:1/123", "default")
    assert path == str(tmp_path / ".stop-chat_1_123")


def test_render_nudge_message():
    """render_nudge_message replaces {{STOP_FILE}} with the sentinel path."""
    result = render_nudge_message("halt: create {{STOP_FILE}}", "/tmp/.stop-x")
    assert result == "halt: create /tmp/.stop-x"
    assert "{{STOP_FILE}}" not in result

    # None sentinel produces empty string
    result2 = render_nudge_message("create {{STOP_FILE}}", None)
    assert result2 == "create "
