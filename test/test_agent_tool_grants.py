"""Tests for tool bloat prevention: a rebuild's mount and auto-approve decisions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_helpers import _pinned_kiro_cli_version  # noqa: F401
from agent_helpers import _run_install

# Shared with ``test_mcp_rebuild_reconsumption`` via a dedicated helpers module --
# the repo's convention, since a test module is not importable from another one.
# Re-exported under the original private names so the call sites below are unchanged.
from mcp_merge_helpers import bundled_defaults as _bundled_defaults


class TestToolBloatFixesMountAndGrant:
    """Tool bloat prevention: which servers a rebuild mounts and which it grants.

    An existing config's own lists and a missing tool_search seed, refs to
    undeclared, unmountable or unresolved servers, and the app claims that decide
    whether a server keeps its auto-approve grant.
    """

    def test_existing_config_tools_untouched(self, tmp_path: Path):
        """Existing tools/allowedTools are preserved exactly as-is (no renames, no additions)."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["execute_bash", "fs_read", "fs_write", "use_aws", "code"],
            "allowedTools": ["fs_read", "use_aws"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # Template (_bundled_defaults) does not grant tool_search, so the narrow
        # seed does not fire and the user's tools (MOUNT) list is preserved exactly.
        assert config["tools"] == ["execute_bash", "fs_read", "fs_write", "use_aws", "code"]
        # allowedTools (AUTO-APPROVE) is different: a floor builtin (fs_read —
        # sensitive-path) is withheld even from a user's explicit grant, because
        # auto-approve skips the always-on gate floor. It still auto-approves via
        # hooks after that floor runs. use_aws carries no such floor → preserved.
        assert config["allowedTools"] == ["use_aws"]

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

    def test_existing_config_seeds_missing_tool_search(self, tmp_path: Path):
        """Existing config missing tool_search gains it (ADD-only) when the
        shipped template grants it, appended without disturbing other tools."""
        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["execute_bash", "fs_read", "code", "@builder-mcp"],
            "allowedTools": ["fs_read", "@builder-mcp"],
            # DECLARED, because the ref is what this test preserves. An empty map
            # beside an `@builder-mcp` ref is the dangling state the rebuild's ref
            # reconcile removes, so leaving it empty would make the two assertions
            # below depend on that rather than on tool_search seeding.
            "mcpServers": {"builder-mcp": {"command": "/bin/true"}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # tool_search appended at the end; all prior tools preserved in order.
        assert config["tools"] == [
            "execute_bash",
            "fs_read",
            "code",
            "@builder-mcp",
            "tool_search",
        ]
        # Read-only auto-allowed built-in: NOT added to allowedTools.
        assert "tool_search" not in config["allowedTools"]
        # fs_read is a floor builtin (sensitive-path) → withheld from auto-approve
        # even though the user's config listed it; @builder-mcp (ungoverned MCP
        # ref, no ceiling) is preserved.
        assert config["allowedTools"] == ["@builder-mcp"]

    def test_a_rebuild_drops_a_ref_whose_server_the_map_does_not_declare(self, tmp_path: Path):
        """The assembled config leaves no ref to a server the map does not declare.

        Pins the CALL SITE, not the helper: the rebuild narrows ``mcpServers`` in
        passes that never touch the ref lists, so a rendered file carrying a ref
        whose server is absent would keep probing that dead name every cycle. The
        ``allowedTools`` side is the one that costs something -- that list never
        reaches the PreToolUse gate, so the grant sits on the NAME and a server
        added under it would inherit an approval nobody granted for it.
        """
        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@kept", "@orphan", "@orphan/search"],
            "allowedTools": ["@kept", "@orphan"],
            "mcpServers": {"kept": {"command": "/bin/true"}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        config = json.loads(_run_install(tmp_path, cfg_dir).read_text(encoding="utf-8"))

        assert "orphan" not in config["mcpServers"]
        assert not [r for r in config["tools"] if str(r).startswith("@orphan")]
        assert not [r for r in config["allowedTools"] if str(r).startswith("@orphan")]
        # The declared server keeps BOTH its mount and its grant, so the pass is
        # measured on both sides rather than only on the removal.
        assert "@kept" in config["tools"]
        assert "@kept" in config["allowedTools"]
        assert "fs_read" in config["tools"]

    def test_a_declaration_the_rebuild_cannot_ever_mount_loses_its_grant(
        self, tmp_path: Path, monkeypatch
    ):
        """Only an unresolved COMMAND is exempt from the ref reconcile, never a stub.

        A declaration with no command, and one that is not even a spec, leave
        nothing for a later pass to resolve, so their refs are real leftovers. An
        `allowedTools` leftover is the one that costs something: that list never
        reaches the PreToolUse gate, so the grant sits on the NAME and whatever
        server is bound to it next inherits an approval nobody granted for it.

        Asserted on the exempt set as well as the rendered file, because the set
        IS the classification and a set built from "declared but not in the final
        map" would sweep both of these in while every assertion about the mounted
        server still passed.
        """
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@mounted", "@commandless", "@notaspec"],
            "allowedTools": ["@mounted", "@commandless", "@notaspec/run"],
            "mcpServers": {
                # sys.executable, not a POSIX literal: the assertions below are
                # that this one mounts and keeps both refs, so it has to resolve
                # on every platform the shards run.
                "mounted": {"command": sys.executable},
                "commandless": {"args": ["--serve"]},
                "notaspec": "this is not a server spec",
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        exempted: list[set[str]] = []
        real_prune = agent_mod.prune_dangling_tool_refs

        def _spy(cfg, *, declared=(), declared_grants=None):
            exempted.append(set(declared))
            return real_prune(cfg, declared=declared, declared_grants=declared_grants)

        monkeypatch.setattr(agent_mod, "prune_dangling_tool_refs", _spy)

        config = json.loads(_run_install(tmp_path, cfg_dir).read_text(encoding="utf-8"))

        assert exempted, "the rebuild never reached the ref reconcile"
        assert "commandless" not in exempted[-1], (exempted, sorted(config["mcpServers"]))
        assert "notaspec" not in exempted[-1]
        # Neither reaches the map, so neither may keep a ref -- and the grant side
        # is asserted separately from the tools side because only one of them is
        # gated at call time.
        for _dead in ("@commandless", "@notaspec"):
            assert not [r for r in config["tools"] if str(r).startswith(_dead)]
            assert not [r for r in config["allowedTools"] if str(r).startswith(_dead)]
        # The mounted server is the control: it keeps both, so the pass is
        # measured as a discrimination rather than as a blanket removal.
        assert "@mounted" in config["tools"]
        assert "@mounted" in config["allowedTools"]
        assert "fs_read" in config["tools"]

    def test_a_declared_command_that_did_not_resolve_keeps_its_mount_not_its_grant(
        self, tmp_path: Path, monkeypatch
    ):
        """A server whose command is merely not installed yet keeps the mount, loses the grant.

        The two lists answer different questions and this is the input where they
        part. Dropping the MOUNT would unmount the server permanently, because an
        existing config never re-adds a template ref and the next rebuild reverses
        this absence once the binary is installed. Dropping the GRANT costs one
        approval a human can grant again.

        So the grant goes. While the binary is absent nothing declares this name,
        and an `allowedTools` entry never reaches the PreToolUse gate, so the
        approval would sit on the NAME for whatever binds to it next. Having read
        every app claim does not argue otherwise: that rules out a disabled app
        hiding behind an unclaimed name, and says nothing about whether any source
        still declares it.

        This input is where the mount/grant split earns its keep: both lists are
        asserted on it, so a reader that treats them alike fails whichever half it
        gets wrong. Needs the resolver override because the stub echoes every
        command, which makes this branch unreachable by default.
        """
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@notinstalled", "@notinstalled/search"],
            "allowedTools": ["@notinstalled"],
            "mcpServers": {"notinstalled": {"command": "kirocrew-not-installed-yet"}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        exempted: list[set[str]] = []
        real_prune = agent_mod.prune_dangling_tool_refs

        def _spy(cfg, *, declared=(), declared_grants=None):
            exempted.append(set(declared))
            return real_prune(cfg, declared=declared, declared_grants=declared_grants)

        monkeypatch.setattr(agent_mod, "prune_dangling_tool_refs", _spy)

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # The entry really was withheld, so the refs below are the dangling shape
        # the reconcile sees rather than a case it never reached.
        assert "notinstalled" not in config["mcpServers"]
        assert exempted and "notinstalled" in exempted[-1]
        # The mount survives the gap, on both the bare ref and the per-tool one.
        assert "@notinstalled" in config["tools"]
        assert "@notinstalled/search" in config["tools"]
        # The grant does not. Asserted on the same input as the mounts above, so
        # the pass is measured as the two lists DIVERGING rather than as either a
        # blanket keep or a blanket removal.
        assert "@notinstalled" not in config["allowedTools"]

    def test_an_unresolved_slashed_descendant_is_never_absorbed_by_its_ancestor(
        self, tmp_path: Path
    ):
        """A transiently absent descendant is never rewritten as an ancestor tool.

        Reserved ownership freezes the descendant's refs while the present
        ancestor moves to its alias. The final reconcile drops the unchanged
        dangling refs.
        """
        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@a/b", "@a/b/tool", "@a/b/c", "@a/b/c/tool"],
            "allowedTools": ["@a/b", "@a/b/tool", "@a/b/c", "@a/b/c/tool"],
            "mcpServers": {
                "a/b": {"command": sys.executable},
                "a/b/c": {"command": "kirocrew-not-installed-yet"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps({"mcpServers": existing["mcpServers"]})
        )

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        assert "a-b" in config["mcpServers"]
        assert "a-b-c" not in config["mcpServers"]
        for key in ("tools", "allowedTools"):
            assert "@a-b" in config[key]
            assert "@a-b/tool" in config[key]
            assert "@a-b/c" not in config[key]
            assert "@a-b/c/tool" not in config[key]
            assert "@a/b/c" not in config[key]

    def test_overlapping_alias_output_is_not_rewritten_by_another_original_key(self):
        """Each ref moves once from its longest original server owner."""
        from kiro_crew import agent as agent_mod

        config = {
            "tools": ["@npm:@scope/pkg/x/run", "@scope-pkg/x/y"],
            "allowedTools": ["@npm:@scope/pkg/x/run", "@scope-pkg/x/y"],
            "mcpServers": {
                "npm:@scope/pkg": {"command": "npm-owner"},
                "scope-pkg/x": {"command": "nested-owner"},
            },
        }

        agent_mod._normalize_mcp_server_keys(config)

        assert config["mcpServers"] == {
            "scope-pkg": {"command": "npm-owner"},
            "scope-pkg-x": {"command": "nested-owner"},
        }
        for key in ("tools", "allowedTools"):
            assert config[key] == ["@scope-pkg/x/run", "@scope-pkg-x/y"]
            assert "@scope-pkg-x/run" not in config[key]

    def test_frozen_allowed_ref_is_dropped_only_when_it_collides_with_a_live_server(self):
        """A frozen grant cannot become a live normalized server's per-tool grant."""
        from kiro_crew import agent as agent_mod

        config = {
            "tools": ["@a/b", "@a-b/c", "@x/y"],
            "allowedTools": ["@a/b", "@a-b/c", "@x/y"],
            "mcpServers": {"a/b": {"command": sys.executable}},
        }

        agent_mod._normalize_mcp_server_keys(
            config,
            reserved_keys={"a-b/c", "x/y"},
        )

        assert config["mcpServers"] == {"a-b": {"command": sys.executable}}
        assert "@a-b/c" in config["tools"]
        assert "@a-b/c" not in config["allowedTools"]
        assert "@x/y" in config["allowedTools"]

    def test_a_rebuild_keeps_the_reserved_builtin_namespace_in_both_lists(self, tmp_path: Path):
        """`@builtin` is a kiro namespace, so a whole rebuild leaves it alone.

        It never appears in `mcpServers`, so a reconcile reading it as a server
        ref would strip it at the funnel every write goes through and take the
        built-in tool surface and the `tool_search` loader with it -- on every
        rebuild, with an existing config that never re-adds the entry.
        """
        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@builtin", "@gone"],
            "allowedTools": ["@builtin", "@gone"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        config = json.loads(_run_install(tmp_path, cfg_dir).read_text(encoding="utf-8"))

        assert "@builtin" in config["tools"]
        assert "@builtin" in config["allowedTools"]
        # Measured beside a real removal, so a pass that keeps EVERYTHING cannot
        # satisfy this test.
        assert "@gone" not in config["tools"]
        assert "@gone" not in config["allowedTools"]

    def test_an_unresolved_app_server_loses_its_grant_once_its_app_is_gone(
        self, tmp_path: Path, monkeypatch
    ):
        """The unresolved exemption's GRANT half does not outlive the app that owns the name.

        A server can fail to resolve on this pass AND have its app deregistered
        concurrently. Keeping the grant exemption then leaves an `allowedTools`
        entry on a name whose owner is gone, and that list never reaches the
        PreToolUse gate, so whatever is bound to the name next inherits the
        approval. The mount half survives by design: it costs one mount attempt
        against an empty name, where a dropped `tools` ref is unrecoverable.
        """
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@deadapp:srv", "@liveapp:srv"],
            "allowedTools": ["@deadapp:srv", "@liveapp:srv"],
            "mcpServers": {
                "deadapp:srv": {"command": "kirocrew-not-installed-yet"},
                "liveapp:srv": {"command": "kirocrew-not-installed-yet"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        monkeypatch.setattr(
            agent_mod,
            "_app_owned_mcp_keys",
            lambda: {"deadapp:srv": False, "liveapp:srv": True},
        )

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # Neither mounted, so the difference is the exemption and nothing else.
        assert "deadapp:srv" not in config["mcpServers"]
        assert "liveapp:srv" not in config["mcpServers"]
        # The GRANT does not outlive the app: gone is not consent. The MOUNT
        # does, deliberately -- dropping a `tools` ref is unrecoverable for a
        # name nothing re-adds, while keeping it costs one mount attempt
        # against an empty name.
        assert "@deadapp:srv" in config["tools"]
        assert "@deadapp:srv" not in config["allowedTools"]
        assert "@liveapp:srv" in config["tools"]
        assert "@liveapp:srv" in config["allowedTools"]

    def test_a_colon_keyed_server_no_app_owns_keeps_its_mount(self, tmp_path: Path, monkeypatch):
        """Ownership comes from the app's manifest, so a colliding prefix is not ownership.

        `mcp_server_alias` returns a slash-free name unchanged, so a global
        mcp.json server keyed `npm:foo` reaches the agent config with its colon
        intact. An installed app may also be called `npm` and declare only `bar`
        -- here switched ON, which is the direction that makes attribution
        observable: a prefix-attributing reader hands `npm:foo` the enabled
        app's answer and KEEPS its grant, leaving an auto-approval on a name
        the app never declared, on the one list that never reaches the
        PreToolUse gate. The correct reader sees `npm:foo` as unclaimed and
        unvouched, so its grant goes while its mount stays. `npm:bar` IS the
        app's own claim on the same input: enabled owner, so it keeps both.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@npm:foo", "@npm:foo/run", "@npm:bar"],
            "allowedTools": ["@npm:foo", "@npm:bar"],
            "mcpServers": {
                "npm:foo": {"command": "kirocrew-not-installed-yet"},
                "npm:bar": {"command": "kirocrew-not-installed-yet"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        class _Manifest:
            mcpServers = {"bar": {"command": "kirocrew-not-installed-yet"}}

        # An app named `npm`, installed and switched ON, declaring only `bar`.
        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "npm"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: True)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: _Manifest())

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # Neither mounted, so the difference is ownership and nothing else.
        assert "npm:foo" not in config["mcpServers"]
        assert "npm:bar" not in config["mcpServers"]
        assert "@npm:foo" in config["tools"]
        assert "@npm:foo/run" in config["tools"]
        # THE discriminating assert: a prefix-attributing reader hands `npm:foo`
        # the enabled app's answer and keeps this grant.
        assert "@npm:foo" not in config["allowedTools"]
        # The app's own exact claim, owner enabled: keeps both.
        assert "@npm:bar" in config["tools"]
        assert "@npm:bar" in config["allowedTools"]

    def test_a_slashed_manifest_name_is_matched_by_its_alias(self, tmp_path: Path, monkeypatch):
        """Ownership is keyed by the alias, because that is what the ref is spelled with.

        A manifest may declare a slashed server -- the npm-scoped
        `@playwright/mcp`, or the registry `namespace/name` form -- and
        `mcp_server_alias` maps `npm:@playwright/mcp` to `playwright-mcp`, which
        is the key a prior rebuild wrote and the candidate this pass sees. Keying
        ownership by the raw composite matches no candidate at all, so the owner
        of exactly the names that NEED aliasing reads as unowned and its grant
        survives its app being switched off.

        The unowned `solo` measures the other direction on the same input: a
        reader that claims every candidate fails here.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@playwright-mcp", "@playwright-mcp/run", "@solo"],
            "allowedTools": ["@playwright-mcp", "@solo"],
            "mcpServers": {
                "playwright-mcp": {"command": "kirocrew-not-installed-yet"},
                "solo": {"command": "kirocrew-not-installed-yet"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        class _Manifest:
            mcpServers = {"@playwright/mcp": {"command": "kirocrew-not-installed-yet"}}

        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "npm"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: False)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: _Manifest())

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # Owned by an app that is switched off, so the grant goes -- that is
        # the alias-keyed match this test pins: keyed by the raw composite it
        # would match no candidate and the grant would survive. The mount
        # stays, in both spellings, because dropping a `tools` ref is
        # unrecoverable while keeping one costs a mount attempt against an
        # empty name.
        assert "@playwright-mcp" not in config["allowedTools"]
        assert "@playwright-mcp" in config["tools"]
        assert "@playwright-mcp/run" in config["tools"]
        # No app declares `solo`: its mount stays, and its grant goes because
        # nothing declares the name while its command is unresolved.
        assert "@solo" in config["tools"]
        assert "@solo" not in config["allowedTools"]

    def test_an_unreadable_app_claim_does_not_read_as_unowned(self, tmp_path: Path, monkeypatch):
        """A claim that could not be read is not a claim of nothing.

        `get_app_manifest` returns None for an absent file, a parse failure and a
        permission error alike, so an app whose manifest stops being readable
        claims no names. Its server is still in the rendered config, because
        `_load_existing_config` carries the entry forward, so the permissive
        "nobody owns this" branch would leave an auto-approval on a name whose
        owner is switched off and cannot re-add the entry.

        `vouched` is the other half on the same input: it is declared by a file
        this rebuild READ, so no app read coming back blind can make it app-owned
        and its exemption must not narrow. That half is what keeps a gated-off
        server -- whose ref an existing config never re-adds -- mounted.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@ghost:srv", "@ghost:srv/run", "@vouched"],
            "allowedTools": ["@ghost:srv", "@vouched"],
            "mcpServers": {
                "ghost:srv": {"command": "kirocrew-not-installed-yet"},
                "vouched": {"command": "kirocrew-not-installed-yet"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps({"mcpServers": {"vouched": {"command": "kirocrew-not-installed-yet"}}})
        )

        # Installed, switched OFF, and its manifest unreadable.
        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "ghost"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: False)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: None)

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        assert "@ghost:srv" not in config["allowedTools"]
        # The MOUNT stays. An unread claim is doubt, and this test's subject is what
        # that doubt costs: on the grant above it could leave a switched-off app's
        # auto-approval sitting on the name, which is why that assertion is the
        # discriminating one. On `tools` the same doubt would permanently unmount a
        # server whose binary is merely off PATH, so it resolves toward keeping.
        assert "@ghost:srv" in config["tools"]
        # Vouched by a file this rebuild read, so an unreadable app claim elsewhere
        # does not cost it its refs.
        assert "@vouched" in config["allowedTools"]
        assert "@vouched" in config["tools"]

    def test_one_apps_read_failure_does_not_unmount_an_unrelated_server(
        self, tmp_path: Path, monkeypatch
    ):
        """An app read failure is doubt about THAT app, never a licence to unmount others.

        A user's own stdio server, added straight to the agent config, claimed by
        no app and declared by no source scope, with its binary off PATH this
        pass. An unrelated installed app's manifest fails to read, which
        `_app_owned_mcp_keys` calls an ordinary shape of failure rather than an
        exotic one. Nothing about that failure concerns this server, and a `tools`
        ref is never re-added by a later pass, so pruning it would silently and
        permanently stop offering the server's tools once the binary returned.

        `nosuch` is the other half on the same input: its server is absent from
        the config entirely rather than merely unresolved, so no pass can ever
        mount it and a reader that keeps everything fails here.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@mine", "@mine/run", "@nosuch", "@nosuch/run"],
            "allowedTools": ["@mine", "@mine/run", "@nosuch"],
            "mcpServers": {"mine": {"command": "kirocrew-not-installed-yet"}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        # An unrelated app, installed and enabled, whose manifest will not read.
        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "unrelated"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: True)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: None)

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # Withheld from the map, so these refs are the dangling shape the reconcile
        # sees rather than a case it never reached.
        assert "mine" not in config["mcpServers"]
        # The mount survives the unrelated failure, on the bare ref and the per-tool
        # one, because dropping either is permanent.
        assert "@mine" in config["tools"]
        assert "@mine/run" in config["tools"]
        # The grant does not: while the command is unresolved no source declares the
        # name, so the approval would sit on it for whatever binds there next.
        assert not [r for r in config["allowedTools"] if str(r).startswith("@mine")]
        # Never declared at all, so it is a real leftover and goes from both lists.
        assert not [r for r in config["tools"] if str(r).startswith("@nosuch")]
        assert not [r for r in config["allowedTools"] if str(r).startswith("@nosuch")]

    def test_a_family_guess_narrows_the_grant_and_never_the_mount(
        self, tmp_path: Path, monkeypatch
    ):
        """A family match may narrow the GRANT, never the MOUNT.

        Pinned by mutation: conditioning the mount exemption on an ownership
        answer reds the `in config["tools"]`
        asserts below; exempting grants unconditionally (a blanket keep) reds
        the `not in config["allowedTools"]` ones.

        `mcp_server_alias` is many-to-one, and `_normalize_mcp_server_keys` hands
        the loser of a collision the lowest free `base-<n>` rather than dropping a
        distinct server. Which claimant a suffix came from is not recoverable (it
        is assigned against the live map, and the slashed key it came from is gone
        by the next rebuild), so family membership is a GUESS: `playwright-mcp-2`
        may be the switched-off app's collision half, or a user's own server that
        merely failed to resolve this pass. Dropping its `tools` ref on that guess
        deletes it with nothing to re-add it, so the mount survives; the grant
        needs positive evidence the guess cannot supply, so it goes -- the same
        mount-survives-doubt / grant-needs-evidence asymmetry the reconcile's
        unresolved and gated handling already runs on.

        `playwright-mcpx` is the precision half on the same input: it shares the
        prefix but is not a numeric-suffixed sibling, so no app claims it and its
        grant goes under either reader while its mount stays.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@playwright-mcp", "@playwright-mcp-2", "@playwright-mcpx"],
            "allowedTools": ["@playwright-mcp", "@playwright-mcp-2", "@playwright-mcpx"],
            "mcpServers": {
                "playwright-mcp": {"command": "kirocrew-not-installed-yet"},
                "playwright-mcp-2": {"command": "kirocrew-not-installed-yet"},
                "playwright-mcpx": {"command": "kirocrew-not-installed-yet"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        class _Manifest:
            mcpServers = {"@playwright/mcp": {"command": "kirocrew-not-installed-yet"}}

        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "npm"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: False)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: _Manifest())

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # The suffixed sibling: the mount rides on the family guess, the grant
        # never does. Asserted on both lists so the pass reads as the two lists
        # diverging, not as a blanket keep or removal.
        assert "@playwright-mcp-2" in config["tools"]
        assert "@playwright-mcp-2" not in config["allowedTools"]
        # The claimed base itself: its owner is positively switched off, so the
        # grant goes; the mount survives, because re-enabling the app re-merges
        # its entry and the kept ref resumes mounting it.
        assert "@playwright-mcp" in config["tools"]
        assert "@playwright-mcp" not in config["allowedTools"]
        # Shares the prefix, is not a sibling: no app claims it, so it keeps its
        # mount, and its grant goes while its command is unresolved because
        # nothing declares the name.
        assert "@playwright-mcpx" in config["tools"]
        assert "@playwright-mcpx" not in config["allowedTools"]

    def test_an_enabled_base_does_not_lend_its_grant_to_a_suffixed_sibling(
        self, tmp_path: Path, monkeypatch
    ):
        """Family membership is a guess, so it carries the mount and never the grant.

        An app owns `playwright-mcp` and is switched ON. A `playwright-mcp-2` also
        sits in the config with its command unresolved this pass. The suffix rule
        matches the two, but nothing recoverable says the sibling came from that
        app's collision rather than being a name its own author chose: the suffix
        is minted against the live map and the slashed key it came from is gone by
        the next rebuild.

        Lending the enabled base's answer to that guess keeps an auto-approval on a
        server the app never declared, and `allowedTools` never reaches the
        PreToolUse gate, so whatever binds to the name next inherits it. The mount
        rides on the same guess because being wrong there costs one mount attempt
        against an empty name, while dropping it cannot be undone.

        The base itself is the other half on the same input: it IS a claimed name,
        its app is on, so it keeps both. A reader that required exactness for the
        mount too would fail on the sibling, and one that accepted the family for
        the grant would fail on it as well.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@playwright-mcp", "@playwright-mcp-2"],
            "allowedTools": ["@playwright-mcp", "@playwright-mcp-2"],
            "mcpServers": {
                "playwright-mcp": {"command": "kirocrew-not-installed-yet"},
                "playwright-mcp-2": {"command": "kirocrew-not-installed-yet"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        class _Manifest:
            mcpServers = {"@playwright/mcp": {"command": "kirocrew-not-installed-yet"}}

        # Installed and switched ON, declaring the base that `playwright-mcp-2`
        # only resembles.
        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "npm"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: True)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: _Manifest())

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # The claimed name itself, owner switched on: both refs stay.
        assert "@playwright-mcp" in config["tools"]
        assert "@playwright-mcp" in config["allowedTools"]
        # The sibling: mount on the guess, grant not.
        assert "@playwright-mcp-2" in config["tools"]
        assert "@playwright-mcp-2" not in config["allowedTools"]

    def test_an_exactly_owned_alias_is_decided_by_its_own_owner_not_a_sibling(
        self, tmp_path: Path, monkeypatch
    ):
        """A claimed name answers to its own app, even when it looks like a sibling.

        Two apps: one switched ON declaring `x-y-2` literally, one switched OFF
        declaring `@x/y`, which aliases to `x-y`. The suffix rule matches `x-y-2` to
        `x-y`, so a reader that requires every family member to pass lets the
        switched-off app delete a server whose own app is running, on both lists at
        once.

        Family matching exists for a name nothing claims exactly, where the suffix
        is the only evidence available. Here there IS an exact claim, so it decides
        and the family is irrelevant.

        `x-y` is the other half on the same input: its own app is off, so it loses
        both refs, which is what the family rule was added for.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@x-y", "@x-y-2", "@x-y-2/run"],
            "allowedTools": ["@x-y", "@x-y-2"],
            "mcpServers": {
                "x-y": {"command": "kirocrew-not-installed-yet"},
                "x-y-2": {"command": "kirocrew-not-installed-yet"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        class _OnManifest:
            mcpServers = {"@x/y-2": {"command": "kirocrew-not-installed-yet"}}

        class _OffManifest:
            mcpServers = {"@x/y": {"command": "kirocrew-not-installed-yet"}}

        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "on"}, {"name": "off"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: name == "on")
        monkeypatch.setattr(
            apps_manager,
            "get_app_manifest",
            lambda name: _OnManifest() if name == "on" else _OffManifest(),
        )

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # Its own app is on, so both spellings and both lists survive the sibling.
        assert "@x-y-2" in config["tools"]
        assert "@x-y-2/run" in config["tools"]
        assert "@x-y-2" in config["allowedTools"]
        # The sibling's own app is off, so it goes: the family rule still bites.
        assert "@x-y" not in config["allowedTools"]

    def test_a_base_two_apps_claim_is_exempt_only_while_both_are_on(
        self, tmp_path: Path, monkeypatch
    ):
        """One alias, two claimants: the family is switched on only while every claimant is.

        Two apps can declare names that collide under `mcp_server_alias`, and the
        suffix `_normalize_mcp_server_keys` assigns is computed against the live
        server map, so which claimant a given sibling came from is not recoverable
        from the manifests. A last-writer-wins map hands the whole family the
        enablement of whichever app was read last, which grants on the strength of
        an unrelated app being switched on.

        `beta:solo` is the positive half on the same input: its only claimant is
        enabled, so a reader that denies the whole map fails here.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@x-y", "@beta:solo"],
            "allowedTools": ["@x-y", "@beta:solo"],
            "mcpServers": {
                "x-y": {"command": "kirocrew-not-installed-yet"},
                "beta:solo": {"command": "kirocrew-not-installed-yet"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        class _Alpha:
            mcpServers = {"x/y": {"command": "kirocrew-not-installed-yet"}}

        class _Beta:
            mcpServers = {
                "x/y": {"command": "kirocrew-not-installed-yet"},
                "solo": {"command": "kirocrew-not-installed-yet"},
            }

        # Both apps alias `x/y` to the base `x-y`. `alpha` is OFF, `beta` is ON,
        # and `beta` is read LAST so a last-writer map would answer "on".
        monkeypatch.setattr(
            apps_manager, "list_apps", lambda: [{"name": "alpha"}, {"name": "beta"}]
        )
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: name == "beta")
        monkeypatch.setattr(
            apps_manager, "get_app_manifest", lambda name: _Alpha() if name == "alpha" else _Beta()
        )

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # A switched-off app claims this base too, so the grant does not linger.
        # The mount survives -- the reconcile never unmounts on an ownership
        # answer, because a kept ref costs one mount attempt against an empty
        # name while a dropped one can be unrecoverable.
        assert "@x-y" not in config["allowedTools"]
        assert "@x-y" in config["tools"]
        # Claimed only by the enabled app, so its resolution miss keeps its refs.
        assert "@beta:solo" in config["allowedTools"]

    def test_a_vouched_suffixed_name_is_not_taken_for_an_app_sibling(
        self, tmp_path: Path, monkeypatch
    ):
        """A source that still declares a `base-<n>` name outranks family attribution.

        `_normalize_mcp_server_keys` assigns a collision suffix against the live
        server map, and the slashed key it derived from is gone from the rendered
        config afterwards, so nothing at reconcile time can tell a minted
        `base-<n>` from a server a source named that way on its own. Family
        attribution therefore has to yield to positive evidence: a name the user's
        own `mcp.json` still declares is not a switched-off app's sibling, because
        nothing re-declares a disabled app's servers.

        `playwright-mcp` is the other half on the same input: the app does own it,
        so a reader that exempts everything fails here.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@playwright-mcp", "@playwright-mcp-2", "@playwright-mcp-2/run"],
            "allowedTools": ["@playwright-mcp", "@playwright-mcp-2"],
            "mcpServers": {
                "playwright-mcp": {"command": "kirocrew-not-installed-yet"},
                "playwright-mcp-2": {"command": "kirocrew-not-installed-yet"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        # The user's own source still declares the suffixed name.
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps(
                {"mcpServers": {"playwright-mcp-2": {"command": "kirocrew-not-installed-yet"}}}
            )
        )

        class _Manifest:
            mcpServers = {"@playwright/mcp": {"command": "kirocrew-not-installed-yet"}}

        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "npm"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: False)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: _Manifest())

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # Declared by a source that read fine, so it is not the app's sibling.
        assert "@playwright-mcp-2" in config["allowedTools"]
        assert "@playwright-mcp-2/run" in config["tools"]
        # The base really is the switched-off app's, so its grant still goes.
        assert "@playwright-mcp" not in config["allowedTools"]

    def test_an_app_record_list_apps_skipped_is_not_read_as_absent(
        self, tmp_path: Path, monkeypatch
    ):
        """`list_apps` drops an unreadable installed record silently, which is not "no such app".

        The skip does not raise, so the returned list alone looks complete and the
        omitted app's carried-forward server reaches the permissive branch --
        leaving an `allowedTools` grant on a name whose owner cannot re-add the
        entry. A directory still holding the record file `list_apps` reads, under a
        name the list does not carry, is the signal that a claim went missing.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@skipped:srv", "@skipped:srv/run"],
            "allowedTools": ["@skipped:srv"],
            "mcpServers": {"skipped:srv": {"command": "kirocrew-not-installed-yet"}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        # On disk with its record file present, but absent from what list_apps
        # returned: exactly the shape a failed per-app record read leaves.
        fake_apps = tmp_path / "fake_apps"
        (fake_apps / "skipped").mkdir(parents=True)
        (fake_apps / "skipped" / apps_manager.INSTALLED_META_FILENAME).write_text("{corrupt")

        monkeypatch.setattr(apps_manager, "apps_dir", lambda: fake_apps)
        monkeypatch.setattr(apps_manager, "list_apps", lambda: [])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: False)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: None)

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        assert "@skipped:srv" not in config["allowedTools"]
        # The MOUNT stays: a silently skipped record is doubt, and the grant
        # assertion above is what this test discriminates on. Dropping a `tools` ref
        # on the same doubt would be permanent.
        assert "@skipped:srv" in config["tools"]
        # A targeted removal, not a sweep.
        assert "fs_read" in config["tools"]

    def test_a_server_no_read_claims_keeps_its_mount_but_not_its_grant(
        self, tmp_path: Path, monkeypatch
    ):
        """An app server neither ownership read claims loses its auto-approval.

        The carried-over config still declares it and its command does not
        resolve, so nothing vouches for the grant. The mount stays: dropping a
        `tools` ref is not recoverable for a name nothing re-adds.
        """
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@ghosted:srv", "@ghosted:srv/run"],
            "allowedTools": ["@ghosted:srv"],
            "mcpServers": {"ghosted:srv": {"command": "kirocrew-not-installed-yet"}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        reads = iter([{}, {}])
        monkeypatch.setattr(agent_mod, "_app_owned_mcp_keys", lambda: next(reads, {}))

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        assert "@ghosted:srv" not in config["allowedTools"]
        # The MOUNT stays; only the grant goes.
        assert "@ghosted:srv" in config["tools"]
        assert "fs_read" in config["tools"]

    def test_an_unreadable_enablement_claims_nothing_rather_than_disabled(self, monkeypatch):
        """None from `app_enabled_state` is "could not read", never "switched off".

        `is_app_enabled` returns a single False for both, which is why
        `app_enabled_state` exists. Recording that False here would state a
        definite disablement the reader never established, and a definite
        disablement revokes an `allowedTools` grant that nothing re-derives. The
        claim has to count as unread instead, so the reconcile falls back on the
        answer it had before the rebuild.
        """
        from kiro_crew import agent as agent_mod
        from kiro_crew.apps import manager as apps_manager

        class _Manifest:
            mcpServers = {"srv": {"command": "x"}}

        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "murky"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: None)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: _Manifest())
        monkeypatch.setattr(apps_manager, "apps_dir", lambda: Path("/nonexistent-apps-root"))

        # No claim is recorded.
        assert agent_mod._app_owned_mcp_keys() == {}

    def test_a_closed_gate_does_not_vouch_for_a_disabled_app_claim(
        self, tmp_path: Path, monkeypatch
    ):
        """Being withheld right now is not the same as anything still declaring the name.

        A source declaring a name outranks a switched-off app's exact claim, because
        pruning would widen the grant on recovery. The managed GATE must not carry
        that privilege: `gated_off` reports only that a shipped server is withheld
        at this moment. If an app's alias lands exactly on a gated managed name and
        the gate vouched for it, the app's auto-approval would sit on the name until
        the gate reopened and then belong to the managed server.

        `@gated-alone` is the other half on the same input: gated and claimed by no
        app, so its ref is preserved -- which is what the gate exemption is for.
        """
        from kiro_crew import agent as agent_mod
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@gated-claimed", "@gated-alone"],
            "allowedTools": ["@gated-claimed", "@gated-alone"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        monkeypatch.setattr(
            agent_mod, "_gated_off_servers", lambda: frozenset({"gated-claimed", "gated-alone"})
        )

        class _Manifest:
            mcpServers = {"gated/claimed": {"command": "kirocrew-not-installed-yet"}}

        # A switched-off app whose alias for `gated/claimed` is exactly `gated-claimed`.
        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "gated"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: False)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: _Manifest())

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # The gate did not rescue the switched-off app's grant.
        assert "@gated-claimed" not in config["allowedTools"]
        # Gated and unclaimed, so the gate exemption still does its job.
        assert "@gated-alone" in config["allowedTools"]
        assert "@gated-alone" in config["tools"]

    def test_a_gated_claim_that_vanishes_mid_rebuild_still_loses_its_grant(
        self, tmp_path: Path, monkeypatch
    ):
        """The pre-rebuild ownership snapshot is what closes the gate's vouching hole.

        An app claiming exactly a gated managed name can be uninstalled between
        the two ownership reads. A reconcile reading only the FINAL read sees the
        name as unclaimed, and the gate vouches for an unclaimed gated name -- so
        the vanished app's auto-approval would sit on the managed name until the
        gate reopened and then belong to the managed server. The union with the
        start snapshot keeps the name exactly-claimed, and a claimant that
        answers nothing is not consent, so the grant goes. The mount stays, as
        everywhere.
        """
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@gated-claimed"],
            "allowedTools": ["@gated-claimed"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        monkeypatch.setattr(agent_mod, "_gated_off_servers", lambda: frozenset({"gated-claimed"}))
        # Claimed (and enabled) at the start read, gone by the final one: the
        # uninstall lands between them. Both reads saw every claim.
        reads = iter([{"gated-claimed": True}, {}])
        monkeypatch.setattr(agent_mod, "_app_owned_mcp_keys", lambda: next(reads, {}))

        config = json.loads(_run_install(tmp_path, cfg_dir).read_text(encoding="utf-8"))

        assert "@gated-claimed" not in config["allowedTools"]
        assert "@gated-claimed" in config["tools"]

    def test_a_source_that_still_declares_a_name_outranks_a_disabled_app_claim(
        self, tmp_path: Path, monkeypatch
    ):
        """Pruning a source-declared name does not narrow its grant, it later widens it.

        The tempting rule is that an EXACT app claim is unambiguous, so a
        switched-off owner should decide even when another source declares the same
        name. That is wrong because of what happens next: the shared sync re-adds
        the BARE `@alias` to `allowedTools` once the command resolves again, so a
        user who had only `@alias/one-tool` gets a whole-server grant back. Pruning
        here therefore WIDENS the approval it was meant to protect.

        So a readable non-app source declaring the name wins. The managed gate is
        deliberately not such a source -- see the gated case below -- because being
        withheld right now is not the same as anyone still declaring the name.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@shared-name", "@shared-name/one-tool", "@orphan-name"],
            "allowedTools": ["@shared-name/one-tool", "@orphan-name"],
            "mcpServers": {
                "shared-name": {"command": "kirocrew-not-installed-yet"},
                "orphan-name": {"command": "kirocrew-not-installed-yet"},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        # The user's own source still declares one of the two names.
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps({"mcpServers": {"shared-name": {"command": "kirocrew-not-installed-yet"}}})
        )

        class _Manifest:
            mcpServers = {
                "shared/name": {"command": "kirocrew-not-installed-yet"},
                "orphan/name": {"command": "kirocrew-not-installed-yet"},
            }

        # A switched-off app claims BOTH names exactly, via aliases of `shared/name`
        # and `orphan/name`. Only one of them is still declared by a source.
        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "shared"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: False)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: _Manifest())

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # Declared by a readable source, so its narrow grant is kept rather than
        # pruned and later restored as a broad one.
        assert "@shared-name/one-tool" in config["allowedTools"]
        # Claimed by the same switched-off app but declared by nobody, so its
        # grant goes; the mount stays, because the reconcile never unmounts on
        # an ownership answer.
        assert "@orphan-name" not in config["allowedTools"]
        assert "@orphan-name" in config["tools"]

    def test_an_unreadable_manifest_narrows_even_for_an_enabled_app(
        self, tmp_path: Path, monkeypatch
    ):
        """An unread claim narrows the exemption whether or not its app is switched on.

        The tempting refinement is to let an ENABLED app's unreadability pass,
        since its own keys would be exempt under either answer. That is wrong for
        the name NO app claims: the reading failure can be at the start while the
        app is uninstalled by the end, and then the base was never recorded, the
        owner is gone, and the name looks unowned though it never was. So both
        reads must have seen every claim before an unclaimed, unvouched name is
        exempted.
        """
        from kiro_crew.apps import manager as apps_manager

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@orphan:srv"],
            "allowedTools": ["@orphan:srv"],
            "mcpServers": {"orphan:srv": {"command": "kirocrew-not-installed-yet"}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        monkeypatch.setattr(apps_manager, "list_apps", lambda: [{"name": "ghost"}])
        monkeypatch.setattr(apps_manager, "app_enabled_state", lambda name: True)
        monkeypatch.setattr(apps_manager, "get_app_manifest", lambda name: None)

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # No readable source declares this name and the ownership read was not
        # whole, so it is not exempted on the strength of an absence.
        assert "@orphan:srv" not in config["allowedTools"]
        # The MOUNT stays. An enabled app's unreadable manifest narrows the grant,
        # which is this test's subject; it must not permanently unmount a server.
        assert "@orphan:srv" in config["tools"]
        assert "fs_read" in config["tools"]
        assert "@npm:bar" not in config["allowedTools"]

    def test_an_app_uninstalled_during_the_rebuild_does_not_keep_its_grant(
        self, tmp_path: Path, monkeypatch
    ):
        """An app that vanishes mid-rebuild answers nothing, and nothing is not consent.

        Uninstalling takes the app out of the listing AND its manifest, so the
        ownership read at the end cannot tell "this app is gone" from "no app ever
        owned this name" -- and the second is the permissive answer. The snapshot
        taken before the rebuild's work is what keeps the two apart, so the grant
        does not sit on the name waiting for the next server bound to it.
        """
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@doomed:srv", "@doomed:srv/run"],
            "allowedTools": ["@doomed:srv"],
            "mcpServers": {"doomed:srv": {"command": "kirocrew-not-installed-yet"}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        # Owned and enabled on the first read, gone by the second: the uninstall
        # lands between them. Both reads saw every claim, so the absence at the end
        # is a confirmed removal rather than an unread claim.
        reads = iter([{"doomed:srv": True}, {}])
        monkeypatch.setattr(agent_mod, "_app_owned_mcp_keys", lambda: next(reads, {}))

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        assert "doomed:srv" not in config["mcpServers"]
        # The GRANT does not sit on the name waiting for the next server bound
        # to it. The MOUNT stays, in both spellings: the reconcile never
        # unmounts on an ownership answer, and a kept ref to an empty name
        # mounts nothing.
        assert "@doomed:srv" not in config["allowedTools"]
        assert "@doomed:srv" in config["tools"]
        assert "@doomed:srv/run" in config["tools"]
        # The unrelated entries are untouched, so this is a targeted removal.
        assert "fs_read" in config["tools"]

    def test_a_transiently_unread_claim_keeps_the_mount_and_drops_the_grant(
        self, tmp_path: Path, monkeypatch
    ):
        """Doubt about ownership is resolved differently for a mount than for a grant.

        This is the pair of the uninstall test above, differing in one field. There
        the final read was whole, so an alias absent from it really was gone. Here
        the final read could not see some claim -- `get_app_manifest` returning None
        is the ordinary shape of that failure -- so the same absence carries no
        information, and the two lists take opposite answers to that doubt.

        The `tools` ref survives, because dropping a mount on a doubt can unmount a
        server for good: an existing config never re-adds a template ref. The
        `allowedTools` grant does NOT, because keeping it on the same doubt leaves
        an auto-approval sitting on a name, on the one list that never reaches the
        PreToolUse gate, and a global "some app was unreadable" is not evidence
        about THIS app. An approval a human can grant again is the cheaper loss.

        The app is enabled throughout. Only the reading of it failed.
        """
        from kiro_crew import agent as agent_mod

        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "tools": ["fs_read", "@live:srv", "@live:srv/run"],
            "allowedTools": ["@live:srv"],
            "mcpServers": {"live:srv": {"command": "kirocrew-not-installed-yet"}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        # Owned and enabled at the start; the second read claims nothing.
        reads = iter([{"live:srv": True}, {}])
        monkeypatch.setattr(agent_mod, "_app_owned_mcp_keys", lambda: next(reads, {}))

        config = json.loads(
            _run_install(
                tmp_path,
                cfg_dir,
                which=lambda c, **kw: None if c == "kirocrew-not-installed-yet" else c,
            ).read_text(encoding="utf-8")
        )

        # The mount survives the doubt, so the server is not unmounted for good.
        assert "@live:srv" in config["tools"]
        assert "@live:srv/run" in config["tools"]
        # The grant does not: an auto-approval is not kept on an unread claim.
        assert "@live:srv" not in config["allowedTools"]
