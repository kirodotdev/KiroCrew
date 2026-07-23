"""Property-based tests for config/loader.py.

Tests the KiroCrewConfig loader validation logic using hypothesis
for property-based testing.
"""

from __future__ import annotations

import json
import logging
import platform
import tempfile
import unittest.mock
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

import kiro_crew.config.loader as loader_module
from kiro_crew.config.loader import (
    _HAS_JSONSCHEMA,
    AgentConfig,
    DashboardConfig,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    MemoryConfig,
    MemoryStoreConfig,
    ResolvedBindings,
    SessionConfig,
    SlackConfig,
    SttConfig,
    WorkspaceConfig,
    _migrate_workspaces,
    config_dir,
    resolve_agent_bindings,
    resolve_memory_store_config,
    validate_kiro_agent_references,
    workspace_dir_for,
)

# Logger used by the loader module — needed for capturing warnings in tests
logger = logging.getLogger("kiro_crew.config.loader")

# ---------------------------------------------------------------------------
# Helpers / Strategies
# ---------------------------------------------------------------------------

# Fields with enum constraints and their allowed values
_ENUM_FIELDS: list[tuple[str, str, list[str]]] = [
    ("agent", "approval_mode", ["auto", "interactive"]),
    ("agent", "provider", ["acp"]),
    ("agent", "sandbox", ["auto", "off"]),
    ("agent", "log_level", ["DEBUG", "INFO", "WARNING", "ERROR"]),
    ("memory", "embedding_provider", ["llama_cpp"]),
]

# Top-level keys recognised by the schema
_KNOWN_TOP_KEYS = {
    "agent",
    "session",
    "memory",
    "slack",
    "dashboard",
    "hooks",
    "agents",
    "default_agent",
    "workspaces",
    "default_workspace",
    "memory_stores",
    "default_memory_store",
    "auto_update",
}

# Skip marker for tests that require jsonschema validation
_requires_jsonschema = pytest.mark.skipif(
    not _HAS_JSONSCHEMA,
    reason="jsonschema not available — validation tests require it",
)


def _load_from_dict(data: object) -> KiroCrewConfig:
    """Write *data* to a temp config file and load via KiroCrewConfig.load()."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
    ) as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f)
        tmp = Path(f.name)

    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=tmp,
        ):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


def _load_from_raw_string(content: str) -> KiroCrewConfig:
    """Write raw string content to a temp file and load."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as f:
        f.write(content)
        tmp = Path(f.name)

    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=tmp,
        ):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


