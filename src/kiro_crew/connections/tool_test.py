# testpaths-ok: production module implementing the authenticated Connections Test action.
"""Authenticated tool enumeration for the Connections Test action.

The ordinary MCP probe is intentionally tokenless, so a healthy OAuth provider
returns a challenge and cannot prove that any tool is usable. This module starts
one promptless kiro-cli ACP session under the real Kiro Crew agent, then reads
kiro-cli's native ``/mcp`` and ``/tools`` structured results. Kiro-cli owns the
bearer, performs the provider ``tools/list``, and applies the agent's final tool
exposure rules; Kiro Crew receives only bounded status and counts.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, TypedDict

from kiro_crew.agent_sdk import run_kiro_native_commands
from kiro_crew.config.loader import data_home
from kiro_crew.connections.registry import Provider
from kiro_crew.mcp_utils import mcp_server_alias

_SCHEMA_VERSION = 1
_TEST_TIMEOUT_SECONDS = 100.0
_MAIN_AGENT = "kirocrew"

Verdict = Literal["usable", "no_tools", "failed"]


class ConnectionTestResult(TypedDict):
    schema_version: int
    slug: str
    verdict: Verdict
    code: str
    toolCount: int


def _result(slug: str, verdict: Verdict, code: str, tool_count: int = 0) -> ConnectionTestResult:
    return {
        "schema_version": _SCHEMA_VERSION,
        "slug": slug,
        "verdict": verdict,
        "code": code,
        "toolCount": tool_count,
    }


def _data(result: object) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    return data if isinstance(data, dict) else None


def _server_row(result: object, alias: str) -> dict[str, Any] | None:
    data = _data(result)
    servers = data.get("servers") if data is not None else None
    if not isinstance(servers, list):
        return None
    for row in servers:
        if isinstance(row, dict) and row.get("name") == alias:
            return row
    return {}


def _exposed_tool_count(result: object, alias: str) -> int | None:
    data = _data(result)
    tools = data.get("tools") if data is not None else None
    if not isinstance(tools, list):
        return None
    source = f"mcp:{alias}"
    names = {
        row.get("name")
        for row in tools
        if isinstance(row, dict)
        and row.get("source") == source
        and isinstance(row.get("name"), str)
        and row.get("name")
    }
    return len(names)


def _classify(results: tuple[dict[str, Any], ...], slug: str, alias: str) -> ConnectionTestResult:
    if len(results) != 2:
        return _result(slug, "failed", "invalid_tool_inventory")
    mcp_result, tools_result = results
    server = _server_row(mcp_result, alias)
    if server is None:
        return _result(slug, "failed", "invalid_tool_inventory")
    if not server:
        return _result(slug, "failed", "mcp_server_not_loaded")

    status = server.get("status")
    if status != "running":
        known = status if status in {"loading", "failed", "disabled"} else "unavailable"
        return _result(slug, "failed", f"mcp_server_{known}")

    listed = server.get("toolCount")
    if isinstance(listed, bool) or not isinstance(listed, int) or listed < 0:
        return _result(slug, "failed", "invalid_tool_inventory")
    if listed == 0:
        return _result(slug, "no_tools", "no_tools_exposed")

    exposed = _exposed_tool_count(tools_result, alias)
    if exposed is None:
        return _result(slug, "failed", "invalid_tool_inventory")
    if exposed == 0:
        return _result(slug, "no_tools", "no_tools_exposed")
    return _result(slug, "usable", "tools_available", exposed)


async def test_connection_tools(provider: Provider) -> ConnectionTestResult:
    """Return the tri-level authenticated tool verdict for one provider.

    No prompt is sent, so this performs no model call and cannot invoke a
    provider tool. ``/mcp`` reads the MCP manager's post-``tools/list`` status;
    ``/tools`` reads the final agent-exposed set. Raw command output and tool
    descriptions never cross the API boundary.
    """
    slug = str(provider["slug"])
    alias = mcp_server_alias(slug)
    try:
        work_root = await asyncio.to_thread(data_home)
        batch = await run_kiro_native_commands(
            ("/mcp", "/tools"),
            work_dir=work_root / "connections" / "test",
            agent=_MAIN_AGENT,
            session_key=f"connections-test-{slug}",
            timeout_seconds=_TEST_TIMEOUT_SECONDS,
        )
    except Exception:
        return _result(slug, "failed", "connection_test_failed")
    if not batch.ok:
        return _result(slug, "failed", batch.code)
    return _classify(batch.results, slug, alias)
