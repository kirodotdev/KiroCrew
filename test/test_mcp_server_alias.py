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

    def test_rewrites_the_per_tool_spelling_carrying_the_suffix_verbatim(self):
        """``@oldkey/tool`` moves with ``@oldkey``; the suffix is carried verbatim.

        A per-tool ref left on the old key names a server this pass just
        removed, so the final reconcile reads it as dangling and drops it from
        BOTH lists -- and a dropped ``tools`` ref is never re-added, while a
        dropped ``allowedTools`` grant is a lost per-tool approval. The server
        key itself contains a slash (``npm:@scope/pkg``), so a rewrite that
        re-derives the suffix by splitting the ref takes the wrong component:
        only a prefix match that carries the remainder verbatim survives it.
        """
        cfg = {
            "mcpServers": {"npm:@scope/pkg": {"command": "x"}},
            "tools": [
                "@npm:@scope/pkg",
                "@npm:@scope/pkg/tool_a",
                # Control (over-application): shares the prefix without the
                # ``/`` boundary, so it is another server's ref and must not
                # be rewritten.
                "@npm:@scope/pkgx",
            ],
            "allowedTools": ["@npm:@scope/pkg/tool_a", "@npm:@scope/pkg/tool_b"],
        }
        _normalize_mcp_server_keys(cfg)
        assert cfg["mcpServers"]["scope-pkg"] == {"command": "x"}
        assert cfg["tools"] == ["@scope-pkg", "@scope-pkg/tool_a", "@npm:@scope/pkgx"]
        assert cfg["allowedTools"] == ["@scope-pkg/tool_a", "@scope-pkg/tool_b"]

    def test_allowed_tools_keeps_the_narrow_runtime_reading_when_claimable(self):
        ambiguous = {
            "mcpServers": {
                "a": {"command": "ancestor"},
                "a/b": {"command": "descendant"},
            },
            "tools": ["@a/b"],
            "allowedTools": ["@a/b"],
        }
        _normalize_mcp_server_keys(ambiguous)
        assert ambiguous["mcpServers"] == {
            "a": {"command": "ancestor"},
            "a-b": {"command": "descendant"},
        }
        assert ambiguous["tools"] == ["@a-b"]
        assert ambiguous["allowedTools"] == ["@a/b"]
        assert "@a-b" not in ambiguous["allowedTools"]

        unambiguous = {
            "mcpServers": {"a/b": {"command": "descendant"}},
            "allowedTools": ["@a/b"],
        }
        _normalize_mcp_server_keys(unambiguous)
        assert unambiguous["mcpServers"] == {"a-b": {"command": "descendant"}}
        assert unambiguous["allowedTools"] == ["@a-b"]

        deeper = {
            "mcpServers": {
                "a": {"command": "ancestor"},
                "a/b/c": {"command": "descendant"},
            },
            "allowedTools": ["@a/b/c"],
        }
        _normalize_mcp_server_keys(deeper)
        assert deeper["mcpServers"] == {
            "a": {"command": "ancestor"},
            "a-b-c": {"command": "descendant"},
        }
        assert deeper["allowedTools"] == ["@a/b/c"]

    def test_an_ancestor_key_does_not_steal_a_descendant_keys_ref(self):
        """Overlapping slash keys: the longer key's bare ref is not a per-tool ref.

        With ``a/b`` and ``a/b/c`` both present, ``@a/b/c`` is ambiguous text:
        the bare ref of server ``a/b/c``, or tool ``c`` on server ``a/b``. An
        exact server-key match is the stronger evidence, so the longer key must
        claim it -- processed ancestor-first, ``a/b``'s per-tool rewrite would
        steal it, stranding ``a-b-c`` in the map with no ref (``tools`` is a
        closed allowlist, so the server would mount and expose nothing) and
        leaving a grant under ``a-b``'s namespace instead.
        """
        cfg = {
            "mcpServers": {
                # Ancestor inserted FIRST, so insertion order alone would
                # process it first; the pass must order by length instead.
                "a/b": {"command": "x"},
                "a/b/c": {"command": "y"},
            },
            "tools": ["@a/b", "@a/b/tool", "@a/b/c", "@a/b/c/tool"],
            "allowedTools": ["@a/b/c"],
        }
        _normalize_mcp_server_keys(cfg)
        assert cfg["mcpServers"]["a-b"] == {"command": "x"}
        assert cfg["mcpServers"]["a-b-c"] == {"command": "y"}
        assert cfg["tools"] == ["@a-b", "@a-b/tool", "@a-b-c", "@a-b-c/tool"]
        assert cfg["allowedTools"] == ["@a-b-c"]

    def test_an_ancestor_key_cannot_absorb_a_reserved_descendants_refs(self):
        """An absent descendant's refs stay verbatim and outside its ancestor.

        A reserved key cannot take part in the present-key longest-first pass.
        Its refs must remain immovable while the present ancestor moves to its
        alias, or the ancestor inherits the descendant's mount and auto-approval.
        """
        cfg = {
            "mcpServers": {"a/b": {"command": "x"}},
            "tools": ["@a/b", "@a/b/tool", "@a/b/c", "@a/b/c/tool"],
            "allowedTools": ["@a/b", "@a/b/tool", "@a/b/c", "@a/b/c/tool"],
        }
        _normalize_mcp_server_keys(cfg, reserved_keys={"a/b/c"})
        assert cfg["mcpServers"] == {"a-b": {"command": "x"}}
        assert cfg["tools"] == ["@a-b", "@a-b/tool", "@a/b/c", "@a/b/c/tool"]
        assert cfg["allowedTools"] == [
            "@a-b",
            "@a-b/tool",
            "@a/b/c",
            "@a/b/c/tool",
        ]
        for key in ("tools", "allowedTools"):
            assert "@a-b/c" not in cfg[key]
            assert "@a-b/c/tool" not in cfg[key]

    def test_reserved_alias_collision_does_not_transfer_refs_to_live_server(self):
        """An unresolved key cannot lend mounts or grants to an occupied alias."""
        cfg = {
            "mcpServers": {"scope-pkg": {"command": "y"}},
            "tools": [
                "@npm:@scope/pkg",
                "@npm:@scope/pkg/tool",
                "@scope-pkg",
            ],
            "allowedTools": [
                "@npm:@scope/pkg",
                "@npm:@scope/pkg/tool",
                "@scope-pkg",
            ],
        }

        _normalize_mcp_server_keys(cfg, reserved_keys={"npm:@scope/pkg"})

        assert cfg["mcpServers"] == {"scope-pkg": {"command": "y"}}
        assert cfg["tools"] == [
            "@npm:@scope/pkg",
            "@npm:@scope/pkg/tool",
            "@scope-pkg",
        ]
        assert cfg["allowedTools"] == [
            "@npm:@scope/pkg",
            "@npm:@scope/pkg/tool",
        ]
        assert "@scope-pkg/tool" not in cfg["tools"]
        assert "@scope-pkg/tool" not in cfg["allowedTools"]

    def test_slash_free_reserved_key_grant_cannot_transfer_to_colliding_live_server(self):
        """A slash-free unresolved key's stale grant never approves the live occupant.

        ``foo-bar`` is reserved (declared, unresolved this pass) while a present
        ``foo/bar`` normalizes onto that exact name. The pre-existing
        ``@foo-bar`` grants belong to the absent server and must be dropped --
        surviving, they would auto-approve the live server on the one list that
        never reaches the PreToolUse gate.
        """
        cfg = {
            "mcpServers": {"foo/bar": {"command": "y"}},
            "tools": ["@foo/bar", "@foo-bar"],
            "allowedTools": ["@foo-bar", "@foo-bar/tool"],
        }
        removed: list[str] = []

        _normalize_mcp_server_keys(cfg, reserved_keys={"foo-bar"}, removed_grants=removed)

        # The live server still lands on its canonical alias.
        assert cfg["mcpServers"] == {"foo-bar": {"command": "y"}}
        # The live ref is rewritten; the reserved key's frozen ref dedupes into it.
        assert cfg["tools"] == ["@foo-bar"]
        # The stale grants must NOT survive to approve the live occupant.
        assert cfg["allowedTools"] == []
        assert set(removed) == {"@foo-bar", "@foo-bar/tool"}

    def test_persisted_reserved_alias_grants_only_survive_without_a_live_occupant(self):
        """Alias-family grants cannot approve distinct servers occupying those aliases."""
        cfg = {
            "mcpServers": {
                "foo-bar": {"command": "live-canonical"},
                "foo-bar-2": {"command": "live-sibling"},
            },
            "tools": ["@foo-bar", "@foo-bar/keep"],
            "allowedTools": [
                "@foo-bar",
                "@foo-bar/tool",
                "@foo-bar-2/y",
                "@baz-qux/x",
                "@foo-barn/x",
                "@foo-bar-2x/z",
            ],
        }

        _normalize_mcp_server_keys(cfg, reserved_keys={"foo/bar", "baz/qux"})

        assert cfg["mcpServers"] == {
            "foo-bar": {"command": "live-canonical"},
            "foo-bar-2": {"command": "live-sibling"},
        }
        assert cfg["tools"] == ["@foo-bar", "@foo-bar/keep"]
        assert cfg["allowedTools"] == [
            "@baz-qux/x",
            "@foo-barn/x",
            "@foo-bar-2x/z",
        ]

    def test_a_reserved_alias_never_lands_on_a_present_keys_computed_alias(self):
        """Reserved refs stay verbatim when a present key computes the same alias."""
        cfg = {
            "mcpServers": {"npm:@scope/pkg": {"command": "x"}},
            "tools": [
                "@npm:@scope/pkg",
                "@npm:@scope/pkg/present-tool",
                "@pip:@scope/pkg",
                "@pip:@scope/pkg/reserved-tool",
            ],
            "allowedTools": [
                "@npm:@scope/pkg",
                "@npm:@scope/pkg/present-tool",
                "@pip:@scope/pkg",
                "@pip:@scope/pkg/reserved-tool",
            ],
        }

        _normalize_mcp_server_keys(cfg, reserved_keys={"pip:@scope/pkg"})

        assert cfg["mcpServers"] == {"scope-pkg": {"command": "x"}}
        assert cfg["tools"] == [
            "@scope-pkg",
            "@scope-pkg/present-tool",
            "@pip:@scope/pkg",
            "@pip:@scope/pkg/reserved-tool",
        ]
        assert cfg["allowedTools"] == [
            "@pip:@scope/pkg",
            "@pip:@scope/pkg/reserved-tool",
        ]
        for key in ("tools", "allowedTools"):
            assert "@scope-pkg/reserved-tool" not in cfg[key]

    def test_a_reserved_ancestor_cannot_freeze_a_present_descendants_refs(self):
        """Longest-match ownership lets a present descendant move normally."""
        cfg = {
            "mcpServers": {"a/b/c": {"command": "x"}},
            "tools": ["@a/b/c", "@a/b/c/tool"],
            "allowedTools": ["@a/b/c", "@a/b/c/tool"],
        }

        _normalize_mcp_server_keys(cfg, reserved_keys={"a/b"})

        assert cfg["mcpServers"] == {"a-b-c": {"command": "x"}}
        assert cfg["tools"] == ["@a-b-c", "@a-b-c/tool"]
        assert cfg["allowedTools"] == ["@a-b-c", "@a-b-c/tool"]

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
            json.dumps({"mcpServers": {"npm:@playwright/mcp": {"command": "npx", "args": ["x"]}}})
        )
        monkeypatch.setattr(agents_mod, "_installed_agent_config", lambda: agent_path)
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", global_path)

        mcp_mod._sync_mcp_to_agent("npm:@playwright/mcp", enabled=True)

        cfg = json.loads(agent_path.read_text(encoding="utf-8"))
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

        cfg = json.loads(agent_path.read_text(encoding="utf-8"))
        assert "@playwright-mcp" not in cfg["tools"]
        assert "@npm:@playwright/mcp" not in cfg["tools"]
        assert "playwright-mcp" not in cfg["mcpServers"]
