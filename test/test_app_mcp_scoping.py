"""App MCP servers land in KiroCrew's agent config, never the shared kiro file.

The shared ``~/.kiro/settings/mcp.json`` is read by everything else under
``~/.kiro`` — Kiro IDE and any other kiro-cli agent — so registering an app's
MCP servers there leaked private app tools into surfaces that never installed
the app. These tests pin the fix: registration targets the agent config, the
shared file is left alone, and a ``clean`` rebuild re-derives app entries from
the enabled apps' manifests (the shared file can no longer supply them).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module-level ~/.kiro paths at a tmp home.

    The paths are module constants resolved at import time, so they are patched
    directly rather than via ``HOME`` (which they no longer consult).
    """
    from kiro_crew.apps import bridges

    agents = tmp_path / ".kiro" / "agents"
    settings = tmp_path / ".kiro" / "settings"
    agents.mkdir(parents=True)
    settings.mkdir(parents=True)

    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: agents / "kirocrew.json")
    monkeypatch.setattr(bridges, "_LEGACY_SHARED_MCP_PATH", settings / "mcp.json")
    return tmp_path


def _manifest(servers: dict[str, Any], entry_point: str = "") -> Any:
    """A stand-in manifest exposing the mcpServers + backend attributes.

    ``entry_point`` mirrors ``backend.entryPoint``: non-empty means the gateway
    launches the backend (its HTTP port is auto-resolved), empty means the app
    is self-managed (its manifest URL is authoritative).
    """

    class _Backend:
        entryPoint = entry_point  # noqa: N815 — mirrors the manifest field name

    class _M:
        mcpServers = servers  # noqa: N815 — mirrors the manifest field name
        backend = _Backend()

    return _M()


