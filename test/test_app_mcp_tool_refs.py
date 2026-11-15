"""App MCP servers are exposed in kirocrew.json's ``tools`` by the lifecycle writers.

kiro-cli gives an agent tools only from servers its ``tools`` list references. The agent
rebuild adds ``@{app}:{server}``; enable/register must too, or an app enabled after the
last rebuild runs its server in the main agent with none of its tools callable.
"""

from __future__ import annotations

import json

from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source

from kiro_crew.apps.bridges import _deregister_mcp_servers, _register_mcp_servers
from kiro_crew.apps.bridges_mcp_config import _sync_app_tool_refs
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest


class TestSyncAppToolRefs:
    def test_adds_refs_for_the_apps_servers(self):
        data = {"mcpServers": {"app-a:srv": {}, "kb": {}}, "tools": ["@kb", "read"]}
        _sync_app_tool_refs(data, "app-a")
        assert data["tools"] == ["@kb", "read", "@app-a:srv"]

    def test_drops_refs_whose_server_is_gone(self):
        data = {"mcpServers": {"app-a:keep": {}}, "tools": ["@app-a:gone", "@app-a:keep"]}
        _sync_app_tool_refs(data, "app-a")
        assert data["tools"] == ["@app-a:keep"]

    def test_leaves_other_apps_and_owner_refs_alone(self):
        data = {"mcpServers": {}, "tools": ["@app-b:srv", "@app-ab:srv", "@kb"]}
        _sync_app_tool_refs(data, "app-a")
        assert data["tools"] == ["@app-b:srv", "@app-ab:srv", "@kb"]

    def test_is_idempotent_and_adds_no_empty_tools_key(self):
        data = {"mcpServers": {"app-a:srv": {}}, "tools": ["@app-a:srv"]}
        _sync_app_tool_refs(data, "app-a")
        _sync_app_tool_refs(data, "app-a")
        assert data["tools"] == ["@app-a:srv"]
        empty: dict = {"mcpServers": {}}
        _sync_app_tool_refs(empty, "app-a")
        assert "tools" not in empty


class TestLifecycleExposure:
    def test_register_exposes_and_deregister_hides(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "kirocrew.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        mcp_path.write_text(json.dumps({"mcpServers": {"kb": {"command": "kb"}}, "tools": ["@kb"]}))

        src = _make_app_source(
            tmp_path, mcpServers={"cmd": {"command": "python3", "args": ["s.py"]}}
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )

        assert _register_mcp_servers("test-app", manifest) == ["test-app:cmd"]
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert data["tools"] == ["@kb", "@test-app:cmd"]

        assert _deregister_mcp_servers("test-app") == 1
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert data["tools"] == ["@kb"]
        assert "test-app:cmd" not in data["mcpServers"]

    def test_skipped_http_server_is_not_exposed(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "kirocrew.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {"test-app:web": {"url": "http://localhost:1/mcp"}},
                    "tools": ["@test-app:web"],
                }
            )
        )

        src = _make_app_source(
            tmp_path,
            mcpServers={"web": {"url": "http://localhost:9000/mcp"}},
            backend={"entryPoint": "server.py", "type": "python", "port": "auto"},
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )

        assert _register_mcp_servers("test-app", manifest) == []
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "test-app:web" not in data["mcpServers"]
        assert data.get("tools", []) == []
