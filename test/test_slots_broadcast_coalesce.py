"""Tests for push_slots_update leading+trailing edge coalescing."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import (
    _SLOTS_BROADCAST_INTERVAL_S,
    DashboardState,
    _ChatSlot,
)


@pytest.fixture
def loop():
    """A dedicated loop, so call_later handles are driven deterministically."""
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)
    yield new_loop
    new_loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def state(monkeypatch, tmp_path, loop):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    s = DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
    )
    s.is_yolo_active = lambda: False
    # Production captures this on the first call from the loop thread; pinning it
    # here keeps each test's timing independent of construction order.
    s._serving_loop = loop
    return s


class TestBurstCoalescing:
    """A burst of N rapid calls produces exactly 1 leading + 1 trailing broadcast."""

    def test_burst_produces_leading_plus_trailing(self, state, loop):
        broadcasts: list[dict] = []
        state._broadcast = lambda note: broadcasts.append(note)

        async def _run_burst():
            state.push_slots_update()
            assert len(broadcasts) == 1, "Leading edge should broadcast immediately"

            for _ in range(5):
                state.push_slots_update()
            assert len(broadcasts) == 1, "Burst within window should not add broadcasts"

            await asyncio.sleep(_SLOTS_BROADCAST_INTERVAL_S + 0.05)
            assert len(broadcasts) == 2, (
                f"Expected exactly 1 trailing broadcast after timer, got {len(broadcasts)}"
            )

        loop.run_until_complete(_run_burst())


class TestTrailingCarriesLatestState:
    """The trailing broadcast carries the LATEST state, not a stale snapshot."""

    def test_trailing_reflects_mutation(self, state, loop):
        payloads: list[dict] = []
        state._broadcast = lambda note: payloads.append(note)

        slot = _ChatSlot("test-1-100", title="before")
        state._slots["test-1-100"] = slot

        async def _run():
            state.push_slots_update()
            assert len(payloads) == 1
            assert any(s["title"] == "before" for s in payloads[0]["_slots_list"])

            slot.title = "after"
            state.push_slots_update()

            await asyncio.sleep(_SLOTS_BROADCAST_INTERVAL_S + 0.05)
            assert len(payloads) == 2
            assert any(s["title"] == "after" for s in payloads[1]["_slots_list"]), (
                "Trailing broadcast must carry the latest (mutated) state"
            )

        loop.run_until_complete(_run())


class TestIsolatedCallImmediate:
    """An isolated single call broadcasts immediately with no delay."""

    def test_single_call_no_delay(self, state, loop):
        broadcasts: list[float] = []
        state._broadcast = lambda note: broadcasts.append(time.monotonic())

        async def _run():
            before = time.monotonic()
            state.push_slots_update()
            after = time.monotonic()

            assert len(broadcasts) == 1
            assert broadcasts[0] - before < 0.05, "Single call should broadcast instantly"
            assert after - before < 0.05, "push_slots_update should return quickly"

        loop.run_until_complete(_run())


class TestSuspendTakesPriority:
    """suspend_slots_push() still wins: nothing leaves the block until exit."""

    def test_suspend_defers_then_emits_once(self, state, loop):
        broadcasts: list[dict] = []
        state._broadcast = lambda note: broadcasts.append(note)

        async def _run():
            with state.suspend_slots_push():
                for _ in range(4):
                    state.push_slots_update()
                assert broadcasts == [], "Suspended block must not broadcast"
            assert len(broadcasts) == 1, "Exiting suspension emits exactly one broadcast"

        loop.run_until_complete(_run())


class TestNonEventLoopThread:
    """Calling from a non-event-loop thread does not raise and does not lose."""

    def test_cross_thread_call(self, state, loop):
        broadcasts: list[dict] = []
        state._broadcast = lambda note: broadcasts.append(note)

        # Prime the window: with a RECENT last-broadcast the worker thread cannot
        # take the leading edge, so it must reach the threadsafe scheduling path.
        state._slots_broadcast_last = time.monotonic()

        errors: list[Exception] = []

        def worker():
            try:
                state.push_slots_update()
            except Exception as e:  # noqa: BLE001 - the assertion is "did not raise"
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2.0)

        assert not errors, f"Cross-thread call raised: {errors}"
        assert broadcasts == [], "In-window cross-thread call must not broadcast inline"

        # Drive the loop so call_soon_threadsafe runs _schedule_trailing_flush.
        loop.run_until_complete(asyncio.sleep(0.05))
        assert state._slots_broadcast_timer is not None, (
            "_schedule_trailing_flush must have armed the trailing timer"
        )

        loop.run_until_complete(asyncio.sleep(_SLOTS_BROADCAST_INTERVAL_S + 0.1))
        assert len(broadcasts) == 1, "Update from non-loop thread must not be lost"

    def test_dead_loop_falls_back_to_inline_broadcast(self, state):
        """Negative control: with no usable loop the update is still delivered."""
        broadcasts: list[dict] = []
        state._broadcast = lambda note: broadcasts.append(note)

        dead = asyncio.new_event_loop()
        dead.close()
        state._serving_loop = dead
        state._slots_broadcast_last = time.monotonic()

        state.push_slots_update()

        assert len(broadcasts) == 1, (
            "A closed loop must degrade to an inline broadcast, not silently drop"
        )
        assert state._slots_broadcast_timer is None, "No timer can be armed on a closed loop"
