# Memory, Skills & Hooks Modules

Last Updated: 2026-07-13 (skills lazy-load usage-ranked top-K + skill_search + SkillUsageLedger, /api/skills discovery_executor offload; pysqlite3, Snowball stemming, Docker fallback simplified)

## Overview

Persistent memory, skill system, and config-driven hooks. Assembled by `ContextBuilder` and injected into ACP prompts.

## Memory (`memory.py`)

Structured files under `~/.kirocrew/workspace/memory/`:
- `preferences.md` — learned user preferences (replaced wholesale by consolidator)
- `projects.md` — active project context (replaced wholesale by consolidator)
- `history/{date}.md` — daily conversation summaries (append-only, pruned by heartbeat)

FTS5 search via `~/.kirocrew/memory_index.db` (SQLite via `pysqlite3-binary` on Linux for FTS5/UPSERT compat, stdlib `sqlite3` on macOS). Self-healing: corrupted DB auto-rebuilt. Incremental updates on writes, full rebuild on gateway startup. Snowball stemming for keyword scoring. Connection leak prevention: all FTS methods use try/finally.

Context injection includes source citations per section. Agent can update memory files via kiro-cli's file tools.

### Decaying Memory (`read_recent_history`)

History context uses natural decay — recent days in full detail, older days compressed:
- **Last 14 days**: full entries (days 0–13, vivid recall)
- **14-60 days ago**: first entry per day + count (fading summary)
- **61-180 days ago**: date + entry count only (existence marker)
- **181-364 days ago**: retained on disk but not loaded into context
- **365+ days**: pruned by heartbeat (forgotten)

Total output capped at `history_cap = 25_000` chars in `get_context()`. Timestamps use local timezone.

`read_recent_history` runs on every message turn (context build) and otherwise
stats + reads up to 181 daily files synchronously. The assembled string is
TTL-cached (`_HISTORY_CACHE_TTL_SECS = 5.0`) on the `MemoryStore` instance,
keyed on `(days, today)` so the decay window shifting at midnight invalidates
naturally; `append_history` and `prune_history` call `_invalidate_history_cache()`
so a new or pruned entry is visible immediately.

### History Pruning

`prune_history(keep_days)` deletes daily files older than `keep_days` (default 365). Runs once per day via heartbeat (`_PRUNE_TICKS = 1440`). Parses `YYYY-MM-DD.md` filenames, skips non-date files.

### Consolidation (`history.py` `HistoryConsolidator`)

Two separate consolidation paths with independent triggers:

| Path | Trigger | What it updates | Offset tracking |
|------|---------|-----------------|-----------------|
| Preferences/projects | 30 messages (per session) | `preferences.md`, `projects.md` | In-memory `_prefs_offset` dict |
| Daily history + lessons | 3h idle (per session) | `history/{date}.md`, `lessons.jsonl` (or `lesson.*` in vector store) | Persisted `last_consolidated` in JSONL metadata |

The prefs path does NOT advance the persisted `last_consolidated` marker — only the history path does. This ensures history consolidation always covers all messages, even if prefs consolidation fired earlier.

Idle detection: `_last_activity[key]` updated on every `maybe_consolidate()` call. `check_idle_sessions()` called every heartbeat tick (60s), fires history consolidation when `now - last_activity > history_idle_secs` and there are unconsolidated messages.

### Lesson Extraction from Chat

The history consolidation prompt includes a `"lessons"` key that extracts only implicit correction patterns — corrections the user made without explicitly saying "remember" (those are already saved immediately via `learn_add`). All lesson writes go through `write_lesson()` which provides substring dedup and topic-overlap dedup (>50% keyword overlap → newer replaces older). When vector memory is not active, falls back to `lessons.jsonl` via `LessonStore.save()`.

### Configuration

`~/.kirocrew/config.json` → `"memory"` section:
```json
{"history_idle_hours": 3.0, "history_max_days": 365}
```

Exposed on dashboard: Overview → Memory tab → Memory Settings card. Changes apply immediately to running consolidator via `PUT /api/memory/settings`.

## Vector Memory (`vector_memory.py`)

Opt-in structured memory system backed by SQLite + FAISS + local Ollama embeddings. Off by default — enabled via dashboard "Enable Vector Memory" button.

### Semantic Memory

SQLite table `semantic_memory` — structured key-value store with:
- **Allowed keys**: only `pref.*`, `project.*`, `user.*` prefixes allowed (+ user-configurable extras)
- **Confidence gating**: LLM writes require confidence ≥ 0.8; user-explicit writes always win
- **Conflict resolution**: higher confidence wins; same confidence → newer source wins; user-explicit overrides all
- **Injection detection**: 14 regex patterns scanned on every value write
- **Audit trail**: `memory_events` table logs every create/update/delete with old+new values

Context injection: formatted as `key: value` pairs in `[Semantic Memory]` block, capped at `_SEMANTIC_MEMORY_CAP` (≈12.7k chars) when injected at session start via `get_context()`. Excludes `lesson.*` keys (they have their own `[Learned corrections]` block). Uses hybrid retrieval when embeddings are available: `0.6 × vector_score + 0.4 × keyword_score`. Falls back to keyword-only scoring (word overlap on keys and values, with Snowball stemming) without embeddings.

### Episodic Memory

