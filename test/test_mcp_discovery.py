"""Tests for MCP discovery module."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.mcp_discovery import (
    SCOPE_CC_GLOBAL,
    SCOPE_KIRO_GLOBAL,
    SCOPE_KIROCREW,
    McpServerInfo,
    _cache_probe,
    _get_cached,
    _load_mcp_json_by_source,
    _note_denied_env,
    _probe_cache,
    _probe_remote,
    _read_jsonrpc_response,
    _read_stdio_jsonrpc_response,
    _scope_priority,
    discover_servers_to_sync,
    list_servers,
    probe_server,
    sync_to_agent_config,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT


def _clear_cache() -> None:
    _probe_cache.clear()


@pytest.fixture(autouse=True)
def _passthrough_sandbox(monkeypatch):
    """``probe_server`` routes the spawned MCP binary through
    ``sandboxed_spawn_argv`` → ``wrap_argv``, which raises when no OS-level
    sandbox backend is available (e.g. macOS without sandbox-exec). These tests
    exercise the probe's protocol handling and process cleanup, not sandbox
    availability, so run the command unwrapped in-test (preserving the caller's
    ``env=`` kwarg / falling back to os.environ)."""
    import os as _os

    def _passthrough(argv, *a, env=None, **k):
        return list(argv), dict(env if env is not None else _os.environ), None

    monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _passthrough)


class TestMcpServerInfo:
    def test_to_dict(self) -> None:
        info = McpServerInfo(
            name="test-mcp",
            command="/usr/bin/test",
            args=["--foo"],
            status="ok",
            tools=["tool_a", "tool_b"],
            source="agent",
        )
        d = info.to_dict()
        assert d["name"] == "test-mcp"
        assert d["command"] == "/usr/bin/test"
        assert d["args"] == ["--foo"]
        assert d["status"] == "ok"
        assert d["tools"] == ["tool_a", "tool_b"]
        assert d["source"] == "agent"
        assert "url" not in d

    def test_defaults(self) -> None:
        info = McpServerInfo(name="x")
        assert info.command == ""
        assert info.args is None
        assert info.env == {}
        assert info.url == ""
        assert info.headers == {}
        assert info.status == "unknown"
        assert info.tools == []
        assert info.error == ""
        assert info.source == "agent"

    def test_remote_server_fields_redact_header_values(self) -> None:
        info = McpServerInfo(
            name="deepwiki",
            url="https://mcp.deepwiki.com/mcp",
            headers={"Authorization": "Bearer tok"},
        )
        assert info.is_remote is True
        assert info.command == ""
        d = info.to_dict()
        assert d["url"] == "https://mcp.deepwiki.com/mcp"
        assert d["headers"] == {"Authorization": "[REDACTED: credential]"}
        assert "Bearer tok" not in json.dumps(d)

    def test_is_remote_false_for_local(self) -> None:
        info = McpServerInfo(name="x", command="cmd")
        assert info.is_remote is False

    def test_is_remote_false_when_both(self) -> None:
        """If both url and command are set, treat as local (command takes precedence)."""
        info = McpServerInfo(name="x", command="cmd", url="http://localhost")
        assert info.is_remote is False

    def test_remote_oauth_hints_surface_unredacted(self) -> None:
        info = McpServerInfo(
            name="github",
            url="https://api.githubcopilot.com/mcp/",
            scopes=["read:user", "read:org"],
            client_id="Iv1.public-identifier",
        )
        d = info.to_dict()
        assert d["scopes"] == ["read:user", "read:org"]
        assert d["clientId"] == "Iv1.public-identifier"

    def test_oauth_hints_default_empty_and_are_omitted(self) -> None:
        info = McpServerInfo(name="x", url="https://mcp.example.com")
        assert info.scopes == []
        assert info.client_id == ""
        d = info.to_dict()
        assert "scopes" not in d
        assert "clientId" not in d

    def test_oauth_hints_omitted_on_stdio_rows(self) -> None:
        """to_dict gates them behind url, so a stdio row never advertises them."""
        info = McpServerInfo(
            name="x", command="cmd", scopes=["read"], client_id="public-id"
        )
        d = info.to_dict()
        assert "scopes" not in d
        assert "clientId" not in d


class TestListServers:
    def setup_method(self) -> None:
        _clear_cache()

    def test_list_merges_installed_config(self, tmp_path, monkeypatch) -> None:
        """defaults.json has no mcpServers; installed kirocrew.json does."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        installed = {"mcpServers": {"kirocrew-cron": {"command": "kirocrew", "args": ["mcp-cron"]}}}
        (kiro_dir / "kirocrew.json").write_text(json.dumps(installed))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        # The installed-config branch resolves ``kiro_agents_dir()``, which reads
        # ``Path.home`` in ``config.paths`` -- NOT the name patched above. Point the
        # binding this module holds at the tmp tree instead, or the assertion below is
        # answered by whatever the operator's own ~/.kiro/agents happens to contain.
        monkeypatch.setattr("kiro_crew.mcp_discovery.kiro_agents_dir", lambda: kiro_dir)
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (tmp_path / "nope.json",))
        servers = list_servers()
        names = {s.name for s in servers}
        assert "kirocrew-cron" in names

    def test_list_from_agent_config(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {
            "mcpServers": {
                "my-server": {"command": "/usr/bin/srv", "args": ["run"]},
                "other-srv": {"command": "other"},
            }
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        servers = list_servers()
        names = {s.name for s in servers}
        assert "my-server" in names
        assert "other-srv" in names

    def test_list_empty_no_config(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (tmp_path / "nope.json",))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        servers = list_servers()
        assert servers == []

    def test_mcp_json_servers_merged(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"agent-srv": {"command": "a"}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"ext-srv": {"command": "b", "args": ["--x"]}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        servers = list_servers()
        names = {s.name for s in servers}
        assert "agent-srv" in names
        assert "ext-srv" in names
        ext = [s for s in servers if s.name == "ext-srv"][0]
        assert ext.source == "mcp.json"

    def test_mcp_json_no_duplicate(self, tmp_path, monkeypatch) -> None:
        """mcp.json server with same name as agent config is NOT duplicated."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"shared": {"command": "agent-cmd"}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"shared": {"command": "mcp-cmd"}}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        servers = list_servers()
        shared = [s for s in servers if s.name == "shared"]
        assert len(shared) == 1
        assert shared[0].command == "agent-cmd"

    def test_list_canonicalizes_slash_key_and_alias(self, tmp_path, monkeypatch) -> None:
        """A server present under both its raw slash key and its slash-free alias
        is reported once, under the canonical alias. Slash-free names unaffected."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {
            "mcpServers": {
                "npm:@playwright/mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy"],
                },
                "playwright-mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy"],
                },
                "plain-srv": {"command": "p"},
            }
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (tmp_path / "nope.json",))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        servers = list_servers()
        names = [s.name for s in servers]
        assert names.count("playwright-mcp") == 1
        assert "npm:@playwright/mcp" not in names
        assert "plain-srv" in names

    def test_list_skips_disabled_servers(self, tmp_path, monkeypatch) -> None:
        """Servers with disabled=true are excluded from listing."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {
            "mcpServers": {
                "enabled-srv": {"command": "a"},
                "disabled-srv": {"command": "b", "disabled": True},
            }
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (tmp_path / "x",))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        servers = list_servers()
        names = {s.name for s in servers}
        assert "enabled-srv" in names
        assert "disabled-srv" not in names

    def test_list_surfaces_kirocrew_disabled_servers_as_disabled_rows(
        self, tmp_path, monkeypatch
    ) -> None:
        """KiroCrew-scope disabled entries get a row marked disabled.

        Consent-disabled installs/custom adds land with ``disabled: true``
        in the KiroCrew scope; the table's enable action is the consent
        step, so the row must exist (previously these were invisible)."""
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "active": {"command": "a"},
                        "inactive": {"command": "b", "disabled": True},
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        servers = list_servers()
        by_name = {s.name: s for s in servers}
        assert "active" in by_name
        assert by_name["active"].disabled is False
        # The disabled entry is present but flagged — and never probed.
        assert "inactive" in by_name
        assert by_name["inactive"].disabled is True
        assert by_name["inactive"].presence["kirocrew"] is False

    def test_kirocrew_disabled_row_survives_agent_mirror(self, tmp_path, monkeypatch) -> None:
        """The row still surfaces when config sync mirrored the disable.

        Custom-add/install config sync writes the consent-disabled entry
        into the agent file as ``disabled: true`` too. That mirror is the
        SAME signal, not an independent user override — without this the
        freshly added server is invisible (live bug: weather-tools)."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(
            json.dumps({"mcpServers": {"pending": {"command": "a", "disabled": True}}})
        )
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"pending": {"command": "a", "disabled": True}}})
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        servers = [s for s in list_servers() if s.name == "pending"]
        assert len(servers) == 1
        assert servers[0].disabled is True

    @pytest.mark.asyncio
    async def test_probe_all_never_probes_disabled_rows(self, tmp_path, monkeypatch) -> None:
        """Consent-disabled rows are excluded from probing — a probe would
        spawn the server process the user has not yet consented to run."""
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"pending": {"command": "definitely-not-run", "disabled": True}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        probed: list[str] = []

        async def fake_probe(server):
            probed.append(server.name)
            return server

        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", fake_probe)
        from kiro_crew.mcp_discovery import probe_all

        await probe_all()
        assert "pending" not in probed

    def test_disabled_in_agent_blocks_mcp_json(self, tmp_path, monkeypatch) -> None:
        """Server disabled in agent config is not re-added from mcp.json."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a", "disabled": True}}})
        )
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"srv": {"command": "b"}}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        assert not any(s.name == "srv" for s in list_servers())

    def test_dashboard_disabled_server_keeps_a_disabled_row(self, tmp_path, monkeypatch) -> None:
        """``/api/mcp/toggle`` off writes ``disabled: true`` into the Kiro global
        AND onto the agent entry (the agent-side marker is what stops a running
        kiro-cli session's server). The row must survive that, or the user has
        nothing to switch back on — and it carries the agent entry's full spec,
        because the global copy can be the bare stub the toggle creates."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(
            json.dumps(
                {"mcpServers": {"srv": {"command": "real-cmd", "args": ["-x"], "disabled": True}}}
            )
        )
        store = tmp_path / "store.json"
        store.write_text(json.dumps({"mcpServers": {}}))
        kiro_mcp = tmp_path / "kiro.json"
        kiro_mcp.write_text(json.dumps({"mcpServers": {"srv": {"disabled": True}}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES",
            ((store, SCOPE_KIROCREW), (kiro_mcp, SCOPE_KIRO_GLOBAL)),
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (store, kiro_mcp))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        rows = [s for s in list_servers() if s.name == "srv"]
        assert len(rows) == 1
        assert rows[0].disabled is True
        assert rows[0].source == "agent"
        assert rows[0].command == "real-cmd"
        assert rows[0].args == ["-x"]

    def test_disabled_mcp_json_still_carries_disabled_tools(self, tmp_path, monkeypatch) -> None:
        """disabledTools from a disabled mcp.json entry are applied to an existing agent server."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a"}}})
        )
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"disabled": True, "disabledTools": ["t1"]}}})
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        servers = list_servers()
        assert len(servers) == 1
        assert servers[0].disabled_tools == ["t1"]

    def test_list_remote_server(self, tmp_path, monkeypatch) -> None:
        """Remote (url-based) servers are listed with url and headers."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {
            "mcpServers": {
                "deepwiki": {
                    "url": "https://mcp.deepwiki.com/mcp",
                    "headers": {"X-Key": "val"},
                }
            }
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (tmp_path / "x",))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        servers = list_servers()
        assert len(servers) == 1
        s = servers[0]
        assert s.name == "deepwiki"
        assert s.url == "https://mcp.deepwiki.com/mcp"
        assert s.headers == {"X-Key": "val"}
        assert s.command == ""
        assert s.is_remote is True

    def test_mcp_json_merges_multiple_files(self, tmp_path, monkeypatch) -> None:
        """Both mcp.json files are read and merged; first path wins on conflict."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        _clear_cache()

        kiro_mcp = tmp_path / "kiro_mcp.json"
        kiro_mcp.write_text(
            json.dumps(
                {"mcpServers": {"shared": {"command": "kiro"}, "kiro-only": {"command": "k"}}}
            )
        )
        kirocrew_mcp = tmp_path / "kirocrew_mcp.json"
        kirocrew_mcp.write_text(
            json.dumps(
                {"mcpServers": {"shared": {"command": "kirocrew"}, "mc-only": {"command": "m"}}}
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (kiro_mcp, kirocrew_mcp))

        servers = list_servers()
        names = {s.name for s in servers}
        assert "kiro-only" in names
        assert "mc-only" in names
        assert "shared" in names
        shared = [s for s in servers if s.name == "shared"][0]
        assert shared.command == "kiro"  # first path wins

    def test_mcp_json_malformed_file_skipped(self, tmp_path, monkeypatch) -> None:
        """A malformed mcp.json is skipped; valid file still loads."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        _clear_cache()

        bad = tmp_path / "bad.json"
        bad.write_text("{invalid json")
        good = tmp_path / "good.json"
        good.write_text(json.dumps({"mcpServers": {"srv": {"command": "x"}}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (bad, good))

        servers = list_servers()
        assert any(s.name == "srv" for s in servers)

    def test_mcp_json_non_dict_servers_skipped(self, tmp_path, monkeypatch) -> None:
        """Non-dict mcpServers value is skipped; other file still loads."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        _clear_cache()

        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"mcpServers": ["not", "a", "dict"]}))
        good = tmp_path / "good.json"
        good.write_text(json.dumps({"mcpServers": {"srv": {"command": "x"}}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (bad, good))

        servers = list_servers()
        assert any(s.name == "srv" for s in servers)

    def test_mcp_json_permission_error_skipped(self, tmp_path, monkeypatch) -> None:
        """PermissionError from safe_read_file is caught; other file loads."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        _clear_cache()

        blocked = tmp_path / "blocked.json"
        blocked.write_text("{}")
        good = tmp_path / "good.json"
        good.write_text(json.dumps({"mcpServers": {"srv": {"command": "x"}}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (blocked, good))

        original = __import__("kiro_crew.hooks", fromlist=["safe_read_file"]).safe_read_file

        def _mock_safe_read(path: str) -> str:
            if "blocked" in path:
                raise PermissionError("Blocked: sensitive path")
            return original(path)

        monkeypatch.setattr("kiro_crew.mcp_discovery.safe_read_file", _mock_safe_read)

        servers = list_servers()
        assert any(s.name == "srv" for s in servers)


class TestExtraScopeSeam:
    """Discovery sources provider scopes from the extra_mcp_scopes() CPP seam
    instead of hardcoding ~/.claude.json, keeping discovery symmetric with the
    apply/uninstall path (no un-uninstallable "zombie" servers)."""

    def test_oss_default_scans_kiro_only(self, tmp_path, monkeypatch) -> None:
        """With no companion (seam returns []), a provider global is NOT scanned."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        _clear_cache()

        cc = tmp_path / "cc.json"
        cc.write_text(json.dumps({"mcpServers": {"companion-srv": {"command": "x"}}}))
        # OSS default: seam contributes nothing.
        monkeypatch.setattr("kiro_crew.mcp_discovery._extra_scope_sources", lambda: [])

        by_source = _load_mcp_json_by_source()
        assert by_source.get("ccGlobal") == {}
        assert "companion-srv" not in {s.name for s in list_servers()}

    def test_companion_scope_is_scanned(self, tmp_path, monkeypatch) -> None:
        """A seam-contributed scope is scanned and its server surfaces w/ presence."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        _clear_cache()

        cc = tmp_path / "cc.json"
        cc.write_text(json.dumps({"mcpServers": {"companion-srv": {"command": "x"}}}))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._extra_scope_sources", lambda: [(cc, "ccGlobal")]
        )

        by_source = _load_mcp_json_by_source()
        assert "companion-srv" in by_source.get("ccGlobal", {})

        server = next(s for s in list_servers() if s.name == "companion-srv")
        assert server.presence["ccGlobal"] is True

    def test_non_cc_scope_reported_in_presence(self, tmp_path, monkeypatch) -> None:
        """A seam scope whose id is NOT 'cc' (e.g. vendorGlobal) must appear in
        every server's presence. If it were omitted, the frontend reads the
        absent key as False and an unrelated apply would DELETE the server from
        the vendor's global config (GPT 5.6 HIGH data-loss finding)."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        _clear_cache()

        vendor = tmp_path / "vendor.json"
        vendor.write_text(json.dumps({"mcpServers": {"vendor-srv": {"command": "x"}}}))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._extra_scope_sources",
            lambda: [(vendor, "vendorGlobal")],
        )

        server = next(s for s in list_servers() if s.name == "vendor-srv")
        # The scope key is present (not omitted) and correctly True here.
        assert "vendorGlobal" in server.presence
        assert server.presence["vendorGlobal"] is True
        # A server that is NOT in the vendor scope still reports the key as
        # False (present, explicit) rather than omitting it.
        (agent_dir / "defaults.json").write_text(
            json.dumps({"mcpServers": {"other-srv": {"command": "y"}}})
        )
        _clear_cache()
        other = next(s for s in list_servers() if s.name == "other-srv")
        assert other.presence.get("vendorGlobal") is False

    def test_seam_scope_ranks_below_kiro_global(self, tmp_path, monkeypatch) -> None:
        """A seam-contributed scope (e.g. ccGlobal) must rank BELOW the Kiro
        global in discovery's merge — matching rebuild_agent_config, which
        treats provider globals as lowest-priority gap-fillers. Otherwise the
        dashboard would show/probe a spec the agent never runs. Guards the
        _CORE_SCOPE_ORDER fix (ccGlobal dropped from the core tuple)."""
        # _scope_priority orders core scopes first, seam scopes in the tail.
        by_source = {
            SCOPE_KIROCREW: {},
            SCOPE_KIRO_GLOBAL: {"shared-srv": {"command": "kiro"}},
            SCOPE_CC_GLOBAL: {"shared-srv": {"command": "cc"}},
        }
        order = _scope_priority(by_source)
        assert order.index(SCOPE_KIRO_GLOBAL) < order.index(SCOPE_CC_GLOBAL), (
            "Kiro global must outrank the seam ccGlobal scope (rebuild parity)"
        )

        # Functional: same server in Kiro global + seam ccGlobal with different
        # disabledTools → first-scope-wins gives the Kiro-global value.
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        _clear_cache()

        kiro_mcp = tmp_path / "kiro.json"
        kiro_mcp.write_text(
            json.dumps(
                {"mcpServers": {"shared-srv": {"command": "x", "disabledTools": ["kiro-tool"]}}}
            )
        )
        cc_mcp = tmp_path / "cc.json"
        cc_mcp.write_text(
            json.dumps(
                {"mcpServers": {"shared-srv": {"command": "x", "disabledTools": ["cc-tool"]}}}
            )
        )
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES", ((kiro_mcp, SCOPE_KIRO_GLOBAL),)
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (kiro_mcp,))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._extra_scope_sources", lambda: [(cc_mcp, "ccGlobal")]
        )

        server = next(s for s in list_servers() if s.name == "shared-srv")
        assert server.disabled_tools == ["kiro-tool"], (
            "Kiro-global disabledTools must win over the seam scope (first-scope-wins)"
        )


class TestDiscoverNew:
    def test_discover_new(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"existing": {"command": "a"}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "existing": {"command": "a"},
                        "brand-new": {"command": "b"},
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        new = discover_servers_to_sync()
        assert len(new) == 1
        assert new[0].name == "brand-new"
        assert new[0].source == "discovered"

    def test_discover_new_remote_preserves_url_and_headers(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        headers = {"Authorization": "Bearer sync-secret", "X-Tenant": "acme"}
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "https://mcp.example.com/v1",
                            "headers": headers,
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].name == "remote"
        assert result[0].is_remote is True
        assert result[0].command == ""
        assert result[0].url == "https://mcp.example.com/v1"
        assert result[0].headers == headers

    def test_discover_flags_existing_remote_url_change(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        headers = {"Authorization": "Bearer sync-secret"}
        cfg = {
            "mcpServers": {
                "remote": {"url": "https://mcp.example.com/v1", "headers": headers}
            }
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "https://mcp.example.com/v2",
                            "headers": headers,
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].url == "https://mcp.example.com/v2"

    def test_discover_flags_existing_remote_headers_change(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {
            "mcpServers": {
                "remote": {
                    "url": "https://mcp.example.com/v1",
                    "headers": {"Authorization": "Bearer old"},
                }
            }
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "https://mcp.example.com/v1",
                            "headers": {"Authorization": "Bearer new"},
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].headers == {"Authorization": "Bearer new"}

    def test_discover_new_remote_preserves_scopes_and_client_id(
        self, tmp_path, monkeypatch
    ) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "https://mcp.example.com/v1",
                            "scopes": ["read:user", "read:org"],
                            "clientId": "public-client-id",
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].scopes == ["read:user", "read:org"]
        assert result[0].client_id == "public-client-id"

    def test_discover_flags_existing_remote_scopes_change(self, tmp_path, monkeypatch) -> None:
        """A Connect that widens or narrows scopes must re-sync, not be ignored."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {
            "mcpServers": {
                "remote": {"url": "https://mcp.example.com/v1", "scopes": ["read"]}
            }
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "https://mcp.example.com/v1",
                            "scopes": ["read", "write"],
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].scopes == ["read", "write"]

    def test_discover_flags_existing_remote_client_id_change(
        self, tmp_path, monkeypatch
    ) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {
            "mcpServers": {
                "remote": {"url": "https://mcp.example.com/v1", "clientId": "old-id"}
            }
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {"url": "https://mcp.example.com/v1", "clientId": "new-id"}
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].client_id == "new-id"

    def test_discover_no_resync_when_oauth_hints_match(self, tmp_path, monkeypatch) -> None:
        """Equal hints must not churn: an unchanged entry stays out of the sync set."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        entry = {
            "url": "https://mcp.example.com/v1",
            "scopes": ["read"],
            "clientId": "public-client-id",
        }
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {"remote": entry}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"remote": entry}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        assert discover_servers_to_sync() == []

    @pytest.mark.parametrize(
        "spec_extra,expected_scopes,expected_client_id",
        [
            ({"scopes": "read"}, [], ""),
            # A partially-valid list degrades to NO scopes, never to its
            # well-formed subset: truncating it would propagate a request the
            # file never made, and would disagree with the emit path, which
            # omits the field entirely on any malformed member.
            ({"scopes": ["read", 7]}, [], ""),
            ({"scopes": ["read", "  "]}, [], ""),
            ({"scopes": None}, [], ""),
            ({"clientId": 42}, [], ""),
            ({"clientId": "   "}, [], ""),
            ({"clientId": None}, [], ""),
        ],
    )
    def test_discover_degrades_malformed_oauth_hints(
        self, tmp_path, monkeypatch, spec_extra, expected_scopes, expected_client_id
    ) -> None:
        """Hand-edited mcp.json must not propagate a bad shape into the agent config."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        spec = {"url": "https://mcp.example.com/v1", **spec_extra}
        mcp_json.write_text(json.dumps({"mcpServers": {"remote": spec}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].scopes == expected_scopes
        assert result[0].client_id == expected_client_id

    def test_discover_none_when_all_known(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"srv": {"command": "a"}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"srv": {"command": "a"}}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        new = discover_servers_to_sync()
        assert new == []

    def test_discover_includes_existing_with_divergent_env(self, tmp_path, monkeypatch) -> None:
        """Existing servers with new env keys in mcp.json are included."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"srv": {"command": "a", "env": {}}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a", "env": {"KEY": "val"}}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert len(result) == 1
        assert result[0].name == "srv"
        assert result[0].env == {"KEY": "val"}

    def test_discover_skips_existing_with_expanded_path(self, tmp_path, monkeypatch) -> None:
        """An expanded env.PATH in the agent config is not a divergence.

        install_agent writes the effective PATH while mcp.json keeps the
        fragment the user authored — the same resolved-vs-authored asymmetry
        ``_commands_diverged`` already absorbs for commands. Comparing the raw
        strings would flag every synced server on every refresh, re-syncing
        forever.
        """
        from kiro_crew.env import spec_env_path

        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        expanded = spec_env_path("/opt/shims")
        cfg = {"mcpServers": {"srv": {"command": "a", "env": {"PATH": expanded}}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a", "env": {"PATH": "/opt/shims"}}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        assert discover_servers_to_sync() == []

    def test_discover_flags_changed_path_fragment(self, tmp_path, monkeypatch) -> None:
        """A genuinely edited env.PATH still triggers a re-sync."""
        from kiro_crew.env import spec_env_path

        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"srv": {"command": "a", "env": {"PATH": spec_env_path("/opt/old")}}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a", "env": {"PATH": "/opt/new"}}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert len(result) == 1
        assert result[0].env == {"PATH": "/opt/new"}

    def test_discover_skips_existing_with_identical_env(self, tmp_path, monkeypatch) -> None:
        """Existing servers with identical env are not flagged for sync."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"srv": {"command": "a", "env": {"KEY": "val"}}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a", "env": {"KEY": "val"}}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert result == []

    def test_discover_skips_existing_when_source_env_is_subset(self, tmp_path, monkeypatch) -> None:
        """Server not flagged when all mcp.json env keys already exist in agent config."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"srv": {"command": "a", "env": {"EXISTING": "keep", "NEW": "val"}}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a", "env": {"NEW": "val"}}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert result == []

    def test_discover_skips_existing_with_divergent_args(self, tmp_path, monkeypatch) -> None:
        """Existing servers with different args are NOT flagged for sync.

        Args are user-customizable (e.g. --include-tools additions).
        Since install_agent() preserves user args via setdefault merge,
        flagging on args divergence only wastes a full config rebuild.
        """
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {
            "mcpServers": {
                "srv": {
                    "command": "srv-cmd",
                    "args": ["--include-tools=ReadInternalWebsites,TicketingWriteActions"],
                }
            }
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "srv-cmd",
                            "args": ["--include-tools=ReadInternalWebsites"],
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert result == []

    def test_discover_skips_disabled_servers(self, tmp_path, monkeypatch) -> None:
        """Disabled servers are never included in sync results."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "disabled-srv": {"command": "x", "disabled": True},
                        "enabled-srv": {"command": "y"},
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert len(result) == 1
        assert result[0].name == "enabled-srv"

    def test_discover_skips_resolved_path_match(self, tmp_path, monkeypatch) -> None:
        """Short command name matching the basename of the agent's resolved path is not flagged."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"srv": {"command": "/usr/local/bin/my-server"}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"srv": {"command": "my-server"}}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert result == []


class TestCommandsDiverged:
    def test_identical_commands(self) -> None:
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("foo", "foo") is False

    def test_short_vs_resolved_path(self) -> None:
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("deep-research", "/home/user/.toolbox/bin/deep-research") is False

    def test_resolved_vs_short(self) -> None:
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("/usr/bin/server", "server") is False

    def test_genuinely_different_commands(self) -> None:
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("old-server", "new-server") is True

    def test_distinct_absolute_paths_sharing_a_basename_diverge(self) -> None:
        """Two different binaries with the same file name are NOT the same server."""
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("/opt/a/bin/srv", "/opt/b/bin/srv") is True

    def test_relative_path_does_not_match_unrelated_rooted_path(self) -> None:
        """A CWD-relative path names a specific file, not a PATH lookup.

        ``bin/srv`` resolves against the working directory, so it is not the bare
        name that ``PATH`` lookup turned into ``/usr/bin/srv`` — treating it as
        one would silently skip syncing a genuinely changed command.
        """
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("bin/srv", "/usr/bin/srv") is True
        assert _commands_diverged("./srv", "/usr/bin/srv") is True
        assert _commands_diverged("/usr/bin/srv", "bin/srv") is True

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="Windows-only: PATHEXT suffixes are only stripped there.",
    )
    def test_differing_pathext_suffixes_diverge(self) -> None:
        """``foo.bat`` and ``foo.cmd`` are different files, not two spellings of one.

        Only the ``shutil.which``-resolved (rooted) side may shed its suffix;
        folding it off both sides would collapse distinct executables.
        """
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("foo.bat", r"C:\x\foo.cmd") is True
        assert _commands_diverged("myserver.js", r"C:\x\myserver.exe") is True

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="Windows-only: PATHEXT suffixing and case/separator-insensitive paths.",
    )
    def test_pathext_resolved_command_does_not_diverge(self, monkeypatch) -> None:
        """A bare name matches the ``shutil.which`` result that carries a PATHEXT suffix.

        ``agent._resolve_command`` resolves ``npx`` to ``...\\npx.CMD`` because
        ``shutil.which`` spells the extension as ``PATHEXT`` does. Treating that as
        divergence would re-sync and reset every session on every startup.
        """
        from kiro_crew.mcp_discovery import _commands_diverged

        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        assert _commands_diverged("npx", r"C:\Program Files\nodejs\npx.CMD") is False
        assert _commands_diverged(r"C:\tools\my-server.exe", "my-server") is False

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="Windows-only: paths are case-insensitive and accept either separator.",
    )
    def test_separator_and_case_variants_do_not_diverge(self) -> None:
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged(r"C:\tools\srv.exe", "C:/Tools/SRV.exe") is False

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="Windows-only: a driveless root is not ntpath.isabs but still names a path.",
    )
    def test_driveless_rooted_path_matches_bare_name(self) -> None:
        """A POSIX-shaped ``mcp.json`` copied onto Windows still resolves by basename.

        ``ntpath.isabs('/usr/bin/srv')`` is False (no drive), so a rooted-path check
        alone would read the whole string as a bare command name.
        """
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("srv", "/usr/bin/srv") is False
        assert _commands_diverged(r"\tools\srv", "srv") is False

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="POSIX-only: filenames are case-sensitive there, unlike Windows.",
    )
    def test_case_differing_commands_diverge_on_posix(self) -> None:
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("Server", "/usr/bin/server") is True


class TestSyncToAgentConfig:
    def test_sync_never_launches_kiro_cli(self, tmp_path, monkeypatch) -> None:
        """sync_to_agent_config launches no subprocess, even with kiro-cli on PATH.

        The ``kiro-cli mcp add`` side channel was an unsynchronized second
        writer of the agent config whose output ``install_agent()`` rewrote
        moments later; the sync is install_agent() alone now, so a reappearing
        Popen here is a regression to the two-writer design.
        """
        calls: list[list[str]] = []

        def mock_which(x: str, **kw: object) -> str | None:
            return "/usr/bin/kiro-cli" if x == "kiro-cli" else None

        class MockPopen:
            returncode = 0

            def __init__(self, cmd: list[str], **kwargs: object) -> None:
                calls.append(list(cmd))

            def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
                return b"", b""

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.Popen", MockPopen)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps({"mcpServers": {}, "tools": [], "allowedTools": []}))

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        new_srv = McpServerInfo(name="new-srv", command="b", args=["--x"])
        ok = sync_to_agent_config([new_srv])
        assert ok is True
        assert install_called, "install_agent() is the one write path"
        assert calls == [], "no subprocess may be launched by the sync"

    def test_sync_fallback_writes_json(self, tmp_path, monkeypatch) -> None:
        """Without kiro-cli, delegates to install_agent() for config merge."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg = {
            "mcpServers": {"existing": {"command": "a"}},
            "tools": ["execute_bash"],
            "allowedTools": [],
        }
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        new_srv = McpServerInfo(name="new-srv", command="b", args=["--x"])
        ok = sync_to_agent_config([new_srv])
        assert ok is True
        assert install_called, "install_agent() should be called"

    def test_sync_no_installed_config(self, tmp_path, monkeypatch) -> None:
        """Works even when no config exists yet — install_agent creates it."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        config_path = kiro_dir / "kirocrew.json"
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(name="srv", command="x")
        ok = sync_to_agent_config([srv])
        assert ok is True
        assert install_called

    def test_sync_remote_server_writes_url(self, tmp_path, monkeypatch) -> None:
        """Remote servers are handled by install_agent() via source file merge."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg: dict = {"mcpServers": {}, "tools": [], "allowedTools": []}
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(
            name="deepwiki",
            url="https://mcp.deepwiki.com/mcp",
            headers={"X-Key": "val"},
        )
        ok = sync_to_agent_config([srv])
        assert ok is True
        assert install_called

    def test_sync_mixed_servers_launch_nothing(self, tmp_path, monkeypatch) -> None:
        """A mixed remote+local set syncs through install_agent() with no subprocess."""
        calls: list[list[str]] = []

        def mock_which(x: str, **kw: object) -> str | None:
            return "/usr/bin/kiro-cli" if x == "kiro-cli" else None

        class MockPopen:
            returncode = 0

            def __init__(self, cmd: list[str], **kwargs: object) -> None:
                calls.append(list(cmd))

            def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
                return b"", b""

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.Popen", MockPopen)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps({"mcpServers": {}, "tools": [], "allowedTools": []}))

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        remote = McpServerInfo(name="deepwiki", url="https://mcp.deepwiki.com/mcp")
        local = McpServerInfo(name="local-srv", command="some-cmd")
        assert sync_to_agent_config([remote, local]) is True
        assert install_called
        assert calls == []

    def test_sync_merges_env_for_existing_local_server(self, tmp_path, monkeypatch) -> None:
        """Existing server env changes are handled by install_agent() re-merge."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg = {
            "mcpServers": {
                "aws-outlook-mcp": {"command": "node", "args": ["server.js"], "env": {}}
            },
            "tools": ["@aws-outlook-mcp"],
            "allowedTools": ["@aws-outlook-mcp"],
        }
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(
            name="aws-outlook-mcp",
            command="node",
            args=["server.js"],
            env={"OUTLOOK_MCP_ENABLE_WRITES": "true"},
        )
        sync_to_agent_config([srv])
        assert install_called, "install_agent() handles env merge"

    def test_sync_preserves_existing_env_keys(self, tmp_path, monkeypatch) -> None:
        """Env merge is handled by install_agent() reading source files."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg = {
            "mcpServers": {
                "my-mcp": {
                    "command": "node",
                    "args": [],
                    "env": {"EXISTING_KEY": "keep"},
                }
            },
            "tools": ["@my-mcp"],
            "allowedTools": ["@my-mcp"],
        }
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(
            name="my-mcp",
            command="node",
            args=[],
            env={"NEW_KEY": "val"},
        )
        sync_to_agent_config([srv])
        assert install_called, "install_agent() handles env merge"

    def test_sync_updates_command_for_existing_local_server(self, tmp_path, monkeypatch) -> None:
        """Existing servers are refreshed via install_agent() which reads source files."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg = {
            "mcpServers": {"my-mcp": {"command": "old-cmd", "args": ["--old"], "env": {}}},
            "tools": ["@my-mcp"],
            "allowedTools": ["@my-mcp"],
        }
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        # install_agent() is called internally — mock it to verify delegation
        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(name="my-mcp", command="new-cmd", args=["--new"])
        result = sync_to_agent_config([srv])
        assert result is True
        assert install_called, "install_agent() should be called to re-merge config"

    def test_sync_source_env_overrides_existing_on_conflict(self, tmp_path, monkeypatch) -> None:
        """Config changes are handled by install_agent() re-merge, not direct edit."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg = {
            "mcpServers": {
                "my-mcp": {
                    "command": "node",
                    "args": [],
                    "env": {"SHARED": "old", "ONLY_EXISTING": "keep"},
                }
            },
            "tools": ["@my-mcp"],
            "allowedTools": ["@my-mcp"],
        }
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(
            name="my-mcp",
            command="node",
            args=[],
            env={"SHARED": "new", "ONLY_SOURCE": "added"},
        )
        result = sync_to_agent_config([srv])
        assert result is True
        assert install_called, "install_agent() should be called to re-merge config"

    def test_sync_skips_disabled_server_in_kiro_cli_add(self, tmp_path, monkeypatch) -> None:
        """Defense-in-depth: disabled servers are not registered via kiro-cli mcp add."""
        calls: list[list[str]] = []

        def mock_which(x: str, **kw: object) -> str | None:
            return "/usr/bin/kiro-cli" if x == "kiro-cli" else None

        class MockPopen:
            returncode = 0

            def __init__(self, cmd: list[str], **kwargs: object) -> None:
                calls.append(list(cmd))

            def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
                return b"", b""

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.Popen", MockPopen)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps({"mcpServers": {}, "tools": [], "allowedTools": []}))

        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: config_path,
        )

        # Source mcp.json marks this server as disabled
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"disabled-srv": {"command": "x", "disabled": True}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        disabled_srv = McpServerInfo(name="disabled-srv", command="x")
        sync_to_agent_config([disabled_srv])

        # kiro-cli mcp add should NOT have been called for the disabled server
        for call in calls:
            assert (
                "disabled-srv" not in call
            ), f"disabled server should not be registered via kiro-cli: {call}"