class TestGrantVersusGovernance:
    """A grant says "the user allows this"; the ceiling says "it may run".

    Auto-approve (``allowedTools``) is the one path kiro-cli never asks about, so
    it never reaches ``hooks.on_tool_call`` where the governance deny runs. A
    ceiling-denied server must therefore stay OUT of the auto-approve list — it
    remains granted (in ``tools``), which forces every call through
    ``session/request_permission``, where the gate denies it.
    """

    def _policy(self, monkeypatch: pytest.MonkeyPatch, denied: bool):
        from kiro_crew.apps import bridges as bmod

        monkeypatch.setattr(bmod, "_may_auto_approve", lambda ref, ceiling=None: not denied)
        return bmod._apply_agent_mcp_policy(
            # The spec lives in the agent config so the grant resolves without a
            # global mcp.json — this test is about the auto-approve decision, not
            # about where a launch spec is discovered.
            {"mcpServers": {"srv": {"command": "x"}}, "tools": [], "allowedTools": []},
            "a",
            {"agents": {"a": {"servers": {"srv": {}}}}},
        )

    def test_permitted_server_is_auto_approved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out = self._policy(monkeypatch, denied=False)
        assert "@srv" in out["tools"]
        assert "@srv" in out["allowedTools"]

    def test_ceiling_denied_server_is_granted_but_never_auto_approved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = self._policy(monkeypatch, denied=True)
        # Still granted — the user's choice is not silently discarded...
        assert "@srv" in out["tools"]
        # ...but every call must now ask, which is what routes it to the gate.
        assert "@srv" not in out["allowedTools"]

    def test_a_governed_host_builtin_loses_auto_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`fs_read` in ``allowedTools`` bypassed the filesystem ceiling entirely.

        The earlier reasoning was that a bare tool name "is governed by its own
        scope at the gate" — the same mistake the per-tool MCP case made. A tool in
        ``allowedTools`` is auto-approved, so a ``filesystem.read`` deny never ran
        for ANY path. When the ceiling constrains the scope a builtin is evaluated
        under, the shortcut has to be declined so the call reaches the gate, which
        decides with the real path.
        """
        from kiro_crew.apps import bridges as bmod

        monkeypatch.setattr(
            bmod,
            "_may_auto_approve",
            lambda ref, ceiling=None: ref not in {"fs_read", "web_fetch"},
        )
        out = bmod._apply_agent_mcp_policy(
            {
                "tools": ["fs_read", "grep", "web_fetch", "@srv"],
                "allowedTools": ["fs_read", "grep", "web_fetch", "@srv"],
            },
            "a",
            {},
        )
        # Still granted — the app keeps the capability...
        assert out["tools"] == ["fs_read", "grep", "web_fetch", "@srv"]
        # ...but the governed ones must ask, so the gate sees them.
        assert out["allowedTools"] == ["grep", "@srv"]

    def test_an_ungoverned_host_keeps_every_builtin_auto_approved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ceiling must mean no behaviour change — this is the standalone path."""
        from kiro_crew.apps import bridges as bmod

        monkeypatch.setattr(bmod, "_may_auto_approve", lambda ref, ceiling=None: True)
        data = {"tools": ["fs_read"], "allowedTools": ["fs_read"]}
        assert bmod._apply_agent_mcp_policy(data, "a", {})["allowedTools"] == ["fs_read"]

    def test_builtin_probe_is_quiet_without_a_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ungoverned host keeps NON-floor entries — the predicate is a no-op
        there — but a tool whose always-on floor (sensitive-path / denied-command)
        lives at the gate is withheld even with no ceiling, since auto-approve
        would skip that un-disableable floor."""
        from kiro_crew.platform import context as ctxmod
        from kiro_crew.platform import governance as gmod

        monkeypatch.setattr(ctxmod, "current_context", lambda: object())
        # A network-only builtin carries no always-on floor → kept ungoverned.
        assert gmod.may_skip_gate("web_fetch", None) is True
        # A tool with no governed scope is never filtered, ceiling or not.
        assert gmod.may_skip_gate("some_unmapped_tool", None) is True
        # But a floor tool is withheld even without a ceiling (would otherwise
        # bypass sensitive-path blocking for ~/.ssh, ~/.aws, ...).
        assert gmod.may_skip_gate("fs_read", None) is False

    def test_a_template_inherited_ref_is_also_filtered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ceiling check must apply to entries the TEMPLATE already had.

        An app's packaged agent JSON ships its own ``allowedTools`` (e.g.
        ``@<app>:<server>``). Those were copied verbatim, so the check on the
        append path did nothing for exactly the refs that were already there — a
        ceiling-denied server stayed auto-approved and never reached the gate.
        """
        from kiro_crew.apps import bridges as bmod

        # Server-level, like the real predicate: `@denied/tool` belongs to
        # `denied`, so a per-tool entry is withheld too.
        monkeypatch.setattr(
            bmod,
            "_may_auto_approve",
            lambda ref, ceiling=None: ref.lstrip("@").split("/", 1)[0] != "denied",
        )
        out = bmod._apply_agent_mcp_policy(
            {
                "mcpServers": {},
                "tools": ["@denied", "@ok"],
                # As shipped by a template: a denied MCP ref, a permitted one, and
                # a host builtin that is not an MCP ref at all.
                "allowedTools": ["@denied", "@denied/tool", "@ok", "fs_read"],
            },
            "a",
            {},
        )
        assert "@denied" not in out["allowedTools"]
        assert "@denied/tool" not in out["allowedTools"]
        # A permitted server and a non-MCP builtin are untouched.
        assert "@ok" in out["allowedTools"]
        assert "fs_read" in out["allowedTools"]

    def test_a_per_tool_rule_also_declines_auto_approve(self) -> None:
        """A per-tool ceiling rule must block the server-wide shortcut.

        The regression this pins: auto-approve has only SERVER granularity, and
        the original check probed a sentinel tool (``@srv/probe``). An
        ``@srv/delete`` deny matches only itself, so the probe passed, the whole
        server entered ``allowedTools``, and ``delete`` was auto-approved —
        meaning it never reached the gate that was supposed to deny it. The
        claim that "the gate still denies it" was hollow precisely because
        auto-approve is the one path the gate never sees.
        """
        from types import SimpleNamespace

        from kiro_crew.platform.governance import _ceiling_mentions_mcp_server

        # deny-mode ruleset naming ONE tool under the server
        ruleset = SimpleNamespace(mode="deny", allow=(), deny=("@srv/delete",))
        ceiling = SimpleNamespace(get=lambda scope: ruleset if scope == "mcp" else None)

        assert _ceiling_mentions_mcp_server(ceiling, "srv") is True
        # A server the ceiling says nothing about keeps the shortcut.
        assert _ceiling_mentions_mcp_server(ceiling, "other") is False

    def test_an_allow_list_naming_some_tools_is_also_an_opinion(self) -> None:
        """Allow-mode listing a subset must not let the whole server through."""
        from types import SimpleNamespace

        from kiro_crew.platform.governance import _ceiling_mentions_mcp_server

        ruleset = SimpleNamespace(mode="allow", allow=("@srv/read",), deny=())
        ceiling = SimpleNamespace(get=lambda scope: ruleset if scope == "mcp" else None)
        assert _ceiling_mentions_mcp_server(ceiling, "srv") is True

    def test_server_prefix_is_not_matched_loosely(self) -> None:
        """`@srv-other` must not count as an opinion about `@srv`."""
        from types import SimpleNamespace

        from kiro_crew.platform.governance import _ceiling_mentions_mcp_server

        ruleset = SimpleNamespace(mode="deny", allow=(), deny=("@srv-other",))
        ceiling = SimpleNamespace(get=lambda scope: ruleset if scope == "mcp" else None)
        assert _ceiling_mentions_mcp_server(ceiling, "srv") is False

    def test_an_ungoverned_scope_is_not_an_opinion(self) -> None:
        from types import SimpleNamespace

        from kiro_crew.platform.governance import _ceiling_mentions_mcp_server

        ceiling = SimpleNamespace(get=lambda scope: None)
        assert _ceiling_mentions_mcp_server(ceiling, "srv") is False

    def test_an_ungoverned_host_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No ceiling composed -> the predicate must not strip grants."""
        from kiro_crew.platform import governance as gmod

        assert gmod.may_skip_gate("@srv", None) is True


class TestRegistrationTarget:
    def test_stdio_server_lands_in_agent_config(self, fake_home: Path) -> None:
        from kiro_crew.apps import bridges

        bridges._register_mcp_servers("mochi", _manifest({"pet": {"command": "mochi-mcp"}}))

        agent_cfg = json.loads(bridges._mcp_json_path().read_text())
        assert "mochi:pet" in agent_cfg["mcpServers"]

    def test_shared_kiro_file_is_untouched(self, fake_home: Path) -> None:
        """The whole point: Kiro IDE must not see the app's servers."""
        from kiro_crew.apps import bridges

        bridges._LEGACY_SHARED_MCP_PATH.write_text('{"mcpServers": {"user-owned": {}}}')

        bridges._register_mcp_servers("mochi", _manifest({"pet": {"command": "mochi-mcp"}}))

        shared = json.loads(bridges._LEGACY_SHARED_MCP_PATH.read_text())
        assert shared["mcpServers"] == {"user-owned": {}}, "app server leaked into shared file"

    def test_registration_preserves_other_agent_config_fields(self, fake_home: Path) -> None:
        """The agent config holds hooks/tools/prompt — a read-modify-write must keep them."""
        from kiro_crew.apps import bridges

        bridges._mcp_json_path().write_text(
            json.dumps({"name": "kirocrew", "includeMcpJson": False, "tools": ["fs_read"]})
        )

        bridges._register_mcp_servers("mochi", _manifest({"pet": {"command": "x"}}))

        cfg = json.loads(bridges._mcp_json_path().read_text())
        assert cfg["tools"] == ["fs_read"]
        assert cfg["includeMcpJson"] is False
        assert "mochi:pet" in cfg["mcpServers"]


class TestDeregistration:
    def test_removes_only_the_named_app(self, fake_home: Path) -> None:
        from kiro_crew.apps import bridges

        bridges._register_mcp_servers("mochi", _manifest({"pet": {"command": "a"}}))
        bridges._register_mcp_servers("other", _manifest({"srv": {"command": "b"}}))

        bridges._deregister_mcp_servers("mochi")

        cfg = json.loads(bridges._mcp_json_path().read_text())
        assert "mochi:pet" not in cfg["mcpServers"]
        assert "other:srv" in cfg["mcpServers"]

    def test_deregister_leaves_legacy_scrub_to_boot_reconcile(self, fake_home: Path) -> None:
        """deregister runs synchronously on the gateway event loop, so it must
        NOT take the legacy shared file's cross-process flock — that file is held
        by other processes (Kiro IDE, other agents) and a stall would freeze
        chat/heartbeat. The scrub is deferred to the OFF-loop boot reconcile, so
        the legacy entry survives a deregister on the hot path."""
        from kiro_crew.apps import bridges

        bridges._LEGACY_SHARED_MCP_PATH.write_text(
            json.dumps({"mcpServers": {"mochi:pet": {"command": "old"}, "keep-me": {}}})
        )

        bridges._deregister_mcp_servers("mochi")

        # Untouched here — reconcile_enabled_app_resources() scrubs it off-loop.
        shared = json.loads(bridges._LEGACY_SHARED_MCP_PATH.read_text())
        assert "mochi:pet" in shared["mcpServers"]
        assert "keep-me" in shared["mcpServers"]

    def test_missing_legacy_file_is_not_an_error(self, fake_home: Path) -> None:
        from kiro_crew.apps import bridges

        bridges._LEGACY_SHARED_MCP_PATH.unlink(missing_ok=True)
        assert bridges._scrub_legacy_shared_mcp("mochi") == 0


