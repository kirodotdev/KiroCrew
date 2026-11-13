"""The gateway-wide ``heartbeat`` tick: one frame per socket per interval.

A running turn holds the tick and releases it when it ends. While any turn holds
it, ``WebSocketHub.broadcast_heartbeat`` sends each socket one ``heartbeat`` frame
per interval, however many turns run. These pin the frame count at the scale
bed's shape, that a closed socket gets nothing more, that the tick stops when no
turn runs and starts again on the next one, that an app-token socket hears it only
while a slot it may see runs, that shutdown stops it, and that ``_run_chat`` holds
it for the turn and sends no per-slot heartbeat of its own.

Time is a fake clock: the tick's ``sleep`` parks on a future the test resolves, so
nothing waits on real time.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from chat_runner_coverage_helpers import _complete, _drive, _runner_state, _slot

from kiro_crew.dashboard.state_parts.dashboard_services import _websocket_for
from kiro_crew.dashboard.turn_heartbeat import HEARTBEAT_INTERVAL_S, TurnHeartbeat
from kiro_crew.dashboard.websocket_hub import WebSocketHub

#: Heartbeat frames per run at fixed shapes, so a change to the tick's volume
#: shows up as a diff in review. Keys name (turns, sockets, fake seconds run).
#: The per-slot broadcast this replaced sent turns x sockets frames per interval:
#: 18,000 for the first row, 6 for the second.
HEARTBEAT_FRAME_SNAPSHOT = {
    "300 turns, 60 sockets, 5 s": 60,
    "1 turn, 1 socket, 30 s": 6,
    "1 turn, 1 socket, 4 s": 0,
}


class _FakeClock:
    """A ``sleep`` that parks until the test advances the clock past its deadline."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    async def sleep(self, seconds: float) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds, fut))
        await fut

    @property
    def parked(self) -> int:
        return sum(1 for _, fut in self._sleepers if not fut.done())

    async def advance(self, seconds: float) -> None:
        """Move time forward in interval-sized steps, waking each due sleeper."""
        end = self.now + seconds
        # A tick started since the last advance has not reached its first sleep.
        for _ in range(5):
            await asyncio.sleep(0)
        while True:
            due = [d for d, fut in self._sleepers if not fut.done() and d <= end]
            if not due:
                break
            self.now = min(due)
            for deadline, fut in self._sleepers:
                if deadline <= self.now and not fut.done():
                    fut.set_result(None)
            # Let the woken tick run its send and park on its next sleep.
            for _ in range(5):
                await asyncio.sleep(0)
        self.now = end


class _FakeWs(dict):
    """A socket the hub can fan out to: a mapping of flags plus ``closed``."""

    def __init__(self, *, dashboard_user: bool = True) -> None:
        super().__init__()
        self["_is_dashboard_user"] = dashboard_user
        self.closed = False

    # The hub keeps sockets in sets, as aiohttp's response hashes by identity.
    __hash__ = object.__hash__

    def __eq__(self, other: object) -> bool:
        return self is other


def _hub(sockets: list[_FakeWs], clock: _FakeClock, *, allowed=None):
    """A hub over *sockets* whose sends are recorded instead of written."""
    sent: list[tuple[_FakeWs, str]] = []

    def client_allowed(ws, msg_type, data):
        if allowed is not None:
            return allowed(ws, msg_type, data)
        return bool(ws.get("_is_dashboard_user"))

    owner = SimpleNamespace(
        _ws_clients=list(sockets),
        _owner_ws_clients=set(),
        _ws_log_subscribers=set(),
        _ws_subagent_subscribers=set(),
        _background_tasks=set(),
        _flush_task=None,
        _spawn_ws_send=lambda ws, msg: sent.append((ws, msg)),
        _ws_client_allowed=client_allowed,
    )
    hub = WebSocketHub(
        owner,
        serving_loop_provider=lambda: None,
        logger_provider=lambda: __import__("logging").getLogger("test"),
    )
    hub.turn_heartbeat = TurnHeartbeat(hub.broadcast_heartbeat, sleep=clock.sleep)
    return hub, owner, sent


def _turns(n: int) -> list[object]:
    return [object() for _ in range(n)]


async def _run(turns: int, sockets: int, seconds: float) -> int:
    clock = _FakeClock()
    hub, _owner, sent = _hub([_FakeWs() for _ in range(sockets)], clock)
    holders = _turns(turns)
    for i, holder in enumerate(holders):
        hub.turn_heartbeat.hold(holder, f"chat-{i}")
    await clock.advance(seconds)
    for holder in holders:
        hub.turn_heartbeat.release(holder)
    await hub.turn_heartbeat.stop()
    return len(sent)


@pytest.mark.asyncio
async def test_frame_counts_match_the_snapshot():
    counts = {
        "300 turns, 60 sockets, 5 s": await _run(300, 60, HEARTBEAT_INTERVAL_S),
        "1 turn, 1 socket, 30 s": await _run(1, 1, 30.0),
        "1 turn, 1 socket, 4 s": await _run(1, 1, 4.0),
    }
    assert counts == HEARTBEAT_FRAME_SNAPSHOT


