# Implementation Plan: Config Schema

## Overview

Formalize KiroCrew's configuration by making the Python dataclass hierarchy the single source of truth. Add field metadata, introduce `SlackConfig` and `DashboardConfig`, build a schema registry, expose it via API, add a baseline generator, and wire in runtime validation with graceful degradation. All work is in Python, targeting the existing `config/loader.py`, a new `config/schema.py`, `dashboard/handlers.py`, and associated test files.

## Tasks

- [x] 1. Add field metadata and new dataclasses to config/loader.py
  - [x] 1.1 Add `_meta()` helper function and annotate all existing fields on `AgentConfig`, `SessionConfig`, and `MemoryConfig` with `field(metadata=_meta(...))` containing `label`, `help`, and where applicable `enum`, `sensitive`, `deprecated`, `tags`
    - Add `from __future__ import annotations` if not present
    - Define `_meta(label, help, **kwargs) -> dict` helper at module top
    - Annotate every field on `AgentConfig` (approval_mode, streaming, model, provider, bedrock_model_id, bedrock_region, default_agent, sandbox) with metadata; include `enum` for approval_mode, provider, sandbox
    - Annotate every field on `SessionConfig` (timeout_secs) with metadata
    - Annotate every field on `MemoryConfig` (all 13 fields) with metadata; include `enum` for embedding_provider
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [x] 1.2 Create `SlackConfig` and `DashboardConfig` dataclasses with metadata
    - Define `SlackConfig` with fields: `allowed_users` (list[dict]), `tracking_channels` (list[dict]), `command` (str, default `"kirocrew"`)
    - Define `DashboardConfig` with field: `url` (str, default `""`)
    - All fields carry `_meta(...)` metadata
    - _Requirements: 2.1_

  - [x] 1.3 Add `slack`, `dashboard`, `workspaces`, `default_workspace` fields to `KiroCrewConfig` and update `load()` / `to_dict()`
    - Add `slack: SlackConfig`, `dashboard: DashboardConfig`, `workspaces: dict[str, str]`, `default_workspace: str`, `auto_update: bool`, `hooks: dict` as proper typed fields with metadata
    - Update `KiroCrewConfig.load()` to parse `slack.*`, `dashboard.*`, `workspaces`, `default_workspace` from JSON into the new dataclass fields, removing all ad-hoc `data.get("slack", {})` parsing
    - Update `KiroCrewConfig.to_dict()` to serialize the new fields back to the same JSON structure for backward compatibility
    - Ensure existing callers of `slack` / `workspaces` / `default_workspace` attributes still work
    - _Requirements: 2.2, 2.3, 2.4, 2.5, 9.4_

- [x] 2. Checkpoint — verify dataclass changes
  - Ensure all tests pass (`black && isort && flake8 && mypy && python -m pytest`), ask the user if questions arise.

