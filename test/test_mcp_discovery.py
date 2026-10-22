"""Tests for MCP discovery module.

This file keeps the server listing: ``McpServerInfo``, ``list_servers`` and the
``extra_mcp_scopes`` seam. The other topics live in ``test_mcp_discovery_sync.py``,
``_agent_config``, ``_probe_cache``, ``_probe_remote``, ``_probe_stdio`` and
``_probe_spawn_policy``, and the fixture and helper they share in ``mcp_discovery_helpers.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_cache,
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew import mcp_cleanup
from kiro_crew.mcp_discovery import (
    SCOPE_CC_GLOBAL,
    SCOPE_KIRO_GLOBAL,
    SCOPE_KIROCREW,
    McpServerInfo,
    _load_mcp_json_by_source,
    _scope_priority,
    discover_servers_to_sync,
    list_servers,
)


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
        info = McpServerInfo(name="x", command="cmd", scopes=["read"], client_id="public-id")
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
        step, so the row must exist."""
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
            json.dumps(
                {"mcpServers": {"pending": {"command": "definitely-not-run", "disabled": True}}}
            )
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

    @pytest.mark.parametrize("shared_scope", [SCOPE_KIRO_GLOBAL, SCOPE_CC_GLOBAL])
    def test_shared_scope_disabled_server_keeps_a_row_after_sync(
        self, tmp_path, monkeypatch, shared_scope
    ) -> None:
        """A server disabled in a SHARED scope's ``mcp.json`` (the Kiro-global
        file the IDE edits, or a provider global) and held by no other source
        still gets a row.

        This is the post-sync state: discovery skips disabled entries and the
        rebuild never adds a disabled shared server to the agent config, so the
        agent file does not know the name. Dropping the row here is what made
        the panel show 6 of 7 configured servers after Discover & Sync while
        the IDE kept listing all 7 (three of them greyed)."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(
            json.dumps({"mcpServers": {"github": {"command": "npx"}}})
        )
        store = tmp_path / "store.json"
        store.write_text(json.dumps({"mcpServers": {}}))
        shared_mcp = tmp_path / "shared.json"
        shared_mcp.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "github": {"command": "npx"},
                        "figma": {"command": "npx", "args": ["figma-mcp"], "disabled": True},
                    }
                }
            )
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES",
            ((store, SCOPE_KIROCREW), (shared_mcp, shared_scope)),
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (store, shared_mcp))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        by_name = {s.name: s for s in list_servers()}
        assert set(by_name) == {"github", "figma"}
        assert by_name["figma"].disabled is True
        assert by_name["figma"].command == "npx"
        assert by_name["figma"].args == ["figma-mcp"]
        assert by_name["figma"].source == "mcp.json"
        # The enabled neighbour is untouched by the new arm.
        assert by_name["github"].disabled is False

    @pytest.mark.parametrize("shared_scope", [SCOPE_KIRO_GLOBAL, SCOPE_CC_GLOBAL])
    def test_shared_scope_disabled_entry_with_malformed_transport_lists_as_text(
        self, tmp_path, monkeypatch, shared_scope
    ) -> None:
        """A disabled shared entry whose ``command`` or ``url`` is not a string
        (``"command": {"not": "a string"}``) still gets its row -- and the row's
        display fields are STRINGS. ``command``/``url`` reach the page as React
        children (``{s.command || s.url}``), and an object there throws and takes
        the whole Connections page down; a disabled entry is never probed, so
        nothing else ever inspects the value. The text is the value's own bounded
        JSON, so the malformation is what the user sees. Red before
        ``_display_transport``: ``to_dict()["command"]`` was the dict itself."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        store = tmp_path / "store.json"
        store.write_text(json.dumps({"mcpServers": {}}))
        shared_mcp = tmp_path / "shared.json"
        huge = {"k" + str(i): "v" * 40 for i in range(20)}
        shared_mcp.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "broken": {"command": {"not": "a string"}, "disabled": True},
                        "remote": {"url": ["https://x.example"], "disabled": True},
                        "flood": {"command": huge, "disabled": True},
                        "fine": {"command": "npx", "disabled": True},
                        "leaky": {
                            "command": {
                                "headers": {"Authorization": "Bearer AKIAIOSFODNN7EXAMPLE"}
                            },
                            "disabled": True,
                        },
                    }
                }
            )
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES",
            ((store, SCOPE_KIROCREW), (shared_mcp, shared_scope)),
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (store, shared_mcp))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        by_name = {s.name: s for s in list_servers()}
        assert set(by_name) == {"broken", "remote", "flood", "fine", "leaky"}
        for row in by_name.values():
            assert row.disabled is True
            d = row.to_dict()
            assert isinstance(d["command"], str), row.name
            assert isinstance(d.get("url", ""), str), row.name
        assert by_name["broken"].command == '{"not": "a string"}'
        assert by_name["remote"].url == '["https://x.example"]'
        assert by_name["fine"].command == "npx"
        flood = by_name["flood"].command
        assert flood.endswith("...") and len(flood) <= 123, len(flood)
        # An object can carry what a string never did in this field: the text is
        # run through the site-wide scanners before it is shown (or cut).
        leaky = by_name["leaky"].command
        assert "AKIAIOSFODNN7EXAMPLE" not in leaky, leaky
        assert "AKIAIOSFODNN7EXAMPLE" not in by_name["leaky"].to_dict()["command"]
        assert "headers" in leaky

    @staticmethod
    def _seed_shared_scope(tmp_path, monkeypatch, disabled_value, servers=None) -> Path:
        """A shared-scope ``mcp.json`` holding ``figma`` with the given ``disabled``
        -- or, with *servers*, exactly that ``mcpServers`` mapping.

        Returns the shared file so a test can rewrite the value between calls.
        Both module-level ledger states are reset so tests do not see each
        other's warn-once history.
        """
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        store = tmp_path / "store.json"
        store.write_text(json.dumps({"mcpServers": {}}))
        shared_mcp = tmp_path / "shared.json"
        if servers is None:
            servers = {"figma": {"command": "npx", "disabled": disabled_value}}
        shared_mcp.write_text(json.dumps({"mcpServers": servers}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES",
            ((store, SCOPE_KIROCREW), (shared_mcp, SCOPE_KIRO_GLOBAL)),
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (store, shared_mcp))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_reported", set())
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_cap_reached", False)
        return shared_mcp

    @staticmethod
    def _invalid_disabled_warnings(caplog) -> list[str]:
        """The per-server 'not a boolean' lines, without the overflow line."""
        return [
            r.getMessage()
            for r in caplog.records
            if "not a boolean" in r.getMessage() and "'disabled' in the" in r.getMessage()
        ]

    @staticmethod
    def _cap_warnings(caplog) -> list[str]:
        """The single 'ledger is full' line, without the per-server lines."""
        return [
            r.getMessage() for r in caplog.records if "not reported individually" in r.getMessage()
        ]

    def test_shared_scope_non_boolean_disabled_is_a_disabled_row_with_a_warning(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """``"disabled": "false"`` (a string) is a config error, read FAIL-CLOSED.

        The listing shares the launch predicate (``mcp_entry_is_muted``) with
        the gateway rewriter and the session projections, and that predicate
        mutes anything but an absent key or a literal ``false``: the row is
        listed Disabled -- the same row no session starts -- and says WHY
        (``disabled_reason == "invalid"``), while the log names the value's type
        and size (``str(len 5)``, never the text -- the line is retained) and
        says the server is disabled, so an operator who wrote ``"false"`` and
        meant "on" learns the switch did not take. Read as "enabled" instead,
        the string would have listed -- and spawned -- a server whose value
        neither schema accepts."""
        self._seed_shared_scope(tmp_path, monkeypatch, "false")
        with caplog.at_level(logging.WARNING):
            rows = list_servers()
        by_name = {s.name: s for s in rows}
        assert "figma" in by_name
        assert by_name["figma"].disabled is True
        assert by_name["figma"].disabled_reason == "invalid"
        assert [s.name for s in rows if s.disabled] == ["figma"]
        warnings = [r for r in caplog.records if "'disabled'" in r.getMessage()]
        assert len(warnings) == 1
        assert "'figma'" in warnings[0].getMessage()
        assert "str(len 5)" in warnings[0].getMessage()
        assert "'false'" not in warnings[0].getMessage()
        assert "not a boolean" in warnings[0].getMessage()
        assert "DISABLED" in warnings[0].getMessage()
        assert "ENABLED" not in warnings[0].getMessage()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "true", 1, "yes", None])
    async def test_non_boolean_disabled_row_is_never_handed_to_the_probe(
        self, tmp_path, monkeypatch, value
    ) -> None:
        """The launch gate: a row muted by a non-boolean is returned as an
        unprobed ``status="disabled"`` placeholder and never spawned.

        Probing runs the server process, so this is where a fail-open read
        would cost a process rather than a chip. Red on the head that read the
        value as absent: ``probe_all`` handed ``figma`` to ``probe_server``."""
        self._seed_shared_scope(
            tmp_path,
            monkeypatch,
            None,
            servers={
                "figma": {"command": "npx", "disabled": value},
                "github": {"command": "gh-mcp"},
            },
        )
        probed: list[str] = []

        async def fake_probe(s):
            probed.append(s.name)
            s.status = "ok"
            return s

        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", fake_probe)
        monkeypatch.setattr("kiro_crew.mcp_discovery._spawn_excluded", lambda: set())
        from kiro_crew.mcp_discovery import probe_all

        rows = {s.name: s for s in await probe_all()}
        assert probed == ["github"], (value, probed)
        assert set(rows) == {"figma", "github"}
        assert rows["figma"].disabled is True
        assert rows["figma"].status == "disabled"
        assert rows["figma"].disabled_reason == "invalid"
        assert rows["figma"].to_dict()["disabledReason"] == "invalid"
        assert rows["github"].disabled is False
        assert "disabledReason" not in rows["github"].to_dict()

    def test_disabled_reason_is_invalid_only_when_no_source_says_true(
        self, tmp_path, monkeypatch
    ) -> None:
        """The reason tells the operator's two fixes apart. A row some source
        switches off with a literal ``true`` is "disabled in <file>" even if
        another source also carries an invalid value: flipping the real switch
        is the honest story, and the invalid value is still reported in the
        log. Only a row off for NO other reason than invalid values says so."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        store = tmp_path / "store.json"
        store.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "figma": {"command": "npx", "disabled": True},
                        "linear": {"command": "linear-mcp", "disabled": "yes"},
                    }
                }
            )
        )
        shared_mcp = tmp_path / "shared.json"
        shared_mcp.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "figma": {"command": "npx", "disabled": "false"},
                        "linear": {"command": "linear-mcp", "disabled": "false"},
                        "github": {"command": "gh-mcp", "disabled": True},
                    }
                }
            )
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES",
            ((store, SCOPE_KIROCREW), (shared_mcp, SCOPE_KIRO_GLOBAL)),
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (store, shared_mcp))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_reported", set())
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_cap_reached", False)
        rows = {s.name: s for s in list_servers()}
        assert {n for n, s in rows.items() if s.disabled} == {"figma", "linear", "github"}
        # A real ``true`` somewhere: the switch is the story.
        assert rows["figma"].disabled_reason is None
        assert rows["github"].disabled_reason is None
        # Invalid in BOTH scopes and ``true`` in neither: the value is the story.
        assert rows["linear"].disabled_reason == "invalid"

    def test_a_rebuilt_agent_mirror_does_not_mask_an_invalid_raw_value(
        self, tmp_path, monkeypatch
    ) -> None:
        """The agent config is the rebuild's output, and the rebuild writes
        ``disabled: true`` for a raw value it read fail-closed. So after one
        rebuild a provider-global ``"disabled": "false"`` sits beside an agent
        entry saying ``true``: provenance must come from the RAW scopes, or the
        table dresses the config error up as a deliberate disable. Red when the
        agent config's ``true`` fed ``boolean_true``: ``figma`` read ``None``."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir(exist_ok=True)
        # The rebuilt mirror: both entries carry the boolean the rebuild writes.
        (agent_dir / "defaults.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "figma": {"command": "npx", "disabled": True},
                        "github": {"command": "gh-mcp", "disabled": True},
                    }
                }
            )
        )
        store = tmp_path / "store.json"
        store.write_text(json.dumps({"mcpServers": {}}))
        shared_mcp = tmp_path / "shared.json"
        shared_mcp.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "figma": {"command": "npx", "disabled": "false"},
                        "github": {"command": "gh-mcp", "disabled": True},
                    }
                }
            )
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES",
            ((store, SCOPE_KIROCREW), (shared_mcp, SCOPE_CC_GLOBAL)),
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (store, shared_mcp))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_reported", set())
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_cap_reached", False)
        rows = {s.name: s for s in list_servers()}
        assert rows["figma"].disabled is True
        assert rows["figma"].disabled_reason == "invalid"
        # A real ``true`` in the raw scope is still a real switch.
        assert rows["github"].disabled is True
        assert rows["github"].disabled_reason is None

    def test_a_provider_global_disable_is_recorded_as_shared_on_the_row(
        self, tmp_path, monkeypatch
    ) -> None:
        """Step 3c records WHICH kind of scope muted the row: any scope but the
        Kiro Crew store is "shared" -- a disable the dashboard cannot lift by
        editing the store. The ``GET /api/mcp`` stamping holds only the
        Kiro-global and store maps, so a server disabled in a PROVIDER global and
        in the store is, to it, a store-only disable: without the row's verdict it
        offered the consent step, whose Apply lifted the store flag and left the
        provider-global one standing. Store-only stays a consent row."""
        from kiro_crew.dashboard.handlers.mcp import _stamp_config_state

        agent_dir = tmp_path / "agents"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        store = tmp_path / "store.json"
        store.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "figma": {"command": "npx", "disabled": True},
                        "weather": {"command": "wx", "disabled": True},
                    }
                }
            )
        )
        cc_global = tmp_path / "cc.json"
        cc_global.write_text(
            json.dumps({"mcpServers": {"figma": {"command": "npx", "disabled": True}}})
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES",
            ((store, SCOPE_KIROCREW), (cc_global, SCOPE_CC_GLOBAL)),
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (store, cc_global))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        rows = {s.name: s for s in list_servers()}
        assert rows["figma"].disabled is True and rows["figma"].disabled_in_shared is True
        assert rows["weather"].disabled is True and rows["weather"].disabled_in_shared is False
        # Through the wire row into the stamping, with the maps the handler has:
        # the Kiro-global file (empty here) and the store.
        store_map = json.loads(store.read_text())["mcpServers"]
        figma, weather = rows["figma"].to_dict(), rows["weather"].to_dict()
        _stamp_config_state(figma, {}, store_map)
        _stamp_config_state(weather, {}, store_map)
        assert figma["disabledIn"] == "shared"
        assert figma["disabledInFile"] is None
        assert "disabledInShared" not in figma
        assert weather["disabledIn"] == "kirocrew"

    def test_non_boolean_disabled_reads_as_disabled_at_every_read(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """One predicate, every read. The listing reported the string and then
        read it again at three later sites -- the agent-config pass, the
        introduce arm, and the aggregate flag pass (3c) -- and the sync offer
        read it a fourth time. Each now takes ``mcp_entry_is_muted``: the
        existing agent row whose shared entry carries the string is flagged, the
        shared-only entry is listed Disabled, the agent entry carrying it takes
        the path a literal ``true`` takes there (no scope copy, so no row -- the
        pre-existing shape for a disabled agent-only entry), and the sync offer
        withholds the shared-only one, because syncing is how it would reach the
        config the sessions load. Red under a reader that took the value as
        absent or as the literal ``True`` alone."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "defaults.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "github": {"command": "gh-mcp"},
                        "linear": {"command": "linear-mcp", "disabled": "false"},
                    }
                }
            )
        )
        store = tmp_path / "store.json"
        store.write_text(json.dumps({"mcpServers": {}}))
        shared_mcp = tmp_path / "shared.json"
        shared_mcp.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "github": {"command": "gh-mcp", "disabled": "false"},
                        "figma": {"command": "npx", "disabled": "yes"},
                    }
                }
            )
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES",
            ((store, SCOPE_KIROCREW), (shared_mcp, SCOPE_KIRO_GLOBAL)),
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (store, shared_mcp))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_reported", set())
        monkeypatch.setattr("kiro_crew.mcp_cleanup._invalid_disabled_cap_reached", False)
        with caplog.at_level(logging.WARNING):
            rows = {s.name: s for s in list_servers()}
        # Step 1 mutes the agent entry (no scope copy, so no row -- as for a
        # literal ``true``); the introduce arm lists the shared-only entry
        # Disabled; 3c flags the existing agent row from its shared entry.
        assert set(rows) == {"github", "figma"}, sorted(rows)
        assert {n for n, s in rows.items() if s.disabled} == {"github", "figma"}
        assert {s.disabled_reason for s in rows.values()} == {"invalid"}
        # Reported once per (server, value) -- the two shared-scope strings.
        named = sorted(m.split("'")[1] for m in self._invalid_disabled_warnings(caplog))
        assert named == ["figma", "github"]
        # The sync offer agrees with the listing: a muted shared entry is never
        # offered into the agent config.
        offered = {s.name for s in discover_servers_to_sync()}
        assert "figma" not in offered
        assert "github" not in offered

    def test_non_boolean_disabled_warns_once_per_server_and_value_shape(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """One warning per (server, value shape), however often the list is read
        and whichever path reads it.

        The ledger is a bounded set of ``(truncated name, type descriptor)``
        pairs and is never pruned: a process says each config error once. So a
        value that is fixed stops being a defect without a second line, and one
        re-broken the same way is NOT announced again -- the row's own
        ``disabled_reason`` is the durable signal, and the pruning/re-arm
        machinery that would have re-announced it cost more than it bought."""
        shared_mcp = self._seed_shared_scope(tmp_path, monkeypatch, "false")
        with caplog.at_level(logging.WARNING):
            list_servers()
            list_servers()
        assert sum("'disabled'" in r.getMessage() for r in caplog.records) == 1
        caplog.clear()
        # Fixed: a real boolean is a real Disabled row -- no reason to add, and
        # nothing new to say.
        shared_mcp.write_text(
            json.dumps({"mcpServers": {"figma": {"command": "npx", "disabled": True}}})
        )
        with caplog.at_level(logging.WARNING):
            rows = list_servers()
        assert [s.name for s in rows if s.disabled] == ["figma"]
        assert rows[0].disabled_reason is None
        assert sum("'disabled'" in r.getMessage() for r in caplog.records) == 0
        # Broken again the same way: the row says so, the log does not repeat.
        shared_mcp.write_text(
            json.dumps({"mcpServers": {"figma": {"command": "npx", "disabled": "false"}}})
        )
        with caplog.at_level(logging.WARNING):
            rows = list_servers()
        assert [s.disabled_reason for s in rows] == ["invalid"]
        assert sum("'disabled'" in r.getMessage() for r in caplog.records) == 0
        # A DIFFERENT wrong shape on the same server is a new config error.
        shared_mcp.write_text(
            json.dumps({"mcpServers": {"figma": {"command": "npx", "disabled": 1}}})
        )
        with caplog.at_level(logging.WARNING):
            list_servers()
        assert sum("'disabled'" in r.getMessage() for r in caplog.records) == 1

    def test_invalid_disabled_ledger_keeps_exactly_the_cap_and_says_so_once(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """Past the cap the ledger holds exactly N bounded pairs, the N are
        reported one line each, and the rest get ONE line saying they are not
        reported individually -- not a line per refused server, and nothing on a
        re-read. A refused pair gets no entry anywhere, so the cap bounds the
        retention itself, not just the count of lines."""
        monkeypatch.setattr("kiro_crew.mcp_cleanup.INVALID_DISABLED_LEDGER_CAP", 3)
        servers = {f"s{i}": {"command": "npx", "disabled": f"no-{i}"} for i in range(5)}
        self._seed_shared_scope(tmp_path, monkeypatch, None, servers=servers)
        with caplog.at_level(logging.WARNING):
            rows = list_servers()
        # Read fail-closed: all five are listed, all five Disabled -- the cap
        # bounds what is REPORTED, never what is refused a launch.
        assert {s.name for s in rows} >= set(servers)
        assert sorted(s.name for s in rows if s.disabled) == sorted(servers)
        assert {s.disabled_reason for s in rows if s.disabled} == {"invalid"}
        assert len(mcp_cleanup._invalid_disabled_reported) == 3
        named = self._invalid_disabled_warnings(caplog)
        assert [n for n in servers if any(f"'{n}'" in m for m in named)] == ["s0", "s1", "s2"]
        capped = self._cap_warnings(caplog)
        assert len(capped) == 1
        assert "more than 3 servers" in capped[0]
        caplog.clear()
        # Same config read again: every pair is already reported, and the cap
        # line is said once per process.
        with caplog.at_level(logging.WARNING):
            list_servers()
        assert self._invalid_disabled_warnings(caplog) == []
        assert self._cap_warnings(caplog) == []
        assert len(mcp_cleanup._invalid_disabled_reported) == 3

    def test_invalid_disabled_ledger_retains_bounded_pairs_never_the_config_text(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """Every retained field is bounded: a long server name and a long value
        leave one ``(truncated name, type descriptor)`` pair with neither text
        whole inside it. The per-server log line is retained too (the dashboard
        ring keeps it), so it carries the name cut to ``LOG_NAME_MAX`` and the
        value's type and size -- not the name whole, and never the value."""
        name = "figma-" + "x" * 300
        value = "false" * 100
        self._seed_shared_scope(
            tmp_path, monkeypatch, None, servers={name: {"command": "npx", "disabled": value}}
        )
        with caplog.at_level(logging.WARNING):
            list_servers()
        ((held_name, held_shape),) = mcp_cleanup._invalid_disabled_reported
        assert len(held_name) <= mcp_cleanup.LOG_NAME_MAX + len("...(+300 chars)")
        assert name not in held_name
        assert held_shape == "str(len 500)"
        assert "false" not in held_shape
        (line,) = self._invalid_disabled_warnings(caplog)
        # "Set it to true or false." is part of the fixed text; the VALUE is
        # the repetition, and none of it may be quoted.
        assert value not in line and "falsefalse" not in line
        assert "str(len 500)" in line
        assert name not in line
        assert name[: mcp_cleanup.LOG_NAME_MAX] in line
        assert f"(+{len(name) - mcp_cleanup.LOG_NAME_MAX} chars)" in line

    def test_invalid_disabled_log_line_is_bounded_whatever_the_value(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """A 1 MiB ``disabled`` string leaves a log line of a few hundred bytes.

        The dashboard log ring retains the last 1000 FORMATTED lines and
        streams each to every log subscriber, with no per-line bound; a line
        that quoted the value would carry the whole megabyte into both on
        every page load that re-reports it. The line names the type and size
        and nothing of the text."""
        value = "n" * (1 << 20)
        self._seed_shared_scope(tmp_path, monkeypatch, value)
        with caplog.at_level(logging.WARNING):
            list_servers()
        (line,) = self._invalid_disabled_warnings(caplog)
        assert f"str(len {1 << 20})" in line
        assert "nnnn" not in line
        assert len(line) < 400, len(line)

    @pytest.mark.parametrize(
        ("value", "shape"),
        [
            (None, "NoneType"),
            (1, "int"),
            (0.5, "float"),
            (["element-text"], "list(len 1)"),
            ({"key-text": True, "other-key": False}, "dict(len 2)"),
        ],
    )
    def test_invalid_disabled_log_line_names_the_type_and_size_of_each_shape(
        self, tmp_path, monkeypatch, caplog, value, shape
    ) -> None:
        """Every JSON shape a ``disabled`` can take is described by type -- and
        by size when it has one -- so the operator can tell ``null`` from ``1``
        from a quoted string without the line quoting any of them."""
        self._seed_shared_scope(tmp_path, monkeypatch, value)
        with caplog.at_level(logging.WARNING):
            rows = list_servers()
        assert {s.name for s in rows if s.disabled} == {"figma"}
        (line,) = self._invalid_disabled_warnings(caplog)
        assert f" is {shape}, not a boolean" in line
        assert "element-text" not in line and "key-text" not in line

    @pytest.mark.asyncio
    async def test_probe_all_returns_disabled_rows_unprobed_in_list_order(
        self, tmp_path, monkeypatch
    ) -> None:
        """The probe response replaces the dashboard's list, so a
        disabled row must come back -- as ``status="disabled"`` with no spawn --
        rather than vanish until the next page load. Enabled rows are probed
        exactly as before, and the order is ``list_servers``' own."""
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "alpha": {"command": "a"},
                        "pending": {"command": "definitely-not-run", "disabled": True},
                        "zulu": {"command": "z"},
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        spawned: list[str] = []

        async def fake_probe(server):
            assert not server.disabled, "a disabled row reached probe_server"
            spawned.append(server.name)
            server.status = "ok"
            return server

        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", fake_probe)
        from kiro_crew.mcp_discovery import probe_all

        rows = await probe_all()
        assert [s.name for s in rows] == ["alpha", "pending", "zulu"]
        assert spawned == ["alpha", "zulu"]
        by_name = {s.name: s for s in rows}
        assert by_name["pending"].status == "disabled"
        assert by_name["pending"].disabled is True
        assert by_name["pending"].error == ""
        assert by_name["alpha"].status == "ok"
        assert by_name["zulu"].status == "ok"

    @pytest.mark.asyncio
    async def test_probe_all_with_only_disabled_rows_returns_them(
        self, tmp_path, monkeypatch
    ) -> None:
        """The early return for an empty spawn set still reports the disabled
        rows -- a config of nothing but disabled servers is not an empty panel."""
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"pending": {"command": "x", "disabled": True}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        from kiro_crew.mcp_discovery import probe_all

        rows = await probe_all()
        assert [(s.name, s.status) for s in rows] == [("pending", "disabled")]

    def test_a_rebuild_withheld_server_is_listed_not_hidden(self, tmp_path, monkeypatch) -> None:
        """The rebuild's own withheld mute is not the user's: the row stays listed
        so the routing switch that lifts it can be reached from this table."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "a",
                            "env": {"API_TOKEN": "secret://X"},
                            "disabled": True,
                            "x-kirocrew-withheld": True,
                        }
                    }
                }
            )
        )
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {"mcpServers": {"srv": {"command": "a", "env": {"API_TOKEN": "secret://X"}}}}
            )
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        rows = [s for s in list_servers() if s.name == "srv"]
        assert len(rows) == 1 and not rows[0].disabled

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
        assert order.index(SCOPE_KIRO_GLOBAL) < order.index(
            SCOPE_CC_GLOBAL
        ), "Kiro global must outrank the seam ccGlobal scope (rebuild parity)"

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
        assert server.disabled_tools == [
            "kiro-tool"
        ], "Kiro-global disabledTools must win over the seam scope (first-scope-wins)"
