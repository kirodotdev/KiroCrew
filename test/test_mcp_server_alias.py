"""Regression tests for kiro-safe MCP server-key aliasing.

kiro-cli resolves agent ``tools``/``allowedTools`` entries (``@server``) by
splitting on ``/``, so a server key containing ``/`` (e.g. the npm-scoped
``npm:@playwright/mcp``) can never be referenced as ``@key`` -- kiro reads the
trailing path segment as a tool name and exposes none of the server's tools.
These tests lock the slash-free aliasing + migration that fixes it.
"""

from __future__ import annotations

import json

from kiro_crew.agent import _normalize_mcp_server_keys
from kiro_crew.mcp_utils import mcp_server_alias


class TestMcpServerAlias:
    def test_slash_free_name_unchanged(self):
        for name in ("builder-mcp", "kirocrew-core", "slack-mcp", "andes-mcp"):
            assert mcp_server_alias(name) == name

    def test_npm_scoped_playwright(self):
        assert mcp_server_alias("npm:@playwright/mcp") == "playwright-mcp"

    def test_registry_namespace_name(self):
        assert mcp_server_alias("namespace/name") == "namespace-name"

    def test_npm_scoped_generic(self):
        assert mcp_server_alias("npm:@scope/pkg") == "scope-pkg"

    def test_deterministic_stable(self):
        # Same input -> same alias across calls (no churn).
        a = mcp_server_alias("npm:@playwright/mcp")
        b = mcp_server_alias("npm:@playwright/mcp")
        assert a == b == "playwright-mcp"

    def test_alias_is_slash_free(self):
        for name in ("npm:@playwright/mcp", "a/b/c", "x:@y/z"):
            assert "/" not in mcp_server_alias(name)

    def test_agent_reexports_same_callable(self):
        # agent.py re-exports the helper from mcp_utils for back-compat; the
        # handlers and agent must share one implementation.
        import kiro_crew.agent as agent_mod

        assert agent_mod.mcp_server_alias is mcp_server_alias


class TestNormalizeMcpServerKeys:
    def test_renames_slash_key_and_rewrites_refs(self):
        cfg = {
            "mcpServers": {"npm:@playwright/mcp": {"command": "x"}},
            "tools": ["@builder-mcp", "@npm:@playwright/mcp"],
            "allowedTools": ["@npm:@playwright/mcp"],
        }
        _normalize_mcp_server_keys(cfg)
        assert "npm:@playwright/mcp" not in cfg["mcpServers"]
        assert cfg["mcpServers"]["playwright-mcp"] == {"command": "x"}
        assert cfg["tools"] == ["@builder-mcp", "@playwright-mcp"]
        assert cfg["allowedTools"] == ["@playwright-mcp"]

    def test_idempotent(self):
        cfg = {
            "mcpServers": {"npm:@playwright/mcp": {"command": "x"}},
            "tools": ["@npm:@playwright/mcp"],
            "allowedTools": ["@npm:@playwright/mcp"],
        }
        _normalize_mcp_server_keys(cfg)
        once = json.dumps(cfg, sort_keys=True)
        _normalize_mcp_server_keys(cfg)
        assert json.dumps(cfg, sort_keys=True) == once

    def test_slash_free_config_untouched(self):
        cfg = {
            "mcpServers": {"builder-mcp": {"command": "x"}},
            "tools": ["@builder-mcp"],
            "allowedTools": ["@builder-mcp"],
        }
        before = json.dumps(cfg, sort_keys=True)
        _normalize_mcp_server_keys(cfg)
        assert json.dumps(cfg, sort_keys=True) == before

    def test_distinct_collision_suffixed_no_data_loss(self):
        # A different server already holds the natural alias -> the slash
        # server is preserved under a numeric suffix (never dropped).
        cfg = {
            "mcpServers": {
                "playwright-mcp": {"command": "existing"},
                "npm:@playwright/mcp": {"command": "distinct"},
            },
            "tools": ["@npm:@playwright/mcp"],
            "allowedTools": [],
        }
        _normalize_mcp_server_keys(cfg)
        assert cfg["mcpServers"]["playwright-mcp"] == {"command": "existing"}
        assert cfg["mcpServers"]["playwright-mcp-2"] == {"command": "distinct"}
        assert cfg["tools"] == ["@playwright-mcp-2"]

    def test_identical_dup_overwritten_and_refs_deduped(self):
        # A byte-identical re-merged duplicate collapses onto the alias with
        # no suffix; the rewritten ref is de-duplicated in place (idempotent).
        cfg = {
            "mcpServers": {
                "playwright-mcp": {"command": "x"},
                "npm:@playwright/mcp": {"command": "x"},
            },
            "tools": ["@npm:@playwright/mcp", "@playwright-mcp"],
            "allowedTools": [],
        }
        _normalize_mcp_server_keys(cfg)
        assert cfg["mcpServers"] == {"playwright-mcp": {"command": "x"}}
        assert cfg["tools"] == ["@playwright-mcp"]

    def test_two_distinct_slash_keys_same_alias_suffixed(self):
        cfg = {
            "mcpServers": {
                "npm:@playwright/mcp": {"command": "a"},
                "pip:@playwright/mcp": {"command": "b"},
            },
            "tools": ["@npm:@playwright/mcp", "@pip:@playwright/mcp"],
            "allowedTools": [],
        }
        _normalize_mcp_server_keys(cfg)
        assert cfg["mcpServers"]["playwright-mcp"] == {"command": "a"}
        assert cfg["mcpServers"]["playwright-mcp-2"] == {"command": "b"}
        assert cfg["tools"] == ["@playwright-mcp", "@playwright-mcp-2"]

    def test_empty_optional_keys_collapse_no_suffix(self):
        # a re-added slash key that differs from the canonical alias
        # only by an empty ``args``/``env`` is the SAME server -> it must reuse
        # the alias (overwrite) instead of minting a -2 suffix. This is the loop
        # that produced playwright-mcp-2..5 on every build/reinstall/update.
        cfg = {
            "mcpServers": {
                "playwright-mcp": {"command": "x", "args": []},
                "npm:@playwright/mcp": {"command": "x", "env": {}},
            },
            "tools": ["@npm:@playwright/mcp", "@playwright-mcp"],
            "allowedTools": [],
        }
        _normalize_mcp_server_keys(cfg)
        # Exactly one entry, empty optionals stripped, no -2 minted.
        assert cfg["mcpServers"] == {"playwright-mcp": {"command": "x"}}
        assert cfg["tools"] == ["@playwright-mcp"]

    def test_converges_preexisting_polluted_siblings(self):
        # A config already polluted by the pre-fix bug (playwright-mcp plus
        # equivalent -2/-3 siblings) self-heals: the siblings fold back onto the
        # canonical alias and their @refs are redirected. A genuinely distinct
        # sibling is preserved.
        cfg = {
            "mcpServers": {
                "playwright-mcp": {"command": "x"},
                "playwright-mcp-2": {"command": "x"},
                "playwright-mcp-3": {"command": "x", "env": {}},
                "playwright-mcp-4": {"command": "DISTINCT"},
                "npm:@playwright/mcp": {"command": "x", "args": []},
            },
            "tools": [
                "@npm:@playwright/mcp",
                "@playwright-mcp-2",
                "@playwright-mcp-4",
            ],
            "allowedTools": ["@playwright-mcp-3"],
        }
        _normalize_mcp_server_keys(cfg)
        assert cfg["mcpServers"] == {
            "playwright-mcp": {"command": "x"},
            "playwright-mcp-4": {"command": "DISTINCT"},
        }
        # Equivalent-sibling refs redirect to the surviving alias; distinct kept.
        assert cfg["tools"] == ["@playwright-mcp", "@playwright-mcp-4"]
        assert cfg["allowedTools"] == ["@playwright-mcp"]

    def test_missing_mcpservers_noop(self):
        cfg = {"tools": []}
        _normalize_mcp_server_keys(cfg)  # must not raise
        assert cfg == {"tools": []}


