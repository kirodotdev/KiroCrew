"""A commandless ``"type": "registry"`` pointer is not a broken local entry.

kiro-cli writes ``{"type": "registry"}`` with no command or url for a server it
resolves from the admin's catalog. Discovery reports it as ``registry``
(rendered as the existing "Not verified" badge with a registry hint), never
spawns it, and keeps it out of the probe cache and the failure count.
"""

from __future__ import annotations

import asyncio
import json

from kiro_crew import mcp_quarantine
from kiro_crew.mcp_discovery import (
    McpServerInfo,
    _probe_cache,
    _server_from_spec,
    list_servers,
    probe_server,
)

POINTER = {"type": "registry", "disabled": False}


def test_bare_pointer_is_flagged() -> None:
    assert _server_from_spec("gov", dict(POINTER), "mcp.json").registry_pointer is True


def test_marker_with_a_local_command_or_url_is_not_a_pointer() -> None:
    """Only the commandless, urlless shape is a pointer; anything carrying a local
    transport keeps today's handling (spawned or probed as declared)."""
    assert (
        _server_from_spec("a", {"type": "registry", "command": "x"}, "agent").registry_pointer
        is False
    )
    assert (
        _server_from_spec(
            "b", {"type": "registry", "url": "https://h/mcp"}, "agent"
        ).registry_pointer
        is False
    )
    assert _server_from_spec("c", {"type": "stdio"}, "agent").registry_pointer is False


def test_probe_reports_registry_without_spawning_or_caching(monkeypatch) -> None:
    def _no_spawn(*a, **k):  # pragma: no cover - reaching here is the failure
        raise AssertionError("a registry pointer must never be spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)
    _probe_cache.pop("gov", None)
    server = _server_from_spec("gov", dict(POINTER), "mcp.json")
    out = asyncio.run(probe_server(server))
    assert out.status == "registry"
    assert out.error == ""
    assert "gov" not in _probe_cache


def test_unmarked_commandless_entry_is_still_an_error() -> None:
    out = asyncio.run(probe_server(McpServerInfo(name="broken")))
    assert out.status == "error"
    assert out.error == "no command"


def test_registry_status_carries_no_quarantine_verdict() -> None:
    assert "registry" not in mcp_quarantine.FAILING_STATUSES


def test_list_servers_reports_pointer_as_registry_before_any_probe(tmp_path, monkeypatch) -> None:
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    cfg = {"mcpServers": {"gov": dict(POINTER), "local": {"command": "cmd"}}}
    (agent_dir / "defaults.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (tmp_path / "x",))
    monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
    _probe_cache.pop("gov", None)
    _probe_cache.pop("local", None)

    rows = {s.name: s for s in list_servers()}
    assert rows["gov"].status == "registry"
    assert rows["gov"].to_dict()["status"] == "registry"
    assert rows["local"].status == "unknown"
