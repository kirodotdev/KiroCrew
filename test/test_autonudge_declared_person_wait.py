"""A legacy loop's agent declares it is waiting on a person (``waiting_on_person``).

The declared hold fires no cycle and spends neither the cycle cap nor the runtime
budget while a person or the agent ends it. A loop that names one pull request reads
it each held tick and settles when it merges or closes; one that names none reads
nothing and ticks at the hold's expiry. A person's return ends it -- a message typed
into the dashboard chat, an answered approval, the popover's Nudge now -- and so does
the agent passing ``false``. The work ledger neither holds nor releases it. It expires
a day after it began, or when the runtime budget runs out if sooner, charged to that
budget.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import AutoNudgeService, NudgeAdmissionRefused, NudgeLoop
from kiro_crew.autonudge_service import firing, model, subject_reads, timers
from kiro_crew.autonudge_service.model import (
    MONITOR_TERMINAL_REASON,
    PERSON_WAIT_MAX_SECS,
    SUBJECT_READ_SECS,
)
from kiro_crew.dashboard import responsive_chat, session_control
from kiro_crew.dashboard.handlers import autonudge as autonudge_routes
from kiro_crew.dashboard.responsive_chat import turn_state
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.monitoring.models import MonitorBudgets
from kiro_crew.probes import gh_pr
from kiro_crew.testing.wait import async_wait_until, await_until, until_parked

SLOT = "chat-7-777"
SESSION = f"dashboard:{SLOT}"
PR_URL = "https://github.com/acme/widgets/pull/7"


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _unpublish():
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
def base(tmp_path_factory):
    return tmp_path_factory.mktemp("autonudge-declared-wait")


@pytest.fixture
def svc(base):
    return AutoNudgeService(base_dir=base)


@pytest.fixture
def _nosleep(monkeypatch):
    """The timer's own wait, skipped: each test drives ticks by hand."""

    async def _noop(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _noop)  # flake-ok: no wall clock is read


async def _armed(svc, fired: list, **kwargs) -> NudgeLoop:
    async def on_fire(loop):
        fired.append(loop.cycle_count)
        return True

    await svc.start()
    svc._on_fire = on_fire
    loop = await svc.add(slot_key=SLOT, message="babysit", idle_secs=300, **kwargs)
    await svc._timers[loop.id]
    return loop


async def _tick(svc, loop) -> None:
    svc._cancel_timer(loop.id)
    await svc._timer(loop)


async def _settle(svc) -> None:
    """Await every detached release the service scheduled."""
    tasks = list(svc._inflight_adds)
    if tasks:
        await await_until(
            asyncio.gather(*tasks, return_exceptions=True), what="the detached releases"
        )


async def _drain(svc: AutoNudgeService) -> None:
    pending = list(svc._timers.values())
    svc.stop()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _no_live_timer(svc, loop) -> bool:
    timer = svc._timers.get(loop.id)
    return timer is None or timer.done() or timer.cancelled()


def _pin_clock(monkeypatch, now: float) -> None:
    """Pin the hold and runtime-budget wall and monotonic clocks."""
    real = firing.time
    pinned = SimpleNamespace(**{n: getattr(real, n) for n in dir(real) if not n.startswith("__")})
    pinned.time = lambda: now
    pinned.monotonic = lambda: now
    monkeypatch.setattr(firing, "time", pinned)
    monkeypatch.setattr(timers, "time", pinned)
    monkeypatch.setattr(subject_reads, "time", pinned)
    monkeypatch.setattr(model, "time", pinned)


class _SuspendClock:
    """A wall clock that can jump while its monotonic reading stays put."""

    def __init__(self) -> None:
        self.wall = 1_800_000_000.0
        self.mono = 5_000.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def __getattr__(self, name: str):
        return getattr(time, name)


# --------------------------------------------------------------------------- #
# The hold on the timer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_declared_hold_fires_nothing_and_spends_nothing(svc, _nosleep, monkeypatch):
    fired: list = []
    loop = await _armed(svc, fired, max_cycles=3, max_runtime_secs=3600)
    assert len(fired) == 1
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    armed: list = []
    monkeypatch.setattr(svc, "_arm_timer", lambda lp, delay=None: armed.append(delay))

    assert await svc.declare_person_wait(SLOT, held=True)
    held = svc._loops[loop.id]
    assert held.waiting_on_person is True and held.person_wait_declared is True
    assert held.waiting_on_person_at > 0
    assert "updated" in events
    expires_at = _an.person_wait_expires_at(held)
    assert expires_at is not None and expires_at > held.waiting_on_person_at
    assert len(armed) == 1 and 0 < armed[0] <= 3600, "one tick, at the hold's expiry"

    cycles = held.cycle_count
    held.cycle_count = held.max_cycles  # a spent cap does not stop a held loop
    await _tick(svc, loop)
    await _tick(svc, loop)
    assert len(fired) == 1, "a declared hold fires no cycle"
    assert held.active is True and held.stopped_reason == ""
    assert len(armed) == 3 and all(0 < d <= 3600 for d in armed), "each tick re-arms its expiry"
    held.cycle_count = cycles

    reading = autonudge_routes._autonudge_loop_reading(held)
    assert reading["waiting_on_person"] is True
    assert reading["person_wait_declared"] is True
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_suspend_before_declaration_keeps_the_monotonic_runtime_remainder(svc, monkeypatch):
    clock = _SuspendClock()
    for module in (firing, timers, subject_reads, model):
        monkeypatch.setattr(module, "time", clock)
    await svc.start()
    loop = await svc.add(
        slot_key=SLOT,
        message="babysit",
        idle_secs=1800,
        max_runtime_secs=3600,
    )
    svc._cancel_timer(loop.id)
    armed: list[float | None] = []
    monkeypatch.setattr(svc, "_arm_timer", lambda lp, delay=None: armed.append(delay))

    clock.wall += 600.0
    clock.mono += 600.0
    assert model.runtime_elapsed(loop, loop.created_ts) == 600.0
    clock.wall += 2 * 3600.0  # Linux/macOS suspend: monotonic does not advance.

    assert await svc.declare_person_wait(SLOT, held=True)

    assert armed == [3000.0]
    assert model.person_wait_expires_at(loop) == clock.wall + 3000.0
    assert model.person_wait_ends_on_budget(loop) is True
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_long_declared_hold_hands_its_time_back(svc, monkeypatch):
    t = 2_000_000_000.0
    loop, _armed, _fired = await _declared_plain(
        svc, monkeypatch, now=t - 7200, max_runtime_secs=10800
    )
    loop.created_ts = t - 7300
    assert await svc.declare_person_wait(SLOT, held=True)
    created = loop.created_ts

    _pin_clock(monkeypatch, t)
    assert await svc.release_approval_hold(SLOT, why="test", arm=False)
    assert loop.created_ts - created == 7200.0, "the whole wait is credited"
    assert loop.waiting_on_person is False and loop.person_wait_declared is False
    assert loop.waiting_on_person_at == 0.0
    await _drain(svc)