class TestSyncMcpToAgentSlashName:
    def test_enabling_slash_server_writes_alias_key_and_ref(self, tmp_path, monkeypatch):
        import kiro_crew.dashboard.handlers.agents as agents_mod
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        agent_path = tmp_path / "kirocrew.json"
        agent_path.write_text(json.dumps({"mcpServers": {}, "tools": [], "allowedTools": []}))
        global_path = tmp_path / "global_mcp.json"
        global_path.write_text(
            json.dumps(
                {"mcpServers": {"npm:@playwright/mcp": {"command": "npx", "args": ["x"]}}}
            )
        )
        monkeypatch.setattr(agents_mod, "_installed_agent_config", lambda: agent_path)
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", global_path)

        mcp_mod._sync_mcp_to_agent("npm:@playwright/mcp", enabled=True)

        cfg = json.loads(agent_path.read_text())
        assert "playwright-mcp" in cfg["mcpServers"]
        assert "npm:@playwright/mcp" not in cfg["mcpServers"]
        assert "@playwright-mcp" in cfg["tools"]
        assert "@playwright-mcp" in cfg["allowedTools"]
        assert "@npm:@playwright/mcp" not in cfg["tools"]

    def test_removing_slash_server_strips_alias_and_legacy_refs(self, tmp_path, monkeypatch):
        import kiro_crew.dashboard.handlers.agents as agents_mod
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        agent_path = tmp_path / "kirocrew.json"
        agent_path.write_text(
            json.dumps(
                {
                    "mcpServers": {"playwright-mcp": {"command": "npx"}},
                    "tools": ["@playwright-mcp", "@npm:@playwright/mcp"],
                    "allowedTools": ["@playwright-mcp"],
                }
            )
        )
        global_path = tmp_path / "global_mcp.json"
        global_path.write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setattr(agents_mod, "_installed_agent_config", lambda: agent_path)
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", global_path)

        mcp_mod._sync_mcp_to_agent("npm:@playwright/mcp", enabled=False, remove=True)

        cfg = json.loads(agent_path.read_text())
        assert "@playwright-mcp" not in cfg["tools"]
        assert "@npm:@playwright/mcp" not in cfg["tools"]
        assert "playwright-mcp" not in cfg["mcpServers"]