class TestProbeCache:
    def setup_method(self) -> None:
        _clear_cache()

    def teardown_method(self) -> None:
        _clear_cache()

    def test_cache_miss_returns_unknown(self) -> None:
        status, tools, error, probed_at, probe_mode = _get_cached("nonexistent")
        assert status == "unknown"
        assert tools == []
        assert error == ""
        assert probed_at == 0.0
        assert probe_mode == "handshake"

    def test_cache_hit_within_ttl(self) -> None:
        server = McpServerInfo(
            name="test-srv", command="x", status="ok", tools=["t1", "t2"], error=""
        )
        before = time.time()
        _cache_probe(server)
        status, tools, error, probed_at, probe_mode = _get_cached("test-srv")
        assert status == "ok"
        assert tools == ["t1", "t2"]
        assert error == ""
        assert probed_at >= before
        assert probe_mode == "handshake"

    def test_cache_expired_returns_outdated_with_tools(self, monkeypatch) -> None:
        server = McpServerInfo(
            name="test-srv", command="x", status="ok", tools=["t1", "t2"], error=""
        )
        _cache_probe(server)
        # Simulate expiry by backdating probed_at
        _probe_cache["test-srv"].probed_at = time.monotonic() - 2000
        status, tools, error, probed_at, _mode = _get_cached("test-srv")
        assert status == "outdated"
        assert tools == ["t1", "t2"]
        assert error == ""
        # WHEN it was last true survives expiry — that is the whole value of
        # an "outdated" row.
        assert probed_at > 0

    def test_cache_error_preserved(self) -> None:
        server = McpServerInfo(
            name="err-srv", command="x", status="error", tools=[], error="timeout"
        )
        _cache_probe(server)
        status, tools, error, _at, _mode = _get_cached("err-srv")
        assert status == "error"
        assert error == "timeout"

    def test_cache_preserves_declared_probe_mode(self) -> None:
        """The in-process fallback's "declared" mode survives the cache round trip."""
        server = McpServerInfo(
            name="managed-srv", command="x", status="ok", tools=["t"], probe_mode="declared"
        )
        _cache_probe(server)
        *_rest, probe_mode = _get_cached("managed-srv")
        assert probe_mode == "declared"

    def test_list_servers_merges_cache(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"my-srv": {"command": "cmd"}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (tmp_path / "x",))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)

        # Before probe: unknown
        servers = list_servers()
        assert servers[0].status == "unknown"

        # Cache a probe result
        _cache_probe(McpServerInfo(name="my-srv", command="cmd", status="ok", tools=["a"]))

        # After probe: cached status and tools merged
        servers = list_servers()
        assert servers[0].status == "ok"
        assert servers[0].tools == ["a"]