class TestRebuildSurvival:
    """A clean rebuild must re-derive app servers from the manifests.

    Before the fix the entries were mirrored in from the shared file; now that
    apps no longer write it, the manifests are the only source — so without this
    re-derivation a clean rebuild would silently drop every app's tools.
    """

    def test_collect_returns_enabled_app_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew import agent

        monkeypatch.setattr(
            "kiro_crew.apps.manager.list_apps", lambda: [{"name": "mochi"}], raising=False
        )
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: True, raising=False)
        monkeypatch.setattr(
            "kiro_crew.apps.manager.get_app_manifest",
            lambda n: _manifest({"pet": {"command": "mochi-mcp"}}),
            raising=False,
        )

        assert agent._collect_app_mcp_servers() == {"mochi:pet": {"command": "mochi-mcp"}}

    def test_disabled_app_contributes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A disabled app's tools must not reach any session."""
        from kiro_crew import agent

        monkeypatch.setattr(
            "kiro_crew.apps.manager.list_apps", lambda: [{"name": "mochi"}], raising=False
        )
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: False, raising=False)

        assert agent._collect_app_mcp_servers() == {}

    def test_one_broken_manifest_does_not_drop_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import agent

        def _manifest_or_boom(name: str) -> Any:
            if name == "broken":
                raise ValueError("corrupt manifest")
            return _manifest({"srv": {"command": "ok"}})

        monkeypatch.setattr(
            "kiro_crew.apps.manager.list_apps",
            lambda: [{"name": "broken"}, {"name": "good"}],
            raising=False,
        )
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: True, raising=False)
        monkeypatch.setattr(
            "kiro_crew.apps.manager.get_app_manifest", _manifest_or_boom, raising=False
        )

        assert agent._collect_app_mcp_servers() == {"good:srv": {"command": "ok"}}

    def test_self_managed_http_url_is_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A self-managed HTTP server (no backend.entryPoint) has an authoritative
        fixed URL and never gets a live registration — its manifest URL must
        survive the rebuild rather than being dropped as an illustrative port."""
        from kiro_crew import agent

        monkeypatch.setattr(
            "kiro_crew.apps.manager.list_apps", lambda: [{"name": "companion"}], raising=False
        )
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: True, raising=False)
        monkeypatch.setattr(
            "kiro_crew.apps.manager.get_app_manifest",
            lambda n: _manifest({"companion": {"url": "http://127.0.0.1:7778/mcp"}}),
            raising=False,
        )
        # No live registration exists for a self-managed server.
        monkeypatch.setattr(
            "kiro_crew.apps.bridges.registered_app_mcp_servers", lambda: {}, raising=False
        )

        out = agent._collect_app_mcp_servers()
        assert out == {"companion:companion": {"url": "http://127.0.0.1:7778/mcp"}}

    def test_gateway_managed_http_url_without_live_port_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gateway-launched backend (backend.entryPoint set) carries only an
        illustrative port until its process resolves one; with no live entry the
        dead URL must be skipped, not written."""
        from kiro_crew import agent

        monkeypatch.setattr(
            "kiro_crew.apps.manager.list_apps", lambda: [{"name": "hosted"}], raising=False
        )
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: True, raising=False)
        monkeypatch.setattr(
            "kiro_crew.apps.manager.get_app_manifest",
            lambda n: _manifest(
                {"srv": {"url": "http://127.0.0.1:1/mcp"}}, entry_point="backend/app.py"
            ),
            raising=False,
        )
        monkeypatch.setattr(
            "kiro_crew.apps.bridges.registered_app_mcp_servers", lambda: {}, raising=False
        )

        assert agent._collect_app_mcp_servers() == {}


