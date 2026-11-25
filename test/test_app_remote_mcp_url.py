"""An app's REMOTE MCP url is the app's own endpoint, never its backend's port.

Only a url that points at this machine (a loopback host) names the app's own
gateway-launched backend, whose manifest port is illustrative and gets swapped for the
live one. A url on any other host (for example a hosted MCP gateway) is written as
declared, whether or not the backend is up.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source

from kiro_crew.apps.bridges import _register_mcp_servers, _resolve_live_mcp_url
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest

_REMOTE = "https://abc.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"


@pytest.mark.parametrize(
    "url",
    [
        _REMOTE,
        "https://mcp.example.com:8443/mcp",
        "http://10.0.0.5:9100/mcp",
        "http://localhost.example.com:9100/mcp",
    ],
)
def test_a_remote_url_is_never_rewritten(url):
    assert _resolve_live_mcp_url("app", url, live_port=9101) == url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:9100/mcp", "http://localhost:9101/mcp"),
        ("http://127.0.0.1:9100/mcp", "http://127.0.0.1:9101/mcp"),
        ("http://127.0.0.2/mcp", "http://127.0.0.2:9101/mcp"),
        ("http://[::1]:9100/mcp", "http://[::1]:9101/mcp"),
    ],
)
def test_a_loopback_url_takes_the_live_port(url, expected):
    assert _resolve_live_mcp_url("app", url, live_port=9101) == expected


def _register(tmp_path, app_env, monkeypatch, *, live_port):
    import kiro_crew.apps.backend as backend_mod
    import kiro_crew.apps.bridges as bmod

    mcp_path = tmp_path / "mcp.json"
    monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
    monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: live_port)
    src = _make_app_source(
        tmp_path,
        backend={"entryPoint": "backend/app.py"},
        mcpServers={
            "remote": {"url": _REMOTE},
            "local": {"url": "http://localhost:9100/mcp"},
        },
    )
    install_app(src)
    manifest = AppManifest.from_json_file(
        app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
    )
    registered = _register_mcp_servers("test-app", manifest)
    written = json.loads(mcp_path.read_text(encoding="utf-8")).get("mcpServers", {})
    return registered, written


def test_a_backend_apps_remote_url_keeps_its_host_and_port(tmp_path, app_env, monkeypatch):
    _, written = _register(tmp_path, app_env, monkeypatch, live_port=9100)
    assert written["test-app:remote"]["url"] == _REMOTE
    assert written["test-app:local"]["url"] == "http://localhost:9100/mcp"


def test_a_remote_url_is_registered_while_the_backend_is_down(tmp_path, app_env, monkeypatch):
    registered, written = _register(tmp_path, app_env, monkeypatch, live_port=None)
    assert registered == ["test-app:remote"]
    assert written["test-app:remote"]["url"] == _REMOTE
    assert "test-app:local" not in written  # the illustrative local port is still skipped


def test_the_rebuild_keeps_a_remote_url_with_no_live_entry(monkeypatch):
    from kiro_crew import agent

    class _M:
        mcpServers = {
            "remote": {"url": _REMOTE},
            "local": {"url": "http://127.0.0.1:9100/mcp"},
        }
        backend = SimpleNamespace(entryPoint="backend.py")

    monkeypatch.setattr(agent, "_ceiling_filtered_spec", lambda ref, spec, *, audit=True: spec)
    import kiro_crew.apps.bridges as bridges
    import kiro_crew.apps.manager as manager

    monkeypatch.setattr(manager, "list_apps", lambda: [{"name": "someapp"}])
    monkeypatch.setattr(manager, "is_app_enabled", lambda n: True)
    monkeypatch.setattr(manager, "get_app_manifest", lambda n: _M())
    monkeypatch.setattr(bridges, "registered_app_mcp_servers", lambda: {})
    out = agent._collect_app_mcp_servers()
    assert out == {"someapp:remote": {"url": _REMOTE}}
