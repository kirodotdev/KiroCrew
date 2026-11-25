"""The live view of an app's slot while that app is turned off.

The rule: from the moment an app is turned off, every slot it owns has its stream
paused, every tab's seen mark cleared (the anonymous one and each ``TAB_ID``), its
takeover handed back, and every route that would keep or start a view (seen mark,
ack, take over, input, reopen, open in browser) refused, including a tab that
reconnects or mounts anew. Hand back and close still work. (A second view's own key
is step 1c's later work.)

Who may see or drive the browser of an app's slot: no one while the app is off,
from any surface, because the app that owns the slot is not running. Every
live-view route that starts or keeps something (visibility, ack, take over, input,
reopen, open in browser) refuses ``app_disabled`` and ends what still runs: a
takeover is handed back and the stream is told no one watches. Ending something
(hand back, close) always works. A person's own chat has no app and is untouched.
Turning the app off from the gateway ends its slots' views at once and tells every
dashboard (``app_state``).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from browser_live_helpers import FakeRelay
from test_browser_live_hub import FakeStream, Wire

from kiro_crew.apps import manager as apps_manager
from kiro_crew.apps import routes_enable
from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.dashboard.handlers import browser_live as mod
from kiro_crew.dashboard.routes import browser_live as route_slice
from kiro_crew.testing.clock import ManualClock

pytestmark = pytest.mark.asyncio

APP_SLOT = "bvtest-1"
OWN_CHAT = "chat-1"

GATED = (
    ("/api/browser/live/visibility", {"visible": True}),
    ("/api/browser/live/ack", {"seq": 1}),
    ("/api/browser/input/claim", {}),
    ("/api/browser/input/event", {"type": "key", "action": "down", "key": "a"}),
    ("/api/browser/live/reopen", {"turn_id": "t"}),
    ("/api/browser/live/open-in-browser", {}),
)


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _floor_monkeypatch.setattr(
        mod, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
    )
    lh.reset_live_hub()
    yield
    lh.reset_live_hub()


@pytest.fixture
def app_on(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Which apps are on; ``bvtest`` starts off."""
    on = {"bvtest": False}
    monkeypatch.setattr(apps_manager, "is_app_enabled", lambda name: on.get(name, False))
    return on


def _app() -> web.Application:
    @web.middleware
    async def owner(request: web.Request, handler):
        request["user"], request["app"] = "local-app", ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    wire = Wire()
    app["state"] = SimpleNamespace(
        owner_id="",
        _slots={OWN_CHAT: SimpleNamespace(_app=""), APP_SLOT: SimpleNamespace(_app="bvtest")},
        deliver_ws_owners=wire.publish,
        deliver_ws_owners_bytes=wire.send_frame,
    )
    route_slice.register(app)
    return app


async def _live(key: str) -> FakeStream:
    stream = await lh.get_live_hub().start_stream(
        key, FakeRelay(), "tgt-" + key, browser="edge", headless=False, stream_factory=FakeStream
    )
    assert isinstance(stream, FakeStream)
    return stream