class TestCeilingProbesFailClosed:
    """A probe that cannot answer must decline the auto-approve shortcut.

    These helpers decide who SKIPS the PreToolUse gate, so "keep the entry on
    error" is not a deferred decision — an entry in `allowedTools` is
    auto-approved and none of its calls ever reach the gate. Keeping it on a
    failed probe would silently bypass the ceiling on a host that has one. The
    cost of failing closed is one permission prompt; no capability is lost.

    An ungoverned host (no ceiling at all) is a different answer and still keeps
    its grants — otherwise every ordinary user pays a prompt for a ceiling they
    do not have.
    """

    def test_mcp_probe_declines_when_the_evaluator_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import governance as gmod

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("evaluator exploded")

        monkeypatch.setattr("kiro_crew.platform.governance.gate_decision", _boom)
        monkeypatch.setattr(
            "kiro_crew.platform.context.current_context",
            lambda: type("C", (), {"governance": object()})(),
        )
        assert gmod.may_skip_gate("@srv", object()) is False

    def test_builtin_probe_declines_when_the_evaluator_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import governance as gmod

        class _Exploding:
            def get(self, _scope: str) -> Any:
                raise RuntimeError("evaluator exploded")

        monkeypatch.setattr(
            "kiro_crew.platform.context.current_context",
            lambda: type("C", (), {"governance": _Exploding()})(),
        )
        tool = next(iter(gmod.BUILTIN_TOOL_SCOPES))
        assert gmod.may_skip_gate(tool, _Exploding()) is False

    def test_an_ungoverned_host_still_keeps_its_grants(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import governance as gmod

        monkeypatch.setattr(
            "kiro_crew.platform.context.current_context",
            lambda: type("C", (), {"governance": None})(),
        )
        # A non-floor builtin: no always-on gate floor, so an ungoverned host
        # keeps its grant. (A floor builtin like fs_read is withheld even
        # ungoverned — covered by test_floor_builtins_are_withheld_*.)
        assert gmod.may_skip_gate("@srv", None) is True
        assert gmod.may_skip_gate("web_fetch", None) is True


class TestBothWritePointsConsultTheCeiling:
    """`allowedTools` is written in TWO places; one predicate governs both.

    Auto-approve is the only path that never reaches `hooks.on_tool_call`, so a
    list written without consulting the ceiling is a set of tools the ceiling can
    no longer refuse. Closing that in app-agent materialization
    (`apps/bridges.py`) left the OTHER writer — the host agent's shared-MCP sync
    in `agent.py` — appending every user-installed server's `@ref` unconditionally,
    so on a governed host the primary agent kept the whole bypass. These pin that
    both writers route through `governance.may_skip_gate`.
    """

    def test_the_predicate_is_the_only_implementation(self) -> None:
        """Neither writer may re-derive the rule or the tool→scope mapping.

        A second copy is how the two drifted: the app layer had its own scope map,
        so a newly governed builtin or scope re-opened the shortcut for whichever
        copy had not heard of it.
        """
        import inspect

        from kiro_crew import agent, cli_doctor
        from kiro_crew.apps import bridges
        from kiro_crew.dashboard.handlers import mcp as mcp_handler

        # Every module that can put an entry on an auto-approve list. Found by an
        # AST sweep for appends/assignments into an `allowedTools` list, not by
        # reading review comments: the first two rounds fixed the two writers that
        # had been REPORTED and left the dashboard enable paths and doctor's
        # auto-fix — the most common way a grant is created — wide open.
        for mod in (agent, bridges, mcp_handler, cli_doctor):
            src = inspect.getsource(mod)
            assert "may_skip_gate" in src, f"{mod.__name__} must use the shared predicate"
            assert "_BUILTIN_TOOL_SCOPES" not in src, (
                f"{mod.__name__} re-declares the builtin tool→scope map; it belongs to "
                f"platform/governance.py (see BUILTIN_TOOL_SCOPES)"
            )

    def test_every_mapped_scope_exists_in_the_catalog(self) -> None:
        """A scope name typo would silently mean "ungoverned", i.e. auto-approved."""
        from kiro_crew.platform.governance import BUILTIN_TOOL_SCOPES, SCOPE_CATALOG

        unknown = sorted(
            {s for scopes in BUILTIN_TOOL_SCOPES.values() for s in scopes} - set(SCOPE_CATALOG)
        )
        assert unknown == [], f"not in SCOPE_CATALOG: {unknown}"

    def test_a_governed_shared_server_is_mounted_but_not_auto_approved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported bypass: mount it, but do not hand it a gate exemption."""
        from kiro_crew import agent

        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: ref != "@denied")
        config: dict[str, Any] = {"tools": [], "allowedTools": []}
        for name in ("denied", "ok"):
            ref = f"@{name}"
            keys = ("tools", "allowedTools") if agent._may_auto_approve(ref) else ("tools",)
            for key in keys:
                if ref not in config[key]:
                    config[key].append(ref)

        assert config["tools"] == ["@denied", "@ok"], "both must still MOUNT"
        assert config["allowedTools"] == ["@ok"], "the governed one must go through the gate"

    def test_a_grant_written_before_the_ceiling_arrived_is_revoked(self) -> None:
        """An existing agent config must not keep a stale exemption.

        The sync runs against a config that may predate the policy, so "do not
        add" is not enough — the entry has to be removed, or the ceiling only
        applies to hosts that were governed before their first launch.
        """
        import inspect

        from kiro_crew import agent

        src = inspect.getsource(agent.install_agent)
        marker = 'if "allowedTools" not in keys:'
        assert marker in src, "the sync must also strip a pre-existing grant"


class TestAnEmptyAllowlistIsTheStrictestCeiling:
    """`mode="allow"` with an EMPTY allow list denies everything.

    Counting patterns inverts the answer in exactly the strictest case: a ruleset
    with no patterns at all reads as "no opinion" and hands out the auto-approve
    exemption, when under allow-mode it is deny-all. `ScopedRuleset`'s own
    docstring says so ("An absent/empty `allow` under allow-mode is the empty set
    (deny-all)"), which is what makes the pattern-count reading a bug rather than
    a judgement call.
    """

    def _ceiling(self, scope: str, ruleset: Any) -> Any:
        return type("C", (), {"get": lambda _self, s: ruleset if s == scope else None})()

    def test_empty_allowlist_withholds_auto_approve(self) -> None:
        from kiro_crew.platform.governance import ScopedRuleset, may_skip_gate

        rules = ScopedRuleset(mode="allow", allow=(), deny=())
        ceiling = self._ceiling("filesystem.read", rules)
        assert may_skip_gate("fs_read", ceiling) is False

    def test_populated_allowlist_also_withholds_it(self) -> None:
        from kiro_crew.platform.governance import ScopedRuleset, may_skip_gate

        rules = ScopedRuleset(mode="allow", allow=("/srv/**",), deny=())
        ceiling = self._ceiling("filesystem.read", rules)
        assert may_skip_gate("fs_read", ceiling) is False

    def test_an_empty_denylist_permits_everything_and_is_not_an_opinion(self) -> None:
        """The one shape that really is ungoverned — do not tax it with a prompt.

        Uses an MCP ref (not a builtin file tool): a bare filesystem builtin is
        now always withheld by the gate floor regardless of the ceiling shape, so
        the "empty denylist is not an opinion" property is observed through a ref,
        for which the arg-derived filesystem.read scope is the deciding factor.
        """
        from kiro_crew.platform.governance import ScopedRuleset, may_skip_gate

        rules = ScopedRuleset(mode="deny", allow=(), deny=())
        ceiling = self._ceiling("filesystem.read", rules)
        assert may_skip_gate("@srv", ceiling) is True

    def test_a_populated_denylist_is_an_opinion(self) -> None:
        from kiro_crew.platform.governance import ScopedRuleset, may_skip_gate

        rules = ScopedRuleset(mode="deny", allow=(), deny=("/etc/**",))
        ceiling = self._ceiling("filesystem.read", rules)
        assert may_skip_gate("fs_read", ceiling) is False

    def test_an_unrecognized_shape_is_not_proof_of_safety(self) -> None:
        from kiro_crew.platform.governance import may_skip_gate

        ceiling = self._ceiling("filesystem.read", type("R", (), {"mode": "???"})())
        assert may_skip_gate("fs_read", ceiling) is False


class TestManifestAutoApproveCannotSelfGrantAnExemption:
    """`autoApprove` is a second, more direct route to the same bypass.

    kiro-cli approves an autoApproved MCP tool locally and emits NO permission
    request, so `hooks.on_tool_call` never runs for it — `agent.py`'s managed-server
    block states the rule outright ("DELIBERATELY NO autoApprove KEY, and none may
    ever be added"). App-contributed specs were copied verbatim, so the grant was
    declared by an app MANIFEST — content that can come from outside this repo —
    rather than by KiroCrew or the user.
    """

    def test_a_governed_server_loses_autoapprove_but_keeps_its_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import agent

        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: False)
        out = agent._ceiling_filtered_spec(
            "someapp:srv", {"url": "http://127.0.0.1:1/mcp", "autoApprove": ["a", "b"]}
        )
        assert "autoApprove" not in out
        assert out["url"] == "http://127.0.0.1:1/mcp", "the server itself must survive"

    def test_an_ungoverned_host_keeps_the_manifest_grant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import agent

        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: True)
        out = agent._ceiling_filtered_spec("someapp:srv", {"autoApprove": ["a"]})
        assert out["autoApprove"] == ["a"]

    def test_the_collector_routes_every_app_spec_through_the_filter(self) -> None:
        """A future caller must not reintroduce the verbatim copy."""
        import inspect

        from kiro_crew import agent

        src = inspect.getsource(agent._collect_app_mcp_servers)
        # The one place a spec is written must be the ceiling-filtered call —
        # whatever spec was chosen (live-registered or the manifest fallback) is
        # routed through the filter, never assigned to servers[ref] raw.
        assert "servers[ref] = _ceiling_filtered_spec(" in src
        # The only assignment to servers[ref] is the filtered one: any raw form
        # (a manifest/live spec written straight in) would be a bypass.
        for bad in ("servers[ref] = dict(", "servers[ref] = spec", "servers[ref] = live"):
            assert bad not in src, f"spec written without the filter: {bad}"

    def test_an_empty_mcp_allowlist_also_withholds_it(self) -> None:
        """The MCP branch reaches the same answer by a different route.

        `_ceiling_mentions_mcp_server` scans patterns and an empty allowlist has
        none — the server-level `gate_decision` probe is what catches deny-all
        here. Pinned because the two branches must not diverge: an operator whose
        ceiling is `mcp: {mode: allow, allow: []}` has denied every MCP tool.
        """
        from kiro_crew.platform import governance as gov

        ceiling = gov.GovernanceCeiling(
            version=1,
            boot=gov.BootControls(),
            controls={"mcp": gov.ScopedRuleset(mode="allow", allow=(), deny=())},
        )
        assert gov.may_skip_gate("@anything", ceiling) is False


class TestATighteningReachesAnExistingConfig:
    """A re-derived app spec must REPLACE the previous rebuild's, not lose to it.

    `_collect_app_mcp_servers` re-derives every app's MCP spec on each rebuild —
    that is what applies the current ceiling, and what lets a clean rebuild keep
    app servers at all. Merging it with `setdefault` made that re-derivation
    inert for any app already in the config: a spec whose `autoApprove` had just
    been stripped lost to the stale entry that still carried it, so the
    tightening reached only fresh installs and those tools kept skipping the gate.
    """

    def test_the_app_key_is_assigned_not_setdefault(self) -> None:
        import inspect

        from kiro_crew import agent

        src = inspect.getsource(agent.rebuild_agent_config)
        marker = 'config.setdefault("mcpServers", {})[_app_srv] = _app_spec'
        assert marker in src, "the app's own key must be assigned, not setdefault"

    def test_a_stale_autoapprove_is_replaced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End state, not just the shape: the stripped spec is what remains."""
        from kiro_crew import agent

        stale = {"url": "http://127.0.0.1:1/mcp", "autoApprove": ["danger"]}
        fresh = {"url": "http://127.0.0.1:1/mcp"}  # ceiling stripped autoApprove
        config: dict[str, Any] = {"mcpServers": {"someapp:srv": dict(stale)}}
        monkeypatch.setattr(agent, "_collect_app_mcp_servers", lambda: {"someapp:srv": fresh})

        for srv, spec in agent._collect_app_mcp_servers().items():
            config.setdefault("mcpServers", {})[srv] = spec

        assert "autoApprove" not in config["mcpServers"]["someapp:srv"]


class TestACapabilityGrantNeedsALiteralTrue:
    """`bool()` on a JSON value grants on anything truthy — including `"false"`.

    A manifest writing `"spawn": "false"` to DENY was handed the capability to
    launch unattended agents, because the STRING is truthy. Requiring the literal
    `true` makes a malformed value deny, which is the direction a grant has to
    fail in.
    """

    @pytest.mark.parametrize("value", ["false", "no", "0", 0, 1, "true", [], {}, None])
    def test_only_a_real_true_grants(self, value: Any) -> None:
        from kiro_crew.apps.manifest import Permissions

        perms = Permissions.from_dict(
            {"spawn": value, "cron": value, "network": value, "storage": value}
        )
        expected = value is True
        assert perms.spawn is expected, f"spawn granted for {value!r}"
        assert perms.cron is expected
        assert perms.network is expected
        assert perms.storage is expected

    def test_a_real_true_still_grants(self) -> None:
        from kiro_crew.apps.manifest import Permissions

        perms = Permissions.from_dict({"spawn": True, "cron": True})
        assert perms.spawn is True
        assert perms.cron is True

    def test_a_restriction_fails_the_other_way(self) -> None:
        """Mirrored fix: an unexpected value must KEEP a restriction on.

        `is True` would be wrong here — `"true"` would turn signature
        verification OFF. The safe default follows what the field withholds.
        """
        from kiro_crew.apps.admission import AppAdmissionPolicy

        for value in ["false", "true", 1, "yes", None]:
            pol = AppAdmissionPolicy.from_dict({"require_signature": value})
            assert pol.require_signature is True, f"restriction dropped for {value!r}"
        assert AppAdmissionPolicy.from_dict({"require_signature": False}).require_signature is False


class TestAutoApproveIsFilteredAtTheWriteChokepoint:
    """One map-level pass, not one patch per source.

    `autoApprove` reaches a written agent config from at least four places — an
    app manifest, a per-agent MCP policy, a materialized managed ref, and an entry
    preserved from the previous file (plus an imported config from another tool).
    Three review rounds each fixed the source that had been REPORTED and left the
    rest, so the filter now runs once on the final map, at the position both
    writers hand it to disk.
    """

    def test_a_governed_server_loses_the_key_and_keeps_the_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import governance as gov

        monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: False)
        out = gov.strip_ungoverned_auto_approve(
            {"srv": {"command": "x", "autoApprove": ["a"]}, "other": {"command": "y"}}
        )
        assert "autoApprove" not in out["srv"]
        assert out["srv"]["command"] == "x"
        assert out["other"] == {"command": "y"}

    def test_an_ungoverned_host_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.platform import governance as gov

        monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: True)
        spec = {"srv": {"command": "x", "autoApprove": ["a"]}}
        assert gov.strip_ungoverned_auto_approve(spec)["srv"]["autoApprove"] == ["a"]

    def test_both_config_writers_run_the_pass(self) -> None:
        """The host agent writer and app-agent materialization, at the same point."""
        import inspect

        from kiro_crew import agent
        from kiro_crew.apps import bridges

        assert "_strip_ungoverned_auto_approve" in inspect.getsource(agent.install_agent)
        assert "_strip_ungoverned_auto_approve" in inspect.getsource(bridges._register_agents)


class TestEveryWriterRevokesAStaleGrant:
    """ "Do not add" is not enough — a grant can predate the ceiling.

    A policy can arrive on a host whose agent config was written while it was
    ungoverned. A writer that only declines to MINT leaves that config carrying
    the exemption forever, so the ceiling would apply only to installs governed
    before their first launch. Doctor was the last writer still doing that.
    """

    def test_doctor_removes_a_grant_the_ceiling_now_denies(self) -> None:
        import inspect

        from kiro_crew import cli_doctor

        src = inspect.getsource(cli_doctor)
        assert "allowed.remove(ref)" in src, "doctor must revoke, not merely decline to add"

    def test_no_writer_only_declines(self) -> None:
        """Each module that mints a grant must also be able to take one back."""
        import inspect

        from kiro_crew import agent, cli_doctor
        from kiro_crew.apps import bridges
        from kiro_crew.dashboard.handlers import mcp as mcp_handler

        for mod in (agent, cli_doctor, bridges, mcp_handler):
            src = inspect.getsource(mod)
            assert "may_skip_gate" in src, f"{mod.__name__} must consult the ceiling"
            revokes = any(
                marker in src
                for marker in ("allowed.remove(", "stale.remove(", "lst.remove(", ".pop(")
            )
            assert revokes, f"{mod.__name__} never revokes a grant it can mint"


class TestBuiltinAutoApprovalsGoThroughTheFinalPass:
    """A governed HOST builtin (fs_read, execute_bash, …) must lose its blanket
    auto-approve too — not just MCP servers.

    The per-writer checks apply the ceiling to entries THEY add, but a builtin
    auto-approve arrives straight from the agent TEMPLATE into ``allowedTools``
    and no writer re-touches it. Under a ``filesystem.read`` ceiling ``fs_read``
    would stay on the blanket list and kiro-cli would approve reads without ever
    reaching the gate. ``rebuild_agent_config`` runs a LAST pass over the whole
    ``allowedTools`` list through the same predicate to close that.
    """

    def test_rebuild_filters_the_allowed_list_through_the_predicate(self) -> None:
        import inspect

        from kiro_crew import agent

        src = inspect.getsource(agent.rebuild_agent_config)
        # The final pass partitions the list through the predicate (kept vs
        # withheld) and writes the kept set back.
        assert "_may_auto_approve(ref)" in src, "the final list pass must consult the ceiling"
        assert 'config["allowedTools"] = kept' in src

    def test_the_final_pass_audits_what_it_withholds(self) -> None:
        """Withholding a template grant is a permission DECISION and must leave a
        SEL trail — the per-writer paths already do; this final pass is the only
        place a template builtin loses its grant, so a silent drop would be the
        one governance outcome with no record.
        """
        import inspect

        from kiro_crew import agent

        src = inspect.getsource(agent.rebuild_agent_config)
        # The withheld branch of the final allowedTools pass emits the same event
        # the shared-sync path uses.
        assert "withheld" in src
        assert 'operation="mcp_auto_approve_withheld"' in src

    def test_a_governed_builtin_is_dropped_but_still_mounted(self) -> None:
        """The end state the pass produces: fs_read leaves allowedTools, stays in tools."""
        # Simulate a filesystem.read ceiling: fs_read is governed, the rest silent.
        governed = {"fs_read"}

        def _fake(ref: str) -> bool:
            return ref not in governed

        config = {
            "tools": ["fs_read", "fs_write", "@ok"],
            "allowedTools": ["fs_read", "fs_write", "@ok"],
        }
        # Exactly the expression rebuild_agent_config applies.
        config["allowedTools"] = [ref for ref in config["allowedTools"] if _fake(ref)]

        assert "fs_read" in config["tools"], "a governed builtin must still MOUNT"
        assert "fs_read" not in config["allowedTools"], "…but lose its blanket auto-approve"
        assert config["allowedTools"] == ["fs_write", "@ok"], "the silent ones are kept"


class TestTheCodeToolIsGovernedForWrites:
    """`code` edits files and runs formatters, so a write/commands/tools ceiling
    must withhold its blanket auto-approve.

    Missing from BUILTIN_TOOL_SCOPES it defaulted to auto-approved — a
    ``filesystem.write`` ceiling could not reach it, so the model could edit
    freely with no permission request. It is mapped to all three scopes it
    touches so any of those ceilings forces it through the gate.
    """

    def _ceiling(self, scope, ruleset):
        return type("C", (), {"get": lambda _s, s: ruleset if s == scope else None})()

    def test_code_is_in_the_map(self) -> None:
        from kiro_crew.platform.governance import BUILTIN_TOOL_SCOPES

        assert set(BUILTIN_TOOL_SCOPES["code"]) == {"commands", "tools", "filesystem.write"}

    def test_a_write_ceiling_withholds_code(self) -> None:
        from kiro_crew.platform.governance import ScopedRuleset, may_skip_gate

        rules = ScopedRuleset(mode="allow", allow=("/srv/**",), deny=())
        assert may_skip_gate("code", self._ceiling("filesystem.write", rules)) is False

    def test_a_commands_ceiling_withholds_code(self) -> None:
        from kiro_crew.platform.governance import ScopedRuleset, may_skip_gate

        rules = ScopedRuleset(mode="deny", allow=(), deny=("rm *",))
        assert may_skip_gate("code", self._ceiling("commands", rules)) is False

    def test_an_ungoverned_host_withholds_code_because_of_its_floor(self) -> None:
        from kiro_crew.platform.governance import may_skip_gate

        # `code` maps to commands + filesystem.write — both always-on gate floors
        # (denied commands, sensitive-path). So it is withheld from auto-approve
        # even on an ungoverned host: auto-approve would skip that floor.
        assert may_skip_gate("code", None) is False

    def test_floor_builtins_are_withheld_even_without_a_ceiling(self) -> None:
        """A builtin whose mandatory floor (sensitive-path / denied-command) is
        enforced at the gate must never be auto-approved, ceiling or not —
        auto-approve is the one path that skips that un-disableable floor.
        Network-only and unmapped builtins carry no such floor and stay allowed."""
        from kiro_crew.platform.governance import may_skip_gate

        for tool in ("fs_read", "fs_write", "glob", "grep", "execute_bash", "code"):
            assert may_skip_gate(tool, None) is False, tool
        for tool in ("web_fetch", "web_search", "some_unmapped_tool"):
            assert may_skip_gate(tool, None) is True, tool


class TestAppServersAreMounted:
    """An enabled app's MCP server must be MOUNTED, not just defined.

    A server present only in `mcpServers` is never referenced, so kiro-cli never
    loads it and the app's tools are silently unavailable. The rebuild adds each
    app server's `@ref` to `tools` (the unconditional mount) alongside writing its
    spec.
    """

    def test_the_rebuild_adds_app_server_refs_to_tools(self) -> None:
        import inspect

        from kiro_crew import agent

        src = inspect.getsource(agent.rebuild_agent_config)
        assert 'config.setdefault("tools", []).append(f"@{_app_srv}")' in src


class TestProfileOnlyGovernanceWithholdsAutoApprove:
    """A Level-2 PROFILE can govern a ref even with NO policy ceiling.

    `may_skip_gate(ref, ceiling)` alone only consults the POLICY ceiling, so on a
    host with no ceiling but a profile that denies `@srv/delete`, a blanket
    auto-approve of `@srv` would bypass that profile. `may_skip_gate_now` must
    withhold whenever any configured profile could govern the ref — the runtime
    gate then applies the specific one.
    """

    def test_a_profile_deny_withholds_even_without_a_ceiling(self, monkeypatch) -> None:
        from kiro_crew.platform import governance as gov
        from kiro_crew.platform import governance_profiles as gp

        # No policy ceiling.
        monkeypatch.setattr(gp, "any_configured_profile_governs", gp.any_configured_profile_governs)

        class _Ctx:
            governance = None

        monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: _Ctx())
        # A profile that governs the `mcp` scope (denies a server's tool).
        prof = gov.Profile(
            name="p",
            controls={"mcp": gov.ScopedRuleset(mode="allow", allow=("@keep",), deny=())},
        )
        monkeypatch.setattr(gp._STORE, "resolved", lambda: True)
        monkeypatch.setattr(gp._STORE, "all_profiles", lambda: [prof])

        # @srv is not in the profile's allow-list → the profile governs/denies it.
        assert gov.may_skip_gate_now("@srv") is False
        # A host with no ceiling AND no profiles keeps auto-approve.
        monkeypatch.setattr(gp._STORE, "all_profiles", lambda: [])
        assert gov.may_skip_gate_now("@srv") is True

    def test_an_unresolved_store_fails_closed(self, monkeypatch) -> None:
        from kiro_crew.platform import governance_profiles as gp

        monkeypatch.setattr(gp._STORE, "resolved", lambda: False)
        assert gp.any_configured_profile_governs("@srv") is True


