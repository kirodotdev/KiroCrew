"""``heartbeat.keep_backoff``: a task that keeps being retained is spaced out.

Each pass is driven at the real ``_process_heartbeat_file`` with a fake clock
that moves one tick (60 whole seconds) per pass.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

import kiro_crew.heartbeat as hb_mod
from kiro_crew.heartbeat import _HEADER, _KEEP_SENTINEL, HeartbeatService

TICK = 60
# Minutes at which an always-retained task runs: 0, then waits 1, 2, 4, 8, 15, 15.
LADDER_MINUTES = [0, 1, 3, 7, 15, 30, 45]


class Rig:
    """One service over a HEARTBEAT.md in *tmp_path*, with a fake clock."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
        self.path = tmp_path / "HEARTBEAT.md"
        self.now = 0
        self.ran: list[tuple[int, str]] = []
        self.cycle_ends = 0
        self.outcome: Callable[[str], str] = lambda text: "still open " + _KEEP_SENTINEL
        cfg = SimpleNamespace(heartbeat=SimpleNamespace(keep_backoff=on))
        monkeypatch.setattr(hb_mod.KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
        monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: self.path)
        self.svc = HeartbeatService(memory=SimpleNamespace(), on_task=self._on_task, interval=TICK)
        self.svc._on_cycle_end = self._on_cycle_end
        self.svc._clock = lambda: self.now

    async def _on_task(self, text: str, deliver: str) -> str:
        self.ran.append((self.now // TICK, text))
        return self.outcome(text)

    async def _on_cycle_end(self) -> None:
        self.cycle_ends += 1

    def write(self, *tasks: str) -> None:
        self.path.write_text(_HEADER + "".join(f"- {t}\n" for t in tasks), encoding="utf-8")

    async def tick(self, count: int = 1) -> None:
        for _ in range(count):
            await self.svc._process_heartbeat_file()
            self.now += TICK

    def ticks_run(self, text: str) -> list[int]:
        return [tick for tick, ran in self.ran if ran == text]


@pytest.fixture
def make_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], Rig]:
    return lambda on: Rig(tmp_path, monkeypatch, on)


def _raises(text: str) -> str:
    raise RuntimeError("boom")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        lambda text: "still open " + _KEEP_SENTINEL,
        lambda text: _KEEP_SENTINEL,  # what a timeout or a fair-admission refusal returns
        _raises,
    ],
    ids=["reply", "sentinel-only", "raised"],
)
async def test_retained_task_runs_only_on_the_ladder(make_rig, outcome) -> None:
    rig = make_rig(True)
    rig.outcome = outcome
    rig.write("watch the build")
    await rig.tick(46)
    assert rig.ticks_run("watch the build") == LADDER_MINUTES
    assert rig.cycle_ends == len(LADDER_MINUTES)
    assert "watch the build" in rig.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_off_runs_a_retained_task_on_every_tick(make_rig) -> None:
    rig = make_rig(False)
    rig.write("watch the build")
    await rig.tick(8)
    assert rig.ticks_run("watch the build") == list(range(8))
    assert rig.cycle_ends == 8


@pytest.mark.asyncio
async def test_edited_task_runs_on_the_next_tick(make_rig) -> None:
    rig = make_rig(True)
    rig.write("watch the build")
    await rig.tick(3)  # runs at 0 and 1; waits through 2
    rig.write("watch the build and the deploy")
    await rig.tick(1)
    assert rig.ticks_run("watch the build") == [0, 1]
    assert rig.ticks_run("watch the build and the deploy") == [3]


@pytest.mark.asyncio
async def test_new_task_runs_while_a_retained_one_waits(make_rig) -> None:
    rig = make_rig(True)
    rig.write("watch the build")
    await rig.tick(2)  # the second retain pushes the next run out to tick 3
    rig.write("watch the build", "check the queue")
    rig.outcome = lambda text: "done" if "queue" in text else "open " + _KEEP_SENTINEL
    await rig.tick(1)
    assert rig.ticks_run("check the queue") == [2]
    assert rig.ticks_run("watch the build") == [0, 1]
    assert "watch the build" in rig.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_completed_task_leaves_the_table(make_rig) -> None:
    rig = make_rig(True)
    answers = iter([_KEEP_SENTINEL, _KEEP_SENTINEL, "all done"])
    rig.outcome = lambda text: next(answers)
    rig.write("watch the build")
    await rig.tick(4)
    assert rig.ticks_run("watch the build") == [0, 1, 3]
    assert rig.svc._keep_table == {}
    assert "watch the build" not in rig.path.read_text(encoding="utf-8")
    # The same text added again is a new task and runs on the next tick.
    rig.outcome = lambda text: "done"
    rig.write("watch the build")
    await rig.tick(1)
    assert rig.ticks_run("watch the build") == [0, 1, 3, 4]


@pytest.mark.asyncio
async def test_removed_task_leaves_the_table(make_rig) -> None:
    rig = make_rig(True)
    rig.write("watch the build")
    await rig.tick(2)
    rig.write("other")
    rig.outcome = lambda text: "done"
    await rig.tick(1)
    assert rig.svc._keep_table == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", ["emptied", "deleted"])
async def test_task_added_again_after_a_pass_without_it_starts_fresh(make_rig, gone) -> None:
    rig = make_rig(True)
    rig.write("watch the build")
    await rig.tick(4)  # runs at 0, 1 and 3; the third retain waits until tick 7
    if gone == "emptied":
        rig.write()
    else:
        rig.path.unlink()
    await rig.tick(1)
    rig.write("watch the build")
    await rig.tick(3)
    # Runs on the next tick, and its first retain waits only the first rung.
    assert rig.ticks_run("watch the build") == [0, 1, 3, 5, 6]


@pytest.mark.asyncio
async def test_spacing_is_measured_from_the_end_of_the_turn(make_rig) -> None:
    rig = make_rig(True)
    starts: list[int] = []

    def slow_second_run(text: str) -> str:
        starts.append(rig.now)
        if len(starts) == 2:
            rig.now += 200  # this turn outlasts the second rung (120 s)
        return _KEEP_SENTINEL

    rig.outcome = slow_second_run
    rig.write("watch the build")
    await rig.tick(6)
    assert starts[:2] == [0, 60]
    # The second turn ended at 260; the second rung starts there.
    assert starts[2] == 260 + 120
