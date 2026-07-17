"""Property-based tests for config/schema.py.

Tests the schema registry, ConfigEntry, and related utilities using
hypothesis for property-based testing.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import typing

from hypothesis import given
from hypothesis import strategies as st

from kiro_crew.config import schema as _schema_module
from kiro_crew.config.loader import (
    AgentConfig,
    DashboardConfig,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    MemoryConfig,
    MemoryStoreConfig,
    SessionConfig,
    SlackConfig,
    WorkspaceConfig,
)
from kiro_crew.config.schema import (
    JSON_SCHEMA,
    SCHEMA_REGISTRY,
    ConfigEntry,
    config_entry_to_dict,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_CONFIG_CLASSES: list[type] = [
    KiroCrewConfig,
    AgentConfig,
    SessionConfig,
    MemoryConfig,
    SlackConfig,
    DashboardConfig,
    KiroCrewAgentConfig,
    WorkspaceConfig,
    MemoryStoreConfig,
]


def _all_fields_recursive(
    cls: type,
    prefix: str = "",
) -> list[tuple[str, dataclasses.Field]]:  # type: ignore[type-arg]
    """Yield (dot_path, field) for every field in the dataclass hierarchy."""
    result: list[tuple[str, dataclasses.Field]] = []  # type: ignore[type-arg]
    for f in dataclasses.fields(cls):
        path = f"{prefix}.{f.name}" if prefix else f.name
        result.append((path, f))
        tp = f.type
        if isinstance(tp, str):
            import kiro_crew.config.loader as _mod

            try:
                tp = eval(tp, vars(_mod))  # noqa: S307
            except Exception:
                continue
        origin = typing.get_origin(tp)
        if origin is dict:
            # For dict[str, DataclassType], add wildcard path and recurse
            args = typing.get_args(tp)
            if len(args) == 2:
                val_type = args[1]
                if dataclasses.is_dataclass(val_type) and isinstance(val_type, type):
                    wildcard_path = f"{path}.*"
                    result.extend(_all_fields_recursive(val_type, wildcard_path))
            continue
        if origin is list:
            # For list[DataclassType], add wildcard path and recurse
            args = typing.get_args(tp)
            if args:
                item_type = args[0]
                if dataclasses.is_dataclass(item_type) and isinstance(item_type, type):
                    wildcard_path = f"{path}.*"
                    result.extend(_all_fields_recursive(item_type, wildcard_path))
            continue
        if origin is not None:
            continue
        if dataclasses.is_dataclass(tp) and isinstance(tp, type):
            result.extend(_all_fields_recursive(tp, path))
    return result


def _resolve_type(f: dataclasses.Field) -> type:  # type: ignore[type-arg]
    """Resolve a field's type annotation to a runtime type."""
    import kiro_crew.config.loader as _mod

    tp = f.type
    if isinstance(tp, str):
        try:
            tp = eval(tp, vars(_mod))  # noqa: S307
        except Exception:
            return str
    return tp  # type: ignore[return-value]


# Expected Python type → JSON Schema type mapping
_EXPECTED_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    set: "array",
    dict: "object",
}

