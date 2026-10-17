"""Shared constants, the store clock and the in-process route world for the change-card suites.

``world`` is a fixture: each suite binds it by name (``world = change_cards_helpers.world``) so
pytest registers it for that module.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from guide_route_helpers import in_dashboard_turn

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.dashboard.state import _ChatSlot

REPO = Path(__file__).resolve().parents[1]
SECRET_VALUE = "sk-live-DO-NOT-STORE-0123456789"
SLOT = "chat-1"
SK = f"dashboard:{SLOT}"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _propose(store: CardStore, kind="setting.change", params=None, before=None) -> dict:
    params = params or {"path": "chat.verbosity", "value": "ultra-brief"}
    before = before if before is not None else {"value": "standard"}
    preview = catalog.build_preview(kind, params, before, {})
    return store.propose(
        slot_key=SLOT,
        session_key=SK,
        kind=kind,
        params=params,
        reason="you asked for shorter answers",
        preview=preview,
        before=before,
        context={},
    )


def _reload(path: Path) -> CardStore:
    again = CardStore(path, clock=Clock())
    asyncio.run(again.warm())
    return again


_FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


class FakeCrons:
    async def get_job_async(self, _job_id):
        return None


class FakeState:
    owner_id = ""

    def __init__(self, store: CardStore) -> None:
        self._slots: dict[str, _ChatSlot] = {SLOT: in_dashboard_turn(_ChatSlot(SLOT))}
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self._change_card_store = store
        self.crons = FakeCrons()
        self.ws_frames: list[tuple[str, dict[str, Any]]] = []

    def get_slot(self, name: str):
        return self._slots.get(name)

    async def deliver_ws_owners(self, kind: str, payload: dict[str, Any]) -> int:
        self.frames.append((kind, payload))
        return 1

    def broadcast_ws(self, kind: str, payload: dict[str, Any]) -> None:
        self.ws_frames.append((kind, json.loads(json.dumps(payload))))


@web.middleware
async def _fake_auth(request: web.Request, handler):
    who = request.headers.get("X-Test-Auth", "")
    if who == "internal":
        # The MCP shim's own call: the transport attests the session it names.
        request["internal_auth"] = True
        request["peer_verified"] = True
        request["app"] = ""
        request["user"] = "local-app"
    elif who == "owner":
        request["user"] = "local-app"
        request["app"] = ""
    elif who == "app":
        request["user"] = "local-app"
        request["app"] = "some-app"
    return await handler(request)


class World:
    """The in-memory state the fake settings routes write and the readers read."""

    def __init__(self) -> None:
        self.config = {"chat.verbosity": "standard"}
        self.secrets: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.memory: list[str] = []
        #: Awaited by the fake config route AFTER it has written (a route that
        #: raises, is cancelled or refuses once its write already happened).
        self.after_write: Any = None


@pytest.fixture
def world(monkeypatch):
    w = World()

    async def read_state(kind, params, evidence, *, state, app):
        if kind == "setting.change":
            return {"value": w.config.get(params["path"])}
        if kind == "secret.save":
            return {"exists": params["name"] in w.secrets}
        raise AssertionError(kind)

    async def read_context(kind, params, before, *, state, app):
        return {}

    monkeypatch.setattr(cards, "read_state", read_state)
    monkeypatch.setattr(cards, "read_context", read_context)
    monkeypatch.setattr(
        routes, "_config_before", lambda route, body: w.config.get((body or {}).get("path"))
    )

    class _Null:
        def log_api_access(self, **_kw):
            return None

    monkeypatch.setattr(routes, "sel", lambda: _Null())

    class _Mem:
        def append_history(self, entry: str) -> None:
            w.memory.append(entry)

    from kiro_crew import context

    monkeypatch.setattr(
        context.ContextBuilder, "get_memory_for", staticmethod(lambda *a, **k: _Mem())
    )
    return w


def _app(world: World, store: CardStore) -> web.Application:
    async def patch_config(request):
        body = await request.json()
        world.calls.append(("PATCH", "/api/config/kirocrew"))
        world.config[body["path"]] = body["value"]
        if world.after_write is not None:
            await world.after_write(request)
        return web.json_response({"ok": True})

    async def save_secret(request):
        body = await request.json()
        world.calls.append(("POST", "/api/secrets"))
        world.secrets[body["name"]] = body["value"]
        return web.json_response({"ok": True, "name": body["name"]})

    async def delete_secret(request):
        world.calls.append(("DELETE", "/api/secrets"))
        world.secrets.pop(request.match_info["name"], None)
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[_fake_auth, routes.change_card_middleware])
    app["state"] = FakeState(store)
    routes.register_change_card_routes(app)
    app.router.add_patch("/api/config/kirocrew", patch_config)
    app.router.add_post("/api/secrets", save_secret)
    app.router.add_delete("/api/secrets/{name}", delete_secret)
    return app


def _run(world: World, store: CardStore, fn):
    async def main():
        client = TestClient(TestServer(_app(world, store)))
        await client.start_server()
        try:
            out = await fn(client)
            if routes._BACKGROUND:
                await asyncio.gather(*list(routes._BACKGROUND))
            return out
        finally:
            await client.close()

    return asyncio.run(main())


AGENT = {"X-Test-Auth": "internal", "X-Session-Key": SK}
OWNER = {"X-Test-Auth": "owner"}


def _card_headers(card, op="apply", step=0, auth="owner"):
    return {
        "X-Test-Auth": auth,
        "X-Card-Id": card["id"],
        "X-Card-Revision": str(card["revision"]),
        "X-Card-Op": op,
        "X-Card-Step": str(step),
    }


async def _propose_setting(c, value="ultra-brief"):
    r = await c.post(
        "/api/cards/agent/propose",
        json={"kind": "setting.change", "params": {"path": "chat.verbosity", "value": value}},
        headers=AGENT,
    )
    assert r.status == 200, await r.text()
    return await r.json()


_CAP_PARAMS = {
    "member": "mate",
    "draft": {
        "operations": [{"section": "allowedTools", "id": "@a/b", "action": "set", "value": True}]
    },
}
_CAP_BEFORE = {
    "revision": "r0",
    "rows": [{"section": "allowedTools", "id": "@a/b", "state": "inherited", "value": None}],
}
_CAP_IMPACT = [
    {"section": "allowedTools", "id": "@a/b", "change": "added", "approval_expanded": True}
]


def _propose_capability(store: CardStore) -> dict[str, Any]:
    params = catalog.validate_params("crewmate.capabilities", _CAP_PARAMS)
    preview = catalog.build_preview(
        "crewmate.capabilities", params, _CAP_BEFORE, {"impact": _CAP_IMPACT}
    )
    return store.propose(
        slot_key=SLOT,
        session_key=SK,
        kind="crewmate.capabilities",
        params=params,
        reason="widen tools",
        preview=preview,
        before=_CAP_BEFORE,
        context={"impact": _CAP_IMPACT},
    )
