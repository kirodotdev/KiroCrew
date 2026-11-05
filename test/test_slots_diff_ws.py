"""``/api/ws?caps=slot_patch,slot_diff.1`` receives the slot list as row deltas.

End to end through ``api_ws``: a tab that declares ``slot_diff.1`` gets a numbered
full list on the first coalesced push and ``slots_diff`` frames after it, a tab that
declares only ``slot_patch`` keeps getting full lists, a ``slots_resync`` message is
answered with the held list to that tab alone, and an app token that declares the
capability is served as if it had not.

The coalescing window is patched to zero, so each push broadcasts at once and a
``refresh`` frame sent after it fences the frames it produced.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.dashboard.websocket_hub import SLOT_DIFF_CAPABILITY
from kiro_crew.testing.wait import finished

_SLOT = "slots-diff-e2e"
_FENCE = "slots-diff-fence"
_APP = "slots-diff-app"


@pytest.fixture
def e2e_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"slots-diff-test-signing-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    # Through the facade: every state part that binds the name sees the patch.
    monkeypatch.setattr("kiro_crew.dashboard.state._slots_broadcast_interval_for", lambda _n: 0.0)
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    monkeypatch.setattr("kiro_crew.dashboard.ws.shutdown_event", stopped)
    monkeypatch.setattr(
        "kiro_crew.dashboard.ws.load_declared_events_for_connect",
        lambda _app: (True, frozenset({"slots", "refresh"})),
    )
    st = _make_state(tmp_path)
    st._slots_broadcast_interval = 0.0
    st.get_or_create_slot(_SLOT, origin=SlotOrigin.USER)
    # Other rows, so one changed row is a small share of the full frame.
    for i in range(4):
        st.get_or_create_slot(f"{_SLOT}-other-{i}", origin=SlotOrigin.USER)
    monkeypatch.setattr(st, "status_snapshot", lambda **_kwargs: {})
    yield st
    stopped.set()


def _push_and_fence(st, step=None) -> None:
    """Make one change, push it (the window is zero, so it broadcasts now), then fence."""
    if step is not None:
        step()
    st.push_slots_update()
    st.push_refresh(_FENCE)


async def _slot_frames_until_fence(socket) -> list[dict]:
    frames = []
    while True:
        frame = await finished(socket.receive_json(), "the next frame arrives")
        if frame.get("type") == "refresh" and _FENCE in frame.get("data", {}).get("kinds", []):
            return frames
        if frame.get("type") in ("slots", "slots_diff"):
            frames.append(frame)


async def _next_slot_frame(socket) -> dict:
    while True:
        frame = await finished(socket.receive_json(), "the next slot frame arrives")
        if frame.get("type") in ("slots", "slots_diff"):
            return frame


def _app(e2e_state):
    from kiro_crew.dashboard.ws import api_ws

    app = _make_app(e2e_state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)
    return app


async def _open(client, token, caps):
    tab = await client.ws_connect("/api/ws", params={"token": token, "caps": caps})
    first = await finished(tab.receive_json(), "the connect snapshot arrives")
    assert first["type"] == "slots" and "slotsSeq" not in first
    return tab


@pytest.mark.asyncio
async def test_a_diff_tab_gets_deltas_and_a_patch_tab_full_lists(e2e_state):
    app = _app(e2e_state)
    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app["allowed_origins"].add(origin)
        client.session.headers["Origin"] = origin
        token = token_auth.generate_token("local-app")
        diff_tab = await _open(client, token, f"slot_patch,{SLOT_DIFF_CAPABILITY}")
        patch_tab = await _open(client, token, "slot_patch")

        slot = e2e_state._slots[_SLOT]
        _push_and_fence(e2e_state, lambda: slot.append("user", "first", "msg"))
        frames = await _slot_frames_until_fence(diff_tab)
        assert [f["type"] for f in frames] == ["slots"]
        full = frames[0]
        assert isinstance(full["slotsSeq"], int)
        assert [f["type"] for f in await _slot_frames_until_fence(patch_tab)] == ["slots"]

        _push_and_fence(e2e_state, lambda: slot.append("assistant", "second", "msg"))
        frames = await _slot_frames_until_fence(diff_tab)
        assert [f["type"] for f in frames] == ["slots_diff"]
        delta = frames[0]
        assert delta["data"]["base"] == full["slotsSeq"]
        assert [row["key"] for row in delta["data"]["rows"]] == [_SLOT]
        plain = await _slot_frames_until_fence(patch_tab)
        assert [f["type"] for f in plain] == ["slots"] and "slotsSeq" not in plain[0]

        # A resync is answered with the held list, to the diff tab alone.
        await diff_tab.send_json({"type": "slots_resync"})
        answer = await _next_slot_frame(diff_tab)
        assert answer["type"] == "slots" and answer["slotsSeq"] == delta["data"]["seq"]
        # A resync from a tab without the capability is ignored: the next frame
        # either tab gets is the next push's.
        await patch_tab.send_json({"type": "slots_resync"})
        await diff_tab.send_json({"type": "slots_resync"})
        assert (await _next_slot_frame(diff_tab))["type"] == "slots"
        _push_and_fence(e2e_state)
        assert [f["type"] for f in await _slot_frames_until_fence(patch_tab)] == ["slots"]
        assert [f["type"] for f in await _slot_frames_until_fence(diff_tab)] == ["slots_diff"]


@pytest.mark.asyncio
async def test_an_app_token_declaring_the_capability_gets_no_diff(e2e_state, monkeypatch):
    app = _app(e2e_state)
    pushes: list[bool] = []
    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app["allowed_origins"].add(origin)
        client.session.headers["Origin"] = origin
        user_tab = await _open(
            client, token_auth.generate_token("local-app"), f"slot_patch,{SLOT_DIFF_CAPABILITY}"
        )
        app_token = token_auth.generate_token(_APP, ttl_seconds=300, app=_APP)
        app_tab = await client.ws_connect(
            "/api/ws",
            params={"token": app_token, "caps": f"slot_patch,{SLOT_DIFF_CAPABILITY}"},
        )
        # The connect snapshot is sent after the socket joins the fan-out, so once
        # it arrives every later push and fence reaches this socket too.
        snapshot = await _next_slot_frame(app_tab)
        assert snapshot["type"] == "slots" and "slotsSeq" not in snapshot
        slot = e2e_state._slots[_SLOT]
        for i in range(2):
            _push_and_fence(e2e_state, lambda: slot.append("user", f"m{i}", "msg"))
            await _slot_frames_until_fence(user_tab)
        real_push = e2e_state.push_slots_update
        monkeypatch.setattr(
            e2e_state, "push_slots_update", lambda **kw: (pushes.append(True), real_push(**kw))
        )
        await app_tab.send_json({"type": "slots_resync"})
        e2e_state.push_refresh(_FENCE)
        await _slot_frames_until_fence(user_tab)
        # Every frame the app socket got after its snapshot, up to the last fence:
        # the two pushes and their fences, then the fence after its resync.
        app_frames = []
        fences = 0
        while fences < 3:
            msg = await finished(app_tab.receive(), "the app socket's next message arrives")
            assert msg.type == WSMsgType.TEXT, msg
            frame = json.loads(msg.data)
            app_frames.append(frame)
            if frame.get("type") == "refresh" and _FENCE in frame.get("data", {}).get("kinds", []):
                fences += 1
        assert pushes == []
        # The socket was served: it got plain slot lists, never a numbered one.
        assert len([f for f in app_frames if f.get("type") == "slots"]) >= 1
        assert not [f for f in app_frames if f.get("type") == "slots_diff" or "slotsSeq" in f]
