"""GATES A3 + A5 — scheduling semantics of parallel() and pipeline().

A3: ``pipeline`` has NO barrier between stages (an item can reach a later stage
before another item finishes an earlier one); ``parallel`` IS a barrier (awaits
all). A5: a failing thunk/stage resolves to ``None`` and the call never raises.

The A3 no-barrier proof is deadlock-or-complete: item 0's stage-1 blocks on an
event that only item 1's stage-2 sets. With a real barrier (all items finish
stage 1 before any starts stage 2) this DEADLOCKS; with no barrier it completes.
A ``wait_for`` timeout turns "barrier regression" into a hard test failure.

See ``docs/system-specs/modules/workflows.md`` and GATES.md (A3, A5).
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.workflows.dsl import parallel, pipeline

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# parallel — barrier + ordering + A5 failure handling
# --------------------------------------------------------------------------- #


async def test_parallel_returns_in_input_order() -> None:
    async def mk(n: int):
        await asyncio.sleep(0.001 * (3 - n))  # later items finish sooner
        return n

    out = await parallel([lambda n=n: mk(n) for n in range(3)])
    assert out == [0, 1, 2]


async def test_parallel_is_a_barrier() -> None:
    """All thunks complete before parallel returns."""
    done = []

    async def mk(n: int):
        await asyncio.sleep(0.001)
        done.append(n)
        return n

    out = await parallel([lambda n=n: mk(n) for n in range(5)])
    assert sorted(done) == [0, 1, 2, 3, 4]
    assert out == [0, 1, 2, 3, 4]


async def test_parallel_failed_thunk_becomes_none_and_does_not_raise() -> None:
    async def good():
        return "ok"

    async def boom():
        raise RuntimeError("explode")

    def sync_boom():
        raise ValueError("sync explode")  # thunk raises before returning awaitable

    out = await parallel([good, boom, sync_boom])
    assert out == ["ok", None, None]


async def test_parallel_accepts_sync_thunks() -> None:
    """Thunks may be sync (return a plain value) or async — both must work."""
    out = await parallel([lambda: 1, lambda: 2])  # sync lambdas, not coroutines
    assert out == [1, 2]


async def test_parallel_accepts_already_created_coroutines() -> None:
    """Regression (wf_000003): authors naturally pass ctx.agent(...) calls — i.e.
    already-created coroutines — directly to parallel, instead of thunks. These
    must run, not silently become None (which previously emptied the result list
    and tripped scripts' 'no usable results' guards)."""
    async def work(n):
        await asyncio.sleep(0)
        return n * 2

    out = await parallel([work(1), work(2), work(3)])  # coroutines, NOT thunks
    assert out == [2, 4, 6]


async def test_parallel_accepts_mixed_thunks_and_coroutines() -> None:
    async def work(n):
        return n

    out = await parallel([work(10), lambda: 20, work(30)])
    assert out == [10, 20, 30]


async def test_parallel_failing_coroutine_becomes_none() -> None:
    async def boom():
        raise ValueError("x")

    async def ok():
        return "ok"

    out = await parallel([ok(), boom()])
    assert out == ["ok", None]


async def test_parallel_empty() -> None:
    assert await parallel([]) == []


async def test_parallel_respects_limit() -> None:
    """With limit=2, no more than 2 thunks run concurrently."""
    active = 0
    peak = 0

    async def mk():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return None

    await parallel([mk for _ in range(6)], limit=2)
    assert peak <= 2


# --------------------------------------------------------------------------- #
# pipeline — no inter-stage barrier (A3)
# --------------------------------------------------------------------------- #


async def test_pipeline_no_inter_stage_barrier_deadlock_proof() -> None:
    gate = asyncio.Event()

    async def stage1(prev, item, idx):
        if item == 0:
            await gate.wait()  # only released by item 1 reaching stage 2
        return prev

    async def stage2(prev, item, idx):
        if item == 1:
            gate.set()
        return prev

    # If pipeline imposed a stage barrier, item 0 would block stage1 forever and
    # item 1 could never reach stage2 → deadlock → wait_for times out.
    out = await asyncio.wait_for(pipeline([0, 1], stage1, stage2), timeout=2.0)
    assert out == [0, 1]


async def test_pipeline_results_in_input_order() -> None:
    async def double(prev, item, idx):
        await asyncio.sleep(0.001 * (5 - item))  # later items finish sooner
        return prev * 2

    out = await pipeline([1, 2, 3], double)
    assert out == [2, 4, 6]


async def test_pipeline_stage_receives_prev_item_index() -> None:
    seen = []

    async def s1(prev, item, idx):
        seen.append(("s1", prev, item, idx))
        return prev + 100

    async def s2(prev, item, idx):
        seen.append(("s2", prev, item, idx))
        return prev

    out = await pipeline([10, 20], s1, s2)
    assert out == [110, 120]
    # stage 0 gets prev == item; later stage gets previous result.
    assert ("s1", 10, 10, 0) in seen
    assert ("s2", 110, 10, 0) in seen


async def test_pipeline_arity_adapted_single_arg_stage() -> None:
    async def just_prev(prev):
        return prev + 1

    out = await pipeline([1, 2], just_prev, just_prev)
    assert out == [3, 4]


async def test_pipeline_accepts_sync_stages() -> None:
    """Stages may be sync (return a plain value) or async — both must work."""
    out = await pipeline([1, 2], lambda prev: prev * 10)  # sync lambda stage
    assert out == [10, 20]


async def test_pipeline_mixed_sync_and_async_stages() -> None:
    async def a_stage(prev):
        return prev + 1

    out = await pipeline([1, 2], lambda prev: prev * 10, a_stage)
    assert out == [11, 21]


async def test_pipeline_failed_stage_drops_item_to_none() -> None:
    async def s1(prev, item, idx):
        if item == 1:
            raise RuntimeError("stage failure on item 1")
        return prev

    async def s2(prev, item, idx):
        return prev * 10

    out = await pipeline([0, 1, 2], s1, s2)
    assert out == [0, None, 20]


async def test_pipeline_empty_and_no_stages() -> None:
    assert await pipeline([]) == []
    assert await pipeline([1, 2, 3]) == [1, 2, 3]  # no stages → passthrough


async def test_pipeline_respects_limit() -> None:
    active = 0
    peak = 0

    async def stage(prev, item, idx):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return prev

    await pipeline(list(range(6)), stage, limit=2)
    assert peak <= 2
