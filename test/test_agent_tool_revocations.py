"""Tests for tool bloat prevention: revocations, disabled servers and alias families."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from agent_helpers import _pinned_kiro_cli_version  # noqa: F401
from agent_helpers import _run_install

# Shared with ``test_mcp_rebuild_reconsumption`` via a dedicated helpers module --
# the repo's convention, since a test module is not importable from another one.
# Re-exported under the original private names so the call sites below are unchanged.
from mcp_merge_helpers import bundled_defaults as _bundled_defaults


class TestToolBloatFixesRevocationsAndAliases:
    """Tool bloat prevention: revoked grants, disabled servers and alias families.

    The SEL events a revocation writes, the tool_search seed's idempotence and
    template gate, the managed and dashboard-store servers a rebuild adds or drops,
    OAuth hints, alias-family ownership, malformed or non-dict input, and the dedup
    of the final lists.
    """

    def _tool_search_defaults(self, tmp_path: Path) -> Path:
        """Bundled defaults whose template grants the tool_search built-in."""
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        defaults = {
            "model": "claude-default",
            "tools": ["ReadFile", "tool_search"],
            "allowedTools": ["ReadFile"],
            "mcpServers": {},
            "toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf /"]}},
            "hooks": {"preToolUse": "audit"},
        }
        (cfg_dir / "defaults.json").write_text(json.dumps(defaults))
        (cfg_dir / "prompt.md").write_text("system prompt")
        return cfg_dir

    def test_reserved_alias_collision_revocation_has_its_own_sel_event(
        self, tmp_path: Path, monkeypatch
    ):
        """The normalizer's collision filter revokes a grant through the caller audit."""
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@scope-pkg"],
            "allowedTools": ["@scope-pkg"],
            "mcpServers": {
                "scope-pkg": {"command": "live-tool"},
                "npm:@scope/pkg": {"command": "missing-tool"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        monkeypatch.setattr(agent_mod, "_app_owned_mcp_keys", lambda: {})
        events: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw)

        monkeypatch.setattr(agent_mod, "sel", lambda: _Sel())
        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                managed_mcps={},
                which=lambda command, **_kwargs: None if command == "missing-tool" else command,
            ).read_text(encoding="utf-8")
        )

        assert "@scope-pkg" not in config["allowedTools"]
        revoked = [
            event["resources"]
            for event in events
            if event.get("operation") == "mcp_auto_approve_revoked"
            and "reserved-alias collision or deleted-proxy purge" in event.get("resources", "")
        ]
        assert revoked == [
            "@scope-pkg auto-approval removed " "(reserved-alias collision or deleted-proxy purge)"
        ]

    def test_alias_migration_is_not_a_revocation_sel_event(self, tmp_path: Path, monkeypatch):
        """Renamed bare and per-tool grants stay live without a false revocation event."""
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": [
                "fs_read",
                "@npm:@scope/pkg",
                "@npm:@scope/pkg/tool",
                "@playwright-mcp",
            ],
            "allowedTools": [
                "@npm:@scope/pkg",
                "@npm:@scope/pkg/tool",
                "@playwright-mcp",
            ],
            "mcpServers": {
                "npm:@scope/pkg": {"command": sys.executable},
                "playwright-mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy"],
                },
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        monkeypatch.setattr(agent_mod, "_app_owned_mcp_keys", lambda: {})
        events: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw)

        monkeypatch.setattr(agent_mod, "sel", lambda: _Sel())
        config = json.loads(
            _run_install(tmp_path, cfg_dir, managed_mcps={}).read_text(encoding="utf-8")
        )

        assert "@scope-pkg" in config["allowedTools"]
        assert "@scope-pkg/tool" in config["allowedTools"]
        revoked = [
            event["resources"]
            for event in events
            if event.get("operation") == "mcp_auto_approve_revoked"
        ]
        for ref in (
            "@npm:@scope/pkg",
            "@npm:@scope/pkg/tool",
            "@scope-pkg",
            "@scope-pkg/tool",
        ):
            assert not any(ref in resources for resources in revoked)
        bracket_revoked = [
            resources
            for resources in revoked
            if "reserved-alias collision or deleted-proxy purge" in resources
        ]
        assert bracket_revoked == [
            "@playwright-mcp auto-approval removed "
            "(reserved-alias collision or deleted-proxy purge)"
        ]

    def test_ambiguous_grant_survives_without_a_revocation_sel_event(
        self, tmp_path: Path, monkeypatch
    ):
        """A verbatim-kept ambiguous grant is not reported as revoked."""
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@a", "@a/b", "@playwright-mcp"],
            "allowedTools": ["@a/b", "@playwright-mcp"],
            "mcpServers": {
                "a": {"command": sys.executable},
                "a/b": {"command": sys.executable},
                "playwright-mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy"],
                },
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        monkeypatch.setattr(agent_mod, "_app_owned_mcp_keys", lambda: {})
        events: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw)

        monkeypatch.setattr(agent_mod, "sel", lambda: _Sel())
        config = json.loads(
            _run_install(tmp_path, cfg_dir, managed_mcps={}).read_text(encoding="utf-8")
        )

        assert "a" in config["mcpServers"]
        assert "a-b" in config["mcpServers"]
        assert "@a/b" in config["allowedTools"]
        assert "@a-b" not in config["allowedTools"]
        revoked = [
            event["resources"]
            for event in events
            if event.get("operation") == "mcp_auto_approve_revoked"
            and "reserved-alias collision or deleted-proxy purge" in event.get("resources", "")
        ]
        assert revoked == [
            "@playwright-mcp auto-approval removed "
            "(reserved-alias collision or deleted-proxy purge)"
        ]
        assert not any("@a/b" in resources or "@a-b" in resources for resources in revoked)

    def test_deleted_proxy_revocation_has_its_own_sel_event(self, tmp_path: Path, monkeypatch):
        """The every-rebuild proxy purge is covered by the same caller audit bracket."""
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@playwright-mcp", "@playwright-mcp/browser_navigate"],
            "allowedTools": ["@playwright-mcp", "@playwright-mcp/browser_navigate"],
            "mcpServers": {
                "playwright-mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy"],
                }
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        monkeypatch.setattr(agent_mod, "_app_owned_mcp_keys", lambda: {})
        events: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw)

        monkeypatch.setattr(agent_mod, "sel", lambda: _Sel())
        config = json.loads(
            _run_install(tmp_path, cfg_dir, managed_mcps={}).read_text(encoding="utf-8")
        )

        assert "playwright-mcp" not in config["mcpServers"]
        revoked = [
            event["resources"]
            for event in events
            if event.get("operation") == "mcp_auto_approve_revoked"
            and "reserved-alias collision or deleted-proxy purge" in event.get("resources", "")
        ]
        assert revoked == [
            "@playwright-mcp, @playwright-mcp/browser_navigate auto-approval removed "
            "(reserved-alias collision or deleted-proxy purge)"
        ]

    def test_truthy_disabled_values_disable_shared_servers(self, tmp_path: Path):
        """Every truthy disabled value denies grants; falsey values stay enabled."""
        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": [
                "fs_read",
                "@string-true",
                "@string-true/run",
                "@literal-true",
                "@literal-true/run",
                "@string-false",
                "@string-false/run",
                "@literal-false",
                "@literal-false/run",
            ],
            "allowedTools": [
                "@string-true",
                "@string-true/run",
                "@literal-true",
                "@literal-true/run",
                "@string-false",
                "@string-false/run",
                "@literal-false",
                "@literal-false/run",
            ],
            "mcpServers": {
                "string-true": {"command": "string-true-tool"},
                "literal-true": {"command": "literal-true-tool"},
                "string-false": {"command": "string-false-tool"},
                "literal-false": {"command": "literal-false-tool"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "string-true": {
                            "command": "string-true-tool",
                            "disabled": "true",
                        },
                        "literal-true": {
                            "command": "literal-true-tool",
                            "disabled": True,
                        },
                        "string-false": {
                            "command": "string-false-tool",
                            "disabled": "false",
                        },
                        "literal-false": {
                            "command": "literal-false-tool",
                            "disabled": False,
                        },
                    }
                }
            )
        )

        config = json.loads(
            _run_install(tmp_path, cfg_dir, managed_mcps={}).read_text(encoding="utf-8")
        )

        for name in ("string-true", "literal-true", "string-false"):
            assert f"@{name}" not in config["tools"]
            assert f"@{name}/run" in config["tools"]
            assert not [
                ref
                for ref in config["allowedTools"]
                if ref == f"@{name}" or str(ref).startswith(f"@{name}/")
            ]
        assert "@literal-false" in config["tools"]
        assert "@literal-false/run" in config["tools"]
        assert "@literal-false" in config["allowedTools"]
        assert "@literal-false/run" in config["allowedTools"]

    def test_revoking_a_grant_is_audited_rather_than_only_logged(self, tmp_path: Path, monkeypatch):
        """Dropping a ref out of `allowedTools` removes an auto-approval, so it is audited.

        The `tools` side only mounts; the grant side is a permission decision, and
        the withhold path in the same pass already emits one. A silent revoke
        leaves an operator no record of why a tool now prompts.
        """
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@gone"],
            "allowedTools": ["@gone"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        events: list[tuple[str, str]] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append((kw.get("operation", ""), kw.get("resources", "")))

        monkeypatch.setattr(agent_mod, "sel", lambda: _Sel())

        config = json.loads(_run_install(tmp_path, cfg_dir).read_text(encoding="utf-8"))

        assert "@gone" not in config["allowedTools"]
        revoked = [r for op, r in events if op == "mcp_auto_approve_revoked"]
        assert revoked == ["@gone auto-approval removed (no such server in mcpServers)"]
        assert "reserved-alias collision or deleted-proxy purge" not in revoked[0]

    def test_existing_config_tool_search_idempotent(self, tmp_path: Path):
        """A config that already grants tool_search is left unchanged (no dup)."""
        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["execute_bash", "tool_search", "code"],
            "allowedTools": ["code"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["tools"] == ["execute_bash", "tool_search", "code"]
        assert config["tools"].count("tool_search") == 1

    def test_existing_config_no_tool_search_seed_when_template_omits(self, tmp_path: Path):
        """When the shipped template does NOT grant tool_search, an existing
        config's tools are left exactly as-is (seed is template-gated)."""
        cfg_dir = _bundled_defaults(tmp_path)  # tools == ["ReadFile"], no tool_search
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["execute_bash", "code"],
            "allowedTools": ["code"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["tools"] == ["execute_bash", "code"]
        assert "tool_search" not in config["tools"]

    def test_existing_config_no_managed_mcp_added(self, tmp_path: Path):
        """Existing configs don't get @managed-mcp refs injected."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["shell", "read"],
            "allowedTools": ["read"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["tools"] == ["shell", "read"]
        assert config["allowedTools"] == ["read"]

    def test_fresh_install_adds_managed_mcp_to_tools_only(self, tmp_path: Path):
        """Fresh install adds @managed-mcp to tools but NOT allowedTools."""
        cfg_dir = _bundled_defaults(tmp_path)
        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert "@kirocrew-cron" in config["tools"]
        assert "@kirocrew-core" in config["tools"]
        assert "@kirocrew-cron" not in config["allowedTools"]
        assert "@kirocrew-core" not in config["allowedTools"]

    def test_dashboard_added_remote_server_reaches_the_tools_allowlist(self, tmp_path: Path):
        """A Connections provider (or any dashboard-added MCP entry) must land in
        `tools`, not just `mcpServers`.

        `tools` is a CLOSED allowlist with no wildcard, so an entry present in
        `mcpServers` but absent from `tools` is mounted with none of its tools
        exposed — the model then truthfully reports it has no such integration
        even though the provider is fully connected. This shipped unnoticed
        because nothing asserted the registration; that is what this test pins.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        (user_home / "mcp.json").write_text(
            json.dumps({"mcpServers": {"notion": {"url": "https://mcp.notion.com/mcp"}}})
        )

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))

        assert "notion" in config["mcpServers"]
        assert "@notion" in config["tools"]

    def test_disabled_dashboard_remote_server_is_removed_from_tools(self, tmp_path: Path):
        """Disconnect must be the inverse: a disabled entry loses its ref, so a
        disconnected provider cannot keep exposing tools."""
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        (user_home / "mcp.json").write_text(
            json.dumps(
                {"mcpServers": {"notion": {"url": "https://mcp.notion.com/mcp", "disabled": True}}}
            )
        )
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        (kiro_dir / "kirocrew.json").write_text(
            json.dumps({"tools": ["@notion"], "allowedTools": [], "mcpServers": {}})
        )

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))

        assert "@notion" not in config["tools"]

    def test_removed_oauth_hints_do_not_survive_a_rebuild_of_a_managed_entry(self, tmp_path: Path):
        """Row 3 of the ownership table, through the real rebuild.

        The dashboard store owns this name, and the custom-update API removes a
        hint by DELETING the key. Since the last-rendered config is the
        merge base and ``dict.update()`` cannot remove anything, absence has to
        mean removed here or the last-rendered grant stays in the spec forever.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        # Dashboard store: the hints were removed, so the keys are simply gone.
        (user_home / "mcp.json").write_text(
            json.dumps({"mcpServers": {"notion": {"url": "https://mcp.notion.com/mcp"}}})
        )
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        # Previous render, still carrying the hints it was built with.
        (kiro_dir / "kirocrew.json").write_text(
            json.dumps(
                {
                    "tools": [],
                    "allowedTools": [],
                    "mcpServers": {
                        "notion": {
                            "url": "https://mcp.notion.com/mcp",
                            "oauthScopes": ["read", "write"],
                            "oauth": {"clientId": "stale-id", "issuer": "https://issuer"},
                        }
                    },
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["notion"]

        assert "oauthScopes" not in entry
        assert entry.get("oauth") == {"issuer": "https://issuer"}, "issuer is the user's"

    def test_an_unmanaged_wire_only_entry_keeps_its_oauth_hints(self, tmp_path: Path):
        """Row 4 of the ownership table, through the real rebuild.

        This server is defined only in kiro-cli's own settings file, hand-authored
        in the wire spelling. Nothing of ours owns it, so the wire values are the
        only copy and deleting them destroys configuration we never wrote. The
        entry is byte-identical to row 3's -- ownership is the ONLY difference.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        (user_home / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "handmade": {
                            "url": "https://mcp.example.com/mcp",
                            "oauthScopes": ["read:user"],
                            "oauth": {"clientId": "hand-authored", "issuer": "https://issuer"},
                        }
                    }
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["handmade"]

        assert entry["oauthScopes"] == ["read:user"]
        assert entry["oauth"] == {"clientId": "hand-authored", "issuer": "https://issuer"}

    def test_a_malformed_store_value_does_not_confer_ownership(self, tmp_path: Path):
        """A non-dict store value must not mark a global entry as ours.

        Membership is not ownership. The merge skips a malformed
        ``kirocrew_mcp`` value entirely, so it contributes no hints and cannot be
        the source of truth for any — yet a bare `name in kirocrew_mcp` test
        would read the collision as "the store owns this" and delete the global
        entry's hand-authored wire hints on behalf of a store entry that does not
        really exist.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        # Hand-edited garbage under the same name as the global server below.
        (user_home / "mcp.json").write_text(json.dumps({"mcpServers": {"handmade": "not-a-dict"}}))
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "handmade": {
                            "url": "https://mcp.example.com/mcp",
                            "oauthScopes": ["read:user"],
                            "oauth": {"clientId": "hand-authored", "issuer": "https://issuer"},
                        }
                    }
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["handmade"]

        assert entry["oauthScopes"] == ["read:user"]
        assert entry["oauth"] == {"clientId": "hand-authored", "issuer": "https://issuer"}

    def test_a_globally_disabled_server_is_not_remounted_by_the_store_entry(self, tmp_path: Path):
        """An operator disable must survive a same-named dashboard-store entry.

        `POST /api/mcp/toggle enabled:false` writes `disabled: true` into the
        kiro-global mcp.json ONLY. The store entry for the same name carries no
        `disabled` key, so a chain that visits the store last would re-mount the
        server AND auto-approve it -- and auto-approve is the one path that never
        reaches the PreToolUse gate, so the operator's disable would be silently
        void for every tool on that server.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        # Store entry: no `disabled` key at all.
        (user_home / "mcp.json").write_text(
            json.dumps({"mcpServers": {"notion": {"url": "https://mcp.notion.com/mcp"}}})
        )
        # Kiro global: the operator's disable lives here and only here.
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps(
                {"mcpServers": {"notion": {"url": "https://mcp.notion.com/mcp", "disabled": True}}}
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))

        assert "@notion" not in config.get("tools", []), "disabled server must not mount"
        assert "@notion" not in config.get("allowedTools", []), "and must not be auto-approved"
        # The other half of the same bug: the enabled branch also cleared the flag
        # off the emitted spec, so kiro-cli itself never saw the disable either.
        entry = config.get("mcpServers", {}).get("notion")
        assert entry is None or entry.get("disabled") is True, "the flag must reach the spec"

    def test_a_disable_strips_the_per_tool_spelling_and_duplicates(self, tmp_path: Path):
        """A disable strips bare refs and per-tool grants while preserving mounts.

        ``@notion/search`` in ``allowedTools`` is a grant on the disabled
        server, and that list never reaches the PreToolUse gate. Every grant
        occurrence must be stripped. The same spelling in ``tools`` is a
        selective mount that stays inert while the server entry is disabled and
        must survive so re-enabling the entry restores the user's mount.

        ``notionx`` shares the prefix without the ``/`` boundary, so its mount
        and grants remain untouched.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        # The per-tool refs live only in the existing config: the rebuild's ref
        # sync writes whole-server refs, so these are user-authored state.
        existing = {
            "tools": ["fs_read", "@notion", "@notion/search", "@notion/search", "@notionx"],
            "allowedTools": [
                "@notion",
                "@notion/search",
                "@notion/search",
                "@notionx",
                "@notionx/keep",
            ],
            "mcpServers": {
                "notion": {"url": "https://mcp.notion.com/mcp"},
                "notionx": {"url": "https://mcp.notionx.example.com/mcp"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        # The operator's disable lives in the kiro global; notionx stays enabled.
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "notion": {"url": "https://mcp.notion.com/mcp", "disabled": True},
                        "notionx": {"url": "https://mcp.notionx.example.com/mcp"},
                    }
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))

        assert "@notion" not in config["tools"]
        assert config["tools"].count("@notion/search") == 1
        assert not [
            t for t in config["allowedTools"] if t == "@notion" or str(t).startswith("@notion/")
        ]
        assert "@notionx" in config["tools"]
        assert "@notionx" in config["allowedTools"]
        assert "@notionx/keep" in config["allowedTools"]

    def test_a_disabled_slash_name_strips_only_its_collision_alias(self, tmp_path: Path):
        """A disable strips its concrete mount and denies canonical-family grants."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": [
                "@foo-bar",
                "@foo-bar/search",
                "@foo-bar-2",
                "@foo-bar-2/search",
            ],
            "allowedTools": [
                "@foo-bar",
                "@foo-bar/search",
                "@foo-bar-2",
                "@foo-bar-2/search",
            ],
            "mcpServers": {"foo-bar": {"command": "live-tool"}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "foo/bar": {
                            "command": "disabled-tool",
                            "disabled": True,
                        }
                    }
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))

        assert config["mcpServers"]["foo-bar"]["command"] == "live-tool"
        assert config["mcpServers"]["foo-bar-2"]["command"] == "disabled-tool"

        # The disabled server's bare ref leaves both lists, while its selective
        # mount remains available for re-enable and only its grant is revoked.
        assert "@foo-bar-2" not in config["tools"]
        assert "@foo-bar-2/search" in config["tools"]
        assert not [
            ref
            for ref in config["allowedTools"]
            if ref == "@foo-bar-2" or str(ref).startswith("@foo-bar-2/")
        ]

        # The distinct enabled server keeps both mounts, but the disabled
        # canonical family denies its grants in both spellings.
        assert "@foo-bar" in config["tools"]
        assert "@foo-bar/search" in config["tools"]
        assert not [
            ref
            for ref in config["allowedTools"]
            if ref == "@foo-bar" or str(ref).startswith("@foo-bar/")
        ]

    def test_an_unresolved_disable_revokes_its_canonical_alias_family(self, tmp_path: Path):
        """An unresolved disable revokes family grants without destroying sibling mounts."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": [
                "@foo-bar",
                "@foo-bar/keep",
                "@foo-bar-2",
                "@foo-bar-2/tool",
                "@foo-barn-2/keep",
                "@foo-bar-2x/keep",
                "@other-live/keep",
            ],
            "allowedTools": [
                "@foo-bar",
                "@foo-bar/keep",
                "@foo-bar-2",
                "@foo-bar-2/tool",
                "@foo-barn-2/keep",
                "@foo-bar-2x/keep",
                "@other-live/keep",
            ],
            "mcpServers": {
                "foo-bar": {"command": "live-tool"},
                "foo-bar-2": {"command": "replacement-tool"},
                "foo-barn-2": {"command": "boundary-tool"},
                "foo-bar-2x": {"command": "boundary-suffix-tool"},
                "other-live": {"command": "other-live-tool"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "foo/bar": {
                            "command": "missing-tool",
                            "disabled": True,
                        },
                        "foo-bar": {"command": "live-tool"},
                        "foo-bar-3": {"command": "replacement-three-tool"},
                        "other-live": {"command": "other-live-tool"},
                    }
                }
            )
        )

        path = _run_install(
            tmp_path,
            cfg_dir,
            which=lambda command, **_kwargs: (None if command == "missing-tool" else command),
        )
        config = json.loads(path.read_text(encoding="utf-8"))

        assert config["mcpServers"]["foo-bar"]["command"] == "live-tool"
        assert config["mcpServers"]["foo-bar-2"]["command"] == "replacement-tool"
        assert config["mcpServers"]["foo-bar-3"]["command"] == "replacement-three-tool"
        assert "@foo-bar" in config["tools"]
        assert "@foo-bar/keep" in config["tools"]
        assert "@foo-bar-2" in config["tools"]
        assert "@foo-bar-2/tool" in config["tools"]
        assert "@foo-bar-3" in config["tools"]
        assert not [
            ref
            for ref in config["allowedTools"]
            if ref in {"@foo-bar", "@foo-bar-2", "@foo-bar-3"}
            or str(ref).startswith(("@foo-bar/", "@foo-bar-2/", "@foo-bar-3/"))
        ]
        assert "@foo-barn-2/keep" in config["allowedTools"]
        assert "@foo-bar-2x/keep" in config["allowedTools"]
        assert "@other-live/keep" in config["tools"]
        assert "@other-live/keep" in config["allowedTools"]

    def test_alias_keyed_sibling_keeps_mount_but_loses_family_grant(self, tmp_path: Path):
        """Alias-equivalent sources deny grants without guessed mount ownership.

        Agent refs are written as ``@<mcp_server_alias(name)>``, which is
        many-to-one: ``acme:@acme/notion`` and ``acme-notion`` are different
        source keys in the same canonical family. The disabled slashed source
        denies that family's auto-approval, while the alias-keyed source keeps
        its concrete mount because spec identity is not ownership proof.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        # Store entry keyed by the ALIAS form, with no `disabled` key.
        (user_home / "mcp.json").write_text(
            json.dumps({"mcpServers": {"acme-notion": {"url": "https://mcp.acme.com/mcp"}}})
        )
        # Kiro global keyed by the SLASHED form -- the operator's disable.
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "acme:@acme/notion": {
                            "url": "https://mcp.acme.com/mcp",
                            "disabled": True,
                        }
                    }
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))

        assert "@acme-notion" in config.get("tools", []), "the sibling's mount must survive"
        assert "@acme-notion" not in config.get(
            "allowedTools", []
        ), "the disabled family must not be auto-approved"

    def test_an_agent_config_only_server_keeps_its_oauth_hints_verbatim(self, tmp_path: Path):
        """The agent config is a merge source, so its own entries are preserved.

        A remote server can be defined only in the agent config -- added with
        ``kiro-cli mcp add --agent kirocrew``, or hand-edited in. No mcp.json scope
        declares it and no store entry owns it, so the file is the ONLY copy of its
        OAuth hints. The rebuild merges onto that file, so rewriting the hints here
        would destroy them with nothing to restore from.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        (user_home / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
        (tmp_path / "fake_kiro_mcp.json").write_text(json.dumps({"mcpServers": {}}))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        hand_added = {
            "url": "https://mcp.handadded.com/mcp",
            "oauthScopes": ["hand:read"],
            "oauth": {"clientId": "hand-client", "issuer": "https://issuer.example"},
        }
        config.setdefault("mcpServers", {})["handadded"] = dict(hand_added)
        path.write_text(json.dumps(config), encoding="utf-8")

        for _ in range(2):
            path = _run_install(tmp_path, cfg_dir)
            entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["handadded"]
            assert entry["oauthScopes"] == ["hand:read"], "the only copy must survive"
            assert entry["oauth"]["clientId"] == "hand-client"
            assert entry["oauth"]["issuer"] == "https://issuer.example"

    def test_a_store_clear_persists_across_a_later_rebuild(self, tmp_path: Path):
        """Narrowing a grant is the store's job, and the rebuild honours it.

        The store states the hints, so it owns them: once it stops stating them the
        emitted spec stops requesting them, and stays that way on every later
        rebuild rather than being refilled from the previous render.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        (tmp_path / "fake_kiro_mcp.json").write_text(json.dumps({"mcpServers": {}}))
        store = user_home / "mcp.json"
        url = "https://mcp.acme.com/mcp"

        store.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "acme": {"url": url, "scopes": ["acme:read"], "clientId": "acme-client"}
                    }
                }
            )
        )
        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["acme"]
        assert entry["oauthScopes"] == ["acme:read"]
        assert entry["oauth"]["clientId"] == "acme-client"

        # The editor clears both hints: the store entry survives, stating neither.
        store.write_text(json.dumps({"mcpServers": {"acme": {"url": url}}}))
        for _ in range(2):
            path = _run_install(tmp_path, cfg_dir)
            entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["acme"]
            assert "oauthScopes" not in entry, "the clear must not be refilled"
            assert "clientId" not in entry.get("oauth", {}), "nor the client id"

    def test_a_slash_keyed_remote_survives_repeated_rebuilds(self, tmp_path: Path):
        """Key normalization and the store lookup must agree on the name form.

        ``_normalize_mcp_server_keys`` rewrites a slashed key to its alias, so
        the NEXT rebuild reads the aliased key off disk while the source still
        declares the slashed one, and the merge re-inserts the slashed spelling
        alongside it. Both must converge on one entry carrying the source's hints:
        an equivalent pair collapses onto the canonical alias, so a repeated
        rebuild neither grows ``mcpServers`` nor accumulates ``tools`` refs.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        (user_home / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "acme:@acme/notion": {
                            "url": "https://mcp.acme.com/mcp",
                            "oauthScopes": ["acme:read"],
                            "oauth": {"clientId": "acme-client"},
                        }
                    }
                }
            )
        )

        counts: list[int] = []
        for _ in range(3):
            path = _run_install(tmp_path, cfg_dir)
            config = json.loads(path.read_text(encoding="utf-8"))
            servers = config.get("mcpServers", {})
            counts.append(len(servers))
            entry = servers.get("acme-notion")
            assert entry is not None, "the aliased server must stay mounted"
            assert entry.get("oauthScopes") == ["acme:read"], "a live source keeps its scopes"
            assert entry.get("oauth", {}).get("clientId") == "acme-client"
            assert not [
                k for k in servers if k.startswith("acme-notion-")
            ], f"no duplicate sibling may be minted, got {sorted(servers)}"
        assert counts[0] == counts[1] == counts[2], f"entry count must not grow: {counts}"

    def test_a_slashed_store_name_owns_its_aliased_config_entry(self, tmp_path: Path):
        """The store lookup must use the same name form as the ownership test.

        The store keeps its own raw (slashed) key while normalization rewrites the
        config key to the alias. Looking the store up by the raw key alone misses
        the owner of the aliased entry, so it reads as unmanaged and the
        last-rendered wire hints are preserved verbatim -- an editor clear
        answers 200 and never takes effect, and the now-divergent specs stop
        deduping, minting a fresh sibling on every rebuild.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        (tmp_path / "fake_kiro_mcp.json").write_text(json.dumps({"mcpServers": {}}))
        store = user_home / "mcp.json"
        url = "https://mcp.acme.com/mcp"

        # Rebuild 1: the store states scopes, so the emitted spec carries them.
        store.write_text(
            json.dumps({"mcpServers": {"acme/notion": {"url": url, "scopes": ["acme:read"]}}})
        )
        path = _run_install(tmp_path, cfg_dir)
        assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["acme-notion"][
            "oauthScopes"
        ] == ["acme:read"]

        # The user clears the hints in the editor: the store entry keeps its raw
        # slashed key and simply states no scopes.
        store.write_text(json.dumps({"mcpServers": {"acme/notion": {"url": url}}}))
        path = _run_install(tmp_path, cfg_dir)
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]

        assert "oauthScopes" not in servers["acme-notion"], "the cleared scopes must not survive"
        assert not [
            k for k in servers if k.startswith("acme-notion-")
        ], f"no duplicate sibling may be minted, got {sorted(servers)}"

    def test_a_malformed_exact_name_does_not_hide_a_valid_aliased_owner(self, tmp_path: Path):
        """A malformed store value contributes nothing -- including no veto.

        The store can hold a usable slashed entry whose alias collides with a
        malformed value under the alias key itself. The malformed value states
        nothing, so it must not stand in for the real owner: gating the alias
        lookup on absence alone lets it shadow that owner, the entry reads as
        unmanaged, and the last-rendered hints survive a clear.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        (tmp_path / "fake_kiro_mcp.json").write_text(json.dumps({"mcpServers": {}}))
        store = user_home / "mcp.json"
        url = "https://mcp.acme.com/mcp"

        store.write_text(
            json.dumps({"mcpServers": {"acme:@acme/notion": {"url": url, "scopes": ["acme:read"]}}})
        )
        path = _run_install(tmp_path, cfg_dir)
        assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["acme-notion"][
            "oauthScopes"
        ] == ["acme:read"]

        # The owner clears its scopes; a malformed value now sits under the alias.
        store.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "acme:@acme/notion": {"url": url},
                        "acme-notion": "not-a-dict",
                    }
                }
            )
        )
        path = _run_install(tmp_path, cfg_dir)
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]

        assert (
            "oauthScopes" not in servers["acme-notion"]
        ), "the valid aliased owner's clear must apply"
        assert not [
            k for k in servers if k.startswith("acme-notion-")
        ], f"no duplicate sibling may be minted, got {sorted(servers)}"

    def test_an_alias_match_binds_hints_only_to_the_same_server(self, tmp_path: Path):
        """One rule for every alias binding, keyed on the direction of the effect.

        ``mcp_server_alias`` is many-to-one, so two unrelated names can collide on
        one alias. A binding that GRANTS something (OAuth hints) must therefore
        also match transport identity -- otherwise a managed server's credentials
        land on a different, user-owned server. A binding that DENIES something
        (the disabled guard) deliberately stays name-only: over-matching there
        merely over-disables, while under-matching would let an operator's disable
        be missed, which is the hole the tightest-wins rule closes.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        user_url = "https://user.example.com/mcp"
        managed_url = "https://managed.example.com/mcp"

        # GRANT site: a managed slashed entry aliases onto a user-owned global name.
        (user_home / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "foo/bar": {
                            "url": managed_url,
                            "scopes": ["managed:read"],
                            "clientId": "managed-client",
                        }
                    }
                }
            )
        )
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps({"mcpServers": {"foo-bar": {"url": user_url}}})
        )
        path = _run_install(tmp_path, cfg_dir)
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        theirs = next(e for e in servers.values() if e.get("url") == user_url)
        assert "oauthScopes" not in theirs, "a different server must not receive these scopes"
        assert "clientId" not in theirs.get("oauth", {}), "nor this client id"

        # GRANT site, legitimate case: the aliased entry IS this server (same url).
        (user_home / "mcp.json").write_text(
            json.dumps({"mcpServers": {"foo/bar": {"url": user_url, "scopes": ["managed:read"]}}})
        )
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps({"mcpServers": {"foo-bar": {"url": user_url}}})
        )
        path = _run_install(tmp_path, cfg_dir)
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        assert any(
            e.get("oauthScopes") == ["managed:read"] for e in servers.values()
        ), "the same server's hints must still bind through the alias"

    def test_the_disabled_guard_preserves_a_distinct_alias_collision(self, tmp_path: Path):
        """A collision sibling keeps its mount but loses its family grant."""
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        disabled_url = "https://m.example.com/mcp"
        enabled_url = "https://u.example.com/mcp"
        (user_home / "mcp.json").write_text(
            json.dumps({"mcpServers": {"foo/bar": {"url": disabled_url, "disabled": True}}})
        )
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps({"mcpServers": {"foo-bar": {"url": enabled_url}}})
        )

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))

        assert config["mcpServers"]["foo-bar"]["url"] == enabled_url
        assert config["mcpServers"]["foo-bar-2"]["url"] == disabled_url
        assert "@foo-bar" in config.get("tools", []), "the enabled sibling must mount"
        assert "@foo-bar" not in config.get("allowedTools", []), "its family grant is denied"
        assert "@foo-bar-2" not in config.get("tools", []), "the disabled server must not mount"
        assert "@foo-bar-2" not in config.get("allowedTools", []), "or receive a grant"

    @pytest.mark.parametrize(
        ("enabled_spec", "disabled_spec"),
        [
            (
                {"command": "/opt/shared-tool"},
                {"command": "shared-tool", "disabled": True},
            ),
            (
                {
                    "command": "shared-tool",
                    "args": ["enabled"],
                    "env": {"MODE": "enabled"},
                },
                {
                    "command": "shared-tool",
                    "args": ["disabled"],
                    "env": {"MODE": "disabled"},
                    "disabled": True,
                },
            ),
        ],
        ids=("command-spelling", "args-and-env"),
    )
    def test_disabled_family_revokes_grants_across_spec_differences(
        self,
        tmp_path: Path,
        enabled_spec: dict,
        disabled_spec: dict,
    ):
        """Spec-field differences cannot preserve a canonical-family grant."""
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["@foo-bar", "@foo-bar/run", "@foo-bar-2", "@foo-bar-2/run"],
            "allowedTools": [
                "@foo-bar",
                "@foo-bar/run",
                "@foo-bar-2",
                "@foo-bar-2/run",
            ],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps({"mcpServers": {"foo-bar": enabled_spec}})
        )
        (user_home / "mcp.json").write_text(json.dumps({"mcpServers": {"foo/bar": disabled_spec}}))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))

        assert "@foo-bar" in config["tools"], "the enabled collision sibling must mount"
        assert "@foo-bar/run" in config["tools"], "its selective mount must survive"
        assert not [
            ref
            for ref in config["allowedTools"]
            if ref == "@foo-bar" or str(ref).startswith("@foo-bar/")
        ], "the disabled canonical family must deny the sibling grant"
        assert "@foo-bar-2" not in config["tools"]
        assert "@foo-bar-2/run" in config["tools"]

    def test_a_hand_named_suffix_is_not_claimed_by_the_alias_family(self, tmp_path: Path):
        """A ``-n`` name a user chose is theirs; the family search must not claim it.

        Nothing distinguishes a name normalization minted from one a user typed, so
        the family search needs corroboration that a mint actually happened. Absent
        it, a store entry at the same transport would rewrite a hand-authored
        entry's grant and inject its own client identity into a file we do not own.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        url = "https://mcp.notion.com/mcp"
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps({"mcpServers": {"notion-2": {"url": url, "oauthScopes": ["hand:write"]}}})
        )
        (user_home / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "notion": {
                            "url": url,
                            "scopes": ["store:read"],
                            "clientId": "store-client",
                        }
                    }
                }
            )
        )
        path = _run_install(tmp_path, cfg_dir)
        hand = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["notion-2"]
        assert hand.get("oauthScopes") == ["hand:write"], f"hand-authored grant rewritten: {hand}"
        assert "oauth" not in hand, f"store client identity injected: {hand}"

    def test_two_owners_sharing_alias_and_url_keep_their_own_grants(self, tmp_path: Path):
        """Transport identity alone cannot break a tie between two owners.

        When two store names share both an alias and a url, picking the first
        candidate is an insertion-order coin flip -- one owner's grant lands in the
        other's slot. An ambiguous family match must resolve to no owner instead.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        (tmp_path / "fake_kiro_mcp.json").write_text(json.dumps({"mcpServers": {}}))
        url = "https://shared.example.com/mcp"
        (user_home / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "foo-bar": {"url": url, "scopes": ["b:read"]},
                        "foo/bar": {"url": url, "scopes": ["a:read"]},
                    }
                }
            )
        )
        counts: list[int] = []
        for _ in range(3):
            path = _run_install(tmp_path, cfg_dir)
            servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
            counts.append(len(servers))
            grants = [
                tuple(e["oauthScopes"])
                for e in servers.values()
                if e.get("url") == url and "oauthScopes" in e
            ]
            assert grants.count(("a:read",)) <= 1, f"a grant duplicated across slots: {servers}"
            assert grants.count(("b:read",)) <= 1, f"a grant duplicated across slots: {servers}"
        assert counts[0] == counts[-1] == counts[1], f"entry count must not grow: {counts}"

    def test_a_suffixed_alias_keeps_its_managed_owner(self, tmp_path: Path):
        """A collision-suffixed entry is still the same server, so still owned.

        When a managed name's alias is already held by a genuinely different
        server, normalization preserves the managed one under a numeric suffix.
        That suffixed key matches neither the store key nor its alias, so an
        ownership lookup that stops there reads the entry as unmanaged and
        preserves hints the store does not state -- the clear stops applying to
        exactly the server the store owns.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        user_url = "https://user.example.com/mcp"
        managed_url = "https://managed.example.com/mcp"
        store = user_home / "mcp.json"

        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps({"mcpServers": {"foo-bar": {"url": user_url}}})
        )
        store.write_text(
            json.dumps(
                {"mcpServers": {"foo/bar": {"url": managed_url, "scopes": ["managed:read"]}}}
            )
        )
        path = _run_install(tmp_path, cfg_dir)
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        assert any(
            e.get("url") == managed_url and e.get("oauthScopes") == ["managed:read"]
            for e in servers.values()
        ), "the managed server's hints render somewhere"

        # The store clears the grant; the managed entry lives under the suffix.
        store.write_text(json.dumps({"mcpServers": {"foo/bar": {"url": managed_url}}}))
        path = _run_install(tmp_path, cfg_dir)
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]

        stale = [
            k for k, e in servers.items() if e.get("url") == managed_url and "oauthScopes" in e
        ]
        assert not stale, f"the owner's clear must reach its suffixed entry, stale in {stale}"

    def test_two_store_owners_sharing_one_alias_both_stay_resolvable(self, tmp_path: Path):
        """An alias index must not drop an owner just because another shares its alias.

        Two store entries can alias to the same slug, so keying the index by alias
        alone keeps only one of them. The other server's collision-suffixed entry
        then finds a candidate whose transport does not match, reads as unmanaged,
        and keeps a grant its owner cleared -- and because the stale copy does not
        dedup against the freshly rendered one, each rebuild mints another sibling.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        (tmp_path / "fake_kiro_mcp.json").write_text(json.dumps({"mcpServers": {}}))
        store = user_home / "mcp.json"
        url_a = "https://a.example.com/mcp"
        url_b = "https://b.example.com/mcp"

        store.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "foo/bar": {"url": url_a, "scopes": ["a:read"]},
                        "foo-bar": {"url": url_b, "scopes": ["b:read"]},
                    }
                }
            )
        )
        path = _run_install(tmp_path, cfg_dir)
        first = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        assert any(e.get("url") == url_a for e in first.values()), "both servers render"
        assert any(e.get("url") == url_b for e in first.values())

        # Owner A clears its grant; B keeps its own.
        store.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "foo/bar": {"url": url_a},
                        "foo-bar": {"url": url_b, "scopes": ["b:read"]},
                    }
                }
            )
        )
        counts: list[int] = []
        for _ in range(2):
            path = _run_install(tmp_path, cfg_dir)
            servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
            counts.append(len(servers))
            stale = [k for k, e in servers.items() if e.get("url") == url_a and "oauthScopes" in e]
            assert not stale, f"owner A's clear must apply, stale in {stale}"
            assert any(
                e.get("url") == url_b and e.get("oauthScopes") == ["b:read"]
                for e in servers.values()
            ), "owner B keeps its own grant"
        assert counts[0] == counts[1], f"entry count must not grow: {counts}"

    def test_an_enabled_store_server_still_mounts_with_a_global_sibling(self, tmp_path: Path):
        """The tightest-wins gate must not break the enabled path it guards.

        Same two-scope shape as the test above with nothing disabled anywhere --
        this is the case the slice exists to fix, so it must keep working.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(parents=True, exist_ok=True)
        (user_home / "mcp.json").write_text(
            json.dumps({"mcpServers": {"notion": {"url": "https://mcp.notion.com/mcp"}}})
        )
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps({"mcpServers": {"notion": {"url": "https://mcp.notion.com/mcp"}}})
        )

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))

        assert "@notion" in config["tools"]

    def test_malformed_allowedtools_entries_are_dropped(self, tmp_path: Path):
        """A non-string allowedTools entry (hand-edited config) must be dropped,
        not crash rebuild via may_skip_gate's ref.startswith()."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["use_aws"],
            "allowedTools": [1, "use_aws", None, "web_fetch"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)  # must not raise
        config = json.loads(path.read_text(encoding="utf-8"))
        # Non-string junk dropped; valid non-floor entries preserved.
        assert config["allowedTools"] == ["use_aws", "web_fetch"]

    def test_dedup_preserves_order(self, tmp_path: Path):
        """Duplicate tools are removed while preserving first-occurrence order."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["shell", "read", "shell", "use_aws", "read"],
            "allowedTools": ["read", "read", "use_aws"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["tools"] == ["shell", "read", "use_aws"]
        # Dedup preserves first-occurrence order. ("read"/"shell"/"use_aws" are
        # not floor builtins, so none is withheld from auto-approve here.)
        assert config["allowedTools"] == ["read", "use_aws"]

    def test_non_dict_json_treated_as_fresh_install(self, tmp_path: Path):
        """Valid JSON that is not a dict → treated as fresh install."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        (kiro_dir / "kirocrew.json").write_text('"just a string"')

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # Should get defaults (fresh install path)
        assert config["model"] == "claude-default"
        assert "@kirocrew-cron" in config["tools"]
