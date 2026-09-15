"""Correlated prompt echoes use the authenticated, slot-scoped WS boundary."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import revocation_gen, token_auth, ws_event_scope
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.state import SlotOrigin

_SLOT = "echo-scope-slot"
_APP = "echo-scope-observer"
_FENCE = "send-echo-test-finished"
_PROMPT = "Keep this credential private: ghp_" + "x" * 36


@pytest.fixture
def echo_state(tmp_path, monkeypatch):
    # Real authentication with isolated signing/nonce state and fixture manifests.
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"echo-scope-test-signing-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    for name in ("_declared_cache", "_exposeto_cache", "_sel_last_audit"):
        monkeypatch.setattr(ws_event_scope, name, {})
    for name in ("_declared_refreshing", "_exposeto_refreshing"):
        monkeypatch.setattr(ws_event_scope, name, set())
    monkeypatch.setattr(ws_event_scope, "is_app_enabled", lambda _name: True)
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    monkeypatch.setattr("kiro_crew.dashboard.ws.shutdown_event", stopped)
    state = _make_state(tmp_path)
    state.get_or_create_slot(_SLOT, origin=SlotOrigin.USER)
    # Periodic status is unrelated to prompt delivery or its permission check.
    monkeypatch.setattr(state, "status_snapshot", lambda **_kwargs: {})

    async def reply(st, slot, message, *, _directive_user_origin):
        slot.append("assistant", "reply")
        slot.append("done", "", "done", broadcast=False)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", reply)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
    yield state
    stopped.set()


async def _ws_until_fence(socket):
    frames = []
    while True:
        frame = await socket.receive_json()
        if frame.get("type") == "refresh" and _FENCE in frame.get("data", {}).get("kinds", []):
            return frames
        frames.append(frame)


async def _sse_until_fence(response):
    frames = []
    while True:
        block = (await response.content.readuntil(b"\n\n")).decode()
        if not block:
            raise EOFError("SSE closed before the delivery fence")
        if block.startswith("event: refresh") and _FENCE in block:
            return frames
        frames.append(block)


@pytest.mark.asyncio
@pytest.mark.parametrize("can_read_user_slots", [False, True])
async def test_user_echo_reaches_only_authorized_ws_clients(
    echo_state, monkeypatch, can_read_user_slots
):
    from kiro_crew.dashboard.ws import api_ws

    manifest = SimpleNamespace(
        permissions=SimpleNamespace(
            api=["/api/stream"], events=["slots:user"] if can_read_user_slots else []
        )
    )
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda _name: manifest)
    monkeypatch.setattr(ws_event_scope, "get_app_manifest", lambda _name: manifest)
    app = _make_app(echo_state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)
    app.router.add_get("/api/stream", updates.api_stream)

    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app["allowed_origins"].add(origin)
        client.session.headers["Origin"] = origin
        owner = await client.ws_connect(
            "/api/ws", params={"token": token_auth.generate_token("local-app")}
        )
        observer = await client.ws_connect(
            "/api/ws", params={"token": token_auth.generate_token("local-app", app=_APP)}
        )
        stream = await client.get(
            "/api/stream", params={"token": token_auth.generate_token("local-app", app=_APP)}
        )
        assert stream.status == 200
        # The first status frame proves registration before the send; an open
        # response alone only proves prepare() ran, which precedes register_sse().
        initial = await asyncio.wait_for(stream.content.readuntil(b"\n\n"), timeout=5)
        assert initial.startswith(b"event: dashboard")
        response = await client.post(
            "/api/chat?ws=1",
            params={"token": token_auth.generate_token("local-app")},
            json={"slot": _SLOT, "message": _PROMPT, "meta": {"sendId": "s-scope"}},
        )
        assert response.status == 200
        receipt = await response.json()
        await echo_state.get_slot(_SLOT).task
        # An allowed event on both transports is a FIFO fence. Negative checks
        # inspect everything before it, rather than hoping an echo arrives in N ms.
        echo_state._broadcast({"_type": "refresh", "kinds": _FENCE})
        owner_frames, app_frames, sse_frames = await asyncio.wait_for(
            asyncio.gather(
                _ws_until_fence(owner), _ws_until_fence(observer), _sse_until_fence(stream)
            ),
            timeout=5,
        )
        stream.close()

    def user_rows(frames):
        return [
            frame["data"]
            for frame in frames
            if frame.get("type") == "chat_message" and frame["data"].get("role") == "user"
        ]

    owner_rows = user_rows(owner_frames)
    assert len(owner_rows) == 1
    assert owner_rows[0]["content"] == _PROMPT
    assert owner_rows[0]["meta"]["mid"] == receipt["mid"]
    assert owner_rows[0]["meta"]["sendId"] == "s-scope"
    assert [
        frame["data"]["role"]
        for frame in owner_frames
        if frame.get("type") == "chat_message" and frame["data"].get("slot") == _SLOT
    ] == ["user", "assistant"]
    assert user_rows(app_frames) == (owner_rows if can_read_user_slots else [])
    stream_rows = [
        json.loads(frame.split("data: ", 1)[1])
        for frame in sse_frames
        if frame.startswith("event: chat_message")
    ]
    assert all(row["role"] != "user" for row in stream_rows)
    assert _PROMPT not in "".join(sse_frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("relay", [False, True])
async def test_in_band_send_keeps_its_existing_stream_contract(echo_state, monkeypatch, relay):
    published = []
    original_broadcast = echo_state.broadcast_ws

    def record(kind, data):
        published.append((kind, data))
        original_broadcast(kind, data)

    monkeypatch.setattr(echo_state, "broadcast_ws", record)
    app = _make_app(echo_state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat?relay=1" if relay else "/api/chat",
            params={"token": token_auth.generate_token("local-app")},
            json={"slot": _SLOT, "message": "in-band message", "meta": {"sendId": "s-in-band"}},
        )
        assert response.status == 200
        body = await asyncio.wait_for(response.text(), timeout=5)
        rows = [
            json.loads(line[6:])
            for line in body.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        assert [row["type"] for row in rows] == ["assistant"]
        assert rows[0]["content"] == "reply"
        if relay:
            assert rows[0]["meta"]["mid"]
        else:
            assert "meta" not in rows[0]
        users = [row for row in echo_state.get_slot(_SLOT).messages if row["role"] == "user"]
        assert len(users) == 1
        assert users[0]["content"] == "in-band message"
    assert not any(
        kind == "chat_message" and data.get("role") == "user" for kind, data in published
    )