- [ ] 3. Implement config/schema.py — schema registry
  - [x] 3.1 Create `config/schema.py` with `ConfigEntry` dataclass, `build_json_schema()`, `flatten_to_entries()`, `config_entry_to_dict()`, and module-level singletons `JSON_SCHEMA` and `SCHEMA_REGISTRY`
    - `ConfigEntry` fields: path, kind, type, required, deprecated, sensitive, tags, label, help, hasChildren, enumValues, defaultValue
    - `build_json_schema(root_cls)` walks `dataclasses.fields()` recursively, maps Python types to JSON Schema types, embeds `x-meta` extensions for label/help/tags/sensitive/deprecated
    - `flatten_to_entries(json_schema, prefix)` DFS flattens nested JSON Schema into flat `ConfigEntry` list
    - `config_entry_to_dict(entry)` serializes a `ConfigEntry` to a JSON-compatible dict
    - Module-level `JSON_SCHEMA = build_json_schema(KiroCrewConfig)` and `SCHEMA_REGISTRY = flatten_to_entries(JSON_SCHEMA)` built at import time
    - Type mapping: str→string, int→integer, float→number, bool→boolean, list→array, dict/dataclass→object
    - Safe defaults for missing optional metadata: tags=[], sensitive=False, deprecated=False, enum=None
    - _Requirements: 1.5, 3.1, 3.2, 3.3, 3.4_

  - [x]* 3.2 Write property test: all config fields carry required metadata (Property 1)
    - **Property 1: All config fields carry required metadata**
    - Enumerate all dataclass fields via `dataclasses.fields()` recursion, verify each has `label` (str) and `help` (str) in metadata
    - **Validates: Requirements 1.1**

  - [x]* 3.3 Write property test: safe defaults for missing optional metadata (Property 2)
    - **Property 2: Safe defaults for missing optional metadata**
    - Generate partial metadata dicts missing optional keys, verify ConfigEntry has tags=[], sensitive=False, deprecated=False, enumValues=None
    - **Validates: Requirements 1.5**

  - [x]* 3.4 Write property test: registry entries are structurally complete (Property 3)
    - **Property 3: Registry entries are structurally complete**
    - Iterate all SCHEMA_REGISTRY entries, verify all required fields present and every path reachable via dataclasses.fields() recursion on KiroCrewConfig
    - **Validates: Requirements 3.2, 2.6**

  - [x]* 3.5 Write property test: Python-to-schema type mapping is correct (Property 4)
    - **Property 4: Python-to-schema type mapping is correct**
    - Enumerate all fields, compare Python type annotation to schema type string
    - **Validates: Requirements 3.3, 3.4**

  - [x]* 3.6 Write property test: ConfigEntry serialization round-trip (Property 5)
    - **Property 5: ConfigEntry serialization round-trip**
    - Generate ConfigEntry instances with hypothesis, serialize via config_entry_to_dict(), reconstruct, verify equivalence
    - **Validates: Requirements 4.4**

  - [x]* 3.7 Write property test: all config paths use snake_case (Property 15)
    - **Property 15: All config paths use snake_case**
    - Iterate all SCHEMA_REGISTRY entry paths, regex-check each segment matches `[a-z][a-z0-9_]*` or `*`
    - **Validates: Requirements 9.3**

- [x] 4. Checkpoint — verify schema module
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Add runtime validation to config/loader.py
  - [x] 5.1 Integrate `jsonschema.validate()` into `KiroCrewConfig.load()` with graceful degradation
    - Import `jsonschema` and `JSON_SCHEMA` from `config/schema.py`
    - After parsing JSON, run `jsonschema.validate(data, JSON_SCHEMA)` wrapped in try/except
    - On `ValidationError`, log warnings with dot-path, expected type, actual type; fall back to field defaults for invalid values
    - For enum violations, log warning with dot-path, allowed values, actual value; fall back to default
    - For unrecognized top-level keys, log warning listing them
    - For deprecated fields, log deprecation warning with dot-path and help text
    - For sensitive fields, mask actual value as `"***"` in log messages
    - For malformed JSON, log warning and return default `KiroCrewConfig`
    - Always return a valid `KiroCrewConfig` regardless of validation errors
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 7.2, 8.1, 8.2_

  - [x]* 5.2 Write property test: KiroCrewConfig load/to_dict round-trip (Property 6)
    - **Property 6: KiroCrewConfig load/to_dict round-trip**
    - Generate KiroCrewConfig instances with hypothesis, call to_dict() then load() from that dict, verify equivalence
    - **Validates: Requirements 2.4, 2.5, 9.4, 9.6**

  - [x]* 5.3 Write property test: type mismatch falls back to default (Property 9)
    - **Property 9: Type mismatch falls back to default**
    - Generate config dicts with wrong-type values for known keys, verify load() uses field defaults
    - **Validates: Requirements 6.1, 6.2**

  - [x]* 5.4 Write property test: enum violation falls back to default (Property 10)
    - **Property 10: Enum violation falls back to default**
    - Generate config dicts with invalid enum values, verify load() uses field defaults
    - **Validates: Requirements 6.3**

  - [x]* 5.5 Write property test: unrecognized keys are detected (Property 11)
    - **Property 11: Unrecognized keys are detected**
    - Generate config dicts with random extra top-level keys, verify load() detects them
    - **Validates: Requirements 6.4**

  - [x]* 5.6 Write property test: load() always returns valid KiroCrewConfig (Property 12)
    - **Property 12: load() always returns valid KiroCrewConfig**
    - Generate arbitrary strings/bytes as config input, verify load() returns a KiroCrewConfig without raising
    - **Validates: Requirements 6.6**

  - [x]* 5.7 Write property test: deprecated fields are accepted during loading (Property 14)
    - **Property 14: Deprecated fields are accepted during loading**
    - Generate configs with deprecated field values, verify load() applies the provided value
    - **Validates: Requirements 8.2**

