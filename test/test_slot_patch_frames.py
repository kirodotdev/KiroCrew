"""One-row ``slot_patch`` frames for sidebar metadata edits.

A pin, rename, folder move or close changes one slot. A tab that declares
``?caps=slot_patch`` on ``/api/ws`` receives a one-row patch for it instead
of the whole slot list, and every other consumer (an older bundle, an app
token, an SSE reader) receives the full list. These tests pin both halves, the
coalescer's audience bookkeeping, and the wire contract end to end.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.state import DashboardState, SlotOrigin
from kiro_crew.dashboard.websocket_hub import SLOT_PATCH_WS_FLAG


@pytest.fixture(autouse=True)
def sync_event_loop():
    """A loop for ``ensure_future`` in the fire-and-forget WS sends."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
    asyncio.set_event_loop(None)


class _WS:
    """Fake socket: records every frame it is handed, synchronously."""

    def __init__(self, *, patch_capable: bool, dashboard_user: bool = True) -> None:
        self.closed = False
        self.send_str = AsyncMock()
        self._flags = {"_is_dashboard_user": dashboard_user, SLOT_PATCH_WS_FLAG: patch_capable}

    def get(self, key, default=None):
        return self._flags.get(key, default)

    def frames(self) -> list[dict]:
        return [json.loads(call.args[0]) for call in self.send_str.call_args_list]

    def types(self) -> list[str]:
        return [frame["type"] for frame in self.frames()]


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _slot(state: DashboardState, key: str):
    return state.get_or_create_slot(key, origin=SlotOrigin.USER)


class TestSlotPatch:
    def test_capable_socket_gets_one_row_and_no_full_list(self, state: DashboardState) -> None:
        slot = _slot(state, "chat-a")
        _slot(state, "chat-b")
        capable = _WS(patch_capable=True)
        state.register_ws(capable)  # type: ignore[arg-type]
        state.serialize_slots = MagicMock(wraps=state.serialize_slots)  # type: ignore[method-assign]

        slot.pinned = True
        state.push_slot_patch("chat-a", ("pinned",))

        assert capable.frames() == [
            {"type": "slot_patch", "data": {"slots": [{"key": "chat-a", "pinned": True}]}}
        ]
        # With no legacy consumer connected the whole list is never serialized.
        state.serialize_slots.assert_not_called()

    def test_legacy_socket_still_gets_the_full_list(self, state: DashboardState) -> None:
        slot = _slot(state, "chat-a")
        capable = _WS(patch_capable=True)
        legacy = _WS(patch_capable=False)
        state.register_ws(capable)  # type: ignore[arg-type]
        state.register_ws(legacy)  # type: ignore[arg-type]

        slot.pinned = True
        state.push_slot_patch("chat-a", ("pinned",))

        assert capable.types() == ["slot_patch"]
        assert legacy.types() == ["slots"]
        [row] = [r for r in legacy.frames()[0]["data"] if r["key"] == "chat-a"]
        assert row["pinned"] is True

    def test_legacy_owner_socket_gets_the_owner_frame_and_capable_owner_does_not(
        self, state: DashboardState
    ) -> None:
        slot = _slot(state, "chat-a")
        capable_owner = _WS(patch_capable=True)
        legacy_owner = _WS(patch_capable=False)
        state.register_ws(capable_owner, owner=True)  # type: ignore[arg-type]
        state.register_ws(legacy_owner, owner=True)  # type: ignore[arg-type]

        slot.pinned = True
        state.push_slot_patch("chat-a", ("pinned",))

        assert capable_owner.types() == ["slot_patch"]
        assert legacy_owner.types() == ["slots"]

    def test_sse_reader_counts_as_legacy(self, state: DashboardState) -> None:
        _slot(state, "chat-a")
        queue: asyncio.Queue = asyncio.Queue()
        state._sse_queues.append(queue)

        state.push_slot_patch("chat-a", ("pinned",))

        note = queue.get_nowait()
        assert note["_type"] == "slots"

    def test_title_is_the_projected_value(self, state: DashboardState, monkeypatch) -> None:
        slot = _slot(state, "chat-a")
        capable = _WS(patch_capable=True)
        state.register_ws(capable)  # type: ignore[arg-type]
        slot.title = "Renamed"

        state.push_slot_patch("chat-a", ("title",))

        expected = state.serialize_slot(slot, dashboard_user=True)["title"]
        assert capable.frames()[0]["data"]["slots"] == [{"key": "chat-a", "title": expected}]

    def test_per_audience_field_is_rejected(self, state: DashboardState) -> None:
        _slot(state, "chat-a")

        with pytest.raises(ValueError, match="unsupported slot patch fields: source_links"):
            state.push_slot_patch("chat-a", ("source_links",))

    def test_unknown_slot_falls_back_to_a_full_push(self, state: DashboardState) -> None:
        capable = _WS(patch_capable=True)
        state.register_ws(capable)  # type: ignore[arg-type]

        state.push_slot_patch("gone", ("pinned",))

        assert capable.types() == ["slots"]

    def test_ordinary_push_still_reaches_capable_sockets(self, state: DashboardState) -> None:
        _slot(state, "chat-a")
        capable = _WS(patch_capable=True)
        state.register_ws(capable)  # type: ignore[arg-type]

        state.push_slots_update()

        assert capable.types() == ["slots"]


