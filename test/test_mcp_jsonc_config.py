"""A commented ``mcp.json`` is read everywhere and never rewritten.

Kiro reads ``~/.kiro/settings/mcp.json`` as JSONC, so a person may comment out a
server there. Every reader must still see the other servers in the file, and
every writer that would emit the file back as plain JSON must refuse instead,
so the comments survive.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.user_json import (
    has_json_comments,
    load_mcp_servers,
    loads_mcp_config,
    loads_user_jsonc,
)

#: The shape from the report: a live server, then a commented-out last server
#: after a trailing comma.
COMMENTED = """{
  "mcpServers": {
    // remote server, the URL holds a "//" that is not a comment
    "live": {"url": "https://example.com/mcp", "note": "a,} b // c", "disabledTools": ["t"]},
    /* parked for now */
    // "parked": {"command": "parked"}
  }
}
"""

LIVE = {"url": "https://example.com/mcp", "note": "a,} b // c", "disabledTools": ["t"]}


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


@pytest.fixture
def commented(tmp_path: Path) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(COMMENTED, encoding="utf-8")
    return path


class TestLoadsUserJsonc:
    def test_comments_and_trailing_commas_parse(self):
        assert loads_user_jsonc(COMMENTED) == {"mcpServers": {"live": LIVE}}

    def test_strict_json_is_unchanged(self):
        assert loads_user_jsonc('\ufeff{"a": [1, 2]}') == {"a": [1, 2]}

    def test_broken_file_still_raises(self):
        with pytest.raises(json.JSONDecodeError):
            loads_user_jsonc('{\n  // ok\n  "a": }\n')
        with pytest.raises(json.JSONDecodeError) as excinfo:
            loads_user_jsonc('{\n  "a": 1,\n  "b": }\n')
        assert excinfo.value.lineno == 3

    def test_has_json_comments(self):
        assert has_json_comments(COMMENTED)
        assert not has_json_comments('{"a": 1}')
        assert not has_json_comments("{ // broken\n")


class TestReadersAgree:
    def test_shared_mcp_parsers(self, commented):
        assert loads_mcp_config(COMMENTED, jsonc=True)["mcpServers"] == {"live": LIVE}
        with pytest.raises(json.JSONDecodeError):
            loads_mcp_config(COMMENTED)
        assert load_mcp_servers(commented) == {"live": LIVE}

    def test_discovery(self, commented, monkeypatch, caplog):
        from kiro_crew import mcp_discovery

        monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (commented,))
        monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", lambda: ())

        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_discovery"):
            by_source = mcp_discovery._load_mcp_json_by_source()

        assert {name for bucket in by_source.values() for name in bucket} == {"live"}
        assert not [r for r in caplog.records if "Failed to load MCP config" in r.message]
        assert mcp_discovery._mcp_names_from_file(commented) == {"live"}

    def test_session_deny_set_reads_disabled_tools(self, commented):
        from kiro_crew.acp import session_mcp

        settings = session_mcp._read_mcp_settings(commented)
        assert settings["mcpServers"]["live"]["disabledTools"] == ["t"]

    def test_a_broken_file_is_still_skipped(self, tmp_path, monkeypatch, caplog):
        from kiro_crew import mcp_discovery

        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text('{"mcpServers": {"a": }', encoding="utf-8")
        monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (mcp_json,))
        monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", lambda: ())

        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_discovery"):
            by_source = mcp_discovery._load_mcp_json_by_source()

        assert not any(by_source.values())
        assert [r for r in caplog.records if "Failed to load MCP config" in r.message]


class TestWritersRefuse:
    def test_scope_writer_raises_and_leaves_the_file(self, commented):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        with (
            patch.object(mcp_mod, "_GLOBAL_MCP_JSON", commented),
            pytest.raises(mcp_mod.McpConfigHasComments) as excinfo,
        ):
            mcp_mod._set_scope_entry(commented, "other", enabled=True, spec={"command": "x"})

        assert excinfo.value.status == 409
        assert json.loads(excinfo.value.text)["code"] == mcp_mod.MCP_CONFIG_HAS_COMMENTS
        assert commented.read_text(encoding="utf-8") == COMMENTED

    def test_store_writer_raises_and_leaves_the_file(self, commented, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "_kirocrew_mcp_json", lambda: commented)
        with pytest.raises(mcp_mod.McpConfigHasComments):
            mcp_mod._atomic_write(commented, {"mcpServers": {}})
        assert commented.read_text(encoding="utf-8") == COMMENTED

    def test_global_writer_raises_and_leaves_the_file(self, commented):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        with patch.object(mcp_mod, "_GLOBAL_MCP_JSON", commented):
            with pytest.raises(mcp_mod.McpConfigHasComments):
                mcp_mod._write_mcp_json({"mcpServers": {}})
        assert commented.read_text(encoding="utf-8") == COMMENTED

    def test_plain_json_is_still_written(self, tmp_path):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        path = tmp_path / "mcp.json"
        path.write_text('{"mcpServers": {}}', encoding="utf-8")
        with patch.object(mcp_mod, "_GLOBAL_MCP_JSON", path):
            mcp_mod._refuse_commented_config(path, tmp_path / "absent.json")
            mcp_mod._write_mcp_json({"mcpServers": {"a": {"command": "a"}}})
        assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"] == {"a": {"command": "a"}}

    def test_cc_sidecar_is_not_rewritten(self, commented):
        from kiro_crew import mcp_discovery

        server = mcp_discovery.McpServerInfo(name="new", command="new-cmd")
        with patch.object(mcp_discovery, "kirocrew_managed_names", return_value=set()):
            assert mcp_discovery.register_servers_for_cc([server], mcp_json_path=commented) is False
        assert commented.read_text(encoding="utf-8") == COMMENTED

    @pytest.mark.asyncio
    async def test_sync_is_refused_before_any_write(self, commented):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        req = MagicMock()
        req.app = {"state": MagicMock()}
        with (
            patch.object(mcp_mod, "_GLOBAL_MCP_JSON", commented),
            patch("kiro_crew.mcp_discovery.sync_discovered_servers") as mock_sync,
            patch.object(mcp_mod, "_write_mcp_json") as mock_write,
        ):
            with pytest.raises(mcp_mod.McpConfigHasComments) as excinfo:
                await mcp_mod.api_mcp_sync(req)

        assert excinfo.value.status == 409
        assert json.loads(excinfo.value.text)["code"] == mcp_mod.MCP_CONFIG_HAS_COMMENTS
        mock_sync.assert_not_called()
        mock_write.assert_not_called()
        assert commented.read_text(encoding="utf-8") == COMMENTED

    def test_rendered_agent_files_are_not_gated(self, tmp_path, monkeypatch):
        """Only hand-edited MCP configs are guarded; a rendered agent file is ours."""
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", list)
        agent = tmp_path / "kirocrew.json"
        agent.write_text(COMMENTED, encoding="utf-8")
        mcp_mod._atomic_write(agent, {"mcpServers": {}})
        assert json.loads(agent.read_text(encoding="utf-8")) == {"mcpServers": {}}
