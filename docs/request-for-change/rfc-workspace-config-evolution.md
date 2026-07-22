# RFC: Config System, Named Memory Stores & Plugin Architecture

**Author:** KiroCrew contributors  
**Date:** 2026-03-25 (rev 2: 2026-03-25)  
**Status:** Draft  
**Branch (parked):** `feat/workspace-scoped-vector-memory`  
**Branch (active):** `config-standarize-`

---

## 1. Problem Statement

KiroCrew's configuration and memory systems have grown organically. Several pain points have emerged:

1. **Ad-hoc config parsing** — `workspaces`, `default_workspace`, `slack.*` are parsed outside the dataclass hierarchy in `KiroCrewConfig.load()`. No validation, no schema, no discoverability for the dashboard.
2. **Global memory** — `VectorMemoryStore` uses a single `memory.db` + `memory.faiss` at `~/.kirocrew/`. Users working across multiple projects (oncall vs. feature work vs. personal) get cross-contaminated context. The parked `feat/workspace-scoped-vector-memory` branch prototyped per-workspace stores but depends on a proper config foundation.
3. **No plugin system** — memory backends are hardcoded (SQLite+FAISS local, in-process llama.cpp embeddings). No way to swap in remote vector DBs, different embedding providers, or team-shared memory without code changes.

These three problems are coupled: named memory stores need config to declare them, and plugins need config to declare and configure backends. Solving them in the wrong order creates rework.

## 2. Evolution Path

Four phases, each independently shippable. Solid arrows (──►) are hard prerequisites; dashed arrows (- -►) are soft dependencies (benefits from, but can proceed without).

```
Phase 1 ──────► Phase 2 ──────► Phase 3
(Config System)  (Agent Config    (Named Memory
                  + Workspaces     Stores)
                  + Memory Stores)    :
                       │              :
                       │              ▼ (soft)
                       └──────► Phase 4
                                (Plugin System)
```

Phase 1 → 2 → 3 is a strict chain. Phase 4 requires Phase 2 (for schema extensibility) and benefits from Phase 3 (named stores are the first pluggable surface) but can start in parallel with Phase 3.

### Key Architectural Insight: Memory ≠ Workspace

Memory stores and workspaces are **independent dimensions**. A workspace is a working directory for file I/O (kiro-cli cwd, task runner output). A memory store is a knowledge container (semantic entries, episodic memories, lessons). They don't have to be 1:1.

An agent session picks both:
- **Workspace** — where files live (e.g. `oncall`, `feature-work`)
- **Memory store** — what knowledge to use (e.g. `oncall-knowledge`, `kirocrew-dev`, `shared-team`)

This decoupling enables scenarios that 1:1 binding can't:
- Two workspaces sharing the same memory store (e.g. `frontend` and `backend` workspaces both using `project-x` memory)
- One workspace switching memory stores depending on the task (oncall workspace uses `oncall-knowledge` during incidents, `general` otherwise)
- The agent auto-selecting a memory store based on context (future: metadata on stores enables LLM-driven selection)

The config declares both independently. An agent config or session can bind them together.

### Test Strategy (all phases)

Each phase includes its own test plan. The goal is to catch regressions early without slowing velocity — tests are written alongside implementation, not after.

**Phase 1** (already specified in `.kiro/specs/config-schema/design.md`):
- 15 property-based tests via `hypothesis` covering schema registry completeness, type mapping, round-trip serialization, validation fallback, snake_case paths
- Unit tests for API endpoint (`GET /api/config/schema`), baseline generator, edge cases (malformed JSON, unknown keys, deprecated fields, sensitive masking)
- Gate: `pytest` passes, baseline generator produces valid JSON

**Phase 2:**
- Unit tests for `WorkspaceConfig` and `MemoryStoreConfig` dataclasses (field defaults, metadata)
- Unit tests for dict-level merge logic (`resolve_effective_config`) — verify partial overrides inherit from top-level, not from dataclass defaults
- Property test: for any valid top-level config + any partial workspace override dict, the merged result has all fields and no field reverts to dataclass default unless the top-level also uses that default
- Unit tests for auto-migration from flat `dict[str, str]` to structured shape
- Round-trip property: `load()` → `to_dict()` → `load()` produces equivalent config
- Integration test: dashboard workspace selector renders effective merged config
- Gate: all existing tests still pass (backward compat), new tests pass