class TestAudienceBookkeeping:
    def test_legacy_only_when_nothing_else_is_owed(self, state: DashboardState) -> None:
        state._slots_push_legacy_owed = True
        assert state._take_slots_audience() is True
        # Consumed: the next broadcast is owed to everyone again.
        assert state._take_slots_audience() is False

    def test_an_ordinary_push_in_the_same_window_widens_the_broadcast(
        self, state: DashboardState
    ) -> None:
        state._slots_push_legacy_owed = True
        state._slots_push_all_owed = True
        assert state._take_slots_audience() is False

    def test_a_trailing_broadcast_after_a_mixed_window_reaches_capable_sockets(
        self, state: DashboardState, sync_event_loop
    ) -> None:
        slot = _slot(state, "chat-a")
        capable = _WS(patch_capable=True)
        legacy = _WS(patch_capable=False)
        state.register_ws(capable)  # type: ignore[arg-type]
        state.register_ws(legacy)  # type: ignore[arg-type]

        async def scenario() -> None:
            slot.pinned = True
            # Leading edge: legacy-only broadcast plus the patch.
            state.push_slot_patch("chat-a", ("pinned",))
            # Inside the coalescing window: an ordinary change arms the trailing
            # flush, which must reach the capable socket too.
            slot.title = "changed by a turn"
            state.push_slots_update()
            await asyncio.sleep(0.35)

        sync_event_loop.run_until_complete(scenario())

        # The capable socket got the patch at once and the trailing list for the
        # ordinary change; the legacy socket got the list, never the patch.
        assert capable.types()[0] == "slot_patch"
        assert capable.types()[-1] == "slots"
        assert "slot_patch" not in legacy.types()
        [row] = [r for r in capable.frames()[-1]["data"] if r["key"] == "chat-a"]
        assert row["pinned"] is True


class TestSlotRemoved:
    def test_removal_frame_restates_orphaned_parents(
        self, state: DashboardState, monkeypatch
    ) -> None:
        _slot(state, "worker")
        capable = _WS(patch_capable=True)
        state.register_ws(capable)  # type: ignore[arg-type]

        def fake_attach(rows, _aliases):
            for row in rows:
                row["parent"] = (
                    {"slot": "conductor", "key": None} if row["key"] == "worker" else None
                )

        monkeypatch.setattr("kiro_crew.dashboard.state._attach_slot_parents", fake_attach)

        state.push_slot_removed("conductor")

        assert capable.frames() == [
            {
                "type": "slot_patch",
                "data": {
                    "slots": [{"key": "worker", "parent": {"slot": "conductor", "key": None}}],
                    "removed": ["conductor"],
                },
            }
        ]

    def test_a_reregistered_key_is_not_removed(self, state: DashboardState) -> None:
        _slot(state, "chat-a")
        capable = _WS(patch_capable=True)
        state.register_ws(capable)  # type: ignore[arg-type]

        state.push_slot_removed("chat-a")

        assert capable.types() == ["slots"]

    def test_removal_still_runs_the_member_event_log_diff(
        self, state: DashboardState, monkeypatch
    ) -> None:
        emitted = MagicMock()
        monkeypatch.setattr(state, "_emit_member_slot_transitions", emitted)
        state.register_ws(_WS(patch_capable=True))  # type: ignore[arg-type]

        state.push_slot_removed("gone")

        emitted.assert_called_once_with()


