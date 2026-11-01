"""End to end through the socket handler: two watchers, then a legacy tab.

Real aiohttp server and ``/api/ws`` handler, real hub, exporter, bus, eager folder
and session logs on disk. A report on board A reaches only the tab watching A, and
board B's only the tab watching B. Once a tab without the capability connects, the
exporter goes back to its wildcard subscription and that tab gets every board.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from test_fold_watch_e2e import (  # noqa: F401 -- fixtures register by import
    _FENCE,
    _frames_until_fence,
    _isolated_home,
    _report,
    _slot_frames,
    e2e_state,
)

from kiro_crew.crew_log import session_tree_projection
from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.handlers.crew_log import CrewLogPublisher
from kiro_crew.dashboard.websocket_hub import FOLD_DELTA_CAPABILITY, FOLD_HELLO_FRAME
from kiro_crew.testing.wait import async_wait_until, finished


@pytest.mark.asyncio
async def test_two_watchers_get_their_own_boards_and_a_legacy_tab_gets_every_board(
    e2e_state, monkeypatch  # noqa: F811 -- the fixture imported above
):
    from kiro_crew.dashboard.ws import api_ws

    monkeypatch.setattr(
        session_tree_projection,
        "projection",
        lambda: SimpleNamespace(seeded_for_current_store=True),
    )
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
            caps = f"slot_patch,{FOLD_DELTA_CAPABILITY}"
            on_a = await client.ws_connect("/api/ws", params={"token": token, "caps": caps})
            on_b = await client.ws_connect("/api/ws", params={"token": token, "caps": caps})
            for tab, board in ((on_a, "board-a"), (on_b, "board-b")):
                assert (await finished(tab.receive_json(), "snapshot"))["type"] == "slots"
                while (await finished(tab.receive_json(), "hello"))["type"] != FOLD_HELLO_FRAME:
                    pass
                await tab.send_json({"type": "fold_watch", "slots": [board]})
            await async_wait_until(
                lambda: publisher.slot_subscription_cells()
                == {
                    (key, "work")
                    for key in ("board-a", "dashboard:board-a", "board-b", "dashboard:board-b")
                },
                describe=publisher.slot_subscription_cells,
            )
            await _report("s-h-a", "board-a", "it-a")
            await _report("s-h-b", "board-b", "it-b")
            e2e_state.push_refresh(_FENCE)
            assert _slot_frames(await _frames_until_fence(on_a)) == [("board-a", "work")]
            assert _slot_frames(await _frames_until_fence(on_b)) == [("board-b", "work")]

            old = await client.ws_connect("/api/ws", params={"token": token, "caps": "slot_patch"})
            assert (await finished(old.receive_json(), "snapshot"))["type"] == "slots"
            await async_wait_until(
                lambda: publisher.slot_subscription_cells() == {("*", "*")},
                describe=publisher.slot_subscription_cells,
            )
            await _report("s-h-c", "board-c", "it-c")
            e2e_state.push_refresh(_FENCE)
            assert ("board-c", "work") in _slot_frames(await _frames_until_fence(old))
            for tab in (on_a, on_b, old):
                await tab.close()
        finally:
            publisher.unsubscribe_bus()
