"""Snapshot of how many ``session_projection`` frames one turn puts on the wire.

``session_projection`` goes to every owner socket, whichever slot each one shows, so
its count is turns times folds moved times sockets: at the scale bed's shape it was
28,268 frames and 155 MB per run. This test records the count for one turn at the
bed's six sockets as a fixed number, so a change that sends more (a new advertised
session fold, a lost coalescing window, a send per socket per fold) shows up as a
diff in review rather than on the bed.

The turn is the entry pair a plain reply writes (``turn/started``, ``turn/completed``)
folded in one eager batch and sent in one coalescing window, the fewest frames a
turn can cost. A turn whose entries span several windows sends one more set per
window.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, bus, eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard.handlers import crew_log as routes
from kiro_crew.dashboard.handlers.crew_log import FRAME, CrewLogPublisher
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.testing.wait import async_wait_until

SOCKETS = 6
SESSION = "s-frame-volume"

# Measured on the main this file was written against: a session's first turn moves all
# six advertised session folds, and each is sent once to each of the six sockets.
SESSION_PROJECTION_FRAMES_PER_TURN = 36


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    bus.reset_for_tests()
    emit.reset_caches()
    yield
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    bus.reset_for_tests()
    emit.reset_caches()


class _OwnerWS:
    """An owner socket whose sends are recorded by frame type, not written."""

    def __init__(self) -> None:
        self.closed = False
        self.send_str = AsyncMock()
        self._flags = {"_is_dashboard_user": True}

    def get(self, key, default=None):
        return self._flags.get(key, default)

    def count(self, frame_type: str) -> int:
        return sum(
            1 for c in self.send_str.call_args_list if f'"type": "{frame_type}"' in c.args[0]
        )


def _one_turn(handle: CrewLog) -> int:
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    done = handle.append(
        "turn/completed",
        {
            "turn": 1,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 900,
            "model": "opus",
            "provider": "kiro",
            "credits": 0.5,
            "tokens": {"input": 100, "output": 20, "cache_read": 5, "cache_write": 1},
        },
        src="gateway",
    )
    return int(done.seq)


@pytest.mark.asyncio
async def test_one_turn_sends_a_fixed_number_of_session_projection_frames(monkeypatch) -> None:
    # The coalescing window is driven by hand below; a timer that fired first would
    # send and clear the held values before the test could take them.
    monkeypatch.setattr(routes, "COALESCE_SECONDS", 3600.0)
    state = DashboardState(
        sessions=MagicMock(count=0), crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    sockets = [_OwnerWS() for _ in range(SOCKETS)]
    for ws in sockets:
        state.register_ws(ws, owner=True)  # type: ignore[arg-type]
    publisher = CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop(), state)
    publisher.subscribe_bus()
    try:
        handle = CrewLog.create(lg.KIND_SESSION, SESSION, owner="raymond", agent="kirocrew")
        eager.note_commit(SESSION, "turn/completed", _one_turn(handle))
        assert await asyncio.to_thread(eager.drain, 10.0)
        # Every fold callback was queued on this loop before ``to_thread`` queued its
        # own completion, so by now each one has run and the window is armed.
        await async_wait_until(
            lambda: publisher._sessions_armed,
            describe=lambda: "the folded values were queued for the coalescing window",
        )
        # The window's own send, run now instead of after COALESCE_SECONDS.
        pending, publisher._pending = publisher._pending, {}
        publisher._sessions_armed = False
        await publisher._send_pending(pending)
    finally:
        publisher.unsubscribe_bus()

    per_socket = [ws.count(FRAME) for ws in sockets]
    assert len(set(per_socket)) == 1, per_socket
    total = sum(per_socket)
    assert total == SESSION_PROJECTION_FRAMES_PER_TURN, (
        f"one turn sent {total} {FRAME} frames to {SOCKETS} sockets "
        f"({per_socket[0]} each, folds {sorted(fold for _s, fold in pending)}); "
        f"the snapshot is {SESSION_PROJECTION_FRAMES_PER_TURN}"
    )