class TestKirocrewJsonHasOneSerializedWriter:
    """kirocrew.json is written by BOTH the regenerating rebuild and the app-MCP
    registration path (bridges._register_mcp_servers, under bridges._mcp_lock).
    Rebuild must hold that same lock across a final re-read+merge so a register
    that lands after its snapshot is not silently overwritten.
    """

    def test_rebuild_writes_kirocrew_json_under_the_shared_mcp_lock(self) -> None:
        import inspect

        from kiro_crew import agent

        src = inspect.getsource(agent.rebuild_agent_config)
        assert "with _mcp_lock():" in src, "the kirocrew.json write must hold bridges' lock"
        assert "_read_mcp_json_unlocked()" in src, "…and re-read app entries under it"
        # The merge only re-adds app-namespaced servers the snapshot missed.
        assert '":" in _k' in src


class TestEveryBuiltinIsCheckedAgainstTheToolsScope:
    """The `tools` scope governs tool NAMES, so it must gate EVERY builtin's
    auto-approve — including tools absent from the capability map. Otherwise an
    unmapped builtin (report / introspect / session / any future one) returned
    True unconditionally and its shipped grant bypassed a `tools` ceiling.
    """

    def _ceiling(self, scope, ruleset):
        return type("C", (), {"get": lambda _s, s: ruleset if s == scope else None})()

    def test_an_unmapped_builtin_is_withheld_by_a_tools_opinion(self) -> None:
        from kiro_crew.platform.governance import ScopedRuleset, may_skip_gate

        rules = ScopedRuleset(mode="deny", allow=(), deny=("report",))
        # `report` is NOT in BUILTIN_TOOL_SCOPES, yet a tools opinion must reach it.
        assert may_skip_gate("report", self._ceiling("tools", rules)) is False
        assert may_skip_gate("introspect", self._ceiling("tools", rules)) is False

    def test_an_unmapped_builtin_is_kept_when_tools_is_silent(self) -> None:
        from kiro_crew.platform.governance import may_skip_gate

        # No ceiling opinion anywhere → keep the grant.
        assert may_skip_gate("report", self._ceiling("filesystem.read", None)) is True

    def test_a_mapped_builtin_still_honours_its_capability_scope(self) -> None:
        from kiro_crew.platform.governance import ScopedRuleset, may_skip_gate

        rules = ScopedRuleset(mode="allow", allow=("/srv/**",), deny=())
        assert may_skip_gate("fs_read", self._ceiling("filesystem.read", rules)) is False