@pytest.mark.asyncio
async def test_declaring_over_a_ledger_hold_credits_it_and_starts_a_fresh_wait(svc, monkeypatch):
    t = _whole_now()
    loop, _armed, _fired = await _declared_plain(svc, monkeypatch, now=t)
    ledger_since = t - 23 * 3600
    created = t - 25 * 3600
    loop.created_ts = created
    loop.waiting_on_person = True
    loop.waiting_on_person_at = ledger_since
    loop.person_wait_declared = False
    elapsed_before = model.runtime_elapsed(loop, created)

    assert await svc.declare_person_wait(SLOT, held=True)

    assert loop.person_wait_declared is True
    assert loop.waiting_on_person_at == t
    assert loop.created_ts == created + 23 * 3600
    assert _an.person_wait_expires_at(loop) == t + PERSON_WAIT_MAX_SECS
    assert model.runtime_elapsed(loop, loop.created_ts) == elapsed_before - 23 * 3600
    stored = json.loads(svc._store.path.read_text())["loops"][0]
    assert stored["waiting_on_person_at"] == t
    assert stored["created_ts"] == created + 23 * 3600
    await _drain(svc)


@pytest.mark.asyncio
async def test_redeclaring_an_already_declared_hold_keeps_its_start(svc, monkeypatch):
    t = _whole_now()
    loop, _armed, _fired = await _declared_plain(svc, monkeypatch, now=t - 600)
    assert await svc.declare_person_wait(SLOT, held=True)
    since = loop.waiting_on_person_at
    expires_at = _an.person_wait_expires_at(loop)
    writer = AsyncMock()
    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", writer)

    _pin_clock(monkeypatch, t)
    assert await svc.declare_person_wait(SLOT, held=True)

    assert loop.waiting_on_person_at == since
    assert _an.person_wait_expires_at(loop) == expires_at
    writer.assert_not_awaited()
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_person_s_message_releases_and_an_app_s_does_not(svc, _nosleep):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)

    svc.notify_user_input(SLOT, human=False)
    await _settle(svc)
    assert svc._loops[loop.id].person_wait_declared is True, "an app token is not a person"

    svc.notify_user_input(SLOT, human=True)
    await _settle(svc)
    refreshed = svc._loops[loop.id]
    assert refreshed.person_wait_declared is False and refreshed.waiting_on_person is False

    # The person's own turn ends and re-arms the loop, which then fires as usual.
    svc.notify_turn_complete(SLOT)
    await svc._timers[loop.id]
    assert len(fired) == 2
    await _drain(svc)


@pytest.mark.asyncio
async def test_an_answered_approval_releases(svc, _nosleep):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    assert _an.get_instance() is svc

    _an.release_approval_hold_for(SLOT, why="an approval was answered")
    await _settle(svc)
    assert svc._loops[loop.id].person_wait_declared is False
    await _drain(svc)


@pytest.mark.asyncio
async def test_fire_now_releases_through_the_handler_order(svc, monkeypatch):
    loop, armed, _fired = await _declared_plain(svc, monkeypatch, now=_whole_now())
    assert await svc.declare_person_wait(SLOT, held=True)

    # The route's own order: release the hold unarmed, then bring the cycle forward.
    armed.clear()
    assert await svc.release_approval_hold(SLOT, why="fired by hand", arm=False)
    _loop, _err, status = await svc.fire_now(loop.id)
    assert status == 200
    assert armed == [0.0]
    await _drain(svc)


@pytest.mark.asyncio
async def test_the_ledger_neither_holds_nor_releases_a_declared_hold(svc, _nosleep, monkeypatch):
    state = {"fp": None, "reads": 0}

    def _fp(_key, *_a):
        state["reads"] += 1
        return state["fp"]

    monkeypatch.setattr(firing, "_ledger_person_wait_fp", _fp)
    monkeypatch.setattr(AutoNudgeService, "_observes_work_ledger", lambda self, loop: True)
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    reads = state["reads"]

    state["fp"] = "fp-moved"
    await _tick(svc, loop)
    assert svc._loops[loop.id].person_wait_declared is True
    assert state["reads"] == reads, "a declared hold never reads the ledger"
    assert await svc._holds_for_person_wait(svc._loops[loop.id]) is False
    assert len(fired) == 1
    await _drain(svc)


@pytest.mark.asyncio
async def test_the_reconciler_rescues_a_declared_hold_with_nothing_to_read(svc, monkeypatch):
    t = _whole_now()
    loop, armed, _fired = await _declared_plain(svc, monkeypatch, now=t)
    assert await svc.declare_person_wait(SLOT, held=True)
    armed.clear()
    svc._timers.pop(loop.id, None)  # its expiry tick died
    svc._reconcile_once()
    assert armed == [], "the first pass only marks the candidate"
    svc._reconcile_once()
    assert len(armed) == 1 and 0 < armed[0] <= PERSON_WAIT_MAX_SECS
    await _drain(svc)


# --------------------------------------------------------------------------- #
# A declared hold expires
# --------------------------------------------------------------------------- #


def _whole_now() -> float:
    """The wall clock in whole seconds, so expiry arithmetic compares exactly."""
    return float(int(time.time()))


def _pin_budget_clock(monkeypatch, loop: NudgeLoop, now: float) -> None:
    """Pin the runtime budget's wall clock too, and drop its anchor so it reads it."""
    real = model.time
    pinned = SimpleNamespace(**{n: getattr(real, n) for n in dir(real) if not n.startswith("__")})
    pinned.time = lambda: now
    monkeypatch.setattr(model, "time", pinned)
    loop.__dict__.pop("_runtime_anchor", None)


async def _declared_plain(svc, monkeypatch, *, now: float, message: str = "babysit", **kwargs):
    """A started service with one loop (not yet held), its arms and fires recorded."""
    fired: list = []

    async def on_fire(lp):
        fired.append(lp.cycle_count)
        return True

    await svc.start()
    svc._on_fire = on_fire
    loop = await svc.add(slot_key=SLOT, message=message, idle_secs=1800, **kwargs)
    svc._cancel_timer(loop.id)
    armed: list = []
    monkeypatch.setattr(svc, "_arm_timer", lambda lp, delay=None: armed.append(delay))
    _pin_clock(monkeypatch, now)
    return svc._loops[loop.id], armed, fired


