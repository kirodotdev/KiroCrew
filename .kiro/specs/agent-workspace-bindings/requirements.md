# Requirements Document

## Introduction

KiroCrew currently treats agent selection as a thin pass-through to kiro-cli's `session/set_mode` — the `agent.default_agent` field in `config.json` is a single string naming a kiro agent, and workspaces are flat `dict[str, str]` mappings with no structured metadata. This feature introduces KiroCrew-owned agent definitions that serve as the orchestration layer binding together a workspace, a memory store, and a reference to a kiro/AIM agent (the execution layer). Kiro agents handle LLM details (prompt, tools, MCP servers, model). KiroCrew agents handle operational bindings (which workspace, which memory store, behavioral preferences). This is Phase 2 of the RFC config evolution, building on the formalized config system from Phase 1.

## Glossary

- **KiroCrew_Agent**: A named agent definition in `config.json` under the `agents` section. Each KiroCrew_Agent binds a workspace, a memory store, and a reference to a Kiro_Agent. Multiple KiroCrew_Agents can reference the same Kiro_Agent with different operational bindings.
- **Kiro_Agent**: An agent config file installed at `~/.kiro/agents/*.json` by AIM or KiroCrew setup. Kiro_Agents define LLM behavior (prompt, tools, MCP servers, model). Selected via `session/set_mode` with the agent's `modeId`.
- **Config_Loader**: The Python module (`config/loader.py`) responsible for reading, validating, and writing `config.json`.
- **Schema_Registry**: The in-memory registry built at import time from the dataclass field metadata, producing flat `ConfigEntry` records.
- **Workspace_Config**: A structured workspace definition containing at minimum a directory path. Replaces the current flat `dict[str, str]` workspace mapping.
- **Memory_Store_Config**: A named memory store definition with its own embedding and memory settings, independent from workspaces.
- **Agent_Resolver**: The logic that resolves the effective workspace and memory store for a session by reading the active KiroCrew_Agent's bindings, falling back to top-level defaults.
- **AIM_Discovery**: The existing `aim_agents.py` module that scans `~/.kiro/agents/` for installed Kiro_Agent configs.

## Requirements

### Requirement 1: KiroCrew Agent Definitions in Config

**User Story:** As a KiroCrew user, I want to define named KiroCrew agents in my config.json, so that I can have multiple operational profiles (e.g. coding, oncall) each with their own workspace and memory store bindings.

#### Acceptance Criteria

1. THE Config_Loader SHALL define a `KiroCrewAgentConfig` dataclass with fields `kiro_agent` (str), `workspace` (str), and `memory_store` (str), each with appropriate field metadata.
2. THE `KiroCrewConfig` dataclass SHALL include an `agents` field of type `dict[str, KiroCrewAgentConfig]` representing the named KiroCrew_Agent definitions.
3. WHEN `config.json` contains an `agents` section, THE Config_Loader SHALL parse each entry into a `KiroCrewAgentConfig` instance keyed by the agent name.
4. THE `KiroCrewAgentConfig.kiro_agent` field SHALL reference the name of a Kiro_Agent (the `modeId` used for `session/set_mode`), defaulting to an empty string.
5. THE `KiroCrewAgentConfig.workspace` field SHALL reference a named workspace from the `workspaces` section, defaulting to `"default"`.
6. THE `KiroCrewAgentConfig.memory_store` field SHALL reference a named memory store from the `memory_stores` section, defaulting to `"default"`.
7. WHEN multiple KiroCrew_Agent definitions reference the same Kiro_Agent name, THE Config_Loader SHALL accept the configuration without error, allowing different operational bindings for the same execution layer.

### Requirement 2: Default Agent Selection

**User Story:** As a KiroCrew user, I want to designate a default KiroCrew agent, so that new sessions automatically use the correct workspace and memory store without manual selection.

#### Acceptance Criteria

1. THE `KiroCrewConfig` dataclass SHALL include a `default_agent` field (str) at the top level that names the active KiroCrew_Agent from the `agents` section.
2. WHEN `default_agent` references a name present in the `agents` section, THE Agent_Resolver SHALL use that KiroCrew_Agent's bindings for new sessions.
3. WHEN `default_agent` references a name not present in the `agents` section, THE Config_Loader SHALL log a warning and fall back to the top-level `default_workspace` and `default_memory_store` values.
4. WHEN `default_agent` is empty and the `agents` section is empty, THE Agent_Resolver SHALL fall back to the top-level `default_workspace` and `default_memory_store` values.

### Requirement 3: Structured Workspace Definitions

**User Story:** As a KiroCrew user, I want workspaces to be structured objects with a directory path, so that the config system can support richer workspace metadata in the future.