class TestDoctorAuditsItsRevocation:
    """Doctor revoking a stale grant is a permission decision — it must leave the
    same SEL trail every other writer does, not revoke silently."""

    def test_doctor_emits_the_withheld_event_on_revoke(self) -> None:
        import inspect

        from kiro_crew import cli_doctor

        src = inspect.getsource(cli_doctor)
        assert 'operation="mcp_auto_approve_withheld"' in src
        assert "allowed.remove(ref)" in src


class TestQueuedSpawnKeepsAppIdentity:
    """A spawn deferred by the concurrency cap is re-issued by _drain_queue from
    the queued params dict. If `app` is dropped there, the re-spawn rechecks
    governance without it and a tightened app profile is bypassed.
    """

    def test_the_queued_params_include_app(self) -> None:
        import inspect

        from kiro_crew import subagent

        src = inspect.getsource(subagent.SubagentManager.spawn)
        assert '"app": app,' in src, "the queued params must carry the app identity"


class TestAppAgentWithholdIsAudited:
    """The app-agent materializer drops a ceiling-governed auto-approve from a
    template's list — a permission decision that must be SEL-audited, like the
    host sync and doctor do."""

    def test_withholding_emits_the_sel_event(self, monkeypatch) -> None:
        from kiro_crew.apps import bridges

        monkeypatch.setattr(bridges, "_may_auto_approve", lambda ref: ref != "@app:denied")
        events: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw)

        monkeypatch.setattr(bridges, "sel", lambda: _Sel())
        out = bridges._ceiling_filtered_allowed(["@app:denied", "@app:ok"], "myagent")
        assert out == ["@app:ok"]
        assert events and events[0]["operation"] == "mcp_auto_approve_withheld"
        assert "@app:denied" in events[0]["resources"]

    def test_no_event_when_nothing_withheld(self, monkeypatch) -> None:
        from kiro_crew.apps import bridges

        monkeypatch.setattr(bridges, "_may_auto_approve", lambda ref: True)
        events: list[dict] = []
        monkeypatch.setattr(
            bridges,
            "sel",
            lambda: type("S", (), {"log_api_access": lambda _s, **k: events.append(k)})(),
        )
        out = bridges._ceiling_filtered_allowed(["@app:ok"], "myagent")
        assert out == ["@app:ok"] and events == []


