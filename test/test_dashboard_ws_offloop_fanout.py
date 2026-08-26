"""Regression: a WS fan-out reached off the event loop must not unregister clients.

``_send_ws_all`` wrapped serialization AND send-scheduling in one
``except Exception: dead.append(ws)``. Scheduling went through
``asyncio.ensure_future``, which raises off a worker thread — so a thread-origin
broadcast (``push_slots_update``'s leading edge runs inline on whatever thread
calls it) was read as "this peer is dead" and the socket was dropped from
``_ws_clients``/``_owner_ws_clients`` WITHOUT being closed. The client kept its
connection and its per-slot chat stream, and silently never received another
``slots``/``slot_title``/``refresh`` frame until it reconnected, so the sidebar
froze on a stale snapshot with no error surfaced anywhere.

Two guarantees are pinned here: an off-loop fan-out is DELIVERED (by handing the
send to the serving loop), and no delivery failure of any kind costs a client its
registration. The genuinely-gone peer must still be reaped, so that path is
pinned too — otherwise a fix for this bug leaks closed sockets.
"""
from __future__ import annotations

import asyncio
import gc
import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


class _FakeWS:
    """Healthy dashboard-user socket that records what it was sent."""

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []
        # These tests target the fan-out's failure handling, not the per-app
        # scope filter — flag as a dashboard user so _ws_client_allowed returns
        # True unconditionally.
        self._flags: dict = {"_is_dashboard_user": True}

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)


