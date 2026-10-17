"""Agents, workspaces and memory stores: parsing, migration and the bindings resolver.

Property tests for the agent-workspace bindings (field metadata, the workspaces
migration, serialization, memory-store merging, ``resolve_agent_bindings`` and its
fallbacks, kiro-agent reference warnings, workspace paths), the memory-store
binding floor and resource independence. The bindings edge cases also pin
``memory.history_max_days``, the dashboard's ``recent_tint_count``,
``folder_sort``, ``session_sort`` and ``update_nudge``, and the embedding-provider
coercion. The
``agent`` section's ``subagent_cwd_allowed_roots`` and each agent's ``triggers``
are here too.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import tempfile
import unittest.mock
from pathlib import Path

import pytest
from config_loader_helpers import (
    _kirocrew_config_st,
    _load_from_dict,
    _safe_name_st,
    _store_name_st,
)
from hypothesis import assume, given, settings
from hypothesis import strategies as st

import kiro_crew.config.loader as loader_module
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    MemoryStoreConfig,
    ResolvedBindings,
    WorkspaceConfig,
    _migrate_workspaces,
    config_dir,
    resolve_agent_bindings,
    resolve_memory_store_config,
    validate_kiro_agent_references,
    workspace_dir_for,
)
from kiro_crew.memory_stores import UnknownMemoryStore, provision_member_memory

# Logger used by the loader module — needed for capturing warnings in tests
logger = logging.getLogger("kiro_crew.config.loader")


# ---------------------------------------------------------------------------
# Phase 2: Agent-Workspace Bindings Property Tests
# ---------------------------------------------------------------------------


class TestAgentWorkspaceBindingsProperties:
    """Property-based tests for Phase 2 agent-workspace-bindings."""

    # Feature: agent-workspace-bindings, Property 1: New dataclass metadata completeness
    @given(
        cls_idx=st.integers(min_value=0, max_value=2),
    )
    @settings(deadline=None)
    def test_new_dataclass_metadata_completeness(
        self,
        cls_idx: int,
    ) -> None:
        """All fields of KiroCrewAgentConfig, WorkspaceConfig, and
        MemoryStoreConfig carry required metadata (label, help).

        **Validates: Requirements 1.1, 3.1, 5.1**
        """
        import dataclasses

        classes = [KiroCrewAgentConfig, WorkspaceConfig, MemoryStoreConfig]
        cls = classes[cls_idx]

        fields = dataclasses.fields(cls)
        assert len(fields) > 0, f"{cls.__name__} has no fields"

        for f in fields:
            meta = dict(f.metadata) if f.metadata else {}
            assert "label" in meta, f"{cls.__name__}.{f.name} missing 'label' in metadata"
            assert isinstance(meta["label"], str), f"{cls.__name__}.{f.name} label must be str"
            assert len(meta["label"]) > 0, f"{cls.__name__}.{f.name} label must not be empty"
            assert "help" in meta, f"{cls.__name__}.{f.name} missing 'help' in metadata"
            assert isinstance(meta["help"], str), f"{cls.__name__}.{f.name} help must be str"
            assert len(meta["help"]) > 0, f"{cls.__name__}.{f.name} help must not be empty"

    # Feature: agent-workspace-bindings, Property 5: Workspace migration preserves directory paths
    @given(
        raw_workspaces=st.dictionaries(
            keys=_safe_name_st,
            values=st.one_of(
                # Flat string format (legacy)
                st.text(min_size=1, max_size=30),
                # Structured dict format (new)
                st.builds(
                    lambda d: {"dir": d},
                    d=st.text(min_size=1, max_size=30),
                ),
            ),
            min_size=0,
            max_size=5,
        ),
    )
    @settings(deadline=None)
    def test_workspace_migration_preserves_directory_paths(
        self,
        raw_workspaces: dict,
    ) -> None:
        """For any workspace dict mixing flat strings and structured
        {"dir": str}, _migrate_workspaces produces WorkspaceConfig
        instances whose dir matches the original value.

        **Validates: Requirements 4.1, 4.2, 4.4**
        """
        result = _migrate_workspaces(raw_workspaces)

        if not raw_workspaces:
            # Empty input → default entry
            assert "default" in result
            assert result["default"].dir == "workspace"
        else:
            for name, value in raw_workspaces.items():
                assert name in result
                assert isinstance(result[name], WorkspaceConfig)
                if isinstance(value, str):
                    assert result[name].dir == value
                elif isinstance(value, dict):
                    assert result[name].dir == value.get("dir", "workspace")

    # Feature: agent-workspace-bindings, Property 10: Config serialization round-trip
    @given(config=_kirocrew_config_st)
    @settings(deadline=None)
    def test_config_serialization_round_trip(
        self,
        config: KiroCrewConfig,
    ) -> None:
        """For any valid KiroCrewConfig with agents/workspaces/stores,
        to_dict() → load() produces an equivalent instance.

        **Validates: Requirements 9.4, 11.5**
        """
        d = config.to_dict()
        loaded = _load_from_dict(d)

        # Compare agents — migration may add a "default" agent if none exist
        if config.agents:
            # Existing agents are preserved; migration may add "default"
            for name in config.agents:
                assert name in loaded.agents
                assert loaded.agents[name].kiro_agent == config.agents[name].kiro_agent
                assert loaded.agents[name].workspace == config.agents[name].workspace
                assert loaded.agents[name].memory_store == config.agents[name].memory_store
        else:
            # Empty agents → migration creates "default" agent
            assert "default" in loaded.agents
            assert len(loaded.agents) >= 1

        # Compare default_agent — migration may fix invalid values
        if config.default_agent and config.default_agent in loaded.agents:
            assert loaded.default_agent == config.default_agent
        else:
            # Migration fixes invalid/empty default_agent
            assert loaded.default_agent in loaded.agents

        # Compare workspaces
        if config.workspaces:
            assert set(loaded.workspaces.keys()) == set(config.workspaces.keys())
            for name in config.workspaces:
                assert loaded.workspaces[name].dir == config.workspaces[name].dir
        else:
            # Empty workspaces → default entry synthesized by _migrate_workspaces
            assert "default" in loaded.workspaces
            assert loaded.workspaces["default"].dir == "workspace"

        # Compare memory_stores
        if config.memory_stores:
            assert set(loaded.memory_stores.keys()) == set(config.memory_stores.keys())
            for name in config.memory_stores:
                assert (
                    loaded.memory_stores[name].description == config.memory_stores[name].description
                )
                assert (
                    loaded.memory_stores[name].embedding_provider
                    == config.memory_stores[name].embedding_provider
                )
        else:
            # Empty memory_stores → default entry synthesized
            assert "default" in loaded.memory_stores

        # Compare default_memory_store
        assert loaded.default_memory_store == config.default_memory_store

        # Compare core fields still round-trip
        assert loaded.agent.approval_mode == config.agent.approval_mode
        assert loaded.agent.provider == config.agent.provider
        assert loaded.session.timeout_secs == config.session.timeout_secs
        assert loaded.memory.embedding_provider == config.memory.embedding_provider
        assert loaded.default_workspace == config.default_workspace
        assert loaded.auto_update == config.auto_update

    # Feature: agent-workspace-bindings, Property 11: Serialization format correctness
    @pytest.mark.skipif(platform.system() == "Darwin", reason="Hypothesis flaky on macOS CI")
    @given(config=_kirocrew_config_st)
    @settings(deadline=None)
    def test_serialization_format_correctness(
        self,
        config: KiroCrewConfig,
    ) -> None:
        """For any config, to_dict() output has agents as dict-of-dicts,
        workspaces values as dicts with dir key, memory_stores as
        dict-of-dicts.

        **Validates: Requirements 11.1, 11.2, 11.3, 11.4**
        """
        d = config.to_dict()

        # agents is a dict of dicts with expected keys
        assert isinstance(d["agents"], dict)
        for name, agent_dict in d["agents"].items():
            assert isinstance(agent_dict, dict)
            assert "kiro_agent" in agent_dict
            assert "workspace" in agent_dict
            assert "memory_store" in agent_dict

        # workspaces values are dicts with "dir" key
        assert isinstance(d["workspaces"], dict)
        for name, ws_dict in d["workspaces"].items():
            assert isinstance(ws_dict, dict)
            assert "dir" in ws_dict

        # memory_stores is a dict of dicts with expected keys
        assert isinstance(d["memory_stores"], dict)
        for name, ms_dict in d["memory_stores"].items():
            assert isinstance(ms_dict, dict)
            assert "description" in ms_dict
            assert "embedding_provider" in ms_dict

        # default_agent and default_memory_store are present
        assert "default_agent" in d
        assert isinstance(d["default_agent"], str)
        assert "default_memory_store" in d
        assert isinstance(d["default_memory_store"], str)

    # Feature: agent-workspace-bindings, Property 6: Memory store merge correctness
    @given(
        top_level=st.fixed_dictionaries(
            {},
            optional={
                "embedding_provider": st.sampled_from(["llama_cpp"]),
                "embedding_dim": st.sampled_from([256, 512, 1024]),
                "semantic_confidence_threshold": st.floats(min_value=0.0, max_value=1.0),
                "episodic_dedup_threshold": st.floats(min_value=0.0, max_value=1.0),
                "episodic_max_results": st.integers(min_value=1, max_value=50),
                "history_max_days": st.integers(min_value=1, max_value=365),
                "migrated": st.booleans(),
            },
        ),
        store_overrides=st.fixed_dictionaries(
            {},
            optional={
                "description": st.text(min_size=0, max_size=30),
                "embedding_provider": st.sampled_from(["", "none", "llama_cpp"]),
                "embedding_dim": st.one_of(st.just(None), st.sampled_from([256, 512, 1024])),
            },
        ),
    )
    @settings(deadline=None)
    def test_memory_store_merge_correctness(
        self,
        top_level: dict,
        store_overrides: dict,
    ) -> None:
        """For any top-level memory dict and partial store override dict,
        resolve_memory_store_config produces a merged dict where
        store-level values override and unspecified fields inherit from
        top-level.

        **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
        """
        merged = resolve_memory_store_config(top_level, store_overrides)

        # Req 6.2: Unspecified fields inherit from top-level
        for key, value in top_level.items():
            if key not in store_overrides:
                assert merged[key] == value, (
                    f"Key '{key}' should inherit from top-level "
                    f"(expected {value!r}, got {merged.get(key)!r})"
                )

        # Req 6.3: Explicit non-empty, non-None store values override
        for key, value in store_overrides.items():
            if key == "description":
                # description is store-only metadata, must not appear in merged
                assert key not in merged or merged.get(key) == top_level.get(
                    key
                ), "'description' should be skipped during merge"
                continue
            if value != "" and value is not None:
                assert merged[key] == value, (
                    f"Key '{key}' should be overridden by store "
                    f"(expected {value!r}, got {merged.get(key)!r})"
                )

        # Req 6.4: Empty string and None values do not override
        for key, value in store_overrides.items():
            if key == "description":
                continue
            if value == "" or value is None:
                # Should inherit from top-level (or not be present if not in top-level)
                if key in top_level:
                    assert merged[key] == top_level[key], (
                        f"Key '{key}' with empty/None value should inherit from top-level "
                        f"(expected {top_level[key]!r}, got {merged.get(key)!r})"
                    )

        # Original top_level dict must not be mutated
        assert merged is not top_level

    # Feature: agent-workspace-bindings, Property 3: Resolver correct bindings
    @given(
        agent_name=_safe_name_st,
        ws_name=_safe_name_st,
        store_name=_store_name_st,
        kiro_agent_name=st.text(min_size=1, max_size=20),
        ws_dir=st.text(min_size=1, max_size=30),
        store_desc=st.text(min_size=0, max_size=20),
        store_provider=st.sampled_from(["", "none", "llama_cpp"]),
    )
    @settings(deadline=None)
    def test_resolver_correct_bindings(
        self,
        agent_name: str,
        ws_name: str,
        store_name: str,
        kiro_agent_name: str,
        ws_dir: str,
        store_desc: str,
        store_provider: str,
    ) -> None:
        """For configs with valid agent→workspace→memory_store chains,
        resolve_agent_bindings returns the correct workspace dir and
        memory store name.

        **Validates: Requirements 7.1, 7.2, 7.5**
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
            memory_stores={
                store_name: MemoryStoreConfig(
                    description=store_desc,
                    embedding_provider=store_provider,
                )
            },
            default_memory_store=store_name,
        )

        expected_store = (
            "default" if agent_name == "default" else provision_member_memory(config, agent_name)
        )

        # Resolve via explicit agent_name
        result = resolve_agent_bindings(config, agent_name=agent_name)
        assert isinstance(result, ResolvedBindings)
        assert result.workspace_dir == Path(ws_dir)
        assert result.memory_store_name == expected_store
        assert result.kiro_agent == kiro_agent_name

        # No agent_name: a TEMPLATE session on the default workspace and the
        # default memory store, never the default crewmate's bindings.
        result2 = resolve_agent_bindings(config)
        assert result2.workspace_dir == Path(ws_dir)
        assert result2.memory_store_name == "default"
        assert result2.kiro_agent == "kirocrew"
        assert result2.selection_kind == "template"
        assert result2.resolved_alias == ""
        assert result2.model == ""

    # Feature: agent-workspace-bindings, Property 4: Resolver fallback on missing references
    @given(
        agent_name=_safe_name_st,
        missing_ws=_safe_name_st,
        missing_store=_safe_name_st,
        fallback_ws_name=_safe_name_st,
        fallback_store_name=_store_name_st,
        fallback_ws_dir=st.text(min_size=1, max_size=30),
    )
    @settings(deadline=None)
    def test_resolver_fallback_on_missing_references(
        self,
        agent_name: str,
        missing_ws: str,
        missing_store: str,
        fallback_ws_name: str,
        fallback_store_name: str,
        fallback_ws_dir: str,
    ) -> None:
        """Workspace fallback never repairs an invalid member memory binding."""
        # Ensure the agent references names that do NOT exist in the maps
        assume(missing_ws != fallback_ws_name)
        assume(missing_store != fallback_store_name)

        config = KiroCrewConfig(
            agents={
                agent_name: KiroCrewAgentConfig(
                    kiro_agent="some-agent",
                    workspace=missing_ws,
                    memory_store=missing_store,
                ),
            },
            default_agent=agent_name,
            workspaces={fallback_ws_name: WorkspaceConfig(dir=fallback_ws_dir)},
            default_workspace=fallback_ws_name,
            memory_stores={fallback_store_name: MemoryStoreConfig()},
            default_memory_store=fallback_store_name,
        )

        if agent_name != "default":
            with pytest.raises(UnknownMemoryStore):
                resolve_agent_bindings(config, agent_name=agent_name)
        expected_store = (
            "default" if agent_name == "default" else provision_member_memory(config, agent_name)
        )
        result = resolve_agent_bindings(config, agent_name=agent_name)

        # Should fall back to default_workspace dir
        assert result.workspace_dir == Path(fallback_ws_dir)
        assert result.memory_store_name == expected_store

    # Feature: agent-workspace-bindings, Property 8: Kiro agent validation warnings
    @given(
        agents_data=st.dictionaries(
            keys=_safe_name_st,
            values=st.builds(
                KiroCrewAgentConfig,
                kiro_agent=st.text(min_size=0, max_size=20),
                workspace=st.just("default"),
                memory_store=st.just("default"),
            ),
            min_size=0,
            max_size=5,
        ),
        installed=st.lists(
            st.text(min_size=1, max_size=20),
            min_size=0,
            max_size=5,
        ),
    )
    @settings(deadline=None)
    def test_kiro_agent_validation_warnings(
        self,
        agents_data: dict[str, KiroCrewAgentConfig],
        installed: list[str],
    ) -> None:
        """For configs with kiro_agent values and mock installed agent
        lists, validate_kiro_agent_references logs warnings for
        unresolved references and never raises.

        **Validates: Requirements 8.1, 8.2, 8.3**
        """
        config = KiroCrewConfig(agents=agents_data)
        installed_set = set(installed)

        # Capture warnings
        log_messages: list[str] = []

        def capture_warning(msg: object, *args: object) -> None:
            try:
                log_messages.append(str(msg) % args)
            except Exception:
                log_messages.append(str(msg))

        with unittest.mock.patch.object(logger, "warning", capture_warning):
            # Must never raise
            validate_kiro_agent_references(config, installed)

        # Check that warnings were logged for unresolved references
        for mc_name, mc_agent in agents_data.items():
            if mc_agent.kiro_agent and mc_agent.kiro_agent not in installed_set:
                # Should have a warning mentioning this agent
                matching = [m for m in log_messages if mc_name in m and mc_agent.kiro_agent in m]
                assert len(matching) > 0, (
                    f"Expected warning for agent '{mc_name}' referencing "
                    f"'{mc_agent.kiro_agent}', got: {log_messages}"
                )

        # Agents with empty kiro_agent or matching installed agents should NOT warn
        for mc_name, mc_agent in agents_data.items():
            if not mc_agent.kiro_agent or mc_agent.kiro_agent in installed_set:
                # Use precise prefix to avoid substring false positives
                prefix = f"KiroCrew agent '{mc_name}' references"  # brand-ok: loader log text
                matching = [m for m in log_messages if prefix in m]
                assert len(matching) == 0, (
                    f"Unexpected warning for agent '{mc_name}' with "
                    f"kiro_agent='{mc_agent.kiro_agent}': {log_messages}"
                )

    # Feature: agent-workspace-bindings, Property 7: Workspace path resolution
    @pytest.mark.skipif(
        os.name == "nt",
        reason=(
            "known Windows gap that additionally cannot be EXECUTED there: this is a "
            "hypothesis property test with deadline=None, so its example budget "
            "outruns the shards' per-test --timeout, and on Windows pytest-timeout "
            "has no SIGALRM to interrupt with -- it kills the process, taking the "
            "whole session with it (measured: the run died at 22% with no summary). "
            "It is skipped HERE rather than tracked in windows-expected-failures.txt, "
            "because that list is now executed as a strict xfail and a hanging entry "
            "in it is a shard-killer."
        ),
    )
    @given(
        ws_name=_safe_name_st,
        path_kind=st.sampled_from(["absolute_slash", "absolute_tilde", "relative"]),
        rel_segment=st.text(
            alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
            min_size=1,
            max_size=20,
        ),
    )
    @settings(deadline=None)
    def test_workspace_path_resolution(
        self,
        ws_name: str,
        path_kind: str,
        rel_segment: str,
    ) -> None:
        """For absolute paths (``/...``, ``~/...``) the resolved path is
        absolute; for relative paths the resolved path is under
        ``config_dir()``.

        **Validates: Requirements 3.4**
        """
        if path_kind == "absolute_slash":
            dir_value = f"/tmp/ws-{rel_segment}"
        elif path_kind == "absolute_tilde":
            dir_value = f"~/ws-{rel_segment}"
        else:
            dir_value = rel_segment

        # Build a raw config dict with the structured workspace format
        raw_config = {
            "default_workspace": ws_name,
            "workspaces": {ws_name: {"dir": dir_value}},
        }

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
        ) as f:
            json.dump(raw_config, f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch(
                "kiro_crew.config.loader.config_path",
                return_value=tmp,
            ):
                result = workspace_dir_for(ws_name)
        finally:
            tmp.unlink(missing_ok=True)

        if path_kind == "absolute_slash":
            assert result.is_absolute(), (
                f"Absolute path '{dir_value}' should resolve to absolute, " f"got '{result}'"
            )
            assert str(result) == dir_value
        elif path_kind == "absolute_tilde":
            assert result.is_absolute(), (
                f"Tilde path '{dir_value}' should resolve to absolute, " f"got '{result}'"
            )
            # expanduser resolves ~ to home dir
            assert str(result) == str(Path(dir_value).expanduser())
        else:
            # Relative path should be under config_dir()
            assert result.is_absolute(), (
                f"Relative path should be resolved to absolute via config_dir(), " f"got '{result}'"
            )
            assert str(result) == str(config_dir() / dir_value)

    # Feature: agent-workspace-bindings, Property 2: Agents parsing with duplicate kiro_agent values
    @given(
        agents_data=st.dictionaries(
            keys=_safe_name_st,
            values=st.fixed_dictionaries(
                {
                    "kiro_agent": st.sampled_from(["kirocrew", "oncall-agent", "custom", ""]),
                    "workspace": _safe_name_st,
                    "memory_store": _safe_name_st,
                },
            ),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(deadline=None)
    def test_agents_parsing_with_duplicate_kiro_agent_values(
        self,
        agents_data: dict[str, dict[str, str]],
    ) -> None:
        """For any agents dict with optional duplicate kiro_agent values,
        load() parses all entries without error.

        **Validates: Requirements 1.3, 1.7**
        """
        raw_config: dict = {"agents": agents_data}
        cfg = _load_from_dict(raw_config)

        # All agent entries must be parsed
        assert set(cfg.agents.keys()) == set(agents_data.keys())

        for name, raw_entry in agents_data.items():
            parsed = cfg.agents[name]
            assert isinstance(parsed, KiroCrewAgentConfig)
            assert parsed.kiro_agent == raw_entry["kiro_agent"]
            assert parsed.workspace == raw_entry["workspace"]
            assert parsed.memory_store == raw_entry["memory_store"]

        # Verify duplicate kiro_agent values are accepted (no error)
        kiro_values = [e["kiro_agent"] for e in agents_data.values()]
        if len(set(kiro_values)) < len(kiro_values):
            # Duplicates exist — config still loaded fine
            assert len(cfg.agents) == len(agents_data)

    # Feature: agent-workspace-bindings, Property 9: Backward compatibility with legacy configs
    @given(
        legacy_default_agent=st.text(min_size=0, max_size=20),
        flat_workspaces=st.dictionaries(
            keys=_safe_name_st,
            values=st.text(min_size=1, max_size=30),
            min_size=0,
            max_size=3,
        ),
        embedding_provider=st.sampled_from(["llama_cpp"]),
    )
    @settings(deadline=None)
    def test_backward_compatibility_with_legacy_configs(
        self,
        legacy_default_agent: str,
        flat_workspaces: dict[str, str],
        embedding_provider: str,
    ) -> None:
        """For legacy configs (no agents, no top-level default_agent,
        flat workspaces), load() migrates to include a default agent
        using agent.default_agent as kiro agent name.

        **Validates: Requirements 9.1, 9.2, 9.3, 9.5**
        """
        raw_config: dict = {
            "agent": {"default_agent": legacy_default_agent},
            "memory": {"embedding_provider": embedding_provider},
        }
        if flat_workspaces:
            raw_config["workspaces"] = flat_workspaces

        cfg = _load_from_dict(raw_config)

        # After migration: default agent created from legacy config
        assert isinstance(cfg.agents, dict)
        assert len(cfg.agents) >= 1
        assert "default" in cfg.agents
        assert cfg.default_agent == "default"

        # Req 9.5: agent.default_agent is preserved as kiro agent name
        # in the migrated default agent
        expected_kiro = legacy_default_agent if legacy_default_agent else "kirocrew"
        assert cfg.agents["default"].kiro_agent == expected_kiro

        # Req 9.2: Flat workspaces auto-migrated to structured format
        for ws_name, ws_dir in flat_workspaces.items():
            assert isinstance(cfg.workspaces[ws_name], WorkspaceConfig)
            assert cfg.workspaces[ws_name].dir == ws_dir

        # Always has at least one workspace (default synthesized if empty)
        assert len(cfg.workspaces) >= 1

        # Req 9.3: No memory_stores → default synthesized
        assert "default" in cfg.memory_stores
        assert isinstance(cfg.memory_stores["default"], MemoryStoreConfig)

        # Resolve bindings → uses migrated default agent
        result = resolve_agent_bindings(cfg)
        assert result.kiro_agent == expected_kiro

    def test_flat_workspace_strings_migrate_and_write_back(self, tmp_path: Path) -> None:
        """Flat ``workspaces`` strings load as their dirs and persist as ``{"dir": ...}``."""
        home = tmp_path / "home"
        home.mkdir()
        cfg_file = home / "config.json"
        cfg_file.write_text(
            json.dumps({"workspaces": {"default": "~/ws", "other": "/abs"}}),
            encoding="utf-8",
        )
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=home),
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
        ):
            cfg = KiroCrewConfig.load()

        assert cfg.workspaces == {
            "default": WorkspaceConfig(dir="~/ws"),
            "other": WorkspaceConfig(dir="/abs"),
        }
        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert on_disk["workspaces"] == {"default": {"dir": "~/ws"}, "other": {"dir": "/abs"}}


class TestMemoryStoreBindingFloor:
    """Legacy bindings stay exact, and explicit V2 selection creates a new store."""

    @pytest.mark.parametrize(
        "bound",
        ["default", "", None],
        ids=["explicit-default", "empty-string", "key-absent"],
    )
    def test_default_assistant_retains_v1_without_repairing_member_bindings(self, bound) -> None:
        # ``None`` stands for the key being absent from the crew's config entry: the
        # field's own default is the floor, and this pins that the two agree.
        assert KiroCrewAgentConfig().memory_store == "default"
        agent_kwargs: dict = {"kiro_agent": "kirocrew", "workspace": "default"}
        if bound is not None:
            agent_kwargs["memory_store"] = bound

        config = KiroCrewConfig(
            agents={
                "default": KiroCrewAgentConfig(**agent_kwargs),
                "floor": KiroCrewAgentConfig(**agent_kwargs),
                "chose": KiroCrewAgentConfig(kiro_agent="kirocrew", memory_store="work"),
                "broken": KiroCrewAgentConfig(kiro_agent="kirocrew", memory_store="gone"),
            },
            default_agent="floor",
            workspaces={"default": WorkspaceConfig(dir="workspace")},
            default_workspace="default",
            # The operator's table names ONLY other stores, and ``default_memory_store``
            # is one of them. This is the exact config shape that relocated every crew.
            memory_stores={"work": MemoryStoreConfig(), "email": MemoryStoreConfig()},
            default_memory_store="work",
        )
        assert "default" not in config.memory_stores, "the floor must not be declared here"

        assert resolve_agent_bindings(config, agent_name="default").memory_store_name == "default"
        with pytest.raises(UnknownMemoryStore, match="is unavailable; Global was not used"):
            resolve_agent_bindings(config, agent_name="broken")
        if bound == "":
            with pytest.raises(UnknownMemoryStore, match="invalid memory store name"):
                resolve_agent_bindings(config, agent_name="floor")
        else:
            assert resolve_agent_bindings(config, agent_name="floor").memory_store_name == "default"
        # No name is a template session on Global, whatever the default crewmate
        # alias binds -- the alias is never consulted.
        assert resolve_agent_bindings(config).memory_store_name == "default"
        assert (
            resolve_agent_bindings(
                config, agent_name="chose", validate_memory_files=False
            ).memory_store_name
            == "work"
        )

        private_store = provision_member_memory(config, "chose")
        assert resolve_agent_bindings(config, agent_name="chose").memory_store_name == private_store


class TestResourceIndependence:
    """Property-based test for resource independence between config types."""

    # Feature: agent-workspace-bindings, Property 12: Resource independence
    @given(
        check_workspace=st.booleans(),
    )
    @settings(deadline=None)
    def test_resource_independence(
        self,
        check_workspace: bool,
    ) -> None:
        """WorkspaceConfig has no agent/memory fields;
        MemoryStoreConfig has no workspace/agent fields.

        **Validates: Requirements 3.3, 5.6**
        """
        import dataclasses

        agent_memory_field_names = {
            "kiro_agent",
            "agent",
            "agents",
            "memory_store",
            "memory_stores",
            "memory",
            "default_agent",
        }
        workspace_field_names = {
            "workspace",
            "workspaces",
            "default_workspace",
            "dir",
        }

        if check_workspace:
            # WorkspaceConfig must not have agent or memory fields
            ws_fields = {f.name for f in dataclasses.fields(WorkspaceConfig)}
            overlap = ws_fields & agent_memory_field_names
            assert not overlap, f"WorkspaceConfig has agent/memory fields: {overlap}"
        else:
            # MemoryStoreConfig must not have workspace or agent fields
            ms_fields = {f.name for f in dataclasses.fields(MemoryStoreConfig)}
            ws_agent_names = workspace_field_names | {
                "kiro_agent",
                "agent",
                "agents",
            }
            overlap = ms_fields & ws_agent_names
            assert not overlap, f"MemoryStoreConfig has workspace/agent fields: {overlap}"


class TestEdgeCases:
    """Unit tests for edge cases in agent-workspace-bindings.

    **Validates: Requirements 2.4, 4.3, 4.4, 5.4**
    """

    def test_empty_agents_empty_default_agent_falls_back(self) -> None:
        """Empty agents + empty default_agent triggers migration to create
        a default agent, then resolver uses that agent.

        **Validates: Requirement 2.4**
        """
        raw_config: dict = {
            "agents": {},
            "default_agent": "",
            "workspaces": {"default": {"dir": "my-workspace"}},
            "default_workspace": "default",
            "memory_stores": {"default": {"description": "test"}},
            "default_memory_store": "default",
        }
        cfg = _load_from_dict(raw_config)

        # Migration creates default agent
        assert "default" in cfg.agents
        assert cfg.default_agent == "default"

        result = resolve_agent_bindings(cfg)
        # Resolves via migrated default agent → workspace "default" → "my-workspace"
        assert result.workspace_dir == Path("my-workspace")
        # Resolves via migrated default agent → memory_store "default"
        assert result.memory_store_name == "default"
        # Migrated default agent uses "kirocrew" as kiro_agent (no legacy value)
        assert result.kiro_agent == "kirocrew"

    def test_missing_workspaces_creates_default_entry(self) -> None:
        """Missing workspaces section creates default entry.

        **Validates: Requirement 4.4**
        """
        raw_config: dict = {"agent": {"default_agent": "test"}}
        cfg = _load_from_dict(raw_config)

        assert "default" in cfg.workspaces
        assert isinstance(cfg.workspaces["default"], WorkspaceConfig)
        assert cfg.workspaces["default"].dir == "workspace"

    def test_missing_memory_stores_synthesizes_default(self) -> None:
        """Missing memory_stores section synthesizes default store.

        **Validates: Requirement 5.4**
        """
        raw_config: dict = {
            "memory": {"embedding_provider": "llama_cpp"},
        }
        cfg = _load_from_dict(raw_config)

        assert "default" in cfg.memory_stores
        assert isinstance(cfg.memory_stores["default"], MemoryStoreConfig)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(-5, 365), (0, 0), (30, 30)],
    )
    def test_history_max_days_sanitized_at_load(self, raw: int, expected: int) -> None:
        """A hand-edited negative history_max_days falls back to the default
        at load instead of reaching prune_history raw."""
        cfg = _load_from_dict({"memory": {"history_max_days": raw}})
        assert cfg.memory.history_max_days == expected

    def test_recent_tint_count_loaded_from_config(self) -> None:
        """recent_tint_count from config.json is used instead of default 0."""
        cfg = _load_from_dict({"dashboard": {"recent_tint_count": 8}})
        assert cfg.dashboard.recent_tint_count == 8

    def test_recent_tint_count_defaults_to_0(self) -> None:
        """recent_tint_count defaults to 0 (off) when not in config."""
        cfg = _load_from_dict({})
        assert cfg.dashboard.recent_tint_count == 0

    def test_folder_sort_loaded_from_config(self) -> None:
        for mode in ("custom", "name", "created"):
            cfg = _load_from_dict({"dashboard": {"folder_sort": mode}})
            assert cfg.dashboard.folder_sort == mode

    def test_folder_sort_defaults_to_custom(self) -> None:
        """Absent = the stored-order sidebar every earlier build drew, so an
        upgrade changes nothing the person sees."""
        cfg = _load_from_dict({})
        assert cfg.dashboard.folder_sort == "custom"

    def test_folder_sort_unknown_value_reads_as_custom(self) -> None:
        """A hand-edited or downgraded value must not fail open into a mode the
        sidebar's reader would not recognise either; both fall back the same way."""
        for junk in ("alphabetical", "", None, 3, ["name"], "Name"):
            cfg = _load_from_dict({"dashboard": {"folder_sort": junk}})
            assert cfg.dashboard.folder_sort == "custom", junk

    def test_session_sort_loaded_from_config(self) -> None:
        from kiro_crew.config.sections import SESSION_SORT_MODES

        for key in SESSION_SORT_MODES:
            cfg = _load_from_dict({"dashboard": {"session_sort": key}})
            assert cfg.dashboard.session_sort == key

    def test_session_sort_defaults_to_date_desc(self) -> None:
        """Absent = latest activity first, the order the sidebar draws with no
        stored choice, so an upgrade changes nothing the person sees."""
        cfg = _load_from_dict({})
        assert cfg.dashboard.session_sort == "date-desc"

    def test_session_sort_unknown_value_reads_as_date_desc(self) -> None:
        """A hand-edited or downgraded value falls back the same way the
        sidebar's reader does."""
        for junk in ("newest", "", None, 3, ["name-asc"], "Name-Asc"):
            cfg = _load_from_dict({"dashboard": {"session_sort": junk}})
            assert cfg.dashboard.session_sort == "date-desc", junk

    def test_a_build_without_manual_reads_it_as_the_default(self, monkeypatch) -> None:
        """A downgrade breaks nothing: the loader of a build whose key list ends
        before ``manual`` reads a stored ``manual`` as ``date-desc``."""
        from kiro_crew.config import sections

        before_manual = tuple(k for k in sections.SESSION_SORT_MODES if k != "manual")
        monkeypatch.setattr(sections, "SESSION_SORT_MODES", before_manual)
        cfg = _load_from_dict({"dashboard": {"session_sort": "manual"}})
        assert cfg.dashboard.session_sort == "date-desc"

    def test_update_nudge_loaded_from_config(self) -> None:
        """The popup's snooze/skip record round-trips through load, so a GET
        after a PATCH reads back what was written."""
        rec = {"version": "0.5.0", "snoozed_until": 1756000000.0, "skipped": True}
        cfg = _load_from_dict({"dashboard": {"update_nudge": rec}})
        assert cfg.dashboard.update_nudge == rec

    def test_update_nudge_defaults_to_empty_dict(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.dashboard.update_nudge == {}

    def test_update_nudge_non_dict_coerces_to_empty(self) -> None:
        """A hand-edited scalar must not crash load or leak a non-dict to the
        dashboard's config read-back."""
        cfg = _load_from_dict({"dashboard": {"update_nudge": "corrupt"}})
        assert cfg.dashboard.update_nudge == {}

    def test_embedding_provider_defaults_to_llama_cpp(self) -> None:
        """embedding_provider defaults to 'llama_cpp' (in-process, default-on)."""
        cfg = _load_from_dict({})
        assert cfg.memory.embedding_provider == "llama_cpp"

    def test_legacy_ollama_provider_coerces_to_llama_cpp(self) -> None:
        """Old configs with 'ollama' load fine and coerce to the in-process runtime."""
        raw_config: dict = {
            "memory": {"embedding_provider": "ollama"},
        }
        cfg = _load_from_dict(raw_config)
        assert cfg.memory.embedding_provider == "llama_cpp"

    def test_none_provider_coerces_to_llama_cpp(self) -> None:
        """Embeddings are always-on: a legacy 'none' (the disabled spelling) coerces too."""
        raw_config: dict = {
            "memory": {"embedding_provider": "none"},
        }
        cfg = _load_from_dict(raw_config)
        assert cfg.memory.embedding_provider == "llama_cpp"

    def test_legacy_removed_embedding_keys_are_ignored(self) -> None:
        """Configs carrying deleted Ollama-era keys still load without error.

        embedding_url / embedding_managed / embedding_auth / embedding_model /
        embedding_timeout_secs / embedding_runtime / allow_remote_embedding were
        removed from MemoryConfig; the loader ignores unknown keys.
        """
        raw_config: dict = {
            "memory": {
                "embedding_provider": "ollama",
                "embedding_url": "http://localhost:11434",
                "embedding_managed": True,
                "embedding_auth": "none",
                "embedding_model": "snowflake-arctic-embed2",
                "embedding_timeout_secs": 5.0,
                "embedding_runtime": "docker",
                "allow_remote_embedding": False,
            },
        }
        cfg = _load_from_dict(raw_config)
        assert cfg.memory.embedding_provider == "llama_cpp"  # coerced
        for removed in (
            "embedding_url",
            "embedding_managed",
            "embedding_auth",
            "embedding_model",
            "embedding_timeout_secs",
            "embedding_runtime",
            "allow_remote_embedding",
        ):
            assert not hasattr(cfg.memory, removed)

    def test_to_dict_always_writes_structured_workspace_format(self) -> None:
        """to_dict() always writes structured workspace format.

        **Validates: Requirement 4.3**
        """
        # Load from structured format (flat strings are rejected by schema validation)
        raw_config: dict = {
            "workspaces": {
                "default": {"dir": "workspace"},
                "oncall": {"dir": "workspace-oncall"},
            },
        }
        cfg = _load_from_dict(raw_config)

        # Serialize
        d = cfg.to_dict()

        # Workspaces must be structured dicts with "dir" key
        assert isinstance(d["workspaces"], dict)
        for ws_name, ws_val in d["workspaces"].items():
            assert isinstance(
                ws_val, dict
            ), f"Workspace '{ws_name}' should be a dict, got {type(ws_val)}"
            assert "dir" in ws_val, f"Workspace '{ws_name}' missing 'dir' key"

        assert d["workspaces"]["default"]["dir"] == "workspace"
        assert d["workspaces"]["oncall"]["dir"] == "workspace-oncall"


class TestUnsatisfiableSubagentCwdRoots(unittest.TestCase):
    """``subagent_cwd_allowed_roots`` is never widened, and never probed on load.

    A config persisted when the shipped default was narrower keeps that list
    through every upgrade, so a host with no ``~/workspace`` rejects every
    ``spawn_run`` cwd. Tempting as it is to repair that, these roots are a
    least-privilege allowlist: an operator who allowlisted a single directory
    sitting on an unmounted automount looks exactly like a stale default, so
    auto-repairing the second case would grant access the first withheld. The
    operator edits the config; nothing here rewrites it for them.
    """

    def _agents(self) -> dict:
        """A config stanza that triggers no write-back migration."""
        return {
            "agents": {
                "default": {
                    "kiro_agent": "kirocrew",
                    "workspace": "default",
                    "memory_store": "default",
                }
            },
            "default_agent": "default",
        }

    def _load(self, data: dict) -> tuple[KiroCrewConfig, dict, bool]:
        """Load *data* from a temp file; return (cfg, on-disk json, migrated)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp = Path(f.name)
        bak = tmp.with_suffix(".json.bak")
        try:
            with unittest.mock.patch(
                "kiro_crew.config.loader.config_path",
                return_value=tmp,
            ):
                cfg = KiroCrewConfig.load()
            return (cfg, json.loads(tmp.read_text(encoding="utf-8")), bak.exists())
        finally:
            tmp.unlink(missing_ok=True)
            bak.unlink(missing_ok=True)

    def test_unsatisfiable_list_is_never_widened(self):
        """No configured root exists → the list is left exactly as configured.

        Widening here would admit a cwd the operator deliberately excluded.
        """
        ghost = str(Path(tempfile.gettempdir()) / "kc-no-such-root-9f3a")
        data = self._agents() | {"agent": {"subagent_cwd_allowed_roots": [ghost]}}
        cfg, on_disk, migrated = self._load(data)

        self.assertEqual(cfg.agent.subagent_cwd_allowed_roots, [ghost])
        self.assertEqual(on_disk["agent"]["subagent_cwd_allowed_roots"], [ghost])
        self.assertFalse(migrated, "an unsatisfiable list must not rewrite the config")

    def test_load_does_not_stat_the_roots(self):
        """load() runs per spawn — it must not touch the filesystem for roots.

        A configured root on a stalled network mount would otherwise block the
        caller, and load() is reached from the async spawn path. Nothing stats
        the configured roots today; this guards against reintroducing it.
        """
        ghost = str(Path(tempfile.gettempdir()) / "kc-no-such-root-9f3a")
        data = self._agents() | {"agent": {"subagent_cwd_allowed_roots": [ghost]}}
        real_isdir = os.path.isdir

        with unittest.mock.patch("os.path.isdir", side_effect=real_isdir) as isdir:
            self._load(data)

        # Compare the call ARGUMENT, not str(call): a repr'd Windows path has
        # its backslashes escaped, so substring matching would find nothing
        # and pass vacuously on the one OS most likely to differ.
        probed = [
            c
            for c in isdir.call_args_list
            if c.args and os.path.normcase(str(c.args[0])) == os.path.normcase(ghost)
        ]
        self.assertEqual(probed, [], f"load() stat'd an allowed root: {probed}")

    def test_deliberately_narrow_list_survives(self):
        """One existing root is enough — a narrow allowlist is honored as written."""
        real = tempfile.gettempdir()
        data = self._agents() | {"agent": {"subagent_cwd_allowed_roots": [real]}}
        cfg, _, migrated = self._load(data)

        self.assertEqual(cfg.agent.subagent_cwd_allowed_roots, [real])
        self.assertFalse(migrated)

    def test_empty_list_stays_empty(self):
        """An empty list disables cwd overrides on purpose — not a broken config."""
        data = self._agents() | {"agent": {"subagent_cwd_allowed_roots": []}}
        cfg, _, migrated = self._load(data)

        self.assertEqual(cfg.agent.subagent_cwd_allowed_roots, [])
        self.assertFalse(migrated)

    def test_absent_key_keeps_the_historical_four_roots(self):
        """An absent key reaches the same fallback as a malformed one.

        Narrowing that fallback would revoke ~/workspaces and ~/workplaces from
        every config that simply omits the field.
        """
        cfg, _, _ = self._load(self._agents() | {"agent": {"log_level": "WARNING"}})

        self.assertEqual(
            cfg.agent.subagent_cwd_allowed_roots,
            ["~/workspace", "~/workspaces", "~/workplace", "~/workplaces"],
        )
        self.assertEqual(
            loader_module.DEFAULT_CWD_ALLOWED_ROOTS,
            cfg.agent.subagent_cwd_allowed_roots,
            "field default and fallback must not drift apart again",
        )


def test_agent_triggers_load() -> None:
    """`triggers` loads from config verbatim; a crew without triggers has ''."""
    cfg = _load_from_dict(
        {
            "agents": {
                "oncall": {"kiro_agent": "kirocrew", "triggers": "incident, outage"},
                "research": {"kiro_agent": "kirocrew", "description": "deep research crew"},
                "weird": {"kiro_agent": "kirocrew", "triggers": 1},
            },
            "default_agent": "oncall",
            "workspaces": {"default": {"dir": "workspace"}},
        }
    )
    # Explicit triggers load verbatim.
    assert cfg.agents["oncall"].triggers == "incident, outage"
    # A crew that defines no triggers keeps an empty string — it is not a routing
    # candidate (no fallback to the description).
    assert cfg.agents["research"].triggers == ""
    # A non-string triggers value is normalized to "" on load (never survives to
    # select_crew's .strip()).
    assert cfg.agents["weird"].triggers == ""
