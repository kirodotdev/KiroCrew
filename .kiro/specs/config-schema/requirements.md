# Requirements Document

## Introduction

KiroCrew's configuration (`~/.kirocrew/config.json`) is defined implicitly by Python dataclasses in `config/loader.py`, with several keys (`workspaces`, `default_workspace`, `slack.*`) parsed ad-hoc outside the dataclass hierarchy. This feature formalizes the config schema by pulling all keys into the dataclass hierarchy, attaching rich metadata to the dataclass fields, generating a flat baseline document, exposing the schema via API for dashboard consumption, and adding runtime validation with graceful degradation. After this feature, the dataclasses are the complete and sole source of truth — no ad-hoc parsing remains.

## Glossary

- **Config_Loader**: The Python module (`config/loader.py`) responsible for reading, validating, and writing `config.json`.
- **Schema_Registry**: An in-memory registry built at import time from the dataclass field metadata and any supplementary ad-hoc key definitions.
- **Baseline_Generator**: A build-time script (`scripts/generate_config_baseline.py`) that imports the dataclasses, walks their fields, and emits `config-baseline.json`.
- **Config_Entry**: A single flat record describing one config path, including its type, label, help text, tags, sensitive flag, deprecated flag, enum values, default value, and whether it has children.
- **Schema_Endpoint**: The `GET /api/config/schema` HTTP handler that returns Config_Entry records as JSON.
- **Ad_Hoc_Keys** (legacy): Config keys (`workspaces`, `default_workspace`, `slack.allowed_users`, `slack.tracking_channels`, `slack.command`) that currently exist in `config.json` outside the dataclass hierarchy. This feature eliminates them by pulling them into proper dataclasses.

## Requirements

### Requirement 1: Field Metadata on Dataclasses

**User Story:** As a KiroCrew developer, I want each dataclass config field to carry structured metadata, so that schema information has a single source of truth in Python.

#### Acceptance Criteria

1. THE Config_Loader SHALL define each field in `AgentConfig`, `SessionConfig`, `MemoryConfig`, and `KiroCrewConfig` using `dataclasses.field()` with a `metadata` dict containing at minimum `label` (str) and `help` (str).
2. WHEN a field restricts its values to a fixed set, THE Config_Loader SHALL include an `enum` key in the field metadata listing all valid values.
3. WHERE a field is security-sensitive (credentials, tokens), THE Config_Loader SHALL include `sensitive: True` in the field metadata.
4. WHERE a field is deprecated, THE Config_Loader SHALL include `deprecated: True` in the field metadata.
5. WHEN a field metadata dict omits optional keys (`tags`, `sensitive`, `deprecated`, `enum`), THE Schema_Registry SHALL apply safe defaults: `tags=[]`, `sensitive=False`, `deprecated=False`, `enum=None`.

### Requirement 2: Eliminate Ad-Hoc Keys

**User Story:** As a KiroCrew developer, I want all config keys to live inside the dataclass hierarchy, so that the dataclasses are the complete source of truth and no ad-hoc parsing exists.

#### Acceptance Criteria

1. THE Config_Loader SHALL define a `SlackConfig` dataclass with fields `allowed_users` (list[dict]), `tracking_channels` (list[dict]), and `command` (str, default `"kirocrew"`), each with appropriate metadata.
2. THE Config_Loader SHALL define a `WorkspacesConfig` dataclass (or equivalent fields on `KiroCrewConfig`) to hold `workspaces` (dict[str, str]) and `default_workspace` (str, default `"default"`).
3. THE `KiroCrewConfig` dataclass SHALL include `slack: SlackConfig` and workspace fields as proper typed fields, replacing the current ad-hoc parsing in `load()`.
4. THE `KiroCrewConfig.to_dict()` method SHALL serialize the new `SlackConfig` and workspace fields back to the same JSON structure (`slack.allowed_users`, `slack.tracking_channels`, `slack.command`, `workspaces`, `default_workspace`) for backward compatibility.
5. THE `KiroCrewConfig.load()` method SHALL parse `slack.*`, `workspaces`, and `default_workspace` from JSON into the new dataclass fields, removing all ad-hoc `data.get("slack", {})` parsing.
6. AFTER this change, THE Schema_Registry SHALL derive entries for all config paths purely from `dataclasses.fields()` recursion on `KiroCrewConfig`, with no supplementary ad-hoc registry needed.

### Requirement 3: Schema Registry Construction

**User Story:** As a KiroCrew developer, I want a single in-memory registry of all config entries, so that the API endpoint and the baseline generator share one canonical representation.

#### Acceptance Criteria

1. THE Schema_Registry SHALL be constructed at module import time by walking `dataclasses.fields()` recursively on `KiroCrewConfig`. No supplementary ad-hoc definitions are needed since all keys are dataclass fields.
2. THE Schema_Registry SHALL produce a flat list of Config_Entry records, each with: `path` (dot-separated), `type` (one of `string`, `integer`, `number`, `boolean`, `array`, `object`), `required` (bool), `deprecated` (bool), `sensitive` (bool), `tags` (list of str), `label` (str), `help` (str), `hasChildren` (bool), `enumValues` (list or null), and `defaultValue` (JSON-serializable or null).
3. WHEN a dataclass field type is `str`, THE Schema_Registry SHALL map it to `"string"`. WHEN the type is `int`, THE Schema_Registry SHALL map it to `"integer"`. WHEN the type is `float`, THE Schema_Registry SHALL map it to `"number"`. WHEN the type is `bool`, THE Schema_Registry SHALL map it to `"boolean"`. WHEN the type is `list`, THE Schema_Registry SHALL map it to `"array"`. WHEN the type is a dataclass, THE Schema_Registry SHALL map it to `"object"` and set `hasChildren` to `True`.
4. THE Schema_Registry SHALL assign parent `"object"` entries for each nested dataclass (e.g., `agent`, `session`, `memory`, `slack`) with `hasChildren=True`.

