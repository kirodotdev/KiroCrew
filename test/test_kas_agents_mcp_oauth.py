"""A remote MCP server's ``oauth`` block on the KAS projection.

The projection always sends the block, narrowed to the keys the engine accepts and
never with its client secret: KAS 0.66.104 reads it, and older engines drop it. The
session-start notice names the server and says sign-in will not work here only when
Crew knows the engine is the older kind.
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
            "redirectUri": "http://localhost:8080/callback",
            "oauthScopes": ["channels:history"],
        },
    }
    entry.update(extra)
    return _spec(mcpServers={"slack-example": entry})


class TestProjection:
    def test_a_public_client_block_is_sent(self):
        out = to_client_custom_agent("a", _remote(), "p")
        entry = out["mcpServers"]["slack-example"]
        assert entry["url"] == "https://mcp.example.com/mcp"
        assert entry["oauth"] == {
            "clientId": _CLIENT_ID,
            "redirectUri": "http://localhost:8080/callback",
        }
        assert entry["oauthScopes"] == ["channels:history"]

    def test_a_confidential_client_block_is_withheld_and_never_logged(self, caplog):
        spec = _remote()
        spec["mcpServers"]["slack-example"]["oauth"]["clientSecret"] = _SECRET
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_agents"):
            out = to_client_custom_agent("a", spec, "p")
        assert "oauth" not in out["mcpServers"]["slack-example"]
        assert _SECRET not in repr(out)
        assert "names a clientSecret" in caplog.text
        assert _SECRET not in caplog.text and _CLIENT_ID not in caplog.text

    def test_an_unknown_key_is_not_sent(self):
        spec = _spec(mcpServers={"s": {"url": "https://x.example", "oauth": {"tok3n": _SECRET}}})
        out = to_client_custom_agent("a", spec, "p")
        assert "oauth" not in out["mcpServers"]["s"]
        assert _SECRET not in repr(out)

    def test_a_non_object_block_is_not_sent(self, caplog):
        spec = _spec(mcpServers={"s": {"url": "https://mcp.example.com", "oauth": _SECRET}})
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_agents"):
            out = to_client_custom_agent("a", spec, "p")
        assert "oauth" not in out["mcpServers"]["s"]
        assert _SECRET not in repr(out)
        assert _SECRET not in caplog.text


class TestNotice:
    def test_kas_reports_the_server_oauth_block(self):
        assert spec_keys_without_carrier(_remote(), discards_mcp_oauth=True) == [
            "mcpServers.slack-example.oauth"
        ]

    def test_a_relay_session_names_only_a_confidential_client(self):
        """Its block is withheld on every engine; a public client's is sent."""
        spec = _remote()
        spec["mcpServers"]["confidential"] = {
            "url": "https://mcp2.example.com/mcp",
            "oauth": {"clientId": _CLIENT_ID, "clientSecret": _SECRET},
        }
        lost = spec_keys_without_carrier(
            spec, discards_mcp_oauth=False, withholds_oauth_secret=True
        )
        assert lost == ["mcpServers.confidential.oauth"]

    def test_a_relay_session_names_every_block_that_sends_nothing(self):
        """A metadata-url-only block keeps no key Crew sends, like a secret block."""
        spec = _remote()
        spec["mcpServers"]["metadata-only"] = {
            "url": "https://mcp3.example.com/mcp",
            "oauth": {"clientMetadataUrl": "https://app.example/client.json"},
        }
        spec["mcpServers"]["confidential"] = {
            "url": "https://mcp2.example.com/mcp",
            "oauth": {"clientId": _CLIENT_ID, "clientSecret": _SECRET},
        }
        spec["mcpServers"]["scopes-only"] = {
            "url": "https://mcp4.example.com/mcp",
            "oauth": {"oauthScopes": ["read"]},
        }
        lost = spec_keys_without_carrier(
            spec, discards_mcp_oauth=False, withholds_oauth_secret=True
        )
        assert lost == ["mcpServers.confidential.oauth", "mcpServers.metadata-only.oauth"]

    def test_a_muted_confidential_client_is_not_reported(self):
        spec = _remote(disabled=True)
        spec["mcpServers"]["slack-example"]["oauth"]["clientSecret"] = _SECRET
        assert spec_keys_without_carrier(spec, withholds_oauth_secret=True) == []

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

    def test_the_notice_names_the_server_and_offers_no_harness_switch(self):
        # A Kiro CLI pick runs on KAS where kiro-cli offers v3, so naming it as the fix misleads.
        text = _spec_keys_notice("a", ["mcpServers.slack-example.oauth"])
        assert "mcpServers.slack-example.oauth" in text
        assert "Sign-in to MCP server slack-example will not work on this backend yet." in text
        assert "kiro-cli" not in text

    def test_a_dotted_name_and_several_servers_read_back_whole(self):
        text = _spec_keys_notice("a", ["mcpServers.a.b.oauth", "mcpServers.oauth.oauth"])
        assert "Sign-in to MCP servers a.b, oauth will not work" in text

    def test_a_bare_prefix_and_suffix_names_no_server(self):
        assert "Sign-in" not in _spec_keys_notice("a", ["mcpServers.oauth"])

    def test_the_notice_for_other_keys_is_unchanged(self):
        text = _spec_keys_notice("a", ["slashCommand"])
        assert "Sign-in" not in text
