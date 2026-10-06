"""push_slots_update paces a long slot list by bytes, not by the fixed floor.

Every slots broadcast re-sends the whole list, so a fixed 200 ms window lets a
busy fleet of 100 agents push five ~150 KB frames a second to every socket.
The window after each broadcast is stretched to ``frame bytes / budget``.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import (
    _SLOTS_BROADCAST_BYTES_PER_S,
    _SLOTS_BROADCAST_INTERVAL_S,
    _SLOTS_BROADCAST_MAX_INTERVAL_S,
    DashboardState,
    _ChatSlot,
    _slots_broadcast_interval_for,
)


@pytest.fixture
def loop():
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
    s._serving_loop = loop
    return s


def _fill(state: DashboardState, n: int) -> None:
    for i in range(n):
        key = f"chat-{i}-1790000000"
        state._slots[key] = _ChatSlot(key, title=f"Load agent {i} working on its task")


class TestIntervalFor:
    def test_small_frame_stays_on_the_floor(self):
        assert _slots_broadcast_interval_for(0) == _SLOTS_BROADCAST_INTERVAL_S
        assert _slots_broadcast_interval_for(40_000) == _SLOTS_BROADCAST_INTERVAL_S

    def test_large_frame_stretches_to_the_byte_budget(self):
        frame = 150_000
        assert _slots_broadcast_interval_for(frame) == pytest.approx(
            frame / _SLOTS_BROADCAST_BYTES_PER_S
        )
        assert _slots_broadcast_interval_for(frame) > _SLOTS_BROADCAST_INTERVAL_S

    def test_ceiling_bounds_staleness(self):
        assert _slots_broadcast_interval_for(50_000_000) == _SLOTS_BROADCAST_MAX_INTERVAL_S


class _FakeClock:
    """Stands in for ``_slots_broadcast_clock``; moves only when a test says so."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# Time the fake clock spends building the leading frame. The trailing window is
# measured from the leading-edge stamp, so the armed delay is the window minus this.
_BUILD_S = 0.05


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr("kiro_crew.dashboard.state._slots_broadcast_clock", fake)
    return fake


class TestWindowFollowsFrameSize:
    def test_long_list_arms_a_stretched_trailing_window(self, state, loop, monkeypatch, clock):
        _fill(state, 100)
        sizes: list[int] = []

        def broadcast(note):
            sizes.append(len(note["slots"]))
            clock.now += _BUILD_S

        state._broadcast = broadcast
        delays: list[float] = []

        def capture(delay, callback, *args, **kwargs):
            delays.append(delay)
            return MagicMock()

        async def _run():
            monkeypatch.setattr(loop, "call_later", capture)
            state.push_slots_update()  # leading edge, measures the frame
            state.push_slots_update()  # absorbed, arms the trailing flush

        loop.run_until_complete(_run())
        assert len(sizes) == 1
        assert sizes[0] > 100_000
        assert len(delays) == 1
        window = sizes[0] / _SLOTS_BROADCAST_BYTES_PER_S
        assert _slots_broadcast_interval_for(sizes[0]) == pytest.approx(window)
        # The deadline is the stretched window minus the leading frame's build time.
        assert delays[0] == pytest.approx(window - _BUILD_S)
        assert delays[0] > 2 * _SLOTS_BROADCAST_INTERVAL_S

    def test_short_list_keeps_the_200ms_window(self, state, loop, monkeypatch, clock):
        _fill(state, 5)

        def broadcast(note):
            clock.now += _BUILD_S

        state._broadcast = broadcast
        delays: list[float] = []

        def capture(delay, callback, *args, **kwargs):
            delays.append(delay)
            return MagicMock()

        async def _run():
            monkeypatch.setattr(loop, "call_later", capture)
            state.push_slots_update()
            state.push_slots_update()

        loop.run_until_complete(_run())
        assert delays == [pytest.approx(_SLOTS_BROADCAST_INTERVAL_S - _BUILD_S)]

    def test_sustained_burst_over_100_slots_sends_fewer_frames(self, state, loop):
        """2 s of pushes every 50 ms: the fixed window sent ~11 full lists."""
        _fill(state, 100)
        frames: list[int] = []
        state._broadcast = lambda note: frames.append(len(note["slots"]))

        async def _run():
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                state.push_slots_update()
                await asyncio.sleep(0.05)
            await asyncio.sleep(_SLOTS_BROADCAST_MAX_INTERVAL_S + 0.1)

        loop.run_until_complete(_run())
        per_second = sum(frames) / 2.0
        assert len(frames) <= 6, f"{len(frames)} full lists in a 2 s burst"
        # Leading frame plus the paced ones; well under the old ~5 frames/s.
        assert per_second < 2.5 * _SLOTS_BROADCAST_BYTES_PER_S

    def test_trailing_frame_after_stretch_carries_latest_state(self, state, loop):
        _fill(state, 100)
        payloads: list[dict] = []
        state._broadcast = lambda note: payloads.append(note)
        probe = state._slots["chat-0-1790000000"]

        async def _run():
            state.push_slots_update()
            probe.title = "changed during the window"
            for _ in range(10):
                state.push_slots_update()
            await asyncio.sleep(_SLOTS_BROADCAST_MAX_INTERVAL_S + 0.1)

        loop.run_until_complete(_run())
        assert len(payloads) == 2, "a burst is one leading plus one trailing frame"
        assert any(s["title"] == "changed during the window" for s in payloads[1]["_slots_list"])


class TestOrderingPathsBypassTheStretch:
    def test_deferred_create_flush_still_publishes_immediately(self, state, loop):
        """The create path's deferred flush must not wait out a stretched window."""
        _fill(state, 100)
        frames: list[dict] = []
        state._broadcast = lambda note: frames.append(note)
        state._slots_broadcast_interval = _SLOTS_BROADCAST_MAX_INTERVAL_S
        state._slots_broadcast_last = time.monotonic()

        async def _run():
            state._deferred_slots_flush("create slot 'x'", 1)

        loop.run_until_complete(_run())
        assert len(frames) == 1

    def test_idle_after_a_stretched_window_is_a_leading_edge(self, state, loop):
        _fill(state, 100)
        frames: list[dict] = []
        state._broadcast = lambda note: frames.append(note)
        state._slots_broadcast_interval = 0.3
        state._slots_broadcast_last = time.monotonic() - 0.31

        async def _run():
            state.push_slots_update()

        loop.run_until_complete(_run())
        assert len(frames) == 1

    def test_partially_constructed_state_keeps_the_floor(self):
        s = DashboardState.__new__(DashboardState)
        assert s._slots_broadcast_interval == _SLOTS_BROADCAST_INTERVAL_S
