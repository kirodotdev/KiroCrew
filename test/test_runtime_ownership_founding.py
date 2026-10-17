"""A founding spawn must not hold the lease table against everyone else.

A real launch inside ``RuntimeOwnership.acquire`` must not make every other
acquire and every release wait it out. These drive a ``spawn`` the test
controls, so the launch is held open for exactly as long as each assertion
needs.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import runtime_ownership as ro
from kiro_crew.testing.wait import await_until


class _Runtime:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def is_alive(self) -> bool:
        return True


class _Launch:
    """A spawn callback that starts, then blocks until the test lets it finish."""

    def __init__(self, pid: int, *, fail: BaseException | None = None) -> None:
        self.pid = pid
        self.fail = fail
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.calls = 0

    async def __call__(self) -> _Runtime:
        self.calls += 1
        self.started.set()
        await self.finish.wait()
        if self.fail is not None:
            raise self.fail
        return _Runtime(self.pid)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _wait(event: asyncio.Event) -> None:
    await await_until(event)


@pytest.mark.asyncio
async def test_foundings_on_different_keys_overlap() -> None:
    reg = ro.RuntimeOwnership()
    a, b = _Launch(1), _Launch(2)
    ta = asyncio.create_task(reg.acquire("key-a", "s-a", a, cap=2))
    await _wait(a.started)
    tb = asyncio.create_task(reg.acquire("key-b", "s-b", b, cap=2))
    # B's launch must begin while A's is still in flight.
    await _wait(b.started)
    a.finish.set()
    b.finish.set()
    got_a, got_b = await await_until(asyncio.gather(ta, tb))
    assert (got_a.runtime.pid, got_b.runtime.pid) == (1, 2)
    assert not got_a.joined and not got_b.joined


@pytest.mark.asyncio
async def test_a_release_completes_while_a_spawn_is_in_flight() -> None:
    reg = ro.RuntimeOwnership()
    held = await reg.acquire("key-a", "s-old", _instant(9), cap=1)
    launch = _Launch(1)
    task = asyncio.create_task(reg.acquire("key-b", "s-new", launch, cap=1))
    await _wait(launch.started)
    released = await await_until(asyncio.ensure_future(reg.release(held.lease)))
    assert released is not None and released.pid == 9
    launch.finish.set()
    await await_until(task)


@pytest.mark.asyncio
async def test_a_same_key_burst_under_cap_founds_one_runtime() -> None:
    reg = ro.RuntimeOwnership()
    founder, second = _Launch(1), _Launch(2)
    t1 = asyncio.create_task(reg.acquire("k", "s1", founder, cap=2))
    await _wait(founder.started)
    t2 = asyncio.create_task(reg.acquire("k", "s2", second, cap=2))
    await _settle()
    assert second.calls == 0, "an arrival with room on the launch waits for it"
    founder.finish.set()
    first, joined = await await_until(asyncio.gather(t1, t2))
    assert joined.runtime is first.runtime and joined.joined and not first.joined
    assert first.lease != joined.lease and joined.leases_on_runtime == 2


@pytest.mark.asyncio
async def test_an_arrival_with_no_room_on_the_launch_founds_concurrently() -> None:
    """At cap=1 a launch is full with its founder, so waiting would only
    serialize two launches -- the stall being removed."""
    reg = ro.RuntimeOwnership()
    first, second = _Launch(1), _Launch(2)
    t1 = asyncio.create_task(reg.acquire("k", "s1", first, cap=1))
    await _wait(first.started)
    t2 = asyncio.create_task(reg.acquire("k", "s2", second, cap=1))
    await _wait(second.started)
    first.finish.set()
    second.finish.set()
    a, b = await await_until(asyncio.gather(t1, t2))
    assert a.runtime is not b.runtime and not a.joined and not b.joined


@pytest.mark.asyncio
async def test_waiters_beyond_the_launchs_room_found_concurrently() -> None:
    reg = ro.RuntimeOwnership()
    founder, waiter, third = _Launch(1), _Launch(2), _Launch(3)
    t1 = asyncio.create_task(reg.acquire("k", "s1", founder, cap=2))
    await _wait(founder.started)
    t2 = asyncio.create_task(reg.acquire("k", "s2", waiter, cap=2))
    await _settle()
    t3 = asyncio.create_task(reg.acquire("k", "s3", third, cap=2))
    await _wait(third.started)
    assert waiter.calls == 0
    founder.finish.set()
    third.finish.set()
    r1, r2, r3 = await await_until(asyncio.gather(t1, t2, t3))
    assert r2.runtime is r1.runtime and r3.runtime is not r1.runtime


@pytest.mark.asyncio
async def test_a_raising_spawn_leaves_no_marker_and_no_parked_waiter() -> None:
    reg = ro.RuntimeOwnership()
    founder = _Launch(1, fail=RuntimeError("launch failed"))
    t1 = asyncio.create_task(reg.acquire("k", "s1", founder, cap=2))
    await _wait(founder.started)
    t2 = asyncio.create_task(reg.acquire("k", "s2", _instant(2), cap=2))
    await _settle()
    founder.finish.set()
    with pytest.raises(RuntimeError, match="launch failed"):
        await await_until(t1)
    # The waiter is not handed the founder's error; it founds on its own.
    got = await await_until(t2)
    assert got.runtime.pid == 2 and not got.joined
    assert reg._foundings == []


@pytest.mark.asyncio
async def test_a_cancelled_founder_wakes_its_waiters_and_clears_its_marker() -> None:
    reg = ro.RuntimeOwnership()
    founder = _Launch(1)
    t1 = asyncio.create_task(reg.acquire("k", "s1", founder, cap=2))
    await _wait(founder.started)
    t2 = asyncio.create_task(reg.acquire("k", "s2", _instant(2), cap=2))
    await _settle()
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(t1)
    got = await await_until(t2)
    assert got.runtime.pid == 2 and reg._foundings == [] and len(reg._entries) == 1


@pytest.mark.asyncio
async def test_a_cancelled_waiter_does_not_cancel_the_shared_launch() -> None:
    reg = ro.RuntimeOwnership()
    founder = _Launch(1)
    t1 = asyncio.create_task(reg.acquire("k", "s1", founder, cap=3))
    await _wait(founder.started)
    quitter = asyncio.create_task(reg.acquire("k", "s2", _instant(2), cap=3))
    stayer = asyncio.create_task(reg.acquire("k", "s3", _instant(3), cap=3))
    await _settle()
    quitter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(quitter)
    founder.finish.set()
    first, joined = await await_until(asyncio.gather(t1, stayer))
    assert joined.runtime is first.runtime and joined.leases_on_runtime == 2


@pytest.mark.asyncio
async def test_a_cancelled_waiter_returns_its_reservation() -> None:
    """Otherwise a quitter keeps a seat on the launch, and the next arrival
    founds a second process although the first has room for it."""
    reg = ro.RuntimeOwnership()
    founder, late = _Launch(1), _Launch(9)
    t1 = asyncio.create_task(reg.acquire("k", "s1", founder, cap=2))
    await _wait(founder.started)
    quitter = asyncio.create_task(reg.acquire("k", "s2", _instant(2), cap=2))
    await _settle()
    quitter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(quitter)
    t3 = asyncio.create_task(reg.acquire("k", "s3", late, cap=2))
    await _settle()
    assert late.calls == 0
    founder.finish.set()
    first, joined = await await_until(asyncio.gather(t1, t3))
    assert joined.runtime is first.runtime


def _instant(pid: int):
    async def spawn() -> _Runtime:
        return _Runtime(pid)

    return spawn