class TestReadJsonrpcResponse:
    @pytest.mark.asyncio
    async def test_json_content_type(self) -> None:
        resp = MagicMock()
        resp.content_type = "application/json"
        resp.json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
        result = await _read_jsonrpc_response(resp)
        assert result == {"jsonrpc": "2.0", "id": 1, "result": {}}

    @pytest.mark.asyncio
    async def test_sse_content_type(self) -> None:
        sse_body = (
            "event: message\n" 'data: {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}\n' "\n"
        )
        resp = MagicMock()
        resp.content_type = "text/event-stream"
        resp.text = AsyncMock(return_value=sse_body)
        result = await _read_jsonrpc_response(resp)
        assert result["id"] == 1
        assert result["result"] == {"tools": []}

    @pytest.mark.asyncio
    async def test_sse_picks_last_response(self) -> None:
        """Multiple data lines — picks the last one with an id."""
        sse_body = (
            'data: {"jsonrpc": "2.0", "method": "log"}\n'
            'data: {"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}\n'
        )
        resp = MagicMock()
        resp.content_type = "text/event-stream"
        resp.text = AsyncMock(return_value=sse_body)
        result = await _read_jsonrpc_response(resp)
        assert result["result"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_sse_empty_returns_empty_dict(self) -> None:
        resp = MagicMock()
        resp.content_type = "text/event-stream"
        resp.text = AsyncMock(return_value="")
        result = await _read_jsonrpc_response(resp)
        assert result == {}


class TestProbeRemote:
    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_probe_remote_ok(self) -> None:
        """Successful HTTP probe returns ok status and tools."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        init_resp = MagicMock()
        init_resp.status = 200
        init_resp.content_type = "application/json"
        init_resp.headers = {}
        init_resp.json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
        init_resp.__aenter__ = AsyncMock(return_value=init_resp)
        init_resp.__aexit__ = AsyncMock(return_value=False)

        # notifications/initialized gets a body-less accept.
        notif_resp = MagicMock()
        notif_resp.status = 202
        notif_resp.__aenter__ = AsyncMock(return_value=notif_resp)
        notif_resp.__aexit__ = AsyncMock(return_value=False)

        tools_resp = MagicMock()
        tools_resp.status = 200
        tools_resp.content_type = "application/json"
        tools_resp.json = AsyncMock(
            return_value={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"tools": [{"name": "search"}, {"name": "read"}]},
            }
        )
        tools_resp.__aenter__ = AsyncMock(return_value=tools_resp)
        tools_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=[init_resp, notif_resp, tools_resp])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "ok"
        assert result.tools == ["search", "read"]
        # Lifecycle order: initialize, notifications/initialized, tools/list.
        methods = [c.kwargs["json"].get("method") for c in mock_session.post.call_args_list]
        assert methods == ["initialize", "notifications/initialized", "tools/list"]

    @pytest.mark.asyncio
    async def test_probe_remote_carries_the_session_id(self) -> None:
        """A stateful server's Mcp-Session-Id must ride every follow-up request,
        or a HEALTHY server renders errored when it rejects the sessionless
        tools/list."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        init_resp = MagicMock()
        init_resp.status = 200
        init_resp.content_type = "application/json"
        init_resp.headers = {"Mcp-Session-Id": "sess-42"}
        init_resp.json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
        init_resp.__aenter__ = AsyncMock(return_value=init_resp)
        init_resp.__aexit__ = AsyncMock(return_value=False)

        notif_resp = MagicMock()
        notif_resp.status = 202
        notif_resp.__aenter__ = AsyncMock(return_value=notif_resp)
        notif_resp.__aexit__ = AsyncMock(return_value=False)

        tools_resp = MagicMock()
        tools_resp.status = 200
        tools_resp.content_type = "application/json"
        tools_resp.json = AsyncMock(
            return_value={"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
        )
        tools_resp.__aenter__ = AsyncMock(return_value=tools_resp)
        tools_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=[init_resp, notif_resp, tools_resp])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "ok"
        for call in mock_session.post.call_args_list[1:]:
            assert call.kwargs["headers"].get("Mcp-Session-Id") == "sess-42"

    @pytest.mark.asyncio
    async def test_probe_remote_http_error(self) -> None:
        """Non-200 response sets error status."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 500
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "error"
        assert "500" in result.error

    @pytest.mark.asyncio
    async def test_probe_remote_401_reports_needs_auth(self) -> None:
        """A tokenless 401 from a remote OAuth server is needs_auth, not error.

        The runtime holds the OAuth token and calls the server fine; the probe
        never sees that token, so 401 means "authenticate", not "broken".
        """
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 401
        resp.headers = {}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_probe_remote_403_with_challenge_reports_needs_auth(self) -> None:
        """A 403 carrying a WWW-Authenticate challenge is also needs_auth."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 403
        resp.headers = {"WWW-Authenticate": 'Bearer resource_metadata="https://example.com/.well-known"'}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_probe_remote_403_without_challenge_is_error(self) -> None:
        """A plain 403 (no challenge) is a real error, not needs_auth."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 403
        resp.headers = {}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "error"
        assert "403" in result.error

    @pytest.mark.asyncio
    async def test_probe_remote_401_with_static_auth_header_is_error(self) -> None:
        """A 401 despite a configured Authorization header is a real error.

        The caller supplied a credential and it was rejected — that is a
        genuine failure, so it must not be masked as needs_auth.
        """
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer stale-token"},
        )

        resp = MagicMock()
        resp.status = 401
        resp.headers = {}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "error"
        assert "401" in result.error

    def test_needs_authorization_predicate(self) -> None:
        """Unit-level truth table for _needs_authorization."""
        from kiro_crew.mcp_discovery import _needs_authorization

        # Tokenless 401 → authenticate.
        assert _needs_authorization(401, {}, {}) is True
        # 403 with a challenge → authenticate.
        assert _needs_authorization(403, {"WWW-Authenticate": "Bearer"}, {}) is True
        # Header lookups are case-insensitive.
        assert _needs_authorization(403, {"www-authenticate": "Bearer"}, {}) is True
        # 403 without a challenge → not an auth prompt.
        assert _needs_authorization(403, {}, {}) is False
        # A rejected static credential is a real error, never needs_auth.
        assert _needs_authorization(401, {}, {"Authorization": "Bearer x"}) is False
        assert _needs_authorization(401, {}, {"authorization": "Bearer x"}) is False
        # Other statuses are never needs_auth.
        assert _needs_authorization(500, {}, {}) is False

    def test_a_real_oauth_challenge_is_recognised(self) -> None:
        """The two pieces of evidence that make a 401 an OAuth challenge."""
        from kiro_crew.mcp_discovery import _is_bearer_challenge

        assert _is_bearer_challenge(
            'Bearer resource_metadata="https://mcp.example.ai/.well-known/'
            'oauth-protected-resource/mcp", scope="openid email offline_access"'
        )
        # Either piece alone is enough.
        assert _is_bearer_challenge('Bearer resource_metadata="https://x.example/.well-known/y"')
        assert _is_bearer_challenge('Bearer scope="openid"')

    @pytest.mark.parametrize(
        "challenge",
        [
            pytest.param("", id="empty"),
            pytest.param('Basic realm="x"', id="another-scheme-entirely"),
            pytest.param('BearerToken scope="openid"', id="bearer-prefix-is-another-scheme"),
            pytest.param(
                'bearerish resource_metadata="https://x.example/y"',
                id="bearer-word-prefix-is-another-scheme",
            ),
            pytest.param("Bearer", id="bearer-with-no-evidence"),
            pytest.param('Bearer scope=""', id="empty-scope"),
            pytest.param('Bearer resource_metadata="http://insecure.example/x"', id="http-metadata"),
            pytest.param(
                'Bearer resource_metadata="javascript:alert(1)"', id="non-http-scheme-metadata"
            ),
            pytest.param("Bearer resource_metadata=" + "x" * 4096, id="over-length"),
            pytest.param(None, id="not-a-string-at-all"),
        ],
    )
    def test_anything_it_cannot_vouch_for_is_not_a_challenge(self, challenge) -> None:
        """Unrecognised or unsafe evidence reads as "no challenge", and never raises.

        An http or javascript metadata URL from an unauthenticated endpoint is not
        evidence of anything, so it does not count toward recognition.
        """
        from kiro_crew.mcp_discovery import _is_bearer_challenge

        assert _is_bearer_challenge(challenge) is False

    @pytest.mark.asyncio
    async def test_a_tokenless_401_records_the_challenge_and_an_absent_grant(self) -> None:
        """needs_auth carries the evidence that separates it from 'signed in already'."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 401
        resp.headers = {
            "WWW-Authenticate": 'Bearer resource_metadata="https://example.com/.well-known/x",'
            ' scope="openid email"'
        }
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session), patch(
            "kiro_crew.mcp_discovery._runtime_grant_present", AsyncMock(return_value=False)
        ):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.auth_challenge is True
        assert result.auth_grant_present is False
        payload = result.to_dict()
        assert payload["authChallenge"] is True
        assert payload["authGrantPresent"] is False

    @pytest.mark.asyncio
    async def test_an_existing_runtime_grant_is_reported_alongside_needs_auth(self) -> None:
        """A held grant is what makes 'cannot verify' the honest wording rather than 'sign in'."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 401
        resp.headers = {"WWW-Authenticate": 'Bearer scope="openid"'}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session), patch(
            "kiro_crew.mcp_discovery._runtime_grant_present", AsyncMock(return_value=True)
        ):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.auth_grant_present is True

    @pytest.mark.asyncio
    async def test_a_rejected_static_credential_still_records_the_oauth_challenge(self) -> None:
        """The error branch keeps the challenge — and looks up no grant.

        A pasted token against an OAuth-only server is a real error — but the
        useful thing to say is that the server wants a sign-in, not that it
        answered 401.

        The GRANT is the half that stops here. Its only reader gates on
        ``needs_auth``, so a lookup on this branch would run a stat, and let
        ``grant_observed`` write a critical SEL event, for an observation nothing
        renders.
        """
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer pasted-placeholder"},
        )

        resp = MagicMock()
        resp.status = 401
        resp.headers = {"WWW-Authenticate": 'Bearer scope="openid offline_access"'}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        grant_lookup = AsyncMock(return_value=False)
        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session), patch(
            "kiro_crew.mcp_discovery._runtime_grant_present", grant_lookup
        ):
            result = await _probe_remote(server)

        assert result.status == "error"
        assert "401" in result.error
        assert result.auth_challenge is True
        grant_lookup.assert_not_awaited()
        assert result.auth_grant_present is None
        assert "authGrantPresent" not in result.to_dict()

    def test_a_probe_that_learned_nothing_emits_no_auth_keys(self) -> None:
        """An absent key is what lets a client tell 'unknown' from 'no grant'."""
        payload = McpServerInfo(name="remote", url="https://example.com/mcp").to_dict()

        assert "authChallenge" not in payload
        assert "authGrantPresent" not in payload

    def test_the_probe_cache_round_trips_the_authorization_evidence(self) -> None:
        """The panel is served from this cache, so the evidence has to survive it."""
        from kiro_crew.mcp_discovery import _cache_probe, probe_metadata

        server = McpServerInfo(
            name="cached-remote",
            url="https://example.com/mcp",
            status="needs_auth",
            auth_challenge=True,
            auth_grant_present=True,
        )
        _cache_probe(server)

        cached = probe_metadata("cached-remote")
        assert cached is not None
        assert cached.auth_challenge is True
        assert cached.auth_grant_present is True

    @pytest.mark.asyncio
    async def test_an_unobservable_grant_is_omitted_rather_than_reported_absent(self) -> None:
        """"Could not observe" must not reach the client as "no grant".

        The sign-in wording is gated on an explicit ``false``, so emitting ``false``
        when the lookup merely failed would tell the owner of an already-authorized
        server to sign in again — the exact harm the gate exists to prevent. An
        unreadable cache home lands here.
        """
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        resp = MagicMock()
        resp.status = 401
        resp.headers = {"WWW-Authenticate": 'Bearer scope="openid"'}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session), patch(
            "kiro_crew.mcp_discovery._runtime_grant_present", AsyncMock(return_value=None)
        ):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.auth_challenge is True
        assert result.auth_grant_present is None
        payload = result.to_dict()
        assert payload["authChallenge"] is True
        assert "authGrantPresent" not in payload

    @pytest.mark.asyncio
    async def test_the_probe_asks_for_the_absent_grant_to_be_audited(self) -> None:
        """The probe acts on absence, so it must not inherit the mint's poll default.

        ``grant_observed`` records only the positive unless a caller opts in,
        because the mint polls and would otherwise write a critical SEL event per
        iteration. The probe is the opposite shape: one read, and the NEGATIVE is
        what turns a row into "Sign-in required". Without the opt-in that acted-on
        access leaves no trail at all.
        """
        from kiro_crew.mcp_discovery import _runtime_grant_present

        lookup = AsyncMock(return_value=False)
        with patch("kiro_crew.mcp_discovery.grant_observed", lookup):
            assert await _runtime_grant_present("https://example.com/mcp", "remote") is False

        lookup.assert_awaited_once_with("https://example.com/mcp", audit_absence=True)

    def test_importing_this_module_does_not_pull_in_the_mint_engine(self) -> None:
        """The grant helpers are shared through a leaf module, not through the mint.

        This module is reachable from the handlers package the gateway imports at
        boot, and ``connections.mint`` reaches the agent and ACP layers -- which
        ``test_the_handlers_package_does_not_import_the_mint_engine`` refuses to
        have loaded at boot. Sharing via ``mcp_grant`` is what lets the grant
        lookup be an ordinary module-scope import instead of a runtime one, so
        that separation is the thing worth pinning.

        Run in a subprocess: this test module already imports the mint, so an
        in-process ``sys.modules`` check would always find it.
        """
        probe = (
            "import sys; import kiro_crew.mcp_discovery;"
            " print('MINT' if 'kiro_crew.connections.mint' in sys.modules else 'CLEAN')"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, timeout=180, **UTF8_TEXT
        )
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.strip().endswith("CLEAN"), out.stdout

    @pytest.mark.asyncio
    async def test_a_re_probe_does_not_inherit_the_previous_endpoints_challenge(self) -> None:
        """Authorization evidence is per-probe, never carried forward.

        The probe cache is keyed by NAME and ``list_servers`` rehydrates these
        fields onto the row before a re-probe, so a server whose url was edited
        arrives carrying the OLD endpoint's verdict. A probe that only ever SETS
        the flags would keep reporting "Sign-in required" for an endpoint that
        never asked for one.
        """
        server = McpServerInfo(
            name="remote",
            url="https://example.com/mcp",
            auth_challenge=True,
            auth_grant_present=True,
        )

        resp = MagicMock()
        resp.status = 401
        resp.headers = {}  # a bare 401: no challenge this time
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "needs_auth"
        assert result.auth_challenge is False
        assert result.auth_grant_present is None
        assert "authChallenge" not in result.to_dict()

    @pytest.mark.asyncio
    async def test_a_credential_bearing_url_never_reaches_the_log(self, caplog) -> None:
        """The grant lookup runs for ANY url a user typed, so it must not log one.

        A custom endpoint can carry a credential in its userinfo or query string.
        The probe's own failure path therefore names the server, never the url —
        this line lands in gateway.log, which is not a credential store.
        """
        from kiro_crew.mcp_discovery import _runtime_grant_present

        secret_url = "https://user:sup3r-secret@mcp.example.com/mcp?token=abcd1234"

        # ``None`` is what an unreadable cache home resolves to: ``grant_presence``
        # classifies the failed stat itself, so nothing raises out to this caller.
        with patch("kiro_crew.mcp_discovery.grant_observed", AsyncMock(return_value=None)):
            with caplog.at_level(logging.DEBUG, logger="kiro_crew.mcp_discovery"):
                present = await _runtime_grant_present(secret_url, "higgsfield")

        # None, not False: the lookup could not answer, and the payload must not
        # report that as "no grant" -- that would name an action on no evidence.
        assert present is None
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "sup3r-secret" not in blob
        assert "abcd1234" not in blob
        assert "mcp.example.com" not in blob
        # The server name is what makes the line diagnosable at all.
        assert "higgsfield" in blob

    @pytest.mark.asyncio
    async def test_probe_remote_connection_error(self) -> None:
        """Connection failure sets error status."""
        server = McpServerInfo(name="remote", url="https://unreachable.example.com/mcp")

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=ConnectionError("refused"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_probe_dispatches_to_remote(self) -> None:
        """probe_server dispatches to _probe_remote for url-based servers."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        with patch("kiro_crew.mcp_discovery._probe_remote", new_callable=AsyncMock) as mock_remote:
            mock_remote.return_value = server
            result = await probe_server(server)

        mock_remote.assert_awaited_once_with(server)
        assert result is server

    @pytest.mark.asyncio
    async def test_probe_local_not_dispatched_to_remote(self) -> None:
        """probe_server does NOT dispatch to _probe_remote for command-based servers."""
        server = McpServerInfo(name="local", command="nonexistent-cmd-xyz")

        with patch("kiro_crew.mcp_discovery._probe_remote", new_callable=AsyncMock) as mock_remote:
            result = await probe_server(server)

        mock_remote.assert_not_awaited()
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_directory_qualified_command_reports_no_search_path(self) -> None:
        """A directory-qualified command is looked up directly, not PATH-searched.

        Regression for #5053: ``shutil.which`` returns before it reads ``path=``
        when the command carries a directory component, so it checks exactly the
        one location named. Reporting the declared search path for it told the
        reader it "searched N directories" that were never consulted -- the exact
        not-installed vs installed-elsewhere confusion #4954 exists to prevent,
        stated backwards. The error for such a command must be the bare
        ``command not found: <cmd>`` with no directory list.
        """
        abs_missing = os.path.join(os.sep, "opt", "vendor", "bin", "ghost-mcp")
        server = McpServerInfo(name="dirq", command=abs_missing)
        result = await probe_server(server)
        assert result.status == "error"
        assert f"command not found: {abs_missing}" in result.error
        assert "directories" not in result.error  # nothing was searched
        assert "empty PATH" not in result.error

    @pytest.mark.asyncio
    async def test_bare_command_still_reports_the_search_path(self) -> None:
        """The bare-command path is unchanged: it IS PATH-searched, so say so."""
        server = McpServerInfo(name="bare", command="ghost-mcp-xyz")
        result = await probe_server(server)
        assert result.status == "error"
        assert "command not found: ghost-mcp-xyz" in result.error
        assert "directories" in result.error  # PATH was searched, report it


