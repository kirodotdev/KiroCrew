"""MCP Tool Search on the wire-settings channel (KAS).

kiro-cli's Rust engine takes Tool Search from the workspace ``cli.json`` overlay
and activates it only when the agent's ``tools`` also grants ``tool_search``.
KAS takes it from ``initialize``'s ``_meta.kiro.settings`` and defers every MCP
spec when told to, loader or no loader. These tests pin the client-side half of
that invariant: the setting is sent as true only when the spec grants the
loader, an explicit false otherwise, and nothing at all when no toggle value was
threaded in. The overlay file, meanwhile, is written only for the engine that
reads it.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.runtime import AcpRuntime, AcpToolSurfaceBindingError, AcpWorkspaceBindingError
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_CLIENT_META_SETTINGS,
    ACP_BACKENDS_KIRO_FAMILY,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_TOOL_SEARCH_OVERLAY,
    KAS_CLIENT_CAPABILITIES,
)
from kiro_crew.agent_sdk.tool_search import (
    TOOL_SEARCH_DEFAULT_MIN_PCT,
    TOOL_SEARCH_DEFAULT_MIN_TOKENS,
    ToolSearchSettings,
    kas_client_meta_settings,
    spec_grants_tool_search,
    with_client_meta_settings,
)
from kiro_crew.providers.acp import AcpProvider

# ── the grant predicate ──


class TestSpecGrantsToolSearch:
    @pytest.mark.parametrize(
        "tools",
        [
            ["fs_read", "tool_search", "@srv"],
            ["tool_search"],
            "*",
            ["fs_read", "*"],
        ],
    )
    def test_granting_shapes(self, tools):
        assert spec_grants_tool_search({"tools": tools}) is True

    @pytest.mark.parametrize(
        "tools",
        [
            ["fs_read", "@srv"],
            [],
            None,
            "tool_search",  # a bare string is not the list form; only "*" is special
            ["tool_search/"],
            [{"name": "tool_search"}],
        ],
    )
    def test_non_granting_shapes(self, tools):
        assert spec_grants_tool_search({"tools": tools}) is False

    def test_no_spec_grants_nothing(self):
        assert spec_grants_tool_search(None) is False
        assert spec_grants_tool_search({}) is False

    def test_the_builtin_group_grants_the_loader(self):
        assert spec_grants_tool_search({"tools": ["@builtin", "@srv"]}) is True

    @pytest.mark.parametrize(
        "spec",
        [
            {"tools": "*", "excludedTools": ["tool_search"]},
            {"tools": ["*"], "excludedTools": ["tool_search"]},
            {"tools": ["@builtin"], "excludedTools": ["tool_search"]},
            {"tools": ["fs_read", "tool_search"], "excludedTools": ["tool_search"]},
            {"tools": "*", "excludedTools": ["*"]},
        ],
    )
    def test_an_exclusion_wins_over_any_grant(self, spec):
        """A wildcard grant minus the loader is no loader: the engine applies
        ``excludedTools`` after ``tools``, so the gate must too."""
        assert spec_grants_tool_search(spec) is False

    @pytest.mark.parametrize(
        "excluded",
        [["fs_write"], [], None, "tool_search", [{"name": "tool_search"}]],
    )
    def test_an_exclusion_that_does_not_name_the_loader_leaves_the_grant(self, excluded):
        assert spec_grants_tool_search({"tools": "*", "excludedTools": excluded}) is True


# ── the payload ──


class TestKasClientMetaSettings:
    def test_no_toggle_leaves_the_channel_silent(self):
        assert kas_client_meta_settings(None, loader_granted=True) == {}

    def test_enabled_with_loader_sends_true(self):
        out = kas_client_meta_settings(ToolSearchSettings(True, 5, 50_000), loader_granted=True)
        assert out == {"toolSearch": {"enabled": True, "minPct": 5, "minTokens": 50_000}}

    def test_enabled_without_loader_sends_an_explicit_false(self):
        """THE regression: deferral on for a spec that cannot load a deferred tool.

        On KAS that pairing leaves every MCP tool unreachable for the session, so
        the client must turn the setting off itself -- and say so explicitly rather
        than omit the key, because an omitted key is the host's default to fill.
        """
        out = kas_client_meta_settings(ToolSearchSettings(True, 5, 50_000), loader_granted=False)
        assert out["toolSearch"]["enabled"] is False
        assert "enabled" in out["toolSearch"]

    def test_disabled_sends_false_regardless_of_grant(self):
        out = kas_client_meta_settings(ToolSearchSettings(False), loader_granted=True)
        assert out["toolSearch"]["enabled"] is False

    def test_from_config_clamps_like_the_overlay(self):
        s = ToolSearchSettings.from_config(True, 250, -3)
        assert (s.min_pct, s.min_tokens) == (100, 0)
        s = ToolSearchSettings.from_config(True, "junk", None)
        assert (s.min_pct, s.min_tokens) == (
            TOOL_SEARCH_DEFAULT_MIN_PCT,
            TOOL_SEARCH_DEFAULT_MIN_TOKENS,
        )


class TestKasNeverDefer:
    """KAS defers every MCP tool not on ``toolSearch.neverDefer``, whatever
    ``enabled`` says, so a spec without the loader must list its tools there."""

    TOOLS = {
        "kirocrew-core": ["memory_recall", "skill_search", "skill_fetch"],
        "example-app:example-mcp": ["read_state", "write_state"],
    }

    def _ids(self, spec):
        from kiro_crew.agent_sdk.tool_search import kas_never_defer_ids

        return kas_never_defer_ids(spec, self.TOOLS.get)

    def test_a_named_tool_is_spelled_the_way_kas_matches_it(self):
        spec = {"tools": ["fs_read", "@example-app:example-mcp/read_state"]}
        assert self._ids(spec) == ["example-app:example-mcp::read_state"]

    def test_a_whole_server_grant_expands_from_the_known_list(self):
        assert self._ids({"tools": ["@kirocrew-core"]}) == [
            "kirocrew-core::memory_recall",
            "kirocrew-core::skill_fetch",
            "kirocrew-core::skill_search",
        ]

    def test_a_tool_glob_expands_to_its_matches(self):
        assert self._ids({"tools": ["@kirocrew-core/skill_*"]}) == [
            "kirocrew-core::skill_fetch",
            "kirocrew-core::skill_search",
        ]

    def test_a_server_with_no_known_list_contributes_nothing(self):
        assert self._ids({"tools": ["@unprobed", "@unprobed/x*"]}) == []

    def test_the_wildcard_stands_for_every_declared_server(self):
        spec = {
            "tools": ["*"],
            "excludedTools": ["tool_search"],
            "mcpServers": {"example-app:example-mcp": {}, "unprobed": {}},
        }
        assert self._ids(spec) == [
            "example-app:example-mcp::read_state",
            "example-app:example-mcp::write_state",
        ]

    @pytest.mark.parametrize(
        "spec", [None, {}, {"tools": None}, {"tools": ["@builtin", "fs_read", "@", 3]}]
    )
    def test_no_mcp_grant_lists_nothing(self, spec):
        assert self._ids(spec) == []

    def test_without_the_loader_the_payload_carries_the_list(self):
        out = kas_client_meta_settings(
            ToolSearchSettings(True, 5, 50_000),
            loader_granted=False,
            never_defer=["b::y", "a::x", "a::x"],
        )
        assert out["toolSearch"]["neverDefer"] == ["a::x", "b::y"]
        assert out["toolSearch"]["enabled"] is False

    def test_without_the_loader_an_empty_list_is_still_sent(self):
        out = kas_client_meta_settings(ToolSearchSettings(False), loader_granted=False)
        assert out["toolSearch"]["neverDefer"] == []

    def test_with_the_loader_no_list_is_sent(self):
        """The spec-grants-tool_search path keeps deferring exactly as before."""
        out = kas_client_meta_settings(
            ToolSearchSettings(True, 5, 50_000), loader_granted=True, never_defer=["a::x"]
        )
        assert "neverDefer" not in out["toolSearch"]


class TestHandshakeNeverDefer:
    """The KAS ``initialize`` request built from the projected spawn spec."""

    def _caps(self, monkeypatch, spec):
        from kiro_crew.acp import kas_secret_storage, runtime_spawn_launch

        monkeypatch.setattr(kas_secret_storage, "persistence_enabled", lambda: False)
        monkeypatch.setattr(
            runtime_spawn_launch,
            "_known_server_tool_names",
            {"kirocrew-core": ["memory_recall", "skill_search"]}.get,
            raising=False,
        )
        rt = AcpRuntime(acp_backend=ACP_BACKEND_KAS)
        rt._tool_search = ToolSearchSettings(True, 5, 50_000)
        rt._projected_spawn_spec = lambda: spec
        caps = asyncio.run(runtime_spawn_launch._handshake_client_capabilities(rt))
        return caps["_meta"]["kiro"]["settings"]["toolSearch"]

    def test_a_spec_without_the_loader_keeps_its_mcp_tools_direct(self, monkeypatch):
        wire = self._caps(monkeypatch, {"tools": ["@kirocrew-core", "@srv/one"]})
        assert wire["enabled"] is False
        assert wire["neverDefer"] == [
            "kirocrew-core::memory_recall",
            "kirocrew-core::skill_search",
            "srv::one",
        ]

    def test_a_spec_with_the_loader_still_defers(self, monkeypatch):
        wire = self._caps(monkeypatch, {"tools": ["tool_search", "@kirocrew-core"]})
        assert wire == {"enabled": True, "minPct": 5, "minTokens": 50_000}

    def test_an_unreadable_spec_lists_nothing(self, monkeypatch):
        wire = self._caps(monkeypatch, None)
        assert wire["enabled"] is False
        assert wire["neverDefer"] == []


class TestKnownServerToolNames:
    def test_a_crew_server_is_never_imported_into_the_gateway(self, monkeypatch):
        """Its declaration module sits in an agent-writable package, so the
        handshake reads the probe cache and imports nothing."""
        from kiro_crew import mcp_discovery
        from kiro_crew.acp.runtime_spawn_launch import _known_server_tool_names

        reads: list[str] = []

        def record(name, *_a, **_k):
            reads.append(name)
            return ["declared_tool"]

        # Through the facade only: it is the one import path the handshake has.
        monkeypatch.setattr(mcp_discovery, "_managed_tools_in_process", record)
        monkeypatch.setattr(
            mcp_discovery, "_get_cached", lambda name: ("unknown", [], "", 0.0, "handshake")
        )
        assert _known_server_tool_names("kirocrew-guide") is None
        assert reads == []

    def test_an_unknown_server_answers_none(self):
        from kiro_crew.acp.runtime_spawn_launch import _known_server_tool_names

        assert _known_server_tool_names("no-such-server-for-this-test") is None

    def test_a_third_party_server_answers_from_the_probe_cache(self, monkeypatch):
        from kiro_crew import mcp_discovery
        from kiro_crew.acp.runtime_spawn_launch import _known_server_tool_names

        monkeypatch.setattr(
            mcp_discovery,
            "_get_cached",
            lambda name: ("outdated", ["a", "b"], "", 0.0, "handshake"),
        )
        assert _known_server_tool_names("third-party") == ["a", "b"]


class TestWithClientMetaSettings:
    def test_fills_the_channel_and_keeps_the_declaration(self):
        out = with_client_meta_settings(KAS_CLIENT_CAPABILITIES, {"toolSearch": {"enabled": True}})
        assert out["_meta"]["kiro"]["settings"] == {"toolSearch": {"enabled": True}}
        assert {k: v for k, v in out.items() if k != "_meta"} == {
            k: v for k, v in KAS_CLIENT_CAPABILITIES.items() if k != "_meta"
        }

    def test_never_mutates_the_shared_constant(self):
        before = json.dumps(KAS_CLIENT_CAPABILITIES, sort_keys=True)
        with_client_meta_settings(KAS_CLIENT_CAPABILITIES, {"toolSearch": {"enabled": True}})
        assert json.dumps(KAS_CLIENT_CAPABILITIES, sort_keys=True) == before
        assert KAS_CLIENT_CAPABILITIES["_meta"]["kiro"]["settings"] == {}

    def test_empty_settings_is_an_equal_copy(self):
        out = with_client_meta_settings(KAS_CLIENT_CAPABILITIES, {})
        assert out == KAS_CLIENT_CAPABILITIES
        assert out is not KAS_CLIENT_CAPABILITIES

    def test_existing_settings_survive_and_new_keys_win(self):
        base = {"_meta": {"kiro": {"settings": {"a": 1, "toolSearch": {"enabled": False}}}}}
        out = with_client_meta_settings(base, {"toolSearch": {"enabled": True}})
        assert out["_meta"]["kiro"]["settings"] == {"a": 1, "toolSearch": {"enabled": True}}


# ── the membership sets (harness-parity H6) ──


class TestMembership:
    def test_the_two_channels_are_disjoint_and_known(self):
        assert ACP_BACKENDS_TOOL_SEARCH_OVERLAY <= ACP_BACKENDS_KNOWN
        assert ACP_BACKENDS_CLIENT_META_SETTINGS <= ACP_BACKENDS_KNOWN
        assert not (ACP_BACKENDS_TOOL_SEARCH_OVERLAY & ACP_BACKENDS_CLIENT_META_SETTINGS)

    def test_the_overlay_is_narrower_than_the_kiro_family(self):
        """KAS is in the kiro family but never reads the Tool Search overlay."""
        assert ACP_BACKENDS_TOOL_SEARCH_OVERLAY < ACP_BACKENDS_KIRO_FAMILY
        assert ACP_BACKEND_KIRO in ACP_BACKENDS_TOOL_SEARCH_OVERLAY
        assert ACP_BACKEND_KAS not in ACP_BACKENDS_TOOL_SEARCH_OVERLAY
        assert ACP_BACKEND_KAS in ACP_BACKENDS_CLIENT_META_SETTINGS

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
    def test_every_harness_answers_from_the_table(self, backend):
        try:
            harness = harness_for(backend)
        except ValueError:
            pytest.skip("no shared-process harness for this backend")
        assert harness.client_meta_settings is (backend in ACP_BACKENDS_CLIENT_META_SETTINGS)


# ── the provider side ──


def _build_provider(backend: str, **kwargs) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend, **kwargs)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


class TestProviderChannels:
    def test_kas_does_not_write_the_overlay_file(self, tmp_path):
        provider = _build_provider(ACP_BACKEND_KAS)
        provider._client._work_dir = tmp_path
        provider._tool_search = True
        provider._apply_tool_search_overlay()
        assert not (tmp_path / ".kiro" / "settings" / "cli.json").exists()

    def test_kiro_still_writes_the_overlay_file(self, tmp_path):
        provider = _build_provider(ACP_BACKEND_KIRO)
        provider._client._work_dir = tmp_path
        provider._tool_search = True
        provider._apply_tool_search_overlay()
        data = json.loads((tmp_path / ".kiro" / "settings" / "cli.json").read_text())
        assert data["toolSearch.enabled"] is True

    def test_resolved_settings_carry_the_configured_values(self):
        provider = _build_provider(ACP_BACKEND_KAS, tool_search=True)
        provider._tool_search_min_pct = 12
        provider._tool_search_min_tokens = 4000
        assert provider._tool_search_settings() == ToolSearchSettings(True, 12, 4000)

    def test_no_toggle_resolves_to_none(self):
        provider = _build_provider(ACP_BACKEND_KAS)
        provider._tool_search = None
        assert provider._tool_search_settings() is None

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX])
    def test_the_runtime_is_handed_the_resolved_settings(self, backend, monkeypatch):
        """Whatever the backend, the runtime gets the operator's choice; IT decides
        per host whether the handshake carries it."""
        captured: dict = {}

        class _Runtime:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                raise _Stop

        class _Stop(Exception):
            pass

        monkeypatch.setattr("kiro_crew.providers.acp.AcpRuntime", _Runtime)
        provider = _build_provider(backend, tool_search=True)
        provider._client._work_dir = MagicMock()
        provider._client._agent = "kirocrew"
        provider._client._resume_session_id = ""

        async def run():
            with pytest.raises(_Stop):
                await provider._start_kiro_runtime_impl({}, {})

        asyncio.run(run())
        assert captured.get("tool_search") == ToolSearchSettings(
            True, TOOL_SEARCH_DEFAULT_MIN_PCT, TOOL_SEARCH_DEFAULT_MIN_TOKENS
        )

    def test_the_resume_respawn_carries_the_same_settings(self):
        """A runtime that dies during resume is respawned WITH the wire settings.

        On KAS the setting travels only through this constructor argument, so a
        fallback spawn without it would run the replayed session with Tool Search
        silently off for its whole life.
        """
        provider = _build_provider(ACP_BACKEND_KAS, tool_search=True)
        provider._client._work_dir = "/tmp/ws"
        provider._client._agent = "kirocrew"
        provider._client._sandbox_mode = "auto"
        provider._client._extra_env = {}
        provider._client._mcp_gateway_overlay = None
        provider._client._mcp_gateway_socket = None
        provider._client._model = "auto"
        provider._client._resume_session_id = "old-sess"

        dead = MagicMock()
        dead.pid = 1
        dead.spawn = AsyncMock()
        dead.is_alive = MagicMock(return_value=False)
        dead.kill = AsyncMock()
        dead.load_session = AsyncMock(side_effect=RuntimeError("load failed"))
        dead.saw_not_logged_in = MagicMock(return_value=False)
        fresh_handle = MagicMock()
        fresh_handle.session_id = "fresh"
        fresh_handle.set_model = AsyncMock()
        fresh_handle.store_session_config = MagicMock()
        fresh = MagicMock()
        fresh.pid = 2
        fresh.spawn = AsyncMock()
        fresh.is_alive = MagicMock(return_value=True)
        fresh.create_session = AsyncMock(return_value=fresh_handle)
        fresh.saw_not_logged_in = MagicMock(return_value=False)
        runtimes = iter([dead, fresh])
        constructions: list[dict] = []

        def build(**kw):
            constructions.append(kw)
            return next(runtimes)

        with (
            patch("kiro_crew.providers.acp.AcpRuntime", side_effect=build),
            patch(
                "kiro_crew.providers.acp.AcpSessionProvider",
                side_effect=lambda handle, runtime, **kw: MagicMock(
                    _handle=handle, _runtime=runtime, resumed=False
                ),
            ),
            patch("pathlib.Path.exists", return_value=True),
        ):
            asyncio.run(provider._start_kiro_runtime())

        assert len(constructions) == 2, "the resume fallback did not respawn"
        expected = ToolSearchSettings(
            True, TOOL_SEARCH_DEFAULT_MIN_PCT, TOOL_SEARCH_DEFAULT_MIN_TOKENS
        )
        assert [c.get("tool_search") for c in constructions] == [expected, expected]


class TestEveryKasCapableConstructorThreadsTheSetting:
    def test_companion_runtime_kwargs_mirror_the_parent_setting(self):
        from kiro_crew.session_allocation import _collect_parent_runtime_kwargs

        settings = ToolSearchSettings(True, 5, 50_000)
        provider = MagicMock()
        provider._client = MagicMock(
            _sandbox_mode="auto",
            _extra_env={},
            _mcp_gateway_overlay=None,
            _mcp_gateway_socket=None,
            backend=ACP_BACKEND_KAS,
        )
        provider.client = provider._client
        provider.tool_search_settings = settings
        owner = MagicMock()
        owner.get_provider = MagicMock(return_value=provider)
        assert _collect_parent_runtime_kwargs(owner, "parent")["tool_search"] == settings

    def test_the_capability_is_declared_on_the_provider_contract(self):
        """H14: read off ``LLMProvider``, never probed for a private name. The base
        answers None; the ACP provider answers its resolved settings."""
        from kiro_crew.providers.base import LLMProvider

        assert "tool_search_settings" in vars(LLMProvider)
        assert isinstance(vars(LLMProvider)["tool_search_settings"], property)
        provider = _build_provider(ACP_BACKEND_KAS, tool_search=True)
        assert provider.tool_search_settings == ToolSearchSettings(
            True, TOOL_SEARCH_DEFAULT_MIN_PCT, TOOL_SEARCH_DEFAULT_MIN_TOKENS
        )
        provider._tool_search = None
        assert provider.tool_search_settings is None

    def test_companion_runtime_kwargs_stay_silent_without_a_toggle(self):
        from kiro_crew.session_allocation import _collect_parent_runtime_kwargs

        provider = MagicMock()
        provider._client = MagicMock(backend=ACP_BACKEND_KAS)
        provider.client = provider._client
        provider.tool_search_settings = None
        owner = MagicMock()
        owner.get_provider = MagicMock(return_value=provider)
        assert "tool_search" not in _collect_parent_runtime_kwargs(owner, "parent")

    def test_the_background_runtime_is_built_with_the_setting(self):
        """Source-level pin: the background runtime constructor names the kwarg,
        derived from the same ``agent.tool_search*`` config as the foreground."""
        import inspect

        from kiro_crew import session_background

        source = inspect.getsource(session_background)
        assert "tool_search=ToolSearchSettings.from_config(" in source


class TestProjectedSpawnSpecRead:
    def test_the_managed_default_is_materialized_before_it_is_read(self, monkeypatch):
        """A checkout that skipped setup has no managed spec yet; reading it as
        absent would decide "no loader" for the process while the projection,
        moments later, materializes a spec that grants one."""
        from kiro_crew.acp import kas_agents as kas_agents_mod
        from kiro_crew.acp import runtime as runtime_mod

        calls: list[str] = []
        monkeypatch.setattr(
            runtime_mod,
            "ensure_agent_materialized",
            lambda agent: calls.append(f"materialize:{agent}"),
        )
        monkeypatch.setattr(
            kas_agents_mod,
            "load_agent_spec",
            lambda agents_dir, agent: (calls.append(f"load:{agent}"), {"tools": ["tool_search"]})[
                1
            ],
        )
        rt = object.__new__(AcpRuntime)
        rt._agent = "kirocrew"
        rt._derived_spec_snapshot = None
        assert rt._projected_spawn_spec() == {"tools": ["tool_search"]}
        assert calls == ["materialize:kirocrew", "load:kirocrew"]

    def test_a_derived_snapshot_is_used_without_any_read(self, monkeypatch):
        from types import SimpleNamespace

        from kiro_crew.acp import kas_agents as kas_agents_mod
        from kiro_crew.acp import runtime as runtime_mod

        monkeypatch.setattr(
            runtime_mod,
            "ensure_agent_materialized",
            lambda agent: (_ for _ in ()).throw(AssertionError("must not materialize")),
        )
        monkeypatch.setattr(
            kas_agents_mod,
            "load_agent_spec",
            lambda *a: (_ for _ in ()).throw(AssertionError("must not read")),
        )
        rt = object.__new__(AcpRuntime)
        rt._agent = "kirocrew-worker"
        rt._derived_spec_snapshot = SimpleNamespace(spec={"tools": ["fs_read"]})
        assert rt._projected_spawn_spec() == {"tools": ["fs_read"]}

    def test_an_unreadable_spec_grants_nothing(self, monkeypatch):
        from kiro_crew.acp import kas_agents as kas_agents_mod
        from kiro_crew.acp import runtime as runtime_mod

        monkeypatch.setattr(runtime_mod, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(
            kas_agents_mod,
            "load_agent_spec",
            lambda *a: (_ for _ in ()).throw(FileNotFoundError("gone")),
        )
        rt = object.__new__(AcpRuntime)
        rt._agent = "kirocrew"
        rt._derived_spec_snapshot = None
        assert rt._projected_spawn_spec() is None
        assert spec_grants_tool_search(None) is False


# ── the process-wide decision vs a later session's agent ──


def _bare_runtime(*, agent: str, wire: dict) -> AcpRuntime:
    rt = object.__new__(AcpRuntime)
    rt._agent = agent
    rt._tool_search_wire = wire
    return rt


class TestLoaderReachabilityOnSharedProcess:
    ON = {"toolSearch": {"enabled": True, "minPct": 5, "minTokens": 50_000}}
    OFF = {"toolSearch": {"enabled": False, "minPct": 5, "minTokens": 50_000}}

    @pytest.mark.parametrize(
        ("wire", "agents", "refused"),
        [
            pytest.param(
                ON,
                [{"id": "worker", "tools": ["fs_read"]}],
                True,
                id="a_different_agent_without_the_loader_is_refused",
            ),
            pytest.param(
                ON,
                [{"id": "worker", "tools": ["tool_search"]}],
                False,
                id="a_different_agent_with_the_loader_passes",
            ),
            pytest.param(
                OFF,
                [{"id": "worker", "tools": []}],
                False,
                id="nothing_is_refused_when_deferral_is_off_or_unsent-off",
            ),
            pytest.param(
                {},
                [{"id": "worker", "tools": []}],
                False,
                id="nothing_is_refused_when_deferral_is_off_or_unsent-unsent",
            ),
            pytest.param(ON, None, False, id="a_host_with_no_projection_is_never_refused"),
        ],
    )
    def test_a_later_agent_on_the_shared_process(self, wire, agents, refused):
        rt = _bare_runtime(agent="kirocrew", wire=wire)
        if refused:
            with pytest.raises(AcpToolSurfaceBindingError, match="worker"):
                rt._refuse_if_loader_unreachable("worker", agents)
        else:
            assert rt._refuse_if_loader_unreachable("worker", agents) is None

    def test_the_refusal_is_the_binding_error_the_run_runtime_caller_handles(self):
        """Callers that already fall back to a dedicated runtime on a workspace
        binding refusal must take this refusal the same way."""
        assert issubclass(AcpToolSurfaceBindingError, AcpWorkspaceBindingError)

    def test_the_spawn_agent_is_judged_on_its_projection_too(self):
        """The process decision was made from the spec as it stood at spawn; a later
        projection of the SAME agent that has since lost the grant would run
        deferred with no loader, so the name buys no bypass."""
        rt = _bare_runtime(agent="kirocrew", wire=self.ON)
        with pytest.raises(AcpToolSurfaceBindingError):
            rt._refuse_if_loader_unreachable("kirocrew", [{"id": "kirocrew", "tools": []}])
        # The unchanged spec, as projected (possibly widened), passes.
        rt._refuse_if_loader_unreachable(
            "kirocrew", [{"id": "kirocrew", "tools": ["tool_search", "@kirocrew-dashboard"]}]
        )

    def test_the_projection_seam_itself_refuses_so_both_start_paths_are_covered(self):
        """``create_session`` and ``load_session`` both take their payload from
        ``_kas_custom_agents``; judging it there is what makes a resumed session
        on a deferral-enabled process as safe as a new one."""
        from kiro_crew.acp.harness.base import SessionExtras

        rt = _bare_runtime(agent="kirocrew", wire=self.ON)
        rt._mcp_gateway_overlay = None
        rt._work_dir = None
        harness = MagicMock()
        harness.session_extras = AsyncMock(
            return_value=SessionExtras(custom_agents=[{"id": "worker", "tools": ["fs_read"]}])
        )
        rt._harness_resolved = harness
        with pytest.raises(AcpToolSurfaceBindingError):
            asyncio.run(rt._kas_custom_agents("worker"))
        harness.session_extras = AsyncMock(
            return_value=SessionExtras(custom_agents=[{"id": "worker", "tools": ["tool_search"]}])
        )
        extras = asyncio.run(rt._kas_custom_agents("worker"))
        assert extras.custom_agents == [{"id": "worker", "tools": ["tool_search"]}]
