"""KAS projection: a remote MCP entry that carries a placeholder ``command`` stays remote.

The agent schema requires ``command`` on every server, so a remote entry in the
wild carries one, such as ``"http"``. KAS's wire schema has no ``type`` and its
mapper starts any entry with a ``command`` as a local process, so the projection
sends every entry :func:`kiro_crew.mcp_cleanup.mcp_entry_is_remote` reads as
remote without its ``command``. The entry then takes the same path as one with no
``command`` at all: its headers go through the local bridge, and a url KAS refuses
is named as not running.
"""

from __future__ import annotations

import json

import pytest
from kas_agents_helpers import _spec

from kiro_crew import kas_env_shim
from kiro_crew.acp import kas_env_launch
from kiro_crew.acp.kas_agents import NOT_RUN_INSECURE_URL, WithheldOut, to_client_custom_agent

URL = "https://mcp.example.invalid/mcp"
HEADERS = {"Authorization": "Bearer remote-entry-31595"}
SESSION = "dashboard_chat-31595-1"


@pytest.fixture
def home(tmp_path, monkeypatch):
    data_home = tmp_path / "crew-home"
    data_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    return data_home


def _project(
    entry: dict, session_key: str = "", withheld: WithheldOut | None = None
) -> dict[str, dict]:
    spec = _spec(mcpServers={"kirocrew-core": {"command": "x"}, "remote": entry})
    out = to_client_custom_agent("a", spec, "p", session_key=session_key, withheld_out=withheld)
    return out["mcpServers"]


class TestARemoteEntryIsSentWithoutItsPlaceholderCommand:
    @pytest.mark.parametrize("declared_type", ["http", "sse"])
    def test_the_engine_gets_the_url_and_no_command(self, home, declared_type):
        projected = _project({"command": "http", "type": declared_type, "url": URL})["remote"]
        assert "command" not in projected
        assert projected["url"] == URL

    def test_its_headers_go_through_the_local_bridge(self, home):
        entry = {"command": "http", "type": "http", "url": URL, "headers": HEADERS}
        launched = _project(entry, SESSION)["remote"]
        assert "url" not in launched and "headers" not in launched
        assert kas_env_shim.SHIM_MODULE in launched["args"]
        names = kas_env_launch.batch_snapshot_names([{"mcpServers": {"remote": launched}}])
        doc = json.loads((home / "kas-mcp-launch" / names[0]).read_text(encoding="utf-8"))
        assert (doc["url"], doc["headers"]) == (URL, HEADERS)

    def test_withheld_headers_are_reported_for_a_remote_server(self, home):
        withheld: WithheldOut = {}
        _project({"command": "http", "type": "http", "url": URL, "headers": HEADERS}, "", withheld)
        assert withheld == {"remote": ((), ("headers",), False)}

    def test_a_url_the_engine_refuses_is_named_as_not_running(self, home):
        withheld: WithheldOut = {}
        insecure = {"command": "http", "type": "http", "url": "http://mcp.example.invalid/mcp"}
        _project(insecure, "", withheld)
        assert withheld == {"remote": ((), (NOT_RUN_INSECURE_URL,), False)}


class TestAnUntypedEntryWithARealCommandStaysLocal:
    def test_the_command_is_kept_and_nothing_is_bridged(self, home):
        entry = {"command": "mcp-proxy", "args": ["--port", "0"], "url": URL, "headers": HEADERS}
        projected = _project(entry, SESSION)["remote"]
        assert projected["command"] == "mcp-proxy"
        assert projected["args"] == ["--port", "0"]
        assert not (home / "kas-mcp-launch").exists()