class TestProbeServerConsentGate:
    """``probe_server`` itself refuses a consent-disabled server.

    Probing is what RUNS the server, so the refusal has to live in the function
    every entry point funnels through — not in each caller's pre-filter, which
    only holds until a new call site forgets it.
    """

    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_disabled_stdio_server_is_never_spawned(self) -> None:
        """No subprocess for a disabled stdio server, even with a resolvable command.

        ``shutil.which`` is stubbed so the probe cannot bail out early on
        "command not found" — that would make this test pass for the wrong
        reason, without ever proving the consent check ran.
        """
        server = McpServerInfo(name="held", command="true", disabled=True)

        with (
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/bin/true"),
            patch(
                "kiro_crew.mcp_discovery.create_subprocess_limited",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            result = await probe_server(server)

        mock_spawn.assert_not_awaited()
        assert result.status == "disabled"
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_disabled_remote_server_is_never_connected(self) -> None:
        """A disabled remote server opens no connection.

        The refusal sits ahead of the local/remote dispatch: probing a remote
        server reaches out over the network, which is equally not-consented.
        """
        server = McpServerInfo(name="held-remote", url="https://example.com/mcp", disabled=True)

        with patch("kiro_crew.mcp_discovery._probe_remote", new_callable=AsyncMock) as mock_remote:
            result = await probe_server(server)

        mock_remote.assert_not_awaited()
        assert result.status == "disabled"

    @pytest.mark.asyncio
    async def test_truthy_non_bool_disabled_still_withholds_spawn(self) -> None:
        """A non-bool ``disabled`` fails CLOSED.

        ``McpServerInfo`` is hand-built by callers (``cli_doctor`` does exactly
        that) and the flag can originate in unvalidated config JSON, so the
        check is truthiness rather than ``is True``.
        """
        server = McpServerInfo(name="held-str", command="true")
        server.disabled = "yes"  # type: ignore[assignment]

        with (
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/bin/true"),
            patch(
                "kiro_crew.mcp_discovery.create_subprocess_limited",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            result = await probe_server(server)

        mock_spawn.assert_not_awaited()
        assert result.status == "disabled"

    @pytest.mark.asyncio
    async def test_refusal_does_not_clobber_cached_tools(self) -> None:
        """The refusal must not write to the shared probe cache.

        Guards a specific future refactor rather than the missing guard: adding
        a well-meaning ``_cache_probe(server)`` to the refusal path to "record
        the disabled state". The cache is keyed by name and read by
        ``GET /api/mcp`` through ``_get_cached``, so an empty "disabled" entry
        would erase the tool list a real probe recorded before the user
        disabled the server. Verified by adding that call and watching this
        fail — it does NOT fail merely from removing the guard, because the
        probe's early error returns skip ``_cache_probe`` anyway.
        """
        probed = McpServerInfo(
            name="was-ok", command="true", status="ok", tools=["alpha", "beta"]
        )
        _cache_probe(probed)

        disabled = McpServerInfo(name="was-ok", command="true", disabled=True)
        with patch("kiro_crew.mcp_discovery.shutil.which", return_value="/bin/true"):
            await probe_server(disabled)

        status, tools, *_rest = _get_cached("was-ok")
        assert status == "ok"
        assert tools == ["alpha", "beta"]

    @pytest.mark.asyncio
    async def test_refusal_preserves_last_known_tools_and_clears_stale_error(self) -> None:
        """``tools`` survives the refusal; a stale probe ``error`` does not.

        ``list_servers`` merges cached status/tools/error onto every row, so a
        disabled row can arrive carrying both — and a leftover failure message
        is not the reason this call returned.
        """
        server = McpServerInfo(
            name="held-with-history",
            command="true",
            tools=["alpha"],
            error="timeout",
            disabled=True,
        )

        result = await probe_server(server)

        assert result.status == "disabled"
        assert result.tools == ["alpha"]
        assert result.error == ""


class TestProbeTempContainment:
    """#5064 probe wiring: spec-declared temp yields; Windows defers cleanup.

    Both tests assert ONLY on the containment calls and tolerate any probe
    outcome -- the probe command is a real interpreter that exits instantly,
    so the handshake fails, which is irrelevant to the temp lifecycle.
    """

    def setup_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_spec_declared_temp_suppresses_probe_containment(self, tmp_path) -> None:
        # Mirror of the backend chokepoint: an operator-declared temp key in
        # the SPEC (any casing -- Windows env keys are case-insensitive)
        # means no allocation at all, so a storage-heavy probe honors the
        # configured volume instead of ENOSPC-ing the data home.
        import sys

        from kiro_crew.mcp_gateway import backend_tmp as bt

        server = McpServerInfo(
            name="declared-temp",
            command=sys.executable,
            args=["-c", "pass"],
            env={"tmpdir": str(tmp_path / "chosen")},
        )
        alloc_spy = MagicMock(side_effect=AssertionError("must not allocate"))
        with patch.object(bt, "allocate_probe_tmp", alloc_spy):
            await probe_server(server)
        alloc_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_windows_probe_cleanup_defers_to_daemon_sweep(
        self, tmp_path, monkeypatch
    ) -> None:
        # On Windows tree death is unprovable (taskkill /T loses orphaned
        # children), so the probe's finally must NOT delete -- the daemon's
        # dual-condition sweep is the single deletion authority there. The
        # probe command exits on its own, so skipping the POSIX reap branch
        # (a side effect of patching IS_POSIX) leaks nothing.
        import sys

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        # The IS_POSIX patch also flips restrict_dir_to_owner onto its Windows
        # DACL branch, which cannot run on the POSIX host executing this test --
        # shim it to POSIX behavior so ALLOCATION survives and the test
        # exercises the logic it targets.
        monkeypatch.setattr(
            platform_compat,
            "restrict_dir_to_owner",
            # 0o700 is the RESTRICTIVE mode for a directory (see the identical
            # suppression in platform_compat.restrict_dir_to_owner itself).
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
            lambda p: os.chmod(p, 0o700),
        )

        # Pre-seed a PRIOR probe's dead+idle dir: the deferral path must run
        # the root-wide dual-condition sweep from THIS (gateway) process, so
        # reclamation exists even in a topology that never starts the mcp-tmp
        # daemon (no stub servers). Owner pid 2**22+5 is far above any live
        # pid on CI runners; mtimes aged beyond the grace window.
        stale = bt.backend_tmp_root() / "probe-deadbeef-old"
        stale.mkdir(parents=True)
        (stale / bt.OWNER_FILENAME).write_text(str(2**22 + 5), encoding="utf-8")
        old = time.time() - 7200
        os.utime(stale / bt.OWNER_FILENAME, (old, old))
        os.utime(stale, (old, old))
        os.utime(bt.backend_tmp_root(), (old, old))

        server = McpServerInfo(name="win-probe", command=sys.executable, args=["-c", "pass"])
        await probe_server(server)

        # The prior dead dir was reclaimed by the probe-side sweep...
        assert not stale.exists()
        # ...while the fresh probe's own dir (seconds-old tree) was kept.
        root = home / "run" / "mcp-tmp"
        assert root.exists() and any(root.iterdir())
        owners = [
            int((child / bt.OWNER_FILENAME).read_text(encoding="utf-8").strip())
            for child in root.iterdir()
            if (child / bt.OWNER_FILENAME).is_file()
        ]
        assert owners and all(pid != os.getpid() for pid in owners)

    @pytest.mark.asyncio
    async def test_probe_spawn_failure_reclaims_the_fresh_dir(
        self, tmp_path, monkeypatch
    ) -> None:
        # A dir whose probe never existed has no dead-owner future: the sweeps
        # never delete provisional/ownerless dirs, so the spawn-failure path
        # is its only reclamation point (mirrors spawn_backend). Exercised on
        # the WINDOWS path (IS_POSIX=False) where the finally-sweep defers to
        # the daemon -- there the except-block reclamation is the ONLY one;
        # on POSIX the finally would double-cover it and mask a regression.
        import sys

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        # The IS_POSIX patch also flips restrict_dir_to_owner onto its Windows
        # DACL branch, which cannot run on the POSIX host executing this test --
        # shim it to POSIX behavior so ALLOCATION survives and the test
        # exercises the logic it targets.
        monkeypatch.setattr(
            platform_compat,
            "restrict_dir_to_owner",
            # 0o700 is the RESTRICTIVE mode for a directory (see the identical
            # suppression in platform_compat.restrict_dir_to_owner itself).
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
            lambda p: os.chmod(p, 0o700),
        )

        server = McpServerInfo(name="spawnfail", command=sys.executable, args=["-c", "pass"])
        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("spawn failed"),
        ):
            result = await probe_server(server)

        assert result.status == "error"
        root = home / "run" / "mcp-tmp"
        assert not root.exists() or not any(root.iterdir())

    @pytest.mark.asyncio
    async def test_partial_spec_declaration_strips_ambient_tmpdir(
        self, tmp_path, monkeypatch
    ) -> None:
        # Spec declares only TMP: the probe env must not keep the ambient
        # TMPDIR (tempfile consults it first), and no managed dir may be
        # allocated. Mirrors the backend chokepoint's pruning.
        import sys

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setenv("TMPDIR", str(tmp_path / "ambient"))

        server = McpServerInfo(
            name="partial-temp",
            command=sys.executable,
            args=["-c", "pass"],
            env={"TMP": str(tmp_path / "chosen")},
        )
        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("stop after env capture"),
        ) as spawn_mock:
            await probe_server(server)

        captured = spawn_mock.call_args.kwargs["env"]
        assert "TMPDIR" not in captured
        assert captured["TMP"] == str(tmp_path / "chosen")
        root = home / "run" / "mcp-tmp"
        assert not root.exists() or not any(root.iterdir())

    @pytest.mark.asyncio
    async def test_probe_drops_reserved_kirocrew_namespace_from_spec_env(
        self, tmp_path, monkeypatch
    ) -> None:
        """SECURITY: the probe is the SIBLING site of the cron tool bridge.

        Both apply a config-declared ``env`` to a child they spawn themselves, but
        only ``cron_script`` had a ``KIROCREW_*`` deny (``_CRON_ENV_DENY``, via
        ``KIROCREW_OWNER_ID``) -- the probe applied none. Putting the
        reserved-namespace deny in the shared sanitizer rather than in the cron
        deny-set is what covers this path too, so this test is the reason for that
        placement. A gateway-authored value must still be INHERITED: the probe
        builds its env from ``dict(os.environ)`` and only the OVERRIDE is refused.
        """
        import sys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "real-home"))
        monkeypatch.delenv("KIROCREW_CLI", raising=False)

        server = McpServerInfo(
            name="identity-forger",
            command=sys.executable,
            args=["-c", "pass"],
            env={
                "KIROCREW_CLI": "1",
                "KIROCREW_SESSION_KEY": "some-other-session",
                "KIROCREW_HOME": str(tmp_path / "attacker-home"),
                "MCP_TOKEN": "keep-me",
            },
        )
        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("stop after env capture"),
        ) as spawn_mock:
            await probe_server(server)

        captured = spawn_mock.call_args.kwargs["env"]
        assert "KIROCREW_CLI" not in captured
        assert "KIROCREW_SESSION_KEY" not in captured
        # Inherited value survives; the spec's override of it does not.
        assert captured["KIROCREW_HOME"] == str(tmp_path / "real-home")
        assert captured["MCP_TOKEN"] == "keep-me"

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="POSIX-only control: on Windows the finally-path deferral to the "
        "daemon sweep is the designed behavior (see the deferral test above)",
    )
    @pytest.mark.asyncio
    async def test_posix_probe_cleanup_sweeps_its_own_dir(self, tmp_path, monkeypatch) -> None:
        # Control for the Windows deferral: on POSIX the group reap is
        # tree-faithful, so the probe's finally DOES reclaim its private dir.
        import sys

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        server = McpServerInfo(name="posix-probe", command=sys.executable, args=["-c", "pass"])
        await probe_server(server)

        root = home / "run" / "mcp-tmp"
        assert not root.exists() or not any(root.iterdir())


