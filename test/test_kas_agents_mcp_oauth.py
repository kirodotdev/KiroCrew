"""A remote MCP server's ``oauth`` block on the KAS projection.

KAS's ``mapClientMcpServers`` rebuilds each remote entry from ``url``/``headers``/
``env``/``timeout`` only, so a pre-registered client's ``oauth`` block never reaches
the backend and its sign-in cannot start. The projection withholds the block (it can
hold a client secret), logs its key names only, and the session-start notice names
the server and says sign-in will not work here.
"""

from __future__ import annotations

import pytest
from kas_agents_helpers import _spec

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_agents import spec_keys_without_carrier, to_client_custom_agent
from kiro_crew.dashboard.chat_turn.tool_approval import _spec_keys_notice

_SECRET = "placeholder-client-secret-value"
_CLIENT_ID = "placeholder-client-id"


@pytest.fixture(autouse=True)
def _not_registry_governed(_floor_monkeypatch):
    """Off unless a test says otherwise, so the host's config cannot decide."""
    _floor_monkeypatch.setattr(kas_agents, "_registry_governed", lambda: False)


def _remote(**extra):
    entry = {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {
            "clientId": _CLIENT_ID,
            "clientSecret": _SECRET,
            "redirectUri": "http://localhost:8080/callback",
            "oauthScopes": ["channels:history"],
        },
    }
    entry.update(extra)
    return _spec(mcpServers={"slack-example": entry})


class TestProjection:
    def test_the_oauth_block_is_not_sent(self):
        out = to_client_custom_agent("a", _remote(), "p")
        assert out["mcpServers"]["slack-example"]["url"] == "https://mcp.example.com/mcp"
        assert "oauth" not in out["mcpServers"]["slack-example"]

    def test_the_log_names_keys_and_never_values(self, caplog):
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent("a", _remote(), "p")
        assert "slack-example" in caplog.text
        assert "oauth block (4 keys)" in caplog.text
        for key in ("clientId", "clientSecret", "redirectUri", "oauthScopes"):
            assert key not in caplog.text
        assert "kiro-cli" in caplog.text
        assert _SECRET not in caplog.text
        assert _CLIENT_ID not in caplog.text

    def test_any_key_is_counted_not_named(self, caplog):
        spec = _spec(mcpServers={"s": {"url": "https://x.example", "oauth": {"tok3n": _SECRET}}})
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent("a", spec, "p")
        assert "oauth block (1 keys)" in caplog.text
        assert "tok3n" not in caplog.text and _SECRET not in caplog.text

    def test_a_non_object_block_is_withheld_and_named_without_its_value(self, caplog):
        spec = _spec(mcpServers={"s": {"url": "https://mcp.example.com", "oauth": _SECRET}})
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_agents"):
            out = to_client_custom_agent("a", spec, "p")
        assert "oauth" not in out["mcpServers"]["s"]
        assert "oauth block (0 keys)" in caplog.text
        assert _SECRET not in caplog.text


class TestNotice:
    def test_kas_reports_the_server_oauth_block(self):
        assert spec_keys_without_carrier(_remote(), discards_mcp_oauth=True) == [
            "mcpServers.slack-example.oauth"
        ]

    def test_a_muted_server_is_not_reported(self):
        spec = _remote(disabled=True)
        assert spec_keys_without_carrier(spec, discards_mcp_oauth=True) == []

    def test_a_backend_that_keeps_oauth_reports_nothing(self):
        """kiro-cli reads the block off disk; only KAS drops it."""
        assert spec_keys_without_carrier(_remote()) == []
        assert spec_keys_without_carrier(_remote(), translates_tools_settings=True) == []

    def test_a_registry_withheld_server_is_not_reported(self, monkeypatch):
        """A server never declared does not run, so its sign-in is not the loss."""
        monkeypatch.setattr(kas_agents, "_registry_governed", lambda: True)
        assert spec_keys_without_carrier(_remote(), discards_mcp_oauth=True) == []

    def test_a_registry_marked_server_is_not_reported(self):
        spec = _remote(type="registry")
        assert spec_keys_without_carrier(spec, discards_mcp_oauth=True) == []

    def test_no_oauth_block_reports_nothing(self):
        spec = _spec(mcpServers={"s": {"url": "https://mcp.example.com"}})
        assert spec_keys_without_carrier(spec, discards_mcp_oauth=True) == []

    def test_the_notice_names_the_server_and_the_supported_backend(self):
        text = _spec_keys_notice("a", ["mcpServers.slack-example.oauth"])
        assert "mcpServers.slack-example.oauth" in text
        assert "Sign-in to MCP server slack-example will not work on this backend" in text
        assert "kiro-cli backend supports it" in text

    def test_a_dotted_name_and_several_servers_read_back_whole(self):
        text = _spec_keys_notice("a", ["mcpServers.a.b.oauth", "mcpServers.oauth.oauth"])
        assert "Sign-in to MCP servers a.b, oauth will not work" in text

    def test_a_bare_prefix_and_suffix_names_no_server(self):
        assert "Sign-in" not in _spec_keys_notice("a", ["mcpServers.oauth"])

    def test_the_notice_for_other_keys_is_unchanged(self):
        text = _spec_keys_notice("a", ["slashCommand"])
        assert "Sign-in" not in text
