"""An app's enable, update, disable or uninstall reaches the chats already open.

Each route ends with the step an MCP page write ends with
(``template_server_request._reach_sessions``) once the agent spec holds the change,
when the app declares MCP servers and was enabled or is being enabled, or an update
registered one, and leaves open chats alone otherwise (app-kit-platform §1). A
registry install over an installed app is an update. The step itself is pinned in
``test_template_server_request_parts.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _install, _make_app, _make_app_source, _setup_env

import kiro_crew.apps.routes as routes_mod
import kiro_crew.dashboard.handlers.template_server_request as tsr
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    AppResult,
    app_dir,
    enable_app,
    get_app,
    install_app,
)

#: A backend-less app's HTTP server: registered as declared, with no backend to start.
_ONE_SERVER = {"srv": {"url": "http://127.0.0.1:7778/mcp"}}
_ACTIONS = [
    "update",
    "registry-update",
    "registry-install",
    "registry-install-stream",
    "disable",
    "uninstall",
]
#: (action, servers the installed app declares, enabled, servers the update ships, steps)
_CASES = [
    *[(a, _ONE_SERVER, True, _ONE_SERVER, 1) for a in _ACTIONS],
    *[(a, {}, True, {}, 0) for a in _ACTIONS],
    *[(a, _ONE_SERVER, False, _ONE_SERVER, 0) for a in _ACTIONS],
    ("update", {}, True, _ONE_SERVER, 1),
    # The re-installed version drops the server an enabled app's open chats hold.
    ("registry-install", _ONE_SERVER, True, {}, 1),
    ("registry-install-stream", _ONE_SERVER, True, {}, 1),
    ("enable", _ONE_SERVER, False, _ONE_SERVER, 1),
    ("enable", {}, False, {}, 0),
]


@pytest.mark.parametrize(("action", "declared", "enabled", "shipped", "steps"), _CASES)
@pytest.mark.asyncio
async def test_lifecycle_route_reaches_open_chats_when_an_enabled_app_has_mcp_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    declared: dict[str, Any],
    enabled: bool,
    shipped: dict[str, Any],
    steps: int,
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, mcpServers=declared)
    if enabled:
        assert enable_app(APP).ok
    spec = tmp_path / "kiro-agents" / "kirocrew.json"
    # Whether the app's server is in the agent spec each time the step runs.
    reached: list[bool] = []

    async def _reach(request: object) -> int:
        servers = json.loads(spec.read_text()).get("mcpServers", {}) if spec.exists() else {}
        reached.append(f"{APP}:srv" in servers)
        return 0

    def _ship() -> None:
        manifest_path = app_dir(APP) / APP_MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text())
        manifest_path.write_text(json.dumps({**manifest, "mcpServers": shipped}))

    def _update(source: str, expected_name: str | None = None) -> AppResult:
        _ship()
        return AppResult(ok=True, name=APP, message="updated")

    async def _registry_install(name: str, **kwargs: Any) -> dict[str, Any]:
        await kwargs["before_replace"]()
        _ship()
        return {"ok": True, "name": name}

    monkeypatch.setattr(tsr, "_reach_sessions", _reach)
    monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
    monkeypatch.setattr(routes_mod, "update_app", _update)
    if action.startswith("registry-"):
        monkeypatch.setattr(routes_mod, "git_unavailable_refusal", lambda n: None)
        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _registry_install)

    path = {
        "registry-update": f"/api/apps/{APP}/update",
        "registry-install": "/api/apps/registry/install",
        "registry-install-stream": "/api/apps/registry/install-stream",
    }.get(action, f"/api/apps/{APP}/{action}")
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(path, json={"name": APP})
        assert resp.status in (200, 201), await resp.text()
        await resp.text()
    # The step runs once the spec holds the change: the shipped server after an enable,
    # an update or a re-install, no server after a disable or uninstall.
    assert reached == [bool(shipped) and action not in ("disable", "uninstall")] * steps


@pytest.mark.asyncio
async def test_an_enable_reaches_open_chats_before_the_health_watch_writes_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An HTTP server on the app's own backend is written by the backend's health watch
    after the route returns, so the enable takes the step on what the app declares."""
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(
        tmp_path, mcpServers=_ONE_SERVER, backend={"entryPoint": "server.py", "port": "auto"}
    )
    (src / "server.py").write_text("")
    assert install_app(src).ok
    spec = tmp_path / "kiro-agents" / "kirocrew.json"
    reached: list[bool] = []

    async def _reach(request: object) -> int:
        servers = json.loads(spec.read_text()).get("mcpServers", {}) if spec.exists() else {}
        reached.append(f"{APP}:srv" in servers)
        return 0

    monkeypatch.setattr(tsr, "_reach_sessions", _reach)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/enable", json={})
        assert resp.status == 200, await resp.text()
    assert reached == [False]


@pytest.mark.asyncio
async def test_an_enable_decides_the_step_on_the_record_it_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reinstall that lands between the enable's first read and its lifecycle lock
    adds a server; the enable registers it, so it takes the step on that record."""
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    spec = tmp_path / "kiro-agents" / "kirocrew.json"
    reached: list[bool] = []
    reads: list[str] = []

    async def _reach(request: object) -> int:
        servers = json.loads(spec.read_text()).get("mcpServers", {}) if spec.exists() else {}
        reached.append(f"{APP}:srv" in servers)
        return 0

    def _get_app(name: str) -> dict[str, Any] | None:
        info = get_app(name)
        reads.append(name)
        if len(reads) == 1:
            manifest_path = app_dir(APP) / APP_MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text())
            manifest_path.write_text(json.dumps({**manifest, "mcpServers": _ONE_SERVER}))
        return info

    monkeypatch.setattr(tsr, "_reach_sessions", _reach)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
    monkeypatch.setattr(routes_mod, "get_app", _get_app)
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/enable", json={})
        assert resp.status == 200, await resp.text()
    assert reached == [True]