class TestProbeServerProcessCleanup:
    """Tests for the finally block that tears down the probed subprocess."""

    def _make_mock_proc(self, *, wait_side_effect=None):
        proc = AsyncMock()
        proc.returncode = None  # process still running
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.kill = MagicMock()
        if wait_side_effect:
            proc.wait = AsyncMock(side_effect=wait_side_effect)
        else:
            proc.wait = AsyncMock(return_value=0)
        return proc

    @pytest.mark.asyncio
    async def test_graceful_stdin_close(self) -> None:
        """Closing stdin causes process to exit within timeout."""
        proc = self._make_mock_proc()
        server = McpServerInfo(name="test", command="echo")

        with (
            patch("kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", return_value=proc),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
        ):
            proc.stdout = AsyncMock()
            proc.stdout.readline = AsyncMock(return_value=b"")
            await probe_server(server)

        proc.stdin.close.assert_called_once()
        proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_kill_on_timeout(self) -> None:
        """When graceful shutdown times out, falls back to proc.kill()."""
        proc = self._make_mock_proc(
            wait_side_effect=[asyncio.TimeoutError(), 0]
        )
        server = McpServerInfo(name="test", command="echo")

        with (
            patch("kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", return_value=proc),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
        ):
            proc.stdout = AsyncMock()
            proc.stdout.readline = AsyncMock(return_value=b"")
            await probe_server(server)

        proc.stdin.close.assert_called_once()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_kill_also_fails(self) -> None:
        """When both graceful and forceful shutdown fail, exception is swallowed."""
        proc = self._make_mock_proc(
            wait_side_effect=[asyncio.TimeoutError(), OSError("kill failed")]
        )
        server = McpServerInfo(name="test", command="echo")

        with (
            patch("kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", return_value=proc),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
        ):
            proc.stdout = AsyncMock()
            proc.stdout.readline = AsyncMock(return_value=b"")
            await probe_server(server)

        # Should not raise — the exception is caught and swallowed
        proc.stdin.close.assert_called_once()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_stdin_none_skips_close(self) -> None:
        """When proc.stdin is None, close is skipped without error."""
        proc = self._make_mock_proc()
        proc.stdin = None
        server = McpServerInfo(name="test", command="echo")

        with (
            patch("kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", return_value=proc),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
        ):
            proc.stdout = AsyncMock()
            proc.stdout.readline = AsyncMock(return_value=b"")
            await probe_server(server)

        # Should not raise — stdin None is handled gracefully
        proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_spawns_with_the_expanded_path(self, monkeypatch) -> None:
        """The probe's child gets the same PATH the emitted config carries.

        Pinning the ``env`` kwarg is the only way to prove the probe and
        ``install_agent`` agree — a divergence here is what let a server report
        healthy on the dashboard and fail in a session.
        """
        from kiro_crew.env import spec_env_path

        monkeypatch.setenv("PATH", "/usr/bin")
        proc = self._make_mock_proc()
        server = McpServerInfo(name="test", command="echo", env={"PATH": "/opt/shims"})
        captured: dict = {}

        def _spawn(*argv, **kw):  # noqa: ANN002, ANN003 - test shim
            captured.update(kw)
            return proc

        with (
            patch("kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", side_effect=_spawn),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
        ):
            proc.stdout = AsyncMock()
            proc.stdout.readline = AsyncMock(return_value=b"")
            await probe_server(server)

        spawned = captured["env"]["PATH"]
        assert spawned == spec_env_path("/opt/shims")
        entries = spawned.split(os.pathsep)
        assert entries[0] == "/opt/shims"
        assert "/usr/bin" in entries


class TestInstallAgentRemote:
    """Test that install_agent preserves remote url-based MCP servers."""

    def test_install_preserves_remote_server(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.agent import install_agent

        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}))
        (agent_dir / "prompt.md").write_text("prompt")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        existing = {
            "mcpServers": {
                "deepwiki": {"url": "https://mcp.deepwiki.com/mcp"},
                "local-srv": {"command": "nonexistent-cmd-xyz"},
            },
            "tools": [],
            "allowedTools": [],
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
        monkeypatch.setattr(
            "kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "nonexistent_kiro_mcp.json"
        )
        monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
        monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")
        monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)

        install_agent()

        data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
        assert "deepwiki" in data["mcpServers"]
        assert data["mcpServers"]["deepwiki"]["url"] == "https://mcp.deepwiki.com/mcp"
        assert "local-srv" not in data["mcpServers"]

    def test_install_merges_kiro_mcp_json(self, tmp_path, monkeypatch) -> None:
        """install_agent picks up servers from ~/.kiro/settings/mcp.json."""
        from kiro_crew.agent import install_agent

        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}))
        (agent_dir / "prompt.md").write_text("prompt")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "mcp.json").write_text(
            json.dumps({"mcpServers": {"deepwiki": {"url": "https://mcp.deepwiki.com/mcp"}}})
        )

        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
        monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
        monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
        monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")
        monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)

        install_agent()

        data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
        assert "deepwiki" in data["mcpServers"]
        assert data["mcpServers"]["deepwiki"]["url"] == "https://mcp.deepwiki.com/mcp"


class TestGetProbeTimeout:
    """Tests for the config-aware _get_probe_timeout() getter."""

    def test_get_probe_timeout_reads_config(self) -> None:
        """_get_probe_timeout() returns the config value when available."""
        from kiro_crew.mcp_discovery import _get_probe_timeout

        mock_cfg = MagicMock()
        mock_cfg.dashboard.mcp_probe_timeout_secs = 45
        mock_cls = MagicMock()
        mock_cls.load.return_value = mock_cfg

        with patch("kiro_crew.config.loader.KiroCrewConfig", mock_cls):
            result = _get_probe_timeout()
        assert result == 45

    def test_get_probe_timeout_fallback(self) -> None:
        """_get_probe_timeout() returns 15 when config is unavailable."""
        from kiro_crew.mcp_discovery import _PROBE_TIMEOUT_SECS, _get_probe_timeout

        mock_cls = MagicMock()
        mock_cls.load.side_effect = RuntimeError("no config")

        with patch("kiro_crew.config.loader.KiroCrewConfig", mock_cls):
            result = _get_probe_timeout()
        assert result == _PROBE_TIMEOUT_SECS
        assert result == 15


class TestProbeServerTimeout:
    """Tests that probe_server uses _get_probe_timeout() and handles timeout."""

    @pytest.mark.asyncio
    async def test_probe_server_timeout_on_tools_list(self) -> None:
        """probe_server times out on tools/list (second readline), covering L456."""
        server = McpServerInfo(
            name="slow-server",
            command=sys.executable,
            args=["-c", "import time; time.sleep(999)"],
        )

        init_resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n"

        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.close = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=[init_resp, asyncio.TimeoutError])
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls,
        ):
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 42
            mock_cls.load.return_value = mock_cfg

            result = await probe_server(server)

        assert result.status == "error"
        assert result.error == "timeout"

    @pytest.mark.asyncio
    async def test_probe_server_config_fallback_on_error(self) -> None:
        """probe_server falls back to 15s when config loading fails."""
        server = McpServerInfo(name="test", command=sys.executable)

        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        # StreamWriter.write/close are synchronous while drain is async; stderr.read
        # is async but returns bytes. Model each boundary explicitly so AsyncMock
        # cannot invent a coroutine-returning bytes.decode() on the error path.
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.close = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls,
        ):
            mock_cls.load.side_effect = RuntimeError("corrupt config")

            result = await probe_server(server)

        assert result.status == "error"
        assert result.error == "timeout"


