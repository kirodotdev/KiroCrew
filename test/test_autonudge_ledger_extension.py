"""A work-ledger watch with open items is not ended by its cycle cap or runtime budget.

The conductor's patrol ends when its ledger does (every item closed is the probe's
own terminal settlement) or when a user or agent stops it. A spent bound on a ledger
that still has open work is raised server-side instead, bounded by the runaway
backstop (the configured monitoring runtime ceiling).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_service import firing, model


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _unpublish():
    """Same teardown as the approval-stall suite: no service outlives its test."""
    yield
    svc = _an.get_instance()
    if svc is None:
        return
    try:
        for task in list(getattr(svc, "_inflight_adds", ())):
            task.cancel()
        svc.stop()
    finally:
        _an._INSTANCE = None


@pytest.fixture
def svc(tmp_path_factory):
    return AutoNudgeService(base_dir=tmp_path_factory.mktemp("autonudge-ledger"))


@pytest.fixture
def _nosleep(monkeypatch):
    async def _noop(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _noop)


@pytest.fixture
def ledger(monkeypatch):
    """Make every loop a work-ledger watch whose ledger answers ``state["open"]``."""
    state = {"open": True, "backstop": 7 * 24 * 3600, "asked": []}

    def _open(key: str) -> bool:
        state["asked"].append(key)
        return state["open"]

    monkeypatch.setattr(firing, "_ledger_has_open_items", _open)
    monkeypatch.setattr(firing, "_ledger_backstop_secs", lambda: state["backstop"])
    monkeypatch.setattr(AutoNudgeService, "_observes_work_ledger", lambda self, loop: True)
    return state


def _freeze_clock(monkeypatch, now: float) -> None:
    """Pin the timer's and the budget's own clock, not the shared ``time`` module.

    Each module gets a namespace whose ``time()`` answers *now* and whose other
    names are the real module's, so nothing else in the worker sees the pin.
    """
    real = firing.time
    frozen = SimpleNamespace(**{n: getattr(real, n) for n in dir(real) if not n.startswith("__")})
    frozen.time = lambda: now
    monkeypatch.setattr(firing, "time", frozen)
    monkeypatch.setattr(model, "time", frozen)


async def _armed(svc, **kwargs) -> NudgeLoop:
    await svc.start()
    loop = await svc.add(slot_key="chat-7-777", message="patrol", idle_secs=300, **kwargs)
    await svc._timers[loop.id]
    return loop


async def _drain(svc: AutoNudgeService) -> None:
    timers = list(svc._timers.values())
    svc.stop()
    if timers:
        await asyncio.gather(*timers, return_exceptions=True)
    inflight = list(svc._inflight_adds)
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)


@pytest.mark.asyncio
async def test_spent_cycle_cap_with_open_items_is_extended(svc, _nosleep, ledger, caplog):
    loop = await _armed(svc, max_cycles=200)
    loop.cycle_count = 200
    svc._cancel_timer(loop.id)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        await svc._timer(loop)

    refreshed = svc._loops[loop.id]
    assert refreshed.active is True, "a ledger with open work must not end the patrol"
    assert refreshed.stopped_reason == ""
    assert refreshed.max_cycles == 250, "extended by a quarter of the cap"
    assert ledger["asked"] == ["chat-7-777"], "the ledger read must name the conductor"
    assert any("extended it to 250" in r.getMessage() for r in caplog.records)
    assert refreshed.id in svc._timers and not svc._timers[refreshed.id].done()
    await _drain(svc)


@pytest.mark.asyncio
async def test_small_cap_is_extended_by_at_least_the_floor(svc, _nosleep, ledger):
    loop = await _armed(svc, max_cycles=4)
    loop.cycle_count = 4
    svc._cancel_timer(loop.id)

    await svc._timer(loop)

    assert svc._loops[loop.id].max_cycles == 4 + firing._LEDGER_EXTEND_MIN_CYCLES
    await _drain(svc)


@pytest.mark.asyncio
async def test_finished_ledger_keeps_the_cycle_cap_stop(svc, _nosleep, ledger):
    ledger["open"] = False
    loop = await _armed(svc, max_cycles=3)
    loop.cycle_count = 3
    svc._cancel_timer(loop.id)

    await svc._timer(loop)

    refreshed = svc._loops[loop.id]
    assert refreshed.active is False
    assert refreshed.stopped_reason == "cycle_cap"
    assert refreshed.max_cycles == 3
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_loop_that_is_not_a_ledger_watch_stops_as_before(
    svc, _nosleep, ledger, monkeypatch
):
    monkeypatch.setattr(AutoNudgeService, "_observes_work_ledger", lambda self, loop: False)
    loop = await _armed(svc, max_cycles=3)
    loop.cycle_count = 3
    svc._cancel_timer(loop.id)

    await svc._timer(loop)

    assert svc._loops[loop.id].stopped_reason == "cycle_cap"
    assert ledger["asked"] == [], "a non-ledger loop must not read any ledger"
    await _drain(svc)


@pytest.mark.asyncio
async def test_past_the_runaway_backstop_nothing_is_extended(svc, _nosleep, ledger, caplog):
    ledger["backstop"] = 3600
    loop = await _armed(svc, max_cycles=3)
    loop.cycle_count = 3
    loop.created_ts -= 3601
    svc._cancel_timer(loop.id)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        await svc._timer(loop)

    refreshed = svc._loops[loop.id]
    assert refreshed.active is False
    assert refreshed.stopped_reason == "cycle_cap"
    assert any("runaway backstop" in r.getMessage() for r in caplog.records)
    await _drain(svc)


@pytest.mark.asyncio
async def test_spent_runtime_budget_with_open_items_is_extended(svc, _nosleep, ledger, monkeypatch):
    loop = await _armed(svc, max_runtime_secs=7200)
    now = 1_900_000_000.0
    _freeze_clock(monkeypatch, now)
    loop.created_ts = now - 7300
    svc._cancel_timer(loop.id)

    await svc._timer(loop)

    refreshed = svc._loops[loop.id]
    assert refreshed.active is True
    assert refreshed.stopped_reason == ""
    # The loop's age plus a quarter of the budget, never less than an hour.
    assert refreshed.max_runtime_secs == 7300 + 3600
    assert not _an.runtime_budget_exceeded(refreshed, now=now)
    await _drain(svc)


@pytest.mark.asyncio
async def test_runtime_extension_is_clamped_to_the_backstop(svc, _nosleep, ledger, monkeypatch):
    ledger["backstop"] = 8000
    loop = await _armed(svc, max_runtime_secs=7200)
    now = 1_900_000_000.0
    _freeze_clock(monkeypatch, now)
    loop.created_ts = now - 7300
    svc._cancel_timer(loop.id)

    await svc._timer(loop)

    assert svc._loops[loop.id].max_runtime_secs == 8000
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_user_stop_is_not_overridden(svc, _nosleep, ledger):
    """The extension runs only where the timer applies a bound, never on a stop."""
    loop = await _armed(svc, max_cycles=3)
    await svc.update(loop.id, active=False)

    loop.cycle_count = 3
    await svc._timer(loop)

    refreshed = svc._loops[loop.id]
    assert refreshed.active is False
    assert refreshed.stopped_reason == "manual"
    assert refreshed.max_cycles == 3
    await _drain(svc)


def test_open_items_reads_positive_evidence_only(monkeypatch):
    from kiro_crew.probes import work_ledger as probe

    work_ledger = probe.work_ledger

    items = [SimpleNamespace(is_terminal=True), SimpleNamespace(is_terminal=False)]
    monkeypatch.setattr(work_ledger, "list_work_items", lambda key: items)
    assert firing._ledger_has_open_items("chat-7-777") is True

    monkeypatch.setattr(
        work_ledger, "list_work_items", lambda key: [SimpleNamespace(is_terminal=True)]
    )
    assert firing._ledger_has_open_items("chat-7-777") is False

    monkeypatch.setattr(work_ledger, "list_work_items", lambda key: [])
    assert firing._ledger_has_open_items("chat-7-777") is False

    def _boom(key):
        raise OSError("torn")

    monkeypatch.setattr(work_ledger, "list_work_items", _boom)
    assert firing._ledger_has_open_items("chat-7-777") is False


def test_backstop_falls_back_to_seven_days(monkeypatch):
    from kiro_crew.monitoring import limits

    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(limits, "runtime_ceiling_secs", _boom)
    assert firing._ledger_backstop_secs() == limits.DEFAULT_RUNTIME_CEILING_SECS


@pytest.mark.asyncio
async def test_an_armed_tick_survives_its_own_update_and_logs(svc, _nosleep, ledger, caplog):
    """The real path: the tick runs as the loop's registered timer task.

    ``update`` cancels the registered timer from its shielded task, so a tick that
    stayed registered was cancelled mid-await and its WARNING never ran.
    """
    loop = await _armed(svc, max_cycles=8)
    loop.cycle_count = 8
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        svc._arm_timer(loop, delay=0)
        tick = svc._timers[loop.id]
        await asyncio.gather(tick, return_exceptions=True)

    assert not tick.cancelled(), "the update cancelled the tick that called it"
    assert svc._loops[loop.id].max_cycles == 18
    assert any("extended it to 18" in r.getMessage() for r in caplog.records)
    await _drain(svc)