**Phase 3:**
- Unit tests for `get_memory_store_for()` lazy cache keyed by store name
- Isolation test: write to store A, verify store B doesn't see it
- Migration test: global `memory.db` becomes the `default` store, data intact
- API test: memory endpoints with `?memory_store=` param return correct scoped data
- Consolidation test: history consolidator resolves correct store from session metadata
- Gate: existing memory tests pass unchanged (they implicitly use `default` store)

**Phase 4:**
- ABC compliance test: built-in `SqliteFaissBackend` implements `MemoryBackend` interface
- Plugin discovery test: entry point registration, unknown backend name → graceful error
- Remote consent test: plugin with `"remote": true` rejected when `allow_remote_access` is false
- Integration test: swap backend via config, verify read/write round-trip
- Gate: all Phase 3 tests pass with the built-in backend selected via plugin system

**How to run:** All tests run via `black src/kiro_crew test && isort src/kiro_crew test && flake8 src/kiro_crew test && pytest`. No separate test commands. Property tests use `@settings(max_examples=100)`. Test files: `test/test_config_schema.py`, `test/test_config_loader.py`, plus new files per phase.

### Phase 1: Formalized Config System

**Branch:** `config-standarize-` (active, spec complete, implementation starting)  
**Spec:** `.kiro/specs/config-schema/`  
**Goal:** Make Python dataclasses the single source of truth for all config keys.

What it delivers:
- Field metadata (`label`, `help`, `tags`, `sensitive`, `deprecated`, `enum`) on every dataclass field via `_meta()` helper
- New `SlackConfig` and `DashboardConfig` dataclasses — eliminates all ad-hoc `data.get("slack", {})` parsing
- `workspaces` and `default_workspace` as proper typed fields on `KiroCrewConfig`
- Schema registry (`config/schema.py`) — walks `dataclasses.fields()` recursively, produces flat `ConfigEntry` list
- Three-layer schema: dataclasses → nested JSON Schema (for `jsonschema.validate()`) → flat entry list (for API + baseline)
- `GET /api/config/schema` endpoint for dashboard consumption
- `scripts/generate_config_baseline.py` for CI drift detection
- Runtime validation with graceful degradation — type checks, enum checks, unknown-key warnings, never crashes

Target `config.json` structure after Phase 1:
```json
{
  "agent": {
    "approval_mode": "auto",
    "streaming": true,
    "model": "auto",
    "provider": "acp",
    "bedrock_model_id": "anthropic.claude-sonnet-4-20250514",
    "bedrock_region": "us-west-2",
    "default_agent": "",
    "sandbox": "auto"
  },
  "session": { "timeout_secs": 1800 },
  "memory": {
    "embedding_provider": "llama_cpp",
    "embedding_dim": 1024,
    "semantic_confidence_threshold": 0.8,
    "episodic_dedup_threshold": 0.88,
    "episodic_max_results": 8,
    "episodic_max_count": 10000,
    "semantic_keys": [],
    "history_idle_hours": 3.0,
    "history_max_days": 365,
    "migrated": false
  },
  "slack": {
    "allowed_users": [],
    "tracking_channels": [],
    "command": "kirocrew"
  },
  "dashboard": { "url": "" },
  "hooks": {},
  "workspaces": { "default": "workspace" },
  "default_workspace": "default",
  "auto_update": true
}
```

**Backward compatibility:** existing `config.json` files load without errors. `to_dict()` produces the same JSON shape. Round-trip property holds.

**Only new dependency:** `jsonschema` (pure Python, well-established).

### Phase 2: Agent Config in Config — Workspaces, Memory Stores & Merge Semantics

**Branch:** TBD (after Phase 1 merges)  
**Goal:** Extend the config schema to support structured workspaces, named memory stores, and per-workspace agent overrides.

Today `workspaces` is a flat `dict[str, str]` mapping name → directory. Phase 2 introduces two independent concepts:

1. **Workspaces** — named working directories with optional agent overrides
2. **Memory stores** — named knowledge containers with their own embedding/memory settings

Proposed config shape:
```json
{
  "workspaces": {
    "default": {
      "dir": "workspace",
      "agent": { "default_agent": "kirocrew" }
    },
    "oncall": {
      "dir": "workspace-oncall",
      "agent": { "default_agent": "oncall-agent" }
    }
  },
  "memory_stores": {
    "default": {
      "description": "General-purpose memory",
      "embedding_provider": "llama_cpp",
      "semantic_keys": ["pref.*", "project.*"]
    },
    "oncall-knowledge": {
      "description": "Oncall runbooks, incident patterns, escalation paths",
      "embedding_provider": "llama_cpp"
    },
    "shared-team": {
      "description": "Team-wide knowledge base",
      "embedding_provider": "none"
    }
  },
  "default_workspace": "default",
  "default_memory_store": "default"
}
```