@pytest.mark.asyncio
async def test_an_unanswered_declared_wait_expires_after_a_day_and_fires(svc, monkeypatch):
    t = _whole_now()
    loop, armed, fired = await _declared_plain(svc, monkeypatch, now=t)
    assert await svc.declare_person_wait(SLOT, held=True)
    assert armed == [float(PERSON_WAIT_MAX_SECS)], "one tick, at the expiry instant"
    assert _an.person_wait_expires_at(loop) == t + PERSON_WAIT_MAX_SECS
    created = loop.created_ts
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))

    armed.clear()
    _pin_clock(monkeypatch, t + PERSON_WAIT_MAX_SECS - 1)
    await svc._timer(loop, 0)
    assert loop.person_wait_declared is True and fired == []
    assert armed == [1.0], "a tick before the expiry re-arms at it"

    _pin_clock(monkeypatch, t + PERSON_WAIT_MAX_SECS)
    await svc._timer(loop, 0)
    assert loop.waiting_on_person is False and loop.person_wait_declared is False
    assert loop.waiting_on_person_at == 0.0
    assert loop.person_wait_release == "expired"
    assert loop.created_ts == created, "an expired wait is charged, not credited"
    assert "updated" in events
    assert len(fired) == 1, "the agent gets a turn"
    assert loop.active is True
    stored = json.loads(svc._store.path.read_text())["loops"][0]
    assert stored["person_wait_release"] == "expired"
    assert stored["person_wait_declared"] is False and stored["waiting_on_person"] is False
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_declared_hold_with_a_lost_start_stamp_expires_without_crashing(svc, monkeypatch):
    """A damaged row (no start stamp) expires at once and the tick still completes."""
    t = _whole_now()
    loop, _armed, fired = await _declared_plain(svc, monkeypatch, now=t)
    assert await svc.declare_person_wait(SLOT, held=True)
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    loop.waiting_on_person_at = None  # what a damaged row loads as

    _pin_clock(monkeypatch, t + 1)
    await svc._timer(loop, 0)
    assert loop.person_wait_declared is False and loop.waiting_on_person is False
    assert loop.person_wait_release == "expired"
    assert "updated" in events, "the state notification still goes out"
    assert len(fired) == 1, "the tick goes on to its cycle"
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_declared_wait_expires_with_the_budget_and_the_tick_stops_the_loop(
    svc, monkeypatch
):
    t = _whole_now()
    loop, armed, fired = await _declared_plain(svc, monkeypatch, now=t, max_runtime_secs=3600)
    loop.created_ts = t - 3000.0  # 600 seconds of budget left
    assert await svc.declare_person_wait(SLOT, held=True)
    assert armed == [600.0]
    assert _an.person_wait_expires_at(loop) == t + 600.0

    _pin_clock(monkeypatch, t + 600.0)
    _pin_budget_clock(monkeypatch, loop, t + 600.0)
    await svc._timer(loop, 0)

    assert fired == [], "a spent budget buys no turn"
    assert loop.active is False and loop.stopped_reason == "runtime_budget"
    assert loop.person_wait_release == "expired"
    assert loop.created_ts == t - 3000.0
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_person_release_after_the_deadline_expires_without_refunding_budget(
    svc, monkeypatch
):
    t = _whole_now()
    loop, _armed, fired = await _declared_plain(svc, monkeypatch, now=t, max_runtime_secs=3600)
    loop.created_ts = t - 3000.0  # the wait's deadline is 600 seconds away
    assert await svc.declare_person_wait(SLOT, held=True)
    created = loop.created_ts

    # An app-origin send cancels the held timer but does not release the wait.
    svc.notify_user_input(SLOT, human=False)
    _pin_clock(monkeypatch, t + 601.0)
    _pin_budget_clock(monkeypatch, loop, t + 601.0)
    svc.notify_user_input(SLOT, human=True)
    await _settle(svc)

    assert loop.person_wait_release == "expired", "a late person reply is an expiry"
    assert loop.created_ts == created, "an expired wait receives no runtime refund"
    await svc._timer(loop, 0)
    assert fired == []
    assert loop.active is False and loop.stopped_reason == "runtime_budget"
    await _drain(svc)


@pytest.mark.asyncio
async def test_an_agent_release_after_the_deadline_expires_without_refunding_budget(
    svc, monkeypatch
):
    t = _whole_now()
    loop, _armed, fired = await _declared_plain(svc, monkeypatch, now=t, max_runtime_secs=3600)
    loop.created_ts = t - 3000.0  # the wait's deadline is 600 seconds away
    assert await svc.declare_person_wait(SLOT, held=True)
    created = loop.created_ts

    _pin_clock(monkeypatch, t + 601.0)
    _pin_budget_clock(monkeypatch, loop, t + 601.0)
    assert await svc.declare_person_wait(SLOT, held=False)

    assert loop.person_wait_release == "expired", "a late agent release is an expiry"
    assert loop.created_ts == created, "an expired wait receives no runtime refund"
    await svc._timer(loop, 0)
    assert fired == []
    assert loop.active is False and loop.stopped_reason == "runtime_budget"
    await _drain(svc)


def _pin_ticking_clock(monkeypatch, start: float) -> None:
    """Pin every clock to one that moves a millisecond forward on each read.

    A real clock never reads the same twice, so an exhausted budget's projected
    expiry ("now plus nothing") lands after any reading taken before it.
    """
    real = firing.time
    reading = [start]

    def _read() -> float:
        reading[0] += 0.001
        return reading[0]

    pinned = SimpleNamespace(**{n: getattr(real, n) for n in dir(real) if not n.startswith("__")})
    pinned.time = _read
    pinned.monotonic = _read
    for module in (firing, timers, subject_reads, model):
        monkeypatch.setattr(module, "time", pinned)


@pytest.mark.asyncio
async def test_a_late_person_release_on_a_spent_live_budget_is_an_expiry(svc, monkeypatch):
    """The budget is read on this process's clock, which keeps moving during the check."""
    t = _whole_now()
    loop, _armed, fired = await _declared_plain(svc, monkeypatch, now=t, max_runtime_secs=3600)
    loop.created_ts = t - 3000.0  # 600 seconds of budget left
    model.runtime_elapsed(loop, loop.created_ts)  # the live anchor, as a running loop has
    assert await svc.declare_person_wait(SLOT, held=True)
    created = loop.created_ts

    svc.notify_user_input(SLOT, human=False)
    _pin_ticking_clock(monkeypatch, t + 601.0)
    svc.notify_user_input(SLOT, human=True)
    await _settle(svc)

    assert loop.person_wait_release == "expired", "a reply after the budget ran out is an expiry"
    assert loop.created_ts == created, "a spent budget is not handed back"
    await svc._timer(loop, 0)
    assert fired == []
    assert loop.active is False and loop.stopped_reason == "runtime_budget"
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_late_agent_release_on_a_spent_live_budget_is_an_expiry(svc, monkeypatch):
    t = _whole_now()
    loop, _armed, fired = await _declared_plain(svc, monkeypatch, now=t, max_runtime_secs=3600)
    loop.created_ts = t - 3000.0  # 600 seconds of budget left
    model.runtime_elapsed(loop, loop.created_ts)  # the live anchor, as a running loop has
    assert await svc.declare_person_wait(SLOT, held=True)
    created = loop.created_ts

    _pin_ticking_clock(monkeypatch, t + 601.0)
    assert await svc.declare_person_wait(SLOT, held=False)

    assert loop.person_wait_release == "expired", "an agent release after the budget ran out"
    assert loop.created_ts == created, "a spent budget is not handed back"
    await svc._timer(loop, 0)
    assert fired == []
    assert loop.active is False and loop.stopped_reason == "runtime_budget"
    await _drain(svc)