class TestProbeRemoteTimeout:
    """Test that _probe_remote uses _get_probe_timeout() for HTTP timeout."""

    @pytest.mark.asyncio
    async def test_probe_remote_timeout_uses_config(self) -> None:
        """Remote probe uses _get_probe_timeout() for aiohttp timeout."""
        server = McpServerInfo(name="remote", url="https://example.com/mcp")

        with (
            patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls,
            patch("aiohttp.ClientSession") as mock_session_cls,
        ):
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 60
            mock_cls.load.return_value = mock_cfg

            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.post = MagicMock(side_effect=asyncio.TimeoutError)
            mock_session_cls.return_value = mock_session

            result = await _probe_remote(server)

        assert result.status == "error"
        assert result.error == "timeout"
        # Verify the configured timeout was actually used
        timeout_used = mock_session_cls.call_args.kwargs.get("timeout")
        assert timeout_used is not None
        assert timeout_used.total == 60


class TestFixStaleManagedCommand:
    """Tests for _fix_stale_managed_command.

    The managed invocation is delegated to ``_kirocrew_mcp_invocation`` (the
    single source of truth), which returns a runnable ``(command, args)`` —
    either a standalone ``kirocrew`` binary (POSIX ``bin/kirocrew`` / Windows
    ``Scripts\\kirocrew.exe``) or the ``<interpreter> -m kiro_crew <sub>``
    fallback. ``_fix_stale_managed_command`` must rewrite BOTH command and args
    onto the spec (rewriting only the command silently dropped the fallback's
    args and spawned a bare ``kirocrew`` that isn't on PATH — the Windows
    ``command not found: kirocrew`` regression)."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        import kiro_crew.mcp_discovery as _d

        _d._resolved_managed_invocation = {}
        yield
        _d._resolved_managed_invocation = {}

    def test_rewrites_command_and_args_from_invocation(self):
        """Both command and args come from _kirocrew_mcp_invocation."""
        from kiro_crew.mcp_discovery import _fix_stale_managed_command

        spec = {"command": "/stale/bin/kirocrew", "args": ["mcp-core"]}
        with patch(
            "kiro_crew.agent._kirocrew_mcp_invocation",
            return_value=("/usr/local/bin/kirocrew", ["mcp-core"]),
        ) as inv:
            _fix_stale_managed_command("kirocrew-core", spec)
        inv.assert_called_once_with("mcp-core")
        assert spec["command"] == "/usr/local/bin/kirocrew"
        assert spec["args"] == ["mcp-core"]

    def test_applies_python_dash_m_fallback_with_args(self):
        """When no standalone binary resolves, the python -m kiro_crew fallback
        (command + its args) is applied — regression for Windows where rewriting
        the command alone left a bare 'kirocrew' that isn't on PATH."""
        from kiro_crew.mcp_discovery import _fix_stale_managed_command

        spec = {"command": "kirocrew", "args": []}
        with patch(
            "kiro_crew.agent._kirocrew_mcp_invocation",
            return_value=("/venv/Scripts/python.exe", ["-m", "kiro_crew", "mcp-cron"]),
        ):
            _fix_stale_managed_command("kirocrew-cron", spec)
        assert spec["command"] == "/venv/Scripts/python.exe"
        assert spec["args"] == ["-m", "kiro_crew", "mcp-cron"]

    def test_maps_each_managed_server_to_its_subcommand(self):
        from kiro_crew.mcp_discovery import _fix_stale_managed_command

        for name, sub in (("kirocrew-core", "mcp-core"), ("kirocrew-cron", "mcp-cron")):
            spec = {"command": "x", "args": []}
            with patch(
                "kiro_crew.agent._kirocrew_mcp_invocation", return_value=("/bin/kirocrew", [sub])
            ) as inv:
                _fix_stale_managed_command(name, spec)
            inv.assert_called_once_with(sub)
            assert spec["args"] == [sub]

    def test_skips_non_managed_server(self):
        from kiro_crew.mcp_discovery import _fix_stale_managed_command

        spec = {"command": "/nonexistent/path/other", "args": []}
        with patch("kiro_crew.agent._kirocrew_mcp_invocation") as inv:
            _fix_stale_managed_command("other-server", spec)
        inv.assert_not_called()
        assert spec["command"] == "/nonexistent/path/other"

    def test_caches_resolution_across_calls(self):
        """The invocation is resolved once and reused (no repeated subprocess
        work on every list_servers() call)."""
        from kiro_crew.mcp_discovery import _fix_stale_managed_command

        with patch(
            "kiro_crew.agent._kirocrew_mcp_invocation", return_value=("/bin/kirocrew", ["mcp-core"])
        ) as inv:
            _fix_stale_managed_command("kirocrew-core", {"command": "x", "args": []})
            _fix_stale_managed_command("kirocrew-core", {"command": "y", "args": []})
        inv.assert_called_once()  # cached after the first resolve

    def test_resolution_failure_leaves_spec_untouched(self):
        """If invocation resolution raises, the spec is left as-is (no crash)."""
        from kiro_crew.mcp_discovery import _fix_stale_managed_command

        spec = {"command": "/old/kirocrew", "args": ["mcp-core"]}
        with patch("kiro_crew.agent._kirocrew_mcp_invocation", side_effect=RuntimeError("boom")):
            _fix_stale_managed_command("kirocrew-core", spec)
        assert spec["command"] == "/old/kirocrew"


class TestSharedServerToolsRegistration:
    """Tests for shared MCP servers being added to tools/allowedTools."""

    def test_shared_servers_added_to_tools_and_allowedtools(self, tmp_path, monkeypatch) -> None:
        """Enabled shared servers appear in both tools and allowedTools."""
        from kiro_crew.agent import rebuild_agent_config

        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}))
        (agent_dir / "prompt.md").write_text("prompt")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "my-srv": {"command": "srv"},
                    }
                }
            )
        )

        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
        monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
        monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
        monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")
        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")

        rebuild_agent_config()

        data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
        assert "my-srv" in data["mcpServers"]
        assert "@my-srv" in data.get("tools", [])
        assert "@my-srv" in data.get("allowedTools", [])

    def test_disabled_shared_server_removed_from_tools(self, tmp_path, monkeypatch) -> None:
        """Disabled shared server is removed from tools/allowedTools."""
        from kiro_crew.agent import rebuild_agent_config

        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}))
        (agent_dir / "prompt.md").write_text("prompt")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        (kiro_dir / "kirocrew.json").write_text(
            json.dumps(
                {
                    "mcpServers": {"my-srv": {"command": "srv"}},
                    "tools": ["@my-srv"],
                    "allowedTools": ["@my-srv"],
                }
            )
        )

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "my-srv": {"command": "srv", "disabled": True},
                    }
                }
            )
        )

        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
        monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
        monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
        monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")
        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")

        rebuild_agent_config()

        data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
        assert "@my-srv" not in data.get("tools", [])
        assert "@my-srv" not in data.get("allowedTools", [])

    def test_reenabled_server_added_back(self, tmp_path, monkeypatch) -> None:
        """Server re-enabled in mcp.json gets added back to tools/allowedTools."""
        from kiro_crew.agent import rebuild_agent_config

        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}))
        (agent_dir / "prompt.md").write_text("prompt")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        (kiro_dir / "kirocrew.json").write_text(
            json.dumps(
                {
                    "mcpServers": {"my-srv": {"command": "srv", "disabled": True}},
                    "tools": [],
                    "allowedTools": [],
                }
            )
        )

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "my-srv": {"command": "srv"},
                    }
                }
            )
        )

        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
        monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
        monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
        monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")
        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")

        rebuild_agent_config()

        data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
        assert "@my-srv" in data.get("tools", [])
        assert "@my-srv" in data.get("allowedTools", [])
        assert "disabled" not in data["mcpServers"]["my-srv"]

    def test_disabled_removal_no_tools_key(self, tmp_path, monkeypatch) -> None:
        """Disabled removal doesn't crash when config has no tools key."""
        from kiro_crew.agent import rebuild_agent_config

        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}))
        (agent_dir / "prompt.md").write_text("prompt")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "disabled-srv": {"command": "srv", "disabled": True},
                    }
                }
            )
        )

        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
        monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
        monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
        monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")
        monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)

        rebuild_agent_config()

        data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
        assert "@disabled-srv" not in data.get("tools", [])
        assert "@disabled-srv" not in data.get("allowedTools", [])


class TestProbeServerStderrCapture:
    """`probe_server` drains child stderr on failure and appends a
    redacted tail to `server.error` so callers (doctor, dashboard) can
    surface the real cause instead of generic 'no response'/'timeout'.
    """

    @pytest.mark.asyncio
    async def test_stderr_captured_when_child_exits_before_response(self, tmp_path) -> None:
        """Child writes to stderr and exits without speaking MCP → stderr
        tail is appended to `server.error`."""
        from kiro_crew.mcp_discovery import probe_server

        stub = tmp_path / "broken-server.sh"
        stub.write_text(
            "#!/bin/sh\n" "echo 'ModuleNotFoundError: No module named foo' >&2\n" "exit 1\n"
        )
        stub.chmod(0o755)

        server = McpServerInfo(name="broken", command=str(stub))
        with patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls:
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 2
            mock_cls.load.return_value = mock_cfg

            result = await probe_server(server)

        assert result.status == "error"
        assert "stderr:" in (result.error or "")
        assert "ModuleNotFoundError" in (result.error or "")

    @pytest.mark.asyncio
    async def test_successful_probe_does_not_mention_stderr(self, tmp_path) -> None:
        """Healthy server's benign stderr warnings must not bleed into
        `server.error`."""
        from kiro_crew.mcp_discovery import probe_server

        stub = tmp_path / "noisy-ok-server.sh"
        stub.write_text(
            "#!/bin/sh\n"
            "echo 'WARNING: deprecated flag' >&2\n"
            "while IFS= read -r line; do\n"
            '  case "$line" in\n'
            '    *\\"initialize\\"*) '
            'printf \'{"jsonrpc":"2.0","id":1,"result":{}}\\n\' ;;\n'
            '    *\\"tools/list\\"*) '
            'printf \'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\\n\' ;;\n'
            "  esac\n"
            "done\n"
        )
        stub.chmod(0o755)

        server = McpServerInfo(name="noisy-ok", command=str(stub))
        with patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls:
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 3
            mock_cls.load.return_value = mock_cfg

            result = await probe_server(server)

        assert result.status == "ok", f"unexpected error: {result.error}"
        assert "stderr:" not in (result.error or "")
        assert "deprecated" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_stderr_tail_is_bounded(self, tmp_path) -> None:
        """Very large stderr is truncated so it cannot explode logs or
        dashboard responses."""
        from kiro_crew.mcp_discovery import probe_server

        stub = tmp_path / "verbose-broken.sh"
        stub.write_text(
            "#!/bin/sh\n"
            "for i in $(seq 1 200); do\n"
            "  echo 'this is a long diagnostic line that repeats many times' >&2\n"
            "done\n"
            "exit 1\n"
        )
        stub.chmod(0o755)

        server = McpServerInfo(name="verbose", command=str(stub))
        with patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls:
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 2
            mock_cls.load.return_value = mock_cfg

            result = await probe_server(server)

        assert result.status == "error"
        # 500-char stderr tail + 200-char error head + headers = well under 1KB.
        assert len(result.error or "") < 1024

    @pytest.mark.asyncio
    async def test_credential_in_stderr_is_redacted(self, tmp_path) -> None:
        """stderr is untrusted output — credentials and exfiltration URLs
        must be scrubbed before they land in `server.error`."""
        from kiro_crew.mcp_discovery import probe_server

        stub = tmp_path / "leaky-server.sh"
        stub.write_text(
            "#!/bin/sh\n"
            "echo 'config error: AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLEXXX' >&2\n"
            "exit 1\n"
        )
        stub.chmod(0o755)

        server = McpServerInfo(name="leaky", command=str(stub))
        with patch("kiro_crew.config.loader.KiroCrewConfig") as mock_cls:
            mock_cfg = MagicMock()
            mock_cfg.dashboard.mcp_probe_timeout_secs = 2
            mock_cls.load.return_value = mock_cfg

            result = await probe_server(server)

        assert result.status == "error"
        # The literal secret must not appear verbatim in the error field.
        assert "AKIAIOSFODNN7EXAMPLEXXX" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_long_probe_error_keeps_remedy_sentence(self, monkeypatch) -> None:
        """A long spawn exception must not be chopped mid-sentence.

        SandboxUnavailableError ends with the remedy naming
        agent.sandbox_allow_unsandboxed_exec; the old 200-char cap discarded
        it, so a Windows user saw '...Probe detail: not Linux. I' and no fix.
        """
        from kiro_crew.mcp_discovery import _PROBE_ERROR_MAX_CHARS, probe_server

        # A credential early in the message must be REDACTED (not merely
        # truncated away): raising the cap must not widen a disclosure hole.
        long_msg = (
            "Sandbox backend unavailable, token=AKIAIOSFODNN7EXAMPLEXXX. "
            "Probe detail: not Linux. "
            + ("x" * 300)
            + " set agent.sandbox_allow_unsandboxed_exec=true in ~/.kiro/crew/config.json"
        )
        assert len(long_msg) > 200  # the old cap would have chopped this

        server = McpServerInfo(name="srv", command="srv")

        # Resolve the command, then fail at the sandbox chokepoint with the long
        # message — the real path a Windows host takes with no sandbox backend.
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery.shutil.which", lambda *a, **k: "/usr/bin/srv"
        )

        def boom(*_a: object, **_k: object) -> object:
            raise RuntimeError(long_msg)

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", boom)
        result = await probe_server(server)

        assert result.status == "error"
        # The remedy sentence at the tail survives the (larger) cap.
        assert "sandbox_allow_unsandboxed_exec=true" in (result.error or "")
        assert len(result.error or "") <= _PROBE_ERROR_MAX_CHARS
        # The credential is redacted before it reaches server.error.
        assert "AKIAIOSFODNN7EXAMPLEXXX" not in (result.error or "")