Key design decisions:

- **Workspaces and memory stores are fully independent** — workspaces don't reference memory stores. They're two orthogonal dimensions. The **agent** is the natural binding point. Each agent definition already has a system prompt, tools, and model — adding `workspace` and `memory_store` fields is a natural extension:

  ```json
  {
    "oncall-agent": {
      "model": "auto",
      "workspace": "oncall",
      "memory_store": "oncall-knowledge",
      "tools": ["@kirocrew-cron", "@kirocrew-core"]
    }
  }
  ```

  When you switch agents (`!agent oncall-agent`), the workspace and memory store switch with it. The agent IS the binding between workspace and memory — no separate user override needed.

  Resolution at session start:
  1. Agent config specifies `workspace` and/or `memory_store` → use those
  2. Not specified in agent config → fall back to `default_workspace` and `default_memory_store` from global config
  3. Future: the agent reads store descriptions and dynamically selects based on task context

- **Memory store metadata** — each store has a `description` field (human-readable purpose). This metadata enables future LLM-driven store selection: the agent can read store descriptions and pick the right one for the task, rather than requiring manual selection. Think of stores as "knowledge domains" — oncall knowledge, project context, team conventions.

- **Merge semantics (addressing review comment)** — workspace-level `agent` overrides are merged at the **raw dict level** before constructing the dataclass, not after. This avoids the dataclass-defaults trap where a partial `agent` object fills unspecified fields with dataclass defaults instead of top-level values:

  ```python
  def resolve_effective_config(
      top_level: dict, workspace_overrides: dict
  ) -> dict:
      """Deep-merge workspace overrides onto top-level defaults.
      
      Merge happens at the dict level BEFORE dataclass construction.
      This ensures a workspace that only sets agent.default_agent
      inherits agent.provider from the top-level config, not from
      AgentConfig's dataclass default.
      """
      merged = copy.deepcopy(top_level)
      for key, value in workspace_overrides.items():
          if isinstance(value, dict) and isinstance(merged.get(key), dict):
              merged[key] = {**merged[key], **value}
          else:
              merged[key] = value
      return merged
  ```

  The rule: "not present in the workspace dict" means "inherit from top-level." Only keys explicitly written in the workspace override take effect. No sentinel values needed — the raw JSON dict is the source of truth for what was specified.

- **Migration** — existing flat `"workspaces": {"default": "workspace"}` auto-migrates to `{"default": {"dir": "workspace"}}` on first load. A `"default"` memory store is created automatically from the top-level `memory` section. `default_memory_store` defaults to `"default"`. `to_dict()` writes the new shape. Old shape still accepted by `load()`.

- **Schema registry** — `workspaces.*` and `memory_stores.*` entries use `additionalProperties` in JSON Schema (dynamic keys). Flat entries get `workspaces.*.dir`, `memory_stores.*.embedding_provider`, etc.

- **Dashboard** — workspace selector and memory store selector in settings panel. Each workspace shows its effective config (merged defaults + overrides). Each memory store shows its settings and stats.

What this enables:
- Different agents per workspace (oncall agent vs. coding agent)
- Independent memory stores that can be combined with any workspace
- An agent working in the `oncall` workspace can use `oncall-knowledge` memory, `shared-team` memory, or both — the binding is at session time, not config time
- Foundation for Phase 3's store-scoped `VectorMemoryStore` instances
- Future: agent auto-selects memory store based on task context + store descriptions

### Phase 3: Named Memory Stores (Runtime)

**Branch:** `feat/workspace-scoped-vector-memory` (parked, prototype exists — needs rebase)  
**Goal:** Each named memory store gets its own `memory.db` + `memory.faiss`. The agent session resolves which store to use.

The parked branch prototyped per-workspace stores. Phase 3 generalizes this to named stores (decoupled from workspaces):

- `context.py`: `get_memory_store_for(store_name, cfg)` with lazy cache in `_memory_stores` dict (renamed from `_vector_stores`)
- `history.py`: consolidator resolves the memory store from session metadata (which records both workspace and store name)
- `handlers.py`: memory API endpoints accept `?memory_store=` query param
- `gateway.py` / `cli.py`: resolve store name from session config, pass to `get_memory_store_for()`

Store resolution at session start:
1. Agent config specifies `memory_store` → use that
2. Not specified → use `default_memory_store` from global config (defaults to `"default"`)