@pytest.mark.asyncio
async def test_an_ungated_declared_wait_sleeps_without_reading_its_pull_request(monkeypatch):
    """Its expiry sleep is one plain sleep: only a gated loop's hold reads at all."""
    loop = NudgeLoop(
        id="l1",
        slot_key=SLOT,
        message=f"babysit {PR_URL}",
        idle_secs=1800,
        gate=False,
        person_wait_declared=True,
        waiting_on_person=True,
        waiting_on_person_at=time.time(),
    )
    naps: list[float] = []
    reads: list[str] = []

    async def _nap(secs: float) -> None:
        naps.append(secs)

    async def _read(message: str):
        reads.append(message)
        return None

    monkeypatch.setattr(subject_reads, "_nap", _nap)
    monkeypatch.setattr(subject_reads, "_read_state", _read)

    await subject_reads.sleep_until_cycle(SimpleNamespace(_subject_wake_fp={}), loop, 3600.0)

    assert reads == [], "a held ungated loop runs no gh read while it waits"
    assert naps == [3600.0]


@pytest.mark.asyncio
async def test_a_declared_hold_on_a_pull_request_reads_no_later_than_its_expiry(svc, monkeypatch):
    t = _whole_now()
    loop, armed, _fired = await _declared_plain(
        svc, monkeypatch, now=t, message=f"babysit {PR_URL}", max_runtime_secs=3600
    )
    loop.created_ts = t - 3480.0  # 120 seconds of budget left
    assert await svc.declare_person_wait(SLOT, held=True)
    assert armed == [120.0], "the expiry comes before the next read"

    async def _read(_message):
        return _obs()

    monkeypatch.setattr(subject_reads, "_read_state", _read)
    armed.clear()
    _pin_clock(monkeypatch, t + 100.0)
    await svc._timer(loop, 0)
    assert loop.person_wait_declared is True
    assert armed == [20.0]

    armed.clear()
    svc.notify_turn_complete(SLOT)
    assert armed == [20.0], "a turn's end keeps the expiry too"
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_hold_that_expired_while_the_gateway_was_down_expires_promptly(
    base, svc, monkeypatch
):
    t = _whole_now()
    loop, _armed, _fired = await _declared_plain(svc, monkeypatch, now=t)
    assert await svc.declare_person_wait(SLOT, held=True)
    await _drain(svc)
    _an._INSTANCE = None

    reloaded = AutoNudgeService(base_dir=base)
    reloaded._load()
    again = reloaded._loops[loop.id]
    assert _an.person_wait_expires_at(again) == t + PERSON_WAIT_MAX_SECS
    rearmed: list = []
    monkeypatch.setattr(reloaded, "_arm_timer", lambda lp, delay=None: rearmed.append(delay))
    _pin_clock(monkeypatch, t + PERSON_WAIT_MAX_SECS + 3600)
    reloaded._arm_from_deadline(again)
    assert rearmed == [0.0]
    reloaded.stop()


@pytest.mark.asyncio
async def test_a_refused_expiry_write_keeps_the_hold(svc, monkeypatch):
    t = _whole_now()
    loop, armed, fired = await _declared_plain(svc, monkeypatch, now=t)
    assert await svc.declare_person_wait(SLOT, held=True)

    def _state_of(lp):
        return (
            lp.waiting_on_person,
            lp.waiting_on_person_at,
            lp.person_wait_declared,
            lp.person_wait_release,
            lp.created_ts,
        )

    before = _state_of(loop)
    writer = AsyncMock(side_effect=OSError("write refused"))
    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", writer)
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    armed.clear()

    _pin_clock(monkeypatch, t + PERSON_WAIT_MAX_SECS)
    await svc._timer(loop, 0)

    writer.assert_awaited_once()
    assert _state_of(loop) == before
    assert fired == [] and events == []
    assert armed == [SUBJECT_READ_SECS], "the expiry is retried, not spun on"
    await _drain(svc)


@pytest.mark.asyncio
async def test_each_release_records_why_the_declared_wait_ended(svc, monkeypatch):
    t = _whole_now()
    loop, _armed, _fired = await _declared_plain(svc, monkeypatch, now=t)
    assert await svc.declare_person_wait(SLOT, held=True)
    assert loop.person_wait_release == ""

    assert await svc.release_approval_hold(SLOT, why="a person replied", arm=False)
    assert loop.person_wait_release == "person"

    assert await svc.declare_person_wait(SLOT, held=True)
    assert loop.person_wait_release == "", "a new declaration clears it"
    assert await svc.declare_person_wait(SLOT, held=False)
    assert loop.person_wait_release == "agent"
    stored = json.loads(svc._store.path.read_text())["loops"][0]
    assert stored["person_wait_release"] == "agent"

    # A ledger hold is not a declaration, and its release records nothing.
    assert await firing._commit_person_wait(svc, loop, held=True)
    assert loop.person_wait_release == "agent"
    assert await firing._commit_person_wait(svc, loop, held=False, release="person")
    assert loop.person_wait_release == "agent"

    await svc.update(loop.id, active=False)
    await svc.update(loop.id, active=True)
    assert svc._loops[loop.id].person_wait_release == "", "a revival clears it"
    await _drain(svc)


@pytest.mark.asyncio
async def test_the_rest_row_and_the_inspect_reading_carry_the_expiry_and_the_release(
    svc, monkeypatch
):
    t = _whole_now()
    loop, _armed, _fired = await _declared_plain(svc, monkeypatch, now=t)
    row = autonudge_routes._serialize(loop)
    reading = autonudge_routes._autonudge_loop_reading(loop)
    assert row["person_wait_release"] == "" and "person_wait_expires_at" not in row
    assert "person_wait_release" not in reading and "person_wait_expires_at" not in reading

    assert await svc.declare_person_wait(SLOT, held=True)
    row = autonudge_routes._serialize(loop)
    reading = autonudge_routes._autonudge_loop_reading(loop)
    assert row["person_wait_expires_at"] == t + PERSON_WAIT_MAX_SECS
    assert reading["person_wait_expires_at"] == t + PERSON_WAIT_MAX_SECS
    assert row["person_wait_ends_on_budget"] is False
    assert reading["person_wait_ends_on_budget"] is False

    assert await svc.declare_person_wait(SLOT, held=False)
    row = autonudge_routes._serialize(loop)
    reading = autonudge_routes._autonudge_loop_reading(loop)
    assert row["person_wait_release"] == "agent" and "person_wait_expires_at" not in row
    assert reading["person_wait_release"] == "agent"
    assert "person_wait_expires_at" not in reading
    await _drain(svc)