- [x] 6. Checkpoint — verify validation logic
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Add schema API endpoint and baseline generator
  - [x] 7.1 Add `GET /api/config/schema` endpoint in `dashboard/handlers.py`
    - Import `SCHEMA_REGISTRY` and `config_entry_to_dict` from `config/schema.py`
    - Implement `api_config_schema` handler: return JSON `{"entries": [...]}` with status 200 and `Content-Type: application/json`
    - Support `tags` query param (comma-separated) to filter entries by tag intersection
    - Support `deprecated=false` query param to exclude deprecated entries
    - For entries with `sensitive=True`, set `defaultValue` to `null` in the response
    - Register route in the app router
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 7.1_

  - [x]* 7.2 Write property test: tag filtering returns only matching entries (Property 7)
    - **Property 7: Tag filtering returns only matching entries**
    - Generate random tag sets, filter SCHEMA_REGISTRY, verify only entries with intersecting tags are returned
    - **Validates: Requirements 5.2**

  - [x]* 7.3 Write property test: deprecated filtering excludes deprecated entries (Property 8)
    - **Property 8: Deprecated filtering excludes deprecated entries**
    - Filter SCHEMA_REGISTRY with deprecated=false, verify zero deprecated entries in result
    - **Validates: Requirements 5.3**

  - [x]* 7.4 Write property test: sensitive entries have null defaultValue in API (Property 13)
    - **Property 13: Sensitive entries have null defaultValue in API**
    - Iterate sensitive entries, verify API response dict has defaultValue=null
    - **Validates: Requirements 7.1**

  - [x] 7.5 Create `scripts/generate_config_baseline.py` baseline generator script
    - Import `SCHEMA_REGISTRY` and `config_entry_to_dict` from `config/schema.py`
    - Serialize entries to JSON with `generatedBy`, `generatedAt` (ISO-8601), and `entries` keys
    - Write to `config-baseline.json` in the repo root
    - Make script executable via `python scripts/generate_config_baseline.py`
    - _Requirements: 4.1, 4.2, 4.3, 4.5_

  - [x]* 7.6 Write unit tests for baseline generator
    - Verify output JSON has `generatedBy`, `generatedAt`, and `entries` keys
    - Verify `entries` array contains expected number of ConfigEntry dicts
    - Verify script is runnable and produces valid JSON
    - _Requirements: 4.1, 4.2, 4.3, 4.5_

  - [x]* 7.7 Write unit tests for schema API endpoint
    - Test `GET /api/config/schema` returns 200 with `Content-Type: application/json`
    - Test `tags` query param filtering
    - Test `deprecated=false` query param filtering
    - Test sensitive entries have null defaultValue
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 7.1_

- [x] 8. Final checkpoint — full build and test
  - Run `black && isort && flake8 && mypy && python -m pytest` to verify everything passes. Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation after each major phase
- Property tests use the `hypothesis` library with `@settings(max_examples=100)`
- All code targets Python 3.9+ with `from __future__ import annotations`
- The `jsonschema` library is the only new dependency
