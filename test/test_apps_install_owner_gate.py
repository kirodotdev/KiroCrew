"""Owner gate on the app install / update / registry-write routes.

These routes choose the code the gateway loads: a registry URL, a registry
install, a local-path install, or an update from a body-supplied source. A
dashboard subject that is not the owner (``app == ""``, ``user != owner_id``)
must get 403 before any side effect runs.

Rows per route:

* owner: passes the gate and reaches the side effect;
* non-owner dashboard subject: 403, side effect never reached;
* app token, including one on its own ``/api/apps/<self>/update``: refused by
  the owner gate on every route.

The requests go through ``register_app_routes``, so the production route
table is what is exercised. Only the side-effect functions are stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import routes

pytestmark = pytest.mark.asyncio

OWNER = "owner-subject"
NON_OWNER = "channel-user"
APP = "victim"


class _RecordingSel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_api_access(self, *a: Any, **k: Any) -> None:
        self.calls.append(k)


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    hits: list[str] = []
    monkeypatch.setattr(routes, "sel", lambda: _RecordingSel())

    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(routes, "config_path", lambda: str(cfg))
    monkeypatch.setattr(routes, "_pinned_registries", lambda: [])

    # The registry PUT commits through the locked config writer (run off the
    # loop by ``run_config_write``), so that call is the side effect to observe.
    real_update = routes.update_config_locked

    def _update(path: Any = None, *a: Any, **k: Any) -> Any:
        if path is not None and Path(path) == cfg:
            hits.append("registries_saved")
        return real_update(path, *a, **k)

    monkeypatch.setattr(routes, "update_config_locked", _update)

    async def _hooks_stopped(*a: Any, **k: Any) -> bool:
        return True

    async def _noop_async(*a: Any, **k: Any) -> None:
        return None

    def _install_app(source: str, expected_name: str | None = None) -> Any:
        hits.append("install_app")
        return SimpleNamespace(ok=False, error="stub", name="", to_dict=lambda: {"ok": False})

    async def _install_from_registry(name: str, *a: Any, **k: Any) -> dict:
        hits.append("install_from_registry")
        return {"ok": False, "error": "stub"}

    def _update_app(source: str, expected_name: str | None = None) -> Any:
        hits.append("update_app")
        return SimpleNamespace(ok=False, error="stub", to_dict=lambda: {"ok": False})

    monkeypatch.setattr(routes, "stop_retained_startup_hooks", _hooks_stopped)
    monkeypatch.setattr(routes, "install_app", _install_app)
    monkeypatch.setattr(routes, "install_from_registry", _install_from_registry)
    monkeypatch.setattr(routes, "update_app", _update_app)
    monkeypatch.setattr(routes, "stop_app_backend", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_deregister_app_off_loop", _noop_async)
    monkeypatch.setattr(routes, "_restore_app_after_failed_update", _noop_async)
    monkeypatch.setattr(
        routes,
        "get_app",
        lambda name: {"lifecycle": "gateway", "source": "/orig/src"} if name == APP else None,
    )
    return hits


def _server(user: str, app_claim: str) -> TestServer:
    @web.middleware
    async def _claims(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = user
        request["app"] = app_claim
        return await handler(request)

    app = web.Application(middlewares=[_claims])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    routes.register_app_routes(app)
    return TestServer(app)


def _source_dir(tmp_path: Path) -> str:
    src = tmp_path / "chosen-src"
    src.mkdir(exist_ok=True)
    (src / "app.json").write_text(json.dumps({"name": APP, "version": "1.0.0"}))
    return str(src)


def _cases(tmp_path: Path) -> dict[str, tuple[str, str, dict, str]]:
    return {
        "registries_put": (
            "PUT",
            "/api/apps/registries",
            {"registries": [{"name": "x", "repo": "https://git.example.test/x/apps.git"}]},
            "registries_saved",
        ),
        "registry_install": (
            "POST",
            "/api/apps/registry/install",
            {"name": "some-app"},
            "install_from_registry",
        ),
        "registry_install_stream": (
            "POST",
            "/api/apps/registry/install-stream",
            {"name": "some-app"},
            "install_from_registry",
        ),
        "install": ("POST", "/api/apps/install", {"source": _source_dir(tmp_path)}, "install_app"),
        "update": (
            "POST",
            f"/api/apps/{APP}/update",
            {"source": _source_dir(tmp_path)},
            "update_app",
        ),
    }


ROUTES = [
    "registries_put",
    "registry_install",
    "registry_install_stream",
    "install",
    "update",
]


async def _call(user: str, app_claim: str, method: str, path: str, body: dict) -> int:
    async with TestClient(_server(user, app_claim)) as client:
        resp = await client.request(method, path, json=body)
        await resp.read()
        return resp.status


@pytest.mark.parametrize("route", ROUTES)
async def test_owner_reaches_the_side_effect(
    route: str, reached: list[str], tmp_path: Path
) -> None:
    method, path, body, effect = _cases(tmp_path)[route]
    status = await _call(OWNER, "", method, path, body)
    assert status != 403
    assert effect in reached


@pytest.mark.parametrize("route", ROUTES)
async def test_non_owner_dashboard_subject_is_refused(
    route: str, reached: list[str], tmp_path: Path
) -> None:
    method, path, body, effect = _cases(tmp_path)[route]
    status = await _call(NON_OWNER, "", method, path, body)
    assert status == 403
    assert reached == []


@pytest.mark.parametrize(
    "route",
    ["registries_put", "registry_install", "registry_install_stream", "install"],
)
async def test_granted_app_token_is_refused_on_install_routes(
    route: str, reached: list[str], tmp_path: Path
) -> None:
    method, path, body, effect = _cases(tmp_path)[route]
    status = await _call(APP, "some-app", method, path, body)
    assert status == 403
    assert reached == []


async def test_app_token_on_own_update_is_refused(reached: list[str], tmp_path: Path) -> None:
    method, path, body, effect = _cases(tmp_path)["update"]
    status = await _call(APP, APP, method, path, body)
    assert status == 403
    assert reached == []


async def test_app_token_cannot_update_itself_from_another_registry_app(
    reached: list[str],
) -> None:
    body = {"source": "registry:other-app"}
    status = await _call(APP, APP, "POST", f"/api/apps/{APP}/update", body)
    assert status == 403
    assert "install_from_registry" not in reached


async def test_registries_get_stays_open_to_non_owner(reached: list[str]) -> None:
    async with TestClient(_server(NON_OWNER, "")) as client:
        resp = await client.get("/api/apps/registries")
        assert resp.status == 200