# ── End to end through /api/ws and the real handlers ──


_SLOT = "slot-patch-e2e"
_FENCE = "slot-patch-fence"


@pytest.fixture
def e2e_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"slot-patch-test-signing-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    monkeypatch.setattr("kiro_crew.dashboard.ws.shutdown_event", stopped)
    st = _make_state(tmp_path)
    st.get_or_create_slot(_SLOT, origin=SlotOrigin.USER)
    monkeypatch.setattr(st, "status_snapshot", lambda **_kwargs: {})
    yield st
    stopped.set()


async def _settle_and_fence(st) -> None:
    """Wait out the slots coalescing window, then mark the end of this step.

    A full list owed to an old tab can be the trailing edge of the 200 ms
    window, so the fence goes out only after that edge has fired.
    """
    await asyncio.sleep(0.35)
    st.push_refresh(_FENCE)


async def _frames_until_fence(socket) -> list[dict]:
    frames = []
    while True:
        frame = await asyncio.wait_for(socket.receive_json(), timeout=5)
        if frame.get("type") == "refresh" and _FENCE in frame.get("data", {}).get("kinds", []):
            return [f for f in frames if f.get("type") != "dashboard"]
        frames.append(frame)


@pytest.mark.asyncio
async def test_pin_reaches_a_capable_tab_as_a_patch_and_an_old_tab_as_a_list(
    e2e_state, monkeypatch
):
    from kiro_crew.dashboard.chat import api_chat_slot_pin
    from kiro_crew.dashboard.ws import api_ws

    monkeypatch.setattr(
        "kiro_crew.dashboard.ws.load_declared_events_for_connect",
        lambda _app: (True, frozenset()),
    )

    app = _make_app(e2e_state)
    app.router.add_patch("/api/chat/slots/{slot}/pin", api_chat_slot_pin)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)

    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app["allowed_origins"].add(origin)
        client.session.headers["Origin"] = origin
        token = token_auth.generate_token("local-app")
        app_token = token_auth.generate_token("local-app", app="test-app")
        new_tab = await client.ws_connect("/api/ws", params={"token": token, "caps": "slot_patch"})
        old_tab = await client.ws_connect("/api/ws", params={"token": token})
        app_tab = await client.ws_connect(
            "/api/ws", params={"token": app_token, "caps": "slot_patch"}
        )
        for tab in (new_tab, old_tab, app_tab):
            first = await asyncio.wait_for(tab.receive_json(), timeout=5)
            assert first["type"] == "slots"

        response = await client.patch(
            f"/api/chat/slots/{_SLOT}/pin", params={"token": token}, json={"pinned": True}
        )
        assert response.status == 200
        assert await response.json() == {"ok": True, "pinned": True}
        app_frame = await asyncio.wait_for(app_tab.receive_json(), timeout=5)
        while app_frame["type"] not in ("slots", "slot_patch"):
            app_frame = await asyncio.wait_for(app_tab.receive_json(), timeout=5)
        assert app_frame["type"] == "slots"
        await _settle_and_fence(e2e_state)

        new_frames = await _frames_until_fence(new_tab)
        old_frames = await _frames_until_fence(old_tab)

        assert [f["type"] for f in new_frames if f["type"] in ("slots", "slot_patch")] == [
            "slot_patch"
        ]
        [patch] = [f for f in new_frames if f["type"] == "slot_patch"]
        assert patch["data"] == {"slots": [{"key": _SLOT, "pinned": True}]}

        slots_frames = [f for f in old_frames if f["type"] == "slots"]
        assert slots_frames, "an old tab must keep receiving the full list"
        assert not [f for f in old_frames if f["type"] == "slot_patch"]
        [row] = [r for r in slots_frames[-1]["data"] if r["key"] == _SLOT]
        assert row["pinned"] is True

        # Rename: the title rides the patch (and the long-standing slot_title).
        response = await client.patch(
            f"/api/chat/slots/{_SLOT}/title", params={"token": token}, json={"title": "Renamed"}
        )
        assert response.status == 200
        await _settle_and_fence(e2e_state)
        new_frames = await _frames_until_fence(new_tab)
        old_frames = await _frames_until_fence(old_tab)
        assert not [f for f in new_frames if f["type"] == "slots"]
        assert [f["data"] for f in new_frames if f["type"] == "slot_patch"] == [
            {"slots": [{"key": _SLOT, "title": "Renamed"}]}
        ]
        assert [f for f in old_frames if f["type"] == "slot_title"]
        assert [f for f in old_frames if f["type"] == "slots"]

        # Close: the capable tab learns the key left; the old tab gets lists.
        response = await client.delete(f"/api/chat/slots/{_SLOT}", params={"token": token})
        assert response.status == 200
        await _settle_and_fence(e2e_state)
        new_frames = await _frames_until_fence(new_tab)
        old_frames = await _frames_until_fence(old_tab)
        assert not [f for f in new_frames if f["type"] == "slots"]
        removals = [f["data"] for f in new_frames if f["type"] == "slot_patch"]
        assert removals and all(r.get("removed") == [_SLOT] for r in removals)
        old_lists = [f for f in old_frames if f["type"] == "slots"]
        assert old_lists and all(_SLOT not in {r["key"] for r in f["data"]} for f in old_lists[-1:])
        await new_tab.close()
        await old_tab.close()
        await app_tab.close()