@pytest.mark.parametrize("invalid_release", ["<b>", [], {}])
def test_an_invalid_stored_release_loads_as_none(base, invalid_release):
    svc = AutoNudgeService(base_dir=base)
    path = svc._store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"id": "abc12345", "slot_key": SLOT, "message": "babysit", "idle_secs": 300}
    path.write_text(
        json.dumps(
            {
                "version": _an._STORE_VERSION,
                "loops": [{**row, "person_wait_release": invalid_release}],
            }
        )
    )
    svc._load()
    loaded = svc._loops.get("abc12345")
    assert loaded is not None, "malformed release metadata must not unload the loop"
    assert loaded.person_wait_release == ""
    assert svc._store.unparsed_rows == []
    svc.stop()


# --------------------------------------------------------------------------- #
# A declared hold reads the pull request its loop names
# --------------------------------------------------------------------------- #


def _obs(**fields) -> gh_pr.PrObservation:
    base = {
        "repo": "acme/widgets",
        "pr": 7,
        "host": "",
        "status": gh_pr.STATUS_OK,
        "observed_at": 0.0,
        "state": "OPEN",
        "mergeability": "MERGEABLE",
        "merge_state": "CLEAN",
        "head": "a" * 40,
    }
    base.update(fields)
    return gh_pr.PrObservation(**base)


_MERGED = {"state": "MERGED", "merged_at": "2026-01-01T00:00:00Z"}


async def _declared_on_a_pr(svc, monkeypatch, *, gate: bool = True) -> tuple[NudgeLoop, list]:
    """A started service with one declared-held loop naming PR_URL, re-arms recorded.

    Gated by default: only a gated loop's hold reads its pull request.
    """
    await svc.start()
    loop = await svc.add(slot_key=SLOT, message=f"babysit {PR_URL}", idle_secs=1800, gate=gate)
    svc._cancel_timer(loop.id)
    armed: list = []
    monkeypatch.setattr(svc, "_arm_timer", lambda lp, delay=None: armed.append(delay))
    assert await svc.declare_person_wait(SLOT, held=True)
    return svc._loops[loop.id], armed


@pytest.mark.asyncio
async def test_a_declaration_on_a_pull_request_arms_its_read(svc, monkeypatch):
    loop, armed = await _declared_on_a_pr(svc, monkeypatch)
    assert loop.person_wait_declared is True
    assert armed == [SUBJECT_READ_SECS]
    await _drain(svc)


@pytest.mark.asyncio
async def test_the_declaring_turns_end_keeps_the_read_not_the_deadline(svc, monkeypatch):
    """The nudge that declares the wait then finishes; its turn-complete re-arm must
    not park the held read a whole interval out."""
    loop, armed = await _declared_on_a_pr(svc, monkeypatch)
    loop.idle_secs = 86400
    loop.next_due_ts = 0.0
    armed.clear()

    svc.notify_turn_complete(SLOT)

    assert armed == [SUBJECT_READ_SECS]
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_turn_ending_on_a_declared_wait_with_nothing_to_read_arms_its_expiry(
    svc, monkeypatch
):
    """A turn-complete re-arm keeps a no-subject hold bounded by its expiry."""
    t = _whole_now()
    loop, armed, _fired = await _declared_plain(svc, monkeypatch, now=t)
    assert await svc.declare_person_wait(SLOT, held=True)
    armed.clear()

    svc.notify_turn_complete(SLOT)

    assert armed == [float(PERSON_WAIT_MAX_SECS)]
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_settled_declared_wait_spends_none_of_the_budget(svc, monkeypatch):
    """100 seconds running, then two hours declared, settles as 100 seconds spent."""
    loop, _armed = await _declared_on_a_pr(svc, monkeypatch)
    t = 2_000_000_000.0
    _pin_clock(monkeypatch, t)
    loop.created_ts = t - 7300.0
    loop.waiting_on_person_at = t - 7200.0

    async def _read(_message):
        return _obs(**_MERGED)

    monkeypatch.setattr(subject_reads, "_read_state", _read)

    await svc._timer(loop, 0)

    settled = svc._loops[loop.id]
    assert settled.active is False
    assert settled.created_ts == t - 100.0
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_settled_row_carrying_both_holds_credits_one_span(svc, monkeypatch):
    """The holds never overlap, so a row carrying both flags (a hand-edited store) is
    settled as approval-held: its span alone is handed back, not a union of the two."""
    loop, _armed = await _declared_on_a_pr(svc, monkeypatch)
    t = 2_000_000_000.0
    _pin_clock(monkeypatch, t)
    loop.created_ts = t - 7300.0
    loop.waiting_on_person_at = t - 7200.0
    loop.approval_stalled = True
    loop.approval_stalled_at = t - 600.0

    async def _read(_message):
        return _obs(**_MERGED)

    monkeypatch.setattr(subject_reads, "_read_state", _read)

    await svc._timer(loop, 0)

    settled = svc._loops[loop.id]
    assert settled.active is False
    assert settled.created_ts == t - 6700.0, "the approval hold's 600s, credited once"
    await _drain(svc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reading", "outcome"),
    [(_MERGED, "success"), ({"state": "CLOSED"}, "blocked")],
    ids=["merged", "closed-unmerged"],
)
async def test_a_declared_hold_whose_pull_request_ended_settles(svc, monkeypatch, reading, outcome):
    loop, armed = await _declared_on_a_pr(svc, monkeypatch)
    armed.clear()
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    fired: list = []

    async def on_fire(lp):
        fired.append(lp.id)
        return True

    svc._on_fire = on_fire

    async def _read(_message):
        return _obs(**reading)

    monkeypatch.setattr(subject_reads, "_read_state", _read)

    await svc._timer(loop, 0)

    settled = svc._loops[loop.id]
    assert fired == [], "a held tick fires no cycle"
    assert settled.active is False
    assert settled.stopped_reason == MONITOR_TERMINAL_REASON
    assert settled.monitor is not None and settled.monitor.outcome.value == outcome
    assert settled.waiting_on_person is False
    assert settled.person_wait_declared is False
    assert settled.waiting_on_person_at == 0.0
    assert events.count("expired") == 1
    assert armed == [], "a settled loop is not re-armed"
    stored = json.loads(svc._store.path.read_text())["loops"][0]
    assert stored["active"] is False and stored["monitor"]["outcome"] == outcome
    assert stored["person_wait_declared"] is False and stored["waiting_on_person"] is False
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_declared_hold_on_an_ungated_loop_survives_its_pull_request_merging(
    svc, monkeypatch
):
    """``gate=False`` promises a terminal reading never ends the loop: a declared
    ungated hold reads nothing and still ticks only at its expiry."""
    loop, armed = await _declared_on_a_pr(svc, monkeypatch, gate=False)
    assert (
        len(armed) == 1 and SUBJECT_READ_SECS < armed[0] <= PERSON_WAIT_MAX_SECS
    ), "no read interval, one tick at expiry"
    armed.clear()
    reads: list[str] = []

    async def _read(message):
        reads.append(message)
        return _obs(**_MERGED)

    monkeypatch.setattr(subject_reads, "_read_state", _read)

    await subject_reads.hold_tick(svc, loop)

    kept = svc._loops[loop.id]
    assert reads == [], "an ungated hold does not read its pull request"
    assert kept.active is True and kept.stopped_reason == ""
    assert kept.person_wait_declared is True and kept.waiting_on_person is True
    assert (
        len(armed) == 1 and SUBJECT_READ_SECS < armed[0] <= PERSON_WAIT_MAX_SECS
    ), "re-armed toward its expiry"
    await _drain(svc)


