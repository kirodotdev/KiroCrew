"""``GET /api/mcp`` withholds a server's launch arguments from a delegated (``via_app``) token.

Launch arguments can carry a credential (``--token <value>``), exactly like headers, which the
listing already masks for every caller. The owner's own view keeps the real arguments.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_mcp_global_read_off_loop import _request, _State, global_file  # noqa: F401

import kiro_crew.mcp_discovery as disc
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import McpServerInfo
from kiro_crew.mcp_discovery_server_info import MCP_REDACTED_HEADER_VALUE

_ARGS = ["--token", "sk-live-0123456789", "--port", "9"]


async def _listing(monkeypatch: pytest.MonkeyPatch, via_app: str) -> list[dict[str, Any]]:
    monkeypatch.setattr(
        disc, "list_servers", lambda *a, **k: [McpServerInfo(name="x", command="npx", args=_ARGS)]
    )
    request = _request(_State())
    if via_app:
        request._claims["via_app"] = via_app
    response = await mcp_mod.api_mcp_servers(request)
    return json.loads(response.text)


@pytest.mark.asyncio
async def test_a_delegated_caller_gets_no_launch_argument_value(
    global_file, monkeypatch  # noqa: F811
):
    rows = await _listing(monkeypatch, "tunnel-like")
    assert [r["name"] for r in rows] == ["x"]
    assert rows[0]["args"] == [MCP_REDACTED_HEADER_VALUE] * len(_ARGS)
    assert "sk-live" not in json.dumps(rows)


@pytest.mark.asyncio
async def test_the_owners_own_view_keeps_the_launch_arguments(
    global_file, monkeypatch  # noqa: F811
):
    rows = await _listing(monkeypatch, "")
    assert rows[0]["args"] == _ARGS
