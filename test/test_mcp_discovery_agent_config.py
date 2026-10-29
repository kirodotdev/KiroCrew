"""MCP discovery: the servers ``install_agent`` writes into the agent config.

``install_agent`` keeps remote url-based servers and merges the Kiro-global ``mcp.json``; a
stale managed command is re-resolved through ``_kirocrew_mcp_invocation``; shared servers
join ``tools`` and ``allowedTools`` unless a scope disables them; and the in-process
managed-tool read returns names only.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)


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
        """When no standalone binary resolves, the python -s -m kiro_crew fallback
        (command + its args) is applied. On Windows, rewriting the command alone
        would leave a bare 'kirocrew' that isn't on PATH."""
        from kiro_crew.mcp_discovery import _fix_stale_managed_command

        spec = {"command": "kirocrew", "args": []}
        with patch(
            "kiro_crew.agent._kirocrew_mcp_invocation",
            return_value=("/venv/Scripts/python.exe", ["-s", "-m", "kiro_crew", "mcp-cron"]),
        ):
            _fix_stale_managed_command("kirocrew-cron", spec)
        assert spec["command"] == "/venv/Scripts/python.exe"
        assert spec["args"] == ["-s", "-m", "kiro_crew", "mcp-cron"]

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
            "kiro_crew.agent._kirocrew_mcp_invocation", return_value=(sys.executable, ["mcp-core"])
        ) as inv:
            first = {"command": "x", "args": []}
            second = {"command": "y", "args": []}
            _fix_stale_managed_command("kirocrew-core", first)
            _fix_stale_managed_command("kirocrew-core", second)
        inv.assert_called_once()  # cached after the first resolve
        # The second spec is repaired from the cached answer, not left stale.
        assert first == second == {"command": sys.executable, "args": ["mcp-core"]}

    def test_a_cached_command_from_a_pruned_install_is_re_resolved(self, tmp_path):
        """A cached launcher whose install an update removed is not served
        again: it is evicted and the spec is re-resolved to the current one."""
        import shutil as _shutil

        from kiro_crew.mcp_discovery import _fix_stale_managed_command

        old_install = tmp_path / "0.8.0.4"
        old_bin = old_install / "bin" / "kirocrew"
        old_bin.parent.mkdir(parents=True)
        old_bin.write_text("#!/bin/sh\n")
        new_bin = tmp_path / "0.8.0.6" / "bin" / "kirocrew"
        with patch(
            "kiro_crew.agent._kirocrew_mcp_invocation",
            side_effect=[(str(old_bin), ["mcp-core"]), (str(new_bin), ["mcp-core"])],
        ) as inv:
            first = {"command": "x", "args": []}
            _fix_stale_managed_command("kirocrew-core", first)
            assert first["command"] == str(old_bin)
            _shutil.rmtree(old_install)  # the update's prune
            second = {"command": str(old_bin), "args": ["mcp-core"]}
            _fix_stale_managed_command("kirocrew-core", second)
        assert inv.call_count == 2
        assert second["command"] == str(new_bin)

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

    @pytest.mark.parametrize("value", ["false", None, 0])
    def test_non_boolean_disabled_shared_server_is_not_mounted(
        self, tmp_path, monkeypatch, caplog, value
    ) -> None:
        """The mount-side read shares the launch predicate: a shared entry whose
        ``disabled`` is not a boolean is read FAIL-CLOSED and stripped from
        ``tools``/``allowedTools`` exactly as a literal ``true`` is, so the row
        the dashboard shows as Disabled is a server no session mounts. Red for
        ``null`` and ``0`` under the truthiness read this replaced, which
        mounted -- and auto-approved -- them.

        The rebuild also SAYS why, through the same bounded warn-once ledger the
        listing uses: a headless install rebuilds without a dashboard read, and
        would otherwise strip the mount silently. One line per (server, value
        shape) whichever path reports first, so a second rebuild is quiet."""
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
            json.dumps({"mcpServers": {"my-srv": {"command": "srv", "disabled": value}}})
        )

        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
        monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
        monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
        monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")
        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_reported", set())
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_cap_reached", False)

        with caplog.at_level(logging.WARNING):
            rebuild_agent_config()
            rebuild_agent_config()

        data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
        assert "@my-srv" not in data.get("tools", []), value
        assert "@my-srv" not in data.get("allowedTools", []), value
        # The rendered entry carries the boolean the schema wants, never the raw
        # value: truthiness sent ``null``/``0`` down the mount arm, which popped
        # the key, so the fail-closed arm is the one place the raw value could
        # otherwise reach the file kiro-cli loads.
        rendered = data["mcpServers"]["my-srv"]
        assert rendered.get("disabled") is True, (value, rendered)
        lines = [
            r.getMessage()
            for r in caplog.records
            if "'disabled'" in r.getMessage() and "not a boolean" in r.getMessage()
        ]
        assert len(lines) == 1, lines
        assert "'my-srv'" in lines[0] and "kiro-global" in lines[0]
        assert "DISABLED" in lines[0]

    def test_a_higher_priority_false_never_argues_a_lower_scope_mute_away(
        self, tmp_path, monkeypatch
    ) -> None:
        """Three sources: the shared file mutes ``my-srv`` with ``null`` (read
        fail-closed), the store says ``disabled: false`` and wins the merge, and
        the agent config keeps a selective ``@my-srv/tool`` ref. The rendered
        entry must say ``disabled: true`` -- the multi-scope predicate: one mute
        anywhere mutes, and priority never un-mutes -- or kiro-cli launches a
        server every surface calls muted. Red under the non-boolean-only
        coercion: the merged ``false`` passed the ``isinstance(..., bool)`` check
        and was emitted as ``disabled: false``."""
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
                    "tools": ["@my-srv/tool"],
                    "allowedTools": ["@my-srv"],
                }
            )
        )
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "mcp.json").write_text(
            json.dumps({"mcpServers": {"my-srv": {"command": "srv", "disabled": None}}})
        )
        store_dir = tmp_path / "crew"
        store_dir.mkdir()
        (store_dir / "mcp.json").write_text(
            json.dumps({"mcpServers": {"my-srv": {"command": "srv", "disabled": False}}})
        )

        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
        monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
        monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
        monkeypatch.setattr("kiro_crew.agent._user_dir", lambda: store_dir)
        monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")
        monkeypatch.setattr("shutil.which", lambda cmd, path=None: "/usr/bin/srv")
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_reported", set())
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_cap_reached", False)

        rebuild_agent_config()

        data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
        rendered = data["mcpServers"]["my-srv"]
        assert rendered.get("disabled") is True, rendered
        assert "@my-srv" not in data.get("allowedTools", [])
        # The predicate itself, in isolation: any mute wins over any ``false``.
        from kiro_crew.mcp_cleanup import mcp_entries_muted

        assert mcp_entries_muted([{"disabled": False}, {"disabled": None}]) is True
        assert mcp_entries_muted([{"disabled": False}, {"command": "x"}]) is False
        assert mcp_entries_muted([]) is False

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


