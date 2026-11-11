"""The host-side outbound room bridge, against a LOCAL FAKE ROOM.

The fake room is an in-process websocket server that exists only in this file. Default
room contract assumed here (follow the real one if TEST-MATRIX / COLLAB-RUST post it):

* the host connects out and sends ``{"type": "join", "artifactId": ...}``;
* the room answers ``{"type": "joined", "userId": ..., "displayName": ...}``, the
  identity it verified;
* the room announces ``{"type": "session.turn", "artifactId", "seq", "author", "authorName"}``;
* revoke is a close with code 4403.

Trust boundary: no frame field ever names the principal. Only the join answer does.
"""

from __future__ import annotations

import asyncio
import inspect
import socket
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from kiro_crew.config.sections_shared_crew import shared_crew_config_from
from kiro_crew.shared_crew import room_bridge, typing_lease
from kiro_crew.shared_crew.collab import FRAME_ACTIVITY, FRAME_PRESENCE
from kiro_crew.shared_crew.contracts import Provenance
from kiro_crew.shared_crew.presence import PresenceRegistry
from kiro_crew.shared_crew.preview import PREVIEW_ENV
from kiro_crew.shared_crew.room_bridge import (
    CLOSE_REVOKED,
    RoomBridge,
    RoomJoinError,
    maybe_start,
    room_enabled,
)

ART = "art_room1"


class FakeRoom:
    """An in-process room: scripted join answer, records every frame the host sends."""

    def __init__(self, user_id: str = "usr_bob", display: str = "Bob") -> None:
        self.user_id = user_id
        self.display = display
        self.received: list[dict[str, Any]] = []
        self.sockets: list[web.WebSocketResponse] = []
        self.refuse_join = False
        self.server: TestServer | None = None

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            frame = msg.json()
            self.received.append(frame)
            if frame.get("type") == "join":
                if self.refuse_join:
                    await ws.close(code=CLOSE_REVOKED)
                    break
                await ws.send_json({"type": "joined", "userId": self.user_id, "displayName": self.display})
        return ws

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/room", self.handler)
        self.server = TestServer(app)
        await self.server.start_server()
        return f"ws://127.0.0.1:{self.server.port}/room"

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.close()

    async def push(self, frame: dict[str, Any]) -> None:
        await self.sockets[-1].send_json(frame)


class _State:
    """A gateway stand-in with one open session."""

    def __init__(self) -> None:
        self._slots = {"s1": object()}
        self._shared_crew_presence = PresenceRegistry()
        self.frames: list[tuple[str, dict]] = []

    def broadcast_ws(self, msg_type: str, data: dict) -> None:
        self.frames.append((msg_type, data))