# Segment pattern for snake_case paths (also allow * for dynamic keys)
_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$|^\*$")


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestConfigSchemaProperties:
    """Property-based tests for the config schema registry."""

    # Feature: config-schema, Property 1: All config fields carry required metadata
    def test_all_fields_carry_required_metadata(self) -> None:
        """Every dataclass field in the config hierarchy must have
        'label' (str) and 'help' (str) in its metadata.

        **Validates: Requirements 1.1**
        """
        all_fields = _all_fields_recursive(KiroCrewConfig)
        assert len(all_fields) > 0, "Expected at least one field"

        for path, f in all_fields:
            meta = dict(f.metadata) if f.metadata else {}
            assert "label" in meta, f"Field '{path}' missing 'label' in metadata"
            assert isinstance(
                meta["label"], str
            ), f"Field '{path}' label must be str, got {type(meta['label'])}"
            assert "help" in meta, f"Field '{path}' missing 'help' in metadata"
            assert isinstance(
                meta["help"], str
            ), f"Field '{path}' help must be str, got {type(meta['help'])}"

    # Feature: config-schema, Property 2: Safe defaults for missing optional metadata
    @given(
        has_tags=st.booleans(),
        has_sensitive=st.booleans(),
        has_deprecated=st.booleans(),
        has_enum=st.booleans(),
    )
    def test_safe_defaults_for_missing_optional_metadata(
        self,
        has_tags: bool,
        has_sensitive: bool,
        has_deprecated: bool,
        has_enum: bool,
    ) -> None:
        """When optional metadata keys are omitted, ConfigEntry must use
        safe defaults: tags=[], sensitive=False, deprecated=False,
        enumValues=None.

        **Validates: Requirements 1.5**
        """
        meta: dict = {"label": "Test", "help": "Test help."}
        if has_tags:
            meta["tags"] = ["custom"]
        if has_sensitive:
            meta["sensitive"] = True
        if has_deprecated:
            meta["deprecated"] = True
        if has_enum:
            meta["enum"] = ["a", "b"]

        # Build a ConfigEntry the same way the schema module does:
        # extract optional keys with safe defaults
        tags = meta.get("tags", [])
        sensitive = meta.get("sensitive", False)
        deprecated = meta.get("deprecated", False)
        enum_values = meta.get("enum", None)

        entry = ConfigEntry(
            path="test.field",
            kind="core",
            type="string",
            required=False,
            deprecated=deprecated,
            sensitive=sensitive,
            tags=tags,
            label=meta["label"],
            help=meta["help"],
            has_children=False,
            enum_values=enum_values,
            default_value=None,
        )

        if not has_tags:
            assert entry.tags == [], f"Expected empty tags, got {entry.tags}"
        if not has_sensitive:
            assert entry.sensitive is False
        if not has_deprecated:
            assert entry.deprecated is False
        if not has_enum:
            assert entry.enum_values is None

    # Feature: config-schema, Property 3: Registry entries are structurally complete
    def test_registry_entries_structurally_complete(self) -> None:
        """Every SCHEMA_REGISTRY entry must have all required fields and
        every path must be reachable via dataclasses.fields() recursion
        on KiroCrewConfig.

        **Validates: Requirements 3.2, 2.6**
        """
        required_attrs = [
            "path",
            "kind",
            "type",
            "required",
            "deprecated",
            "sensitive",
            "tags",
            "label",
            "help",
            "has_children",
            "enum_values",
            "default_value",
        ]

        # Build set of all reachable paths from the dataclass hierarchy
        all_fields = _all_fields_recursive(KiroCrewConfig)
        reachable_paths: set[str] = set()
        for path, f in all_fields:
            reachable_paths.add(path)
            # Also add wildcard child paths for list/dict fields
            tp = _resolve_type(f)
            origin = typing.get_origin(tp)
            if origin is list or origin is dict:
                reachable_paths.add(f"{path}.*")

        assert len(SCHEMA_REGISTRY) > 0, "Registry should not be empty"

        for entry in SCHEMA_REGISTRY:
            # Verify all required attributes are present
            for attr in required_attrs:
                assert hasattr(entry, attr), f"Entry '{entry.path}' missing attribute '{attr}'"

            # Verify path is reachable from the dataclass hierarchy
            assert entry.path in reachable_paths, (
                f"Entry path '{entry.path}' not reachable via "
                f"dataclasses.fields() recursion on KiroCrewConfig"
            )

            # Verify type is a valid JSON Schema type
            valid_types = {
                "string",
                "integer",
                "number",
                "boolean",
                "array",
                "object",
            }
            assert (
                entry.type in valid_types
            ), f"Entry '{entry.path}' has invalid type '{entry.type}'"

            # Verify kind is set
            assert entry.kind == "core", f"Entry '{entry.path}' has unexpected kind '{entry.kind}'"

    # Feature: config-schema, Property 4: Python-to-schema type mapping is correct
    def test_python_to_schema_type_mapping(self) -> None:
        """For every field in the config hierarchy, the schema registry
        must map Python types correctly: str→string, int→integer,
        float→number, bool→boolean, list→array, dict/dataclass→object.

        **Validates: Requirements 3.3, 3.4**
        """
        # Build a lookup from path → ConfigEntry
        registry_by_path: dict[str, ConfigEntry] = {e.path: e for e in SCHEMA_REGISTRY}

        all_fields = _all_fields_recursive(KiroCrewConfig)
        for path, f in all_fields:
            tp = _resolve_type(f)
            origin = typing.get_origin(tp)

            # Determine expected JSON Schema type
            if dataclasses.is_dataclass(tp) and isinstance(tp, type):
                expected_type = "object"
                expected_has_children = True
            elif origin is not None:
                base = origin
                expected_type = _EXPECTED_TYPE_MAP.get(base, "string")
                expected_has_children = expected_type in ("array", "object")
            else:
                expected_type = _EXPECTED_TYPE_MAP.get(tp, "string")
                # Bare dict/list (no generic args) still have children
                expected_has_children = expected_type in ("array", "object")

            assert path in registry_by_path, f"Field '{path}' not found in SCHEMA_REGISTRY"
            entry = registry_by_path[path]
            assert entry.type == expected_type, (
                f"Field '{path}': expected type '{expected_type}', "
                f"got '{entry.type}' (Python type: {tp})"
            )
            assert entry.has_children == expected_has_children, (
                f"Field '{path}': expected has_children={expected_has_children}, "
                f"got {entry.has_children}"
            )

    # Feature: config-schema, Property 5: ConfigEntry serialization round-trip
    @given(
        path=st.text(
            alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz_."),
            min_size=1,
            max_size=30,
        ).filter(lambda s: not s.startswith(".") and not s.endswith(".")),
        kind=st.just("core"),
        entry_type=st.sampled_from(
            [
                "string",
                "integer",
                "number",
                "boolean",
                "array",
                "object",
            ]
        ),
        required=st.booleans(),
        deprecated=st.booleans(),
        sensitive=st.booleans(),
        tags=st.lists(st.text(min_size=1, max_size=10), max_size=3),
        label=st.text(min_size=1, max_size=50),
        help_text=st.text(min_size=1, max_size=100),
        has_children=st.booleans(),
        has_enum=st.booleans(),
        default_is_none=st.booleans(),
    )
    def test_config_entry_round_trip(
        self,
        path: str,
        kind: str,
        entry_type: str,
        required: bool,
        deprecated: bool,
        sensitive: bool,
        tags: list[str],
        label: str,
        help_text: str,
        has_children: bool,
        has_enum: bool,
        default_is_none: bool,
    ) -> None:
        """Serializing a ConfigEntry via config_entry_to_dict() and
        reconstructing it must produce an equivalent entry.

        **Validates: Requirements 4.4**
        """
        enum_values = ["a", "b", "c"] if has_enum else None
        default_value = None if default_is_none else "test_default"

        original = ConfigEntry(
            path=path,
            kind=kind,
            type=entry_type,
            required=required,
            deprecated=deprecated,
            sensitive=sensitive,
            tags=tags,
            label=label,
            help=help_text,
            has_children=has_children,
            enum_values=enum_values,
            default_value=default_value,
        )

        d = config_entry_to_dict(original)

        # Reconstruct from dict (camelCase keys → snake_case attrs)
        reconstructed = ConfigEntry(
            path=d["path"],
            kind=d["kind"],
            type=d["type"],
            required=d["required"],
            deprecated=d["deprecated"],
            sensitive=d["sensitive"],
            tags=d["tags"],
            label=d["label"],
            help=d["help"],
            has_children=d["hasChildren"],
            enum_values=d["enumValues"],
            default_value=d["defaultValue"],
        )

        assert reconstructed.path == original.path
        assert reconstructed.kind == original.kind
        assert reconstructed.type == original.type
        assert reconstructed.required == original.required
        assert reconstructed.deprecated == original.deprecated
        assert reconstructed.sensitive == original.sensitive
        assert reconstructed.tags == original.tags
        assert reconstructed.label == original.label
        assert reconstructed.help == original.help
        assert reconstructed.has_children == original.has_children
        assert reconstructed.enum_values == original.enum_values
        assert reconstructed.default_value == original.default_value

    # Feature: config-schema, Property 15: All config paths use snake_case
    def test_all_config_paths_use_snake_case(self) -> None:
        """Every segment of every SCHEMA_REGISTRY entry path must match
        [a-z][a-z0-9_]* or be the wildcard '*'.

        **Validates: Requirements 9.3**
        """
        assert len(SCHEMA_REGISTRY) > 0, "Registry should not be empty"

        for entry in SCHEMA_REGISTRY:
            segments = entry.path.split(".")
            for segment in segments:
                assert _SNAKE_CASE_RE.match(segment), (
                    f"Path '{entry.path}' has segment '{segment}' "
                    f"that does not match snake_case pattern "
                    f"[a-z][a-z0-9_]* or '*'"
                )