SQLite table `episodic_memories` — conversation fragments with optional embeddings:
- **Write**: text validation (10-2000 chars), **prompt-injection screening** (`_contains_injection`, same pattern set as the semantic-KV path), tag sanitization, importance clamping (0-1), FAISS dedup (cosine > 0.88)
- **Injection screening (XPIA defense-in-depth, Talos 696671aa)**: episodic text is derived from conversation transcripts, so a poisoned turn could persist steering instructions that get re-injected into future contexts. `write_episodic()` now runs `_contains_injection()` (before the embed call) and, on match, drops the entry and emits an auditable `injection_blocked` event with `memory_type='episodic'`. The stored audit snippet is scrubbed with `redact_exfiltration_urls()` + `redact_credentials()` first, since `/api/memory/events` surfaces it verbatim on the dashboard. This mirrors the semantic-KV screen at `validate_semantic()`. **Residual (accepted risk)**: this is a best-effort regex screen — a determined owner can still steer their own long-term memory with phrasing that evades the patterns; long-term memory poisoning is an accepted residual. The screen raises the bar against accidental/opportunistic XPIA persistence, not against a motivated self-owner.
- **Search**: FAISS vector similarity with decay scoring: `cosine_sim × (0.7 + 0.3×importance) × exp(-0.03×days)`, then MMR diversity reranking (Jaccard-based, λ=0.6)
- **MMR reranking**: Maximal Marginal Relevance balances relevance with diversity. Greedy iterative selection penalizes candidates similar to already-selected results. Prevents redundant episodic fragments from consuming the context budget. Configurable via `mmr=False` parameter to disable.
- **Relevance threshold**: `cosine_sim ≥ 0.55` required for context injection (empirically determined from 100-query benchmark: 50 relevant + 50 irrelevant, F1=0.980). Results below threshold are filtered in `get_episodic_context()` only — `search_episodic()` returns all results for dashboard/API use. FTS5 keyword fallback is unaffected (no cosine scores).
- **Fallback**: keyword search (OR logic, LIKE on text + tags) when embeddings unavailable
- **Cap**: 10,000 active entries; lowest-importance oldest pruned when exceeded

Context injection: top-8 results in `[Episodic Memory]` block, capped at 3000 chars (`cap=3000`). Injected on the first message of new sessions via `build_message()` — not at plain session start, since `build_session_context` passes no query to `memory.get_context()`.

### Embedding Client (`embeddings.py`)

Async HTTP client for local Ollama server (`localhost:11434`):
- `embed_one(text)` / `embed_batch(texts)` → returns 1024-dim vectors or `None` on error
- Ollama API: `POST /api/embed` with `{"model": "qwen3:0.6b", "input": [...]}`
- Localhost-only URL validation (rejects remote servers, credentials in URL)
- **SSRF protection on the `allow_remote` path (`_validate_url` + `_resolve_blocked_addr`):** even when the owner opts into a remote embedding server (`allow_remote_embedding: true`, https-only), an IP-*literal* host is rejected (raising `ValueError`, so the caller disables embeddings) if it is a private / loopback / link-local / reserved / multicast / unspecified address — covering the IMDS endpoints (`169.254.169.254`, `fd00:ec2::254`), all RFC1918 space (`10/8`, `172.16/12`, `192.168/16`), `127/8`, and `::1`. IPv4-mapped IPv6 (`::ffff:a.b.c.d`) is unwrapped so a mapped internal address cannot slip through. **Trailing-dot normalization (Heimdall follow-up):** a fully-qualified trailing-dot literal (`169.254.169.254.` / `127.0.0.1.`) is rejected by both `ipaddress.ip_address` and `socket.inet_aton`, so it used to fall through as a DNS name and slip past the check; `addr_clean` is now `rstrip('.')`-normalized before parsing (the kernel/resolver treats the FQDN form as the same address; still **no DNS**), while the trailing-dot form of a public literal stays allowed. **Alternate-encoding normalization (Heimdall follow-up to CR-289119233):** because `ipaddress.ip_address` accepts only the canonical dotted-quad / RFC-5952 forms, alternate IPv4 literal encodings — hex (`0x7f000001`), decimal (`2130706433`), octal (`017700000001`), short-form (`127.1`) and the IMDS variants (`0xa9fea9fe` / `2852039166` / `169.254.43518`) — used to fall through the `ValueError` branch as if they were DNS names, letting aiohttp connect straight to loopback/IMDS. The `ValueError` branch now re-parses `addr_clean` through `socket.inet_aton` (the same permissive parse the C resolver/kernel performs, **pure string parse — no DNS**): `OSError` means a genuine DNS name (falls through to the accepted name-based residual), any other exception **fails CLOSED** (treated as blocked), and success is re-classified as an `IPv4Address` against the same private/loopback/link-local/reserved/multicast/unspecified rejection. The classification remains **pure / non-blocking — no DNS**: nothing on this sync/event-loop path calls `socket.getaddrinfo` (satisfies the whole-tree no-blocking-call gate). A DNS *name* is therefore not resolved here and passes the literal check. Malformed / hostless URLs are **denied (fail-closed)** rather than allowed. **Residual (accepted risk):** name-based / DNS-rebinding TOCTOU — a hostname that points at a private/metadata address (at validation and/or request time) is not caught, because the name is never resolved on the loop. Addresses Talos finding `76640a75`.
- Rate-limited warnings (once per 60s on repeated failures)
- Health check: `GET /api/tags` — verifies server running AND model loaded