**Memory boundary permeability (addressing review comment):** Memory stores are soft boundaries, not hard walls. The default behavior is isolation — each store has its own SQLite DB and FAISS index. But cross-store access is possible:

- **Read-through:** an agent can explicitly query another store via API (`GET /api/memory/semantic?memory_store=oncall-knowledge`). This is opt-in per request, not automatic.
- **ConversationLog stays global** — sessions already track their store in metadata. Cross-store history search works. Consolidation writes to the session's designated store.
- **Lessons are global by default** — `lesson.*` keys live in a shared store (or all stores, TBD). Corrections like "always use snake_case" apply everywhere.
- **Future: cross-store search** — a query could fan out to multiple stores with results merged and ranked. Not in Phase 3 scope, but the architecture doesn't block it.

Migration strategy:
- Existing global `memory.db` becomes the `default` store
- Store directory: `~/.kirocrew/memory_stores/{store_name}/` (new stores) or `~/.kirocrew/` (default store, backward compat)
- No data migration needed for existing users — `default` store points to existing files
- New stores start empty

### Phase 4: Plugin System — Swappable Memory Backends

**Branch:** TBD (after Phase 2, can parallel with Phase 3)  
**Goal:** Allow memory backends to be swapped via config without code changes.

Today the memory stack is hardcoded:
```
VectorMemoryStore → SQLite + FAISS (local)
LlamaCppEmbedder → vendored llama-cpp-python (in-process)
```

Phase 4 introduces a plugin interface:

```python
class MemoryBackend(ABC):
    """Plugin interface for memory storage backends."""
    
    @abstractmethod
    async def get_semantic(self, key: str) -> dict | None: ...
    
    @abstractmethod
    async def set_semantic(self, key: str, value: str, **kwargs) -> None: ...
    
    @abstractmethod
    async def search_episodic(self, query: str, **kwargs) -> list[dict]: ...
    
    # ... same surface as VectorMemoryStore's public API

class EmbeddingBackend(ABC):
    """Plugin interface for embedding providers."""
    
    @abstractmethod
    async def embed_one(self, text: str) -> list[float] | None: ...
    
    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]: ...
```

Config shape:
```json
{
  "memory_stores": {
    "default": {
      "backend": "sqlite_faiss",
      "embedding_provider": "llama_cpp"
    },
    "shared-team": {
      "backend": "remote_pgvector",
      "embedding_provider": "bedrock_titan",
      "remote": true,
      "backend_config": {
        "connection_string": "postgresql://...",
        "table_prefix": "kirocrew_"
      }
    }
  }
}
```

Key design decisions:

- **`backend` field** per memory store selects the `MemoryBackend` implementation. Default: `"sqlite_faiss"` (current behavior). Each store can use a different backend.

- **`embedding_provider` field** per store selects the `EmbeddingBackend`. Default: `"llama_cpp"` when enabled, `"none"` otherwise.

- **Per-plugin remote consent (addressing review comment)** — instead of piggy-backing on the embedding-specific `allow_remote_embedding` flag, each store/plugin that communicates with an external service declares `"remote": true`. The system enforces a general-purpose `allow_remote_access` top-level flag. A store with `"remote": true` is rejected at load time unless `allow_remote_access` is also true. This cleanly separates the consent mechanism from embedding semantics.

- **Plugin discovery** — plugins are Python entry points (`kirocrew.memory_backends`, `kirocrew.embedding_backends`). Built-in backends registered by default.

- **Schema extensibility** — the `kind` field on `ConfigEntry` already supports `"core"` vs `"plugin"`. Plugin configs contribute JSON Schema fragments merged into the root schema at runtime.

- **No new dependencies for core** — plugin implementations bring their own deps. The ABC interfaces live in `kiro_crew/plugins/base.py`.

Use cases this enables:
- Team-shared memory via remote PostgreSQL + pgvector
- Cloud-hosted embeddings (Bedrock Titan, OpenAI) for teams without local GPU
- Different backends per memory store (local for personal, remote for team)

## 3. Dependency Graph

```
Phase 1 (Config System)
  └──► Phase 2 (Agent Config + Workspaces + Memory Stores)
         ├──► Phase 3 (Named Memory Stores — runtime)
         │
         └──► Phase 4 (Plugin System)
                 ▲
                 : (soft — benefits from Phase 3)
```

Phase 1 is a hard prerequisite for everything else — without a formalized config system, workspace configs and plugin configs have no home.