def _load_from_dict_with_logs(data: object) -> tuple[KiroCrewConfig, list[str]]:
    """Load config and capture warning log messages."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
    ) as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f)
        tmp = Path(f.name)

    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=tmp,
        ):
            logger = logging.getLogger("kiro_crew.config.loader")
            messages: list[str] = []
            original_warning = logger.warning

            def capture_warning(msg: object, *args: object) -> None:
                try:
                    messages.append(str(msg) % args)
                except Exception:
                    messages.append(str(msg))
                original_warning(msg, *args)

            with unittest.mock.patch.object(logger, "warning", capture_warning):
                result = KiroCrewConfig.load()
            return result, messages
    finally:
        tmp.unlink(missing_ok=True)


def _default_config() -> KiroCrewConfig:
    """Return a default KiroCrewConfig for comparison."""
    return KiroCrewConfig()


def test_max_subagents_defaults_to_auto_sentinel() -> None:
    """Stage 2: max_subagents defaults to the 0 ('auto') sentinel — both on the
    bare dataclass and when hydrated from an empty config — and 0 resolves to a
    computed cap that never regresses below the legacy floor of 3. An explicit
    positive value is preserved verbatim.
    """
    from kiro_crew.subagent import resolve_max_subagents

    # Bare dataclass default.
    assert KiroCrewConfig().agent.max_subagents == 0
    # Hydrated-from-empty default (loader .get path).
    loaded = _load_from_dict({})
    assert loaded.agent.max_subagents == 0
    # 0 sentinel auto-computes; floor guarantees >= 3.
    assert resolve_max_subagents(loaded) >= 3
    # Explicit value is honored, not auto-computed.
    pinned = _load_from_dict({"agent": {"max_subagents": 7}})
    assert pinned.agent.max_subagents == 7
    assert resolve_max_subagents(pinned) == 7


def test_sandbox_allow_unsandboxed_exec_loads_from_config() -> None:
    assert KiroCrewConfig().agent.sandbox_allow_unsandboxed_exec is False
    assert _load_from_dict({}).agent.sandbox_allow_unsandboxed_exec is False
    enabled = _load_from_dict({"agent": {"sandbox_allow_unsandboxed_exec": True}})
    assert enabled.agent.sandbox_allow_unsandboxed_exec is True


def test_publish_relocate_roots_parsed_and_round_trips():
    # Regression (PR #14 nrb): publish.relocate_roots was declared + consumed by
    # the relocate handler but NOT parsed in from_dict, so an operator value was
    # silently dropped (permanently []) and lost on round-trip.
    loaded = _load_from_dict(
        {
            "publish": {
                "allowed_destinations": ["artifactory"],
                "relocate_roots": ["/srv/shared", "  "],
            }
        }
    )
    # Parsed (blank entries filtered), not ignored.
    assert loaded.publish.relocate_roots == ["/srv/shared"]
    assert loaded.publish.allowed_destinations == ["artifactory"]
    # Survives a to_dict() -> load() round-trip.
    reloaded = _load_from_dict(loaded.to_dict())
    assert reloaded.publish.relocate_roots == ["/srv/shared"]


# Hypothesis strategy for safe identifier strings (no control chars, JSON-safe)
_safe_name_st = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
    min_size=1,
    max_size=15,
)

# Strategy for KiroCrewAgentConfig instances
_kirocrew_agent_config_st = st.builds(
    KiroCrewAgentConfig,
    kiro_agent=st.text(min_size=0, max_size=20),
    workspace=_safe_name_st,
    memory_store=_safe_name_st,
)

# Strategy for WorkspaceConfig instances
_workspace_config_st = st.builds(
    WorkspaceConfig,
    dir=st.text(min_size=1, max_size=30),
)

# Strategy for MemoryStoreConfig instances
_memory_store_config_st = st.builds(
    MemoryStoreConfig,
    description=st.text(min_size=0, max_size=30),
    embedding_provider=st.sampled_from(["", "none", "llama_cpp"]),
)

# Hypothesis strategy for generating valid KiroCrewConfig instances
_agent_config_st = st.builds(
    AgentConfig,
    approval_mode=st.sampled_from(["auto", "interactive"]),
    streaming=st.booleans(),
    model=st.text(min_size=0, max_size=20),
    provider=st.just("acp"),
    default_agent=st.text(min_size=0, max_size=20),
    sandbox=st.sampled_from(["auto", "off"]),
    soft_stop_budget_secs=st.floats(min_value=0.5, max_value=60.0),
)

_session_config_st = st.builds(
    SessionConfig,
    timeout_secs=st.integers(min_value=60, max_value=7200),
)

_memory_config_st = st.builds(
    MemoryConfig,
    embedding_provider=st.sampled_from(["llama_cpp"]),
    embedding_dim=st.sampled_from([256, 512, 1024]),
    semantic_confidence_threshold=st.floats(min_value=0.0, max_value=1.0),
    episodic_dedup_threshold=st.floats(min_value=0.0, max_value=1.0),
    episodic_max_results=st.integers(min_value=1, max_value=50),
    episodic_max_count=st.integers(min_value=100, max_value=50000),
    semantic_keys=st.just([]),
    history_idle_hours=st.floats(min_value=0.5, max_value=24.0),
    history_max_days=st.integers(min_value=1, max_value=365),
    migrated=st.booleans(),
)

_slack_config_st = st.builds(
    SlackConfig,
    allowed_users=st.just([]),
    tracking_channels=st.just([]),
    open_channels=st.lists(st.from_regex(r"C[A-Z0-9]{8,10}", fullmatch=True), max_size=5),
    command=st.text(min_size=1, max_size=20),
    reactions_enabled=st.booleans(),
    show_thinking=st.booleans(),
)

_dashboard_config_st = st.builds(
    DashboardConfig,
    url=st.text(min_size=0, max_size=50),
)

_kirocrew_config_st = st.builds(
    KiroCrewConfig,
    agent=_agent_config_st,
    session=_session_config_st,
    memory=_memory_config_st,
    slack=_slack_config_st,
    dashboard=_dashboard_config_st,
    hooks=st.just({}),
    agents=st.dictionaries(
        keys=_safe_name_st,
        values=_kirocrew_agent_config_st,
        min_size=0,
        max_size=3,
    ),
    default_agent=st.one_of(st.just(""), _safe_name_st),
    workspaces=st.dictionaries(
        keys=_safe_name_st,
        values=_workspace_config_st,
        min_size=0,
        max_size=3,
    ),
    default_workspace=st.text(min_size=1, max_size=20),
    memory_stores=st.dictionaries(
        keys=_safe_name_st,
        values=_memory_store_config_st,
        min_size=0,
        max_size=3,
    ),
    default_memory_store=st.one_of(st.just("default"), _safe_name_st),
    auto_update=st.booleans(),
)


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestConfigLoaderProperties:
    """Property-based tests for the config loader validation logic."""

    # Feature: config-schema, Property 6: KiroCrewConfig load/to_dict round-trip
    @given(config=_kirocrew_config_st)
    @settings(deadline=None)
    def test_load_to_dict_round_trip(
        self,
        config: KiroCrewConfig,
    ) -> None:
        """Calling to_dict() then load() from that dict must yield an
        equivalent KiroCrewConfig instance.

        **Validates: Requirements 2.4, 2.5, 9.4, 9.6**
        """
        d = config.to_dict()
        loaded = _load_from_dict(d)

        # Compare agent fields
        assert loaded.agent.approval_mode == config.agent.approval_mode
        assert loaded.agent.streaming == config.agent.streaming
        assert loaded.agent.model == config.agent.model
        assert loaded.agent.provider == config.agent.provider
        assert loaded.agent.default_agent == config.agent.default_agent
        assert loaded.agent.sandbox == config.agent.sandbox

        # Compare session
        assert loaded.session.timeout_secs == config.session.timeout_secs

        # Compare memory fields
        assert loaded.memory.embedding_provider == config.memory.embedding_provider
        assert loaded.memory.embedding_dim == config.memory.embedding_dim
        assert loaded.memory.migrated == config.memory.migrated
        assert loaded.memory.episodic_max_results == config.memory.episodic_max_results
        assert loaded.memory.episodic_max_count == config.memory.episodic_max_count
        assert loaded.memory.history_max_days == config.memory.history_max_days

        # Compare slack
        assert loaded.slack.command == config.slack.command
        assert loaded.slack.allowed_users == config.slack.allowed_users
        assert loaded.slack.tracking_channels == config.slack.tracking_channels
        assert loaded.slack.open_channels == config.slack.open_channels
        assert loaded.slack.reactions_enabled == config.slack.reactions_enabled
        assert loaded.slack.show_thinking == config.slack.show_thinking

        # Compare dashboard
        assert loaded.dashboard.url == config.dashboard.url
        assert loaded.dashboard.widget_density == config.dashboard.widget_density
        assert loaded.dashboard.recent_tint_count == config.dashboard.recent_tint_count

        # Compare top-level fields
        assert loaded.hooks == config.hooks
        assert loaded.default_workspace == config.default_workspace
        assert loaded.auto_update == config.auto_update

        # Compare workspaces (migration produces WorkspaceConfig objects)
        if config.workspaces:
            for ws_name, ws_cfg in config.workspaces.items():
                assert ws_name in loaded.workspaces
                assert loaded.workspaces[ws_name].dir == ws_cfg.dir
        else:
            # Empty workspaces → default entry synthesized
            assert "default" in loaded.workspaces
            assert loaded.workspaces["default"].dir == "workspace"

    # Feature: config-schema, Property 9: Type mismatch falls back to default
    @_requires_jsonschema
    @given(
        field_idx=st.integers(min_value=0, max_value=4),
        wrong_idx=st.integers(min_value=0, max_value=3),
    )
    @settings(deadline=None)
    def test_type_mismatch_falls_back_to_default(
        self,
        field_idx: int,
        wrong_idx: int,
    ) -> None:
        """When a config value has an incorrect type, load() must fall
        back to the field's default value.

        **Validates: Requirements 6.1, 6.2**
        """
        fields = [
            ("agent", "approval_mode", "string"),
            ("agent", "streaming", "boolean"),
            ("session", "timeout_secs", "integer"),
            ("memory", "embedding_dim", "integer"),
            ("memory", "migrated", "boolean"),
        ]
        wrong_values = [
            42,  # wrong for string/boolean
            "not_a_num",  # wrong for integer/boolean
            True,  # wrong for string/integer
            [1, 2, 3],  # wrong for all scalar types
        ]

        section, key, expected_type = fields[field_idx]
        wrong_value = wrong_values[wrong_idx]

        # Skip cases where the wrong_value accidentally has the right type
        type_map = {"string": str, "boolean": bool, "integer": int}
        expected_py = type_map[expected_type]
        if expected_type == "integer":
            assume(not isinstance(wrong_value, int) or isinstance(wrong_value, bool))
        elif expected_type == "boolean":
            assume(not isinstance(wrong_value, bool))
        else:
            assume(not isinstance(wrong_value, expected_py))

        data: dict = {section: {key: wrong_value}}
        loaded = _load_from_dict(data)
        defaults = _default_config()

        loaded_section = getattr(loaded, section)
        default_section = getattr(defaults, section)
        assert getattr(loaded_section, key) == getattr(
            default_section, key
        ), f"Expected default for {section}.{key} after type mismatch"

    # Feature: config-schema, Property 10: Enum violation falls back to default
    @_requires_jsonschema
    @given(
        field_idx=st.integers(min_value=0, max_value=3),
        bad_value=st.text(min_size=1, max_size=20),
    )
    @settings(deadline=None)
    def test_enum_violation_falls_back_to_default(
        self,
        field_idx: int,
        bad_value: str,
    ) -> None:
        """When a config key has an enum constraint and the value is not
        in the allowed set, load() must fall back to the field's default.

        **Validates: Requirements 6.3**
        """
        section, key, allowed = _ENUM_FIELDS[field_idx]
        assume(bad_value not in allowed)

        data: dict = {section: {key: bad_value}}
        loaded = _load_from_dict(data)
        defaults = _default_config()

        loaded_section = getattr(loaded, section)
        default_section = getattr(defaults, section)
        assert getattr(loaded_section, key) == getattr(default_section, key), (
            f"Expected default for {section}.{key} after enum violation "
            f"(value={bad_value!r}, allowed={allowed})"
        )

    # Feature: config-schema, Property 11: Unrecognized keys are detected
    @_requires_jsonschema
    @given(
        extra_keys=st.lists(
            st.text(
                alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz_"),
                min_size=2,
                max_size=15,
            ).filter(lambda k: k not in _KNOWN_TOP_KEYS),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(deadline=None)
    def test_unrecognized_keys_detected(
        self,
        extra_keys: list[str],
    ) -> None:
        """When config.json contains unrecognized top-level keys,
        load() must detect and warn about them.

        **Validates: Requirements 6.4**
        """
        data: dict = {k: "some_value" for k in extra_keys}
        _, messages = _load_from_dict_with_logs(data)

        unrecognized_msgs = [m for m in messages if "unrecognized top-level keys" in m]
        assert len(unrecognized_msgs) > 0, (
            f"Expected warning about unrecognized keys {extra_keys}, " f"got messages: {messages}"
        )

        warning_text = unrecognized_msgs[0]
        for k in extra_keys:
            assert k in warning_text, f"Key '{k}' not mentioned in warning: {warning_text}"

    # Feature: config-schema, Property 12: load() always returns valid KiroCrewConfig
    @given(
        content=st.one_of(
            st.text(min_size=0, max_size=200),
            st.just(""),
            st.just("null"),
            st.just("[]"),
            st.just("42"),
            st.just("{"),
            st.just('{"agent": "not_an_object"}'),
        ),
    )
    @settings(deadline=None)
    def test_load_always_returns_valid_config(
        self,
        content: str,
    ) -> None:
        """For any input content, load() must return a KiroCrewConfig
        instance without raising an exception.

        **Validates: Requirements 6.6**
        """
        result = _load_from_raw_string(content)

        assert isinstance(result, KiroCrewConfig)
        assert isinstance(result.agent, AgentConfig)
        assert isinstance(result.session, SessionConfig)
        assert isinstance(result.memory, MemoryConfig)
        assert isinstance(result.slack, SlackConfig)
        assert isinstance(result.dashboard, DashboardConfig)
        assert isinstance(result.hooks, dict)
        assert isinstance(result.workspaces, dict)
        assert isinstance(result.default_workspace, str)
        assert isinstance(result.auto_update, bool)

    # Feature: config-schema, Property 14: Deprecated fields are accepted during loading
    @_requires_jsonschema
    @given(
        command_val=st.text(min_size=1, max_size=20),
    )
    @settings(deadline=None)
    def test_deprecated_fields_accepted_during_loading(
        self,
        command_val: str,
    ) -> None:
        """When a field is marked deprecated, load() must still accept
        and apply the provided value (not fall back to default).

        Since there are currently no deprecated fields in the config,
        this test temporarily marks ``slack.command`` as deprecated and
        verifies the value is still loaded.

        **Validates: Requirements 8.2**
        """
        from kiro_crew.config import schema as schema_mod

        # Find and temporarily mark slack.command as deprecated
        target_entry = None
        for entry in schema_mod.SCHEMA_REGISTRY:
            if entry.path == "slack.command":
                target_entry = entry
                break
        assert target_entry is not None, "slack.command not in SCHEMA_REGISTRY"

        original_deprecated = target_entry.deprecated
        # Also patch JSON Schema x-meta
        slack_props = (
            schema_mod.JSON_SCHEMA.get("properties", {})
            .get("slack", {})
            .get("properties", {})
            .get("command", {})
        )
        original_xmeta_dep = slack_props.get("x-meta", {}).get("deprecated", False)

        try:
            object.__setattr__(target_entry, "deprecated", True)
            if "x-meta" in slack_props:
                slack_props["x-meta"]["deprecated"] = True

            data: dict = {"slack": {"command": command_val}}
            loaded = _load_from_dict(data)

            assert loaded.slack.command == command_val, (
                f"Expected deprecated field slack.command={command_val!r}, "
                f"got {loaded.slack.command!r}"
            )
        finally:
            object.__setattr__(target_entry, "deprecated", original_deprecated)
            if "x-meta" in slack_props:
                slack_props["x-meta"]["deprecated"] = original_xmeta_dep


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
        store_name=_safe_name_st,
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

        # Resolve via explicit agent_name
        result = resolve_agent_bindings(config, agent_name=agent_name)
        assert isinstance(result, ResolvedBindings)
        assert result.workspace_dir == Path(ws_dir)
        assert result.memory_store_name == store_name
        assert result.kiro_agent == kiro_agent_name

        # Resolve via default_agent (no explicit agent_name)
        result2 = resolve_agent_bindings(config)
        assert result2.workspace_dir == Path(ws_dir)
        assert result2.memory_store_name == store_name
        assert result2.kiro_agent == kiro_agent_name

    # Feature: agent-workspace-bindings, Property 4: Resolver fallback on missing references
    @given(
        agent_name=_safe_name_st,
        missing_ws=_safe_name_st,
        missing_store=_safe_name_st,
        fallback_ws_name=_safe_name_st,
        fallback_store_name=_safe_name_st,
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
        """When an agent references a non-existent workspace or store,
        the resolver falls back to default_workspace / default_memory_store.

        **Validates: Requirements 7.3, 7.4, 2.3**
        """
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

        result = resolve_agent_bindings(config, agent_name=agent_name)

        # Should fall back to default_workspace dir
        assert result.workspace_dir == Path(fallback_ws_dir)
        # Should fall back to default_memory_store name
        assert result.memory_store_name == fallback_store_name

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
                prefix = f"KiroCrew agent '{mc_name}' references"
                matching = [m for m in log_messages if prefix in m]
                assert len(matching) == 0, (
                    f"Unexpected warning for agent '{mc_name}' with "
                    f"kiro_agent='{mc_agent.kiro_agent}': {log_messages}"
                )

    # Feature: agent-workspace-bindings, Property 7: Workspace path resolution
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
        # (schema validation may strip invalid entries, so only check
        # that surviving workspaces are structured)
        for ws_name, ws_cfg in cfg.workspaces.items():
            assert isinstance(ws_cfg, WorkspaceConfig)

        # Always has at least one workspace (default synthesized if empty)
        assert len(cfg.workspaces) >= 1

        # Req 9.3: No memory_stores → default synthesized
        assert "default" in cfg.memory_stores
        assert isinstance(cfg.memory_stores["default"], MemoryStoreConfig)

        # Resolve bindings → uses migrated default agent
        result = resolve_agent_bindings(cfg)
        assert result.kiro_agent == expected_kiro


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

    def test_recent_tint_count_loaded_from_config(self) -> None:
        """recent_tint_count from config.json is used instead of default 0."""
        cfg = _load_from_dict({"dashboard": {"recent_tint_count": 8}})
        assert cfg.dashboard.recent_tint_count == 8

    def test_recent_tint_count_defaults_to_0(self) -> None:
        """recent_tint_count defaults to 0 (off) when not in config."""
        cfg = _load_from_dict({})
        assert cfg.dashboard.recent_tint_count == 0

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
        """Embeddings are always-on: a legacy 'none' (previously-disabled) coerces too."""
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


class TestPersistentLogLevel:
    """Tests for the persistent log_level config field."""

    def test_default_log_level_is_warning(self) -> None:
        """When no log_level is specified, default is WARNING."""
        cfg = _load_from_dict({})
        assert cfg.agent.log_level == "WARNING"

    def test_log_level_loaded_from_config(self) -> None:
        """log_level is read from agent section."""
        cfg = _load_from_dict({"agent": {"log_level": "DEBUG"}})
        assert cfg.agent.log_level == "DEBUG"

    def test_log_level_case_insensitive(self) -> None:
        """log_level is uppercased on load."""
        cfg = _load_from_dict({"agent": {"log_level": "info"}})
        assert cfg.agent.log_level == "INFO"

    def test_log_level_round_trips_through_to_dict(self) -> None:
        """log_level survives save/load round-trip."""
        cfg = _load_from_dict({"agent": {"log_level": "ERROR"}})
        d = cfg.to_dict()
        assert d["agent"]["log_level"] == "ERROR"


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
        store_name=_safe_name_st,
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

        result = resolve_agent_bindings(config, agent_name=agent_name)

        assert result.workspace_dir == Path(ws_dir)
        assert result.memory_store_name == store_name
        assert result.kiro_agent == kiro_agent_name

    # Feature: multi-agent-orchestration, Property 6: Non-KiroCrew agent names resolve via default agent
    @given(
        default_name=_safe_name_st,
        unknown_name=_safe_name_st,
        kiro_agent_name=st.text(min_size=1, max_size=15),
        ws_dir=st.text(min_size=1, max_size=20),
        store_name=_safe_name_st,
    )
    @settings(deadline=None)
    def test_non_kirocrew_agent_names_resolve_via_default(
        self,
        default_name: str,
        unknown_name: str,
        kiro_agent_name: str,
        ws_dir: str,
        store_name: str,
    ) -> None:
        """For any agent name NOT in config.agents, calling
        resolve_agent_bindings(config, agent_name) shall return the same
        ResolvedBindings as calling with config.default_agent.

        **Validates: Requirements 1.2, 2.2, 7.2**
        """
        assume(unknown_name != default_name)

        config = KiroCrewConfig(
            agents={
                default_name: KiroCrewAgentConfig(
                    kiro_agent=kiro_agent_name,
                    workspace="default",
                    memory_store=store_name,
                ),
            },
            default_agent=default_name,
            workspaces={"default": WorkspaceConfig(dir=ws_dir)},
            default_workspace="default",
            memory_stores={store_name: MemoryStoreConfig()},
            default_memory_store=store_name,
        )

        result_unknown = resolve_agent_bindings(config, agent_name=unknown_name)
        result_default = resolve_agent_bindings(config, agent_name=default_name)

        assert result_unknown.workspace_dir == result_default.workspace_dir, (
            f"Unknown agent workspace_dir={result_unknown.workspace_dir} "
            f"!= default={result_default.workspace_dir}"
        )
        assert result_unknown.memory_store_name == result_default.memory_store_name, (
            f"Unknown agent memory_store_name={result_unknown.memory_store_name} "
            f"!= default={result_default.memory_store_name}"
        )
        assert result_unknown.kiro_agent == result_default.kiro_agent, (
            f"Unknown agent kiro_agent={result_unknown.kiro_agent} "
            f"!= default={result_default.kiro_agent}"
        )
        assert result_unknown.effective_memory_config == result_default.effective_memory_config


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
            # Clean up backup file
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
            # Start with empty config
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

        result = resolve_agent_bindings(config, agent_name="test")

        # Falls back to default_workspace dir
        assert result.workspace_dir == Path("my-fallback-dir")

    def test_resolver_with_empty_agent_name_uses_default(self) -> None:
        """Resolver with empty agent name uses default_agent.

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

        # Empty string agent_name → uses default_agent
        result = resolve_agent_bindings(config, agent_name="")
        assert result.kiro_agent == "kirocrew"
        assert result.workspace_dir == Path("ws-dir")

        # None agent_name → uses default_agent
        result2 = resolve_agent_bindings(config, agent_name=None)
        assert result2.kiro_agent == "kirocrew"
        assert result2.workspace_dir == Path("ws-dir")


