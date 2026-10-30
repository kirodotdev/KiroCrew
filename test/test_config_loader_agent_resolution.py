"""How an agent name resolves to what answers a turn.

The multi-agent roster migration and resolver properties; the migration edge
cases (an empty roster, a legacy default agent, a missing workspace, an empty
name); and agents with no roster entry that still dispatch themselves rather than
the default agent: app and project-checkout agents materialized from spec files,
and crewmates bound by a spec file name. The materialized-agent snapshot's lookup,
refresh and off-loop scan are pinned with them.
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path

from config_loader_helpers import _load_from_dict, _safe_name_st, _store_name_st
from hypothesis import assume, given, settings
from hypothesis import strategies as st

import kiro_crew.config.loader as loader_module
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    MemoryStoreConfig,
    WorkspaceConfig,
    resolve_agent_bindings,
)
from kiro_crew.memory_stores import provision_member_memory
from kiro_crew.testing.wait import async_wait_until

# ---------------------------------------------------------------------------
# Phase 3: Multi-Agent Orchestration Property Tests (Task 1.5)
# ---------------------------------------------------------------------------


class TestMultiAgentOrchestrationProperties:
    """Property-based tests for multi-agent-orchestration config migration and resolver."""

    # Feature: multi-agent-orchestration, Property 1: Config load always produces at least one agent with valid default
    @given(
        config_shape=st.sampled_from(
            [
                "empty_object",
                "no_agents_key",
                "empty_agents_dict",
                "missing_default_agent",
                "valid_agents",
            ]
        ),
        legacy_default_agent=st.text(min_size=0, max_size=15),
    )
    @settings(deadline=None)
    def test_config_load_always_produces_agent_with_valid_default(
        self,
        config_shape: str,
        legacy_default_agent: str,
    ) -> None:
        """For any valid JSON config (including empty objects, configs with no
        agents key, configs with empty agents dict, and configs with missing
        default_agent), loading via KiroCrewConfig.load() shall produce a config
        where len(config.agents) >= 1 and config.default_agent names a key in
        config.agents.

        **Validates: Requirements 6.1, 6.2, 6.3, 6.6**
        """
        if config_shape == "empty_object":
            data: dict = {}
        elif config_shape == "no_agents_key":
            data = {"agent": {"default_agent": legacy_default_agent}}
        elif config_shape == "empty_agents_dict":
            data = {"agents": {}, "default_agent": ""}
        elif config_shape == "missing_default_agent":
            data = {
                "agents": {
                    "myagent": {
                        "kiro_agent": "kirocrew",
                        "workspace": "default",
                        "memory_store": "default",
                    }
                },
            }
        else:  # valid_agents
            data = {
                "agents": {
                    "coding": {
                        "kiro_agent": "kirocrew",
                        "workspace": "default",
                        "memory_store": "default",
                    }
                },
                "default_agent": "coding",
            }

        cfg = _load_from_dict(data)

        assert len(cfg.agents) >= 1, (
            f"Expected at least 1 agent, got {len(cfg.agents)} "
            f"for config_shape={config_shape!r}"
        )
        assert cfg.default_agent in cfg.agents, (
            f"default_agent={cfg.default_agent!r} not in agents={list(cfg.agents.keys())} "
            f"for config_shape={config_shape!r}"
        )

    # Feature: multi-agent-orchestration, Property 2: Legacy kiro_agent preserved in migrated default
    @given(
        legacy_kiro=st.text(
            alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
            min_size=1,
            max_size=20,
        ),
    )
    @settings(deadline=None)
    def test_legacy_kiro_agent_preserved_in_migrated_default(
        self,
        legacy_kiro: str,
    ) -> None:
        """For any config JSON with no agents section and a non-empty
        agent.default_agent value, loading shall produce a "default" agent
        whose kiro_agent field equals the legacy value.

        **Validates: Requirements 6.5**
        """
        data: dict = {
            "agent": {"default_agent": legacy_kiro},
        }
        cfg = _load_from_dict(data)

        assert "default" in cfg.agents, "Migration should create 'default' agent"
        assert cfg.agents["default"].kiro_agent == legacy_kiro, (
            f"Expected kiro_agent={legacy_kiro!r}, " f"got {cfg.agents['default'].kiro_agent!r}"
        )

    # Feature: multi-agent-orchestration, Property 3: Existing agents preserved on load
    @given(
        agents_data=st.dictionaries(
            keys=_safe_name_st,
            values=st.fixed_dictionaries(
                {
                    "kiro_agent": st.text(min_size=1, max_size=15),
                    "workspace": _safe_name_st,
                    "memory_store": _safe_name_st,
                },
            ),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(deadline=None)
    def test_existing_agents_preserved_on_load(
        self,
        agents_data: dict[str, dict[str, str]],
    ) -> None:
        """For any config JSON with a non-empty agents section, loading shall
        preserve all existing agent entries without creating additional agents.

        **Validates: Requirements 6.4**
        """
        first_name = next(iter(agents_data))
        data: dict = {
            "agents": agents_data,
            "default_agent": first_name,
        }
        cfg = _load_from_dict(data)

        # All original agents must be preserved
        for name, raw_entry in agents_data.items():
            assert name in cfg.agents, f"Agent '{name}' was lost during load"
            assert cfg.agents[name].kiro_agent == raw_entry["kiro_agent"]
            assert cfg.agents[name].workspace == raw_entry["workspace"]
            assert cfg.agents[name].memory_store == raw_entry["memory_store"]

        # No additional agents should be created
        assert set(cfg.agents.keys()) == set(agents_data.keys()), (
            f"Expected agents {set(agents_data.keys())}, " f"got {set(cfg.agents.keys())}"
        )

    # Feature: multi-agent-orchestration, Property 4: Backward compatibility — migrated default produces identical resolution
    @given(
        legacy_kiro=st.text(
            alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
            min_size=0,
            max_size=15,
        ),
        ws_dir=st.text(
            alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-/"),
            min_size=1,
            max_size=20,
        ),
        store_desc=st.text(min_size=0, max_size=15),
    )
    @settings(deadline=None)
    def test_backward_compat_migrated_default_produces_identical_resolution(
        self,
        legacy_kiro: str,
        ws_dir: str,
        store_desc: str,
    ) -> None:
        """For any legacy config JSON (no agents section), the ResolvedBindings
        produced by resolve_agent_bindings() on the loaded config shall have the
        same values as the previous legacy fallback path would have produced.

        **Validates: Requirements 7.4**
        """
        data: dict = {
            "agent": {"default_agent": legacy_kiro},
            "workspaces": {"default": {"dir": ws_dir}},
            "default_workspace": "default",
            "memory_stores": {"default": {"description": store_desc}},
            "default_memory_store": "default",
        }
        cfg = _load_from_dict(data)

        # The legacy fallback would have used:
        # - kiro_agent = agent.default_agent or "kirocrew"
        # - workspace = default_workspace → workspaces["default"].dir
        # - memory_store = default_memory_store
        expected_kiro = legacy_kiro if legacy_kiro else "kirocrew"

        result = resolve_agent_bindings(cfg)

        assert (
            result.kiro_agent == expected_kiro
        ), f"Expected kiro_agent={expected_kiro!r}, got {result.kiro_agent!r}"
        assert result.workspace_dir == Path(
            ws_dir
        ), f"Expected workspace_dir={ws_dir!r}, got {result.workspace_dir!r}"
        assert (
            result.memory_store_name == "default"
        ), f"Expected memory_store_name='default', got {result.memory_store_name!r}"

    # Feature: multi-agent-orchestration, Property 5: Agent resolution produces correct workspace and memory store
    # NOTE: This property is already covered by TestAgentWorkspaceBindingsProperties.test_resolver_correct_bindings
    # (Property 3 from agent-workspace-bindings spec). Adding a focused variant that validates
    # the multi-agent-orchestration requirements specifically.
    @given(
        agent_name=_safe_name_st,
        ws_name=_safe_name_st,
        store_name=_store_name_st,
        kiro_agent_name=st.text(min_size=1, max_size=15),
        ws_dir=st.text(min_size=1, max_size=20),
    )
    @settings(deadline=None)
    def test_agent_resolution_correct_workspace_and_memory_store(
        self,
        agent_name: str,
        ws_name: str,
        store_name: str,
        kiro_agent_name: str,
        ws_dir: str,
    ) -> None:
        """For any KiroCrewConfig with agents and a valid agent name, calling
        resolve_agent_bindings(config, agent_name) shall return correct
        workspace_dir and memory_store_name.

        **Validates: Requirements 1.1, 1.3, 2.1, 2.4**
        """
        config = KiroCrewConfig(
            agents={
                agent_name: KiroCrewAgentConfig(
                    kiro_agent=kiro_agent_name,
                    workspace=ws_name,
                    memory_store=store_name,
                ),
            },
            default_agent=agent_name,
            workspaces={ws_name: WorkspaceConfig(dir=ws_dir)},
            default_workspace=ws_name,
            memory_stores={store_name: MemoryStoreConfig()},
            default_memory_store=store_name,
        )

        expected_store = (
            "default" if agent_name == "default" else provision_member_memory(config, agent_name)
        )
        result = resolve_agent_bindings(config, agent_name=agent_name)

        assert result.workspace_dir == Path(ws_dir)
        assert result.memory_store_name == expected_store
        assert result.kiro_agent == kiro_agent_name

    # Feature: multi-agent-orchestration, Property 6: Non-KiroCrew agent names
    # resolve as the default TEMPLATE session, never as the default crewmate
    @given(
        default_name=_safe_name_st,
        unknown_name=_safe_name_st,
        kiro_agent_name=st.text(min_size=1, max_size=15),
        ws_dir=st.text(min_size=1, max_size=20),
        store_name=_safe_name_st,
    )
    @settings(deadline=None)
    def test_non_kirocrew_agent_names_resolve_as_the_default_template(
        self,
        default_name: str,
        unknown_name: str,
        kiro_agent_name: str,
        ws_dir: str,
        store_name: str,
    ) -> None:
        """For any agent name NOT in config.agents, resolve_agent_bindings
        returns the same bindings as no name at all: the default template on
        the default workspace and the default memory store, with
        ``requested_resolved`` False -- and NOT the default crewmate's
        bindings, whose template, store and model pin stay its own.

        **Validates: Requirements 1.2, 2.2, 7.2**
        """
        assume(unknown_name != default_name)
        assume(unknown_name != "kirocrew")

        config = KiroCrewConfig(
            agents={
                default_name: KiroCrewAgentConfig(
                    kiro_agent=kiro_agent_name,
                    workspace="default",
                    memory_store=store_name,
                    model="claude-opus-5",
                ),
            },
            default_agent=default_name,
            workspaces={"default": WorkspaceConfig(dir=ws_dir)},
            default_workspace="default",
            memory_stores={store_name: MemoryStoreConfig()},
            default_memory_store=store_name,
        )

        # Isolate from host ~/.kiro/agents/: pin the materialized-agent snapshot
        # as empty and ready so _materialized_kiro_agent never scans the host
        # filesystem. Without this, Hypothesis-generated names like "do" or
        # "architect" collide with real agent configs on macOS developer hosts.
        with unittest.mock.patch.object(loader_module, "_MATERIALIZED_AGENTS", frozenset()):
            with unittest.mock.patch.object(loader_module, "_MATERIALIZED_AGENTS_READY", True):
                result_unknown = resolve_agent_bindings(config, agent_name=unknown_name)
                result_plain = resolve_agent_bindings(config)

        assert result_unknown.requested_resolved is False
        assert result_plain.requested_resolved is True
        for result in (result_unknown, result_plain):
            assert result.workspace_dir == Path(ws_dir)
            assert result.memory_store_name == "default"
            assert result.kiro_agent == "kirocrew"
            assert result.selection_kind == "template"
            assert result.resolved_alias == ""
            assert result.model == ""
        assert result_unknown.effective_memory_config == result_plain.effective_memory_config


# ---------------------------------------------------------------------------
# Phase 3: Multi-Agent Orchestration Unit Tests (Task 1.6)
# ---------------------------------------------------------------------------


class TestMultiAgentMigrationEdgeCases:
    """Unit tests for config migration edge cases.

    **Validates: Requirements 6.1, 6.2, 6.5, 6.7, 1.4, 3.4**
    """

    def test_empty_config_creates_default_agent_and_persists(self) -> None:
        """Empty config → default agent created and persisted to disk.

        **Validates: Requirement 6.1**
        """
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
        ) as f:
            json.dump({}, f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch(
                "kiro_crew.config.loader.config_path",
                return_value=tmp,
            ):
                cfg = KiroCrewConfig.load()

                # In-memory: default agent exists
                assert "default" in cfg.agents
                assert cfg.default_agent == "default"
                assert cfg.agents["default"].kiro_agent == "kirocrew"
                assert cfg.agents["default"].workspace == "default"
                assert cfg.agents["default"].memory_store == "default"

                # On-disk: persisted via save()
                on_disk = json.loads(tmp.read_text(encoding="utf-8"))
                assert "agents" in on_disk
                assert "default" in on_disk["agents"]
                assert on_disk["default_agent"] == "default"
                assert on_disk["agents"]["default"]["kiro_agent"] == "kirocrew"
        finally:
            tmp.unlink(missing_ok=True)
            bak = tmp.with_suffix(".json.bak")
            bak.unlink(missing_ok=True)

    def test_empty_agents_dict_creates_default_agent(self) -> None:
        """Empty agents dict → default agent created.

        **Validates: Requirement 6.2**
        """
        data: dict = {"agents": {}, "default_agent": ""}
        cfg = _load_from_dict(data)

        assert "default" in cfg.agents
        assert cfg.default_agent == "default"
        assert len(cfg.agents) == 1
        assert cfg.agents["default"].kiro_agent == "kirocrew"

    def test_legacy_agent_default_agent_used_as_kiro_agent(self) -> None:
        """Legacy agent.default_agent value used as kiro_agent in migrated default.

        **Validates: Requirement 6.5**
        """
        data: dict = {
            "agent": {"default_agent": "oncall-agent"},
        }
        cfg = _load_from_dict(data)

        assert "default" in cfg.agents
        assert cfg.agents["default"].kiro_agent == "oncall-agent"

    def test_setup_writes_default_agent(self) -> None:
        """kirocrew setup creates config with default agent via
        _ensure_default_agent_in_config.

        **Validates: Requirement 6.7**
        """
        import tempfile

        from kiro_crew.cli_chat import _ensure_default_agent_in_config

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_config = Path(tmpdir) / "config.json"
            tmp_config.write_text("{}", encoding="utf-8")

            with (
                unittest.mock.patch(
                    "kiro_crew.config.loader.config_path",
                    return_value=tmp_config,
                ),
                unittest.mock.patch(
                    "kiro_crew.cli_chat.config_path",
                    return_value=tmp_config,
                ),
            ):
                _ensure_default_agent_in_config()

                on_disk = json.loads(tmp_config.read_text(encoding="utf-8"))
                assert "agents" in on_disk
                assert "default" in on_disk["agents"]
                assert on_disk["default_agent"] == "default"
                assert on_disk["agents"]["default"]["kiro_agent"] == "kirocrew"
                assert on_disk["agents"]["default"]["workspace"] == "default"
                assert on_disk["agents"]["default"]["memory_store"] == "default"

    def test_resolver_with_missing_workspace_falls_back(self) -> None:
        """Resolver with missing workspace falls back to default_workspace.

        **Validates: Requirement 1.4**
        """
        config = KiroCrewConfig(
            agents={
                "test": KiroCrewAgentConfig(
                    kiro_agent="kirocrew",
                    workspace="nonexistent",
                    memory_store="default",
                ),
            },
            default_agent="test",
            workspaces={"default": WorkspaceConfig(dir="my-fallback-dir")},
            default_workspace="default",
            memory_stores={"default": MemoryStoreConfig()},
            default_memory_store="default",
        )

        private_store = provision_member_memory(config, "test")
        result = resolve_agent_bindings(config, agent_name="test")

        # Falls back to default_workspace dir
        assert result.workspace_dir == Path("my-fallback-dir")
        assert result.memory_store_name == private_store

    def test_resolver_with_empty_agent_name_is_the_default_template(self) -> None:
        """Resolver with an empty agent name is the default TEMPLATE session.

        Never the default crewmate: its private store stays its own.

        **Validates: Requirement 3.4**
        """
        config = KiroCrewConfig(
            agents={
                "mydefault": KiroCrewAgentConfig(
                    kiro_agent="kirocrew",
                    workspace="default",
                    memory_store="default",
                ),
            },
            default_agent="mydefault",
            workspaces={"default": WorkspaceConfig(dir="ws-dir")},
            default_workspace="default",
            memory_stores={"default": MemoryStoreConfig()},
            default_memory_store="default",
        )

        private_store = provision_member_memory(config, "mydefault")

        for empty in ("", None):
            result = resolve_agent_bindings(config, agent_name=empty)
            assert result.kiro_agent == "kirocrew"
            assert result.workspace_dir == Path("ws-dir")
            assert result.memory_store_name == "default"
            assert result.selection_kind == "template"
            assert result.resolved_alias == ""
        # The crewmate itself still binds its private store.
        assert resolve_agent_bindings(config, "mydefault").memory_store_name == private_store


class TestAppAgentDispatch(unittest.TestCase):
    """An APP's agents are materialized into ``~/.kiro/agents/`` but are never
    added to ``config.agents``, so they must still dispatch THEMSELVES rather than
    silently falling back to the default agent (which left the slot advertising an
    agent that was not answering, without its MCP tools)."""

    def setUp(self):
        # The snapshot is a module global, so reset it around each test: a leaked
        # snapshot from a sibling test would be read instead of the tmpdir under
        # test. Left cold on purpose — these tests run synchronously (no event
        # loop), where the lookup is allowed to build it lazily.
        import kiro_crew.config.loader as loader

        loader._MATERIALIZED_AGENTS = frozenset()
        loader._MATERIALIZED_STEMS = {}
        loader._MATERIALIZED_SEEN.clear()
        loader._MATERIALIZED_LAST_DROPPED.clear()
        loader._MATERIALIZED_COMPLETE = False
        loader._MATERIALIZED_AGENTS_READY = False
        loader._MATERIALIZED_AGENTS_GENERATION = 0
        loader._MATERIALIZED_REFRESH_ISSUED = 0
        loader._MATERIALIZED_REFRESH_APPLIED = 0

    tearDown = setUp

    def test_crewmate_bound_to_a_file_name_dispatches_the_declared_agent(self):
        # A package installs `KiroPkg-captain.json` declaring "captain", and kiro-cli
        # lists only "captain". A crewmate that recorded the FILE name dispatches
        # the declared agent, and its row is not edited.
        import kiro_crew.config.loader as loader
        from kiro_crew.config.loader import KiroCrewAgentConfig

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"KiroPkg-captain.json": {"name": "captain"}})
            cfg = self._config()
            cfg.agents["KiroPkg-captain"] = KiroCrewAgentConfig(kiro_agent="KiroPkg-captain")
            cfg.default_agent = "KiroPkg-captain"
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                assert loader.resolve_agent_bindings(cfg, "KiroPkg-captain").kiro_agent == "captain"
                # No name is the default TEMPLATE, not this default crewmate.
                assert loader.resolve_agent_bindings(cfg).kiro_agent == "kirocrew"
                assert loader.resolve_agent_identity(cfg, "KiroPkg-captain")[1] == "captain"
        assert cfg.agents["KiroPkg-captain"].kiro_agent == "KiroPkg-captain"

    def test_subagent_paths_of_a_file_name_bound_crewmate_dispatch_the_declared_agent(self):
        # The subagent run hands ``execution.template_id`` to kiro-cli without
        # the resolver, so every builder of that field from a crewmate row must
        # map the file name: the member record itself, a resumed member
        # conversation, and a spawn inheriting the parent's member selection.
        from types import SimpleNamespace

        import kiro_crew.config.loader as loader
        from kiro_crew import execution_context as ec
        from kiro_crew.config.loader import KiroCrewAgentConfig
        from kiro_crew.subagent_manager.admission.gate import _GateMixin

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"KiroPkg-captain.json": {"name": "captain"}})
            cfg = self._config()
            cfg.agents["pkg"] = KiroCrewAgentConfig(kiro_agent="KiroPkg-captain")
            with (
                unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d),
                unittest.mock.patch.object(
                    loader.KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg)
                ),
            ):
                member = ec.resolve_member_execution(cfg, "pkg")
                assert member.template_id == "captain"
                # A record written before the mapping existed still names the file.
                stale = ec.ExecutionContext(
                    None,
                    ec.MemoryStoreRef("default"),
                    "member",
                    "KiroPkg-captain",
                    selection_name="pkg",
                )
                resumed = _GateMixin.resolve_spawn_execution(
                    SimpleNamespace(), conversation_key="subagent:run-1", _record=stale
                )
                assert resumed.template_id == "captain"
                inherited = _GateMixin.resolve_spawn_execution(
                    SimpleNamespace(),
                    parent_session_key="dashboard:chat",
                    _record=None,
                    _inherited_selection=("member", "pkg"),
                )
                assert (inherited.selection_name, inherited.template_id) == ("pkg", "captain")
                # The chat read of such a record dispatches the declared agent too.
                assert (
                    loader.resolve_agent_bindings(
                        cfg, "pkg", selection_kind="member", execution_context=stale
                    ).kiro_agent
                    == "captain"
                )
                from kiro_crew import session_agent_selection as sas

                with unittest.mock.patch.object(sas, "read_session_execution", lambda _k: stale):
                    session = sas.resolve_session_agent_bindings(
                        loader.resolve_agent_bindings, cfg, "dashboard:chat", None
                    )
                assert session.kiro_agent == "captain"

                # A TEMPLATE selection already names the exact provider agent.
                # Even when that id equals a global file stem, only a member
                # record is eligible for stem-to-declared-name repair.
                template = ec.ExecutionContext(
                    None,
                    ec.MemoryStoreRef("default"),
                    "template",
                    "KiroPkg-captain",
                    selection_name="KiroPkg-captain",
                )
                assert (
                    loader.resolve_agent_bindings(
                        cfg,
                        "KiroPkg-captain",
                        selection_kind="template",
                        execution_context=template,
                    ).kiro_agent
                    == "KiroPkg-captain"
                )
                with unittest.mock.patch.object(sas, "read_session_execution", lambda _k: template):
                    session = sas.resolve_session_agent_bindings(
                        loader.resolve_agent_bindings, cfg, "dashboard:chat", None
                    )
                assert session.kiro_agent == "KiroPkg-captain"
        assert cfg.agents["pkg"].kiro_agent == "KiroPkg-captain"

    def test_stem_mapping_never_overrides_a_declared_name(self):
        # Only a name kiro-cli does not know is mapped: a declared name, a stem two
        # configs share, and an unknown name all pass through unchanged.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(
                Path(td),
                {
                    "worker.json": {"name": "worker"},
                    "Pkg-worker.json": {"name": "other"},
                    "worker2.json": {"name": "Pkg-worker"},
                },
            )
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                assert loader.dispatch_kiro_agent("worker") == "worker"
                assert loader.dispatch_kiro_agent("Pkg-worker") == "Pkg-worker"
                assert loader.dispatch_kiro_agent("nope") == "nope"
                assert loader.dispatch_kiro_agent("") == ""

    def test_file_name_bound_crewmate_takes_the_background_role_effort(self):
        # The role set holds declared names; a crew that recorded a background
        # worker's FILE name runs that worker, so it takes the role effort too.
        import kiro_crew.config.loader as loader
        from kiro_crew.config.loader import KiroCrewAgentConfig

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"Pkg-kirocrew-lite.json": {"name": "kirocrew-lite"}})
            cfg = self._config()
            cfg.agent.reasoning_effort = "high"
            cfg.agent.role_efforts = {"background": "low"}
            cfg.agents["bg"] = KiroCrewAgentConfig(kiro_agent="Pkg-kirocrew-lite")
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                assert cfg.resolve_session_effort(None, "bg") == "low"
                # A pin on the crew still outranks the role effort.
                cfg.agents["bg"].reasoning_effort = "medium"
                assert cfg.resolve_session_effort(None, "bg") == "medium"

    def test_file_name_bound_crewmate_resolves_its_template_model_pin(self):
        # The model readers (_session_model, the spawn readout) need no mapping:
        # _resolve_named_agent_model already matches the file stem, so the file
        # name finds the pin the declared name finds, and the factory selection
        # both feed answers the same for either spelling.
        import kiro_crew.config.loader as loader
        from kiro_crew import subagent
        from kiro_crew.config.loader import KiroCrewAgentConfig
        from kiro_crew.session import _session_model

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(
                Path(td), {"KiroPkg-captain.json": {"name": "captain", "model": "claude-opus-4.8"}}
            )
            cfg = self._config()
            cfg.agents["pkg"] = KiroCrewAgentConfig(kiro_agent="KiroPkg-captain")
            with (
                unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d),
                unittest.mock.patch.object(
                    loader.KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg)
                ),
            ):
                by_stem = cfg._resolve_named_agent_model("KiroPkg-captain", agents_dir=d)
                assert by_stem == cfg._resolve_named_agent_model("captain", agents_dir=d)
                assert by_stem == "claude-opus-4.8"
                # The session's ladder reads the pin by the stem, as by the declared name.
                assert _session_model(cfg, None, crew_agent="pkg") == by_stem
                selected = cfg.acp_effective_model("captain", None)
                assert selected and cfg.acp_effective_model("KiroPkg-captain", None) == selected
                assert subagent._spawn_effective_model(None, "pkg") == selected

    def _config(self):
        from kiro_crew.config.loader import (
            KiroCrewAgentConfig,
            KiroCrewConfig,
            MemoryStoreConfig,
            WorkspaceConfig,
        )

        return KiroCrewConfig(
            agents={"default": KiroCrewAgentConfig(kiro_agent="kirocrew")},
            default_agent="default",
            workspaces={"default": WorkspaceConfig(dir="/tmp/ws")},
            default_workspace="default",
            memory_stores={"default": MemoryStoreConfig()},
            default_memory_store="default",
        )

    def _agents_dir(self, tmp: Path, files: dict[str, dict]) -> Path:
        d = tmp / "agents"
        d.mkdir(parents=True, exist_ok=True)
        for filename, body in files.items():
            (d / filename).write_text(json.dumps(body), encoding="utf-8")
        return d

    def test_app_agent_matched_by_name_field_dispatches_itself(self):
        # bridges._register_agents writes the NAMESPACED filename while the config
        # inside keeps the app's bare name, and app panels bind the slot to that
        # bare name. Before this fix the bare name was not a Kiro Crew alias, so it
        # fell through to default_agent and the DEFAULT agent answered.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"mochi--mochi.json": {"name": "mochi"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                r = loader.resolve_agent_bindings(self._config(), agent_name="mochi")
        assert r.kiro_agent == "mochi"

    def test_namespaced_filename_stem_is_not_dispatchable(self):
        # `kiro-cli agent list` enumerates agents by their DECLARED name: a config
        # at mochi--mochi.json with "name": "mochi" is listed as `mochi`, and
        # `mochi--mochi` is not listed at all. Trusting the stem would hand
        # kiro-cli a name it cannot resolve, and it would fall back to its own
        # default silently -- the invisible mismatch this change exists to remove.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"mochi--mochi.json": {"name": "mochi"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                cfg = self._config()
                assert loader.resolve_agent_bindings(cfg, "mochi").kiro_agent == "mochi"
                assert loader.resolve_agent_bindings(cfg, "mochi--mochi").kiro_agent == "kirocrew"

    def test_stem_is_used_when_no_name_is_declared(self):
        # With no `name` the stem is the only identifier, so it is trusted there.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"solo.json": {"description": "no name field"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                assert loader.resolve_agent_bindings(self._config(), "solo").kiro_agent == "solo"

    def test_genuinely_unknown_agent_still_falls_back_to_default(self):
        # Unchanged behavior: a name nothing declares must NOT be passed through
        # to kiro-cli.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"mochi--mochi.json": {"name": "mochi"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                r = loader.resolve_agent_bindings(self._config(), agent_name="nope-xyz")
        assert r.kiro_agent == "kirocrew"

    def test_alias_still_wins_over_materialized_file(self):
        # A Kiro Crew alias keeps full control of the binding (and the directory is
        # not even scanned for it).
        import kiro_crew.config.loader as loader
        from kiro_crew.config.loader import KiroCrewAgentConfig

        cfg = self._config()
        cfg.agents["mochi"] = KiroCrewAgentConfig(kiro_agent="explicitly-bound")
        private_store = provision_member_memory(cfg, "mochi")
        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"mochi--mochi.json": {"name": "mochi"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                r = loader.resolve_agent_bindings(cfg, agent_name="mochi")
        assert r.kiro_agent == "explicitly-bound"
        assert r.memory_store_name == private_store

    def test_non_object_json_in_agents_dir_is_skipped(self):
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "agents"
            d.mkdir(parents=True)
            (d / "junk.json").write_text("[1, 2, 3]", encoding="utf-8")
            (d / "broken.json").write_text("{not json", encoding="utf-8")
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                cfg = self._config()
                r = loader.resolve_agent_bindings(cfg, agent_name="mochi")
                assert r.kiro_agent == "kirocrew"
                # The FILENAME must not make an unparseable file dispatchable:
                # kiro-cli could not load it and would fall back to its own
                # default silently, which is the mismatch this change removes.
                for stem in ("junk", "broken"):
                    assert (
                        loader.resolve_agent_bindings(cfg, agent_name=stem).kiro_agent == "kirocrew"
                    )

    def test_lookup_does_no_filesystem_io(self):
        # This lookup runs on the gateway event loop (via _run_chat ->
        # resolve_agent_bindings) and an app agent takes it on EVERY turn, so it
        # must touch the filesystem zero times: no glob, no reads, not even a
        # stat. The snapshot is refreshed only off-loop.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"mochi--mochi.json": {"name": "mochi"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                loader.refresh_materialized_agents()
            # The directory is gone AND kiro_agents_dir is not patched, so
            # any filesystem access would change the answer. It must not.
            for _ in range(5):
                assert (
                    loader.resolve_agent_bindings(self._config(), agent_name="mochi").kiro_agent
                    == "mochi"
                )

    def test_registration_refresh_makes_a_new_agent_dispatchable(self):
        # Enabling an app writes its agent configs and then refreshes the
        # snapshot (bridges._register_agents), so a freshly registered app agent
        # dispatches immediately instead of after a gateway restart.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"other--other.json": {"name": "other"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                loader.refresh_materialized_agents()
                # Not registered yet -> falls back to the default.
                assert (
                    loader.resolve_agent_bindings(self._config(), agent_name="mochi").kiro_agent
                    == "kirocrew"
                )
                (d / "mochi--mochi.json").write_text(
                    json.dumps({"name": "mochi"}), encoding="utf-8"
                )
                # Writing alone is not enough — the writer must refresh.
                loader.refresh_materialized_agents()
                assert (
                    loader.resolve_agent_bindings(self._config(), agent_name="mochi").kiro_agent
                    == "mochi"
                )

    def test_scan_reads_through_the_sensitive_path_gate(self):
        # The agents dir is user-writable, so a symlink planted there
        # (`evil.json` -> `~/.aws/credentials`) must not be read by a boot refresh.
        # Reads go through hooks.safe_read_file, and a refused path is skipped
        # without taking the rest of the directory down with it.
        import kiro_crew.config.loader as loader
        import kiro_crew.hooks as hooks_mod

        real = hooks_mod.safe_read_file
        refused: list[str] = []

        def _guarded(path: str) -> str:
            if path.endswith("evil.json"):
                refused.append(path)
                raise PermissionError("sensitive path refused")
            return real(path)

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(
                Path(td),
                {"good.json": {"name": "good"}, "evil.json": {"name": "stolen"}},
            )
            with unittest.mock.patch.object(hooks_mod, "safe_read_file", _guarded):
                with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                    loader.refresh_materialized_agents()
                    cfg = self._config()
                    # The refused entry contributes NOTHING — not even its stem.
                    assert loader.resolve_agent_bindings(cfg, "stolen").kiro_agent == "kirocrew"
                    assert loader.resolve_agent_bindings(cfg, "evil").kiro_agent == "kirocrew"
                    # …and the rest of the directory still scans.
                    assert loader.resolve_agent_bindings(cfg, "good").kiro_agent == "good"
        assert refused, "the gate was never consulted for the planted entry"

    def test_a_failed_spec_read_keeps_the_agent_the_last_snapshot_had(self):
        # One transient read failure during a refresh must not drop a template
        # whose file is still on disk: its sessions would fail memory_unavailable
        # until the next complete refresh. An incomplete scan keeps what the last
        # snapshot declared.
        import kiro_crew.config.loader as loader
        import kiro_crew.hooks as hooks_mod

        real = hooks_mod.safe_read_file

        def _flaky(path: str) -> str:
            if path.endswith("lead.json"):
                raise OSError("transient read failure")
            return real(path)

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(
                Path(td),
                {"lead.json": {"name": "team-lead"}, "other.json": {"name": "other"}},
            )
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                loader.refresh_materialized_agents()
                assert loader.materialized_snapshot_complete() is True
                with unittest.mock.patch.object(hooks_mod, "safe_read_file", _flaky):
                    loader.refresh_materialized_agents()
                assert loader.materialized_snapshot_complete() is False
                cfg = self._config()
                kept = loader.resolve_agent_bindings(cfg, "team-lead", selection_kind="template")
                assert kept.kiro_agent == "team-lead"
                assert kept.requested_resolved is True
                # A complete scan afterwards is authoritative again: a file that
                # is really gone stops resolving.
                (d / "lead.json").unlink()
                loader.refresh_materialized_agents()
                assert loader.materialized_snapshot_complete() is True
                gone = loader.resolve_agent_bindings(cfg, "team-lead", selection_kind="template")
                assert gone.requested_resolved is False

    def test_an_incomplete_scan_keeps_the_stems_the_last_snapshot_had(self):
        # The stem map is a second half of the same snapshot: a crewmate bound to
        # a FILE name must not lose its mapping to one unreadable read either.
        import kiro_crew.config.loader as loader
        import kiro_crew.hooks as hooks_mod

        real = hooks_mod.safe_read_file

        def _flaky(path: str) -> str:
            if path.endswith("KiroPkg-captain.json"):
                raise OSError("transient read failure")
            return real(path)

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"KiroPkg-captain.json": {"name": "captain"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                loader.refresh_materialized_agents()
                assert loader.dispatch_kiro_agent("KiroPkg-captain") == "captain"
                with unittest.mock.patch.object(hooks_mod, "safe_read_file", _flaky):
                    loader.refresh_materialized_agents()
                assert loader.dispatch_kiro_agent("KiroPkg-captain") == "captain"

    def test_an_incomplete_scan_still_drops_an_agent_whose_file_was_removed(self):
        # Only what a SKIPPED spec declared is kept. A spec that never parses
        # makes every scan incomplete; an app disabled meanwhile must still
        # leave resolution, or its sessions would dispatch a config that is gone.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(
                Path(td),
                {"lead.json": {"name": "team-lead"}, "app--mochi.json": {"name": "mochi"}},
            )
            (d / "broken.json").write_text("{not json", encoding="utf-8")
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                loader.refresh_materialized_agents()
                cfg = self._config()
                assert loader.resolve_agent_bindings(cfg, "mochi").kiro_agent == "mochi"
                (d / "app--mochi.json").unlink()
                loader.refresh_materialized_agents()
                assert loader.materialized_snapshot_complete() is False
                assert loader.resolve_agent_bindings(cfg, "mochi").requested_resolved is False
                assert loader.resolve_agent_bindings(cfg, "team-lead").kiro_agent == "team-lead"

    def test_out_of_order_refresh_cannot_resurrect_a_deleted_agent(self):
        # Two scans race by COMPLETION order, not start order. An older scan that
        # globbed before a disable, finishing after the newer scan that saw the
        # removal, would reinstall the deleted name — and the next turn would hand
        # kiro-cli a config that is gone. The interleaving is driven exactly:
        # a newer refresh completes from inside the older one's scan.
        import kiro_crew.config.loader as loader

        def _older_scan_that_is_overtaken(_p):
            with unittest.mock.patch.object(
                loader,
                "_scan_materialized_index",
                lambda _p2: (frozenset({"kept"}), {}),
            ):
                loader.refresh_materialized_agents()  # newer refresh lands first
            # This older view still contains the agent that was deleted meanwhile.
            return frozenset({"kept", "deleted"}), {}

        with unittest.mock.patch.object(
            loader, "_scan_materialized_index", _older_scan_that_is_overtaken
        ):
            loader.refresh_materialized_agents()

        cfg = self._config()
        assert loader.resolve_agent_bindings(cfg, "kept").kiro_agent == "kept"
        assert loader.resolve_agent_bindings(cfg, "deleted").kiro_agent == "kirocrew"

    def test_an_unhonored_name_is_the_default_template_not_the_default_crewmate(self):
        # The old trap: storing the DEFAULT crewmate's physical `kiro_agent` when
        # a request is unhonored, which re-resolved as whatever alias carried
        # that name. An unhonored request is now the default TEMPLATE session
        #: no alias is reported, no crewmate's store or pin is taken,
        # and a same-named alias still resolves to its own target.
        import kiro_crew.config.loader as loader
        from kiro_crew.config.loader import KiroCrewAgentConfig

        cfg = self._config()
        cfg.agents["default"] = KiroCrewAgentConfig(kiro_agent="worker")
        cfg.agents["worker"] = KiroCrewAgentConfig(kiro_agent="other")
        cfg.agents["kirocrew"] = KiroCrewAgentConfig(kiro_agent="elsewhere")
        private_store = provision_member_memory(cfg, "worker")

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"unrelated.json": {"name": "unrelated"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                first = loader.resolve_agent_bindings(cfg, agent_name="does-not-exist")
                assert first.requested_resolved is False
                assert first.kiro_agent == "kirocrew"
                assert first.resolved_alias == ""
                assert first.selection_kind == "template"
                assert first.memory_store_name == "default"
                # A crewmate that happens to be NAMED like the default template is
                # still its own binding when picked -- the namespaces never cross.
                alias = loader.resolve_agent_bindings(cfg, agent_name="kirocrew")
                assert alias.kiro_agent == "elsewhere"
                assert alias.resolved_alias == "kirocrew"
                trap = loader.resolve_agent_bindings(cfg, agent_name="worker")
                assert trap.kiro_agent == "other"
                assert trap.memory_store_name == private_store

    def test_an_unattended_session_naming_no_agent_binds_global_not_the_default_crewmate_store(
        self,
    ):
        # A cron, channel or subagent session that names no agent is a plain
        # session: the default TEMPLATE on the default workspace and Global
        # memory. The roster's default crewmate may carry a private store of its own;
        # that store is reached only by the crewmate (its DM thread, or an
        # explicit pick), never inherited by a session that picked nobody. The
        # doctor line beside the binding (test_cli_doctor_config_sections) says so.
        import kiro_crew.config.loader as loader
        from kiro_crew.config.loader import KiroCrewAgentConfig

        cfg = self._config()
        cfg.agents["scout"] = KiroCrewAgentConfig(kiro_agent="kirocrew", model="pinned-model")
        cfg.default_agent = "scout"
        private_store = provision_member_memory(cfg, "scout")
        assert cfg.agents["scout"].memory_store == private_store

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"kirocrew.json": {"name": "kirocrew"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                for empty in ("", None):
                    plain = loader.resolve_agent_bindings(cfg, agent_name=empty)
                    assert plain.selection_kind == "template"
                    assert plain.resolved_alias == ""
                    assert plain.memory_store_name == "default"
                    assert plain.workspace_dir == Path("/tmp/ws")
                    assert plain.model == ""
                # Picking the crewmate by name is the one way to its store and pin.
                picked = loader.resolve_agent_bindings(cfg, agent_name="scout")
                assert picked.resolved_alias == "scout"
                assert picked.memory_store_name == private_store
                assert picked.model == "pinned-model"

    def test_stale_refresh_cannot_erase_a_published_agent(self):
        # The race: a refresh globs the directory BEFORE a registration writes into
        # it, the registration publishes, then the stale scan finishes and assigns.
        # A plain replace would drop the published name and un-dispatch a freshly
        # enabled app. Simulated by publishing from inside the scan.
        import kiro_crew.config.loader as loader

        def _scan_that_races(_p):
            # Stands in for "the directory as it looked before the write".
            loader.publish_materialized_agents({"mochi", "mochi--mochi"})
            return frozenset({"other", "other--other"}), {}

        with unittest.mock.patch.object(loader, "_scan_materialized_index", _scan_that_races):
            loader.refresh_materialized_agents()

        cfg = self._config()
        assert loader.resolve_agent_bindings(cfg, agent_name="mochi").kiro_agent == "mochi"
        # The scan's own findings survive too — the guard unions, it does not
        # discard the newer view.
        assert loader.resolve_agent_bindings(cfg, agent_name="other").kiro_agent == "other"

    def test_refresh_replaces_when_no_publish_intervened(self):
        # Without an intervening publish the refresh is authoritative, so removals
        # take effect — a union-always policy would make deleted agents immortal.
        import kiro_crew.config.loader as loader

        loader.publish_materialized_agents({"gone"})
        assert loader.resolve_agent_bindings(self._config(), agent_name="gone").kiro_agent == "gone"

        with unittest.mock.patch.object(
            loader, "_scan_materialized_index", lambda _p: (frozenset({"kept"}), {})
        ):
            loader.refresh_materialized_agents()

        cfg = self._config()
        assert loader.resolve_agent_bindings(cfg, agent_name="kept").kiro_agent == "kept"
        assert loader.resolve_agent_bindings(cfg, agent_name="gone").kiro_agent == "kirocrew"

    def test_publish_makes_names_dispatchable_with_no_filesystem_access(self):
        # _register_agents publishes what it just wrote BEFORE scheduling the
        # rescan, because a saturated executor can delay the rescan and a slot
        # created in that window is normalized to the default AND stored — the
        # slot would stay bound to the wrong agent. Publishing must therefore be
        # immediate and touch nothing on disk.
        import kiro_crew.config.loader as loader

        def _explode(_p):
            raise AssertionError("publish must not scan the filesystem")

        with unittest.mock.patch.object(loader, "_scan_materialized_agents", _explode):
            loader.publish_materialized_agents({"mochi", "mochi--mochi"})
            cfg = self._config()
            assert loader.resolve_agent_bindings(cfg, agent_name="mochi").kiro_agent == "mochi"
            assert (
                loader.resolve_agent_bindings(cfg, agent_name="mochi--mochi").kiro_agent
                == "mochi--mochi"
            )
            # Unrelated names are unaffected — publishing adds, never asserts
            # completeness.
            assert loader.resolve_agent_bindings(cfg, agent_name="nope").kiro_agent == "kirocrew"

    def test_publish_ignores_empty_input(self):
        import kiro_crew.config.loader as loader

        loader.publish_materialized_agents([])
        assert loader._MATERIALIZED_AGENTS_READY is False
        loader.publish_materialized_agents(["", None])  # type: ignore[list-item]
        assert loader._MATERIALIZED_AGENTS_READY is False

    def test_registration_refresh_is_offloaded_when_a_loop_is_running(self):
        # bridges._register_agents runs ON the event loop for the dashboard paths
        # (register_app documents this), so the writer-side refresh must NOT walk
        # every agent file inline there. Assert the scan executes on a different
        # thread than the loop.
        import asyncio
        import threading

        import kiro_crew.config.loader as loader

        scan_threads: list[int] = []
        real_scan = loader._scan_materialized_index

        def _recording_scan(p):
            scan_threads.append(threading.get_ident())
            return real_scan(p)

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"mochi--mochi.json": {"name": "mochi"}})

            async def _on_loop():
                loop_thread = threading.get_ident()
                loader.schedule_materialized_agents_refresh()
                # Wait for the executor to run the offloaded scan.
                await async_wait_until(
                    lambda: bool(scan_threads),
                    describe=lambda: "the offloaded scan never ran",
                )
                return loop_thread

            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                with unittest.mock.patch.object(
                    loader, "_scan_materialized_index", _recording_scan
                ):
                    loop_thread = asyncio.run(_on_loop())

        assert scan_threads, "the scheduled refresh never ran"
        assert loop_thread not in scan_threads, "the scan ran on the event loop thread"

    def test_registration_refresh_runs_inline_without_a_loop(self):
        # In a synchronous context (CLI, the boot warm already on an executor)
        # there is no loop to protect, so the refresh happens immediately.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"mochi--mochi.json": {"name": "mochi"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                loader.schedule_materialized_agents_refresh()
                assert "mochi" in loader._MATERIALIZED_AGENTS

    def test_cold_snapshot_on_the_event_loop_falls_back_instead_of_scanning(self):
        # With no snapshot yet, a lookup ON a running loop must NOT scan; it falls
        # back to the default for that turn. The boot warm normally precedes any
        # turn, so this is the safety net, not the expected path.
        import asyncio

        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"mochi--mochi.json": {"name": "mochi"}})

            async def _on_loop():
                return loader.resolve_agent_bindings(self._config(), agent_name="mochi")

            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                with unittest.mock.patch.object(loader, "_MATERIALIZED_AGENTS", frozenset()):
                    with unittest.mock.patch.object(loader, "_MATERIALIZED_AGENTS_READY", False):
                        assert asyncio.run(_on_loop()).kiro_agent == "kirocrew"

    def _project_dir(self, tmp: Path, files: dict[str, dict]) -> Path:
        d = tmp / "repo" / ".kiro" / "agents"
        d.mkdir(parents=True, exist_ok=True)
        for filename, body in files.items():
            (d / filename).write_text(json.dumps(body), encoding="utf-8")
        return tmp / "repo"

    def test_project_agent_dispatches_itself(self):
        # A project-local agent is resolvable by kiro-cli (Kiro Crew spawns it with
        # the project dir as cwd) but is not a Kiro Crew alias, so without the
        # project scope it fell through to default_agent and the DEFAULT agent
        # answered a session the user bound to the repo's own agent.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            agents = self._agents_dir(Path(td), {})
            proj = self._project_dir(Path(td), {"repobot.json": {"name": "repobot"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: agents):
                r = loader.resolve_agent_bindings(
                    self._config(), agent_name="repobot", project_dir=str(proj)
                )
        assert r.kiro_agent == "repobot"
        assert r.requested_resolved is True

    def test_project_agent_needs_the_project_dir_to_dispatch(self):
        # Without the project dir there is no second scope to search, so the same
        # agent must still fall back — the pre-existing contract for callers that
        # have no session context.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            agents = self._agents_dir(Path(td), {})
            self._project_dir(Path(td), {"repobot.json": {"name": "repobot"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: agents):
                r = loader.resolve_agent_bindings(self._config(), agent_name="repobot")
        assert r.kiro_agent == "kirocrew"
        assert r.requested_resolved is False

    def test_alias_still_wins_over_a_project_agent(self):
        # An explicit Kiro Crew alias is authored config; it must not be displaced by
        # a file that happens to share its name.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            agents = self._agents_dir(Path(td), {})
            proj = self._project_dir(Path(td), {"default.json": {"name": "default"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: agents):
                r = loader.resolve_agent_bindings(
                    self._config(), agent_name="default", project_dir=str(proj)
                )
        assert r.kiro_agent == "kirocrew"

    def test_unknown_name_still_falls_back_with_a_project_dir(self):
        # The project scope widens where a name can be found; it must not make an
        # unknown name dispatchable.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            agents = self._agents_dir(Path(td), {})
            proj = self._project_dir(Path(td), {"repobot.json": {"name": "repobot"}})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: agents):
                r = loader.resolve_agent_bindings(
                    self._config(), agent_name="nope", project_dir=str(proj)
                )
        assert r.kiro_agent == "kirocrew"
        assert r.requested_resolved is False

    def test_user_level_hit_does_no_project_filesystem_io(self):
        # The hot path must stay filesystem-free: a name already in the snapshot
        # must not trigger the project probe, even when a project dir is supplied.
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            d = self._agents_dir(Path(td), {"mochi--mochi.json": {"name": "mochi"}})
            proj = self._project_dir(Path(td), {})
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: d):
                loader.refresh_materialized_agents()
                with unittest.mock.patch.object(
                    loader, "_project_declares_agent", side_effect=AssertionError("probed")
                ) as probe:
                    r = loader.resolve_agent_bindings(
                        self._config(), agent_name="mochi", project_dir=str(proj)
                    )
                probe.assert_not_called()
        assert r.kiro_agent == "mochi"

    def test_project_lookup_reads_nothing_on_the_loop_with_a_cold_cache(self):
        # The project lookup reads a checkout, so on the loop it consults the cache
        # and nothing else. Cold cache => "not declared" => default agent for that
        # turn, mirroring the user-level cold-snapshot rule. A slow/network checkout
        # would otherwise stall every turn of a project-bound session.
        import asyncio

        import kiro_crew.agent_discovery as ad
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            agents = self._agents_dir(Path(td), {})
            proj = self._project_dir(Path(td), {"repobot.json": {"name": "repobot"}})
            ad.clear_project_agent_cache()

            async def _on_loop():
                return loader.resolve_agent_bindings(
                    self._config(), agent_name="repobot", project_dir=str(proj)
                )

            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: agents):
                with unittest.mock.patch.object(loader, "_MATERIALIZED_AGENTS_READY", True):
                    assert asyncio.run(_on_loop()).kiro_agent == "kirocrew"

    def test_warming_off_loop_lets_the_loop_resolve_a_project_agent(self):
        # The shape the dashboard call sites use: warm through the discovery pool,
        # then resolve inline. The on-loop read is then a cache HIT, so a
        # project-bound session dispatches its own agent on the very first turn.
        import asyncio

        import kiro_crew.agent_discovery as ad
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            agents = self._agents_dir(Path(td), {})
            proj = self._project_dir(Path(td), {"repobot.json": {"name": "repobot"}})
            ad.clear_project_agent_cache()

            async def _warm_then_resolve():
                await ad.warm_project_agent_names(str(proj))
                return loader.resolve_agent_bindings(
                    self._config(), agent_name="repobot", project_dir=str(proj)
                )

            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: agents):
                with unittest.mock.patch.object(loader, "_MATERIALIZED_AGENTS_READY", True):
                    assert asyncio.run(_warm_then_resolve()).kiro_agent == "repobot"

    def test_resolution_raises_synchronously_and_never_stopiteration(self):
        # resolve_agent_bindings raises on a malformed config, and callers rely
        # on catching it: _run_chat's `except Exception` logs and continues.
        #
        # The resolver MUST stay inline and only the cache warm is offloaded, and
        # what it raises must be deliverable through a Future: StopIteration is
        # not (an awaiting caller hangs on 3.10, or sees an unrelated RuntimeError
        # on newer runtimes). The defensive `next(iter(config.agents))` branch
        # that once raised it is gone -- an unresolvable name is the
        # default template, not "the first crewmate" -- and this pins that no
        # `next()` comes back.
        import kiro_crew.config.loader as loader

        with self.assertRaises(Exception) as caught:
            loader.resolve_agent_bindings(unittest.mock.MagicMock(), "anything", None)
        self.assertNotIsInstance(caught.exception, StopIteration)

    def test_legacy_spec_is_not_dispatchable(self):
        # kiro-cli cannot activate a name declared only in
        # <project>/.kiro/*.agent-spec.json, so resolution must NOT dispatch it --
        # otherwise the slot advertises an agent that fails at set_mode.
        import kiro_crew.agent_discovery as ad
        import kiro_crew.config.loader as loader

        with tempfile.TemporaryDirectory() as td:
            agents = self._agents_dir(Path(td), {})
            kiro = Path(td) / "repo" / ".kiro"
            kiro.mkdir(parents=True, exist_ok=True)
            (kiro / "legacy.agent-spec.json").write_text(json.dumps({}), encoding="utf-8")
            ad.clear_project_agent_cache()
            with unittest.mock.patch.object(loader, "kiro_agents_dir", lambda: agents):
                r = loader.resolve_agent_bindings(
                    self._config(), agent_name="legacy", project_dir=str(Path(td) / "repo")
                )
        assert r.kiro_agent == "kirocrew"
        assert r.requested_resolved is False