### Requirement 4: Baseline Generator Script

**User Story:** As a KiroCrew developer, I want a build-time script that emits `config-baseline.json`, so that the schema is available for offline tooling and documentation.

#### Acceptance Criteria

1. THE Baseline_Generator SHALL import the Schema_Registry and serialize its entries to a JSON file at `config-baseline.json` in the repository root.
2. THE Baseline_Generator SHALL include a top-level `generatedBy` field with the value `"scripts/generate_config_baseline.py"` and a `generatedAt` ISO-8601 timestamp.
3. THE Baseline_Generator SHALL include a top-level `entries` array containing one JSON object per Config_Entry.
4. FOR ALL Config_Entry records in the Schema_Registry, serializing to JSON and deserializing back SHALL produce an equivalent Config_Entry (round-trip property).
5. THE Baseline_Generator SHALL be executable via `python scripts/generate_config_baseline.py` from the repository root.

### Requirement 5: Schema API Endpoint

**User Story:** As a dashboard developer, I want a `GET /api/config/schema` endpoint, so that the frontend can dynamically render config forms with labels, help text, and validation constraints.

#### Acceptance Criteria

1. WHEN a GET request is received at `/api/config/schema`, THE Schema_Endpoint SHALL return a JSON response with HTTP status 200 containing the full list of Config_Entry records under an `entries` key.
2. WHEN the request includes a `tags` query parameter (comma-separated), THE Schema_Endpoint SHALL return only entries whose `tags` list intersects with the requested tags.
3. WHEN the request includes a `deprecated=false` query parameter, THE Schema_Endpoint SHALL exclude entries where `deprecated` is `True`.
4. THE Schema_Endpoint SHALL include a `Content-Type: application/json` header in the response.

### Requirement 6: Runtime Validation in Config Loader

**User Story:** As a KiroCrew user, I want config loading to validate my `config.json` against the schema and warn about problems, so that I can fix typos and type errors without KiroCrew crashing.

#### Acceptance Criteria

1. WHEN `KiroCrewConfig.load()` parses `config.json`, THE Config_Loader SHALL validate each recognized key's value against the expected type from the Schema_Registry.
2. WHEN a config value has an incorrect type, THE Config_Loader SHALL log a warning message containing the dot-path of the key, the expected type, and the actual type, then fall back to the field's default value.
3. WHEN a config key has an `enum` constraint and the provided value is not in the allowed set, THE Config_Loader SHALL log a warning message containing the dot-path, the allowed values, and the provided value, then fall back to the field's default value.
4. WHEN `config.json` contains unrecognized top-level keys, THE Config_Loader SHALL log a warning listing the unrecognized keys.
5. IF `config.json` is malformed JSON, THEN THE Config_Loader SHALL log a warning and return a default `KiroCrewConfig` instance.
6. THE Config_Loader SHALL complete loading and return a valid `KiroCrewConfig` instance regardless of the number of validation warnings.

### Requirement 7: Sensitive Field Handling

**User Story:** As a KiroCrew user, I want sensitive config values to be masked in logs and API responses, so that credentials are not accidentally exposed.

#### Acceptance Criteria

1. WHEN the Schema_Endpoint serializes a Config_Entry with `sensitive=True`, THE Schema_Endpoint SHALL omit the `defaultValue` field or set it to `null`.
2. WHEN the Config_Loader logs a validation warning for a field marked `sensitive=True`, THE Config_Loader SHALL mask the actual value in the log message (e.g., replace with `"***"`).

### Requirement 8: Deprecated Field Handling

**User Story:** As a KiroCrew developer, I want deprecated config fields to produce clear warnings, so that users migrate to replacement fields before removal.

#### Acceptance Criteria

1. WHEN `KiroCrewConfig.load()` encounters a config key marked `deprecated=True` in the Schema_Registry, THE Config_Loader SHALL log a deprecation warning containing the dot-path and the help text (which should describe the replacement).
2. THE Config_Loader SHALL continue to accept and apply deprecated field values during loading.

### Requirement 9: Documented Config Structure

**User Story:** As a KiroCrew user, I want a clear, well-organized config.json structure, so that I can understand and edit my configuration without guessing at key names or nesting.

#### Acceptance Criteria

1. THE design document SHALL define the complete target `config.json` structure with all top-level sections, nested objects, and leaf fields.
2. THE config structure SHALL organize related settings into logical groups: `agent`, `session`, `memory`, `slack`, `workspaces`, `dashboard`, and `hooks`.
3. THE config structure SHALL use consistent naming conventions: snake_case for all keys, matching the Python dataclass field names.
4. THE config structure SHALL maintain backward compatibility with existing `config.json` files — existing valid configs MUST continue to load without errors.
5. THE design document SHALL reference OpenClaw's config-baseline.json entry format as prior art for the baseline schema format, while keeping the actual config.json shape specific to KiroCrew's simpler feature set.
6. THE `to_dict()` and `load()` methods SHALL produce a round-trip: `KiroCrewConfig.load()` followed by `.to_dict()` SHALL produce a JSON structure that, when loaded again, yields an equivalent `KiroCrewConfig` instance.
