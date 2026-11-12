"""A server the rebuild drops for a missing command is recorded and shown on its row.

Without the record the drop reaches only the gateway log: the dashboard lists the
server while the agent has none of its tools. The rebuild records each
command-not-found drop, and ``GET /api/mcp`` stamps it onto the server's row as
``agentDropped``.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

from kiro_crew import mcp_dropped_servers
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.mcp_discovery import McpServerInfo


@pytest.fixture(autouse=True)
def _pinned_kiro_cli_version(_floor_monkeypatch):
    # Every rebuild ends in a spec-permissions gate that would otherwise run the
    # host's real ``kiro-cli --version``.
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


def _request() -> Any:
    class _App(dict):
        pass

    class _Req(dict):
        def __init__(self, app):
            super().__init__(user="local-app", app="")
            self.app = app

    app = _App()
    app["state"] = SimpleNamespace(
        _background_tasks=set(), sessions=SimpleNamespace(final_drain_started=False)
    )
    return _Req(app)


async def _rows(monkeypatch, tmp_path) -> dict[str, dict]:
    import kiro_crew.mcp_discovery as disc

    servers = [McpServerInfo(name="example-server", command="example-mcp-program")]
    monkeypatch.setattr(disc, "list_servers", lambda *a, **k: list(servers))
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(mcp_mod, "_kirocrew_mcp_json", lambda: tmp_path / "absent-kc.json")
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
    monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
    monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", True)
    monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", AsyncMock(return_value=None))
    resp = await mcp_mod.api_mcp_servers(_request())
    body = resp.body
    assert isinstance(body, (bytes, bytearray))
    return {r["name"]: r for r in json.loads(body)}


@pytest.mark.asyncio
async def test_a_command_not_found_drop_is_recorded_and_shown_until_it_resolves(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    cfg_dir = bundled_defaults(tmp_path)
    kiro_servers = {"example-server": {"command": "example-mcp-program"}}

    def _missing(c, **kw):
        return None if c == "example-mcp-program" else c

    emitted = run_install_mcp_merge(
        tmp_path, cfg_dir, cc_servers={}, kiro_servers=kiro_servers, which_side_effect=_missing
    )
    assert "example-server" not in emitted["mcpServers"]
    assert mcp_dropped_servers.load_dropped() == {
        "example-server": {"command": "example-mcp-program", "reason": "command_not_found"}
    }
    row = (await _rows(monkeypatch, tmp_path))["example-server"]
    assert row["agentDropped"] == {
        "command": "example-mcp-program",
        "reason": "command_not_found",
    }

    # Once the program is installed, the next rebuild mounts it and the note goes.
    binary = tmp_path / "bin" / "example-mcp-program"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    def _found(c, **kw):
        return str(binary) if c == "example-mcp-program" else c

    emitted = run_install_mcp_merge(
        tmp_path, cfg_dir, cc_servers={}, kiro_servers=kiro_servers, which_side_effect=_found
    )
    assert "example-server" in emitted["mcpServers"]
    assert mcp_dropped_servers.load_dropped() == {}
    assert "agentDropped" not in (await _rows(monkeypatch, tmp_path))["example-server"]


def test_a_raw_slashed_drop_does_not_mark_a_surviving_alias(tmp_path) -> None:
    cfg_dir = bundled_defaults(tmp_path)
    kiro_servers = {
        "playwright-mcp": {"command": "working-playwright"},
        "npm:@playwright/mcp": {"command": "missing-playwright"},
    }
    mcp_dropped_servers.record_rebuild_drops({"npm:@playwright/mcp": "missing-playwright"})

    def _resolve(command, **kw):
        return None if command == "missing-playwright" else command

    emitted = run_install_mcp_merge(
        tmp_path, cfg_dir, cc_servers={}, kiro_servers=kiro_servers, which_side_effect=_resolve
    )

    assert "npm:@playwright/mcp" not in emitted["mcpServers"]
    assert emitted["mcpServers"]["playwright-mcp"]["command"] == "working-playwright"
    assert mcp_dropped_servers.load_dropped() == {}
    rows = [{"name": "playwright-mcp"}]
    mcp_mod._annotate_agent_drops(rows)
    assert "agentDropped" not in rows[0]


def test_a_raw_slashed_drop_is_shown_on_its_canonical_row() -> None:
    mcp_dropped_servers.record_rebuild_drops({"npm:@playwright/mcp": "missing-playwright"})
    rows = [{"name": "playwright-mcp"}]

    mcp_mod._annotate_agent_drops(rows)

    assert rows[0]["agentDropped"] == {
        "command": "missing-playwright",
        "reason": "command_not_found",
    }


def test_an_exact_drop_wins_over_an_aliased_drop() -> None:
    mcp_dropped_servers.record_rebuild_drops(
        {"playwright-mcp": "exact-program", "npm:@playwright/mcp": "raw-program"}
    )
    rows: list[dict] = [{"name": "playwright-mcp"}, {"name": "npm:@playwright/mcp"}]

    mcp_mod._annotate_agent_drops(rows)

    assert [row["agentDropped"]["command"] for row in rows] == ["exact-program", "raw-program"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["api_mcp_probe", "api_mcp_probe_cached"])
async def test_probe_response_carries_current_agent_drops(monkeypatch, tmp_path, endpoint) -> None:
    import kiro_crew.mcp_discovery as disc

    await _rows(monkeypatch, tmp_path)
    servers = [McpServerInfo(name="example-server", command="example-mcp-program")]
    monkeypatch.setattr(disc, "probe_all", AsyncMock(return_value=servers))
    monkeypatch.setattr(mcp_mod, "_evaluate_shareability", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [server.to_dict() for server in servers])
    mcp_dropped_servers.record_rebuild_drops({"example-server": "example-mcp-program"})

    resp = await getattr(mcp_mod, endpoint)(_request())

    assert resp.status == 200
    assert json.loads(resp.body)[0]["agentDropped"] == {
        "command": "example-mcp-program",
        "reason": "command_not_found",
    }

    mcp_dropped_servers.record_rebuild_drops({})
    refreshed = await mcp_mod.api_mcp_probe_cached(_request())
    assert refreshed.status == 200
    body = refreshed.body
    assert isinstance(body, (bytes, bytearray))
    assert "agentDropped" not in json.loads(body)[0]