class TestReactionsEmptyStringFiltering:
    """Empty-string reaction values must be filtered out, preserving defaults."""

    def test_empty_string_reaction_filtered(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"slack": {"reactions": {"done": "", "error": "boom"}}}))
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            cfg = KiroCrewConfig.load()
        # Empty string should be dropped
        assert "done" not in cfg.slack.reactions
        # Non-empty value preserved
        assert cfg.slack.reactions["error"] == "boom"


class TestReactionsNullSuppression:
    """``null`` (JSON) / ``None`` (Python) values must be preserved as suppression sentinels."""

    def test_null_reaction_preserved(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"slack": {"reactions": {"done": None, "error": "boom"}}}))
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            cfg = KiroCrewConfig.load()
        # null should be preserved (distinct from absent key)
        assert "done" in cfg.slack.reactions
        assert cfg.slack.reactions["done"] is None
        # Non-empty value preserved
        assert cfg.slack.reactions["error"] == "boom"

    def test_non_string_non_null_filtered(self, tmp_path: Path) -> None:
        """Values that are neither strings nor null (e.g. numbers, bools) are dropped."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"slack": {"reactions": {"done": 42, "error": True, "tool": "ok"}}})
        )
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            cfg = KiroCrewConfig.load()
        assert "done" not in cfg.slack.reactions
        assert "error" not in cfg.slack.reactions
        assert cfg.slack.reactions["tool"] == "ok"


class TestSttStreamingDefault:
    """Pin the fresh-install default for `stt.streaming` to False."""

    def test_stt_config_dataclass_default_is_false(self) -> None:
        assert SttConfig().streaming is False

    def test_missing_stt_key_loads_streaming_false(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({}))
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            cfg = KiroCrewConfig.load()
        assert cfg.stt.streaming is False

    def test_partial_stt_block_without_streaming_key_loads_false(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"stt": {"provider": "transcribe", "language_code": "en-US"}})
        )
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            cfg = KiroCrewConfig.load()
        assert cfg.stt.streaming is False


# ---------------------------------------------------------------------------
# Phase 3: Soft-Stop Config Field Tests
# ---------------------------------------------------------------------------


class TestSoftStopBudget:
    """Tests for agent.soft_stop_budget_secs config field."""

    def test_soft_stop_budget_default(self) -> None:
        """Default AgentConfig has soft_stop_budget_secs == 10.0."""
        cfg = AgentConfig()
        assert cfg.soft_stop_budget_secs == 10.0

    def test_soft_stop_budget_valid_range(self) -> None:
        """AgentConfig accepts soft_stop_budget_secs within [0.5, 60.0]."""
        cfg = AgentConfig(soft_stop_budget_secs=10.0)
        assert cfg.soft_stop_budget_secs == 10.0

    def test_soft_stop_budget_too_low(self, caplog) -> None:
        """AgentConfig clamps soft_stop_budget_secs below 0.5 to 0.5 with a warning."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            cfg = AgentConfig(soft_stop_budget_secs=0.1)
        assert cfg.soft_stop_budget_secs == 0.5
        assert "out of range" in caplog.text

    def test_soft_stop_budget_too_high(self, caplog) -> None:
        """AgentConfig clamps soft_stop_budget_secs above 60.0 to 60.0 with a warning."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            cfg = AgentConfig(soft_stop_budget_secs=120.0)
        assert cfg.soft_stop_budget_secs == 60.0
        assert "out of range" in caplog.text

    def test_soft_stop_budget_appears_in_schema(self) -> None:
        """Generated config baseline includes soft_stop_budget_secs."""
        from kiro_crew.config.schema import SCHEMA_REGISTRY

        paths = [e.path for e in SCHEMA_REGISTRY]
        assert "agent.soft_stop_budget_secs" in paths

        entry = next(e for e in SCHEMA_REGISTRY if e.path == "agent.soft_stop_budget_secs")
        assert entry.type == "number"
        assert entry.default_value == 10.0


class TestDashboardMcpProbeTimeout:
    """Tests for the dashboard.mcp_probe_timeout_secs config field."""

    def test_dashboard_mcp_probe_timeout_default(self) -> None:
        """DashboardConfig defaults mcp_probe_timeout_secs to 15."""
        cfg = DashboardConfig()
        assert cfg.mcp_probe_timeout_secs == 15

    def test_dashboard_mcp_probe_timeout_from_json(self) -> None:
        """Loading config with mcp_probe_timeout_secs reads the value."""
        content = json.dumps({"dashboard": {"mcp_probe_timeout_secs": 30}})
        cfg = _load_from_raw_string(content)
        assert cfg.dashboard.mcp_probe_timeout_secs == 30

    def test_dashboard_mcp_probe_timeout_invalid_falls_back(self) -> None:
        """Non-int mcp_probe_timeout_secs falls back to default 15."""
        content = json.dumps({"dashboard": {"mcp_probe_timeout_secs": "fast"}})
        cfg = _load_from_raw_string(content)
        assert cfg.dashboard.mcp_probe_timeout_secs == 15


class TestTrackingChannelsValidation:
    """Tests for slack.tracking_channels validation and coercion."""

    def test_dict_format_passes_through(self) -> None:
        """Proper dict format with channel_id is accepted as-is."""
        data = {"slack": {"tracking_channels": [{"channel_id": "C0B371VEW5S", "name": "ops"}]}}
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 1
        assert cfg.slack.tracking_channels[0]["channel_id"] == "C0B371VEW5S"

    def test_bare_string_coerced_to_dict(self) -> None:
        """Bare channel ID strings are auto-coerced to dict format."""
        data = {"slack": {"tracking_channels": ["C0B371VEW5S"]}}
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 1
        assert cfg.slack.tracking_channels[0]["channel_id"] == "C0B371VEW5S"

    def test_bare_string_coercion_logs_warning(self) -> None:
        """Bare string coercion produces a warning log."""
        data = {"slack": {"tracking_channels": ["C0B371VEW5S", "C1234567890"]}}
        cfg, logs = _load_from_dict_with_logs(data)
        assert len(cfg.slack.tracking_channels) == 2
        assert any("bare string" in msg for msg in logs)

    def test_invalid_entries_rejected(self) -> None:
        """Entries that are neither valid dicts nor channel-ID strings are dropped."""
        data = {"slack": {"tracking_channels": [123, None, {"name": "no-id"}]}}
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 0

    def test_invalid_entries_log_warning(self) -> None:
        """Invalid entries produce a warning log."""
        data = {"slack": {"tracking_channels": [42, "not-a-channel-id"]}}
        cfg, logs = _load_from_dict_with_logs(data)
        assert any("invalid entries" in msg for msg in logs)

    def test_mixed_format_all_valid_coerced(self) -> None:
        """Mix of dicts and bare strings both work."""
        data = {
            "slack": {
                "tracking_channels": [
                    {"channel_id": "C111", "name": "one"},
                    "C222",
                ]
            }
        }
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 2
        ids = {c["channel_id"] for c in cfg.slack.tracking_channels}
        assert ids == {"C111", "C222"}

    def test_empty_list_no_warnings(self) -> None:
        """Empty tracking_channels produces no warnings."""
        data = {"slack": {"tracking_channels": []}}
        cfg, logs = _load_from_dict_with_logs(data)
        assert cfg.slack.tracking_channels == []
        assert not any("tracking_channels" in msg for msg in logs)


class TestAllowedEnterpriseIdsFiltering:
    """Tests for ``slack.allowed_enterprise_ids`` prefix filtering.

    The loader accepts both ``E``-prefix Slack enterprise IDs (org-level) and
    ``T``-prefix workspace IDs (Enterprise Grid child workspaces).  Other
    prefixes and non-string entries are dropped.
    """

    def test_e_prefix_enterprise_id_kept(self) -> None:
        """Standard E-prefix enterprise IDs (Slack org-level) are preserved."""
        data = {"slack": {"allowed_enterprise_ids": ["E015GUGD2V6"]}}
        cfg = _load_from_dict(data)
        assert "E015GUGD2V6" in cfg.slack.allowed_enterprise_ids

    def test_t_prefix_workspace_id_kept(self) -> None:
        """T-prefix workspace IDs (Enterprise Grid child workspaces) are preserved."""
        data = {"slack": {"allowed_enterprise_ids": ["T016NEJQWE9"]}}
        cfg = _load_from_dict(data)
        assert "T016NEJQWE9" in cfg.slack.allowed_enterprise_ids

    def test_mixed_e_and_t_prefix_kept(self) -> None:
        """Both E- and T-prefix IDs coexist in the allowlist."""
        data = {"slack": {"allowed_enterprise_ids": ["E015GUGD2V6", "T016NEJQWE9"]}}
        cfg = _load_from_dict(data)
        assert "E015GUGD2V6" in cfg.slack.allowed_enterprise_ids
        assert "T016NEJQWE9" in cfg.slack.allowed_enterprise_ids

    def test_invalid_prefix_dropped(self) -> None:
        """IDs with neither E nor T prefix are stripped."""
        data = {"slack": {"allowed_enterprise_ids": ["X999INVALID", "ABCDEF"]}}
        cfg = _load_from_dict(data)
        assert cfg.slack.allowed_enterprise_ids == []

    def test_non_string_entries_dropped(self) -> None:
        """Non-string entries (int, None) are dropped without raising."""
        data = {"slack": {"allowed_enterprise_ids": [42, None, "E015GUGD2V6"]}}
        cfg = _load_from_dict(data)
        assert cfg.slack.allowed_enterprise_ids == ["E015GUGD2V6"]

    def test_empty_list_yields_empty_allowlist(self) -> None:
        """Empty list produces an empty allowlist."""
        data = {"slack": {"allowed_enterprise_ids": []}}
        cfg = _load_from_dict(data)
        assert cfg.slack.allowed_enterprise_ids == []


class TestWidgetDensityRoundTrip:
    """Tests for dashboard.widget_density persistence."""

    def test_widget_density_defaults_to_more(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.dashboard.widget_density == "more"

    def test_widget_density_loaded_from_config(self) -> None:
        cfg = _load_from_dict({"dashboard": {"widget_density": "less"}})
        assert cfg.dashboard.widget_density == "less"

    def test_widget_density_survives_save_load(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        cfg = _load_from_dict({"dashboard": {"widget_density": "less"}})
        cfg_file = tmp_path / "config.json"
        with patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            cfg.save()
            loaded = KiroCrewConfig.load()
        assert loaded.dashboard.widget_density == "less"


class TestArchiveRetentionDays:
    """session.archive_retention_days parsing and disable sentinel."""

    def test_default_is_30(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.session.archive_retention_days == 30

    def test_explicit_value_loaded(self) -> None:
        cfg = _load_from_dict({"session": {"archive_retention_days": 90}})
        assert cfg.session.archive_retention_days == 90

    def test_null_disables_cleanup(self) -> None:
        cfg = _load_from_dict({"session": {"archive_retention_days": None}})
        assert cfg.session.archive_retention_days == -1

    def test_negative_normalizes_to_disabled(self) -> None:
        cfg = _load_from_dict({"session": {"archive_retention_days": -5}})
        assert cfg.session.archive_retention_days == -1

    def test_invalid_falls_back_to_default(self) -> None:
        cfg = _load_from_dict({"session": {"archive_retention_days": "garbage"}})
        assert cfg.session.archive_retention_days == 30

    def test_survives_save_load(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        cfg = _load_from_dict({"session": {"archive_retention_days": 60}})
        cfg_file = tmp_path / "config.json"
        with patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            cfg.save()
            loaded = KiroCrewConfig.load()
        assert loaded.session.archive_retention_days == 60

    def test_schema_permits_null_sentinel(self) -> None:
        """The generated JSON Schema must accept ``null`` for this field.

        Regression guard: ``null`` is the disable sentinel. If the
        schema emits a bare ``{"type": "integer"}`` then, on any host where
        jsonschema is installed, ``validate_config_data`` strips the null and
        the loader silently reverts to the default 30 (cleanup stays ON — the
        opposite of intent). This assertion is interpreter-independent: it
        checks the schema shape directly, so it fails everywhere if the
        ``nullable`` marker regresses, not only where jsonschema is installed.
        """
        from kiro_crew.config.schema import JSON_SCHEMA

        node = JSON_SCHEMA["properties"]["session"]["properties"]["archive_retention_days"]
        assert (
            "null" in node["type"]
        ), f"archive_retention_days schema must allow null, got {node['type']!r}"

    def test_validation_preserves_null(self) -> None:
        """When jsonschema runs, ``null`` must survive validation (not be stripped)."""
        from kiro_crew.config import validation

        if not validation._HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed on this interpreter")
        data = {"session": {"archive_retention_days": None}}
        validated = validation.validate_config_data(data)
        assert validated["session"]["archive_retention_days"] is None


class TestConfigCache:
    """Validated-data cache keyed on file mtime/size (hot-path load())."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        # Each test starts with a clean module-level cache and leaves one behind.
        from kiro_crew.config.loader import _invalidate_config_cache

        _invalidate_config_cache()
        yield
        _invalidate_config_cache()

    # Canonical scaffold so the one-shot write-back migration (which calls
    # save() and would invalidate the cache mid-test) does not fire.
    _CANON = {
        "agents": {"default": {"kiro_agent": "kirocrew"}},
        "default_agent": "default",
        "workspaces": {"default": {"dir": "~/workspace"}},
    }

    def _write(self, path: Path, data: dict) -> None:
        merged = {**self._CANON, **data}
        path.write_text(json.dumps(merged), encoding="utf-8")

    def test_second_load_skips_validation(self, tmp_path: Path) -> None:
        """A cache hit must not re-run jsonschema validation."""
        from unittest.mock import patch

        cfg_file = tmp_path / "config.json"
        # _write merges a canonical scaffold so the one-shot write-back migration
        # (which calls save() and would invalidate the cache) does not fire.
        self._write(cfg_file, {"agent": {"provider": "acp"}})
        calls = {"n": 0}
        real_validate = loader_module._validate_config_data

        def _counting(data):
            calls["n"] += 1
            return real_validate(data)

        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=tmp_path / "config.local.json",
            ),
            patch("kiro_crew.config.loader._validate_config_data", _counting),
        ):
            KiroCrewConfig.load()
            KiroCrewConfig.load()
            KiroCrewConfig.load()
        # Validated once; subsequent loads served from cache.
        assert calls["n"] == 1

    def test_edit_busts_cache(self, tmp_path: Path) -> None:
        """A changed file (new mtime/size) must be re-read, not served stale."""
        import os as _os
        from unittest.mock import patch

        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
        ):
            self._write(cfg_file, {"agent": {"model": "model-a"}})
            first = KiroCrewConfig.load()
            assert first.agent.model == "model-a"
            # Rewrite with different content (model value differs in length, so
            # the size component of the fingerprint changes); also force a distinct
            # mtime so the change is detected on coarse-resolution filesystems too.
            self._write(cfg_file, {"agent": {"model": "model-bbbb"}})
            st = cfg_file.stat()
            _os.utime(cfg_file, ns=(st.st_atime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000))
            second = KiroCrewConfig.load()
        assert second.agent.model == "model-bbbb"

    def test_save_invalidates_cache(self, tmp_path: Path) -> None:
        """save() must drop the cache so the next load sees the written value."""
        from unittest.mock import patch

        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
        ):
            self._write(cfg_file, {"agent": {"yolo": False}})
            cfg = KiroCrewConfig.load()
            assert cfg.agent.yolo is False
            cfg.agent.yolo = True
            cfg.save()
            reloaded = KiroCrewConfig.load()
        assert reloaded.agent.yolo is True

    def test_returned_config_is_independent(self, tmp_path: Path) -> None:
        """Mutating a loaded config must not corrupt the cached data for the next
        load — each load() returns freshly-constructed dataclasses."""
        from unittest.mock import patch

        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
        ):
            self._write(cfg_file, {"agent": {"model": "orig-model"}})
            first = KiroCrewConfig.load()
            assert first.agent.model == "orig-model"
            # In-place mutation, as settings handlers do.
            first.agents["injected"] = KiroCrewAgentConfig(kiro_agent="x")
            first.agent.model = "MUTATED"
            second = KiroCrewConfig.load()
        assert "injected" not in second.agents
        assert second.agent.model == "orig-model"

    def test_mid_read_write_does_not_cache_stale(self, tmp_path: Path) -> None:
        """A write landing during the read->store window must NOT be served as a
        false cache hit: the cache is keyed on the PRE-read fingerprint, which
        won't match the post-write on-disk stat, so the next load re-reads."""
        import os as _os
        from unittest.mock import patch

        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        self._write(cfg_file, {"agent": {"model": "v0"}})

        real_read_text = Path.read_text
        injected = {"done": False}

        def _read_then_write(self_path, *a, **k):
            # Read the OLD content, then simulate a concurrent writer landing
            # before _store_validated_data stamps the cache.
            content = real_read_text(self_path, *a, **k)
            if not injected["done"] and self_path == cfg_file:
                injected["done"] = True
                self._write(cfg_file, {"agent": {"model": "v1"}})
                st = cfg_file.stat()
                _os.utime(
                    cfg_file,
                    ns=(st.st_atime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000),
                )
            return content

        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
            patch.object(Path, "read_text", _read_then_write),
        ):
            first = KiroCrewConfig.load()  # reads v0, writer swaps to v1 mid-read
            # First load returns the v0 it actually read (acceptable).
            assert first.agent.model == "v0"
        # Next load must re-read and see v1 — NOT serve stale v0 from a poisoned cache.
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
        ):
            second = KiroCrewConfig.load()
        assert second.agent.model == "v1", "stale config served from poisoned cache"