# ---------------------------------------------------------------------------
# Phase 2: Agent-Workspace Bindings Schema Registry Tests
# ---------------------------------------------------------------------------


class TestAgentWorkspaceBindingsSchema:
    """Unit tests for schema registry entries added by Phase 2 dataclasses.

    Verifies that the auto-generated schema registry contains all expected
    paths for agents, workspaces, memory_stores, and top-level defaults.

    **Validates: Requirements 10.1, 10.2, 10.3, 10.4**
    """

    def test_agents_paths_exist(self) -> None:
        """agents.* paths are present in SCHEMA_REGISTRY.

        **Validates: Requirement 10.1**
        """
        paths = {e.path for e in SCHEMA_REGISTRY}
        assert "agents" in paths
        assert "agents.*" in paths
        assert "agents.*.kiro_agent" in paths
        assert "agents.*.workspace" in paths
        assert "agents.*.memory_store" in paths

    def test_workspaces_dir_path_exists(self) -> None:
        """workspaces.*.dir path exists (replacing old workspaces.* string type).

        **Validates: Requirement 10.2**
        """
        paths = {e.path for e in SCHEMA_REGISTRY}
        assert "workspaces" in paths
        assert "workspaces.*" in paths
        assert "workspaces.*.dir" in paths

        # Verify workspaces.* is an object (not a string like the old format)
        by_path = {e.path: e for e in SCHEMA_REGISTRY}
        assert by_path["workspaces.*"].type == "object"
        assert by_path["workspaces.*.dir"].type == "string"

    def test_memory_stores_paths_exist(self) -> None:
        """memory_stores.* paths are present in SCHEMA_REGISTRY.

        **Validates: Requirement 10.3**
        """
        paths = {e.path for e in SCHEMA_REGISTRY}
        assert "memory_stores" in paths
        assert "memory_stores.*" in paths
        assert "memory_stores.*.description" in paths
        assert "memory_stores.*.embedding_provider" in paths

    def test_top_level_defaults_exist(self) -> None:
        """default_agent and default_memory_store top-level entries exist.

        **Validates: Requirement 10.4**
        """
        paths = {e.path for e in SCHEMA_REGISTRY}
        assert "default_agent" in paths
        assert "default_memory_store" in paths

        by_path = {e.path: e for e in SCHEMA_REGISTRY}
        assert by_path["default_agent"].type == "string"
        assert by_path["default_memory_store"].type == "string"

    def test_additional_properties_for_dynamic_keys(self) -> None:
        """JSON Schema uses additionalProperties for agents, workspaces,
        memory_stores dynamic keys.

        **Validates: Requirements 10.1, 10.2, 10.3**
        """
        top_props = JSON_SCHEMA.get("properties", {})

        # agents
        agents_schema = top_props.get("agents", {})
        assert (
            "additionalProperties" in agents_schema
        ), "agents schema should use additionalProperties for dynamic agent names"
        agents_ap = agents_schema["additionalProperties"]
        assert agents_ap.get("type") == "object"
        assert "kiro_agent" in agents_ap.get("properties", {})
        assert "workspace" in agents_ap.get("properties", {})
        assert "memory_store" in agents_ap.get("properties", {})

        # workspaces
        ws_schema = top_props.get("workspaces", {})
        assert (
            "additionalProperties" in ws_schema
        ), "workspaces schema should use additionalProperties for dynamic workspace names"
        ws_ap = ws_schema["additionalProperties"]
        assert ws_ap.get("type") == "object"
        assert "dir" in ws_ap.get("properties", {})

        # memory_stores
        ms_schema = top_props.get("memory_stores", {})
        assert (
            "additionalProperties" in ms_schema
        ), "memory_stores schema should use additionalProperties for dynamic store names"
        ms_ap = ms_schema["additionalProperties"]
        assert ms_ap.get("type") == "object"
        assert "description" in ms_ap.get("properties", {})
        assert "embedding_provider" in ms_ap.get("properties", {})