@pytest.mark.asyncio
@pytest.mark.parametrize("reading", [{}, None], ids=["open", "unread"])
async def test_a_declared_hold_on_a_live_pull_request_stays_held(svc, monkeypatch, reading):
    loop, armed = await _declared_on_a_pr(svc, monkeypatch)
    armed.clear()

    async def _read(_message):
        return None if reading is None else _obs(**reading)

    monkeypatch.setattr(subject_reads, "_read_state", _read)

    await svc._timer(loop, 0)

    kept = svc._loops[loop.id]
    assert kept.active is True and kept.person_wait_declared is True
    assert armed == [SUBJECT_READ_SECS]
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_person_back_during_the_read_is_not_settled(svc, monkeypatch):
    loop, armed = await _declared_on_a_pr(svc, monkeypatch)
    armed.clear()

    async def _read_then_release(_message):
        assert await svc.release_approval_hold(SLOT, why="a person replied", arm=False)
        return _obs(**_MERGED)

    monkeypatch.setattr(subject_reads, "_read_state", _read_then_release)

    await subject_reads.hold_tick(svc, loop)

    kept = svc._loops[loop.id]
    assert kept.active is True and kept.stopped_reason == ""
    assert kept.person_wait_declared is False
    assert armed == [], "a released loop is re-armed by its release, not by the read"
    await _drain(svc)


@pytest.mark.asyncio
async def test_the_reconciler_rescues_a_declared_hold_s_read(svc, monkeypatch):
    loop, armed = await _declared_on_a_pr(svc, monkeypatch)
    armed.clear()
    svc._timers.pop(loop.id, None)
    svc._reconcile_once()
    assert armed == [], "the first pass only marks the candidate"
    svc._reconcile_once()
    assert armed == [SUBJECT_READ_SECS]
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_revival_clears_the_declaration(svc, _nosleep):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    await svc.update(loop.id, active=False)
    await svc.update(loop.id, active=True)
    refreshed = svc._loops[loop.id]
    assert refreshed.person_wait_declared is False and refreshed.waiting_on_person is False
    await _drain(svc)


@pytest.mark.asyncio
async def test_the_hold_survives_a_restart(base, svc, _nosleep):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    await _drain(svc)
    _an._INSTANCE = None

    reloaded = AutoNudgeService(base_dir=base)
    reloaded._load()
    again = reloaded._loops[loop.id]
    assert again.person_wait_declared is True and again.waiting_on_person is True
    refired: list = []

    async def on_fire(lp):
        refired.append(lp.cycle_count)
        return True

    reloaded._on_fire = on_fire
    await reloaded._timer(again)
    assert refired == [], "a reloaded declared hold still fires nothing"
    reloaded.stop()


@pytest.mark.asyncio
async def test_declaring_during_an_approval_hold_writes_nothing(svc, monkeypatch):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    try:
        svc.notify_approval_stalled(SLOT)
        await _settle(svc)
        assert loop.approval_stalled is True
        writer = AsyncMock(wraps=svc._write_monitor_snapshot_locked)
        monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", writer)

        result = await _apply(svc, {"waiting_on_person": True}, producer_is_user_facing=True)
        assert "already waits on a person" in result and "approval" in result
        # The commit's own recheck under the lock, for an approval hold that lands
        # between the declaration's read and its write.
        assert await firing._commit_person_wait(svc, loop, held=True, declared=True)

        writer.assert_not_awaited()
        assert loop.approval_stalled is True, "the loop stays held for the approval"
        assert loop.person_wait_declared is False and loop.waiting_on_person is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_an_unanswered_approval_during_a_declared_wait_records_no_approval_hold(
    svc, _nosleep
):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    held = svc._loops[loop.id]

    svc.notify_approval_stalled(SLOT)
    await _settle(svc)
    # The recording step's own recheck under the lock, for a declaration that lands
    # between the stall's notice and its write.
    await timers._record_approval_hold(svc, SLOT)

    assert held.approval_stalled is False and held.approval_stalled_at == 0.0
    assert held.person_wait_declared is True and held.waiting_on_person is True
    await _tick(svc, loop)
    assert len(fired) == 1, "the declared hold still fires no cycle"
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_structured_monitor_cannot_declare(svc):
    await svc.add_monitor(
        slot_key=SLOT,
        kind="github_pull_request",
        target="https://github.com/octo/repo/pull/1",
        objective="review_ready",
        cadence_secs=300,
        budgets=MonitorBudgets(),
    )
    assert await svc.declare_person_wait(SLOT, held=True) is False
    svc.stop()


# --------------------------------------------------------------------------- #
# monitor_update(waiting_on_person=...) through the directive applier
# --------------------------------------------------------------------------- #


def _state():
    return SimpleNamespace(_slots={SLOT: SimpleNamespace(workspace="default", is_closing=False)})


def _slot(generation: int = 1):
    return SimpleNamespace(key=SLOT, _app="", _turn_generation=generation, _queue=[])


async def _apply(svc, patch_, slot=None, state=None, **producer):
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=svc),
        patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
    ):
        return await apply_session_directive(
            state if state is not None else _state(),
            slot if slot is not None else _slot(),
            SESSION,
            "monitor_update",
            {"patch": patch_},
            **producer,
        )


