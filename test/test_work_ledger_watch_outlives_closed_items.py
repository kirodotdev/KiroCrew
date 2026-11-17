"""A work-ledger watch keeps running after every item is closed.

Closing every item does not finish the goal a ledger serves: a lead with a
standing goal closes item N and waits for item N+1, and a conductor closes one
round before it opens the next. So "every item closed" is one wake that says
so, and the loop stays armed. It ends only on a stop or its own bounds.

These cases drive the real path: a loop armed through ``AutoNudgeService.add``,
the real ``WorkLedgerProbe`` over a real ledger store, the real ``irq`` kernel
and the timer callback that ticks the loop. Only the turn delivery is a stub.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from kiro_crew import work_ledger
from kiro_crew.autonudge import AutoNudgeService

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
    """A real service and a real work-ledger loop; records what each fire carried."""

    def __init__(self, service: AutoNudgeService) -> None:
        self.service = service
        self.delivered: list[str] = []
        self.loop = None

    async def arm(self) -> None:
        async def on_fire(loop) -> bool:
            self.delivered.append(self.service.pending_wake_briefs(loop.id))
            return True

        self.service._on_fire = AsyncMock(side_effect=on_fire)
        self.loop = await self.service.add(
            CONDUCTOR,
            "patrol the work ledger",
            idle_secs=300,
            watch="work-ledger",
        )

    async def tick(self) -> list[str]:
        """One timer tick; the briefs of each turn it delivered."""
        before = len(self.delivered)
        await self.service._run_timer_callback(self.loop)
        return self.delivered[before:]


@pytest_asyncio.fixture
async def harness(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path / "svc")
    h = _Harness(service)
    work_ledger.ensure_conductor(CONDUCTOR, goal="triage every error I am given")
    await h.arm()
    try:
        yield h
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_watch_armed_before_the_first_item_stays_armed_and_quiet(harness):
    assert await harness.tick() == []
    assert harness.loop.active is True
    assert harness.loop.monitor.outcome is None


@pytest.mark.asyncio
async def test_closing_the_last_item_wakes_once_and_keeps_the_loop(harness):
    item = _create()
    _close(item)

    first = await harness.tick()
    assert len(first) == 1, "closing the last item is one wake"
    assert "every item is closed" in first[0]
    assert harness.loop.active is True, "and the loop stays armed"
    assert harness.loop.monitor.outcome is None
    assert harness.loop.stopped_reason in ("", None)

    # The same closed board is not news a second time.
    assert await harness.tick() == []
    assert await harness.tick() == []
    assert harness.loop.active is True


@pytest.mark.asyncio
async def test_a_rejected_last_item_says_how_many_were_accepted(harness):
    good, bad = _create(), _create(worker="chat-worker-b")
    _close(good)
    _close(bad, state="rejected")
    briefs = await harness.tick()
    assert len(briefs) == 1
    assert "1 accepted of 2" in briefs[0]
    assert harness.loop.active is True


@pytest.mark.asyncio
async def test_a_new_item_after_every_item_closed_still_wakes_with_no_re_arm(harness):
    first = _create()
    _close(first)
    assert len(await harness.tick()) == 1

    second = _create("the next error")
    work_ledger.apply_worker_report(CONDUCTOR, second, status="done", summary="fixed it")
    briefs = await harness.tick()
    assert len(briefs) == 1
    assert f"item={second}" in briefs[0]
    assert "status=done" in briefs[0]

    # Closing the new item is a new "all closed" board, so it wakes once again.
    _close(second)
    again = await harness.tick()
    assert len(again) == 1
    assert "every item is closed" in again[0]
    assert harness.loop.active is True


@pytest.mark.asyncio
async def test_a_spent_cycle_cap_still_ends_a_loop_whose_items_are_all_closed(harness):
    item = _create()
    _close(item)
    assert len(await harness.tick()) == 1
    harness.loop.max_cycles = harness.loop.cycle_count

    assert await harness.tick() == []
    assert harness.loop.active is False
    assert harness.loop.stopped_reason == "cycle_cap"
