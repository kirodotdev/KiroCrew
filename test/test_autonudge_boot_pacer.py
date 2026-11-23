"""``BootPacer``: the loops due at gateway start are released one at a time.

The gap follows the host posture: short while the host is ample, up to the tight gap
while it is tight (sooner once it recovers), and none at all while it is critical
until it reads ample again. Nothing is dropped, and a loop that leaves the queue lets
the ones behind it move up. A fake clock stands in for time: every sleep the pacer
takes advances it, so the release times below are exact and nothing really waits.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.autonudge_service.boot_pacer import (
    BOOT_WAKE_GAP_SECS,
    BOOT_WAKE_MAX_HOLD_SECS,
    BOOT_WAKE_TIGHT_GAP_SECS,
    BootPacer,
)
from kiro_crew.testing.wait import await_until


class _Clock:
    """The pacer's clock and its sleep: a sleep advances the clock and yields once."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, secs: float) -> None:
        self.now += secs
        await asyncio.sleep(0)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _postures(clock: _Clock, schedule: "list[tuple[float, str]]"):
    """A reader that answers the last posture whose start time has passed."""

    async def _read() -> str:
        current = schedule[0][1]
        for start, posture in schedule:
            if clock.now >= start:
                current = posture
        return current

    return _read


async def _release_times(pacer: BootPacer, clock: _Clock, ids: "list[str]") -> "list[float]":
    released: dict[str, float] = {}

    async def _one(loop_id: str) -> None:
        await pacer.wait_turn(loop_id)
        released[loop_id] = clock.now

    pacer.queue(ids)
    await await_until(asyncio.gather(*(_one(i) for i in ids)), what="every release")
    return [released[i] for i in ids]


def test_an_ample_host_releases_two_dozen_loops_within_a_minute(clock):
    pacer = BootPacer(_postures(clock, [(0.0, "ample")]), clock=clock, sleep=clock.sleep)
    ids = [f"loop-{n}" for n in range(24)]
    times = asyncio.run(_release_times(pacer, clock, ids))
    assert times == [n * BOOT_WAKE_GAP_SECS for n in range(24)], "in order, one short gap apart"
    assert times[-1] <= 60.0


def test_a_tight_host_spaces_releases_by_the_tight_gap(clock):
    pacer = BootPacer(_postures(clock, [(0.0, "tight")]), clock=clock, sleep=clock.sleep)
    times = asyncio.run(_release_times(pacer, clock, ["a", "b", "c"]))
    assert times == [0.0, BOOT_WAKE_TIGHT_GAP_SECS, 2 * BOOT_WAKE_TIGHT_GAP_SECS]


def test_a_tight_host_that_recovers_goes_back_to_the_short_gap(clock):
    pacer = BootPacer(
        _postures(clock, [(0.0, "tight"), (10.0, "ample")]), clock=clock, sleep=clock.sleep
    )
    times = asyncio.run(_release_times(pacer, clock, ["a", "b", "c"]))
    assert times[0] == 0.0
    assert 10.0 <= times[1] < BOOT_WAKE_TIGHT_GAP_SECS, "released when the host recovered"
    assert times[2] == times[1] + BOOT_WAKE_GAP_SECS


def test_a_critical_host_holds_until_it_reads_ample_not_merely_tight(clock):
    schedule = [(0.0, "critical"), (100.0, "tight"), (200.0, "ample")]
    pacer = BootPacer(_postures(clock, schedule), clock=clock, sleep=clock.sleep)
    times = asyncio.run(_release_times(pacer, clock, ["a", "b"]))
    assert times[0] >= 200.0, "even the first loop waits out a critical host"
    assert times[1] == times[0] + BOOT_WAKE_GAP_SECS


def test_an_unknown_posture_paces_like_tight(clock):
    pacer = BootPacer(_postures(clock, [(0.0, "unknown")]), clock=clock, sleep=clock.sleep)
    times = asyncio.run(_release_times(pacer, clock, ["a", "b"]))
    assert times == [0.0, BOOT_WAKE_TIGHT_GAP_SECS]


def test_a_loop_that_leaves_the_queue_lets_the_next_move_up(clock):
    """A cancelled waiter (a user turn, a stop) is forgotten; nothing behind it is lost."""

    async def _drive() -> "dict[str, float]":
        pacer = BootPacer(_postures(clock, [(0.0, "tight")]), clock=clock, sleep=clock.sleep)
        pacer.queue(["a", "b", "c"])
        released: dict[str, float] = {}

        async def _one(loop_id: str) -> None:
            await pacer.wait_turn(loop_id)
            released[loop_id] = clock.now

        first = asyncio.ensure_future(_one("a"))
        second = asyncio.ensure_future(_one("b"))
        third = asyncio.ensure_future(_one("c"))
        await await_until(first, what="the first release")
        second.cancel()
        await await_until(third, what="the third release")
        assert second.cancelled()
        assert not pacer.queued("b")
        return released

    released = asyncio.run(_drive())
    assert set(released) == {"a", "c"}
    assert released["c"] == BOOT_WAKE_TIGHT_GAP_SECS


def test_a_loop_never_queued_is_not_held(clock):
    async def _never() -> str:
        raise AssertionError("an unqueued loop reads no posture")

    pacer = BootPacer(_never, clock=clock, sleep=clock.sleep)

    async def _drive() -> None:
        await await_until(asyncio.ensure_future(pacer.wait_turn("x")), what="an unqueued turn")

    asyncio.run(_drive())
    assert clock.now == 0.0


def test_a_host_that_stays_critical_still_releases_each_loop_after_the_longest_hold(clock):
    """A critical host slows the boot wakes down; it never stops them."""
    pacer = BootPacer(_postures(clock, [(0.0, "critical")]), clock=clock, sleep=clock.sleep)
    times = asyncio.run(_release_times(pacer, clock, ["a", "b"]))
    assert times == [BOOT_WAKE_MAX_HOLD_SECS, 2 * BOOT_WAKE_MAX_HOLD_SECS]


@pytest.mark.parametrize(
    ("probed", "expected"),
    [("ample", "ample"), ("tight", "tight"), ("critical", "critical"), (None, "unknown")],
)
def test_the_host_posture_is_the_memory_probe_alone(monkeypatch, probed, expected):
    """The gateway's disk sample is not read, so a full temp disk holds nothing back."""
    from types import SimpleNamespace

    from kiro_crew import resource_status
    from kiro_crew.autonudge_service import boot_pacer
    from kiro_crew.notifications import resource_pressure

    def _probe(cfg=None):
        if probed is None:
            raise OSError("probe failed")
        return SimpleNamespace(posture=probed)

    monkeypatch.setattr(resource_status, "probe", _probe)
    monkeypatch.setattr(resource_pressure, "_current_posture", ("critical", "temp disk 1% free"))
    assert asyncio.run(boot_pacer.probe_host_posture()) == expected