class TestStripAutoApproveIsAudited:
    """Dropping a governed `autoApprove` revokes a gate exemption — a permission
    decision that must be SEL-audited like every other withhold path."""

    def test_strip_emits_the_withheld_event(self, monkeypatch) -> None:
        from kiro_crew.platform import governance as gov

        monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: False)  # governed
        events: list[dict] = []

        monkeypatch.setattr(
            gov,
            "sel",
            lambda: type("S", (), {"log_api_access": lambda _s, **k: events.append(k)})(),
        )
        out = gov.strip_ungoverned_auto_approve({"srv": {"url": "u", "autoApprove": ["x"]}})
        assert "autoApprove" not in out["srv"]
        assert events and events[0]["operation"] == "mcp_auto_approve_withheld"
        assert "@srv" in events[0]["resources"]


class TestRebuildDoesNotResurrectDeregisteredApps:
    """The final locked re-merge reconciles app-namespaced servers WITH on_disk:
    on_disk (written under the same lock by register/deregister) is authoritative,
    so an app server a concurrent deregister removed must be DROPPED, not restored
    from the pre-lock snapshot (which would write back a dead URL).
    """

    def test_rebuild_removes_app_keys_absent_from_on_disk(self) -> None:
        import inspect

        from kiro_crew import agent

        src = inspect.getsource(agent.rebuild_agent_config)
        assert "on_disk_app" in src
        assert "del servers[_k]" in src, "an app key absent from on_disk must be removed"
        # And on_disk is authoritative: present app entries are ASSIGNED
        # (overwrite), not merely added-if-missing, so a re-registration on a new
        # port replaces the stale snapshot rather than losing to it.
        assert "if _k in on_disk_app:\n                        servers[_k] = _v" in src

    def test_unverifiable_app_state_fails_closed(self) -> None:
        """If enablement cannot be verified — a malformed installed.json makes
        is_app_enabled raise, or the apps subsystem will not import — the merge
        must DROP the app-scoped entry, never keep it. Retaining it would leave a
        deregistered/unknown app's MCP tools callable with no way to confirm they
        should be. Both _app_of_key_enabled fallbacks therefore return False."""
        import inspect

        from kiro_crew import agent

        src = inspect.getsource(agent.rebuild_agent_config)
        assert "keep tools" not in src
        after = src.split("def _app_of_key_enabled", 1)[1]
        assert "return True" not in after, "unverifiable app state must fail closed"