async def _drain() -> None:
    """Let a cross-thread hop and the send task it schedules both run."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_offloop_fanout_delivers_and_keeps_client(tmp_path) -> None:
    state = _make_state(tmp_path)
    ws = _FakeWS()
    state._ws_clients.append(ws)

    # An on-loop send first, which latches the serving loop.
    state._send_ws_all("ping", {}, "on-loop")
    await _drain()
    assert ws.sent == ["on-loop"]

    await asyncio.to_thread(state._send_ws_all, "ping", {}, "off-loop")
    await _drain()

    assert ws.sent == ["on-loop", "off-loop"], "off-loop fan-out was not delivered"
    assert ws in state._ws_clients, "a worker-thread fan-out unregistered a healthy client"


@pytest.mark.asyncio
async def test_offloop_owner_fanout_delivers_and_keeps_client(tmp_path) -> None:
    state = _make_state(tmp_path)
    ws = _FakeWS()
    state._owner_ws_clients.add(ws)

    state._send_ws_owners("on-loop")
    await _drain()
    assert ws.sent == ["on-loop"]

    await asyncio.to_thread(state._send_ws_owners, "off-loop")
    await _drain()

    assert ws.sent == ["on-loop", "off-loop"]
    assert ws in state._owner_ws_clients


@pytest.mark.asyncio
async def test_unknown_serving_loop_drops_frame_but_keeps_client(tmp_path) -> None:
    """No latched loop ⇒ the frame is unsendable, but the client stays registered."""
    state = _make_state(tmp_path)
    ws = _FakeWS()
    state._ws_clients.append(ws)
    state._serving_loop = None

    await asyncio.to_thread(state._send_ws_all, "ping", {}, "frame")
    await _drain()

    assert ws.sent == []
    assert ws in state._ws_clients


@pytest.mark.asyncio
async def test_serialization_failure_keeps_client(tmp_path) -> None:
    state = _make_state(tmp_path)
    ws = _FakeWS()
    state._ws_clients.append(ws)

    def _boom(*_args, **_kwargs):
        raise ValueError("unserializable payload")

    state._serialize_for_client = _boom  # type: ignore[method-assign]
    state._send_ws_all("slots", {}, "frame")
    await _drain()

    assert ws.sent == []
    assert ws in state._ws_clients, "a payload-shaping bug must not unregister the client"


@pytest.mark.asyncio
async def test_closed_client_is_still_removed(tmp_path) -> None:
    """The genuine dead-peer path must keep reaping, or closed sockets leak."""
    state = _make_state(tmp_path)
    ws = _FakeWS()
    ws.closed = True
    state._ws_clients.append(ws)
    state._owner_ws_clients.add(ws)

    state._send_ws_all("ping", {}, "frame")

    assert ws not in state._ws_clients
    assert ws not in state._owner_ws_clients


@pytest.mark.asyncio
async def test_peer_refusal_still_unregisters(tmp_path) -> None:
    """A SYNCHRONOUS send_str raise is a gone peer and must still be reaped.

    This is the half of the old behaviour worth keeping: the fix narrows eviction
    to peer failures, it does not stop evicting.
    """
    state = _make_state(tmp_path)
    ws = _FakeWS()
    ws.send_str = MagicMock(side_effect=ConnectionResetError)  # type: ignore[method-assign]
    state._ws_clients.append(ws)

    state._send_ws_all("ping", {}, "frame")

    assert ws not in state._ws_clients


@pytest.mark.asyncio
async def test_register_ws_latches_serving_loop_for_first_offloop_frame(tmp_path) -> None:
    """The FIRST frame after a connect must not be dropped.

    Latching the serving loop on the first SEND leaves a window: a freshly
    connected client whose first frame originates off-loop (a background notify
    reaching the fan-out from a worker thread) has no loop to run the send on, so
    that frame is lost until the client reconnects. Registration runs on the
    serving loop, so latching there closes the window.
    """
    state = _make_state(tmp_path)
    ws = _FakeWS()
    state._serving_loop = None
    state.register_ws(ws)  # on the serving loop, as api_ws does
    assert state._serving_loop is not None, "registration did not latch the serving loop"

    # No prior on-loop send: this is the connection's very first frame.
    await asyncio.to_thread(state._send_ws_all, "notification", {}, "first-frame")
    await _drain()

    assert ws.sent == ["first-frame"], "the first off-loop frame after a connect was dropped"


@pytest.mark.asyncio
async def test_offloop_subagent_fanout_delivers_and_keeps_client(tmp_path) -> None:
    """The subagent fan-out is a third _spawn_ws_send call site with the same duty."""
    state = _make_state(tmp_path)
    ws = _FakeWS()
    state.register_ws(ws)
    state.subscribe_subagents(ws)

    await asyncio.to_thread(
        state.broadcast_ws_subagent_subscribers, "subagent_chunk", {"id": "a1"}
    )
    await _drain()

    assert ws.sent, "off-loop subagent fan-out was not delivered"
    assert ws in state._ws_subagent_subscribers


@pytest.mark.asyncio
async def test_subagent_serialization_failure_keeps_subscriber(tmp_path) -> None:
    """_remove_ws strips _ws_clients too, so evicting here freezes the whole tab."""
    state = _make_state(tmp_path)
    ws = _FakeWS()
    state.register_ws(ws)
    state.subscribe_subagents(ws)

    def _boom(*_args, **_kwargs):
        raise ValueError("unserializable payload")

    state._serialize_for_client = _boom  # type: ignore[method-assign]
    state.broadcast_ws_subagent_subscribers("subagent_chunk", {"id": "a1"})
    await _drain()

    assert ws.sent == []
    assert ws in state._ws_subagent_subscribers
    assert ws in state._ws_clients


class _AbandonProbeWS:
    """Socket whose ``send_str`` carries a coroutine qualname unique to one test.

    ``gc.collect()`` sweeps the WHOLE process and ``catch_warnings(record=True)``
    records every warning that sweep raises, so a never-awaited assertion phrased
    against the generic "never awaited" text is satisfied by any other test's
    abandoned coroutine sharing the worker. Filtering on this class's own qualname
    keeps the check answerable only by the coroutine under test.
    """

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []
        self._flags: dict = {"_is_dashboard_user": True}

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)


@pytest.mark.asyncio
async def test_offloop_fanout_leaves_no_unawaited_coroutine(tmp_path) -> None:
    """A worker-thread fan-out must never abandon the send coroutine.

    An abandoned coroutine is this bug's production fingerprint — "coroutine
    'WebSocketResponse.send_str' was never awaited", attributed to the
    ``dead.append(ws)`` line that was evicting the client in the same breath.
    """
    state = _make_state(tmp_path)
    ws = _AbandonProbeWS()
    state._ws_clients.append(ws)
    state._serving_loop = None  # nothing to hand the send to

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await asyncio.to_thread(state._send_ws_all, "ping", {}, "frame")
        gc.collect()

    ours = [
        str(w.message)
        for w in caught
        if "never awaited" in str(w.message)
        and f"{_AbandonProbeWS.__name__}.send_str" in str(w.message)
    ]
    assert not ours, f"send coroutine was abandoned: {ours}"
    assert ws in state._ws_clients