@pytest.mark.asyncio
async def test_one_frame_per_socket_per_window_at_bed_scale():
    clock = _FakeClock()
    sockets = [_FakeWs() for _ in range(60)]
    hub, _owner, sent = _hub(sockets, clock)
    for i, holder in enumerate(_turns(300)):
        hub.turn_heartbeat.hold(holder, f"chat-{i}")

    await clock.advance(HEARTBEAT_INTERVAL_S)

    assert len(sent) == 60
    assert {id(ws) for ws, _ in sent} == {id(ws) for ws in sockets}
    frame = json.loads(sent[0][1])
    assert frame["type"] == "heartbeat"
    assert set(frame["data"]) == {"ts"}
    await hub.turn_heartbeat.stop()


@pytest.mark.asyncio
async def test_a_closed_socket_gets_no_further_frames():
    clock = _FakeClock()
    stays, leaves = _FakeWs(), _FakeWs()
    hub, owner, sent = _hub([stays, leaves], clock)
    hub.turn_heartbeat.hold(object(), "chat-1")
    await clock.advance(HEARTBEAT_INTERVAL_S)
    assert [ws for ws, _ in sent] == [stays, leaves]

    leaves.closed = True
    sent.clear()
    await clock.advance(2 * HEARTBEAT_INTERVAL_S)

    assert [ws for ws, _ in sent] == [stays, stays]
    assert leaves not in owner._ws_clients
    await hub.turn_heartbeat.stop()


@pytest.mark.asyncio
async def test_the_tick_idles_with_no_turn_and_resumes_on_the_next():
    clock = _FakeClock()
    hub, _owner, sent = _hub([_FakeWs()], clock)
    tick = hub.turn_heartbeat
    first = object()
    tick.hold(first, "chat-1")
    await clock.advance(HEARTBEAT_INTERVAL_S)
    assert len(sent) == 1

    tick.release(first)
    await clock.advance(HEARTBEAT_INTERVAL_S)
    assert not tick.running
    assert clock.parked == 0
    await clock.advance(10 * HEARTBEAT_INTERVAL_S)
    assert len(sent) == 1

    tick.hold(object(), "chat-2")
    assert tick.running
    await clock.advance(HEARTBEAT_INTERVAL_S)
    assert len(sent) == 2
    await tick.stop()


@pytest.mark.asyncio
async def test_a_release_and_hold_inside_one_window_keeps_one_task():
    clock = _FakeClock()
    hub, _owner, sent = _hub([_FakeWs()], clock)
    tick = hub.turn_heartbeat
    first, second = object(), object()
    tick.hold(first, "chat-1")
    task = tick._task
    tick.release(first)
    tick.hold(second, "chat-1")
    assert tick._task is task
    await clock.advance(HEARTBEAT_INTERVAL_S)
    assert len(sent) == 1
    await tick.stop()


@pytest.mark.asyncio
async def test_an_app_socket_hears_the_tick_only_while_a_slot_it_sees_runs():
    clock = _FakeClock()
    owner_tab, app = _FakeWs(), _FakeWs(dashboard_user=False)

    def allowed(ws: Any, msg_type: str, data: dict) -> bool:
        assert msg_type == "heartbeat"
        return bool(ws.get("_is_dashboard_user")) or data.get("slot") == "chat-app"

    hub, _owner, sent = _hub([owner_tab, app], clock, allowed=allowed)
    tick = hub.turn_heartbeat
    user_turn, app_turn = object(), object()
    tick.hold(user_turn, "chat-user")
    await clock.advance(HEARTBEAT_INTERVAL_S)
    assert [ws for ws, _ in sent] == [owner_tab]

    tick.hold(app_turn, "chat-app")
    sent.clear()
    await clock.advance(HEARTBEAT_INTERVAL_S)
    assert [ws for ws, _ in sent] == [owner_tab, app]
    await tick.stop()


@pytest.mark.asyncio
async def test_close_all_ws_stops_the_tick():
    clock = _FakeClock()
    hub, _owner, sent = _hub([_FakeWs()], clock)
    hub.turn_heartbeat.hold(object(), "chat-1")
    assert hub.turn_heartbeat.running

    await hub.close_all_ws()

    assert not hub.turn_heartbeat.running
    await clock.advance(3 * HEARTBEAT_INTERVAL_S)
    assert sent == []


@pytest.mark.asyncio
async def test_run_chat_holds_the_tick_for_the_turn_and_sends_no_slot_heartbeat(tmp_path):
    state, client = _runner_state(tmp_path)
    slot = _slot()
    tick = _websocket_for(state).turn_heartbeat
    seen: list[list[str]] = []

    async def stream(*_args, **_kwargs):
        seen.append(sorted(tick._holders.values()))
        yield _complete()

    client.stream = stream
    client.stream_command = stream

    await _drive(state, slot, "hello")

    assert seen == [[slot.key]]
    assert tick._holders == {}
    heartbeat_calls = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "heartbeat"]
    assert heartbeat_calls == []
    await tick.stop()
