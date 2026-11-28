"""MCP discovery: the stdio probe declares MCP Apps UI support as a gateway stub does.

A server that lists its UI tools only to a client declaring the
``io.modelcontextprotocol/ui`` extension shows the dashboard's probe the tools a
session behind the gateway receives, and an app-only tool the gateway withholds
from every session is not reported as one of the server's tools. A server a
session launches directly, outside the gateway, is probed as that session sees it.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_cache,
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew.mcp_discovery import McpServerInfo, probe_server

#: The MCP Apps feature gate both the gateway backend and the probe read.
_APPS_GATE = "kiro_crew.mcp_gateway.backend_mcp_apps._mcp_apps_enabled"
#: Whether the running gateway serves the probed server for a session.
_ROUTING = "kiro_crew.mcp_discovery_probe._served_by_gateway"

_UI_GATED_SERVER = (
    "import json, sys\n"
    "ui = False\n"
    "for line in sys.stdin:\n"
    "    request = json.loads(line)\n"
    "    method = request.get('method')\n"
    "    if 'id' not in request:\n"
    "        continue\n"
    "    if method == 'initialize':\n"
    "        ext = request['params'].get('capabilities', {}).get('extensions', {})\n"
    "        entry = ext.get('io.modelcontextprotocol/ui', {})\n"
    "        ui = 'text/html;profile=mcp-app' in entry.get('mimeTypes', [])\n"
    "        result = {}\n"
    "    elif method == 'tools/list':\n"
    "        tools = [\n"
    "            {'name': 'plain'},\n"
    "            {'name': 'app_only', '_meta': {'ui': {'visibility': ['app']}}},\n"
    "        ]\n"
    "        if ui:\n"
    "            tools.append({'name': 'visualize',\n"
    "                          '_meta': {'ui': {'resourceUri': 'ui://x/app.html'}}})\n"
    "        result = {'tools': tools}\n"
    "    else:\n"
    "        continue\n"
    "    reply = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}\n"
    "    print(json.dumps(reply), flush=True)\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "routed,enabled,expected",
    [
        pytest.param(True, True, ["plain", "visualize"], id="routed-apps-on"),
        pytest.param(True, False, ["plain"], id="routed-apps-off"),
        # A session launches an unstubbed server directly, declaring nothing and
        # filtering nothing, so the probe reports what that session receives.
        pytest.param(False, True, ["plain", "app_only"], id="direct-apps-on"),
    ],
)
async def test_stdio_probe_lists_the_tools_a_stubbed_session_receives(
    tmp_path, monkeypatch, routed: bool, enabled: bool, expected: list[str]
) -> None:
    # The probe's child inherits this process's working directory.
    monkeypatch.chdir(tmp_path)
    stub = tmp_path / "ui-gated-server.py"
    stub.write_text(_UI_GATED_SERVER, encoding="utf-8")
    server = McpServerInfo(name="ui-gated", command=sys.executable, args=[str(stub)])
    with (
        patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls,
        patch(_APPS_GATE, return_value=enabled),
        patch(_ROUTING, return_value=routed),
    ):
        mock_cfg = MagicMock()
        mock_cfg.dashboard.mcp_probe_timeout_secs = 5
        mock_cls.load.return_value = mock_cfg

        result = await probe_server(server)

    assert result.status == "ok", f"unexpected error: {result.error}"
    assert result.tools == expected