class TestManagedToolsInProcessNamesOnly:
    """The in-process managed-tool read keeps only NAMES, so it must take a
    names-only path that never assembles descriptions — so neither builder
    needs a ``get_running_loop`` skip.
    """

    def test_core_names_are_correct_and_complete(self, monkeypatch) -> None:
        """The names returned match a full ``_list_tools`` build exactly, in order.

        Every app is held enabled for the comparison: the full build omits a
        disabled app's tools (``mcp_tools.apps.advertised``), while the names-only
        build takes the declaration and names them all without reading enablement
        -- the one state in which the two legitimately differ.
        """
        import kiro_crew.mcp_core as core
        import kiro_crew.mcp_tools.apps as apps_tools
        from kiro_crew.mcp_discovery import _managed_tools_in_process

        monkeypatch.setattr(apps_tools, "app_enabled_state", lambda _name: True)
        full = [t["name"] for t in core._list_tools()]
        got = _managed_tools_in_process("kirocrew-core")
        assert got == full
        assert len(got) == len(set(got))  # no duplicates

    def test_the_live_value_builders_are_not_invoked(self) -> None:
        """The two descriptions that reach for a live value — the agents-directory
        scan in ``spawn`` and the config read in ``control`` — must NOT run on the
        names-only path, because that caller discards every description.

        Spying the expensive step directly (per the issue's acceptance): the
        directory scan (``spawn.mcp_core.list_agents``) and the config read
        (``control.KiroCrewConfig.load``) are asserted to receive zero calls.
        """
        import kiro_crew.mcp_tools.control as control
        import kiro_crew.mcp_tools.spawn as spawn
        from kiro_crew.mcp_discovery import _managed_tools_in_process

        scan = MagicMock(return_value=[])
        cfg_load = MagicMock(side_effect=AssertionError("config read ran on names-only path"))
        with (
            patch.object(spawn.mcp_core, "list_agents", scan),
            patch.object(control.KiroCrewConfig, "load", classmethod(lambda cls: cfg_load())),
        ):
            got = _managed_tools_in_process("kirocrew-core")

        assert got, "names-only read returned nothing"
        scan.assert_not_called()
        cfg_load.assert_not_called()

    def test_all_managed_servers_resolve_in_process(self) -> None:
        """Every managed server still resolves to a name list (or [] by design for
        kirocrew-computer while its keystone is off) without spawning."""
        from kiro_crew.mcp_discovery import (
            _MANAGED_SERVER_TOOL_MODULES,
            _managed_tools_in_process,
        )

        for name in _MANAGED_SERVER_TOOL_MODULES:
            result = _managed_tools_in_process(name)
            assert result is not None, f"{name} failed to resolve in-process"
            assert all(isinstance(n, str) and n for n in result)

    def test_no_running_loop_skip_remains_in_the_two_builders(self) -> None:
        """Static guard: the ``get_running_loop`` skips the names-only path
        replaced must not creep back into either builder's code (docstring prose
        is allowed; executable ``get_running_loop(`` calls are not)."""
        import kiro_crew.mcp_tools.control as control
        import kiro_crew.mcp_tools.spawn as spawn

        for mod in (spawn, control):
            path = Path(mod.__file__)
            parts = sorted(path.with_name(f"{path.stem}_parts").glob("*.py"))
            source = "".join(p.read_text(encoding="utf-8") for p in (path, *parts))
            assert "get_running_loop(" not in source, (
                f"{mod.__name__} reintroduced a get_running_loop skip; the names-only "
                "read path (build_tool_names / schemas(names_only=True)) is what keeps "
                "the live reads off the gateway loop now"
            )