Phase 2 is needed before Phase 3 because named memory stores need structured config definitions (not just a flat dict).

Phase 4 requires Phase 2 (plugin config needs the schema system). Phase 3 is a soft dependency — Phase 4 benefits from named stores being the first pluggable surface, but can proceed with just the `default` store.

## 4. Current State

| Phase | Status | Branch | Notes |
|-------|--------|--------|-------|
| 1 | Spec complete, implementation starting | `config-standarize-` | Tasks 1-2 designed, tasks 3-8 remain |
| 2 | Not started | — | Depends on Phase 1 merge |
| 3 | Prototype exists, parked | `feat/workspace-scoped-vector-memory` | Needs rebase + generalize to named stores |
| 4 | Design only | — | Depends on Phase 2 |

## 5. Risks & Open Questions

1. **Migration complexity** — Phase 2 changes `workspaces` from `dict[str, str]` to `dict[str, WorkspaceConfig]` and adds `memory_stores`. The auto-migration in `load()` needs to handle both shapes. (Proposal: shape detection is sufficient — if a workspace value is a string, wrap it in `{"dir": value, "memory_store": "default"}`. No config versioning needed.)

2. **Memory store selection** — The agent config is the binding point: each agent specifies its `workspace` and `memory_store`. Switching agents switches both. No separate user-facing memory store selection needed. (Proposal: agent config binding only. Future: agent reads store `description` metadata and dynamically selects based on task context.)

3. **Lesson scope** — Should lessons (`lesson.*` keys) be per-store or global? Per-store means "always use snake_case" only applies in one context. Global means it applies everywhere. (Proposal: global by default — lessons are corrections that should apply universally. Per-store lessons can be a future opt-in.)

4. **Cross-store search** — Should an agent be able to search across multiple stores in one query? Useful for "find everything I know about X" regardless of which store it's in. (Proposal: not in Phase 3 scope. The API supports `?memory_store=` per request. Fan-out search is a Phase 4+ feature.)

5. **Plugin security** — Plugins that send data to remote servers require explicit consent. (Proposal: per-store `"remote": true` flag + top-level `allow_remote_access` gate. No piggy-backing on `allow_remote_embedding`.)

6. **Rebase strategy for Phase 3** — The `feat/workspace-scoped-vector-memory` branch diverged from an older mainline. The core logic (lazy store cache, `get_vector_store_for()`) is sound but needs renaming (workspace → store) and the loader integration needs rewriting against the new config structure. (Proposal: cherry-pick `context.py` and `handlers.py` changes, rewrite loader integration.)

7. **Config file size** — With per-workspace overrides and multiple memory stores, `config.json` could grow. (Proposal: dashboard settings panel becomes the primary editing surface; raw JSON is power-user escape hatch.)

## 6. Success Criteria

- Phase 1: `pytest` passes, `GET /api/config/schema` returns all entries, existing configs round-trip without changes
- Phase 2: workspace selector works in dashboard, memory store selector works, per-workspace agent overrides apply correctly via dict-level merge, auto-migration from flat workspaces works, `default_memory_store` resolves correctly
- Phase 3: each named store has isolated `memory.db`, dashboard memory tab is store-aware, existing default store data untouched, consolidation writes to correct store
- Phase 4: at least one alternative backend (e.g. pgvector) works end-to-end via config change only, `"remote": true` consent enforced

## Appendix: Code Review Comments Addressed (rev 2)

| # | Author | Comment | Resolution |
|---|--------|---------|------------|
| 1 | Code-review bot | Evolution path diagram inconsistent with dependency graph | Reconciled both diagrams. Solid vs dashed arrows distinguish hard vs soft deps. |
| 2 | Code-review bot | Merge semantics ambiguity — dataclass defaults trap | Specified dict-level merge before dataclass construction. Added `resolve_effective_config()` code. |
| 3 | Code-review bot | `allow_remote_embedding` too narrow for plugin consent | Replaced with per-store `"remote": true` + top-level `allow_remote_access` gate. |
| 4 | Reviewer | Workspace as knowledge domain — richer metadata, AI selection | Fully decoupled memory stores from workspaces. They're orthogonal dimensions — agent picks both independently. Stores have `description` for future LLM-driven selection. |
| 5 | Reviewer | Memory boundary permeability — when to cross boundaries | Added boundary permeability section: soft boundaries, opt-in cross-store read, global lessons. |
| 6 | Reviewer | Test plan per phase | Added "Test Strategy" section with concrete test plans for all 4 phases. |