#### Acceptance Criteria

1. THE Config_Loader SHALL define a `WorkspaceConfig` dataclass with a `dir` field (str) specifying the workspace directory path, with appropriate field metadata.
2. THE `KiroCrewConfig.workspaces` field type SHALL change from `dict[str, str]` to `dict[str, WorkspaceConfig]`.
3. THE Workspace_Config SHALL NOT contain any agent or memory store references — the binding flows from KiroCrew_Agent to workspace, not from workspace to agent.
4. WHEN a workspace `dir` value starts with `/` or `~`, THE Config_Loader SHALL treat the path as absolute. WHEN the value is a relative path, THE Config_Loader SHALL resolve it relative to the KiroCrew config directory (`~/.kirocrew/`).
5. THE `KiroCrewConfig` SHALL retain a `default_workspace` field (str) naming the fallback workspace when no KiroCrew_Agent binding applies.

### Requirement 4: Workspace Migration from Flat Format

**User Story:** As an existing KiroCrew user, I want my current flat workspace config to continue working after the upgrade, so that I do not need to manually rewrite my config.json.

#### Acceptance Criteria

1. WHEN `config.json` contains a `workspaces` entry where a value is a plain string (the legacy `dict[str, str]` format), THE Config_Loader SHALL auto-migrate it to the structured format by wrapping the string in `{"dir": <value>}`.
2. WHEN `config.json` contains a `workspaces` entry where a value is a dict with a `dir` key (the new structured format), THE Config_Loader SHALL parse it directly into a `WorkspaceConfig` instance.
3. THE `KiroCrewConfig.to_dict()` method SHALL serialize workspaces in the new structured format (`{"dir": "..."}`) regardless of which format was loaded.
4. WHEN `config.json` contains no `workspaces` section, THE Config_Loader SHALL create a default workspace entry `{"default": {"dir": "workspace"}}`.

### Requirement 5: Named Memory Store Definitions

**User Story:** As a KiroCrew user, I want to define named memory stores in my config, so that different agents or tasks can use isolated knowledge containers with their own settings.

#### Acceptance Criteria

1. THE Config_Loader SHALL define a `MemoryStoreConfig` dataclass with fields `description` (str), `embedding_provider` (str), and any memory-related fields that can be overridden per store, each with appropriate field metadata.
2. THE `KiroCrewConfig` dataclass SHALL include a `memory_stores` field of type `dict[str, MemoryStoreConfig]` representing named memory store definitions.
3. THE `KiroCrewConfig` dataclass SHALL include a `default_memory_store` field (str, default `"default"`) naming the fallback memory store.
4. WHEN `config.json` contains no `memory_stores` section, THE Config_Loader SHALL create a default memory store entry named `"default"` that inherits settings from the top-level `memory` section.
5. THE top-level `memory` section SHALL remain as global defaults — `MemoryStoreConfig` entries override specific fields while inheriting unspecified fields from the top-level `memory` section.
6. THE Memory_Store_Config SHALL be independent from Workspace_Config — memory stores and workspaces are orthogonal dimensions bound together only through KiroCrew_Agent definitions.

### Requirement 6: Memory Store Settings Resolution

**User Story:** As a KiroCrew developer, I want memory store settings to merge with global defaults at the dict level, so that a store that only overrides `embedding_provider` inherits all other memory settings from the top-level config.

#### Acceptance Criteria

1. THE Config_Loader SHALL implement a `resolve_memory_store_config` function that deep-merges a `MemoryStoreConfig` entry's raw dict onto the top-level `memory` section's raw dict before constructing the final effective config.
2. WHEN a `MemoryStoreConfig` entry omits a field present in the top-level `memory` section, THE resolved config SHALL use the top-level value for that field.
3. WHEN a `MemoryStoreConfig` entry explicitly sets a field, THE resolved config SHALL use the store-level value, overriding the top-level default.
4. THE merge SHALL happen at the raw dict level before dataclass construction, avoiding the dataclass-defaults trap where unspecified fields revert to dataclass defaults instead of top-level values.

### Requirement 7: Agent Resolver — Session Binding

**User Story:** As a KiroCrew user, I want switching agents to automatically switch my workspace and memory store, so that each agent profile provides a complete operational context.

#### Acceptance Criteria