class TestProbeStdioMalformedResponse:
    """Stdio probe must not crash on non-spec JSON-RPC response shapes.

    Regression for: MCP probe failed [...]: 'str' object has no attribute 'get'
    — some servers return an `error` value (or whole response) that is a bare
    string rather than the spec's {"message": ...} object / dict.
    """

    def setup_method(self) -> None:
        _clear_cache()

    def _make_proc(self, init_line: bytes, list_line: bytes = b"") -> MagicMock:
        proc = MagicMock()
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()
        proc.stdout = MagicMock()
        # Trailing b"" models a real stream reaching EOF. The probe's response
        # reader may consume more than one line (it skips banner/blank/non-
        # response lines), so the mock must yield EOF rather than exhausting
        # its side_effect and raising StopAsyncIteration.
        proc.stdout.readline = AsyncMock(side_effect=[init_line, list_line, b""])
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        return proc

    def test_init_error_as_string_does_not_crash(self, monkeypatch) -> None:
        """An `error` value that is a plain string is handled, not raised."""
        server = McpServerInfo(name="srv", command="srv")
        init_line = json.dumps({"jsonrpc": "2.0", "id": 1, "error": "boom"}).encode() + b"\n"
        proc = self._make_proc(init_line)

        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")
        with patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            result = asyncio.run(probe_server(server))

        assert result.status == "error"
        assert result.error == "boom"

    def test_tools_list_non_dict_does_not_crash(self, monkeypatch) -> None:
        """A tools/list response that parses to a bare string is a failed probe.

        The reader yields no response object for it (only dicts carrying an
        ``id`` count), so the probe reports the tools/list failure instead of
        certifying a server no session can get a tool out of.
        """
        server = McpServerInfo(name="srv", command="srv")
        init_line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n"
        list_line = json.dumps("unexpected-string").encode() + b"\n"
        proc = self._make_proc(init_line, list_line)

        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")
        with patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            result = asyncio.run(probe_server(server))

        assert result.status == "error"
        assert "tools/list" in result.error
        assert result.tools == []

    def test_tools_list_missing_tools_key_is_an_error(self, monkeypatch) -> None:
        """A dict result WITHOUT a tools list is malformed, not a tool-less
        server — green-with-zero-tools would certify a server whose one
        required answer didn't parse."""
        server = McpServerInfo(name="srv", command="srv")
        init_line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n"
        list_line = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}}).encode() + b"\n"
        proc = self._make_proc(init_line, list_line)

        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")
        with patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            result = asyncio.run(probe_server(server))

        assert result.status == "error"
        assert "malformed" in result.error


def _make_stream(lines: list[bytes]) -> asyncio.StreamReader:
    """Build a StreamReader pre-fed with ``lines`` and an EOF marker."""
    reader = asyncio.StreamReader()
    for chunk in lines:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


class TestReadStdioJsonrpcResponse:
    """_read_stdio_jsonrpc_response skips banner/blank/notification lines before the response."""

    @pytest.mark.asyncio
    async def test_immediate_response(self) -> None:
        stream = _make_stream([b'{"jsonrpc":"2.0","id":1,"result":{}}\n'])
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp == {"jsonrpc": "2.0", "id": 1, "result": {}}

    @pytest.mark.asyncio
    async def test_skips_leading_banner_line(self) -> None:
        """A non-JSON banner (the aim self-update case) is skipped, not fatal."""
        stream = _make_stream(
            [
                b"example-mcp v0.1.4 starting (backend: wss://mcp.example.com)\n",
                b'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\n',
            ]
        )
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 1

    @pytest.mark.asyncio
    async def test_skips_blank_lines(self) -> None:
        stream = _make_stream([b"\n", b"   \n", b'{"jsonrpc":"2.0","id":2,"result":{}}\n'])
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 2

    @pytest.mark.asyncio
    async def test_skips_notifications_without_id(self) -> None:
        """JSON-RPC notifications (no id) are not responses — keep reading."""
        stream = _make_stream(
            [
                b'{"jsonrpc":"2.0","method":"notifications/message","params":{}}\n',
                b'{"jsonrpc":"2.0","id":1,"result":{}}\n',
            ]
        )
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp["id"] == 1

    @pytest.mark.asyncio
    async def test_eof_returns_none(self) -> None:
        stream = _make_stream([b"just a banner, no json\n"])
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is None

    @pytest.mark.asyncio
    async def test_empty_stream_returns_none(self) -> None:
        stream = _make_stream([])
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is None

    @pytest.mark.asyncio
    async def test_banner_flood_capped(self) -> None:
        """More than _MAX_BANNER_LINES junk lines → give up (None), don't hang."""
        from kiro_crew.mcp_discovery import _MAX_BANNER_LINES

        lines = [b"noise\n"] * (_MAX_BANNER_LINES + 5)
        lines.append(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
        stream = _make_stream(lines)
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is None

    @pytest.mark.asyncio
    async def test_skips_non_object_json_payloads(self) -> None:
        """Bare string / array / number JSON lines are not responses — skip them."""
        stream = _make_stream(
            [
                b'"unexpected-string"\n',
                b"[1, 2, 3]\n",
                b"42\n",
                b'{"jsonrpc":"2.0","id":1,"result":{}}\n',
            ]
        )
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 1

    @pytest.mark.asyncio
    async def test_notifications_do_not_count_toward_cap(self) -> None:
        """>_MAX_BANNER_LINES JSON-RPC notifications must NOT trip the banner cap."""
        from kiro_crew.mcp_discovery import _MAX_BANNER_LINES

        notif = b'{"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n'
        lines = [notif] * (_MAX_BANNER_LINES + 10)
        lines.append(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
        stream = _make_stream(lines)
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 1

    @pytest.mark.asyncio
    async def test_blank_lines_do_not_count_toward_cap(self) -> None:
        """>_MAX_BANNER_LINES blank lines must NOT trip the banner cap."""
        from kiro_crew.mcp_discovery import _MAX_BANNER_LINES

        lines = [b"\n"] * (_MAX_BANNER_LINES + 10)
        lines.append(b'{"jsonrpc":"2.0","id":3,"result":{}}\n')
        stream = _make_stream(lines)
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 3

    @pytest.mark.asyncio
    async def test_cap_boundary_exact(self) -> None:
        """Exactly _MAX_BANNER_LINES junk lines still lets the response through."""
        from kiro_crew.mcp_discovery import _MAX_BANNER_LINES

        lines = [b"noise\n"] * _MAX_BANNER_LINES
        lines.append(b'{"jsonrpc":"2.0","id":7,"result":{}}\n')
        stream = _make_stream(lines)
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is not None
        assert resp["id"] == 7

    @pytest.mark.asyncio
    async def test_cap_boundary_one_over(self) -> None:
        """One junk line past the cap drops the response (returns None)."""
        from kiro_crew.mcp_discovery import _MAX_BANNER_LINES

        lines = [b"noise\n"] * (_MAX_BANNER_LINES + 1)
        lines.append(b'{"jsonrpc":"2.0","id":7,"result":{}}\n')
        stream = _make_stream(lines)
        resp = await _read_stdio_jsonrpc_response(stream, timeout=5)
        assert resp is None

    @pytest.mark.asyncio
    async def test_timeout_raises(self) -> None:
        """A stream that never yields a full line times out (mapped to 'timeout')."""
        reader = asyncio.StreamReader()  # no data, no EOF → readline blocks
        with pytest.raises(asyncio.TimeoutError):
            await _read_stdio_jsonrpc_response(reader, timeout=0.05)


class TestProbeServerBannerTolerance:
    """probe_server no longer errors when a banner precedes the handshake."""

    @pytest.mark.asyncio
    async def test_leading_banner_does_not_fail_probe(self) -> None:
        proc = AsyncMock()
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.stdout = AsyncMock()
        proc.stderr = AsyncMock()
        proc.stdout.readline = AsyncMock(
            side_effect=[
                b"example-mcp v0.1.4 starting (backend: wss://mcp.example.com)\n",
                b'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\n',
                b'{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"read"}]}}\n',
                b"",
            ]
        )
        server = McpServerInfo(name="local-chorus-mcp", command="local-chorus-mcp")

        with (
            patch("kiro_crew.mcp_discovery.asyncio.create_subprocess_exec", return_value=proc),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/local-chorus-mcp"),
        ):
            result = await probe_server(server)

        assert result.status == "ok"
        assert result.tools == ["read"]
        assert "Expecting value" not in result.error


# ── Probe process-group reap tests ───────────


class TestProbeGroupReap:
    """probe_server must own and reap a dedicated process group so launcher
    grandchildren (npx shim -> node MCP server) cannot leak."""

    def _make_mock_proc(self, pid: int = 4242) -> AsyncMock:
        proc = AsyncMock()
        proc.pid = pid
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.close = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.stdout = AsyncMock()
        proc.stdout.readline = AsyncMock(return_value=b"")
        return proc

    @pytest.mark.asyncio
    async def test_spawn_uses_start_new_session_on_posix(self) -> None:
        """The probe child must be its own session/process-group leader."""
        from kiro_crew import platform_compat

        if not platform_compat.IS_POSIX:
            pytest.skip("POSIX-only spawn flag")
        proc = self._make_mock_proc()
        server = McpServerInfo(name="test", command="echo")

        with (
            patch(
                "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
                return_value=proc,
            ) as mock_exec,
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
            patch("kiro_crew.mcp_discovery.os.killpg"),
        ):
            await probe_server(server)

        assert mock_exec.call_args.kwargs.get("start_new_session") is True

    @pytest.mark.asyncio
    async def test_teardown_reaps_process_group(self) -> None:
        """Even after a graceful leader exit, the whole group is SIGKILLed —
        a leader-only kill leaves npx/node grandchildren alive (the leaked
        MCP-tree accumulation)."""
        import signal as _signal

        from kiro_crew import platform_compat

        if not platform_compat.IS_POSIX:
            pytest.skip("killpg is POSIX-only")
        proc = self._make_mock_proc(pid=5151)
        server = McpServerInfo(name="test", command="echo")

        with (
            patch(
                "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
                return_value=proc,
            ),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
            patch("kiro_crew.mcp_discovery.os.killpg") as mock_killpg,
        ):
            await probe_server(server)

        mock_killpg.assert_called_once_with(5151, _signal.SIGKILL)

    @pytest.mark.asyncio
    async def test_teardown_tolerates_empty_group(self) -> None:
        """ESRCH (group already empty) must not surface as a probe error."""
        from kiro_crew import platform_compat

        if not platform_compat.IS_POSIX:
            pytest.skip("killpg is POSIX-only")
        proc = self._make_mock_proc()
        server = McpServerInfo(name="test", command="echo")

        with (
            patch(
                "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
                return_value=proc,
            ),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
            patch("kiro_crew.mcp_discovery.os.killpg", side_effect=ProcessLookupError),
        ):
            result = await probe_server(server)

        # teardown error handling must not clobber the probe result
        assert result.name == "test"

    @pytest.mark.asyncio
    async def test_teardown_refuses_non_int_pid(self) -> None:
        """Mock/sentinel pids must never coerce into killpg(1) == init."""
        from kiro_crew import platform_compat

        if not platform_compat.IS_POSIX:
            pytest.skip("killpg is POSIX-only")
        proc = self._make_mock_proc()
        proc.pid = MagicMock()  # non-int stand-in
        server = McpServerInfo(name="test", command="echo")

        with (
            patch(
                "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
                return_value=proc,
            ),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/echo"),
            patch("kiro_crew.mcp_discovery.os.killpg") as mock_killpg,
        ):
            await probe_server(server)

        mock_killpg.assert_not_called()

    @pytest.mark.asyncio
    async def test_real_grandchild_is_reaped(self, monkeypatch, tmp_path) -> None:
        """End-to-end: a probed 'server' that forks a grandchild and never
        answers must leave NO survivors after the probe returns."""
        import time as _time

        from kiro_crew import platform_compat

        if not platform_compat.IS_POSIX:
            pytest.skip("process groups are POSIX-only")

        grandchild_pid_file = tmp_path / "grandchild.pid"
        # Fake launcher: forks a long-lived grandchild (same process group),
        # writes its pid, then sleeps without ever answering the handshake —
        # modeling a launcher shim wedged mid-cold-start.
        script = tmp_path / "fake_launcher.sh"
        script.write_text(
            "#!/bin/sh\n" "sleep 300 &\n" f"echo $! > {grandchild_pid_file}\n" "sleep 300\n"
        )
        script.chmod(0o755)

        monkeypatch.setattr("kiro_crew.mcp_discovery._get_probe_timeout", lambda: 1)
        # The child deliberately never exits, so `probe_server`'s teardown pays its
        # graceful-exit budget AND its post-SIGKILL budget in full (2 x 5s) before
        # reaching the process-group reap this test is about. Shrink both: the reap
        # is what is asserted, and waiting out the real budget made this the single
        # slowest test in the suite at 12s.
        monkeypatch.setattr("kiro_crew.mcp_discovery._PROBE_TEARDOWN_WAIT_SECS", 0.5)
        server = McpServerInfo(name="fake", command=str(script))
        result = await probe_server(server)
        assert result.status == "error"  # timed out, as designed

        deadline = _time.monotonic() + 5
        gc_pid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())
        while _time.monotonic() < deadline:
            # Windows-safe liveness probe (a raw os.kill(pid, 0) TERMINATES the
            # target on Windows — the platform_compat rule); this test is
            # POSIX-gated, but route through the shim to stay consistent.
            if platform_compat.pid_liveness(gc_pid) == platform_compat.PID_DEAD:
                break  # grandchild reaped — pass
            _time.sleep(0.1)
        else:
            platform_compat.kill_pid(gc_pid, platform_compat.SIGKILL)  # cleanup
            pytest.fail("grandchild survived probe teardown — process-group reap regressed")


class TestDisabledIsCrossScope:
    """``McpServerInfo.disabled`` must reflect a ``disabled: true`` in ANY scope.

    ``/api/mcp/toggle`` writes the flag into the Kiro-global ``mcp.json``, but the
    merge only marked rows introduced from the Kiro Crew scope. A server also
    present in the agent config was therefore introduced first with
    ``disabled = False`` and stayed probeable after the user switched it off —
    and now that ``probe_server`` keys its refusal on this flag, under-reporting
    it is the whole bypass.
    """

    def setup_method(self) -> None:
        _clear_cache()

    @staticmethod
    def _env(tmp_path, monkeypatch, *, agent_spec, global_spec):
        """Agent config introduces the row; Kiro-global mcp.json disables it."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(
            json.dumps({"mcpServers": {"srv": agent_spec}}), encoding="utf-8"
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        kiro_mcp = tmp_path / "kiro-mcp.json"
        kiro_mcp.write_text(
            json.dumps({"mcpServers": {"srv": global_spec}}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (kiro_mcp,))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES", ((kiro_mcp, SCOPE_KIRO_GLOBAL),)
        )

    def test_kiro_global_disable_marks_an_agent_introduced_row(
        self, tmp_path, monkeypatch
    ) -> None:
        self._env(
            tmp_path,
            monkeypatch,
            agent_spec={"command": "node"},
            global_spec={"command": "node", "disabled": True},
        )
        rows = {s.name: s for s in list_servers()}
        assert "srv" in rows, "the row must still be listed so it can be re-enabled"
        assert rows["srv"].disabled is True

    def test_enabled_everywhere_stays_enabled(self, tmp_path, monkeypatch) -> None:
        """Guard against the fix over-reaching into a false positive."""
        self._env(
            tmp_path,
            monkeypatch,
            agent_spec={"command": "node"},
            global_spec={"command": "node"},
        )
        rows = {s.name: s for s in list_servers()}
        assert rows["srv"].disabled is False

    @pytest.mark.asyncio
    async def test_probe_server_refuses_a_cross_scope_disabled_row(
        self, tmp_path, monkeypatch
    ) -> None:
        """End-to-end: the populated flag reaches the chokepoint and withholds
        the spawn. Without the cross-scope fix the row arrives enabled and
        ``probe_server`` would run the command."""
        self._env(
            tmp_path,
            monkeypatch,
            agent_spec={"command": "node"},
            global_spec={"command": "node", "disabled": True},
        )
        spawned = []

        async def _no_spawn(*a, **k):
            spawned.append(a)
            raise AssertionError("a disabled server must never be spawned")

        # Patch the actual spawn primitive (the stdio path is inline in
        # probe_server, not a helper), so this asserts on the real side effect
        # consent gates rather than on a stand-in.
        monkeypatch.setattr("kiro_crew.mcp_discovery.create_subprocess_limited", _no_spawn)
        row = next(s for s in list_servers() if s.name == "srv")
        out = await probe_server(row)
        assert out.status == "disabled"
        assert spawned == []

    def test_raw_scoped_key_disable_marks_the_canonical_row(
        self, tmp_path, monkeypatch
    ) -> None:
        """Row names are CANONICALIZED (step 3b): ``npm:@playwright/mcp`` is
        reported as ``playwright-mcp``. Scope dicts stay keyed by the raw name,
        so matching before canonicalization misses a raw-keyed disable whenever
        the agent config retained the canonical row — the row would arrive
        enabled and probe_server would spawn it."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(
            json.dumps({"mcpServers": {"playwright-mcp": {"command": "npx"}}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        kiro_mcp = tmp_path / "kiro-mcp.json"
        kiro_mcp.write_text(
            json.dumps(
                {"mcpServers": {"npm:@playwright/mcp": {"command": "npx", "disabled": True}}}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (kiro_mcp,))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES", ((kiro_mcp, SCOPE_KIRO_GLOBAL),)
        )
        rows = {s.name: s for s in list_servers()}
        assert "playwright-mcp" in rows
        assert rows["playwright-mcp"].disabled is True


class TestWindowsTeardownOffLoop:
    """The Windows probe teardown must not run ``taskkill`` on the event loop.

    ``platform_compat.kill_process_tree`` shells out to ``taskkill /T /F`` via a
    blocking ``subprocess.run`` on Windows. Awaited inline that stalls the loop
    once per failed probe, and ``probe_all`` fans out across every configured
    server -- so several unreachable servers serialize that many process spawns
    onto the loop and the dashboard health check starts dropping.

    Asserted against the SHIPPED SOURCE rather than by simulating a Windows run:
    the branch is unreachable on this platform (``IS_WINDOWS`` is False), so a
    behavioural test here would pass no matter what the code did.
    """

    def test_kill_process_tree_is_offloaded(self) -> None:
        import inspect

        from kiro_crew import mcp_discovery

        src = inspect.getsource(mcp_discovery.probe_server)
        assert "kill_process_tree" in src, "teardown moved -- retarget this guard"
        # Every kill_process_tree call in the probe path must be wrapped.
        for line in src.splitlines():
            if "kill_process_tree" in line and not line.strip().startswith("#"):
                assert "to_thread" in line or "platform_compat.kill_process_tree," in line, (
                    f"kill_process_tree called on the loop: {line.strip()}"
                )
        assert "asyncio.to_thread(" in src


class TestProbeSandboxUnavailable:
    """A probe that could not RUN must not be reported as a broken server.

    kiro-cli launches MCP servers from the agent config without going through
    this probe, so on a host with no sandbox backend (any Windows host, macOS
    >= 26) the servers work while the probe cannot spawn them. Reporting that as
    an ordinary server fault renders every row red with "0 tools" and sends the
    user debugging a server that is fine.
    """

    @pytest.mark.asyncio
    async def test_sandbox_refusal_is_reported_as_a_probe_limitation(self, monkeypatch) -> None:
        import kiro_crew.mcp_discovery as md
        from kiro_crew.sandbox import SandboxUnavailableError

        monkeypatch.setattr(md, "_probe_sandbox_warned", set())

        def _refuse(*args, **kwargs):
            raise SandboxUnavailableError(
                "Sandbox backend unavailable and allow_unsandboxed_exec is not set.",
                kind="no_backend",
                detail="not Linux",
            )

        # A THIRD-PARTY server: managed ones never reach the spawn path at all
        # (their tools are read in-process), so they cannot exercise this branch.
        server = McpServerInfo(name="playwright-mcp", command="node")
        with patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _refuse), patch(
            "kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/node"
        ):
            result = await probe_server(server)

        # Machine-readable prefix so a presentation layer can tell this apart from
        # a genuine handshake failure without parsing prose.
        assert result.error.startswith("mcp_probe_sandbox_unavailable:"), result.error
        assert "server itself may be fine" in result.error, result.error
        assert "sandbox_allow_unsandboxed_exec" in result.error, result.error

    @pytest.mark.asyncio
    async def test_a_managed_server_is_still_spawned_when_the_sandbox_works(self) -> None:
        """The spawn is the only thing that proves the server can START.

        `_fix_stale_managed_command` exists because the managed invocation does go
        stale ("command not found: kirocrew; the built-in cron/core tools then never
        load"), and the probe was the one surface that caught it. Short-circuiting
        on the server name would report `ok` for a managed server that cannot run —
        silently changing what `ok` means in the shared `_cache_probe` store.
        """
        spawned: dict[str, bool] = {}

        def _wrap(argv, **kwargs):
            spawned["yes"] = True
            raise RuntimeError("stop at the wrap")

        server = McpServerInfo(name="kirocrew-core", command="kirocrew", args=["mcp-core"])
        with patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap), patch(
            "kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/kirocrew"
        ):
            await probe_server(server)

        assert spawned.get("yes") is True, "a working sandbox must still be used"

    @pytest.mark.asyncio
    async def test_a_managed_server_falls_back_to_its_declaration_with_no_backend(
        self, monkeypatch
    ) -> None:
        """No backend: serve the declared list rather than an error.

        This is what removes the opt-in for a read-only listing. The import runs
        package code in the gateway process, which is only acceptable BECAUSE the
        sandbox could not confine anything on this host anyway — hence fallback,
        never primary.
        """
        import kiro_crew.mcp_discovery as md
        from kiro_crew.sandbox import SandboxUnavailableError

        monkeypatch.setattr(md, "_managed_in_process_warned", set())

        def _refuse(*args, **kwargs):
            raise SandboxUnavailableError("no backend", kind="no_backend", detail="not Linux")

        for name, expect_tools in (("kirocrew-core", True), ("kirocrew-cron", True)):
            server = McpServerInfo(name=name, command="kirocrew", args=["mcp-x"])
            with patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _refuse), patch(
                "kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/kirocrew"
            ):
                result = await probe_server(server)

            assert result.status == "ok", (name, result.error)
            assert bool(result.tools) is expect_tools, (name, len(result.tools))

    @pytest.mark.asyncio
    async def test_a_third_party_server_gets_no_declaration_fallback(self) -> None:
        """Only OUR OWN servers have a declaration to read; a third-party one keeps
        the honest probe-limitation error."""
        from kiro_crew.sandbox import SandboxUnavailableError

        def _refuse(*args, **kwargs):
            raise SandboxUnavailableError("no backend", kind="no_backend", detail="not Linux")

        server = McpServerInfo(name="playwright-mcp", command="node")
        with patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _refuse), patch(
            "kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/node"
        ):
            result = await probe_server(server)

        assert result.status == "error"
        assert result.error.startswith("mcp_probe_sandbox_unavailable:"), result.error

    @pytest.mark.asyncio
    async def test_the_remedy_paragraph_is_logged_once_per_server(
        self, monkeypatch, caplog
    ) -> None:
        """The cause is the HOST, so it recurs every cycle for every server.

        Unbounded, a four-server config logged four identical multi-line remedy
        paragraphs per discovery cycle, forever.
        """
        import logging

        import kiro_crew.mcp_discovery as md

        monkeypatch.setattr(md, "_probe_sandbox_warned", set())
        with caplog.at_level(logging.WARNING, logger=md.logger.name):
            md._warn_probe_sandbox_unavailable_once("kirocrew-core")
            md._warn_probe_sandbox_unavailable_once("kirocrew-core")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert "probe skipped" in warnings[0].getMessage()