# ---------------------------------------------------------------------------
# Dynamic sub-agent sizing config fields (dynamic-subagent-sizing.md §6)
# ---------------------------------------------------------------------------


class TestDynamicSubagentSizingFields:
    """The 5 auto-sizing config fields load, default, and round-trip."""

    def test_defaults(self) -> None:
        cfg = _load_from_dict({})
        a = cfg.agent
        assert a.max_subagents == 0  # auto-size sentinel (0 = auto; Stage 2)
        assert a.subagent_mem_buffer_pct == 20
        assert a.subagent_cost_gb == 0.5
        assert a.subagent_cpu_cost_cores == 1.0
        assert a.subagent_auto_max == 32
        assert a.subagent_spawn_stagger_secs == 2.0

    def test_explicit_values_load(self) -> None:
        cfg = _load_from_dict(
            {
                "agent": {
                    "max_subagents": 0,  # auto sentinel
                    "subagent_mem_buffer_pct": 30,
                    "subagent_cost_gb": 0.4,
                    "subagent_cpu_cost_cores": 0.8,
                    "subagent_auto_max": 24,
                    "subagent_spawn_stagger_secs": 1.5,
                }
            }
        )
        a = cfg.agent
        assert a.max_subagents == 0
        assert a.subagent_mem_buffer_pct == 30
        assert a.subagent_cost_gb == 0.4
        assert a.subagent_cpu_cost_cores == 0.8
        assert a.subagent_auto_max == 24
        assert a.subagent_spawn_stagger_secs == 1.5

    def test_to_dict_round_trip(self) -> None:
        cfg = _load_from_dict(
            {
                "agent": {
                    "max_subagents": 0,
                    "subagent_mem_buffer_pct": 25,
                    "subagent_cost_gb": 0.6,
                    "subagent_cpu_cost_cores": 0.9,
                    "subagent_auto_max": 12,
                    "subagent_spawn_stagger_secs": 3.0,
                }
            }
        )
        agent_dict = cfg.to_dict()["agent"]
        for key in (
            "subagent_mem_buffer_pct",
            "subagent_cost_gb",
            "subagent_cpu_cost_cores",
            "subagent_auto_max",
            "subagent_spawn_stagger_secs",
        ):
            assert key in agent_dict, f"{key} missing from to_dict()"

        # Re-load the serialized form and confirm values survive the round-trip.
        reloaded = _load_from_dict(cfg.to_dict())
        a = reloaded.agent
        assert a.max_subagents == 0
        assert a.subagent_mem_buffer_pct == 25
        assert a.subagent_cost_gb == 0.6
        assert a.subagent_cpu_cost_cores == 0.9
        assert a.subagent_auto_max == 12
        assert a.subagent_spawn_stagger_secs == 3.0


