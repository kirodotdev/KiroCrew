"""Project MCP approval cards refuse launch rows that redaction would hide."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.change_card_catalog import CardCatalogError
from kiro_crew.change_card_project_mcp import build_preview, server_snapshot
from kiro_crew.dashboard.project_mcp_cards import offer_project_mcp_cards
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE

# Joined from pieces, like ``kiro_crew.testing.synthetic_credentials``, so no source
# line carries a whole key block.
_PEM = "PRIVATE" + " KEY"
_HIDDEN = f"# -----BEGIN {_PEM}-----\nprintf hidden\n# -----END {_PEM}-----"


@pytest.fixture
def project(tmp_path, monkeypatch):
    settled = threading.Event()
    settled.set()
    monkeypatch.setattr(approvals, "_MIGRATION_SETTLED", settled)
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def _server(folder, entry, name="weather"):
    (folder / CLAUDE_PROJECT_MCP_FILE).write_text(
        json.dumps({"mcpServers": {name: entry}}), encoding="utf-8"
    )
    [server] = approvals.pending_servers(folder)
    params = {
        "folder": server.folder,
        "file": server.file,
        "server": server.name,
        "fingerprint": server.fingerprint,
    }
    return server, params


@pytest.mark.parametrize("key", ["args", "url"])
def test_a_redacted_launch_has_no_apply_step(project, key):
    entry = {"command": "weather-server", key: ["-c", _HIDDEN] if key == "args" else _HIDDEN}
    server, params = _server(project, entry)
    out: dict[str, Any] = {}
    with pytest.raises(CardCatalogError) as caught:
        build_preview(params, server_snapshot(params), out)
    assert caught.value.code == "card_hides_launch"
    assert "apply" not in out
    assert approvals.pending_servers(project) == [server]


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, "plain", "hidden"])
async def test_a_redacted_launch_gets_one_notice_and_stays_held(project, monkeypatch, previous):
    from kiro_crew.agent_sdk import capabilities
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
    from kiro_crew.dashboard import state as dashboard_state
    from kiro_crew.dashboard.handlers import change_cards_propose

    entry = {"command": "sh", "args": ["-c", _HIDDEN]}
    server, _ = _server(project, {"command": "weather-server"} if previous == "plain" else entry)
    if previous:
        approvals.approve(server.folder, server.file, server.name, server.fingerprint)
        server, _ = _server(project, {**entry, "env": {"MODE": "new"}})
    rows = []
    cards = []

    async def propose(*_a, params, **_kw):
        out: dict[str, Any] = {}
        build_preview(params, server_snapshot(params), out)
        cards.append(out)
        return out

    monkeypatch.setattr(change_cards_propose, "propose_in_slot", propose)
    monkeypatch.setattr(
        dashboard_state,
        "append_and_surface",
        lambda state, slot, role, content, *_a: rows.append((role, content)),
    )
    monkeypatch.setattr(
        capabilities, "capabilities_of", lambda _: SimpleNamespace(backend=ACP_BACKEND_CLAUDE)
    )
    slot = SimpleNamespace(key="chat-1", project=str(project))
    await offer_project_mcp_cards(object(), slot, SimpleNamespace(cwd=str(project)), "sk")
    assert cards == []
    assert rows == [
        (
            "notice",
            f"⚠ weather in {server.path} has a secret in its command or URL. "
            "Move the secret into env or headers, then start a new chat.",
        )
    ]
    assert approvals.pending_servers(project) == [server]


def test_an_ordinary_launch_still_builds_with_redacted_settings(project):
    entry = {
        "command": "weather-server",
        "env": {"VALUE": _HIDDEN},
        "headers": {"X-Value": _HIDDEN},
        "extra-setting": _HIDDEN,
    }
    _, params = _server(project, entry)
    out: dict[str, Any] = {}
    build_preview(params, server_snapshot(params), out)
    rows = {row["label"]: row for row in out["changes"]}
    assert rows["Runs"]["after"] == "weather-server"
    assert _HIDDEN not in rows["Environment"]["add"][0]
    assert _HIDDEN not in rows["Headers"]["add"][0]
    assert rows["extra-setting"]["after"] != _HIDDEN
    assert out["apply"][0]["path"] == "/api/mcp/project-servers/approve"


@pytest.mark.parametrize("side", ["before", "after", "removed"])
@pytest.mark.parametrize("key", ["args", "url"])
def test_only_a_redacted_launch_allow_would_approve_is_refused(project, side, key):
    plain = {"command": "weather-server"}
    hidden = {**plain, key: ["-c", _HIDDEN] if key == "args" else _HIDDEN}
    entry = hidden if side == "after" else plain
    if side == "removed" and key == "args":
        entry = {"url": "https://example.invalid/mcp"}
    _, params = _server(project, entry)
    snapshot = server_snapshot(params)
    snapshot.update(
        status="changed", approved=approvals.summary(plain if side == "after" else hidden)
    )
    out: dict[str, Any] = {}
    if side != "after":
        # An earlier launch shows only as "before"; Allow approves the current one.
        build_preview(params, snapshot, out)
        assert out["apply"][0]["path"] == "/api/mcp/project-servers/approve"
        return
    with pytest.raises(CardCatalogError) as caught:
        build_preview(params, snapshot, out)
    assert caught.value.code == "card_hides_launch"
    assert "apply" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, "plain", "hidden"])
@pytest.mark.parametrize("target", ["env", "headers"])
async def test_moving_the_secret_allows_the_card_and_approval(
    project, monkeypatch, previous, target
):
    from unittest.mock import AsyncMock, Mock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import _shared, sessions
    from kiro_crew.dashboard.handlers.project_mcp_approve import api_mcp_project_server_approve

    hidden = (
        {"command": "weather-server", "args": ["--token", _HIDDEN]}
        if target == "env"
        else {"url": "https://example.invalid/mcp?api_key=" + _HIDDEN}
    )
    server, params = _server(
        project, {"command": "weather-server"} if previous == "plain" else hidden
    )
    if previous:
        approvals.approve(server.folder, server.file, server.name, server.fingerprint)
        server, params = _server(project, {**hidden, "timeout": 100, "extra-setting": "new"})
    with pytest.raises(CardCatalogError) as caught:
        build_preview(params, server_snapshot(params), {})
    assert caught.value.code == "card_hides_launch"

    entry = (
        {"command": "weather-server", "env": {"WEATHER_TOKEN": _HIDDEN}}
        if target == "env"
        else {"url": "https://example.invalid/mcp", "headers": {"X-API-Key": _HIDDEN}}
    )
    _, params = _server(project, entry)
    out: dict[str, Any] = {}
    build_preview(params, server_snapshot(params), out)
    assert out["apply"][0]["path"] == "/api/mcp/project-servers/approve"
    owner = AsyncMock(return_value=None)
    reset = AsyncMock(return_value=1)
    monkeypatch.setattr(_shared, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(sessions, "_reset_all_sessions", reset)
    request = Mock(spec=web.Request, json=AsyncMock(return_value=out["apply"][0]["body"]))
    response = await api_mcp_project_server_approve(request)
    assert response.status == 200
    owner.assert_awaited_once()
    reset.assert_awaited_once()
    assert approvals.pending_servers(project) == []