@pytest.mark.asyncio
async def test_a_human_turn_declares_the_wait(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    try:
        result = await _apply(svc, {"waiting_on_person": True}, producer_is_user_facing=True)
        assert "waiting on a person" in result
        assert loop.person_wait_declared is True and loop.waiting_on_person is True

        result = await _apply(svc, {"waiting_on_person": False}, producer_is_user_facing=True)
        assert "no longer waiting" in result
        assert loop.person_wait_declared is False and loop.waiting_on_person is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_the_loop_s_own_wake_declares_the_wait(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    try:
        result = await _apply(
            svc,
            {"waiting_on_person": True},
            producer_is_self_wake=True,
            producer_wake_loop_id=loop.id,
        )
        assert "waiting on a person" in result
        assert loop.person_wait_declared is True
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_the_mirror_probe_runs_off_the_event_loop(svc, monkeypatch):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    event_loop_thread = threading.get_ident()
    probe_threads: list[int] = []

    def _probe(_state, _slot):
        probe_threads.append(threading.get_ident())
        return False

    monkeypatch.setattr(session_control, "_has_channel_mirror", _probe)
    try:
        result = await _apply(svc, {"waiting_on_person": True}, producer_is_user_facing=True)
        assert "waiting on a person" in result
        assert loop.person_wait_declared is True
        assert len(probe_threads) == 1
        assert probe_threads[0] != event_loop_thread
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_the_loop_s_own_wake_on_a_linked_dashboard_slot_may_not_declare(svc):
    """The wake runs on the dashboard key, but the person answers in Slack."""
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    mirror = MagicMock(return_value=None)
    slack = MagicMock(return_value=("1712793600.123456", "C123"))
    state = _state()
    state.sessions = SimpleNamespace(get_mirror_link=mirror, get_slack_link=slack)
    try:
        result = await _apply(
            svc,
            {"waiting_on_person": True},
            state=state,
            producer_is_self_wake=True,
            producer_wake_loop_id=loop.id,
        )
        assert loop.person_wait_declared is False and loop.waiting_on_person is False
        assert "dashboard chat" in result
        mirror.assert_called_once_with(SESSION)
        slack.assert_called_once_with(SESSION)
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_cron_or_app_turn_may_not_declare(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    try:
        result = await _apply(svc, {"waiting_on_person": True})
        assert loop.person_wait_declared is False
        assert "needs a turn a person sent" in result
        # Ending a wait is open to any producer that may revise the loop.
        assert "no longer waiting" in await _apply(svc, {"waiting_on_person": False})
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_channel_binding_may_not_declare(svc):
    channel_key = "slack:C123:1712793600.123456"
    loop = await svc.add(slot_key=channel_key, message="check", idle_secs=600)
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(_slots={}),
                None,
                channel_key,
                "monitor_update",
                {"patch": {"waiting_on_person": True}},
                producer_is_user_facing=True,
                producer_is_channel=True,
            )
        assert loop.person_wait_declared is False
        assert "dashboard chat" in result
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_slack_thread_linked_to_a_dashboard_slot_may_not_declare(svc):
    """The turn runs on the dashboard key, but the person answers in Slack."""
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    try:
        result = await _apply(
            svc,
            {"waiting_on_person": True},
            producer_is_user_facing=True,
            producer_is_channel=True,
        )
        assert loop.person_wait_declared is False and loop.waiting_on_person is False
        assert "dashboard chat" in result
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_structured_monitor_is_refused(svc):
    loop = await svc.add_monitor(
        slot_key=SLOT,
        kind="github_pull_request",
        target="https://github.com/octo/repo/pull/1",
        objective="review_ready",
        cadence_secs=300,
        budgets=MonitorBudgets(),
    )
    try:
        result = await _apply(svc, {"waiting_on_person": True}, producer_is_user_facing=True)
        assert "monitor_start loop" in result
        assert loop.person_wait_declared is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_an_inactive_loop_is_refused(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    await svc.update(loop.id, active=False)
    try:
        result = await _apply(svc, {"waiting_on_person": True}, producer_is_user_facing=True)
        assert "PAUSED" in result
        assert loop.person_wait_declared is False and loop.active is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_it_combines_with_a_message_edit(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    try:
        result = await _apply(
            svc,
            {"message": "check again once they approve", "waiting_on_person": True},
            producer_is_user_facing=True,
        )
        assert "updated" in result and "waiting on a person" in result
        assert loop.message == "check again once they approve"
        assert loop.person_wait_declared is True
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_ending_a_ledger_hold_reports_it_as_the_ledger_s(svc, monkeypatch):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    assert await firing._commit_person_wait(svc, loop, held=True)
    declare = AsyncMock(wraps=svc.declare_person_wait)
    monkeypatch.setattr(svc, "declare_person_wait", declare)
    try:
        result = await _apply(svc, {"waiting_on_person": False}, producer_is_user_facing=True)
        assert "work ledger" in result and "no longer waiting" not in result
        declare.assert_not_awaited()
        assert loop.waiting_on_person is True and loop.person_wait_declared is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_refused_hold_after_a_message_edit_says_the_edit_landed(svc, monkeypatch):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    monkeypatch.setattr(svc, "declare_person_wait", AsyncMock(return_value=False))
    try:
        result = await _apply(
            svc,
            {"message": "check again once they approve", "waiting_on_person": True},
            producer_is_user_facing=True,
        )
        assert "could not be recorded" in result
        assert "other fields were applied" in result and "updated" in result
        assert loop.message == "check again once they approve"
        assert loop.person_wait_declared is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_person_who_already_answered_is_not_waited_on(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    slot = _slot(generation=4)
    turn_state(slot).human_input_generation = 4  # they sent a message during this turn
    try:
        result = await _apply(
            svc, {"waiting_on_person": True}, slot=slot, producer_is_user_facing=True
        )
        assert "NOT held" in result
        assert loop.person_wait_declared is False

        queued = _slot(generation=5)
        queued._queue = [{"id": "q1", "_directive_user_origin": True}]
        result = await _apply(
            svc, {"waiting_on_person": True}, slot=queued, producer_is_user_facing=True
        )
        assert "NOT held" in result
        assert loop.person_wait_declared is False

        earlier = _slot(generation=6)
        turn_state(earlier).human_input_generation = 5  # the message that started this turn
        result = await _apply(
            svc, {"waiting_on_person": True}, slot=earlier, producer_is_user_facing=True
        )
        assert loop.person_wait_declared is True
    finally:
        svc.stop()


def test_the_tool_puts_the_field_in_the_patch_and_validates_it():
    from kiro_crew import validation

    validated = validation.validate_tool_args(
        {"waiting_on_person": True}, validation.MONITOR_UPDATE_SCHEMA
    )
    assert validated["waiting_on_person"] is True
    with pytest.raises(validation.ValidationError):
        validation.validate_tool_args(
            {"waiting_on_person": "yes"}, validation.MONITOR_UPDATE_SCHEMA
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [True, False])
async def test_a_stop_queued_before_a_declaration_publishes_no_hold(svc, monkeypatch, held):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    if not held:
        assert await svc.declare_person_wait(SLOT, held=True)
    before = (loop.waiting_on_person, loop.person_wait_declared, loop.created_ts)
    writer = AsyncMock(wraps=svc._write_monitor_snapshot_locked)
    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", writer)
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    tasks: list[asyncio.Task] = []
    remove_entered = asyncio.Event()
    continue_remove = asyncio.Event()
    real_remove = svc._remove_unserialized

    async def _remove(*args, **kwargs):
        remove_entered.set()
        await await_until(continue_remove, what="the test to continue the admitted remove")
        return await real_remove(*args, **kwargs)

    monkeypatch.setattr(svc, "_remove_unserialized", _remove)
    try:
        stop = asyncio.create_task(svc.remove(loop.id))
        tasks.append(stop)
        await await_until(remove_entered, what="the admitted remove transaction")
        async with svc._lock:
            continue_remove.set()
            await until_parked(svc._lock, timeout=30)
            declaration = asyncio.create_task(svc.declare_person_wait(SLOT, held=held))
            tasks.append(declaration)
            await until_parked(svc._lock, count=2, timeout=30)
        stopped, declared = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
        assert stopped is True and svc.get_by_slot(SLOT) is None
        assert declared is False
        writer.assert_not_awaited()
        assert events == ["removed"], "a deleted loop publishes no updated frame"
        assert (loop.waiting_on_person, loop.person_wait_declared, loop.created_ts) == before
        assert _no_live_timer(svc, loop)
    finally:
        continue_remove.set()
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)
        svc.stop()


@pytest.mark.asyncio
async def test_shutdown_drains_a_declaration_admitted_before_closure(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    svc._cancel_timer(loop.id)
    declaration: asyncio.Task[bool]
    shutdown: asyncio.Task[None]
    async with svc._lock:
        declaration = asyncio.create_task(svc.declare_person_wait(SLOT, held=True))
        await until_parked(svc._lock, timeout=30)
        shutdown = asyncio.create_task(svc.shutdown())
        await async_wait_until(
            lambda: not svc.accepting_mutations,
            describe=lambda: f"accepting_mutations={svc.accepting_mutations}",
        )
        assert not shutdown.done(), "shutdown drains the already-admitted declaration"
    assert await asyncio.wait_for(declaration, timeout=30) is True
    await asyncio.wait_for(shutdown, timeout=30)
    assert loop.person_wait_declared is True and loop.waiting_on_person is True
    stored = json.loads(svc._store.path.read_text())["loops"][0]
    assert stored["person_wait_declared"] is True and stored["waiting_on_person"] is True
    assert loop.id not in svc._timers


@pytest.mark.asyncio
async def test_shutdown_refuses_a_declared_wait_release(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    svc._cancel_timer(loop.id)
    assert await svc.declare_person_wait(SLOT, held=True)
    await svc.shutdown()

    with pytest.raises(NudgeAdmissionRefused, match="gateway is restarting"):
        await svc.release_approval_hold(SLOT, why="too late", arm=False)
    assert loop.person_wait_declared is True and loop.waiting_on_person is True


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [True, False])
async def test_a_refused_ledger_hold_write_keeps_its_requested_state_unpublished(
    svc, monkeypatch, held
):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    if not held:
        assert await firing._commit_person_wait(svc, loop, held=True)
    loop.ledger_seen_fp = "seen"
    monkeypatch.setattr(svc, "_observes_work_ledger", lambda lp: True)
    monkeypatch.setattr(firing, "_ledger_person_wait_fp", lambda *_a: "seen" if held else "moved")
    writer = AsyncMock(side_effect=OSError("write refused"))
    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", writer)
    arm = MagicMock()
    monkeypatch.setattr(svc, "_arm_from_deadline", arm)
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    before = (loop.waiting_on_person, loop.waiting_on_person_at, loop.created_ts)
    try:
        assert await svc._holds_for_person_wait(loop) is (not held)
        writer.assert_awaited_once()
        assert events == []
        assert (loop.waiting_on_person, loop.waiting_on_person_at, loop.created_ts) == before
        if held:
            arm.assert_not_called()
        else:
            arm.assert_called_once_with(loop)
    finally:
        svc.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["during-turn", "queued"])
async def test_a_reply_during_the_declaration_write_releases_the_hold(svc, monkeypatch, reply):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    slot = _slot(generation=4)
    write = svc._write_monitor_snapshot_locked
    events: list[bool] = []
    writes = 0
    svc.subscribe(lambda ev, lp: events.append(lp.waiting_on_person))

    async def _write_with_reply(payload, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            assert loop.waiting_on_person is False
            inflight_before_reply = set(svc._inflight_adds)
            if reply == "during-turn":
                responsive_chat.notify_user_input({"is_dashboard_user": True}, slot)
                assert turn_state(slot).human_input_generation == slot._turn_generation
            else:
                slot._queue.append({"id": "q1", "_directive_user_origin": True})
                svc.notify_user_input(SLOT, human=True)
            assert (
                svc._inflight_adds == inflight_before_reply
            ), "the person's release finds no published hold"
        await write(payload, **kwargs)

    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", _write_with_reply)
    try:
        result = await _apply(
            svc, {"waiting_on_person": True}, slot=slot, producer_is_user_facing=True
        )
        assert writes == 2, "the hold and its release both commit"
        assert events == [True, False]
        assert "NOT held" in result and "already sent" in result
        assert loop.person_wait_declared is False and loop.waiting_on_person is False
        assert loop.waiting_on_person_at == 0.0
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_replacement_during_the_mirror_probe_is_not_held(svc, monkeypatch):
    original = await svc.add(slot_key=SLOT, message="original", idle_secs=600)
    event_loop = asyncio.get_running_loop()
    replacement: dict[str, NudgeLoop] = {}

    async def _swap_loop() -> None:
        assert await svc.remove(original.id)
        replacement["loop"] = await svc.add(slot_key=SLOT, message="replacement", idle_secs=600)

    def _probe(_state, _slot):
        asyncio.run_coroutine_threadsafe(_swap_loop(), event_loop).result(timeout=30)
        return False

    monkeypatch.setattr(session_control, "_has_channel_mirror", _probe)
    try:
        result = await _apply(svc, {"waiting_on_person": True}, producer_is_user_facing=True)
        current = replacement["loop"]
        assert "changed while its chat-link status was checked" in result
        assert current.person_wait_declared is False
        assert current.waiting_on_person is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_declaration_during_a_ledger_read_is_not_released(svc, monkeypatch):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    svc._cancel_timer(loop.id)
    loop.ledger_seen_fp = "seen"
    monkeypatch.setattr(svc, "_observes_work_ledger", lambda lp: True)
    entered = asyncio.Event()
    resume = threading.Event()
    event_loop = asyncio.get_running_loop()
    fired: list[int] = []

    async def _on_fire(lp):
        fired.append(lp.cycle_count)
        return True

    def _read_ledger(*_args):
        event_loop.call_soon_threadsafe(entered.set)
        assert resume.wait(timeout=30)
        return "moved"

    svc._on_fire = _on_fire
    monkeypatch.setattr(firing, "_ledger_person_wait_fp", _read_ledger)
    tick = asyncio.create_task(svc._timer(loop, 0))
    try:
        await asyncio.wait_for(entered.wait(), timeout=30)
        assert await svc.declare_person_wait(SLOT, held=True)
        resume.set()
        await asyncio.wait_for(tick, timeout=30)
        assert loop.person_wait_declared is True
        assert loop.waiting_on_person is True
        assert fired == [], "the stale ledger release fires no cycle"
    finally:
        resume.set()
        tick.cancel()
        await asyncio.gather(tick, return_exceptions=True)
        svc.stop()