@pytest.mark.asyncio
async def test_folder_move_uses_a_full_list_only_when_it_unhides_the_folder(e2e_state):
    from kiro_crew.dashboard.chat_folders import api_chat_slot_folder
    from kiro_crew.dashboard.ws import api_ws

    hidden_folder = "hidden-folder"
    visible_folder = "visible-folder"
    e2e_state._folders = [
        {
            "id": hidden_folder,
            "name": "Hidden",
            "parent_id": "",
            "order": 0,
            "hidden": True,
        },
        {
            "id": visible_folder,
            "name": "Visible",
            "parent_id": "",
            "order": 1,
            "hidden": False,
        },
    ]

    app = _make_app(e2e_state)
    app.router.add_patch("/api/chat/slots/{slot}/folder", api_chat_slot_folder)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)

    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app["allowed_origins"].add(origin)
        client.session.headers["Origin"] = origin
        token = token_auth.generate_token("local-app")
        tab = await client.ws_connect("/api/ws", params={"token": token, "caps": "slot_patch"})
        first = await asyncio.wait_for(tab.receive_json(), timeout=5)
        assert first["type"] == "slots"

        response = await client.patch(
            f"/api/chat/slots/{_SLOT}/folder",
            params={"token": token},
            json={"folder_id": hidden_folder},
        )
        assert response.status == 200
        assert await response.json() == {"ok": True, "folder_id": hidden_folder}
        await _settle_and_fence(e2e_state)
        hidden_frames = await _frames_until_fence(tab)
        assert [f["type"] for f in hidden_frames if f["type"] in ("slots", "slot_patch")] == [
            "slots"
        ]
        assert e2e_state._folders[0]["hidden"] is False

        response = await client.patch(
            f"/api/chat/slots/{_SLOT}/folder",
            params={"token": token},
            json={"folder_id": visible_folder},
        )
        assert response.status == 200
        assert await response.json() == {"ok": True, "folder_id": visible_folder}
        await _settle_and_fence(e2e_state)
        visible_frames = await _frames_until_fence(tab)
        assert not [f for f in visible_frames if f["type"] == "slots"]
        assert [f["data"] for f in visible_frames if f["type"] == "slot_patch"] == [
            {"slots": [{"key": _SLOT, "folder_id": visible_folder}]}
        ]
        await tab.close()