# ---------------------------------------------------------------------------
# Security: load-time clamping of resource-limit knobs (config-loader bound
# bypass pentest). A direct edit of config.json must not exceed the same
# ceilings the dashboard API enforces at write time.
# ---------------------------------------------------------------------------


class TestSecurityBoundClamping:
    """The loader clamps out-of-range resource-limit knobs read from disk."""

    def test_subagent_auto_max_clamped_to_ceiling(self) -> None:
        from kiro_crew.config.loader import SUBAGENT_AUTO_MAX_CEILING

        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            cfg = _load_from_dict({"agent": {"subagent_auto_max": 200}})
        assert cfg.agent.subagent_auto_max == SUBAGENT_AUTO_MAX_CEILING == 64

    def test_subagent_auto_max_floored_to_min(self) -> None:
        """A value below the auto-size floor (3) is clamped UP to 3 with a
        warning, mirroring the > ceiling clamp."""
        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            cfg = _load_from_dict({"agent": {"subagent_auto_max": 1}})
        assert cfg.agent.subagent_auto_max == 3

    def test_max_subagents_clamped_to_ceiling(self) -> None:
        from kiro_crew.config.loader import SUBAGENT_AUTO_MAX_CEILING

        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            cfg = _load_from_dict({"agent": {"max_subagents": 200}})
        assert cfg.agent.max_subagents == SUBAGENT_AUTO_MAX_CEILING == 64

    def test_max_subagents_below_fixed_floor_raised_to_three(self) -> None:
        """An explicit pin of 1 or 2 is normalized UP to the fixed-pin floor (3):
        a sub-3 pin would silently disable auto-sizing and run below the default.
        The auto sentinel (0) and in-range pins (>= 3) are left untouched."""
        from kiro_crew.config.loader import MAX_SUBAGENTS_FIXED_FLOOR

        assert MAX_SUBAGENTS_FIXED_FLOOR == 3
        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            for pinned in (1, 2):
                cfg = _load_from_dict({"agent": {"max_subagents": pinned}})
                assert cfg.agent.max_subagents == 3
            # 0 (auto sentinel) and a valid pin are preserved verbatim.
            assert _load_from_dict({"agent": {"max_subagents": 0}}).agent.max_subagents == 0
            assert _load_from_dict({"agent": {"max_subagents": 3}}).agent.max_subagents == 3
            assert _load_from_dict({"agent": {"max_subagents": 8}}).agent.max_subagents == 8

    def test_subagent_max_turns_clamped_to_ceiling(self) -> None:
        from kiro_crew.config.loader import SUBAGENT_MAX_TURNS_CEILING

        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            cfg = _load_from_dict({"agent": {"subagent_max_turns": 99999}})
        assert cfg.agent.subagent_max_turns == SUBAGENT_MAX_TURNS_CEILING == 200

    def test_pool_size_clamped_to_max(self) -> None:
        from kiro_crew.config.loader import POOL_SIZE_MAX

        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            cfg = _load_from_dict({"session": {"pool_size": 1000}})
        assert cfg.session.pool_size == POOL_SIZE_MAX == 10

    def test_full_pentest_reproduction_clamped(self) -> None:
        """The exact tester payload is clamped, and to_dict() (what the GET API
        serializes) reports the clamped values, not the inflated ones."""
        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            cfg = _load_from_dict(
                {
                    "agent": {
                        "subagent_auto_max": 200,
                        "max_subagents": 200,
                        "subagent_max_turns": 99999,
                    },
                    "session": {"pool_size": 1000},
                }
            )
        assert cfg.agent.subagent_auto_max == 64
        assert cfg.agent.max_subagents == 64
        assert cfg.agent.subagent_max_turns == 200
        assert cfg.session.pool_size == 10

        d = cfg.to_dict()
        assert d["agent"]["subagent_auto_max"] == 64
        assert d["agent"]["max_subagents"] == 64
        assert d["agent"]["subagent_max_turns"] == 200
        assert d["session"]["pool_size"] == 10

    def test_in_range_values_unchanged(self) -> None:
        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event") as mock_event:
            cfg = _load_from_dict(
                {
                    "agent": {
                        "subagent_auto_max": 32,
                        "max_subagents": 8,
                        "subagent_max_turns": 150,
                    },
                    "session": {"pool_size": 4},
                }
            )
        assert cfg.agent.subagent_auto_max == 32
        assert cfg.agent.max_subagents == 8
        assert cfg.agent.subagent_max_turns == 150
        assert cfg.session.pool_size == 4
        mock_event.assert_not_called()

    def test_boundary_values_not_clamped(self) -> None:
        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event") as mock_event:
            cfg = _load_from_dict(
                {
                    "agent": {"subagent_auto_max": 64, "subagent_max_turns": 200},
                    "session": {"pool_size": 10},
                }
            )
        assert cfg.agent.subagent_auto_max == 64
        assert cfg.agent.subagent_max_turns == 200
        assert cfg.session.pool_size == 10
        mock_event.assert_not_called()

    def test_clamp_logs_warning(self) -> None:
        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            _, logs = _load_from_dict_with_logs({"agent": {"subagent_auto_max": 200}})
        assert any(
            "subagent_auto_max" in m and "out of range" in m for m in logs
        ), f"expected clamp warning, got: {logs}"

    def test_clamp_emits_security_event(self) -> None:
        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event") as mock_event:
            _load_from_dict({"agent": {"subagent_auto_max": 200}})
        mock_event.assert_called_once()
        args = mock_event.call_args.args
        assert args[0] == "agent.subagent_auto_max"
        assert args[1] == 200
        assert args[2] == 64

    def test_non_int_value_not_clamped(self) -> None:
        """The clamp skips non-int values, leaving them exactly as-is. Asserted
        against ``_clamp_security_bounds`` directly so the outcome is
        deterministic regardless of jsonschema availability."""
        from kiro_crew.config.loader import _clamp_security_bounds

        data = {"agent": {"subagent_max_turns": "lots"}}
        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event") as mock_event:
            _clamp_security_bounds(data)
        assert data["agent"]["subagent_max_turns"] == "lots"
        mock_event.assert_not_called()

    def test_bool_value_not_clamped(self) -> None:
        """A JSON true/false (bool is an int subclass) is not a numeric bound
        value: the clamp leaves it untouched and fires no event."""
        from kiro_crew.config.loader import _clamp_security_bounds

        data = {"agent": {"max_subagents": True}}
        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event") as mock_event:
            _clamp_security_bounds(data)
        assert data["agent"]["max_subagents"] is True
        mock_event.assert_not_called()

    def test_log_config_clamp_event_is_best_effort(self) -> None:
        from kiro_crew.config.loader import _log_config_clamp_event

        with unittest.mock.patch("kiro_crew.sel.sel", side_effect=RuntimeError("SEL down")):
            _log_config_clamp_event("agent.subagent_auto_max", 200, 64, 1, 64)


