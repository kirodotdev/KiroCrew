"""POST /api/mcp/probe/{name}: test one MCP server, read-only for its config.

The probe runs for real against a command that does not exist, so nothing
is spawned and the test needs no patch of a shared probe name. A disabled
server is answered ``disabled`` by ``probe_server``'s consent refusal.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.dashboard.handlers import mcp_probe
from kiro_crew.mcp_discovery_server_info import McpServerInfo

MISSING = "/nonexistent/kc-probe-one-test-binary"


@pytest.fixture
def owner(monkeypatch):
    import kiro_crew.dashboard.handlers._shared as shared

    monkeypatch.setattr(shared, "require_owner_dashboard_request", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_probe, "_read_global_mcps", lambda: {})
    monkeypatch.setattr(mcp_probe, "_kirocrew_mcp_servers", lambda: {})
    monkeypatch.setattr(mcp_probe, "_record_probe_verdicts", lambda rows: None)


def _request(name: str) -> SimpleNamespace:
    state = SimpleNamespace(sessions=None)
    return SimpleNamespace(match_info={"name": name}, app={"state": state})


def _server(name: str, disabled: bool = False) -> McpServerInfo:
    return McpServerInfo(name=name, command=MISSING, disabled=disabled)


@pytest.mark.asyncio
async def test_unknown_name_is_404(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc

    monkeypatch.setattr(disc, "list_servers", lambda: [_server("alpha")])
    resp = await mcp_probe.api_mcp_probe_one(_request("missing"))
    assert resp.status == 404
    assert json.loads(resp.body)["name"] == "missing"


@pytest.mark.asyncio
async def test_named_server_is_probed_and_its_row_returned(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc

    monkeypatch.setattr(disc, "list_servers", lambda: [_server("alpha"), _server("beta")])

    resp = await mcp_probe.api_mcp_probe_one(_request("alpha"))

    assert resp.status == 200
    row = json.loads(resp.body)
    assert row["name"] == "alpha"
    assert row["status"] == "error"  # the command does not exist, so the probe failed


@pytest.mark.asyncio
async def test_disabled_server_is_answered_disabled(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc

    monkeypatch.setattr(disc, "list_servers", lambda: [_server("off", disabled=True)])

    resp = await mcp_probe.api_mcp_probe_one(_request("off"))

    assert resp.status == 200
    assert json.loads(resp.body)["status"] == "disabled"


@pytest.mark.asyncio
async def test_a_probed_server_gets_the_denied_env_note(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc

    noted: list[str] = []
    monkeypatch.setattr(disc, "list_servers", lambda: [_server("alpha")])
    monkeypatch.setattr(disc, "_note_denied_env", lambda s: noted.append(s.name))

    await mcp_probe.api_mcp_probe_one(_request("alpha"))

    assert noted == ["alpha"]


@pytest.mark.asyncio
async def test_the_probed_row_replaces_only_its_cache_entry(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc

    cache = [{"name": "alpha", "status": "stale"}, {"name": "beta", "status": "ok"}]
    monkeypatch.setattr(mcp_probe, "_mcp_probe_cache", cache)
    monkeypatch.setattr(disc, "list_servers", lambda: [_server("alpha"), _server("beta")])

    await mcp_probe.api_mcp_probe_one(_request("alpha"))

    assert cache[0]["name"] == "alpha" and cache[0]["status"] == "error"
    assert cache[1] == {"name": "beta", "status": "ok"}


@pytest.mark.asyncio
async def test_the_probed_row_is_recorded_as_a_verdict(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc

    recorded: list[list[dict]] = []
    monkeypatch.setattr(mcp_probe, "_record_probe_verdicts", lambda rows: recorded.append(rows))
    monkeypatch.setattr(disc, "list_servers", lambda: [_server("alpha")])

    await mcp_probe.api_mcp_probe_one(_request("alpha"))

    assert len(recorded) == 1
    assert [row["name"] for row in recorded[0]] == ["alpha"]


@pytest.mark.asyncio
async def test_a_busy_config_file_answers_503(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc

    def busy() -> dict:
        raise PermissionError("held by another process")

    monkeypatch.setattr(disc, "list_servers", lambda: [_server("alpha")])
    monkeypatch.setattr(mcp_probe, "_read_global_mcps", busy)

    resp = await mcp_probe.api_mcp_probe_one(_request("alpha"))

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "mcp_config_busy"


@pytest.mark.asyncio
async def test_a_built_in_server_stays_marked_built_in_after_a_test(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc
    from kiro_crew.dashboard.handlers import mcp as mcp_facade

    monkeypatch.setattr(mcp_facade, "_builtin_mcp_server_names", lambda: frozenset({"core"}))
    monkeypatch.setattr(disc, "list_servers", lambda: [_server("core"), _server("alpha")])

    core = json.loads((await mcp_probe.api_mcp_probe_one(_request("core"))).body)
    alpha = json.loads((await mcp_probe.api_mcp_probe_one(_request("alpha"))).body)

    assert core["builtin"] is True
    assert alpha["builtin"] is False
