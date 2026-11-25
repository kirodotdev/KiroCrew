"""A work-ledger watch ends itself once every item is closed and its conductor is idle.

The conductor sends its final report in the turn that closes the last item, and
opens the next item in that same turn when the goal goes on. So while that turn
runs the watch stays quiet, and once it is over with every item still closed the
gateway ends the watch with no further turn, so the conductor spends none on
``autonudge_stop``. A channel conductor keeps the one "every item is closed" wake
and its watch. A loop that does not watch the ledger is never ended this way.

These cases drive the real path: a loop armed through ``AutoNudgeService.add``,
the real ``WorkLedgerProbe`` over a real ledger store, the real ``irq`` kernel
and the timer callback that ticks the loop. Only the turn delivery and the
conductor's liveness are stubs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from kiro_crew import work_ledger
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.autonudge_service.model import MONITOR_TERMINAL_REASON
from kiro_crew.monitoring.models import MonitorOutcome

CONDUCTOR = "chat-conductor"
WORKER = "chat-worker"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Own data home per test, so no ledger, rate or kernel file outlives it."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _create(title: str = "do the thing", worker: str = WORKER) -> str:
    created = work_ledger.apply_conductor_action(
        CONDUCTOR, "create", title=title, acceptance={"kind": "human_approval"}
    )
    item_id = str(created["item"].item_id)
    work_ledger.apply_conductor_action(
        CONDUCTOR, "bind", item_id=item_id, worker_session_key=worker
    )
    return item_id


def _close(item_id: str, state: str = "accepted") -> None:
    if state == "accepted":
        work_ledger.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="pass")
    work_ledger.apply_conductor_action(
        CONDUCTOR, "close", item_id=item_id, state=state, decision="checked"
    )


class _Harness:
    """A real service and a real loop; records what each fire carried."""

    def __init__(self, service: AutoNudgeService, running: set[str]) -> None:
        self.service = service
        self.running = running
        self.delivered: list[str] = []
        self.loop = None

    async def arm(self, *, watch: str = "work-ledger") -> None:
        async def on_fire(loop) -> bool:
            self.delivered.append(self.service.pending_wake_briefs(loop.id))
            return True

        self.service._on_fire = AsyncMock(side_effect=on_fire)
        self.loop = await self.service.add(
            CONDUCTOR,
            "patrol the work ledger",
            idle_secs=300,
            watch=watch,
        )

    async def tick(self) -> list[str]:
        """One timer tick; the briefs of each turn it delivered."""
        before = len(self.delivered)
        await self.service._run_timer_callback(self.loop)
        return self.delivered[before:]


@pytest_asyncio.fixture
async def make_harness(tmp_path):
    services: list[AutoNudgeService] = []

    async def make(*, watch: str = "work-ledger") -> _Harness:
        running: set[str] = set()
        service = AutoNudgeService(
            base_dir=tmp_path / "svc", worker_running=lambda key: key in running
        )
        services.append(service)
        h = _Harness(service, running)
        work_ledger.ensure_conductor(CONDUCTOR, goal="triage every error I am given")
        await h.arm(watch=watch)
        return h

    try:
        yield make
    finally:
        for service in services:
            service.stop()


@pytest_asyncio.fixture
async def harness(make_harness):
    return await make_harness()


def _ended(loop, outcome: MonitorOutcome) -> bool:
    return (
        loop.active is False
        and loop.stopped_reason == MONITOR_TERMINAL_REASON
        and loop.monitor.outcome is outcome
    )


@pytest.mark.asyncio
async def test_a_watch_armed_before_the_first_item_stays_armed_and_quiet(harness):
    assert await harness.tick() == []
    assert harness.loop.active is True
    assert harness.loop.monitor.outcome is None


@pytest.mark.asyncio
async def test_closing_the_last_item_ends_the_watch_with_no_turn(harness):
    item = _create()
    _close(item)

    assert await harness.tick() == [], "the final report already went out; no turn is owed"
    assert _ended(harness.loop, MonitorOutcome.SUCCESS)


@pytest.mark.asyncio
async def test_an_open_item_keeps_the_watch(harness):
    done, still_open = _create(), _create("the second error", worker="chat-worker-b")
    _close(done)

    assert await harness.tick() == []
    assert harness.loop.active is True
    assert harness.loop.monitor.outcome is None

    # Its report still wakes the conductor on the same watch.
    work_ledger.apply_worker_report(CONDUCTOR, still_open, status="done", summary="fixed it")
    briefs = await harness.tick()
    assert len(briefs) == 1
    assert f"item={still_open}" in briefs[0]
    assert harness.loop.active is True


@pytest.mark.asyncio
async def test_the_watch_waits_out_the_conductors_own_turn(harness):
    item = _create()
    _close(item)
    harness.running.add(CONDUCTOR)

    # The turn that closed the last item is still running: it may open the next.
    assert await harness.tick() == []
    assert harness.loop.active is True

    # It did: the next item keeps the watch once that turn is over.
    nxt = _create("the next error")
    harness.running.discard(CONDUCTOR)
    assert await harness.tick() == []
    assert harness.loop.active is True

    _close(nxt)
    assert await harness.tick() == []
    assert _ended(harness.loop, MonitorOutcome.SUCCESS)


@pytest.mark.asyncio
async def test_a_turn_that_starts_after_the_probe_read_keeps_the_watch(tmp_path):
    # The probe reads a closed board with the conductor idle; before settlement the
    # conductor starts a turn that may bind the next item, and that bind would see
    # this loop still active. Settlement re-reads the liveness and keeps the watch.
    calls = {"n": 0}

    def running(_key: str) -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # idle for the probe's read, busy by settlement

    service = AutoNudgeService(base_dir=tmp_path / "svc-race", worker_running=running)
    service._on_fire = AsyncMock(return_value=True)
    try:
        work_ledger.ensure_conductor(CONDUCTOR, goal="triage every error I am given")
        loop = await service.add(
            CONDUCTOR, "patrol the work ledger", idle_secs=300, watch="work-ledger"
        )
        _close(_create())

        await service._run_timer_callback(loop)
        assert calls["n"] >= 2, "settlement read the conductor's liveness again"
        assert loop.active is True
        assert loop.monitor.outcome is None
        service._on_fire.assert_not_called()
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_bind_after_the_probe_read_keeps_the_watch_with_the_conductor_idle(tmp_path):
    # A turn creates and binds the next item after the probe read the closed board,
    # and is over again by settlement, so liveness alone reads idle. The ledger's
    # change mark moved, and that keeps the watch.
    created: list[str] = []

    def running(_key: str) -> bool:
        if not created:  # the probe's own read is done: the next item lands now
            created.append(_create("the next error"))
        return False

    service = AutoNudgeService(base_dir=tmp_path / "svc-bind", worker_running=running)
    service._on_fire = AsyncMock(return_value=True)
    try:
        work_ledger.ensure_conductor(CONDUCTOR, goal="triage every error I am given")
        loop = await service.add(
            CONDUCTOR, "patrol the work ledger", idle_secs=300, watch="work-ledger"
        )
        _close(_create())

        await service._run_timer_callback(loop)
        assert created, "the bind landed between the read and settlement"
        assert loop.active is True
        assert loop.monitor.outcome is None
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_rejected_last_item_ends_the_watch_as_blocked(harness):
    good, bad = _create(), _create(worker="chat-worker-b")
    _close(good)
    _close(bad, state="rejected")

    assert await harness.tick() == []
    assert _ended(harness.loop, MonitorOutcome.BLOCKED)


@pytest.mark.asyncio
async def test_a_channel_conductor_keeps_the_closed_board_wake_and_its_watch(tmp_path):
    # The liveness resolver reads dashboard slots only, and a channel terminal settles
    # through an owed turn a concurrent bind could race. So a channel conductor gets
    # the one "every item is closed" wake, the watch stays armed, and the conductor
    # stops it itself.
    channel = "slack:C1:1700000000.000100"
    service = AutoNudgeService(base_dir=tmp_path / "svc-channel", worker_running=lambda _k: False)
    delivered: list[str] = []

    async def on_fire(loop) -> bool:
        delivered.append(service.pending_wake_briefs(loop.id))
        return True

    service._on_fire = AsyncMock(side_effect=on_fire)
    try:
        work_ledger.ensure_conductor(channel, goal="triage every error I am given")
        loop = await service.add(
            channel, "patrol the work ledger", idle_secs=300, watch="work-ledger"
        )
        created = work_ledger.apply_conductor_action(
            channel, "create", title="do the thing", acceptance={"kind": "human_approval"}
        )
        item = str(created["item"].item_id)
        work_ledger.apply_conductor_action(channel, "bind", item_id=item, worker_session_key=WORKER)
        work_ledger.apply_conductor_action(channel, "verdict", item_id=item, verdict="pass")
        work_ledger.apply_conductor_action(
            channel, "close", item_id=item, state="accepted", decision="checked"
        )

        await service._run_timer_callback(loop)
        assert len(delivered) == 1
        assert "every item is closed and accepted (1 total)" in delivered[0]
        assert loop.active is True
        assert loop.monitor.outcome is None
        assert not loop.monitor.terminal_pending
        # The same closed board is not news a second time.
        await service._run_timer_callback(loop)
        assert len(delivered) == 1
        assert loop.active is True
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_loop_that_does_not_watch_the_ledger_is_untouched(make_harness):
    # A person's own loop on the conductor's session: no ledger watch, so no ledger
    # probe and nothing that could end it.
    h = await make_harness(watch="")
    item = _create()
    _close(item)

    await h.tick()
    await h.tick()
    assert h.loop.active is True
    assert h.loop.monitor is None
    assert h.loop.stopped_reason in ("", None)
