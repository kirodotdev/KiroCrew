"""Owner gate on pptx-maker's mutating routes.

Every PUT, POST and DELETE route changes the owner's deck root or style and
template library, or starts a clone and build. A dashboard caller that is not
the owner, and a request with no app claim, must get the shared 403
``owner_only`` with nothing written and nothing started. The owner and an app
token keep working.

Requests run through the real ``register_routes``. A middleware stamps the
identity the dashboard auth middleware would. Only the app-enabled probe, the
SEL writer, the engine's user-dir lookup and the two provision workers are
replaced, so no network and no engine venv are touched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import sel as sel_mod
from kiro_crew.apps.builtins.pptx_maker.backend import engine, routes

_BASE = "/api/apps/pptx-maker"
_OWNER = "owner-user"
_NON_OWNER = "other-dashboard-user"
_NO_APP = "<absent>"

#: (method, path, body kwargs) for all ten mutating routes.
_MUTATING = [
    ("PUT", "/config", {"json": {"deckRoot": "/tmp"}}),
    ("POST", "/styles/import?name=evil", {"data": b"<html><body>x</body></html>"}),
    ("POST", "/styles/rename", {"json": {"name": "owner-brand", "to": "evil"}}),
    ("POST", "/styles/pin", {"json": {"name": "owner-brand", "pinned": True}}),
    ("DELETE", "/styles?name=owner-brand", {}),
    ("POST", "/templates/import?name=evil", {"data": b"PK\x03\x04junk"}),
    ("POST", "/templates/rename", {"json": {"name": "owner-deck", "to": "evil"}}),
    ("DELETE", "/templates?name=owner-deck", {}),
    ("POST", "/engine/provision", {}),
    ("POST", "/assets/provision?force=true", {}),
]


@web.middleware
async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
    request["user"] = request.headers.get("X-Test-User", _OWNER)
    app_claim = request.headers.get("X-Test-App", "")
    if app_claim != _NO_APP:
        request["app"] = app_claim
    return await handler(request)


def _as(user: str, app: str = "") -> dict[str, str]:
    return {"X-Test-User": user, "X-Test-App": app}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    xdg = tmp_path / "xdg"
    user_cfg = xdg / "sdpm"
    (user_cfg / "styles").mkdir(parents=True)
    (user_cfg / "templates").mkdir()
    (user_cfg / "config.json").write_text(json.dumps({"output_dir": str(tmp_path / "decks")}))
    (user_cfg / "styles" / "owner-brand.html").write_text("<html><body>brand</body></html>")
    (user_cfg / "templates" / "owner-deck.pptx").write_bytes(b"PK\x03\x04owner")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("KIROCREW_PPTX_DECK_ROOT", raising=False)
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)
    monkeypatch.setattr(routes, "_audit", lambda *a, **k: None)
    denials: list[str] = []
    monkeypatch.setattr(
        sel_mod,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **k: denials.append(k["operation"])),
    )
    monkeypatch.setattr(engine, "user_config_dir", lambda: user_cfg)
    started: list[str] = []
    monkeypatch.setattr(routes, "_run_provision", lambda: started.append("engine"))
    monkeypatch.setattr(routes, "_provision_assets", lambda force: started.append("assets"))
    monkeypatch.setattr(routes._engine_state, "state", "idle", raising=False)
    monkeypatch.setattr(routes._assets_state, "state", "idle", raising=False)
    return SimpleNamespace(tmp=tmp_path, user_cfg=user_cfg, started=started, denials=denials)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


async def _client() -> TestClient:
    app = web.Application(middlewares=[_identity])
    app["state"] = SimpleNamespace(owner_id=_OWNER)
    routes.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.parametrize(
    "who",
    [
        pytest.param(_as(_NON_OWNER), id="non-owner"),
        pytest.param(_as(_OWNER, _NO_APP), id="no-app-claim"),
    ],
)
@pytest.mark.parametrize("method,path,kwargs", _MUTATING, ids=[f"{m} {p}" for m, p, _ in _MUTATING])
@pytest.mark.asyncio
async def test_refused_caller_gets_owner_only_and_nothing_changes(
    env, who: dict[str, str], method: str, path: str, kwargs: dict
) -> None:
    before = _snapshot(env.user_cfg)
    client = await _client()
    try:
        resp = await client.request(method, f"{_BASE}{path}", headers=who, **kwargs)
        status = resp.status
        body = await resp.json()
        await asyncio.sleep(0.05)
    finally:
        await client.close()
    assert (status, body.get("code")) == (403, "owner_only"), f"{method} {path} -> {status} {body}"
    assert (
        _snapshot(env.user_cfg) == before
    ), "a refused request changed the owner's config or library"
    assert env.started == [], f"a refused request started provisioning: {env.started}"
    assert routes._engine_state.state == "idle" and routes._assets_state.state == "idle"
    assert len(env.denials) == 1 and env.denials[0].startswith("pptx_maker."), env.denials


@pytest.mark.parametrize(
    "who",
    [
        pytest.param(_as(_OWNER), id="owner"),
        pytest.param(_as("app:pptx-maker", "pptx-maker"), id="own-app-token"),
    ],
)
@pytest.mark.asyncio
async def test_owner_and_app_token_are_admitted(env, who: dict[str, str]) -> None:
    deck_root = env.tmp / "new-decks"
    deck_root.mkdir()
    client = await _client()
    try:
        put = await client.put(f"{_BASE}/config", json={"deckRoot": str(deck_root)}, headers=who)
        imp = await client.post(
            f"{_BASE}/styles/import?name=mine", data=b"<html><body>mine</body></html>", headers=who
        )
        pin = await client.post(
            f"{_BASE}/styles/pin", json={"name": "mine", "pinned": True}, headers=who
        )
        prov = await client.post(f"{_BASE}/engine/provision", headers=who)
        statuses = (put.status, imp.status, pin.status, prov.status)
        await asyncio.sleep(0.1)
    finally:
        await client.close()
    assert statuses == (200, 200, 200, 202), statuses
    config = json.loads((env.user_cfg / "config.json").read_text())
    assert config.get("output_dir") == str(deck_root.resolve())
    assert (env.user_cfg / "styles" / "mine.html").exists()
    assert env.started == ["engine"]
    assert env.denials == []
