"""A loop's tick is held while host memory is critical and runs once it recovers.

Drives the real timer callback of a loop armed through ``AutoNudgeService.add``.
Only the admission read, the turn delivery and the re-arm are stubs: the re-arm is
recorded instead of scheduled, so no held tick runs on its own during a test.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from autonudge_helpers import _structured_monitor

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.autonudge_service import memory_hold
from kiro_crew.autonudge_service.model import is_structured_monitor_loop

SLOT = "chat-memory-hold"
CRITICAL = (False, "host memory is critical (1.2 GB free)")
ADMITTED = (True, "")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


@pytest.fixture
def admission(monkeypatch):
    """``state["answer"]`` is what the next admission read returns."""
    state = {"answer": ADMITTED, "reads": 0}

    async def _read() -> tuple[bool, str]:
        state["reads"] += 1
        return state["answer"]

    monkeypatch.setattr(memory_hold, "read_admission", _read)
    return state


@pytest_asyncio.fixture
async def armed(tmp_path, monkeypatch):
    service = AutoNudgeService(base_dir=tmp_path / "svc")
    fired: list[int] = []

    async def on_fire(loop) -> bool:
        fired.append(loop.cycle_count)
        return True

    service._on_fire = AsyncMock(side_effect=on_fire)
    loop = await service.add(SLOT, "keep going", idle_secs=300)
    rearms: list[float | None] = []
    monkeypatch.setattr(service, "_arm_timer", lambda lp, delay=None: rearms.append(delay))
    try:
        yield service, loop, fired, rearms
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_critical_host_holds_the_tick_and_keeps_the_loop(armed, admission):
    service, loop, fired, rearms = armed
    admission["answer"] = CRITICAL
    cycles = loop.cycle_count

    await service._run_timer_callback(loop)

    assert fired == [], "no turn starts while memory is critical"
    assert loop.active is True and loop.stopped_reason in ("", None)
    assert loop.cycle_count == cycles, "a held tick spends no cycle"
    assert rearms == [memory_hold.CRITICAL_HOLD_RECHECK_SECS], "it asks again later"
    assert service._memory_held[loop.id] == CRITICAL[1]


@pytest.mark.asyncio
async def test_a_normal_host_runs_the_tick(armed, admission):
    service, loop, fired, rearms = armed

    await service._run_timer_callback(loop)

    assert len(fired) == 1, "an admitted tick fires as before"
    assert memory_hold.CRITICAL_HOLD_RECHECK_SECS not in rearms
    assert loop.id not in service._memory_held


@pytest.mark.asyncio
async def test_the_first_tick_after_memory_recovers_fires(armed, admission):
    service, loop, fired, _rearms = armed
    admission["answer"] = CRITICAL
    await service._run_timer_callback(loop)
    await service._run_timer_callback(loop)
    assert fired == []

    admission["answer"] = ADMITTED
    await service._run_timer_callback(loop)

    assert len(fired) == 1
    assert loop.id not in service._memory_held


@pytest.mark.asyncio
async def test_a_pushed_tick_runs_at_critical(armed, admission):
    service, loop, fired, rearms = armed
    admission["answer"] = CRITICAL
    service._pushed_ticks.add(loop.id)

    await service._run_timer_callback(loop)

    assert len(fired) == 1, "a worker's report or the posture wake reaches the conductor"
    assert memory_hold.CRITICAL_HOLD_RECHECK_SECS not in rearms
    assert loop.id not in service._memory_held


@pytest.mark.asyncio
async def test_an_owed_wake_runs_at_critical(armed, admission):
    # A pushed report whose delivery was refused leaves the owed wake but spends
    # the push mark; its retry must not wait for memory to recover.
    service, loop, _fired, rearms = armed
    admission["answer"] = CRITICAL
    service._pending_monitor_wake[loop.id] = "worker report brief"
    assert loop.id not in service._pushed_ticks

    held = await memory_hold.holds_for_critical_memory(service, loop)

    assert held is False, "the owed report reaches the conductor at critical"
    assert rearms == [] and admission["reads"] == 0
    assert loop.id not in service._memory_held
    assert service._pending_monitor_wake == {loop.id: "worker report brief"}, "read, not consumed"


@pytest.mark.asyncio
async def test_a_held_loop_still_stops_on_its_runtime_budget(armed, admission):
    service, loop, fired, _rearms = armed
    loop.max_runtime_secs = 600
    admission["answer"] = CRITICAL
    await service._run_timer_callback(loop)
    assert fired == []
    # The loop started an hour ago: the hold counts toward its wall-time budget.
    loop.created_ts -= 3600

    admission["answer"] = ADMITTED
    await service._run_timer_callback(loop)

    assert fired == [], "the spent budget stops the loop instead of firing it"
    assert loop.active is False and loop.stopped_reason == "runtime_budget"


@pytest.mark.asyncio
async def test_a_pushed_tick_clears_the_hold_record(armed, admission):
    service, loop, fired, _rearms = armed
    admission["answer"] = CRITICAL
    await service._run_timer_callback(loop)
    assert loop.id in service._memory_held
    service._pushed_ticks.add(loop.id)

    await service._run_timer_callback(loop)

    assert len(fired) == 1
    assert service._memory_held == {}, "the release is logged, not left stale"


@pytest.mark.asyncio
async def test_an_inactive_loop_is_never_held(armed, admission):
    service, loop, fired, rearms = armed
    admission["answer"] = CRITICAL
    loop.active = False

    await service._run_timer_callback(loop)

    assert admission["reads"] == 0, "an inactive loop does not read memory"
    assert rearms == [] and fired == []


@pytest_asyncio.fixture
async def live(tmp_path):
    """A loop whose re-arms are real timers, for the Fire now path."""
    service = AutoNudgeService(base_dir=tmp_path / "svc")
    fired: list[int] = []

    async def on_fire(loop) -> bool:
        fired.append(loop.cycle_count)
        return True

    service._on_fire = AsyncMock(side_effect=on_fire)
    loop = await service.add(SLOT, "keep going", idle_secs=300)
    try:
        yield service, loop, fired
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_fire_now_press_runs_at_critical(live, admission):
    service, loop, fired = live
    admission["answer"] = CRITICAL

    armed, error, status = await service.fire_now(loop.id)
    assert (error, status) == ("", 200)
    await asyncio.wait_for(service._timers[loop.id], timeout=60)

    assert len(fired) == 1, "a person's press is their own call, so it is not held"
    assert loop.id not in service._memory_held
    assert loop.id not in service._manual_ticks, "the press covers one tick only"


@pytest.mark.asyncio
async def test_a_tick_a_workers_push_armed_runs_at_critical(live, admission, monkeypatch):
    service, loop, fired = live
    admission["answer"] = CRITICAL

    armed, error, status = await service.fire_now(loop.id, defer_if_firing=True)
    assert (error, status) == ("", 200)
    await asyncio.wait_for(service._timers[loop.id], timeout=60)

    assert len(fired) == 1, "a pushed wake is how a conductor acts at critical"
    assert loop.id not in service._memory_held
    rearms: list[float | None] = []
    monkeypatch.setattr(service, "_arm_timer", lambda lp, delay=None: rearms.append(delay))

    await service._run_timer_callback(loop)

    assert len(fired) == 1, "the loop's own scheduled tick is still held"
    assert rearms == [memory_hold.CRITICAL_HOLD_RECHECK_SECS]


@pytest.mark.asyncio
async def test_the_next_automatic_tick_after_a_press_is_held(live, admission, monkeypatch):
    service, loop, fired = live
    admission["answer"] = CRITICAL
    await service.fire_now(loop.id)
    await asyncio.wait_for(service._timers[loop.id], timeout=60)
    rearms: list[float | None] = []
    monkeypatch.setattr(service, "_arm_timer", lambda lp, delay=None: rearms.append(delay))

    await service._run_timer_callback(loop)

    assert len(fired) == 1, "only the pressed tick ran"
    assert rearms == [memory_hold.CRITICAL_HOLD_RECHECK_SECS]


@pytest.mark.asyncio
async def test_a_later_automatic_arm_drops_the_press_mark(live, admission):
    service, loop, _fired = live
    service._manual_ticks.add(loop.id)

    service._arm_timer(loop, delay=300)

    assert loop.id not in service._manual_ticks


@pytest.mark.asyncio
async def test_a_structured_monitor_is_never_held(armed, admission):
    service, loop, _fired, rearms = armed
    loop.monitor = _structured_monitor()
    assert is_structured_monitor_loop(loop)
    admission["answer"] = CRITICAL

    assert await memory_hold.holds_for_critical_memory(service, loop) is False

    assert admission["reads"] == 0, "a structured monitor never reads admission"
    assert rearms == [], "nothing re-armed"
    assert loop.id not in service._memory_held


@pytest.mark.parametrize(
    ("admitted", "expected"),
    [(True, (True, "")), (False, (False, "host memory is critical"))],
)
def test_the_reader_relays_the_admission_check(monkeypatch, admitted, expected):
    from kiro_crew import resource_status

    decision = resource_status.AdmissionDecision(
        admitted=admitted,
        posture="ample" if admitted else "critical",
        available_gb=1.0,
        reason="" if admitted else "host memory is critical",
    )
    monkeypatch.setattr(resource_status, "admission_check", lambda: decision)
    assert asyncio.run(memory_hold.probe_admission()) == expected


def test_a_failing_admission_check_admits(monkeypatch):
    from kiro_crew import resource_status

    def _boom():
        raise RuntimeError("probe broke")

    monkeypatch.setattr(resource_status, "admission_check", _boom)
    assert asyncio.run(memory_hold.probe_admission()) == (True, "")