1. WHEN a session starts with a named KiroCrew_Agent, THE Agent_Resolver SHALL resolve the workspace directory from the agent's `workspace` field by looking up the named workspace in the `workspaces` section.
2. WHEN a session starts with a named KiroCrew_Agent, THE Agent_Resolver SHALL resolve the memory store from the agent's `memory_store` field by looking up the named store in the `memory_stores` section.
3. WHEN a KiroCrew_Agent's `workspace` field references a workspace name not present in the `workspaces` section, THE Agent_Resolver SHALL log a warning and fall back to the `default_workspace`.
4. WHEN a KiroCrew_Agent's `memory_store` field references a store name not present in the `memory_stores` section, THE Agent_Resolver SHALL log a warning and fall back to the `default_memory_store`.
5. WHEN a KiroCrew_Agent specifies a `kiro_agent` value, THE Agent_Resolver SHALL pass that value as the `modeId` to `session/set_mode` for kiro-cli agent switching.

### Requirement 8: Kiro Agent Validation

**User Story:** As a KiroCrew user, I want to be warned when my KiroCrew agent references a kiro agent that is not installed, so that I can fix the configuration before encountering runtime errors.

#### Acceptance Criteria

1. WHEN `config.json` is loaded, THE Config_Loader SHALL cross-reference each KiroCrew_Agent's `kiro_agent` value against the list of installed Kiro_Agents discovered by AIM_Discovery.
2. WHEN a `kiro_agent` value does not match any installed Kiro_Agent name, THE Config_Loader SHALL log a warning identifying the KiroCrew_Agent name and the missing Kiro_Agent reference.
3. THE Config_Loader SHALL continue loading the configuration without error when a `kiro_agent` reference is unresolved — validation is advisory, not fatal.

### Requirement 9: Backward Compatibility with Existing Config

**User Story:** As an existing KiroCrew user, I want my current config.json to continue working without modification after the upgrade, so that the transition is seamless.

#### Acceptance Criteria

1. WHEN `config.json` contains no `agents` section, THE Config_Loader SHALL load the configuration using the existing `agent.default_agent` field as the kiro agent name and the `default_workspace` for workspace resolution.
2. WHEN `config.json` contains the legacy flat `workspaces` format (`dict[str, str]`), THE Config_Loader SHALL auto-migrate to the structured format as specified in Requirement 4.
3. WHEN `config.json` contains no `memory_stores` section, THE Config_Loader SHALL synthesize a `"default"` memory store from the top-level `memory` section as specified in Requirement 5.
4. THE `KiroCrewConfig.load()` followed by `to_dict()` followed by `load()` SHALL produce an equivalent `KiroCrewConfig` instance (round-trip property).
5. WHEN `config.json` uses the legacy `agent.default_agent` field and no top-level `default_agent` is present, THE Config_Loader SHALL treat `agent.default_agent` as the kiro agent name for backward compatibility, not as a KiroCrew_Agent name.

### Requirement 10: Schema Registry Extension

**User Story:** As a KiroCrew developer, I want the new agent, workspace, and memory store config sections to appear in the schema registry, so that the dashboard and baseline generator reflect the full config surface.

#### Acceptance Criteria

1. THE Schema_Registry SHALL include entries for `agents.*` paths using `additionalProperties` in the JSON Schema for dynamic agent names.
2. THE Schema_Registry SHALL include entries for `workspaces.*` paths using `additionalProperties` in the JSON Schema for dynamic workspace names, with child entries for `workspaces.*.dir`.
3. THE Schema_Registry SHALL include entries for `memory_stores.*` paths using `additionalProperties` in the JSON Schema for dynamic store names, with child entries for each `MemoryStoreConfig` field.
4. THE Schema_Registry SHALL include entries for the top-level `default_agent` and `default_memory_store` fields.
5. THE `GET /api/config/schema` endpoint SHALL return the new entries alongside existing entries without breaking the response format.

### Requirement 11: Config Serialization

**User Story:** As a KiroCrew developer, I want `to_dict()` to serialize the new config sections correctly, so that saving config preserves all agent, workspace, and memory store definitions.

#### Acceptance Criteria

1. THE `KiroCrewConfig.to_dict()` method SHALL serialize the `agents` section as a dict of agent name to `KiroCrewAgentConfig` dicts, each containing `kiro_agent`, `workspace`, and `memory_store` keys.
2. THE `KiroCrewConfig.to_dict()` method SHALL serialize the `workspaces` section in the new structured format with `dir` keys.
3. THE `KiroCrewConfig.to_dict()` method SHALL serialize the `memory_stores` section as a dict of store name to `MemoryStoreConfig` dicts.
4. THE `KiroCrewConfig.to_dict()` method SHALL serialize the `default_agent` and `default_memory_store` top-level fields.
5. FOR ALL valid `KiroCrewConfig` instances, serializing via `to_dict()` and deserializing via `load()` SHALL produce an equivalent instance (round-trip property).
