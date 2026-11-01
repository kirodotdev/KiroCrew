"""End to end: a tab that declares ``fold_delta.1`` against today's socket handler.

The real path: an aiohttp server running the unmodified ``/api/ws`` handler, a real
``DashboardState`` and hub, a real crew-log exporter subscribed to the real bus, real
session logs on disk under a temporary data home, and the real eager folder.

The socket handler does not set the capability flag and ignores ``fold_watch``, so
this is the NEW-TAB-ON-OLD-GATEWAY case of the migration: a tab sending the new caps
and a watch for one board still receives every board's frames, exactly what a tab
without the capability receives, and the exporter keeps its wildcard subscription.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from crew_log_fold_eager_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolated_home,
    _memo_keys,
    _slot_log,
    _work_entry,
)

from kiro_crew.crew_log import bus, eager
from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.handlers.crew_log import SLOT_FRAME, CrewLogPublisher
from kiro_crew.dashboard.websocket_hub import (
    FOLD_DELTA_CAPABILITY,
    FOLD_DELTA_WS_FLAG,
    FOLD_HELLO_FRAME,
)
from kiro_crew.testing.wait import async_wait_until, finished

_FENCE = "fold-watch-fence"


@pytest.fixture
def e2e_state(tmp_path, monkeypatch):
    bus.reset_for_tests()
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"fold-watch-test-signing-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    monkeypatch.setattr("kiro_crew.dashboard.ws.shutdown_event", stopped)
    monkeypatch.setattr(
        "kiro_crew.dashboard.ws.load_declared_events_for_connect",
        lambda _app: (True, frozenset()),
    )
    st = _make_state(tmp_path)
    monkeypatch.setattr(st, "status_snapshot", lambda **_kwargs: {})
    yield st
    stopped.set()
    bus.reset_for_tests()


async def _frames_until_fence(socket) -> list[dict]:
    frames = []
    while True:
        frame = await finished(socket.receive_json(), "the next frame arrives")
        if frame.get("type") == "refresh" and _FENCE in frame.get("data", {}).get("kinds", []):
            return frames
        frames.append(frame)


async def _report(unit: str, board: str, item: str) -> None:
    handle = _slot_log(unit, board)
    seq = _work_entry(handle, item, slot=board)
    eager.note_commit(unit, "work/recorded", seq, board=board)
    assert await asyncio.to_thread(eager.drain, 10.0)


def _slot_frames(frames: list[dict]) -> list[tuple[str, str]]:
    return sorted(
        (f["data"]["slot"], f["data"]["fold"]) for f in frames if f.get("type") == SLOT_FRAME
    )


@pytest.mark.asyncio
async def test_a_tab_declaring_fold_delta_on_todays_handler_still_gets_every_board(
    e2e_state, monkeypatch
):
    from kiro_crew.dashboard.ws import api_ws

    # An older gateway never flags the socket: the handler asks the state, and this
    # one answers as a state without the capability would.
    monkeypatch.setattr(e2e_state, "set_fold_delta_capable", lambda _ws, _capable: False)
    app = _make_app(e2e_state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)

    async with TestClient(TestServer(app)) as client:
        publisher = CrewLogPublisher(e2e_state)
        publisher.bind(asyncio.get_running_loop(), e2e_state)
        publisher.subscribe_bus()
        try:
            origin = str(client.make_url("/")).rstrip("/")
            app["allowed_origins"].add(origin)
            client.session.headers["Origin"] = origin
            token = token_auth.generate_token("local-app")
            new_tab = await client.ws_connect(
                "/api/ws", params={"token": token, "caps": f"slot_patch,{FOLD_DELTA_CAPABILITY}"}
            )
            old_tab = await client.ws_connect(
                "/api/ws", params={"token": token, "caps": "slot_patch"}
            )
            for tab in (new_tab, old_tab):
                first = await finished(tab.receive_json(), "the first frame arrives")
                assert first["type"] == "slots"
            # What the new bundle sends once it has the hello; sent here regardless,
            # since the point is that today's handler drops it. The handler reads one
            # socket's messages in order, so once the subscription after it has
            # landed, the watch has been read too.
            await new_tab.send_json({"type": "fold_watch", "slots": ["board-a"]})
            await new_tab.send_json({"type": "subscribe_subagents"})
            await async_wait_until(
                lambda: bool(e2e_state._ws_subagent_subscribers),
                describe=lambda: "the handler read the messages after fold_watch",
            )

            assert e2e_state._owner_ws_clients, "both tabs are owner sockets"
            assert not any(ws.get(FOLD_DELTA_WS_FLAG, False) for ws in e2e_state._owner_ws_clients)
            assert publisher.slot_subscription_cells() == {("*", "*")}
            assert not bus._slot_folds_on_demand

            await _report("s-e2e-a", "board-a", "it-a")
            await _report("s-e2e-b", "board-b", "it-b")
            # ``drain`` returns once both publishes ran, and each queued its frame on
            # this loop before the fence below is queued, so the fence comes last.
            e2e_state.push_refresh(_FENCE)

            new_frames = await _frames_until_fence(new_tab)
            old_frames = await _frames_until_fence(old_tab)
            assert ("board-a", "work") in _slot_frames(new_frames)
            assert ("board-b", "work") in _slot_frames(
                new_frames
            ), "an unwatched board is still sent"
            assert _slot_frames(new_frames) == _slot_frames(old_frames)
            assert not [f for f in new_frames if f.get("type") == FOLD_HELLO_FRAME]
            # Every woken slot fold was still folded: nothing is skipped on this gateway.
            assert ("board-b", "work") in _memo_keys()
            assert {f for s, f in _memo_keys() if s == "board-b"} > {"work"}
            await new_tab.close()
            await old_tab.close()
        finally:
            publisher.unsubscribe_bus()