class TestFieldTypeResolution:
    """Pin the get_type_hints-based field-type resolution (replaced eval()).

    Schema generation resolves dataclass field annotations via
    ``typing.get_type_hints(cls)`` against each class's own module namespace,
    not by ``eval()``-ing annotation strings against the loader module globals.
    These tests guard against a silent regression where a field type fails to
    resolve and falls back to the catch-all ``str``/``"string"`` JSON type.
    """

    # Fields whose declared type is NOT a plain ``str`` — they must resolve to
    # their real JSON type, never silently degrade to "string".
    _NON_STRING_FIELDS: list[tuple[str, str]] = [
        ("slack.tracking_channels", "array"),
        ("slack.reactions", "object"),
        ("slack.trusted_bot_ids", "array"),
        ("agent.subagent_cwd_allowed_roots", "array"),
        ("memory.semantic_keys", "array"),
        ("slack_channels", "object"),
        ("session.timeout_secs", "integer"),
        ("session.pool_size", "integer"),
    ]

    def test_non_string_fields_resolve_to_real_types(self) -> None:
        by_path = {e.path: e for e in SCHEMA_REGISTRY}
        for path, expected in self._NON_STRING_FIELDS:
            entry = by_path.get(path)
            assert entry is not None, f"missing schema entry for {path}"
            assert entry.type == expected, (
                f"{path}: type resolved to {entry.type!r}, expected {expected!r} "
                f"— a 'string' here usually means field-type resolution silently "
                f"fell back (regression of the get_type_hints fix)"
            )

    def test_schema_module_does_not_reach_into_loader_namespace(self) -> None:
        # The eval()-against-loader-globals coupling has been removed. Guard
        # against its reintroduction so schema generation stays self-contained.
        src = inspect.getsource(_schema_module)
        assert "vars(_loader_mod)" not in src, "schema must not eval() against the loader namespace"
        assert "eval(" not in src, "schema field-type resolution must not use eval()"

    def test_nested_dataclass_fields_resolve_as_objects(self) -> None:
        # dict[str, ChannelConfig] must resolve ChannelConfig as a nested object
        # schema (additionalProperties), proving cross-module forward refs work
        # through get_type_hints without the loader-namespace eval.
        sc = JSON_SCHEMA["properties"].get("slack_channels", {})
        assert sc.get("type") == "object"
        ap = sc.get("additionalProperties", {})
        assert isinstance(ap, dict) and ap.get("type") == "object"
        assert "properties" in ap and len(ap["properties"]) > 0
