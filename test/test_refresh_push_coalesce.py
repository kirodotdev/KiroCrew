"""push_refresh sends at most one ``turn_end`` frame per window; other kinds go at once."""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.state_parts import refresh_coalesce

WINDOW = 1.5


@pytest.fixture(autouse=True)
def _fixed_window(_floor_monkeypatch):
    """Every behaviour test runs on a 1.5 s window, whatever the shell's
    ``KIROCREW_REFRESH_COALESCE_S`` set at import."""
    _floor_monkeypatch.setattr(refresh_coalesce, "REFRESH_COALESCE_WINDOW_S", WINDOW)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def loop():
    new_loop = asyncio.new_event_loop()
    yield new_loop
    new_loop.close()


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(refresh_coalesce, "_clock", fake)
    return fake


@pytest.fixture
def state(loop):
    s = DashboardState.__new__(DashboardState)
    s._serving_loop = loop
    frames: list[tuple[float, str]] = []
    s._broadcast = lambda note: frames.append((refresh_coalesce._clock(), note["kinds"]))  # type: ignore[method-assign]
    s.frames = frames  # type: ignore[attr-defined]
    return s


def _kinds(state: DashboardState) -> list[str]:
    return [kinds for _, kinds in state.frames]  # type: ignore[attr-defined]


TURN_END = refresh_coalesce.TURN_END_KIND
HISTORY = refresh_coalesce.HISTORY_KIND


def _timer(state: DashboardState, kind: str = TURN_END) -> "asyncio.TimerHandle | None":
    coalescer = getattr(state, refresh_coalesce._STATE_ATTR[kind])
    return coalescer._timer if coalescer is not None else None


def _fire(state: DashboardState, kind: str = TURN_END) -> None:
    timer = _timer(state, kind)
    assert timer is not None
    timer._run()  # type: ignore[attr-defined]


def test_burst_sends_leading_then_one_trailing_frame(state, loop, clock):
    async def _run() -> None:
        state.push_refresh("turn_end")
        assert _kinds(state) == ["turn_end"]
        for _ in range(20):
            clock.now += 0.05
            state.push_refresh("turn_end")
        assert _kinds(state) == ["turn_end"]
        timer = _timer(state)
        assert timer is not None
        assert timer.when() - loop.time() <= WINDOW
        _fire(state)
        assert _kinds(state) == ["turn_end", "turn_end"]
        assert _timer(state) is None

    loop.run_until_complete(_run())


def test_lone_push_after_quiet_window_is_sent_at_once(state, loop, clock):
    async def _run() -> None:
        state.push_refresh("turn_end")
        clock.now += WINDOW
        state.push_refresh("turn_end")
        assert _kinds(state) == ["turn_end", "turn_end"]
        assert _timer(state) is None

    loop.run_until_complete(_run())


def test_steady_stream_sends_one_frame_per_window(state, loop, clock):
    async def _run() -> None:
        start = clock.now
        for _ in range(3000):
            state.push_refresh("turn_end")
            clock.now += 0.02
            timer = _timer(state)
            if timer is not None and clock.now >= state._refresh_coalescer._last_sent + WINDOW:
                _fire(state)
        span = clock.now - start
        sent_at = [at for at, _ in state.frames]
        assert len(sent_at) <= span / WINDOW + 1
        assert all(b - a >= WINDOW - 1e-9 for a, b in zip(sent_at, sent_at[1:]))
        assert state._refresh_coalescer.pushes_offered == 3000
        assert state._refresh_coalescer.frames_sent == len(sent_at)

    loop.run_until_complete(_run())


def test_trailing_frame_is_delivered_by_the_loop(state, loop, clock, monkeypatch):
    monkeypatch.setattr(refresh_coalesce, "REFRESH_COALESCE_WINDOW_S", 0.02)
    trailing = asyncio.Event()
    frames = state.frames

    def _record(note: dict) -> None:
        frames.append((0.0, note["kinds"]))
        if len(frames) == 2:
            trailing.set()

    state._broadcast = _record

    async def _run() -> None:
        state.push_refresh("turn_end")
        state.push_refresh("turn_end")
        assert _kinds(state) == ["turn_end"]
        try:
            await asyncio.wait_for(trailing.wait(), timeout=30)
        except asyncio.TimeoutError:
            pytest.fail(f"trailing frame never delivered; frames={frames!r}")
        assert _timer(state) is None

    loop.run_until_complete(_run())


