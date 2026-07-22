"""Tests for MCP discovery module."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.mcp_discovery import (
    McpServerInfo,
    _cache_probe,
    _get_cached,
    _probe_cache,
    _probe_remote,
    _read_jsonrpc_response,
    _read_stdio_jsonrpc_response,
    discover_servers_to_sync,
    list_servers,
    probe_server,
    sync_to_agent_config,
)


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

    def test_remote_server_fields(self) -> None:
        info = McpServerInfo(
            name="deepwiki",
            url="https://mcp.deepwiki.com/mcp",
            headers={"Authorization": "Bearer tok"},
        )
        assert info.is_remote is True
        assert info.command == ""
        d = info.to_dict()
        assert d["url"] == "https://mcp.deepwiki.com/mcp"
        assert d["headers"] == {"Authorization": "Bearer tok"}

    def test_is_remote_false_for_local(self) -> None:
        info = McpServerInfo(name="x", command="cmd")
        assert info.is_remote is False

    def test_is_remote_false_when_both(self) -> None:
        """If both url and command are set, treat as local (command takes precedence)."""
        info = McpServerInfo(name="x", command="cmd", url="http://localhost")
        assert info.is_remote is False


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

    def test_list_skips_disabled_mcp_json_servers(self, tmp_path, monkeypatch) -> None:
        """Disabled servers in mcp.json are also excluded."""
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
        names = {s.name for s in servers}
        assert "active" in names
        assert "inactive" not in names

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


class TestSyncToAgentConfig:
    def test_sync_uses_kiro_cli(self, tmp_path, monkeypatch) -> None:
        """sync_to_agent_config calls kiro-cli mcp add --agent kirocrew for new servers."""
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

        new_srv = McpServerInfo(name="new-srv", command="b", args=["--x"])
        ok = sync_to_agent_config([new_srv])
        assert ok is True
        assert len(calls) == 1
        assert "--agent" in calls[0]
        assert "kirocrew" in calls[0]
        assert "new-srv" in calls[0]

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

    def test_sync_remote_server_skips_kiro_cli(self, tmp_path, monkeypatch) -> None:
        """Remote servers skip kiro-cli mcp add (no command to register)."""
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

        remote = McpServerInfo(name="deepwiki", url="https://mcp.deepwiki.com/mcp")
        local = McpServerInfo(name="local-srv", command="some-cmd")
        sync_to_agent_config([remote, local])

        # Only local new server gets kiro-cli registration
        assert len(calls) == 1
        assert "local-srv" in calls[0]

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
        status, tools, error = _get_cached("nonexistent")
        assert status == "unknown"
        assert tools == []
        assert error == ""

    def test_cache_hit_within_ttl(self) -> None:
        server = McpServerInfo(
            name="test-srv", command="x", status="ok", tools=["t1", "t2"], error=""
        )
        _cache_probe(server)
        status, tools, error = _get_cached("test-srv")
        assert status == "ok"
        assert tools == ["t1", "t2"]
        assert error == ""

    def test_cache_expired_returns_outdated_with_tools(self, monkeypatch) -> None:
        server = McpServerInfo(
            name="test-srv", command="x", status="ok", tools=["t1", "t2"], error=""
        )
        _cache_probe(server)
        # Simulate expiry by backdating probed_at
        _probe_cache["test-srv"].probed_at = time.monotonic() - 2000
        status, tools, error = _get_cached("test-srv")
        assert status == "outdated"
        assert tools == ["t1", "t2"]
        assert error == ""

    def test_cache_error_preserved(self) -> None:
        server = McpServerInfo(
            name="err-srv", command="x", status="error", tools=[], error="timeout"
        )
        _cache_probe(server)
        status, tools, error = _get_cached("err-srv")
        assert status == "error"
        assert error == "timeout"

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
        init_resp.json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
        init_resp.__aenter__ = AsyncMock(return_value=init_resp)
        init_resp.__aexit__ = AsyncMock(return_value=False)

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
        mock_session.post = MagicMock(side_effect=[init_resp, tools_resp])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=mock_session):
            result = await _probe_remote(server)

        assert result.status == "ok"
        assert result.tools == ["search", "read"]

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


class TestProbeServerProcessCleanup:
    """Tests for the finally block that tears down the probed subprocess."""

    def _make_mock_proc(self, *, wait_side_effect=None):
        proc = AsyncMock()
        proc.returncode = None  # process still running
        proc.stdin = MagicMock()
        proc.stdin.close = MagicMock()
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
            wait_side_effect=[asyncio.TimeoutError(), AsyncMock(return_value=0)()]
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

        data = json.loads((kiro_dir / "kirocrew.json").read_text())
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

        data = json.loads((kiro_dir / "kirocrew.json").read_text())
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
        server = McpServerInfo(name="slow-server", command="sleep", args=["999"])

        init_resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n"

        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.close = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=[init_resp, asyncio.TimeoutError])
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
        server = McpServerInfo(name="test", command="echo")

        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.close = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=asyncio.TimeoutError)
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

        data = json.loads((kiro_dir / "kirocrew.json").read_text())
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

        data = json.loads((kiro_dir / "kirocrew.json").read_text())
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

        data = json.loads((kiro_dir / "kirocrew.json").read_text())
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

        data = json.loads((kiro_dir / "kirocrew.json").read_text())
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
        """A tools/list response that parses to a bare string yields no tools."""
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

        assert result.status == "ok"
        assert result.tools == []


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
        server = McpServerInfo(name="fake", command=str(script))
        result = await probe_server(server)
        assert result.status == "error"  # timed out, as designed

        deadline = _time.monotonic() + 5
        gc_pid = int(grandchild_pid_file.read_text().strip())
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