**Sync embedding cache** (`make_sync_embed_fn`): The sync callable used by `vector_memory.py` caches results via `functools.lru_cache` keyed by input text. Embeddings are deterministic (same text → same vector for a given model), so caching is safe. Bounded to 128 entries (~4 MB with Python boxed floats). Failures (None) are not cached — `lru_cache` does not cache exceptions, so transient errors are retried. Cache stats logged every 20 misses. Cache lives per `make_sync_embed_fn()` call — reset when embeddings are disabled/re-enabled or gateway restarts.

### Ollama Manager (`embeddings.py`)

Manages Ollama server lifecycle and model provisioning:

**Install** (`install_ollama()`):
- macOS: `brew install ollama` (requires Homebrew), fallback to direct binary download
- Linux: `brew install ollama` or `curl -fsSL https://ollama.com/install.sh | sh`
- Docker fallback code preserved for runtime recovery (triggers only if native binary fails with GLIBC error and brew is unavailable)
- Only triggered from dashboard "Enable Vector Memory" or gateway startup when `embedding_provider: "ollama"`

**Docker fallback** (legacy, last-resort only):
- Only activated when native binary crashes with GLIBC error **and** brew reinstall fails (or brew is unavailable)
- Preserved for backwards compatibility but not recommended — `brew install ollama` resolves glibc issues
- `_needs_sudo_cache` persists across `OllamaManager` instances within the gateway process

**Model loading** (`pull_model()`):
- Clones `KiroCrewModelQwen3Embedding` from Gitfarm (internal, no external model download)
- Finds `qwen3-embedding-0.6b.gguf` (Q8_0 quantized, 610MB) in cloned package
- Creates Ollama model via `ollama create qwen3:0.6b -f Modelfile` from local GGUF
- IMPORTANT: `ollama pull qwen3:0.6b` from registry is a CHAT model — does NOT support embeddings
- Only the Gitfarm GGUF produces a working embedding model
- No fallback to Ollama registry — internal package is the only model source

**Server** (`start_server()` / `stop()`):
- Native: starts `ollama serve` as subprocess (Metal GPU on macOS, CUDA/CPU on Linux)
- Docker fallback: `docker rm -f kirocrew-ollama` then `docker run -d` (only if native fails and brew unavailable)
- Health polling with 30s timeout
- SIGTERM → SIGKILL cleanup (native) or `docker stop` (Docker)

**Dashboard Enable Flow** (retryable):
- `POST /api/memory/enable-embeddings` — installs Ollama, starts server, loads model
- On failure: status resets to `idle` with error message, frontend shows error + 🔄 Retry button
- Prevents concurrent setup attempts (409 if already in progress)
- `can_retry` flag in status response for frontend retry button
- Progress steps: `checking` → `installing_ollama` (or `installing_docker` if Docker fallback) → `starting` → `downloading` → `done`

### Model Security & Policy

