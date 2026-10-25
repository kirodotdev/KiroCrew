"""What ``load()`` does with a value it cannot use as written.

A value the schema rejects falls back to the field default, which the property
tests check over generated configs alongside the ``load()``/``to_dict()`` round
trip; a malformed value never crashes or bricks the load; and a numeric knob read
from disk is clamped into the range the dashboard enforces on write: the sub-agent
limits, the pool size, the chat-entry cache bounds, the session-start timeout, the
autocompact threshold and the empty-response ladder. The pool size's single
default, archive retention's disable sentinel and the MCP probe timeout's fallback
are pinned here too.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import re
import tempfile
import unittest.mock
from pathlib import Path

import pytest
from config_loader_helpers import _kirocrew_config_st, _load_from_dict, _load_from_dict_with_logs
from hypothesis import assume, given, settings
from hypothesis import strategies as st

import kiro_crew.config.loader as loader_module
from kiro_crew.config.loader import (
    _HAS_JSONSCHEMA,
    AgentConfig,
    DashboardConfig,
    KiroCrewConfig,
    MemoryConfig,
    SessionConfig,
    SlackConfig,
    coerce_config_field,
)

# Fields with enum constraints and their allowed values
_ENUM_FIELDS: list[tuple[str, str, list[str]]] = [
    ("agent", "approval_mode", ["auto", "interactive"]),
    ("agent", "provider", ["acp"]),
    ("agent", "sandbox", ["auto", "strict", "off"]),
    ("agent", "log_level", ["DEBUG", "INFO", "WARNING", "ERROR"]),
    ("memory", "embedding_provider", ["llama_cpp"]),
]

# Top-level keys recognised by the schema. Keep the property strategy aligned
# with the loader's emitted config sections as new sections are added.
_KNOWN_TOP_KEYS = set(loader_module._KNOWN_CONFIG_SECTIONS)

# Skip marker for tests that require jsonschema validation
_requires_jsonschema = pytest.mark.skipif(
    not _HAS_JSONSCHEMA,
    reason="jsonschema not available — validation tests require it",
)


def _load_absent_config() -> KiroCrewConfig:
    """Load with NEITHER config.json nor config.local.json present.

    This is the only path that reaches the dataclass field defaults, so it is
    what a default must be compared against.
    """
    with tempfile.TemporaryDirectory() as d:
        missing = Path(d) / "config.json"
        missing_local = Path(d) / "config.local.json"
        with (
            unittest.mock.patch(
                "kiro_crew.config.loader.config_path",
                return_value=missing,
            ),
            unittest.mock.patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=missing_local,
            ),
        ):
            return KiroCrewConfig.load()


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


def _default_config() -> KiroCrewConfig:
    """Return a default KiroCrewConfig for comparison."""
    return KiroCrewConfig()


class TestMalformedConfigValuesNeverCrashLoad:
    """Round-2 hardening: several config parse sites coerced values with a bare
    .upper()/int()/list()/set()/.items() and no guard. jsonschema is optional
    (absent in the shipped runtime), so a hand-edited config.json with a wrongly
    typed value reached the parse and crashed KiroCrewConfig.load() outright —
    bricking every caller. Each must now degrade to the default instead."""

    def test_non_string_log_level_falls_back(self):
        # agent.log_level=42 -> "42".upper()? no: 42 has no .upper() -> crash.
        loaded = _load_from_dict({"agent": {"log_level": 42}})
        assert loaded.agent.log_level == "WARNING"
        # a valid string still applies (and uppercases)
        assert _load_from_dict({"agent": {"log_level": "debug"}}).agent.log_level == "DEBUG"

    def test_non_list_subagent_cwd_allowed_roots_falls_back(self):
        # This fallback is the value REAL configs get: from_dict always passes an
        # explicit value, so an absent key lands here too. It must stay at the
        # historical four roots, and the field default now reads the same
        # constant instead of stating a narrower two-root list of its own.
        fallback = ["~/workspace", "~/workspaces", "~/workplace", "~/workplaces"]
        assert loader_module.DEFAULT_CWD_ALLOWED_ROOTS == fallback
        # int would crash list(); a string would char-split silently.
        assert (
            _load_from_dict(
                {"agent": {"subagent_cwd_allowed_roots": 5}}
            ).agent.subagent_cwd_allowed_roots
            == fallback
        )
        assert (
            _load_from_dict(
                {"agent": {"subagent_cwd_allowed_roots": "abc"}}
            ).agent.subagent_cwd_allowed_roots
            == fallback
        )
        # a real list still parses (non-str entries dropped)
        got = _load_from_dict(
            {"agent": {"subagent_cwd_allowed_roots": ["~/x", 9]}}
        ).agent.subagent_cwd_allowed_roots
        assert got == ["~/x"]

    def test_non_list_trusted_bot_ids_falls_back(self):
        # set() on a non-iterable (int) crashes; on a string it char-splits.
        assert _load_from_dict({"slack": {"trusted_bot_ids": 5}}).slack.trusted_bot_ids == set()
        assert (
            _load_from_dict({"slack": {"trusted_bot_ids": "B123"}}).slack.trusted_bot_ids == set()
        )
        assert _load_from_dict({"slack": {"trusted_bot_ids": ["B1", 2]}}).slack.trusted_bot_ids == {
            "B1"
        }

    def test_non_dict_reactions_falls_back(self):
        # .items() on a non-dict (list) crashes.
        assert _load_from_dict({"slack": {"reactions": ["x"]}}).slack.reactions == {}
        assert _load_from_dict({"slack": {"reactions": {"eyes": "👀"}}}).slack.reactions == {
            "eyes": "👀"
        }

    def test_non_finite_wecom_thresholds_fall_back(self):
        # JSON/TOML parse 1e1000 to float("inf"); int(inf) raises OverflowError,
        # not ValueError, so the old _safe_int guard still crashed load().
        cfg = _load_from_dict(
            {"wecom": {"soft_threshold_pct": float("inf"), "hard_threshold_pct": float("nan")}}
        )
        assert cfg.wecom.soft_threshold_pct == 80
        # NaN: int(nan) raises ValueError, already caught — pin it anyway.
        assert cfg.wecom.hard_threshold_pct == 95

    def test_non_numeric_wecom_thresholds_fall_back(self):
        cfg = _load_from_dict({"wecom": {"soft_threshold_pct": "lots", "hard_threshold_pct": None}})
        assert cfg.wecom.soft_threshold_pct == 80
        assert cfg.wecom.hard_threshold_pct == 95

    def test_non_list_skills_extra_paths_falls_back(self):
        assert _load_from_dict({"skills": {"extra_paths": 5}}).skills.extra_paths == []
        assert _load_from_dict({"skills": {"extra_paths": ["/a", 9]}}).skills.extra_paths == ["/a"]


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
        # An unset default template is carried from the roster default's row
        # on load (MIGRATE_STAR_DEFAULT_TEMPLATE), so the round trip holds for
        # the field once that one-shot carry is applied.
        from kiro_crew.config.loader import dispatch_kiro_agent
        from kiro_crew.config.migration import star_default_template_due

        carried = star_default_template_due(
            config.agent.default_agent,
            loaded.default_agent,
            {name: dataclasses.asdict(row) for name, row in loaded.agents.items()},
        )
        expected_template = (
            dispatch_kiro_agent(carried) if carried is not None else config.agent.default_agent
        )
        assert loaded.agent.default_agent == expected_template
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
        assert cfg.agent.subagent_max_turns == SUBAGENT_MAX_TURNS_CEILING == 1000

    def test_subagent_timeout_zero_sentinel_survives_the_clamp(self) -> None:
        """``0`` means "use the default", so a MIN floor must not rewrite it.

        Clamping it to the 60s floor hands a healthy subagent a one-minute
        deadline -- the opposite of what the field's own help text promises.
        Asserted for the int form (the raw-dict clamp) and the numeric-string
        form (which that clamp skips and only the coercion site bounds).
        """
        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            assert (
                _load_from_dict({"agent": {"subagent_timeout_secs": 0}}).agent.subagent_timeout_secs
                == 0
            )
            assert (
                _load_from_dict(
                    {"agent": {"subagent_timeout_secs": "0"}}
                ).agent.subagent_timeout_secs
                == 0
            )
            # A non-zero value below the floor is still raised to it.
            assert (
                _load_from_dict({"agent": {"subagent_timeout_secs": 5}}).agent.subagent_timeout_secs
                == 60
            )
            assert (
                _load_from_dict(
                    {"agent": {"subagent_timeout_secs": "5"}}
                ).agent.subagent_timeout_secs
                == 60
            )

    def test_subagent_timeout_clamped_to_bounds(self) -> None:
        from kiro_crew.config.loader import SUBAGENT_TIMEOUT_MAX, SUBAGENT_TIMEOUT_MIN

        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            hi = _load_from_dict({"agent": {"subagent_timeout_secs": 999999}})
            lo = _load_from_dict({"agent": {"subagent_timeout_secs": 1}})
        assert hi.agent.subagent_timeout_secs == SUBAGENT_TIMEOUT_MAX == 86400
        assert lo.agent.subagent_timeout_secs == SUBAGENT_TIMEOUT_MIN == 60

    def test_pool_size_clamped_to_max(self) -> None:
        from kiro_crew.config.loader import POOL_SIZE_MAX

        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            cfg = _load_from_dict({"session": {"pool_size": 1000}})
        assert cfg.session.pool_size == POOL_SIZE_MAX == 10

    def test_chat_entry_cache_bounds_default_when_absent(self) -> None:
        """Omitted from config.json, the entry-cache bounds are byte-identical
        to the previous hardcoded constants (4096 entries / 32 MiB)."""
        cfg = _load_from_dict({})
        assert cfg.dashboard.chat_entry_cache_max_entries == 4096
        assert cfg.dashboard.chat_entry_cache_max_bytes == 32 * 1024 * 1024

    def test_chat_entry_cache_max_entries_clamped_at_both_ends(self) -> None:
        from kiro_crew.config.loader import (
            CHAT_ENTRY_CACHE_ENTRIES_MAX,
            CHAT_ENTRY_CACHE_ENTRIES_MIN,
        )

        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            low = _load_from_dict({"dashboard": {"chat_entry_cache_max_entries": 1}})
            high = _load_from_dict({"dashboard": {"chat_entry_cache_max_entries": 10**9}})
        assert low.dashboard.chat_entry_cache_max_entries == CHAT_ENTRY_CACHE_ENTRIES_MIN == 256
        assert high.dashboard.chat_entry_cache_max_entries == CHAT_ENTRY_CACHE_ENTRIES_MAX == 262144

    def test_chat_entry_cache_max_bytes_clamped_at_both_ends(self) -> None:
        from kiro_crew.config.loader import (
            CHAT_ENTRY_CACHE_BYTES_MAX,
            CHAT_ENTRY_CACHE_BYTES_MIN,
        )

        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            low = _load_from_dict({"dashboard": {"chat_entry_cache_max_bytes": 1024}})
            high = _load_from_dict({"dashboard": {"chat_entry_cache_max_bytes": 10**12}})
        assert low.dashboard.chat_entry_cache_max_bytes == CHAT_ENTRY_CACHE_BYTES_MIN
        assert CHAT_ENTRY_CACHE_BYTES_MIN == 4 * 1024 * 1024
        assert high.dashboard.chat_entry_cache_max_bytes == CHAT_ENTRY_CACHE_BYTES_MAX
        assert CHAT_ENTRY_CACHE_BYTES_MAX == 512 * 1024 * 1024

    def test_chat_entry_cache_bounds_in_range_preserved(self) -> None:
        """A deliberate in-range setting is preserved verbatim -- the clamp must
        not be a blanket overwrite."""
        cfg = _load_from_dict(
            {
                "dashboard": {
                    "chat_entry_cache_max_entries": 16384,
                    "chat_entry_cache_max_bytes": 64 * 1024 * 1024,
                }
            }
        )
        assert cfg.dashboard.chat_entry_cache_max_entries == 16384
        assert cfg.dashboard.chat_entry_cache_max_bytes == 64 * 1024 * 1024

    def test_session_start_timeout_default(self) -> None:
        """Omitted from config.json, the budget is the built-in 90s default."""
        cfg = _load_from_dict({})
        assert cfg.agent.session_start_timeout_secs == 90

    def test_session_start_timeout_custom_in_range(self) -> None:
        """An in-range value from disk is preserved verbatim."""
        cfg = _load_from_dict({"agent": {"session_start_timeout_secs": 240}})
        assert cfg.agent.session_start_timeout_secs == 240

    def test_session_start_timeout_floored_to_min(self) -> None:
        """A value below the floor is clamped UP: a session-start budget under
        the backend's 30s OAuth authorization wait recreates the race the
        dedicated budget exists to prevent."""
        from kiro_crew.config.loader import SESSION_START_TIMEOUT_MIN

        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            cfg = _load_from_dict({"agent": {"session_start_timeout_secs": 10}})
        assert cfg.agent.session_start_timeout_secs == SESSION_START_TIMEOUT_MIN == 90

    def test_session_start_timeout_clamped_to_max(self) -> None:
        with unittest.mock.patch("kiro_crew.config.loader._log_config_clamp_event"):
            cfg = _load_from_dict({"agent": {"session_start_timeout_secs": 99999}})
        from kiro_crew.config.loader import SESSION_START_TIMEOUT_MAX

        assert cfg.agent.session_start_timeout_secs == SESSION_START_TIMEOUT_MAX == 900

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
        assert cfg.agent.subagent_max_turns == 1000
        assert cfg.session.pool_size == 10

        d = cfg.to_dict()
        assert d["agent"]["subagent_auto_max"] == 64
        assert d["agent"]["max_subagents"] == 64
        assert d["agent"]["subagent_max_turns"] == 1000
        assert d["session"]["pool_size"] == 10

    def test_numeric_string_ceiling_is_still_enforced_at_extraction(self) -> None:
        """``_clamp_security_bounds`` runs over the RAW dict and deliberately
        skips non-int values (see ``test_non_int_value_not_clamped``) --
        ``_safe_int``'s own docstring says clamping at the coercion site is
        what actually enforces the range for a numeric STRING that slips past
        it. ``max_subagents``/``subagent_max_turns`` reach the
        dataclass with NO coercion at all, and ``subagent_auto_max``/
        ``pool_size`` are ``_safe_int``-coerced but without bounds -- all
        four let a numeric-string value bypass the declared ceiling entirely."""
        from kiro_crew.config.loader import (
            POOL_SIZE_MAX,
            SUBAGENT_AUTO_MAX_CEILING,
            SUBAGENT_MAX_TURNS_CEILING,
        )

        cfg = _load_from_dict(
            {
                "agent": {
                    "max_subagents": "200",
                    "subagent_max_turns": "99999",
                    "subagent_auto_max": "500",
                },
                "session": {"pool_size": "1000"},
            }
        )
        assert cfg.agent.max_subagents == SUBAGENT_AUTO_MAX_CEILING == 64
        assert cfg.agent.subagent_max_turns == SUBAGENT_MAX_TURNS_CEILING == 1000
        assert cfg.agent.subagent_auto_max == SUBAGENT_AUTO_MAX_CEILING == 64
        assert cfg.session.pool_size == POOL_SIZE_MAX == 10
        for v in (
            cfg.agent.max_subagents,
            cfg.agent.subagent_max_turns,
            cfg.agent.subagent_auto_max,
            cfg.session.pool_size,
        ):
            assert isinstance(v, int) and not isinstance(v, bool)

    def test_numeric_string_in_range_still_parses(self) -> None:
        """A well-formed numeric string within bounds must keep working --
        the fix must not turn an already-valid legacy string value into
        the default."""
        cfg = _load_from_dict(
            {
                "agent": {
                    "max_subagents": "8",
                    "subagent_max_turns": "150",
                    "subagent_auto_max": "32",
                },
                "session": {"pool_size": "4"},
            }
        )
        assert cfg.agent.max_subagents == 8
        assert cfg.agent.subagent_max_turns == 150
        assert cfg.agent.subagent_auto_max == 32
        assert cfg.session.pool_size == 4

    def test_subagent_max_turns_as_string_no_longer_crashes_a_turn_limit_check(
        self,
    ) -> None:
        """The concrete downstream failure this bug caused: subagent.py compares
        ``turns > turn_limit`` where turns is always a real int -- a string
        subagent_max_turns reaching that comparison raised TypeError on the
        first tool call of every subagent. Assert the loaded value is always
        int-comparable."""
        cfg = _load_from_dict({"agent": {"subagent_max_turns": "50"}})
        assert not (cfg.agent.subagent_max_turns > 999)  # must not raise TypeError

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
                    "agent": {"subagent_auto_max": 64, "subagent_max_turns": 1000},
                    "session": {"pool_size": 10},
                }
            )
        assert cfg.agent.subagent_auto_max == 64
        assert cfg.agent.subagent_max_turns == 1000
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


class TestAutocompactPctLoadClamp:
    """session.autocompact_pct clamps into the documented 5-90 range at load
    time.

    The dashboard config API rejected out-of-range writes but a hand-edited
    config.json loaded verbatim: at 500 the autocompactor trigger
    (``pct >= autocompact_pct``) can never fire, silently disabling the
    context-window backstop, and at 0 or below it fires on every turn.
    """

    def test_above_range_clamped_to_max(self) -> None:
        from kiro_crew.config.loader import AUTOCOMPACT_PCT_MAX

        cfg = _load_from_dict({"session": {"autocompact_pct": 500}})
        assert cfg.session.autocompact_pct == AUTOCOMPACT_PCT_MAX == 90.0

    def test_negative_clamped_to_min(self) -> None:
        from kiro_crew.config.loader import AUTOCOMPACT_PCT_MIN

        cfg = _load_from_dict({"session": {"autocompact_pct": -10}})
        assert cfg.session.autocompact_pct == AUTOCOMPACT_PCT_MIN == 5.0

    def test_zero_clamped_to_min(self) -> None:
        """0 would fire auto-compaction on every turn; clamped UP to the floor."""
        cfg = _load_from_dict({"session": {"autocompact_pct": 0}})
        assert cfg.session.autocompact_pct == 5.0

    def test_in_range_value_preserved(self) -> None:
        cfg = _load_from_dict({"session": {"autocompact_pct": 75.5}})
        assert cfg.session.autocompact_pct == 75.5

    def test_default_when_omitted(self) -> None:
        """An omitted key takes the module default, not a clamp bound. Asserted
        against the constant rather than a literal so the two cannot drift: a
        ``load()`` fallback restated as its own literal still fails here."""
        from kiro_crew.config.loader import DEFAULT_AUTOCOMPACT_PCT

        cfg = _load_from_dict({})
        assert cfg.session.autocompact_pct == DEFAULT_AUTOCOMPACT_PCT

    def test_load_range_and_dashboard_validator_share_one_constant(self) -> None:
        """Drift guard: the dashboard write-gate bounds for
        ``session.autocompact_pct`` must BE the loader's clamp constants — the
        same objects, not two sets of literals that can drift apart (the drift
        is exactly how the load path lost the range in the first place)."""
        from kiro_crew.config.loader import AUTOCOMPACT_PCT_MAX, AUTOCOMPACT_PCT_MIN
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        spec = _EDITABLE_CONFIG["session.autocompact_pct"]
        assert spec["type"] == "float"
        assert spec["min"] is AUTOCOMPACT_PCT_MIN
        assert spec["max"] is AUTOCOMPACT_PCT_MAX
        assert (AUTOCOMPACT_PCT_MIN, AUTOCOMPACT_PCT_MAX) == (5.0, 90.0)


class TestPoolSizeDefaultSingleSource:
    """``session.pool_size`` has TWO independent paths to its default and they
    must not be able to disagree.

    A home with no ``config.json`` at all constructs ``SessionConfig()`` and
    takes the dataclass field default; a home WITH a ``config.json`` that omits
    the key reaches ``load()``'s file-parse fallback instead. An install that has
    run setup is always the second case, so a literal at the parse site decides
    the real default for essentially every host while the field default decides
    what the schema, the docs, and a never-set-up home report. The disagreement
    is invisible on disk: nothing serializes the difference, and each pooled slot
    it silently buys is a kiro-cli process plus that agent's MCP stdio servers.
    """

    def test_field_default_reads_the_shared_constant(self) -> None:
        assert loader_module.DEFAULT_POOL_SIZE == 0
        assert (
            SessionConfig.__dataclass_fields__["pool_size"].default
            == loader_module.DEFAULT_POOL_SIZE
        )
        assert SessionConfig().pool_size == loader_module.DEFAULT_POOL_SIZE

        # A dataclass default is bound at class creation, so no runtime
        # assertion can tell a literal that happens to equal the constant from
        # the constant itself. Read the declaration to close that gap: a literal
        # here is exactly how the two spellings come apart.
        decl = re.search(
            r"pool_size:\s*int\s*=\s*field\(\s*default=([^,\s]+)\s*,",
            inspect.getsource(SessionConfig),
        )
        assert decl is not None, "SessionConfig.pool_size field declaration not found"
        assert decl.group(1) == "DEFAULT_POOL_SIZE"

    def test_omitted_key_and_absent_file_agree(self) -> None:
        """The behavioural invariant, stated without naming a value: whether a
        config file omits ``pool_size``, omits the whole ``session`` block, or
        does not exist, all three land on the same warm-pool size."""
        omits_key = _load_from_dict({"session": {"timeout_secs": 1800}}).session.pool_size
        omits_section = _load_from_dict({"agent": {"provider": "acp"}}).session.pool_size
        no_file = _load_absent_config().session.pool_size

        assert omits_key == omits_section == no_file == loader_module.DEFAULT_POOL_SIZE

    def test_parse_fallback_holds_no_literal_of_its_own(self) -> None:
        """Repoint the constant and the file-parse path must follow it.

        This is what makes the constant the ONLY definition rather than merely
        one that happens to agree today: with a literal at the parse site, the
        loaded value ignores the repoint and this fails.
        """
        with unittest.mock.patch.object(loader_module, "DEFAULT_POOL_SIZE", 7):
            assert _load_from_dict({"session": {}}).session.pool_size == 7
            # The same fallback catches an unparseable value, so it must be
            # sourced from the constant there too.
            assert _load_from_dict({"session": {"pool_size": "two"}}).session.pool_size == 7

    def test_explicit_value_still_wins_over_the_default(self) -> None:
        """The reconciliation must not swallow a configured pool."""
        assert _load_from_dict({"session": {"pool_size": 2}}).session.pool_size == 2
        assert _load_from_dict({"session": {"pool_size": 0}}).session.pool_size == 0


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
        # The fallback is the field's own default, read from the one constant
        # that defines it rather than restated here.
        cfg = _load_from_dict({"session": {"pool_size": "two"}})
        assert cfg.session.pool_size == loader_module.DEFAULT_POOL_SIZE

    def test_non_numeric_float_field_falls_back(self) -> None:
        cfg = _load_from_dict({"agent": {"subagent_cost_gb": "lots"}})
        assert cfg.agent.subagent_cost_gb == 0.5

    def test_numeric_string_preserves_legacy_coercion(self) -> None:
        cfg = _load_from_dict({"session": {"pool_size": "5"}})
        assert cfg.session.pool_size == 5

    def test_numeric_strings_preserve_legacy_coercion_without_jsonschema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.config.validation as validation

        monkeypatch.setattr(validation, "_HAS_JSONSCHEMA", False)

        cfg = _load_from_dict(
            {
                "agent": {"subagent_cost_gb": "0.25"},
                "session": {"pool_size": "5"},
            }
        )

        assert cfg.agent.subagent_cost_gb == 0.25
        assert cfg.session.pool_size == 5

    def test_well_formed_values_still_parse(self) -> None:
        # The fix must not regress the happy path.
        cfg = _load_from_dict({"session": {"pool_size": 7}, "slack": {"observe_max_messages": 50}})
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
        assert cfg.agent.subagent_spawn_stagger_secs == defaults.agent.subagent_spawn_stagger_secs
        assert cfg.session.watchdog_rss_max_mb == defaults.session.watchdog_rss_max_mb
        assert (
            cfg.cron_history.cron_max_records_per_job
            == defaults.cron_history.cron_max_records_per_job
        )
        assert cfg.instances.tunnel_base_port == defaults.instances.tunnel_base_port
        assert cfg.instances.max_recovery_attempts == defaults.instances.max_recovery_attempts
        assert cfg.instances.recover_backoff_max_secs == defaults.instances.recover_backoff_max_secs
        assert cfg.instances.probe_failure_threshold == defaults.instances.probe_failure_threshold


class TestEmptyResponseAutoContinueWiring:
    """The documented kill switch must be WIRED: load() constructs SessionConfig
    field-by-field, so an omitted kwarg silently discards a persisted false."""

    def test_persisted_false_survives_load(self) -> None:
        cfg = _load_from_dict({"session": {"empty_response_auto_continue": False}})
        assert cfg.session.empty_response_auto_continue is False

    def test_default_is_true(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.session.empty_response_auto_continue is True


class TestEmptyResponseMaxContinuesWiring:
    """session.empty_response_max_continues: wired, defaulted, and RANGE-clamped
    — a hand-edited 0 must not disable recovery and a 999 must not arm an
    unbounded ladder (the ladder's give-up arithmetic trusts this clamp)."""

    def test_persisted_value_survives_load(self) -> None:
        cfg = _load_from_dict({"session": {"empty_response_max_continues": 3}})
        assert cfg.session.empty_response_max_continues == 3

    def test_default_is_one(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.session.empty_response_max_continues == 1

    def test_below_range_clamps_to_min(self) -> None:
        cfg = _load_from_dict({"session": {"empty_response_max_continues": 0}})
        assert cfg.session.empty_response_max_continues == 1

    def test_above_range_clamps_to_max(self) -> None:
        cfg = _load_from_dict({"session": {"empty_response_max_continues": 999}})
        assert cfg.session.empty_response_max_continues == 10


def _loader_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "kiro_crew.config.loader"]


class TestCoerceConfigField:
    """``coerce_config_field`` gives raw config readers the loader's posture for a
    nested value: a wrong type degrades to the default and logs, an absent key
    takes the default silently."""

    def test_a_correctly_typed_value_is_returned(self) -> None:
        assert coerce_config_field({"n": 7}, "n", int, 10) == 7
        assert coerce_config_field({"s": "ok"}, "s", str, "def") == "ok"
        assert coerce_config_field({"xs": [1, 2]}, "xs", list, []) == [1, 2]
        assert coerce_config_field({"d": {"a": 1}}, "d", dict, {}) == {"a": 1}

    def test_an_absent_key_takes_the_default_silently(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            assert coerce_config_field({}, "n", int, 10) == 10
        # Absent is the unconfigured state, not a degradation.
        assert _loader_warnings(caplog) == []

    @pytest.mark.parametrize("bad", [[], {}, "x", 1.5, None])
    def test_a_wrong_typed_int_field_degrades_to_the_default(self, bad: object) -> None:
        assert coerce_config_field({"n": bad}, "n", int, 10) == 10

    @pytest.mark.parametrize("bad", [1, [], {}, None, True])
    def test_a_wrong_typed_str_field_degrades_to_the_default(self, bad: object) -> None:
        assert coerce_config_field({"s": bad}, "s", str, "def") == "def"

    @pytest.mark.parametrize("bad", [1, "x", {}, None])
    def test_a_wrong_typed_list_field_degrades_to_the_default(self, bad: object) -> None:
        assert coerce_config_field({"xs": bad}, "xs", list, []) == []

    @pytest.mark.parametrize("bad", [1, "x", [], None])
    def test_a_wrong_typed_dict_field_degrades_to_the_default(self, bad: object) -> None:
        assert coerce_config_field({"d": bad}, "d", dict, {"fallback": True}) == {"fallback": True}

    def test_a_degrade_logs_a_warning_naming_the_key_and_got_type(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            coerce_config_field({"fetch_top_n": []}, "fetch_top_n", int, 10)
        assert any("fetch_top_n" in m and "list" in m for m in _loader_warnings(caplog))

    def test_a_bool_is_not_accepted_for_an_int_field(self) -> None:
        """JSON ``true``/``false`` is distinct from a number, so a mistyped
        ``true`` cannot pose as ``1``."""
        assert coerce_config_field({"n": True}, "n", int, 10) == 10
        assert coerce_config_field({"n": False}, "n", int, 10) == 10

    def test_a_bool_is_accepted_for_a_bool_field(self) -> None:
        assert coerce_config_field({"b": True}, "b", bool, False) is True
        assert coerce_config_field({"b": False}, "b", bool, True) is False

    def test_a_tuple_of_types_accepts_any_member(self) -> None:
        assert coerce_config_field({"v": 1}, "v", (int, float), 0) == 1
        assert coerce_config_field({"v": 1.5}, "v", (int, float), 0) == 1.5
        assert coerce_config_field({"v": "x"}, "v", (int, float), 0) == 0