async def _until(pred, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def room():
    r = FakeRoom()
    r.url = await r.start()
    yield r
    await r.stop()


@pytest_asyncio.fixture
async def started(room):
    state = _State()
    bridge = RoomBridge(state, room.url, ART)
    await bridge.start()
    yield state, bridge, room
    await bridge.close()


@pytest.mark.asyncio
async def test_join_sends_the_artifact_and_takes_the_identity_the_room_verified(started):
    _state, bridge, room = started
    assert room.received[0] == {"type": "join", "artifactId": ART}
    assert bridge.identity.user_id == "usr_bob"
    assert bridge.identity.display_name == "Bob"
    assert bridge.principal.provenance is Provenance.KIRO_SPACE_ROOM


@pytest.mark.asyncio
async def test_a_join_the_room_refuses_raises_and_leaves_nothing_connected(room):
    room.refuse_join = True
    bridge = RoomBridge(_State(), room.url, ART)
    with pytest.raises(RoomJoinError):
        await bridge.start()
    assert bridge.identity is None


@pytest.mark.asyncio
async def test_a_turn_announcement_reaches_the_host_unchanged(started):
    state, _bridge, room = started
    frame = {"type": "session.turn", "artifactId": ART, "seq": 7, "author": "usr_eve", "authorName": "Eve"}
    await room.push(frame)
    await _until(lambda: state.frames)
    assert state.frames[-1] == ("session.turn", frame)


@pytest.mark.asyncio
async def test_a_host_frame_reaches_the_room_unchanged(started):
    _state, bridge, room = started
    frame = {"type": FRAME_PRESENCE, "session_key": "s1", "entries": [], "schema_version": 1, "x_future": 3}
    await bridge.send(frame)
    await _until(lambda: frame in room.received)


@pytest.mark.asyncio
async def test_a_room_activity_frame_registers_a_presence_connection_for_the_verified_user(started):
    state, bridge, room = started
    await room.push({"type": FRAME_ACTIVITY, "session_key": "s1", "activity": "viewing"})
    await _until(lambda: state._shared_crew_presence.entries("s1"))
    (entry,) = state._shared_crew_presence.entries("s1")
    assert entry.principal.subject == "usr_bob"
    assert entry.principal.provenance is Provenance.KIRO_SPACE_ROOM
    assert bridge.conn_id.startswith("room:")


@pytest.mark.asyncio
async def test_a_frame_claiming_another_author_cannot_change_the_principal(started):
    """TRUST BOUNDARY: author, principal and user fields in a frame are data, never identity."""
    state, _bridge, room = started
    await room.push(
        {
            "type": FRAME_ACTIVITY,
            "session_key": "s1",
            "activity": "typing",
            "author": "usr_mallory",
            "authorName": "Mallory",
            "userId": "usr_mallory",
            "principal": {"id": "x", "subject": "usr_mallory", "kind": "human"},
            "provenance": "local_operator",
        }
    )
    await _until(lambda: state._shared_crew_presence.entries("s1"))
    (entry,) = state._shared_crew_presence.entries("s1")
    assert entry.principal.subject == "usr_bob"
    assert entry.principal.display.name == "Bob"
    assert entry.principal.provenance is Provenance.KIRO_SPACE_ROOM
    holder = typing_lease.typing_lease_for(state).holder("s1")
    assert holder is not None and holder.subject == "usr_bob"


@pytest.mark.asyncio
async def test_a_second_join_frame_cannot_swap_the_identity(started):
    state, bridge, room = started
    await room.push({"type": "joined", "userId": "usr_mallory", "displayName": "Mallory"})
    await room.push({"type": FRAME_ACTIVITY, "session_key": "s1", "activity": "viewing"})
    await _until(lambda: state._shared_crew_presence.entries("s1"))
    assert bridge.identity.user_id == "usr_bob"
    assert state._shared_crew_presence.entries("s1")[0].principal.subject == "usr_bob"


@pytest.mark.asyncio
async def test_revoke_close_4403_drops_the_connection_and_does_not_reconnect(started):
    state, bridge, room = started
    await room.push({"type": FRAME_ACTIVITY, "session_key": "s1", "activity": "viewing"})
    await _until(lambda: state._shared_crew_presence.entries("s1"))
    await room.sockets[-1].close(code=CLOSE_REVOKED)
    await _until(lambda: bridge.revoked)
    assert state._shared_crew_presence.entries("s1") == []
    await asyncio.sleep(0.2)
    assert len([f for f in room.received if f.get("type") == "join"]) == 1


@pytest.mark.asyncio
async def test_the_bridge_never_listens_or_binds(room, monkeypatch):
    calls: list[str] = []
    real_bind, real_listen = socket.socket.bind, socket.socket.listen

    def bind(self, *a, **k):
        calls.append("bind")
        return real_bind(self, *a, **k)

    def listen(self, *a, **k):
        calls.append("listen")
        return real_listen(self, *a, **k)

    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "listen", listen)
    bridge = RoomBridge(_State(), room.url, ART)
    await bridge.start()
    await bridge.send({"type": FRAME_PRESENCE, "session_key": "s1", "entries": []})
    await bridge.close()
    assert calls == []


def test_the_module_source_has_no_listening_server():
    src = inspect.getsource(room_bridge)
    for forbidden in ("TCPSite", "UnixSite", "create_server", "start_server", ".listen(", ".bind(", "web.run_app"):
        assert forbidden not in src


@pytest.mark.asyncio
async def test_a_plain_ws_url_to_a_non_loopback_host_is_refused():
    with pytest.raises(RoomJoinError):
        await RoomBridge(_State(), "ws://room.example.com/room", ART).start()


def test_the_room_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv(PREVIEW_ENV, raising=False)
    assert shared_crew_config_from({}).room is False
    assert room_enabled(shared_crew_config_from({"room": True})) is False


def test_the_room_needs_both_the_feature_preview_and_the_room_switch(monkeypatch):
    monkeypatch.setenv(PREVIEW_ENV, "1")
    assert room_enabled(shared_crew_config_from({})) is False
    assert room_enabled(shared_crew_config_from({"room": "yes"})) is False
    assert room_enabled(shared_crew_config_from({"room": True})) is True


@pytest.mark.asyncio
async def test_maybe_start_does_nothing_while_gated_off(room, monkeypatch):
    monkeypatch.delenv(PREVIEW_ENV, raising=False)
    assert await maybe_start(_State(), shared_crew_config_from({"room": True}), room.url, ART) is None
    assert room.received == []
    monkeypatch.setenv(PREVIEW_ENV, "1")
    assert await maybe_start(_State(), shared_crew_config_from({}), room.url, ART) is None
    assert room.received == []


@pytest.mark.asyncio
async def test_maybe_start_connects_when_both_switches_are_on(room, monkeypatch):
    monkeypatch.setenv(PREVIEW_ENV, "1")
    bridge = await maybe_start(_State(), shared_crew_config_from({"room": True}), room.url, ART)
    assert bridge is not None
    try:
        assert bridge.identity.user_id == "usr_bob"
    finally:
        await bridge.close()