| Field | Value |
|-------|-------|
| Model | Qwen/Qwen3-Embedding-0.6B (Q8_0 GGUF) |
| License | Apache-2.0 (on approved list for self-approval) |
| Source | public Ollama registry (`ollama pull qwen3-embedding:0.6b`) |
| Runtime | Ollama (MIT license, native via brew or install script) |
| Data flow | Text → localhost:11434 → float vectors (no data leaves machine) |
| Policy | Self-approvable under [Public Dataset and ML Model Policy](https://policy.a2z.com/docs/83291/publication) |

Conditions met for self-approval:
1. Internal use only — model runs locally, no 3P API calls
2. Apache-2.0 license — on approved list
3. Outputs are float vectors — no excluded categories (health, financial, biometric, PII)
4. Not recreating training data — generating embeddings, not content
5. Model weights sourced from internal Gitfarm package (no 3P model download at runtime)

### Why Ollama (not TEI)

TEI (Text Embeddings Inference) uses the candle Rust framework with a Metal backend that has an [unmerged memory bug](https://github.com/huggingface/candle/pull/3197) causing unbounded GPU buffer allocation on macOS. The process consumes 4+ GB RAM and never becomes healthy. This affects ALL models on TEI/Metal, not just Qwen3. Ollama uses llama.cpp which works correctly on all platforms (macOS Metal, Linux CUDA/CPU).

### Lessons in Vector Memory

When vector memory is active, lessons are stored as semantic entries:
- Key: `lesson.<md5_of_rule>` (dedup via hash)
- Value: `"rule text"` or `"rule text — NOT: negative text"`
- Confidence: 1.0 for `user_explicit`, 0.9 for `migration`
- Methods: `write_lesson()`, `get_lessons()`, `delete_lesson()`, `get_lessons_context()`
- Context: injected as `[Learned corrections]` block, separate from `[Semantic Memory]`
- Allowlist: `lesson.*` prefix in `_BUILTIN_PREFIXES`
- `start()` / `stop()` — subprocess lifecycle (SIGTERM → SIGKILL, same pattern as kiro-cli)
- `ensure_running()` — auto-start on dashboard "Enable Vector Memory" click when `embedding_provider: "ollama"`

Model: `Qwen/Qwen3-Embedding-0.6B` Q8_0 GGUF (610MB). Apache-2.0 licensed. Served via Ollama on all platforms.

### Consolidation Integration

`HistoryConsolidator._consolidate()` now extracts structured data alongside existing fields:
- `"semantic"` array → `write_semantic()` for each (max 20 per consolidation)
- `"episodic"` array → `write_episodic()` for each (max 10 per consolidation)
- Dual-write mode: when `config.memory.migrated` is False, also writes markdown files (backward compat)

### Dashboard Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/memory/semantic` | List all semantic entries |
| PUT | `/api/memory/semantic` | Create/update (validates key, allowlist, injection) |
| DELETE | `/api/memory/semantic/{key}` | Tombstone + log event |
| GET | `/api/memory/events` | Recent audit trail |
| GET | `/api/memory/episodic` | Paginated episodic list |
| GET | `/api/memory/episodic/search?q=` | Search episodic memories |
| DELETE | `/api/memory/episodic/{id}` | Tombstone episodic entry |
| GET | `/api/memory/stats` | Counts, index size, provider status |
| GET | `/api/memory/embedding-status` | Embedding system health |
| POST | `/api/memory/enable-embeddings` | Install Ollama + load model from Gitfarm + update config |
| POST | `/api/memory/disable-embeddings` | Set provider to none (Ollama keeps running) |
| POST | `/api/memory/migrate` | Migrate markdown → structured memory |
| POST | `/api/memory/import` | Import from JSON export |
| GET | `/api/memory/context-preview?q=` | Preview injected semantic + episodic context |

### CLI

`kirocrew memory {list,search,stats,audit,export,migrate,import}` — manage vector memory from command line:
- `migrate` — one-time markdown → structured migration (preferences.md → semantic, history/*.md → episodic)
- `import <file>` — restore from JSON export with full validation
- `kirocrew security audit` also scans vector memory for injection patterns

### Migration (`migrate_from_markdown`)

Parses legacy markdown files into structured memory:
- `preferences.md`: bullet points with `key: value` → semantic entries (confidence 0.85, source "migration"). Bare prefix keys get `.default` suffix.
- `projects.md`: project names → `project.name` semantic entries, details → episodic
- `history/*.md`: daily summaries → episodic entries (importance 0.4)
- **Embedding during migration**: when Ollama is enabled, the dashboard handler sets `store.embed_fn` before calling migration. Each episodic entry is embedded via Ollama and stored with its FAISS vector, enabling vector search immediately after migration.
- Idempotent: re-running skips existing semantic entries (conflict resolution), episodic dedup via FAISS when available
- Dashboard: "📦 Migrate from Markdown" button shown when `migrated=false` and legacy files exist

### Cross-Platform

macOS (Apple Silicon) and Linux (x86_64, arm64/Graviton) supported. All paths use `pathlib.Path`. GGUF model loaded from internal `KiroCrewModelQwen3Embedding` Gitfarm package (no external model downloads).

| Platform | Ollama Install | GPU | Notes |
|----------|---------------|-----|-------|
| macOS (Apple Silicon) | `brew install ollama` or direct download | Metal | Native, fastest |
| Linux glibc ≥ 2.27 (AL2023+) | `brew install ollama` or `curl install.sh` | CUDA if available | Native |
| Linux glibc < 2.27 (AL2) | `brew install ollama` or Docker fallback | CPU only | Brew avoids glibc issues |

## Lessons (`learn.py` → `vector_memory.py`)

User-taught corrections ("always do X", "never do Y"). Single write path through `vector_memory.write_lesson()`:

1. **Vector memory** (primary): stored as `lesson.<md5hash>` semantic entries with `confidence=1.0, source=user_explicit`. Negative rules stored as `"rule — NOT: negative"`. Injected via `get_lessons_context()` — separate from `[Semantic Memory]` block.
2. **JSONL fallback** (`~/.kirocrew/lessons.jsonl`): only used when vector memory is not initialized. Read-only migration source once vector memory is active.

**Priority**: vector lessons override JSONL. If `vector_store.get_lessons()` returns entries, JSONL is skipped entirely.

**Single write path** — all lesson writes go through `write_lesson()` which provides:
- Substring dedup: "use dark mode" won't duplicate "always use dark mode"
- Topic-overlap dedup: "use light mode" replaces "use dark mode" (>50% keyword overlap → newer wins)
- Allowlist validation, injection scanning, audit logging

**Write sources**:
1. **`learn_add` MCP tool** (immediate): user says "remember X" → LLM calls tool → `POST /api/lessons` → `write_lesson()`
2. **Task runner** (on failure): step fails → LLM extracts lesson → `write_lesson(source="task_runner")`
3. **Consolidation** (background): extracts only implicit corrections not already saved via `learn_add` → `write_lesson(source="consolidation")`
4. **Dashboard/CLI** (manual): `POST /api/lessons` → `write_lesson()`

**Migration**: `migrate_from_markdown()` reads `lessons.jsonl` and writes each entry as `lesson.*` semantic key with `source=migration, confidence=0.9`. User-explicit lessons (confidence 1.0) can't be overwritten by migration.

Categories: `tool`, `preference`, `knowledge`. Injected as `[Learned corrections]` block, capped at 50.

## Skills (`skills.py`)

Markdown files at `~/.kirocrew/skills/{name}/SKILL.md` with optional YAML frontmatter (`name`, `description`, `always`).

Supports nested directories (e.g. `skills/utils/tiny-url/SKILL.md`). The skill name is the relative path from the skills root (e.g. `utils/tiny-url`).

**Source precedence** (project-level wins): `$KIROCREW_PROJECT_DIR/skills/` → `builtin_skills/` (bundled). Auto-copied to `~/.kirocrew/skills/` on first run. Copies entire skill directories (scripts, assets, etc.).

**Loading:**
1. **Always-on**: skills with `always: true` have full content injected every new session
2. **On-demand**: skill summaries (name + description + dir path) in session context; LLM can `cat` the file when relevant

Skills with auxiliary files (scripts, assets) include `dir` path so the LLM can `cd` and run them.

**Lazy-load (`skills.lazy_load`, default false — loader `SkillsConfig`):** controls how `get_context(budget)` (`skills.py`) injects the on-demand set.
- **OFF** (`get_context(budget=None)`): the byte-for-byte legacy full dump — every on-demand skill summarized, unranked and untruncated, under the flat 165k `_CONTEXT_BUDGET_BASE`.
- **ON** (`get_context(budget)`): `always: true` pinned skills are injected in full, plus a usage-ranked **top-K** of on-demand skills filled up to `budget`. Ranking is by `_rank_key` (`skills.py`) — `(usage_hits, effective_recency)` from the `SkillUsageLedger`, with a recency boost so freshly-added skills escape cold start. The long tail is left discoverable via the `skill_search` tool, the `$skillname` inline token, `cat`, and the per-message trigger auto-loader.

**Usage ledger (`skill_usage.py`, `SkillUsageLedger`):** in-memory per-skill hit tally with debounced, atomic persistence to `skill-usage.json` (`SKILL_USAGE_FILENAME`, co-located with the KiroCrew home). Entries older than a 30-day TTL (`_MAX_AGE_SECS`) are dropped on load/flush so a stale skill stops occupying a top-K slot. Hits are recorded in `get_triggered_skills` (`_record_use`) and `resolve_dollar_skills` **regardless of the `lazy_load` flag**, so ranking data accrues even while the feature is off. Best-effort: ledger init failure falls back to recency-only / unweighted ranking without breaking skill loading.

**`skill_search` MCP tool (`kirocrew-core`):** greps skill name/description then, only on a metadata miss, the skill body (bounded, tool-call only — never per message). Schema in `mcp_core.py`, validated against `SKILL_SEARCH_SCHEMA` (`validation.py`). Does NOT record usage — searching is not using.

**Trigger matching (`get_triggered_skills`) — per-message hot path.** Runs on
every non-custom-agent message via the context builder, scoring word-overlap of
the message against each skill's `triggers` (negative `!`-prefixed triggers
exclude). To keep it off the per-message filesystem/config hot path:
- the discovered skill-file list is TTL-cached (`_iter`, `_ITER_CACHE_TTL_SECS`),
  invalidated by `create_auto_skill`;
- the `max_triggered` cap is snapshotted on the loader in `__init__`
  (`self._max_triggered`) — no `KiroCrewConfig.load()` per message — refreshed
  when the loader is rebuilt (per gateway), matching `extra_paths` semantics;
- exactly **one** SEL audit event is emitted for the matched set (skipped
  entirely when nothing matched, the common case), not one per skill scanned.

**CRUD operations** (via `SkillsLoader`):
- `create_skill(name, content)` — creates `{name}/SKILL.md`, supports nested paths
- `update_skill(name, content)` — overwrites existing SKILL.md
- `delete_skill(name)` — removes entire skill directory
- Path traversal protection: `_safe_name()` rejects `..` and `\` (allows `/` for nesting)

**Dashboard endpoints**: GET/POST `/api/skills`, GET/PUT/DELETE `/api/skills/{name:.+}`. POST sanitizes name to lowercase + hyphens + slashes. GET `/api/skills` discovery (kirocrew `list_skills()` os.walk + frontmatter, `list_kiro_skills`, and the skill→agent annotation) is fully offloaded to the dedicated `discovery_executor` pool (`executors.py`) via `collect_skills_blocking`, so it never stalls the event loop past the loop-stall watchdog on large catalogs. The annotation is O(agents) — `annotate_skills_with_agents` parses the agent JSONs and pre-expands each agent's `skill://` globs once, then matches every skill against that in-memory set. The discovery pool is deliberately separate from the reaper-critical `maintenance_executor` so browser-triggered scans can't starve the orphan sweep.

**LLM tool mechanisms:**
- MCP tools (native): kiro-cli calls directly — **preferred for all LLM-facing operations**
  - `kirocrew-cron`: cron scheduling
  - `kirocrew-core`: spawn, learn, task tools
- Skills are for on-demand knowledge only (not for CLI command wrappers — use MCP tools instead)

## MCP Discovery (`mcp_discovery.py`)

Auto-sync at startup + on-demand discovery from dashboard. Default servers: `kirocrew-cron`, `kirocrew-core`.

**Server sources** (merged by `list_servers()`):
1. `agents/defaults.json` → `mcpServers` (default: none beyond the managed servers)
2. `~/.kiro/agents/kirocrew.json` → `mcpServers` (installed config, merged)
3. `~/.kiro/settings/mcp.json` and `~/.kirocrew/mcp.json` (scanned at startup and on-demand)

**Startup behavior**: gateway calls `_init_mcp_discovery()` which runs `discover_servers_to_sync()` + `sync_to_agent_config()` to auto-add new servers from mcp.json, then logs all configured servers. Discovery/sync failures are caught independently so `list_servers()` always runs. Additionally, `server.py` fires `_bg_mcp_probe()` as a background task at startup to populate the probe cache.

**sync_to_agent_config()**: registers servers via `kiro-cli mcp add` in parallel (all Popen spawned at once, then waited), followed by a single config patch pass for `tools`/`allowedTools`. Atomic write (tmp + rename) prevents corrupted config. Checks returncode, logs stderr on failure, separate timeout handling. Falls back to direct JSON edit if kiro-cli unavailable.

**On-demand discovery** (dashboard): same `discover_servers_to_sync()` + `sync_to_agent_config()` triggered by "Discover & Sync" button.

**Probing**: spawns each MCP server, sends JSON-RPC `initialize` + `tools/list` handshake, reports status + tool names. 30-second timeout, 1MB stdout buffer (an MCP server's responses exceed the default 64KB). Cleanup via `finally` block (no zombie processes). Results cached in `handlers.py` with 10-min TTL; GET `/api/mcp/probe` returns cached results non-blocking, POST `/api/mcp/probe` forces a fresh probe and updates cache.

**Enable/Disable**: `POST /api/mcp/toggle` adds/removes `@name` from `tools` and `allowedTools` arrays in installed config (`~/.kiro/agents/kirocrew.json`). Does NOT modify `agents/defaults.json`. Disabled servers stay in `mcpServers` but kiro-cli won't load their tools.

**Sync**: `POST /api/mcp/sync` uses `kiro-cli mcp add --agent kirocrew --force` to properly register new servers with kiro-cli. Falls back to direct JSON edit if kiro-cli unavailable. After sync, all active sessions are reset so kiro-cli picks up the new config (~30s).

**Dashboard workflow**: ① Probe All → ② Enable/Disable → ③ Apply & Restart Sessions.

**Dashboard endpoints**: GET `/api/mcp` (list with enabled state from installed config), GET `/api/mcp/probe` (cached probe results, non-blocking), POST `/api/mcp/probe` (live probe all, updates cache), POST `/api/mcp/sync` (on-demand discover + add + session reset), POST `/api/mcp/toggle` (enable/disable in installed config).

## Auto Skill Creation (`skills.py` + `history.py`)

Hermes-style autonomous skill creation from completed sessions (Mesh-677). Disabled by default; opt-in per user via `skills.auto_create_from_sessions`.

### Flow

```
session ends → HistoryConsolidator (3h idle path)
            → LLM consolidation prompt gains new_skill / refined_skill keys
            → result piped through redact_credentials + redact_exfiltration_urls
            → SkillsLoader.find_similar() dedup check
            → SkillsLoader.create_auto_skill() writes SKILL.md under auto/<slug>/
            → SEL audit event emitted
```

No new timer, no new background task — piggybacks on the existing idle-fired `HistoryConsolidator._consolidate()` path. The auxiliary LLM already runs on the background kiro-cli session every 3 hours of idle per session; the auto-skill keys are appended to the same JSON the LLM already returns.

### Eligibility gate (`_count_tool_call_messages`, `_session_touched_sensitive`)

Prompt keys are only appended when ALL hold:

| Condition | Source |
|-----------|--------|
| `skills.auto_create_from_sessions: true` | Config flag, default off |
| `skills_loader` instance passed | Wired from `slack/gateway.py` + `cli.py` |
| `include_history=True` | Idle path only, not prefs-only |
| `≥ skills.auto_min_tool_calls` messages with non-empty `tools` | Default 5 |
| No tool in the session referenced `~/.aws`, `~/.ssh`, IMDS, etc. | `_SENSITIVE_TOOL_PATTERNS` |

### Namespace

Auto-generated skills live under `~/.kirocrew/skills/auto/<slug>/SKILL.md`. Slug validated against `^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$`. The `auto/` prefix:
- Makes provenance visible without parsing frontmatter (`list_auto_skills()`)
- Prevents accidental overwrite of hand-authored skills via the refine path (`update_auto_skill()` explicitly refuses names outside `auto/`)

### Provenance (`AutoSkillProvenance`)

Serialized into SKILL.md YAML frontmatter on every create/refine:

```yaml
---
name: auto/grep-with-context
description: Search log files with grep then contextualize hits
triggers: grep, log search, context lines
source: auto
session_key: dashboard:chat-1
created_at: 2026-05-05T11:30:00+00:00
refined_at: 2026-05-06T09:15:00+00:00   # omitted until first refinement
reuse_count: 0                          # omitted when zero
---
```

`source: auto` is the canonical marker — hand-authored skills omit it.

### Safety rails (non-negotiable per `security.md`)

1. **Sensitive-session skip** — `_session_touched_sensitive()` scans all tool names across the session; any match in `_SENSITIVE_TOOL_PATTERNS` (AWS/SSH/GPG/netrc/.env/IMDS) skips extraction entirely. Complements the runtime hook-layer block; if the LLM *tried* to read credentials, we still don't synthesize a skill from the session.
2. **Output redaction** — `redact_credentials()` + `redact_exfiltration_urls()` applied to `description`, `triggers`, and `procedure_md` before the SKILL.md is written. `AKIA*`, `ASIA*`, private key headers, Slack tokens, base64-encoded credentials all get scrubbed. Defense even against a prompt-injected LLM that tries to embed credentials in the procedure.
3. **Size cap** — `AUTO_SKILL_MAX_PROCEDURE_CHARS = 10_240`; oversized outputs are rejected entirely (indicates the aux LLM went off-task).
4. **Similarity dedup** — `find_similar()` rejects near-duplicates above `skills.auto_similarity_threshold` (default 0.85) Jaccard overlap on description words.
5. **Namespace lock** — `update_auto_skill()` refuses to touch any skill whose name doesn't start with `auto/`, preventing the refine path from ever clobbering hand-authored skills.
6. **SEL audit** — every create/refine/dedup-rejection emits `tool_name=auto_skill_create` or `auto_skill_refine` to the security event log with session key + skill name metadata.

### Refinement (`skills.auto_refine_on_deviation`)

Opt-in secondary flag, gated by `auto_create_from_sessions`. When on, the consolidation prompt also asks for a `refined_skill` object. LLM judges whether a previously-loaded `auto/...` skill's procedure was improved during the session; if so, returns an updated body. No explicit tool-sequence tracking — the LLM reads both the loaded skill content (from session context) and the actual transcript and makes the call. Same safety rails apply; refine always writes to the same `auto/<slug>/SKILL.md`, never to a new file.

### Config (`config.json` → `skills`)

```json
{
  "skills": {
    "max_triggered": 3,
    "auto_create_from_sessions": false,
    "auto_refine_on_deviation": false,
    "auto_min_tool_calls": 5,
    "auto_similarity_threshold": 0.85
  }
}
```

### CLI

No new command. Users interact via the existing skill management surface:

- Enable: `kirocrew config set skills.auto_create_from_sessions true`
- List auto skills: filter `kirocrew` skill listings to those under `auto/`, or use `SkillsLoader.list_auto_skills()` in code
- Remove unwanted auto skill: `rm -rf ~/.kirocrew/skills/auto/<slug>` (or dashboard skill delete when UI lands)
- Audit trail: `kirocrew security events -n 20 | grep auto_skill`

## Hooks (`hooks.py`)

Config-driven from `config.json` → `hooks` section:
- **auto_approve_tools** / **auto_deny_tools** — tool patterns (exact, `prefix*`, `*suffix`, `*contains*`)
- **auto_replies** — pattern → direct reply (skip ACP entirely)
- **transforms** — pattern → prefix prepended to message
- **context_rules** — trigger keywords → context injected into message

Hook evaluation order: deny overrides approve; auto-reply → transform → context rules.

### `safe_read_file(path: str) -> str`

Central guarded file read. Resolves the path via `expanduser().resolve()`, checks against
`is_sensitive_path()`, and raises `PermissionError` if blocked. All file reads outside of
kiro-cli tool calls must go through this function — never call `is_sensitive_path()` inline.

### `safe_read_file_internal(read_id: str) -> bytes | None` (audited carve-out)

A narrow, hardcoded allowlist (`_INTERNAL_READ_ALLOWLIST`) lets specific **system-internal**
readers read an otherwise-sensitive path (today only the kiro-cli SSO token, read to call the
CodeWhisperer `GetUsageLimits` API that powers the dashboard credit pill). It re-checks
`is_sensitive_path()` (defense in depth), emits an SEL audit on every outcome, and is
**fail-closed**: a `success` read whose audit cannot be recorded synchronously (`critical=True`)
returns `None` instead of the bytes — a `logger.warning` is not itself an audit. Credential-bearing
paths that are *not* sensitive (e.g. the kiro-cli SQLite auth store under `~/.local/share`) use the
sibling `emit_internal_read_audit(read_id)` — same audit + fail-closed contract, gated by its own
`_AUDIT_ONLY_READ_IDS` registry. Adding an allowlist entry is a security-review event; the bytes
never reach an LLM/agent surface.

### User kiro-cli Hooks (`agent.kiro_hooks` in `config.json`)

User-defined kiro-cli hooks that persist across `kirocrew update`. Follows the
`removedTools` precedent — a raw key in `~/.kirocrew/config.json` read by
`_refresh_dynamic_fields()` at install time.

```json
{"agent": {"kiro_hooks": {"preToolUse": [{"matcher": "*", "command": "/path/to/hook.sh"}]}}}
```

Merge rules (implemented in `_merge_kiro_hooks()` in `agent.py`):
- Bundled hooks from `config/defaults.json` are always present and always first
- User hooks are appended per event type after bundled hooks
- Deduped by `(command, matcher)` tuple — same hook won't fire twice
- Malformed entries (missing `command`, non-dict, non-list) are skipped with warning
- Commands are validated via allowlist regex (`[a-zA-Z0-9/_.-]`), must be absolute paths to existing files, not in sensitive locations (`is_sensitive_path`); symlinks and path traversal are resolved before the sensitive-path check
- Matcher values must be strings; non-string matchers are skipped
- Matcher content is validated via allowlist regex (`[a-zA-Z0-9_.*-]`) with a 200-char max length
- Only `command` and `matcher` fields are kept from user entries; arbitrary extra keys are stripped
- Applied in both `build_agent_config()` (fresh install) and `_refresh_dynamic_fields()` (existing config refresh)

## Context Builder (`context.py`)

Assembles all sources into prompts:
- New session: `_CRITICAL_RULES` (diff blocks + OPTIONS buttons) + agent prompt + memory (with citations) + skills + lessons + conversation history (last 20 messages, thread history at TOP with explicit framing)
- Every message: channel history, episodic memory, hook transforms, triggered skills, context rules, OPTIONS hint (interactive sessions only)
- Thread history is injected only at session start (via `build_session_context`). Within the same ACP session, kiro-cli manages conversation history natively — duplicate injection wastes context window and accelerates compaction.
- `_CRITICAL_RULES` injected for ALL agents (including custom) at session start — ensures diff rendering and OPTIONS buttons work universally
- Cap: 165k chars max by default (`_CONTEXT_BUDGET_BASE`, single flat pool). With `skills.lazy_load` opt-in (default off), the single flat pool is replaced by independent per-section percentage caps (`_SKILLS_CAP`=15%, `_STEERING_CAP`=10%, `_LESSONS_CAP`=22.6%, `_MEMORY_HISTORY_CAP`=16%, `_SEMANTIC_MEMORY_CAP`/`_EPISODIC_MEMORY_CAP`=7.7% each, …) whose sum (plus a preamble headroom) is `_MAX_CONTEXT_CHARS` (~190k). This per-section budgeting is used only when `lazy_load` is on, so skills/steering can't crowd out memory/lessons (`context.py`).

#### Dynamic budget scaling (per active model context window)

The `_CONTEXT_BUDGET_BASE` (165k) and its derived per-section caps above are the **1M-reference** values — the base was hand-tuned for a 1M-token window, so each section has a fixed *share of that window*. When a session runs on a **smaller-window** model (e.g. Opus 4.8 200K), injecting the same absolute char counts would consume ~5× the proportional share and accelerate compaction. `build_session_context()` / `build_message()` / `compress_thread_history()` / `build_session_replay()` therefore take an optional `model_window` (tokens); `_resolve_caps(window)` re-derives every cap against a base scaled linearly to that window (`base = _CONTEXT_BUDGET_BASE × window / _REFERENCE_WINDOW_TOKENS`, `_REFERENCE_WINDOW_TOKENS`=1,000,000). This keeps each section's **share of the window invariant across models** — a section that is 20% of a 1M window stays 20% of a 200K window (i.e. one-fifth the chars). Results are `functools.lru_cache`d per distinct window; `_ResolvedCaps.max_context` is a computed property, and the module constant `_MAX_CONTEXT_CHARS` is *derived* from `_resolve_caps(_REFERENCE_WINDOW_TOKENS)` so the section-sum lives in one place.

- **Every char cap scales, not just the memory sections:** the memory caps (prefs/projects/history/semantic/episodic), lessons, skills, steering, compressed-history, the fallback history budget, AND the per-message cap (`caps.per_message`) all scale together. The per-message cap is additionally clamped to `min(caps.per_message, budget)` at its call site so one large recent message can never exceed the scaled history budget and drop *all* history. The episodic block injected in `build_message` (the only live episodic path — `build_session_context` passes no query, so its `episodic_cap` never fires) is bounded by `min(_EPISODIC_INJECT_CAP, caps.episodic)`. The dashboard's `build_session_replay` budget (`_REPLAY_BUDGET_CHARS`, injected *outside* the capped context) scales by the same factor.
- **Reference identity:** at the reference window the scale factor is exactly 1.0, so resolved caps are byte-for-byte the module constants — the caps are derived *from* those constants (single source of the fractions), not a re-listing.
- **Fail-safe fallbacks (`resolve_model_window(model)`):** `""`/`None`/`"auto"` and any id the registry does not confidently place at a smaller window resolve to `None` ⇒ the 1M reference. This is deliberate: the default deployment runs `provider=acp` + `model="auto"`, and the registry maps `"auto"`→200K even though ACP auto runs a 1M-window model — so ONLY an explicitly-selected smaller model scales the budget down; an unknown/auto window never silently shrinks the default deployment. An unlisted id that advertises `[1m]`/`-1m` is trusted as 1M (parity with `model_registry.window()`). **A context window is a property of the model, not the serving provider** — so `resolve_model_window` takes NO provider arg and always consults the window-bearing registry index (`model_registry.has_known_window(model)` / `_WINDOW_INDEX`). (Gating membership on the caller's provider was a bug: the `acp` index is empty, so it disabled scaling for the entire default deployment.)
- **Floor:** `_MIN_CONTEXT_BUDGET_BASE` (20% of base ≈ the 200K tier) clamps a pathologically small/misreported window so caps can't collapse to ~0. Known limitation: below 200K every window collapses to this same floored base (forward-compat only — the registry's smallest real window is 200K), and the **fixed preamble** (`_CRITICAL_RULES` + identity/workspace/date, ~3k chars) does NOT scale, so on a small window it consumes a larger *fixed* fraction than the linear model implies. Linear scaling is intentional per the design (window-share parity); a reserve-fixed-overhead curve is a possible future refinement.
- **Callers:** dashboard (`chat_runner`), Slack (`handler`), and subagents (`subagent`) all resolve the window from the live session client via `window_for_provider_client(client)` — which prefers the provider's public `context_window_tokens()` accessor (0 until a turn completes; at `is_new` it falls through) and otherwise derives from the resolved model id via `resolve_model_window`. Background/cron paths that don't resolve a model pass `None` (reference). See `context.py` `_resolve_caps` / `resolve_model_window` / `window_for_provider_client` and `model_registry.has_known_window()`.

### Session Resume (`resumed=True`)

When a session is restored via ACP `session/load`, `build_session_context()` and
`build_message()` accept `resumed=True`. This skips ONLY the `[THREAD CONVERSATION
HISTORY]` block — kiro-cli already has full native history. All other context blocks
are still injected:

| Block | Skip on resume? | Why |
|-------|-----------------|-----|
| `[THREAD CONVERSATION HISTORY]` | ✅ Skip | kiro-cli has full native history |
| Memory + skills + lessons | ❌ Keep | KiroCrew-specific, not in kiro-cli |
| `[Other chat tabs]` (cross-tab) | ❌ Keep | Reads OTHER sessions' JSONL |
| `[Recent Session Context]` (provenance) | ❌ Keep | Cross-thread entries |
| Agent system prompt | ❌ Keep | kiro-cli ACP doesn't load agent prompts |
| `_CRITICAL_RULES` | ❌ Keep | Diff rendering, OPTIONS buttons |
