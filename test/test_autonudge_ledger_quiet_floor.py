"""A work-ledger watch waits longer for its quiet floor while no worker has reported.

A gated loop is delivered anyway after ``_MAX_QUIET_STREAK`` quiet ticks. For a
work-ledger watch that turn re-reads a long conductor chat to find the board as the
last delivered turn left it. These tests pin the narrower rule: while the worker-report
revision is the one the last turn was delivered at, the floor is
``_IDLE_LEDGER_QUIET_FLOOR`` ticks, and it still delivers there. Every other path -- a
new report, a report or stall wake, a failed recent turn, another watch kind -- keeps
the shipped floor.
"""

from __future__ import annotations

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_service import gate as _gate
from kiro_crew.monitoring.models import MonitorState

_SLOT = "chat-1-123"
_FLOOR = _an._MAX_QUIET_STREAK
_IDLE_FLOOR = _gate._IDLE_LEDGER_QUIET_FLOOR


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


class _Ledger:
    """What the stubbed poll reports: the revision the probe read, and the outcome."""

    def __init__(self, revision: str) -> None:
        self.revision = revision
        self.outcome = _an.irq.Outcome.QUIET

    def poll(self, _identity, _message, probe):
        # Faithful to the real work-ledger probe: it publishes the revision it read on
        # the probe object and sets no pull-request observation.
        probe.revision = self.revision
        if self.outcome is _an.irq.Outcome.WAKE:
            return _an.irq.Verdict(_an.irq.Outcome.WAKE, "item stalled", ("stall:it_1:none",))
        return _an.irq.Verdict(self.outcome, "nothing new", ())


def _service(tmp_path, monkeypatch, ledger: _Ledger, fired: list[str]) -> AutoNudgeService:
    async def on_fire(loop):
        fired.append(loop.id)
        return True

    monkeypatch.setattr(_an.irq, "poll", ledger.poll)
    return AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)


