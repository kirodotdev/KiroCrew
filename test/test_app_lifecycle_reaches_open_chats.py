"""An app's update, disable or uninstall reaches the chats already open.

Each route ends with the step an MCP page write ends with
(``template_server_request._reach_sessions``) once the agent spec holds the change,
when the app was enabled and declares MCP servers or an update registered one, and
leaves open chats alone otherwise (app-kit-platform §1). The step itself is pinned in
``test_template_server_request_parts.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _install, _make_app, _setup_env

import kiro_crew.apps.routes as routes_mod
import kiro_crew.dashboard.handlers.template_server_request as tsr
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, AppResult, app_dir, enable_app

#: A backend-less app's HTTP server: registered as declared, with no backend to start.
_ONE_SERVER = {"srv": {"url": "http://127.0.0.1:7778/mcp"}}
_ACTIONS = ["update", "registry-update", "disable", "uninstall"]
#: (action, servers the installed app declares, enabled, servers the update ships, steps)
_CASES = [
    *[(a, _ONE_SERVER, True, _ONE_SERVER, 1) for a in _ACTIONS],
    *[(a, {}, True, {}, 0) for a in _ACTIONS],
    *[(a, _ONE_SERVER, False, _ONE_SERVER, 0) for a in _ACTIONS],
    ("update", {}, True, _ONE_SERVER, 1),
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

    def _update(source: str, expected_name: str | None = None) -> AppResult:
        manifest_path = app_dir(APP) / APP_MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text())
        manifest_path.write_text(json.dumps({**manifest, "mcpServers": shipped}))
        return AppResult(ok=True, name=APP, message="updated")

    async def _registry_install(name: str, **kwargs: Any) -> dict[str, Any]:
        await kwargs["before_replace"]()
        return {"ok": True, "name": name}

    monkeypatch.setattr(tsr, "_reach_sessions", _reach)
    monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
    monkeypatch.setattr(routes_mod, "update_app", _update)
    if action == "registry-update":
        monkeypatch.setattr(routes_mod, "git_unavailable_refusal", lambda n: None)
        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _registry_install)

    route = "update" if action == "registry-update" else action
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/{route}", json={})
        assert resp.status == 200, await resp.text()
    # The step runs once the spec holds the change: the new server after an update,
    # no server after a disable or uninstall.
    assert reached == [route == "update"] * steps