@pytest.mark.parametrize("kind", ["history", "crons", "cron_history", "agents"])
def test_user_driven_and_other_kinds_are_immediate(state, loop, clock, kind):
    async def _run() -> None:
        state.push_refresh("turn_end")
        state.push_refresh("turn_end")
        state.push_refresh(kind)
        assert _kinds(state) == ["turn_end", kind]

    loop.run_until_complete(_run())


def test_a_push_off_the_serving_loop_is_sent_at_once(state, clock):
    state.push_refresh("turn_end")
    state.push_refresh("turn_end")
    assert _kinds(state) == ["turn_end", "turn_end"]
    assert _timer(state) is None


def test_a_zero_window_sends_every_push(state, loop, clock, monkeypatch):
    monkeypatch.setattr(refresh_coalesce, "REFRESH_COALESCE_WINDOW_S", 0.0)

    async def _run() -> None:
        for _ in range(3):
            state.push_refresh("turn_end")
        assert _kinds(state) == ["turn_end"] * 3
        assert _timer(state) is None

    loop.run_until_complete(_run())


@pytest.mark.parametrize(
    ("raw", "window"),
    [("", 1.5), ("0", 0.0), ("-2", 0.0), ("0.25", 0.25), ("nan", 1.5), ("x", 1.5)],
)
def test_window_reads_the_rollback_switch(monkeypatch, raw, window):
    monkeypatch.setenv("KIROCREW_REFRESH_COALESCE_S", raw)
    assert refresh_coalesce._window_from_env() == window


def test_close_all_ws_sends_the_pending_frame_before_the_sockets_close(
    state, loop, clock, monkeypatch
):
    from kiro_crew.dashboard import state as state_facade

    class _Hub:
        async def close_all_ws(self) -> None:
            state.frames.append((clock.now, "sockets closed"))

    monkeypatch.setattr(state_facade, "_websocket_for", lambda _state: _Hub())

    async def _run() -> None:
        state.push_refresh("turn_end")
        state.push_refresh("turn_end")
        assert _timer(state) is not None
        await state.close_all_ws()
        assert _kinds(state) == ["turn_end", "turn_end", "sockets closed"]
        assert _timer(state) is None

    loop.run_until_complete(_run())


def test_a_bulk_listing_burst_sends_one_leading_and_one_trailing_history_frame(state, loop, clock):
    async def _run() -> None:
        for _ in range(100):
            state.push_listing_refresh()
            clock.now += 0.001
        assert _kinds(state) == [HISTORY]
        assert _timer(state, HISTORY) is not None
        _fire(state, HISTORY)
        assert _kinds(state) == [HISTORY, HISTORY]
        assert _timer(state, HISTORY) is None
        assert state._history_coalescer.pushes_offered == 100
        assert state._history_coalescer.frames_sent == 2

    loop.run_until_complete(_run())


def test_the_turn_end_and_history_coalescers_are_independent(state, loop, clock):
    async def _run() -> None:
        state.push_refresh(TURN_END)
        state.push_listing_refresh()
        assert _kinds(state) == [TURN_END, HISTORY]
        state.push_refresh(TURN_END)
        state.push_listing_refresh()
        assert _timer(state, TURN_END) is not None
        assert _timer(state, HISTORY) is not None
        _fire(state, HISTORY)
        assert _kinds(state) == [TURN_END, HISTORY, HISTORY]
        assert _timer(state, TURN_END) is not None
        assert state._refresh_coalescer is not state._history_coalescer

    loop.run_until_complete(_run())


def test_close_all_ws_sends_a_pending_history_frame_too(state, loop, clock, monkeypatch):
    from kiro_crew.dashboard import state as state_facade

    class _Hub:
        async def close_all_ws(self) -> None:
            state.frames.append((clock.now, "sockets closed"))

    monkeypatch.setattr(state_facade, "_websocket_for", lambda _state: _Hub())

    async def _run() -> None:
        state.push_listing_refresh()
        state.push_listing_refresh()
        assert _timer(state, HISTORY) is not None
        await state.close_all_ws()
        assert _kinds(state) == [HISTORY, HISTORY, "sockets closed"]
        assert _timer(state, HISTORY) is None

    loop.run_until_complete(_run())