def _loop(service: AutoNudgeService, *, kind: str = "work-ledger", **monitor_fields) -> NudgeLoop:
    if kind == "work-ledger":
        target, message = _SLOT, "wake the conductor from its work ledger"
    else:
        target, message = "acme/widgets#42", "watch https://github.com/acme/widgets/pull/42"
    monitor = MonitorState(
        kind=kind, target=target, objective="review_ready", created_ts=1_000.0, **monitor_fields
    )
    loop = NudgeLoop(
        id="monitor-floor",
        slot_key=_SLOT,
        message=message,
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop
    return loop


async def _ticks_until_floor(service: AutoNudgeService, loop: NudgeLoop, limit: int) -> int:
    """Quiet-tick until the floor asks for a turn; return how many ticks that took."""
    for count in range(1, limit + 1):
        if await service._monitor_tick_is_quiet(loop) is False:
            return count
    raise AssertionError(f"no floor within {limit} ticks")


def test_the_idle_floor_is_longer_than_the_shipped_one():
    assert _IDLE_FLOOR == 4 * _FLOOR


@pytest.mark.asyncio
async def test_an_unchanged_revision_waits_out_the_longer_floor_then_delivers(
    tmp_path, monkeypatch
):
    fired: list[str] = []
    ledger = _Ledger("rev-1")
    service = _service(tmp_path, monkeypatch, ledger, fired)
    loop = _loop(service, ledger_revision="rev-1", ledger_delivered_revision="rev-1")
    try:
        assert await _ticks_until_floor(service, loop, 100) == _IDLE_FLOOR
        monitor = loop.monitor
        assert monitor is not None
        assert monitor.quiet_ticks == _IDLE_FLOOR and monitor.floor_fire_pending is True
        await service._run_fire_cycle(loop)
        assert fired == [loop.id] and monitor.floor_ticks == 1
        # And the spacing holds after the delivery: the board is still unchanged.
        assert await _ticks_until_floor(service, loop, 100) == _IDLE_FLOOR
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_changed_revision_keeps_the_shipped_floor(tmp_path, monkeypatch):
    fired: list[str] = []
    ledger = _Ledger("rev-2")
    service = _service(tmp_path, monkeypatch, ledger, fired)
    loop = _loop(service, ledger_revision="rev-1", ledger_delivered_revision="rev-1")
    try:
        assert await _ticks_until_floor(service, loop, 100) == _FLOOR
        await service._run_fire_cycle(loop)
        assert fired == [loop.id]
        assert loop.monitor is not None
        assert loop.monitor.ledger_delivered_revision == "rev-2", "the delivered revision is kept"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_report_mid_streak_brings_the_floor_back_at_once(tmp_path, monkeypatch):
    fired: list[str] = []
    ledger = _Ledger("rev-1")
    service = _service(tmp_path, monkeypatch, ledger, fired)
    loop = _loop(service, ledger_revision="rev-1", ledger_delivered_revision="rev-1")
    try:
        for _ in range(2 * _FLOOR):
            assert await service._monitor_tick_is_quiet(loop) is True
        ledger.revision = "rev-2"
        assert await service._monitor_tick_is_quiet(loop) is False, "past the shipped floor"
    finally:
        service.stop()


@pytest.mark.parametrize(
    "failure_field", ["consecutive_start_failures", "consecutive_failed_cycles"]
)
@pytest.mark.asyncio
async def test_a_failed_recent_turn_keeps_the_shipped_floor(tmp_path, monkeypatch, failure_field):
    # "Delivered" means dispatched: a turn that then failed never read the board,
    # so an unchanged revision must not stretch the floor that retries it.
    fired: list[str] = []
    ledger = _Ledger("rev-1")
    service = _service(tmp_path, monkeypatch, ledger, fired)
    loop = _loop(service, ledger_revision="rev-1", ledger_delivered_revision="rev-1")
    setattr(loop, failure_field, 1)
    try:
        assert await _ticks_until_floor(service, loop, 100) == _FLOOR
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_an_unknown_delivered_revision_keeps_the_shipped_floor(tmp_path, monkeypatch):
    fired: list[str] = []
    ledger = _Ledger("rev-1")
    service = _service(tmp_path, monkeypatch, ledger, fired)
    loop = _loop(service)
    try:
        assert await _ticks_until_floor(service, loop, 100) == _FLOOR
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_stall_wake_still_delivers(tmp_path, monkeypatch):
    fired: list[str] = []
    ledger = _Ledger("rev-1")
    ledger.outcome = _an.irq.Outcome.WAKE
    service = _service(tmp_path, monkeypatch, ledger, fired)
    loop = _loop(service, ledger_revision="rev-1", ledger_delivered_revision="rev-1")
    try:
        assert await service._monitor_tick_is_quiet(loop) is False, "a stall wake fires"
        await service._run_fire_cycle(loop)
        assert loop.monitor is not None
        assert fired == [loop.id]
        assert loop.monitor.wakes == 1
        assert loop.monitor.followup_ticks == 0, "a work-ledger wake buys no free tick"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_pull_request_wake_keeps_its_follow_up_tick(tmp_path, monkeypatch):
    fired: list[str] = []
    ledger = _Ledger("rev-1")
    ledger.outcome = _an.irq.Outcome.WAKE
    service = _service(tmp_path, monkeypatch, ledger, fired)
    loop = _loop(service, kind="gh-pr")
    try:
        assert await service._monitor_tick_is_quiet(loop) is False, "the wake fires"
        await service._run_fire_cycle(loop)
        assert loop.monitor is not None and loop.monitor.wakes == 1
        assert loop.monitor.followup_ticks == _gate._WAKE_FOLLOWUP_TICKS
        ledger.outcome = _an.irq.Outcome.QUIET
        assert await service._monitor_tick_is_quiet(loop) is False, "the follow-up tick fires"
        assert await service._monitor_tick_is_quiet(loop) is True, "then the gate observes"
    finally:
        service.stop()


async def _deliver_a_wake(service, ledger, loop) -> None:
    ledger.outcome = _an.irq.Outcome.WAKE
    assert await service._monitor_tick_is_quiet(loop) is False, "a worker's news wakes"
    await service._run_fire_cycle(loop)
    assert loop.monitor is not None and loop.monitor.wakes >= 1
    ledger.outcome = _an.irq.Outcome.QUIET


@pytest.mark.asyncio
async def test_a_woken_turn_that_fails_is_retried_once(tmp_path, monkeypatch):
    # While the woken turn runs nothing has failed, so a tick then buys nothing; a turn
    # whose end is never reported cannot make the loop fire on every tick either.
    fired: list[str] = []
    ledger = _Ledger("rev-1")
    service = _service(tmp_path, monkeypatch, ledger, fired)
    loop = _loop(service, ledger_revision="rev-1", ledger_delivered_revision="rev-1")
    try:
        await _deliver_a_wake(service, ledger, loop)
        assert await service._monitor_tick_is_quiet(loop) is True, "the turn is still running"
        loop.consecutive_failed_cycles = 1
        assert await service._monitor_tick_is_quiet(loop) is False, "the dead wake is retried"
        for _ in range(3):
            assert await service._monitor_tick_is_quiet(loop) is True, "once"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_wake_inside_a_failure_streak_is_retried_when_its_own_turn_fails(
    tmp_path, monkeypatch
):
    fired: list[str] = []
    ledger = _Ledger("rev-1")
    service = _service(tmp_path, monkeypatch, ledger, fired)
    loop = _loop(service, ledger_revision="rev-1", ledger_delivered_revision="rev-1")
    loop.consecutive_failed_cycles = 2
    try:
        await _deliver_a_wake(service, ledger, loop)
        assert await service._monitor_tick_is_quiet(loop) is True, "the old streak buys nothing"
        loop.consecutive_failed_cycles = 3
        assert await service._monitor_tick_is_quiet(loop) is False, "this wake's turn failed"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_landed_wake_owes_no_retry_to_a_later_failed_turn(tmp_path, monkeypatch):
    fired: list[str] = []
    ledger = _Ledger("rev-1")
    service = _service(tmp_path, monkeypatch, ledger, fired)
    loop = _loop(service, ledger_revision="rev-1", ledger_delivered_revision="rev-1")
    try:
        await _deliver_a_wake(service, ledger, loop)
        service.notify_cycle_landed(_SLOT)
        loop.consecutive_failed_cycles = 1
        assert await service._monitor_tick_is_quiet(loop) is True, "a floor turn's failure"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_other_watch_kinds_keep_the_shipped_floor(tmp_path, monkeypatch):
    fired: list[str] = []
    ledger = _Ledger("rev-1")
    service = _service(tmp_path, monkeypatch, ledger, fired)
    loop = _loop(service, kind="gh-pr", ledger_revision="rev-1", ledger_delivered_revision="rev-1")
    try:
        assert await _ticks_until_floor(service, loop, 100) == _FLOOR
    finally:
        service.stop()


def test_unreadable_stored_revisions_resolve_toward_delivering():
    monitor = MonitorState(
        kind="work-ledger",
        target=_SLOT,
        objective="review_ready",
        created_ts=1_000.0,
        ledger_revision=7,  # type: ignore[arg-type]
        ledger_delivered_revision=None,  # type: ignore[arg-type]
    )
    assert monitor.ledger_revision == ""
    assert monitor.ledger_delivered_revision == ""