class TestConfigWriteProtection:
    """config.json / config.local.json are WRITE-protected (reads allowed)."""

    def test_config_json_is_write_protected(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_write_path("~/.kirocrew/config.json")
        assert is_sensitive_write_path(str(Path.home() / ".kirocrew" / "config.json"))

    def test_config_local_json_is_write_protected(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_write_path("~/.kirocrew/config.local.json")
        assert is_sensitive_write_path(str(Path.home() / ".kirocrew" / "config.local.json"))

    def test_config_json_reads_still_allowed(self) -> None:
        from kiro_crew.security import is_sensitive_bash_command, is_sensitive_path

        assert is_sensitive_path("~/.kirocrew/config.json") is False
        assert is_sensitive_bash_command("cat ~/.kirocrew/config.json") is None

    def test_write_protection_superset_of_sensitive(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_write_path("~/.aws/credentials")
        assert is_sensitive_write_path("~/.kirocrew/security_policy.json")

    def test_non_config_kirocrew_file_not_write_protected(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_write_path("~/.kirocrew/sessions.db") is False


class TestConfigEditToolBlocked:
    """The file-edit tool gate (HookManager.on_tool_call) denies edits to config."""

    def _hooks(self):
        from kiro_crew.hooks import HookManager, HooksConfig

        return HookManager(HooksConfig())

    def test_edit_config_json_denied(self) -> None:
        result = self._hooks().on_tool_call(
            "Editing config.json",
            tool_kind="edit",
            raw_params={"path": "~/.kirocrew/config.json"},
        )
        assert result.action == "deny"
        assert "write-protected config" in (result.reason or "")

    def test_read_config_json_not_denied_by_edit_gate(self) -> None:
        result = self._hooks().on_tool_call(
            "config.json",
            tool_kind="read",
            raw_params={"path": "~/.kirocrew/config.json"},
        )
        assert result.action != "deny"

    def test_edit_normal_workspace_file_allowed(self) -> None:
        result = self._hooks().on_tool_call(
            "Editing notes.md",
            tool_kind="edit",
            raw_params={"path": "~/.kirocrew/workspace/notes.md"},
        )
        assert result.action != "deny"


class TestTelegramAllowedUserIdsGuard:
    """Finding: a non-list allowed_user_ids must not iterate char-by-char."""

    def test_string_value_yields_empty_not_char_list(self) -> None:
        cfg = _load_from_dict({"telegram": {"enabled": True, "allowed_user_ids": "12345"}})
        # A hand-edited string must NOT become [1, 2, 3, 4, 5]; treat non-list
        # as empty (fail closed).
        assert cfg.telegram.allowed_user_ids == []

    def test_real_list_is_preserved(self) -> None:
        cfg = _load_from_dict(
            {"telegram": {"enabled": True, "allowed_user_ids": [8743158320, -100]}}
        )
        assert cfg.telegram.allowed_user_ids == [8743158320, -100]

    def test_malformed_entries_skipped_not_crash(self) -> None:
        # "--100"/"1.5"/"abc" would raise in int() and crash config load; a
        # bool must not sneak in. Only clean base-10 ints survive.
        cfg = _load_from_dict(
            {
                "telegram": {
                    "enabled": True,
                    "allowed_user_ids": ["--100", "1.5", "abc", 42, "-7", True],
                }
            }
        )
        assert cfg.telegram.allowed_user_ids == [42, -7]

    def test_non_numeric_soft_threshold_defaults_not_crash(self) -> None:
        # "abc" would raise in int() and crash config load; must fall back to
        # the default (80) instead.
        cfg = _load_from_dict({"telegram": {"enabled": True, "soft_threshold_pct": "abc"}})
        assert cfg.telegram.soft_threshold_pct == 80


class TestMessagingConfigValidation:
    def test_normalizes_bad_scope_mode_and_clamps_resets(self) -> None:
        from kiro_crew.config.loader import MessagingConfig

        c = MessagingConfig(
            dm_scope="bogus",
            queue_mode="nope",
            idle_reset_minutes=-5,
            daily_reset_hour=99,
        )
        assert c.dm_scope == "per-channel-peer"
        assert c.queue_mode == "steer"
        assert c.idle_reset_minutes == 0
        assert c.daily_reset_hour == -1

    def test_keeps_valid_values(self) -> None:
        from kiro_crew.config.loader import MessagingConfig

        c = MessagingConfig(
            dm_scope="unified",
            queue_mode="queue",
            idle_reset_minutes=30,
            daily_reset_hour=4,
        )
        assert c.dm_scope == "unified"
        assert c.queue_mode == "queue"
        assert c.idle_reset_minutes == 30
        assert c.daily_reset_hour == 4

    def test_load_hydrates_messaging_fields_from_config(self) -> None:
        # Guards the load() gap: fields must be read from config.json, not just
        # defaulted. Without hydration these would all be the defaults.
        cfg = _load_from_dict(
            {
                "messaging": {
                    "dm_scope": "unified",
                    "queue_mode": "queue",
                    "idle_reset_minutes": 30,
                    "daily_reset_hour": 4,
                }
            }
        )
        assert cfg.messaging.dm_scope == "unified"
        assert cfg.messaging.queue_mode == "queue"
        assert cfg.messaging.idle_reset_minutes == 30
        assert cfg.messaging.daily_reset_hour == 4


class TestAppsAllowThirdParty:
    """agent.apps_allow_third_party gate (CSE SEC-012 hard off switch)."""

    def test_defaults_to_true(self) -> None:
        """Fresh dataclass and empty-config load both default to True."""
        assert AgentConfig().apps_allow_third_party is True
        cfg = _load_from_dict({})
        assert cfg.agent.apps_allow_third_party is True

    def test_false_round_trips_from_config(self) -> None:
        """A false value in config.json is parsed onto agent.apps_allow_third_party."""
        cfg = _load_from_dict({"agent": {"apps_allow_third_party": False}})
        assert cfg.agent.apps_allow_third_party is False
        # And survives serialization back out.
        assert cfg.to_dict()["agent"]["apps_allow_third_party"] is False


def test_heartbeat_default_deliver_default_is_slack():
    """Absent config -> backward-compatible 'slack' default."""
    cfg = _load_from_dict({})
    assert cfg.heartbeat.default_deliver == "slack"


def test_heartbeat_default_deliver_accepts_dashboard():
    cfg = _load_from_dict({"heartbeat": {"default_deliver": "dashboard"}})
    assert cfg.heartbeat.default_deliver == "dashboard"


def test_heartbeat_default_deliver_invalid_falls_back_to_slack():
    """Any value outside {slack, dashboard} normalizes to the safe default."""
    cfg = _load_from_dict({"heartbeat": {"default_deliver": "carrier-pigeon"}})
    assert cfg.heartbeat.default_deliver == "slack"


class TestKnowledgePoolIdleTtl:
    """``knowledge.pool_idle_ttl_secs`` parsing: default, override, explicit 0,
    and rejection of negative / bool / typed-wrong values back to the default."""

    def test_default_is_300(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.knowledge.pool_idle_ttl_secs == 300

    def test_reads_value(self) -> None:
        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": 60}})
        assert cfg.knowledge.pool_idle_ttl_secs == 60

    def test_zero_preserved(self) -> None:
        # 0 is a valid explicit "keep warm forever" opt-out, not a fallback.
        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": 0}})
        assert cfg.knowledge.pool_idle_ttl_secs == 0

    def test_negative_falls_back_to_default(self) -> None:
        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": -1}})
        assert cfg.knowledge.pool_idle_ttl_secs == 300

    def test_bool_falls_back_to_default(self) -> None:
        # bool is an int subclass; must not read True as a 1s TTL.
        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": True}})
        assert cfg.knowledge.pool_idle_ttl_secs == 300

    def test_string_falls_back_to_default(self) -> None:
        cfg = _load_from_dict({"knowledge": {"pool_idle_ttl_secs": "60"}})
        assert cfg.knowledge.pool_idle_ttl_secs == 300


class TestSaveRoundTripPreservesAllSections:
    """to_dict() (which save() writes as the ENTIRE config.json) previously
    omitted knowledge/heartbeat/snapshot_dir/watchdog, so any save silently
    deleted those sections from disk. save() fires from many routine paths —
    the theme PUT handler, the AIM auto-update toggle, and (worst) the one-shot
    write-back migration inside load() itself when a config lacks an "agents"
    map. So a user who hand-wrote e.g. {"knowledge": {"pool_idle_ttl_secs": 0}}
    lost it the first time the gateway started."""

    def test_to_dict_includes_previously_dropped_sections(self) -> None:
        cfg = KiroCrewConfig()
        td = cfg.to_dict()
        for key in ("knowledge", "heartbeat", "snapshot_dir", "watchdog"):
            assert key in td, f"to_dict() dropped {key} — save() would delete it"

    def test_migration_save_preserves_hand_written_sections(self) -> None:
        # The finding's exact repro: a config with no "agents" key triggers the
        # write-back migration save() inside load(); the hand-written sections
        # must survive it.
        import json
        import tempfile
        import unittest.mock
        from pathlib import Path

        cfg_data = {
            "knowledge": {"pool_idle_ttl_secs": 0},
            "heartbeat": {"default_deliver": "dashboard"},
            "snapshot_dir": "/tmp/snaps",
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg_data, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                KiroCrewConfig.load()  # migration write-back save fires
            after = json.loads(tmp.read_text())
            assert after.get("knowledge", {}).get("pool_idle_ttl_secs") == 0
            assert after.get("heartbeat", {}).get("default_deliver") == "dashboard"
            assert after.get("snapshot_dir") == "/tmp/snaps"
        finally:
            tmp.unlink(missing_ok=True)


class TestOrchestratorWatchdogThemeAreParsed:
    """load() must actually parse the orchestrator, watchdog, and dashboard-theme
    fields from config.json. They were advertised in config-baseline.json and
    served by /api/config/schema, and real consumers read them
    (acp/session_handle.py .watchdog, dashboard/chat_orchestrator.py
    .orchestrator.stage_timeout_seconds), but the cls(...) construction passed no
    orchestrator=/watchdog= kwargs and DashboardConfig omitted theme_mode/
    theme_color/onboarded — so config values were silently ignored (defaults
    always won) and the server-authoritative theme never round-tripped (the
    onboarding modal re-armed on every gateway restart)."""

    def test_orchestrator_stage_timeout_is_parsed(self) -> None:
        cfg = _load_from_dict({"orchestrator": {"stage_timeout_seconds": 60}})
        assert cfg.orchestrator.stage_timeout_seconds == 60

    def test_watchdog_fields_are_parsed(self) -> None:
        cfg = _load_from_dict(
            {"watchdog": {"tool_stall_hard_cap_secs": 61.0, "check_after_secs": 5.0}}
        )
        assert cfg.watchdog.tool_stall_hard_cap_secs == 61.0
        assert cfg.watchdog.check_after_secs == 5.0

    def test_dashboard_theme_fields_are_parsed(self) -> None:
        cfg = _load_from_dict(
            {"dashboard": {"theme_mode": "dark", "theme_color": "#ff0000", "onboarded": True}}
        )
        assert cfg.dashboard.theme_mode == "dark"
        assert cfg.dashboard.theme_color == "#ff0000"
        assert cfg.dashboard.onboarded is True

    def test_absent_sections_use_defaults(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.orchestrator.stage_timeout_seconds == 1800
        assert cfg.watchdog.tool_stall_hard_cap_secs == 10800.0
        assert cfg.dashboard.theme_mode == ""
        assert cfg.dashboard.onboarded is False

    def test_bad_values_fall_back_without_crashing(self) -> None:
        cfg = _load_from_dict(
            {"watchdog": {"check_after_secs": "junk"}, "orchestrator": {"stage_timeout_seconds": "x"}}
        )
        assert cfg.watchdog.check_after_secs == 60.0
        assert cfg.orchestrator.stage_timeout_seconds == 1800


class TestMalformedConfigNeverBricksLoad:
    """load() must honor its documented "fall back to defaults, never raise"
    contract for malformed-but-writable values reachable via `kirocrew config
    set` (cli_config._parse_value returns the raw string, _dict_set accepts any
    existing key with no type/schema check). Pre-fix three classes crashed
    load() with an uncaught exception, and because `config get/set` and the
    gateway all call load() first, the CLI recovery path was bricked too — the
    user had to hand-edit the JSON.

    (1) a non-dict section value (taskrunner was the only unguarded section)
    (2) a non-dict `slack` value (raw re-reads bypassed the guarded slack_data)
    (3) a non-numeric string where an int/float was expected (bare coercions)
    """

    def test_non_dict_taskrunner_section_falls_back(self) -> None:
        # Pre-fix: AttributeError: 'str' object has no attribute 'get'.
        cfg = _load_from_dict({"taskrunner": "oops"})
        assert cfg.taskrunner is not None

    def test_non_dict_slack_section_falls_back(self) -> None:
        # Pre-fix: AttributeError from the raw data.get("slack", {}).get(...)
        # re-reads that bypassed the guarded slack_data local.
        cfg = _load_from_dict({"slack": "off"})
        assert cfg.slack_dm_activation == "always"

    def test_non_numeric_int_field_falls_back(self) -> None:
        # Pre-fix: ValueError: invalid literal for int() with base 10: 'two'.
        cfg = _load_from_dict({"session": {"pool_size": "two"}})
        assert cfg.session.pool_size == 2

    def test_non_numeric_float_field_falls_back(self) -> None:
        cfg = _load_from_dict({"agent": {"subagent_cost_gb": "lots"}})
        assert cfg.agent.subagent_cost_gb == 0.5

    def test_numeric_string_still_coerces(self) -> None:
        # A numeric string is a valid coercion — must NOT fall back.
        cfg = _load_from_dict({"session": {"pool_size": "5"}})
        assert cfg.session.pool_size == 5

    def test_well_formed_values_still_parse(self) -> None:
        # The fix must not regress the happy path.
        cfg = _load_from_dict(
            {"session": {"pool_size": 7}, "slack": {"observe_max_messages": 50}}
        )
        assert cfg.session.pool_size == 7
        assert cfg.observe_max_messages == 50

    def test_remaining_numeric_coercions_fall_back(self) -> None:
        # Review follow-up: these load-time coercions still used bare
        # int()/float() and would raise on a non-numeric config value — the exact
        # crash class this fix addresses. Each must now fall back to its default.
        defaults = _load_from_dict({})
        cfg = _load_from_dict(
            {
                "agent": {"subagent_spawn_stagger_secs": "soon"},
                "session": {"watchdog_rss_max_mb": "big"},
                "cron_history": {"cron_max_records_per_job": "many"},
                "instances": {
                    "tunnel_base_port": "port",
                    "max_recovery_attempts": "lots",
                    "recover_backoff_max_secs": "slow",
                    "probe_failure_threshold": "few",
                },
            }
        )
        assert (
            cfg.agent.subagent_spawn_stagger_secs
            == defaults.agent.subagent_spawn_stagger_secs
        )
        assert (
            cfg.session.watchdog_rss_max_mb == defaults.session.watchdog_rss_max_mb
        )
        assert (
            cfg.cron_history.cron_max_records_per_job
            == defaults.cron_history.cron_max_records_per_job
        )
        assert cfg.instances.tunnel_base_port == defaults.instances.tunnel_base_port
        assert (
            cfg.instances.max_recovery_attempts
            == defaults.instances.max_recovery_attempts
        )
        assert (
            cfg.instances.recover_backoff_max_secs
            == defaults.instances.recover_backoff_max_secs
        )
        assert (
            cfg.instances.probe_failure_threshold
            == defaults.instances.probe_failure_threshold
        )