class TestFirstPartyManagedArgv:
    """The probe passes ``first_party_fixed_argv`` ONLY for a self-derived argv.

    The flag buys an unconfined spawn on a backend-less host (issue #1563
    carve-out), so it must key on the INVOCATION this package derives for its
    own managed servers — never on the server name alone, which an mcp.json
    scope could pair with user-config command text.
    """

    _INVOCATION = ("/opt/kirocrew/bin/kirocrew", ["mcp-core"])

    def _patch_invocation(self, monkeypatch) -> None:
        import kiro_crew.mcp_discovery as md

        monkeypatch.setattr(
            md, "_resolved_managed_invocation", {"kirocrew-core": self._INVOCATION}
        )
        # Default install: the package-derived managed env is empty.
        monkeypatch.setattr("kiro_crew.agent._managed_mcp_env", lambda: {})

    def test_self_derived_managed_argv_is_first_party(self, monkeypatch) -> None:
        import kiro_crew.mcp_discovery as md

        self._patch_invocation(monkeypatch)
        assert md._is_first_party_managed_argv(
            "kirocrew-core", self._INVOCATION[0], list(self._INVOCATION[1]), {}
        )

    def test_customized_command_under_a_managed_name_is_not(self, monkeypatch) -> None:
        """A managed NAME with user-config command text (the mcp.json-sourced
        case, which ``_fix_stale_managed_command`` never re-resolves) must keep
        the full fail-close + opt-in behavior."""
        import kiro_crew.mcp_discovery as md

        self._patch_invocation(monkeypatch)
        assert not md._is_first_party_managed_argv(
            "kirocrew-core", "/home/user/evil-shim", list(self._INVOCATION[1]), {}
        )
        assert not md._is_first_party_managed_argv(
            "kirocrew-core", self._INVOCATION[0], ["mcp-core", "--extra"], {}
        )

    def test_spec_env_under_a_managed_name_is_not_first_party(self, monkeypatch) -> None:
        """Env is an execution vector for the SAME argv (``LD_PRELOAD`` decides
        what code runs), and ``probe_server`` merges the spec's env into the
        child environment — so any key this package did not derive disqualifies
        the spec from the unconfined carve-out."""
        import kiro_crew.mcp_discovery as md

        self._patch_invocation(monkeypatch)
        assert not md._is_first_party_managed_argv(
            "kirocrew-core",
            self._INVOCATION[0],
            list(self._INVOCATION[1]),
            {"LD_PRELOAD": "/tmp/evil.so"},
        )

    def test_the_package_derived_home_pin_still_matches(self, monkeypatch) -> None:
        """Under an override home the managed spec legitimately carries exactly
        the ``KIROCREW_HOME`` pin this package derived — that must still count
        as first-party, and any EXTRA key alongside it must not."""
        import kiro_crew.mcp_discovery as md

        self._patch_invocation(monkeypatch)
        pin = {"KIROCREW_HOME": "/data/override-home"}
        monkeypatch.setattr("kiro_crew.agent._managed_mcp_env", lambda: dict(pin))
        assert md._is_first_party_managed_argv(
            "kirocrew-core", self._INVOCATION[0], list(self._INVOCATION[1]), dict(pin)
        )
        assert not md._is_first_party_managed_argv(
            "kirocrew-core",
            self._INVOCATION[0],
            list(self._INVOCATION[1]),
            {**pin, "LD_PRELOAD": "/tmp/evil.so"},
        )
        # A spec MISSING the derived pin is also not the derived invocation.
        assert not md._is_first_party_managed_argv(
            "kirocrew-core", self._INVOCATION[0], list(self._INVOCATION[1]), {}
        )

    def test_the_interpreter_fallback_is_never_first_party(self, monkeypatch) -> None:
        """`python -m kiro_crew` prepends the child's CWD to sys.path (3.10 has
        no -P), so a planted `kiro_crew/` tree in an untrusted cwd would shadow
        the install — only a resolved console-script binary qualifies."""
        import sys

        import kiro_crew.mcp_discovery as md

        fallback = (sys.executable, ["-m", "kiro_crew", "mcp-core"])
        monkeypatch.setattr(md, "_resolved_managed_invocation", {"kirocrew-core": fallback})
        monkeypatch.setattr("kiro_crew.agent._managed_mcp_env", lambda: {})
        assert not md._is_first_party_managed_argv(
            "kirocrew-core", fallback[0], list(fallback[1]), {}
        )

    def test_third_party_server_is_never_first_party(self, monkeypatch) -> None:
        import kiro_crew.mcp_discovery as md

        self._patch_invocation(monkeypatch)
        assert not md._is_first_party_managed_argv("playwright-mcp", "node", [], {})

    def test_resolution_failure_fails_toward_not_first_party(self, monkeypatch) -> None:
        import kiro_crew.mcp_discovery as md

        monkeypatch.setattr(md, "_resolved_managed_invocation", {})

        def _boom(subcommand):
            raise RuntimeError("no install")

        monkeypatch.setattr("kiro_crew.agent._kirocrew_mcp_invocation", _boom)
        assert not md._is_first_party_managed_argv(
            "kirocrew-core", self._INVOCATION[0], list(self._INVOCATION[1]), {}
        )

    @pytest.mark.asyncio
    async def test_probe_passes_the_flag_for_a_self_derived_managed_server(
        self, monkeypatch
    ) -> None:
        self._patch_invocation(monkeypatch)
        seen: dict[str, bool] = {}

        def _capture(argv, **kwargs):
            seen["flag"] = kwargs.get("first_party_fixed_argv", False)
            raise RuntimeError("stop at the wrap")

        server = McpServerInfo(
            name="kirocrew-core", command=self._INVOCATION[0], args=list(self._INVOCATION[1])
        )
        with patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _capture), patch(
            "kiro_crew.mcp_discovery.shutil.which", return_value=self._INVOCATION[0]
        ):
            await probe_server(server)

        assert seen["flag"] is True

    @pytest.mark.asyncio
    async def test_probe_passes_false_for_a_third_party_server(self, monkeypatch) -> None:
        self._patch_invocation(monkeypatch)
        seen: dict[str, bool] = {}

        def _capture(argv, **kwargs):
            # Omitting the synchronous API's default is equivalent to passing
            # False and keeps narrow injected preparation seams compatible.
            seen["flag"] = kwargs.get("first_party_fixed_argv", False)
            raise RuntimeError("stop at the wrap")

        server = McpServerInfo(name="playwright-mcp", command="node")
        with patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _capture), patch(
            "kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/node"
        ):
            await probe_server(server)

        assert seen["flag"] is False


class TestNoteDeniedEnv:
    """A red badge caused by policy must say so on the badge's own surface."""

    def _srv(self, **kw):
        s = McpServerInfo(name="py-srv", command="server", args=[])
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_failed_probe_names_the_dropped_key(self) -> None:
        s = self._srv(status="error", error="exec failed", env={"PYTHONPATH": "/srv/lib"})
        _note_denied_env(s)
        assert "PYTHONPATH" in s.error
        assert "exec failed" in s.error, "the original cause must survive"
        assert "a session still does" in s.error, "must say where it DOES work"

    def test_successful_probe_is_left_alone(self) -> None:
        """The drop changed nothing worth reporting when the server came up."""
        s = self._srv(status="ok", error=None, env={"PYTHONPATH": "/srv/lib"})
        _note_denied_env(s)
        assert s.error is None

    def test_failure_without_denied_keys_is_unchanged(self) -> None:
        s = self._srv(status="error", error="command not found: server", env={"TOKEN": "t"})
        _note_denied_env(s)
        assert s.error == "command not found: server"

    def test_missing_env_does_not_raise(self) -> None:
        s = self._srv(status="error", error="boom", env=None)
        _note_denied_env(s)
        assert s.error == "boom"