class TestMcpAutoApproveHonoursArgumentScopes:
    """An MCP server's tools can read/write files or reach the network, and the
    gate enforces those from the real call args. Auto-approving the server skips
    that gate, so a filesystem/network ceiling must withhold auto-approve even
    when the server itself is not named in the mcp ruleset."""

    def _ceiling(self, scope, ruleset):
        return type("C", (), {"get": lambda _s, s: ruleset if s == scope else None})()

    def test_a_filesystem_write_ceiling_withholds_an_mcp_server(self) -> None:
        from kiro_crew.platform.governance import ScopedRuleset, may_skip_gate

        rules = ScopedRuleset(mode="allow", allow=("/srv/**",), deny=())
        # No mcp opinion at all — only filesystem.write — yet @srv must be withheld.
        assert may_skip_gate("@srv", self._ceiling("filesystem.write", rules)) is False

    def test_a_network_egress_ceiling_withholds_an_mcp_server(self) -> None:
        from kiro_crew.platform.governance import ScopedRuleset, may_skip_gate

        rules = ScopedRuleset(mode="deny", allow=(), deny=("evil.example",))
        assert may_skip_gate("@srv", self._ceiling("network.egress", rules)) is False

    def test_an_ungoverned_host_still_keeps_the_mcp_server(self) -> None:
        from kiro_crew.platform.governance import may_skip_gate

        assert may_skip_gate("@srv", None) is True


class TestConfigSanitizerAuditsWithheld:
    """The whole-config sanitizer (PUT /api/agent/config, browser convergence)
    drops governed allowedTools refs — that withhold is a permission decision and
    must emit the same SEL event the per-ref writers do."""

    def test_sanitizer_emits_withheld_for_dropped_allowedtools(self, monkeypatch):
        from kiro_crew.platform import governance as gov

        events: list[dict] = []
        monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: ref != "@denied")
        monkeypatch.setattr(
            gov,
            "sel",
            lambda: type("S", (), {"log_api_access": lambda _s, **k: events.append(k)})(),
        )

        config = {"allowedTools": ["@ok", "@denied", 123], "tools": ["@denied"]}
        gov.sanitize_agent_config_governance(config)

        assert config["allowedTools"] == ["@ok"]  # governed + non-string dropped
        assert config["tools"] == ["@denied"]  # mount untouched
        ops = {e.get("operation") for e in events}
        assert "mcp_auto_approve_withheld" in ops

    def test_sanitizer_silent_when_nothing_withheld(self, monkeypatch):
        from kiro_crew.platform import governance as gov

        events: list[dict] = []
        monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: True)
        monkeypatch.setattr(
            gov,
            "sel",
            lambda: type("S", (), {"log_api_access": lambda _s, **k: events.append(k)})(),
        )
        config = {"allowedTools": ["@ok"]}
        gov.sanitize_agent_config_governance(config)
        assert config["allowedTools"] == ["@ok"]
        assert events == []


class TestCeilingFilteredSpecIsAudited:
    """Dropping an app manifest's autoApprove is a permission DECISION, so this
    fallback path must emit the same SEL withhold event the sanitizer does —
    otherwise it is the one revocation with no audit trail."""

    def test_withheld_autoapprove_emits_sel_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.agent as agent

        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: False)
        events: list[dict] = []

        class _FakeSel:
            def log_api_access(self, **kw: Any) -> None:
                events.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _FakeSel())
        out = agent._ceiling_filtered_spec("myapp:srv", {"autoApprove": ["t"], "url": "u"})
        assert "autoApprove" not in out
        assert any(e.get("operation") == "mcp_auto_approve_withheld" for e in events)

    def test_permitted_spec_is_untouched_and_unaudited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.agent as agent

        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: True)
        events: list[dict] = []

        class _FakeSel:
            def log_api_access(self, **kw: Any) -> None:
                events.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _FakeSel())
        out = agent._ceiling_filtered_spec("myapp:srv", {"autoApprove": ["t"], "url": "u"})
        assert out["autoApprove"] == ["t"]
        assert events == []
