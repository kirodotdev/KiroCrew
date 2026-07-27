# Memory, Skills & Hooks Modules

Last Updated: 2026-07-26 (foreign-agent import boundaries for memories/preferences, MCP servers, user-authored skills, and hooks. Prior — 2026-07-19 in-process embeddings: always-on with no disable path, non-blocking background model load, download robustness — daemon-thread HTTPS download, Ollama-blob salvage, retry ladder — and the `EmbeddingBackend` swap seam; skills lazy-load usage-ranked top-K + skill_search + SkillUsageLedger, /api/skills discovery_executor offload)

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

Structured memory system backed by SQLite + FAISS + in-process embeddings (vendored llama-cpp-python). Embeddings are ALWAYS-ON: `_coerce_embedding_provider` (config/loader.py) coerces EVERY `embedding_provider` value — including legacy `"ollama"` and `"none"` — to `"llama_cpp"`, so there is no config knob to disable them. While the model is still downloading or absent, memory degrades gracefully to keyword/FTS search and the lazy-rebind machinery in `vector_memory._try_embed` picks embeddings up when the model lands — no restart. Per-store overrides (`MemoryStoreConfig.embedding_provider`, enum `["", "llama_cpp"]`) can only inherit or restate the default — per-store disable is not supported.

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
- **Write**: text validation (10-2000 chars), **prompt-injection screening** (`_contains_injection`, same pattern set as the semantic-KV path), tag sanitization, importance clamping (0-1), FAISS dedup (cosine > 0.88). The dedup scan **skips tombstoned ("ghost") matches**: tombstone paths (merge, dashboard delete, cap eviction, stale retirement) set `is_deleted=1` but leave the vector in `_faiss_index`/`_faiss_id_map`, so a high-similarity hit may map to a deleted row. `_get_episodic()` filters `is_deleted=0` and returns `None` for those; the write loop `continue`s past a `None` match (mirroring `search_episodic`'s `if not mem or mem["is_deleted"]: continue`) instead of treating it as a conflict — otherwise a new memory matching a deleted one was silently rejected (data loss).
- **Injection screening (XPIA defense-in-depth, `696671aa`)**: episodic text is derived from conversation transcripts, so a poisoned turn could persist steering instructions that get re-injected into future contexts. `write_episodic()` now runs `_contains_injection()` (before the embed call) and, on match, drops the entry and emits an auditable `injection_blocked` event with `memory_type='episodic'`. The stored audit snippet is scrubbed with `redact_exfiltration_urls()` + `redact_credentials()` first, since `/api/memory/events` surfaces it verbatim on the dashboard. This mirrors the semantic-KV screen at `validate_semantic()`. **Residual (accepted risk)**: this is a best-effort regex screen — a determined owner can still steer their own long-term memory with phrasing that evades the patterns; long-term memory poisoning is an accepted residual. The screen raises the bar against accidental/opportunistic XPIA persistence, not against a motivated self-owner.
- **Search**: FAISS vector similarity with decay scoring: `cosine_sim × (0.7 + 0.3×importance) × exp(-0.03×days)`, then MMR diversity reranking (Jaccard-based, λ=0.6)
- **MMR reranking**: Maximal Marginal Relevance balances relevance with diversity. Greedy iterative selection penalizes candidates similar to already-selected results. Prevents redundant episodic fragments from consuming the context budget. Configurable via `mmr=False` parameter to disable.
- **Relevance threshold**: `cosine_sim ≥ 0.55` required for context injection (empirically determined from 100-query benchmark: 50 relevant + 50 irrelevant, F1=0.980). Results below threshold are filtered in `get_episodic_context()` only — `search_episodic()` returns all results for dashboard/API use. FTS5 keyword fallback is unaffected (no cosine scores).
- **Fallback**: keyword search (OR logic, LIKE on text + tags) when embeddings unavailable
- **Cap**: 10,000 active entries; lowest-importance oldest pruned when exceeded

Context injection: top-8 results in `[Episodic Memory]` block, capped at 3000 chars (`cap=3000`). Injected on the first message of new sessions via `build_message()` — not at plain session start, since `build_session_context` passes no query to `memory.get_context()`.

### In-Process Embedder (`embeddings.py`)

Embeddings run in-process via the vendored llama-cpp-python 0.3.34 runtime (`kiro_crew/_vendor/llama_cpp`) — no external server, no HTTP hop, no runtime pip install. (The Ollama-era remote-URL path — and with it `_validate_url`/`_resolve_blocked_addr` SSRF hardening from commit `76640a75` — was removed together with the network client: there is no embedding URL to validate anymore.)

- `LlamaCppEmbedder.embed(text)` / `embed_batch(texts)` → returns 1024-dim vectors or `None` on any failure (graceful degradation)
- **Non-blocking model load**: the GGUF load runs on a background daemon thread (`_kick_background_load()`, thread name `kc-embed-load`) — `embed()`/`embed_batch()` NEVER block on the load. When the model isn't in memory yet, the call kicks the background load and returns `None` immediately; memory degrades to keyword search until the load lands. The gateway/dashboard event loop is never stalled by embedding work. `wait_ready(timeout)` exists for sync contexts (tests, one-shot CLI flows) that legitimately want to block — never call it from an event-loop thread
- The underlying `Llama` object is NOT thread-safe — inference on a loaded model is serialized behind a lock (tens of ms per short text)
- `get_shared_embedder()` — process-wide singleton (~700MB RSS when loaded), shared by vector memory AND the knowledge library; `close()` unloads the model to free RSS
- Per-platform native libs live in `_vendor/llama_cpp_libs/{linux_x86_64,linux_aarch64,macos_arm64,macos_x86_64,win_amd64}`, selected at import time via `LLAMA_CPP_LIB_PATH` (upstream-supported override; an operator-set value wins, enabling e.g. a GPU build). Unsupported platforms and import failures degrade to keyword-only memory search. See `_vendor/README.md`
- Failed model loads (corrupt file, bad native libs) are retried only after a 300s cooldown so a broken state can't spawn a loader thread per embed call

**Embedding backend abstraction** (`EmbeddingBackend` ABC): the public swap seam for future runtimes (Ollama again, remote endpoints, ONNX) and user-defined models. Surface: `model_id`, `dim`, `is_ready()`, `embed()`, `embed_batch()`, `close()`. Consumers (vector memory, knowledge library) depend only on this interface; everything llama.cpp-specific lives in `LlamaCppEmbedder`. Swap flow: `register_embedding_backend(factory)` + `reset_shared_embedder()` replaces the singleton (pass `None` to restore the default). A backend with a different `model_id`/`dim` produces incomparable vectors — the knowledge library's `embed_signature` folds `model_id` in, so a swap automatically triggers the sig-gated knowledge re-embed; vector memory re-embeds via `migrate`.

**Sync embedding cache** (`make_sync_embed_fn()`, no args): The sync callable used by `vector_memory.py` wraps the shared embedder and caches results via `functools.lru_cache` keyed by `(input text, backend model_id)` — after a backend swap, the old model's cached vectors can never be served for the new model. Embeddings are deterministic (same text → same vector for a given model), so caching is safe. Bounded to 128 entries (~4 MB with Python boxed floats). Failures (None) are not cached — a still-downloading model is retried. Cache stats logged every 20 misses. Cache lives per `make_sync_embed_fn()` call — reset on gateway restart. Embedding through the cache never blocks on the model load (kicked in the background); callers get `None` until the model is resident.

### Model Download Manager (`embeddings.py`)

`ModelDownloadManager` (singleton via `model_download_manager()`) downloads the embedding GGUF in the BACKGROUND at gateway startup — boot is never blocked by the 610MB transfer:

**Download flow** (`ensure_model()` / `start_background_model_download()`):
- **Salvage fast-path** (`_salvage_legacy_ollama_blob`): before downloading, checks the legacy Ollama blob store (`~/.ollama/models/blobs/sha256-<digest>`, honoring `$OLLAMA_MODELS`) — Ollama stores layer blobs content-addressed and the Ollama-era GGUF is byte-identical, so migrating users skip the 610MB re-download entirely. The copy is sha256-verified like a real download; any failure falls through to the normal download
- Downloads `qwen3-embedding-0.6b-q8_0.gguf` (Q8_0 quantized, 610MB) over plain HTTPS from the public KiroCrew CDN — URL resolution order: `KIROCREW_EMBED_MODEL_URL` env var, then the `memory.embed_model_url` config knob, then the built-in `_DEFAULT_MODEL_URL` CDN constant. No git, no cloud SDK. Streaming sha256 is computed while downloading and byte-level progress (`bytes_downloaded`/`bytes_total`) is written to `status` every ~16MB for the dashboard's determinate progress bar
- sha256-verifies the file (`06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439` — the trust anchor for every source: a tampered CDN object or mirror can only fail verification); files under `_GGUF_MIN_BYTES` (1MB) are rejected as truncated
- Installs persistently to `~/.kirocrew/models/qwen3-embedding-0.6b.gguf` — atomic install: stages into a per-process unique file in the TARGET directory (same filesystem) then `os.replace`, so two concurrent processes (gateway + one-shot CLI) can never interleave writes into a shared staging file
- **Daemon-thread download** (`_run_download_on_daemon_thread`): the blocking HTTPS transfer runs on a daemon thread (deliberately NOT `run_in_executor` — executor threads are joined at interpreter exit), so Ctrl-C or a finished one-shot CLI is never pinned by an in-flight 610MB transfer
- **Retry ladder**: background startup task = up to 6 attempts with exponential backoff (60s base, 30min cap, may span hours); every gateway restart retries; dashboard Enable/Retry click = `DOWNLOAD_ATTEMPTS_INTERACTIVE` (3) attempts for fast feedback. `kirocrew run` (one-shot CLI) never kicks downloads — only the long-lived gateway does
- Escape hatch: `KIROCREW_SKIP_MODEL_DOWNLOAD=1` skips the download entirely (tests/CI must never trigger a 610MB download; tests additionally pin `OLLAMA_MODELS` to a tmp dir so the salvage path can't fire)
- Concurrent `ensure_model()` calls (startup task + dashboard Enable click) share one in-flight download
- `status` dict (`step`: `idle`/`downloading`/`verifying`/`waiting_retry`/`ready`/`failed`, plus `error` and `attempt`) is readable at any time by the dashboard status endpoint

**Dashboard Enable Flow** (non-blocking, retryable):
- `POST /api/memory/enable-embeddings` — never blocks on the download: if the model is absent it kicks (or adopts an already-in-flight) background download with `DOWNLOAD_ATTEMPTS_INTERACTIVE` (3) attempts and returns immediately (`{"ok": true, "status": "downloading"}`); the frontend polls `embedding-status` for progress. When the model is present it installs faiss-cpu if missing, wires the embed function, and persists config. The dashboard no longer surfaces a proactive "Start Embedding Engine" button (embeddings auto-start at boot) — this endpoint now backs only the error-state **Retry** affordance
- On failure: status resets to `idle` with error message, frontend shows error + Retry button
- Prevents concurrent setup attempts (409 if already in progress)
- `can_retry` flag in status response for frontend retry button
- `GET /api/memory/embedding-status` — `enabled` is always `true`; `provider` reports the legacy `"ollama"` token (the shipped frontend hard-checks `provider === "ollama"` — kept until the frontend companion change lands); `setup_step` maps the manager's steps to the legacy vocabulary the shipped polling loop terminates on (`ready`→`done`, `failed`→`error`, `downloading`/`verifying`/`waiting_retry`→`downloading`); the raw step and attempt are additionally exposed as `download_step` + `download_attempt` for newer frontends; `server_healthy` = model file present OR model loaded
- `POST /api/memory/disable-embeddings` — **gone**: embeddings are always-on. Kept as a graceful HTTP 410 stub (not a 404) because the shipped frontend still renders a Disable button; remove together with the frontend button

### Model Security & Policy

| Field | Value |
|-------|-------|
| Model | Qwen/Qwen3-Embedding-0.6B (Q8_0 GGUF) |
| License | Apache-2.0 (on approved list for self-approval) |
| Source | public KiroCrew CDN (`_DEFAULT_MODEL_URL`; sha256-pinned; `KIROCREW_EMBED_MODEL_URL` / `memory.embed_model_url` for mirrors) |
| Runtime | Vendored llama-cpp-python 0.3.34 (MIT license, `kiro_crew/_vendor/`) |
| Data flow | Text → in-process function call → float vectors (no data leaves machine) |
| Policy | Self-approvable under a public dataset / ML model policy |

Conditions met for self-approval:
1. Local use only — model runs locally, no 3P API calls
2. Apache-2.0 license — on approved list
3. Outputs are float vectors — no excluded categories (health, financial, biometric, PII)
4. Not recreating training data — generating embeddings, not content
5. Model weights sourced from the sha256-pinned KiroCrew release bucket (integrity-verified download at runtime)

### Why llama.cpp (not TEI)

TEI (Text Embeddings Inference) uses the candle Rust framework with a Metal backend that has an [unmerged memory bug](https://github.com/huggingface/candle/pull/3197) causing unbounded GPU buffer allocation on macOS. The process consumes 4+ GB RAM and never becomes healthy. This affects ALL models on TEI/Metal, not just Qwen3. llama.cpp works correctly on all supported platforms (macOS Metal, Linux CPU) — KiroCrew vendors it directly via llama-cpp-python, which also removes the external Ollama server the previous design depended on.

### Lessons in Vector Memory

When vector memory is active, lessons are stored as semantic entries:
- Key: `lesson.<md5_of_rule>` (dedup via hash)
- Value: `"rule text"` or `"rule text — NOT: negative text"`
- Confidence: 1.0 for `user_explicit`, 0.9 for `migration`
- Methods: `write_lesson()`, `get_lessons()`, `delete_lesson()`, `get_lessons_context()`
- Context: injected as `[Learned corrections]` block, separate from `[Semantic Memory]`
- Allowlist: `lesson.*` prefix in `_BUILTIN_PREFIXES`

Model: `Qwen/Qwen3-Embedding-0.6B` Q8_0 GGUF (610MB). Apache-2.0 licensed. Served in-process via the vendored llama-cpp-python runtime on all supported platforms.

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
| GET | `/api/memory/embedding-status` | Embedding health + download progress. `enabled` always true; `setup_step` in legacy vocabulary (done/error/idle/downloading); raw `download_step` (idle/downloading/verifying/waiting_retry/ready/failed) + `download_attempt` + `bytes_downloaded`/`bytes_total` |
| POST | `/api/memory/enable-embeddings` | Non-blocking: kicks/adopts the background model download and returns `{"ok": true, "status": "downloading"}` when the model is absent; wires embeddings + updates config when present |
| POST | `/api/memory/disable-embeddings` | HTTP 410 stub — embeddings are always-on; kept only until the frontend removes its Disable button |
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
- **Embedding during migration**: when the model file is present, the caller sets `store.embed_fn` before calling migration. Each episodic entry is embedded in-process and stored with its FAISS vector, enabling vector search immediately after migration.
- Idempotent: re-running skips existing semantic entries (conflict resolution), episodic dedup via FAISS when available

**Automatic migration (boot-time, `GatewayOrchestrator._auto_migrate_memory`)**: migration is fully automatic — there is **no dashboard "Migrate" button**. Right after `_start_embeddings()`, the gateway schedules a fire-and-forget background task (retained in `_background_tasks`, cancelled on shutdown) that runs two idempotent phases, all blocking work offloaded to the maintenance executor so boot is never blocked:
1. **Migrate** (gated on `memory.migrated == False`): detects legacy content via the shared `memory.legacy_memory_present()` helper (also used by `/api/memory/stats`), runs `migrate_from_markdown()`, then flips `memory.migrated=True` for **everyone** — fresh installs with zero legacy entries included, so all users land in vector-only mode. Syncs the live `consolidator._migrated`, and **acknowledges** with a `migration` audit event (`memory_events`, visible in the dashboard Audit tab, `source="auto"`, counts in `new_value`) plus a `logger.info` line. On error: logs and leaves `migrated=False` so the next boot retries.
2. **Re-embed sweep** (gated on model readiness, independent of the migrated flag): once the model file is present (awaits the background download task if still in flight — safe, we are our own task), `VectorMemory.backfill_missing_embeddings()` embeds any episodic rows written with a NULL vector (migrated before the model landed) and rebuilds the FAISS index. Self-healing across boots and across a download that failed then later succeeded.

The backend `POST /api/memory/migrate` endpoint and the `kirocrew memory migrate` CLI remain as a manual escape hatch, but the dashboard no longer calls them.

### Cross-Platform

macOS (Apple Silicon and Intel), Linux (x86_64, arm64/Graviton), and Windows supported. All paths use `pathlib.Path`. GGUF model downloaded over sha256-pinned HTTPS from the KiroCrew CDN. No runtime install step — native llama.cpp libraries are vendored per platform in `_vendor/llama_cpp_libs/` and selected via `LLAMA_CPP_LIB_PATH` (the old Docker fallback is gone).

| Platform | Vendored libs | GPU | Notes |
|----------|--------------|-----|-------|
| macOS (Apple Silicon) | `macos_arm64/` | Metal (shader embedded in dylib) | Fastest |
| macOS Intel (x86_64) | `macos_x86_64/` | CPU (Metal OFF) | Built from the pinned 0.3.34 sdist for the universal desktop app's x64 slice |
| Linux x86_64 | `linux_x86_64/` | CPU | manylinux2014 (glibc ≥ 2.17) — AL2 and AL2023 both work |
| Linux aarch64/Graviton | `linux_aarch64/` | CPU | manylinux2014 (glibc ≥ 2.17) — AL2 and AL2023 both work |
| Windows x86_64 | `win_amd64/` | CPU | DLLs found via `os.add_dll_directory` |

The model download requires only outbound HTTPS (no git/git-lfs) on all platforms.

### Foreign-agent memory import

The selectable `memories` category covers durable memories and preferences from
supported foreign agents. It is not a raw file-copy path. Imported values pass
through the same KiroCrew memory writers, key allowlists, per-entry size/count
limits, injection screening, conflict resolution, deduplication, audit events,
and active-entry caps described above. Existing KiroCrew memories/preferences
win on conflict; re-applying the same foreign item is idempotent through the
shared import provenance ledger.

Episodic imports use the native writer's preservation mode. A similarity match
or a full active-entry store rejects the foreign item without tombstoning,
merging into, or evicting an existing entry. Import therefore cannot delete or
replace native episodic memory even when a foreign entry is longer, newer, or
more important. The preservation-mode capacity check and insert run in one
SQLite immediate transaction, so separate store instances cannot both claim the
last slot. Exact-text classification goes through the store's lock-safe lookup
instead of reading its shared connection from the importer.

The importer cannot turn a foreign system prompt, raw instruction, persona,
tool transcript, credential, or runtime record into memory. Items that cannot
be represented within the destination writers and limits are reported as
unsupported or skipped rather than copied around those writers.

Markdown and supported database memory values are injection-screened before they
become selectable, then screened again by the destination writer. When an
import operation needs to create its own `VectorMemoryStore`, it wires
`make_sync_embed_fn()` and its lazy factory exactly as the destination runtime
does. The callable remains non-blocking: until the embedding model is ready,
episodic writes persist normally without vectors and continue to use keyword
retrieval.

Hermes Markdown import is limited to exact `memories/MEMORY.md` and
`memories/USER.md` files under the main home and each profile; arbitrary memory
Markdown is not scanned. A present Hermes `memory_store.db` is diagnosed as an
unsupported store. An unreadable Hermes `profiles` directory is skipped with a
`profiles/read_failed` diagnostic instead of aborting the source scan. Profile
discovery consumes at most 51 directory entries, scans at most 50, and emits
`profiles/profile_count_limit` when overflow is observed instead of materializing
an unbounded directory. Before any supported foreign SQLite database is opened,
the main file and present `-wal`/`-shm` sidecars must all be regular non-symlink
files, must not have multiple hard links, and their aggregate size must not
exceed 64 MiB. The importer reads a descriptor-pinned private snapshot of the
database and sidecars, so a source-file replacement after validation cannot
change the inode being queried. MeshClaw's 10,000-row scan limit applies to the
aggregate active rows across its supported semantic and episodic tables and is
checked before either table contributes an item. Episodic text deduplication is
rechecked under the native store write lock before insertion, preventing a
concurrent native write from being duplicated.

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

**Foreign-agent import:** only user-authored skills are eligible. Imported
skills are isolated under the `imported/<source>/...` namespace so they cannot
replace built-in, project, existing user, or auto-generated skills. Discovery
and copy are symlink-safe: symlinked skill roots/files, path traversal, and any
resolved path outside the declared source skill root are rejected and reported.
On Windows, reparse points (including directory junctions) are link-like for
both source traversal and destination ancestry checks and are rejected by the
same boundary.

Claude includes global skills and `<workspace>/.claude/skills`; MeshClaw uses
workspaces resolved from both `workspace_dir` and `project_dir` pointer files
and scans `<workspace>/skills`, while `~/.meshclaw/skills` remains excluded
because its user-authored provenance is not reliable. Re-import deduplicates
through provenance instead of overwriting the destination. A package with
`always: true` or `triggers` frontmatter is rejected so imported content cannot
gain automatic prompt activation.

OpenClaw scans only documented workspace provenance: explicit
`OPENCLAW_WORKSPACE_DIR`, `agents.entries.<agentId>.workspace`,
`agents.defaults.workspace/<agentId>`, the profile workspace under
`~/.openclaw/workspace-<profile>`, and documented state/agent defaults. From
those roots only `MEMORY.md`, `memory/*.md`, and `skills` are eligible;
instruction, identity, and persona files remain excluded. Hermes subtracts
bundled names from `.bundled_manifest` and hub-installed names/install paths
from `.hub/lock.json`; `.archive`, `.hub`, dependency, and cache trees are
pruned before the file budget, leaving only active local packages selectable.
Accepted packages retain their ordinary assets. Every regular UTF-8 text asset
in a complete, package-bounded traversal is screened in full for credentials
and exfiltration URLs; clean assets are copied byte-for-byte, including leading
and trailing whitespace. No per-asset preview truncation is used for either the
security decision or the copied content.

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

### Foreign-agent MCP import

Only definitions with exactly one supported transport are selectable: stdio
`command` with an optional string-list `args`, or a remote HTTP(S) `url` with no
arguments. Mixed transports, remote arguments, unknown keys, working-directory,
tool/filter, agent/scope, environment, header, credential, token, and cookie
fields reject the whole server rather than producing a narrowed definition.
Remote URLs with any query or fragment are rejected, even when the parameter
name is not credential-like. Secret values themselves are never returned in
scan/apply output or written to KiroCrew config. If the destination
`mcpServers` value already exists but is malformed, import reports a conflict
and preserves it byte-for-byte. The MCP phase runs outside the dashboard config
lock because MCP handlers take the MCP file lock before the config lock; this
keeps concurrent import and enable/disable operations in one lock order.

Source `enabled` and `disabled` fields are runtime state, not portable
structure. They are ignored without invalidating an otherwise exact safe
definition, and every accepted destination definition is forced to
`disabled: true` for explicit review.

The same constraint gate applies to Hermes: its current enabled/disabled state
may be ignored, but nested `tools.include` or `tools.exclude` is tool scoping and
rejects the entire server.

MCP import is merge-only. Before writing, collision detection canonicalizes
server aliases and reserves names from every effective source: the KiroCrew
data-home file, Kiro global settings, bundled/project/installed agent config,
managed servers, and edition-contributed server/scope files. An exact or
alias-equivalent foreign name is rejected, so a disabled import cannot shadow
an enabled global or installed server. Existing server definitions win on
collision, and KiroCrew-managed servers (including `kirocrew-core` and
`kirocrew-cron`) are protected from replacement, deletion, or shadowing by an
imported definition. Malformed effective-source JSON or non-object
`mcpServers` values contribute no names and cannot abort an import. Repeated
imports deduplicate through the provenance ledger.

## Auto Skill Creation (`skills.py` + `history.py`)

Hermes-style autonomous skill creation from completed sessions. Disabled by default; opt-in per user via `skills.auto_create_from_sessions`.

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

Foreign-agent hooks are never imported. Hook scripts, hook commands, matchers,
and hook runtime state are unsupported items: scan/apply may report their
presence, but must not copy or register them.

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
- **Fail-safe fallbacks (`resolve_model_window(model)`):** delegates to the central `model_registry.model_window(model)` authority (kiro-list cache > registry > supplementary id map > `[1m]` heuristic > `None`). `""`/`None`/`"auto"` and any genuinely-unknown id resolve to `None` ⇒ the 1M reference — so ONLY a model with a confidently-known smaller window scales the budget down; an unknown/auto window never silently shrinks the default deployment (`provider=acp` + `model="auto"` runs a 1M model). The central authority returns `None` (not a silent 200K) for unknown ids, so this fail-safe is now the authority's own contract rather than a special case here. **A context window is a property of the model, not the serving provider** — so `resolve_model_window` takes NO provider arg and `model_window` is provider-independent.
- **Floor:** `_MIN_CONTEXT_BUDGET_BASE` (20% of base ≈ the 200K tier) clamps a pathologically small/misreported window so caps can't collapse to ~0. Known limitation: below 200K every window collapses to this same floored base (forward-compat only — the registry's smallest real window is 200K), and the **fixed preamble** (`_CRITICAL_RULES` + identity/workspace/date, ~3k chars) does NOT scale, so on a small window it consumes a larger *fixed* fraction than the linear model implies. Linear scaling is intentional per the design (window-share parity); a reserve-fixed-overhead curve is a possible future refinement.
- **Callers:** dashboard (`chat_runner`), Slack (`handler`), and subagents (`subagent`) all resolve the window from the live session client via `window_for_provider_client(client)` — which prefers the provider's public `context_window_tokens()` accessor (0 until a turn completes; at `is_new` it falls through) and otherwise derives from the resolved model id via `resolve_model_window`. Background/cron paths that don't resolve a model pass `None` (reference). See `context.py` `_resolve_caps` / `resolve_model_window` / `window_for_provider_client` and the central `model_registry.model_window()` / `has_known_window()`.

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