async def _post(client: TestClient, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await client.post(path, json=body)
    return resp.status, await resp.json()


@pytest.mark.parametrize(("path", "extra"), GATED)
async def test_every_route_that_keeps_a_view_refuses_an_off_apps_slot(
    app_on: dict[str, bool], path: str, extra: dict[str, Any]
) -> None:
    await _live(APP_SLOT)
    async with TestClient(TestServer(_app())) as client:
        status, body = await _post(client, path, {"session_key": APP_SLOT, **extra})
    assert status == 403 and body["code"] == "app_disabled"


async def test_a_refusal_hands_back_the_takeover_and_stops_the_stream(
    app_on: dict[str, bool],
) -> None:
    stream = await _live(APP_SLOT)
    hub = lh.get_live_hub()
    app_on["bvtest"] = True
    async with TestClient(TestServer(_app())) as client:
        status, _ = await _post(client, "/api/browser/input/claim", {"session_key": APP_SLOT})
        assert status == 200 and hub.in_control(APP_SLOT)
        # Turned off elsewhere (the CLI, another gateway path): the next keep-alive ends it.
        app_on["bvtest"] = False
        status, body = await _post(
            client, "/api/browser/live/visibility", {"session_key": APP_SLOT, "visible": True}
        )
    assert status == 403 and body["code"] == "app_disabled"
    assert not hub.in_control(APP_SLOT)
    assert ("visible", False) in stream.calls


@pytest.mark.parametrize("path", ["/api/browser/input/release", "/api/browser/live/close"])
async def test_ending_a_view_still_works_while_the_app_is_off(
    app_on: dict[str, bool], path: str
) -> None:
    await _live(APP_SLOT)
    async with TestClient(TestServer(_app())) as client:
        status, body = await _post(client, path, {"session_key": APP_SLOT})
    assert body.get("code") != "app_disabled"
    assert status != 403


async def test_a_persons_own_chat_and_an_on_app_are_not_affected(app_on: dict[str, bool]) -> None:
    await _live(OWN_CHAT)
    await _live(APP_SLOT)
    app_on["bvtest"] = True
    async with TestClient(TestServer(_app())) as client:
        for key in (OWN_CHAT, APP_SLOT):
            status, body = await _post(client, "/api/browser/input/claim", {"session_key": key})
            assert status == 200 and body["in_control"] is True


async def test_turning_the_app_off_ends_its_views_and_tells_every_dashboard(
    app_on: dict[str, bool],
) -> None:
    stream, second = await _live(APP_SLOT), await _live("bvtest-2")
    own = await _live(OWN_CHAT)
    hub = lh.get_live_hub()
    assert await hub.claim(APP_SLOT) and await hub.claim("bvtest-2") and await hub.claim(OWN_CHAT)
    sent: list[tuple[str, dict[str, Any]]] = []
    app_slot = SimpleNamespace(_app="bvtest")
    state = SimpleNamespace(
        _slots={OWN_CHAT: SimpleNamespace(_app=""), APP_SLOT: app_slot, "bvtest-2": app_slot},
        broadcast_ws=lambda kind, data: sent.append((kind, data)),
    )
    request = SimpleNamespace(app={"state": state})
    await routes_enable._end_turned_off_app_views(request, "bvtest")
    # Every slot the app owns, not only the first.
    for key, live in ((APP_SLOT, stream), ("bvtest-2", second)):
        assert not hub.in_control(key) and ("visible", False) in live.calls
    # The person's own chat keeps its takeover and its stream.
    assert hub.in_control(OWN_CHAT) and ("visible", False) not in own.calls
    assert _said(sent) == [("app_state", "bvtest", False)]


async def test_turning_an_app_on_or_off_tells_every_dashboard(monkeypatch) -> None:
    """Both directions send ``app_state``: an app page reads its app once, so a tab
    that shows it learns of the change only from this frame."""
    # A clock that does not move: both frames read one tick, as on Windows (about
    # 15 ms a tick), and seq must still grow.
    ManualClock().install(monkeypatch, routes_enable)
    sent: list[tuple[str, dict[str, Any]]] = []
    state = SimpleNamespace(_slots={}, broadcast_ws=lambda kind, data: sent.append((kind, data)))
    request = SimpleNamespace(app={"state": state})
    routes_enable._announce_app_state(request, "bvtest", enabled=True)
    routes_enable._announce_app_state(request, "bvtest", enabled=False)
    assert _said(sent) == [("app_state", "bvtest", True), ("app_state", "bvtest", False)]
    # The order a tab compares, when frames reach it out of order: same process, newer seq.
    first, second = (data for _, data in sent)
    assert first["boot"] == second["boot"] and first["seq"] < second["seq"]
    # Drawn once per process, never the pid: a restarted gateway can get the same pid.
    assert first["boot"] == routes_enable._APP_STATE_BOOT != str(os.getpid())


def _said(sent: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, str, bool]]:
    return [(kind, data["app"], data["enabled"]) for kind, data in sent]


TAB_A, TAB_B = "tab-a1b2c3", "tab-d4e5f6"


async def _seen_by(client: TestClient, key: str, tab: str) -> int:
    resp = await client.post(
        "/api/browser/live/visibility", json={"session_key": key, "visible": True, "client": tab}
    )
    return resp.status


async def test_turning_off_clears_every_tabs_mark_so_the_stream_stops(
    app_on: dict[str, bool],
) -> None:
    stream = await _live(APP_SLOT)
    hub = lh.get_live_hub()
    app_on["bvtest"] = True
    async with TestClient(TestServer(_app())) as client:
        # Two dashboard tabs watch, each under its own client id, as real tabs do.
        assert await _seen_by(client, APP_SLOT, TAB_A) == 200
        assert await _seen_by(client, APP_SLOT, TAB_B) == 200
        assert hub.get(APP_SLOT).visible
        app_on["bvtest"] = False
        state = SimpleNamespace(
            _slots={APP_SLOT: SimpleNamespace(_app="bvtest")}, broadcast_ws=lambda *_: None
        )
        await routes_enable._end_turned_off_app_views(
            SimpleNamespace(app={"state": state}), "bvtest"
        )
        assert not hub.get(APP_SLOT).visible and hub.get(APP_SLOT).visible_tabs == set()
        assert stream.calls[-1] == ("visible", False)
        # A tab that reconnects, or a view mounted anew, cannot mark it seen again.
        assert await _seen_by(client, APP_SLOT, TAB_A) == 403
        assert await _seen_by(client, APP_SLOT, "tab-new123") == 403
    assert not hub.get(APP_SLOT).visible


async def test_a_refused_route_also_clears_every_tabs_mark(app_on: dict[str, bool]) -> None:
    await _live(APP_SLOT)
    hub = lh.get_live_hub()
    app_on["bvtest"] = True
    async with TestClient(TestServer(_app())) as client:
        assert await _seen_by(client, APP_SLOT, TAB_A) == 200
        # Turned off with no gateway action (the CLI): the next route ends it all.
        app_on["bvtest"] = False
        status, _ = await _post(
            client, "/api/browser/live/ack", {"session_key": APP_SLOT, "seq": 1}
        )
    assert status == 403
    assert hub.get(APP_SLOT).visible_tabs == set() and not hub.get(APP_SLOT).visible
