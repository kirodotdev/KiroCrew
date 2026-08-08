# Memory, Skills & Hooks Modules

## Overview

Persistent memory, skill system, and config-driven hooks. Assembled by
`ContextBuilder` and injected into ACP prompts.

### The six memory layers

Six distinct layers, each with its own store, write path, and context cap. The
nesting below is source-of-truth ordering (a later layer can override an earlier
one), not a storage hierarchy:

```
Context window (reference budget 165,000 chars, ~55k tokens)

  Preferences            Projects            Recent history
  (preferences.md)       (projects.md)       (history/{date}.md)
  consolidator-replaced  consolidator-       multi-tier decay
                         replaced
        |                     |                     |
        +---------------------+---------------------+
                              |
    Semantic memory (SQLite key-value)
    pref.* / project.* / user.* keys, confidence-gated writes
                              |
    Episodic memory (past conversation fragments)
    FAISS (or stdlib) vector search + time decay + MMR reranking
                              |
    Lessons (learned corrections)
    lesson.* keys at confidence 1.0, user-explicit always wins
```

Layers 1 to 3 are Markdown files under the workspace memory dir; layers 4 to 6
are rows in `memory.db` behind one shared `VectorMemoryStore` (lessons fall back
to `lessons.jsonl` only when that store is not initialized). Each layer is
detailed in its own section below, with a single conflict ladder in "Conflict
resolution: which layer wins".

## Memory (`memory.py`)

Structured files under `~/.kiro/crew/workspace/memory/`:
- `preferences.md` — learned user preferences (replaced wholesale by consolidator)
- `projects.md` — active project context (replaced wholesale by consolidator)
- `history/{date}.md` — daily conversation summaries (append-only, pruned by heartbeat)

FTS5 search via `~/.kiro/crew/memory_index.db` (SQLite via `pysqlite3-binary` on Linux for FTS5/UPSERT compat, stdlib `sqlite3` on macOS). The virtual table is created with `tokenize='porter unicode61'`, so keyword matching is porter-stemmed inside SQLite. (This is a different stemmer from the `snowballstemmer` pass used by the vector store's keyword-fallback *scoring* in `vector_memory.py`; two independent code paths, do not conflate them.) Self-healing: corrupted DB auto-rebuilt. Incremental updates on writes, full rebuild on gateway startup and every `_FTS_REBUILD_TICKS = 15` heartbeat ticks (~15 min at the 60s default interval). Connection leak prevention: all FTS methods use try/finally.

Context injection includes source citations per section. Agent can update memory files via kiro-cli's file tools.

### Decaying Memory (`read_recent_history`)

History context uses natural decay: recent days in full detail, older days
progressively compressed. `_read_recent_history_uncached` walks a fixed 181-day
window (`range(181)`) and picks a rendering per day by age.

| Age | What is kept | Why |
|-----|--------------|-----|
| 0–13 days (`i < days`, `days=14`) | Full entries with timestamps | Recent work needs full context |
| 14–60 days (`i < 61`) | Day header + first entry + `…N more entries` | Enough to jog memory at a fraction of the chars |
| 61–180 days | Date + `#### ` count only | Existence marker: "something happened then" |
| 181–364 days | Not read into context | Still on disk as a backup |
| 365+ days | Deleted from disk by heartbeat prune | Too old to be worth the scan |

The assembled string is capped when injected. `MemoryStore.get_context()`
declares `history_cap=25_000` as its own signature default, but the live caller
is `ContextBuilder.build_session_context()`, which passes
`caps.memory_history` (26,400 chars at the reference window, scaled per model
window, see Context Builder below) whenever the `memory` group is in scope, so
25,000 only applies to a direct programmatic call. Timestamps use local timezone.

`read_recent_history` runs on every message turn (context build) and otherwise
stats + reads up to 181 daily files synchronously. The assembled string is
TTL-cached (`_HISTORY_CACHE_TTL_SECS = 5.0`) on the `MemoryStore` instance,
keyed on `(days, today)` so the decay window shifting at midnight invalidates
naturally; `append_history` and `prune_history` call `_invalidate_history_cache()`
so a new or pruned entry is visible immediately.

### History Pruning

`prune_history(keep_days)` deletes daily files older than `keep_days` (default 365). Runs once per day via heartbeat (`_PRUNE_TICKS = 1440`). Parses `YYYY-MM-DD.md` filenames, skips non-date files.

### Consolidation (`history.py` `HistoryConsolidator`)

How a user message becomes durable memory:

```
user message
    |
    +-- learn_add MCP tool -----> write_lesson()  (immediate; user said
    |                                              "remember X", or corrected
    |                                              the agent)
    |
    +-- 30 messages ------------> consolidation, prefs path
    |                             (_CONSOLIDATION_THRESHOLD = 30)
    |                             - preferences.md  (wholesale replace)
    |                             - projects.md     (wholesale replace)
    |                             - semantic entries (max 20)
    |
    +-- 3h idle ----------------> consolidation, history path
                                  - append history/{date}.md
                                  - episodic entries (max 10)
                                  - implicit lessons  (max 10)
```

Two separate consolidation paths with independent triggers:

| Path | Trigger | What it updates | Offset tracking |
|------|---------|-----------------|-----------------|
| Preferences/projects | 30 messages (per session, `_CONSOLIDATION_THRESHOLD`) | `preferences.md`, `projects.md`, semantic entries | In-memory `_prefs_offset` dict |
| Daily history + lessons | 3h idle (per session, `history_idle_hours` = 3.0) | `history/{date}.md`, episodic entries, `lessons.jsonl` (or `lesson.*` in vector store) | Persisted `last_consolidated` in JSONL metadata |

Per-consolidation extraction caps (`vector_memory_constants.py`, also
interpolated into the LLM prompt so the model is told the same numbers):
`_MAX_SEMANTIC_PER_CONSOLIDATION = 20`, `_MAX_EPISODIC_PER_CONSOLIDATION = 10`,
`_MAX_LESSONS_PER_CONSOLIDATION = 10`. The lessons cap exists because each
`write_lesson()` can perform up to 6 blocking embeds (1 rule plus
`_MAX_BACKFILLS_PER_CALL = 5` lazy backfills), so an uncapped LLM array could
occupy a worker thread for minutes.

The `preferences_update` / `projects_update` prompt keys are added ONLY when
`memory.migrated` is false, so a migrated install writes structured memory and
leaves the Markdown files alone.

The prefs path does NOT advance the persisted `last_consolidated` marker — only the history path does. This ensures history consolidation always covers all messages, even if prefs consolidation fired earlier.

Idle detection: `_last_activity[key]` updated on every `maybe_consolidate()` call. `check_idle_sessions()` called every heartbeat tick (60s), fires history consolidation when `now - last_activity > history_idle_secs` and there are unconsolidated messages.

Neither path owns a timer. The prefs path is checked inline on every
`maybe_consolidate()`; the history path is driven entirely by the heartbeat
calling `check_idle_sessions()`. Every embed-bearing step
(`_write_structured_memory`, `_save_lessons`, `append_history`) is dispatched
through `run_in_embed_pool` (the bounded `mc-embed` bulkhead) because
`_consolidate` runs on the gateway event loop, and a slow or hung embed inline
would stall heartbeats, Slack, and the dashboard.

### Lesson Extraction from Chat

The history consolidation prompt includes a `"lessons"` key that extracts only implicit correction patterns — corrections the user made without explicitly saying "remember" (those are already saved immediately via `learn_add`). All lesson writes go through `write_lesson()` which provides substring dedup and topic-overlap dedup (>50% keyword overlap → newer replaces older). When vector memory is not active, falls back to `lessons.jsonl` via `LessonStore.save()`.

### Configuration

`~/.kiro/crew/config.json` → `"memory"` section:
```json
{"history_idle_hours": 3.0, "history_max_days": 365}
```

Exposed on dashboard: Overview → Memory tab → Memory Settings card. Changes apply immediately to running consolidator via `PUT /api/memory/settings`.

## Vector Memory (`vector_memory.py`)

Structured memory system backed by SQLite + FAISS + in-process embeddings (vendored llama-cpp-python). Embeddings are ALWAYS-ON: `_coerce_embedding_provider` (config/loader.py) coerces EVERY `embedding_provider` value — including legacy `"ollama"` and `"none"` — to `"llama_cpp"`, so there is no config knob to disable them. While the model is still downloading or absent, memory degrades gracefully to keyword/FTS search and the lazy-rebind machinery in `vector_memory._try_embed` picks embeddings up when the model lands — no restart. Per-store overrides (`MemoryStoreConfig.embedding_provider`, enum `["", "llama_cpp"]`) can only inherit or restate the default — per-store disable is not supported.

### Thread safety (`_db_lock`, `threading.RLock`)

One `VectorMemoryStore` instance is shared by the gateway event loop (readers)
and several worker threads (writers: consolidation via `run_in_embed_pool`, the
dashboard memory handlers via `asyncio.to_thread`). It holds ONE `sqlite3`
connection and ONE FAISS index, and neither is thread-safe: `sqlite3` caches
prepared statements per connection, so two threads stepping a statement at the
same time corrupt each other's row iteration (observed as
`DatabaseError("another row available")`, and on Windows CI as a `None` value for
a column the `WHERE` clause excluded), while a concurrent FAISS `add` during a
`search` can corrupt the C++ index outright. `self._db_lock` (a reentrant
`threading.RLock`, so a locked method may call another locked method)
serializes every statement on that connection. The critical sections that
matter most:

- **Semantic write** (`_write_semantic`): the whole `SELECT` →
  conflict-resolve → `UPSERT` sequence. Unlocked, a read-modify-write can
  interleave with a concurrent writer and lose an update.
- **Episodic write** (`write_episodic`): the under-lock dedup re-check, the
  `INSERT`, and the FAISS `add` + `_faiss_id_map.append`. The index and the id
  map MUST commit together: a reader that sees `index.ntotal == N+1` while
  `len(id_map) == N` raises `IndexError`. The id is appended first and popped
  back on a failing `add`, so the two structures stay in sync.
- **Episodic search** (`search_episodic`, FAISS path): the FAISS `search`, the
  id-map lookups, and the batched row resolve, so a mid-flight `add` cannot
  desync the lookup. The MMR rerank and `_touch_last_accessed` run after the
  block (the latter re-acquires the lock itself, which is why reentrancy is
  required).
- **Episodic search** (`_sqlite_vector_search`, the no-FAISS fallback): only the
  row fetch is locked; the cosine/decay scoring loop then works on materialized
  rows outside the lock.

**The lock is never held across an embedding call.** An embed on a loaded model
is serialized behind the embedder's own lock and costs tens of ms per short
text; holding a process-wide store lock across that would serialize every reader
behind it and defeat the point of offloading the write to a worker thread in the
first place. So each write embeds FIRST, then takes the lock for local work
only. Two consequences the code handles explicitly: `_write_semantic` calls
`_retire_stale_episodic` AFTER releasing the lock (that helper embeds, then
re-takes the lock itself), and `write_episodic` samples `_space_generation`
before the embed, carries it into the locked region, and re-checks it there,
because an embedding-model swap can land in the gap and a vector from the
previous space must be persisted as NULL rather than committed (the post-swap
backfill re-embeds the row).

This serialization is **per-process only**. It adds no conflict detection or
notification, and it does not coordinate across separate Kiro Crew processes
(gateway plus a one-shot CLI), so two processes writing the same key remain
last-write-wins.

### Semantic Memory

SQLite table `semantic_memory` — structured key-value store with:
- **Allowed keys**: `_BUILTIN_PREFIXES` is `pref.*`, `project.*`, `user.*`, `lesson.*` (+ user-configurable `extra_prefixes`). The first three are the fact prefixes the consolidation prompt offers the LLM; `lesson.*` is the lessons tier writing into the same table.
- **Key format**: `^[a-z][a-z0-9_.]*[a-z0-9]$`, max 100 chars; value JSON max 4,096 bytes
- **Confidence gating**: writes whose source is not `user_explicit` require confidence ≥ `_DEFAULT_CONFIDENCE_THRESHOLD` (0.8); `user_explicit` bypasses the threshold
- **Conflict resolution**: `user_explicit` always wins, and only another `user_explicit` may overwrite an existing `user_explicit` row; otherwise higher confidence wins, and confidences within 0.1 of each other count as equal so the newer write wins. A rejected write logs a `conflict_skip` event.
- **Injection detection**: the `_INJECTION_PATTERNS` regex set (14 patterns, `vector_memory_constants.py`) is scanned on every value write
- **Audit trail**: `memory_events` table logs every create/update/delete with old+new values, bounded at `_MAX_EVENTS = 10_000`

Context injection: formatted as `key: value` pairs in `[Semantic Memory]` block. The cap is passed in by the caller: `build_session_context()` supplies `caps.semantic`, which is `_SEMANTIC_MEMORY_CAP` (7.7% of the base = 12,705 chars) at the reference window and scales down with the model window. Excludes `lesson.*` keys (they have their own `[Learned corrections]` block). Uses hybrid retrieval when embeddings are available: `_SEMANTIC_VECTOR_WEIGHT` 0.6 × vector_score + `_SEMANTIC_KEYWORD_WEIGHT` 0.4 × keyword_score. Falls back to keyword-only scoring (word overlap on keys and values, key matches weighted 3×, with `snowballstemmer` expansion) without embeddings.

### Episodic Memory

SQLite table `episodic_memories` — conversation fragments with optional embeddings:
- **Write**: text validation (10-2000 chars), **prompt-injection screening** (`_contains_injection`, same pattern set as the semantic-KV path), tag sanitization, importance clamping (0-1), FAISS dedup (cosine > 0.88). The dedup scan **skips tombstoned ("ghost") matches**: tombstone paths (merge, dashboard delete, cap eviction, stale retirement) set `is_deleted=1` but leave the vector in `_faiss_index`/`_faiss_id_map`, so a high-similarity hit may map to a deleted row. `_get_episodic()` filters `is_deleted=0` and returns `None` for those; the write loop `continue`s past a `None` match (mirroring `search_episodic`'s `if not mem or mem["is_deleted"]: continue`) instead of treating it as a conflict — otherwise a new memory matching a deleted one was silently rejected (data loss).
- **Injection screening (XPIA defense-in-depth)**: episodic text is derived from conversation transcripts, so a poisoned turn could persist steering instructions that get re-injected into future contexts. `write_episodic()` runs `_contains_injection()` (before the embed call) and, on match, drops the entry and emits an auditable `injection_blocked` event with `memory_type='episodic'`. The stored audit snippet is scrubbed with `redact_exfiltration_urls()` + `redact_credentials()` first, since `/api/memory/events` surfaces it verbatim on the dashboard. This mirrors the semantic-KV screen at `validate_semantic()`. **Residual (accepted risk)**: this is a best-effort regex screen: a determined owner can still steer their own long-term memory with phrasing that evades the patterns; long-term memory poisoning is an accepted residual. The screen raises the bar against accidental/opportunistic XPIA persistence, not against a motivated self-owner.
- **Search**: FAISS vector similarity with decay scoring: `cosine_sim × (0.7 + 0.3×importance) × exp(-0.03×days_old)`, then MMR diversity reranking (Jaccard-based, `_MMR_LAMBDA` = 0.6)
- **MMR reranking**: Maximal Marginal Relevance balances relevance with diversity. Greedy iterative selection penalizes candidates similar to already-selected results. Prevents redundant episodic fragments from consuming the context budget. Configurable via `mmr=False` parameter to disable. The candidate pool is deliberately NOT truncated toward `limit` (that tail pick is the point of MMR); the only bound is the recall-safe `_MMR_MAX_POOL` = 1000 ceiling for pathological inputs.
- **Relevance threshold**: `_EPISODIC_RELEVANCE_THRESHOLD` = 0.55 cosine required for context injection (empirically determined from a 100-query benchmark: 50 relevant + 50 irrelevant, F1=0.980), relaxed to `_EPISODIC_LONG_TEXT_THRESHOLD` = 0.42 for entries longer than `_EPISODIC_LONG_TEXT_CHARS` = 300 chars, because long texts dilute cosine scores. The threshold reads the RAW `cosine_sim`, not the decay-adjusted score, so age and importance affect ordering but never admission. Admission runs BEFORE the decay ranking, MMR, and the `limit` cut: `get_episodic_context()` calls `search_episodic(relevance_filter=True)`, which drops sub-threshold candidates first, so a highly relevant but old memory cannot be ordered past `limit` by a cluster of recent-but-irrelevant rows that the gate would then remove — a case that otherwise returned empty context while an exact match sat in the store. `search_episodic()` defaults to `relevance_filter=False` and returns the full ranked set for dashboard/API/CLI use. The keyword fallback is unaffected because those rows carry no `cosine_sim` key at all.
- **Fallback ladder**: FAISS (needs faiss + numpy) → `_sqlite_vector_search`, stdlib cosine over the stored blobs → FTS5/LIKE keyword search (OR logic on text + tags) when there is no query embedding at all. The middle rung matters: faiss is an optional accelerator, not a declared dependency, so a stock install still gets vector recall from the stored vectors.
- **Cap**: `_DEFAULT_EPISODIC_MAX` = 10,000 active entries. `_enforce_episodic_cap()` tombstones `ORDER BY importance ASC, created_at ASC` (lowest-importance oldest first) on write once the count reaches the cap.

Context injection: `_DEFAULT_EPISODIC_LIMIT` = 8 results in an `[Episodic Memory]` block, each fragment sliced to 1,500 chars, total bounded by `min(_EPISODIC_INJECT_CAP, caps.episodic)` where `_EPISODIC_INJECT_CAP` = 3,000. Injected on the first message of new sessions via `build_message()`, not at plain session start, since `build_session_context` passes no query to `memory.get_context()`, so that call's `episodic_cap` argument never fires.

### Fading: three independent decay mechanisms

Three unrelated mechanisms keep stale memory out of the context budget. They do
not coordinate, so reason about them separately:

1. **History decay (time tiers)**: `memory.py` `read_recent_history()`, table
   above. Cheap, deterministic, no scoring.
2. **Episodic decay (exponential, at query time)**: the score formula above.
   `exp(-0.03 × days_old)` halves at ~23 days and reaches ~10% at ~77 days;
   `(0.7 + 0.3 × importance)` scales the whole score by importance, so a
   high-importance entry decays from a higher starting point rather than more
   slowly. Ranking and filtering are two separate stages in two separate
   functions, in that order: `search_episodic()` ranks by decay-adjusted score
   and returns everything (the dashboard and API want unfiltered results), then
   `get_episodic_context()` drops anything whose RAW `cosine_sim` is below the
   relevance threshold. A 30-day-old entry with importance 0.8 and cosine 0.9
   scores `0.9 × 0.94 × 0.407 ≈ 0.34`, so it likely loses its top-8 slot to
   newer matches; an entry at cosine 0.4 can hold a slot on score yet still be
   dropped at injection time by the threshold.
3. **Cap eviction**: `_enforce_episodic_cap()`, above. Independent of age
   except as a tiebreak.

### In-Process Embedder (`embeddings.py`)

Embeddings run in-process via the vendored llama-cpp-python 0.3.34 runtime (`kiro_crew/_vendor/llama_cpp`) — no external server, no HTTP hop, no runtime pip install. (The Ollama-era remote-URL path — and with it `_validate_url`/`_resolve_blocked_addr` SSRF hardening from commit `76640a75` — was removed together with the network client: there is no embedding URL to validate anymore.)

- `LlamaCppEmbedder.embed(text)` / `embed_batch(texts)` → returns 1024-dim vectors or `None` on any failure (graceful degradation)
- **Non-blocking model load**: the GGUF load runs on a background daemon thread (`_kick_background_load()`, thread name `kc-embed-load`) — `embed()`/`embed_batch()` NEVER block on the load. When the model isn't in memory yet, the call kicks the background load and returns `None` immediately; memory degrades to keyword search until the load lands. The gateway/dashboard event loop is never stalled by embedding work. `wait_ready(timeout)` exists for sync contexts (tests, one-shot CLI flows) that legitimately want to block — never call it from an event-loop thread
- The underlying `Llama` object is NOT thread-safe — inference on a loaded model is serialized behind a lock (tens of ms per short text)
- `get_shared_embedder()` — process-wide singleton (~700MB RSS when loaded), shared by vector memory AND the knowledge library; `close()` unloads the model to free RSS
- Per-platform native libs live in `_vendor/llama_cpp_libs/{linux_x86_64,linux_aarch64,macos_arm64,macos_x86_64,win_amd64}`, selected at import time via `LLAMA_CPP_LIB_PATH` (upstream-supported override; an operator-set value wins, enabling e.g. a GPU build). Unsupported platforms and import failures degrade to keyword-only memory search. See `_vendor/README.md`
- **The shipped closure is declared, not inferred.** `_REQUIRED_VENDORED_LIBS` names the exact files each platform must carry, and `verify_vendored_libs(root=None)` returns `{platform: [missing…]}` (empty when complete) against a source tree, an unpacked sdist, or an installed wheel. `_load_llama_class()` consults it before importing, so an incomplete install is reported as a **packaging defect naming the absent files** rather than surfacing as ctypes' `Shared library with base name 'llama' not found` — which reads as an unsupported architecture and misdirected the real-world diagnosis of this bug. `kirocrew doctor` prints the same detail. The check is **skipped when `LLAMA_CPP_LIB_PATH` is set**: the libs then load from the operator's directory, so the bundled tree's contents no longer determine whether the runtime works, and refusing on them would disable the documented override for exactly the users an incomplete wheel stranded (the warning names the env var as a remedy for that reason). Each packaging lane selects these files by a different mechanism (MANIFEST.in for the sdist, `package_data` for the wheel, the PyInstaller spec for the desktop bundle), so each is guarded independently in `test/test_vendored_llama_payload.py`, and both `build.yml` (every PR) and `build-wheel.yml` (release/nightly) re-check the built wheel **and** sdist against the same declaration via the shared `scripts/verify_vendored_payload.py` (one script for both lanes, so they cannot drift into a gate that stops guarding without failing) — the sdist explicitly, because `python -m build --wheel` never evaluates `MANIFEST.in` and so cannot see an sdist regression at all. Linux ships no BLAS backend by design: upstream publishes none in its Linux CPU wheels (macOS gets `libggml-blas` only via the system Accelerate framework), and the Linux `libggml-cpu` carries the optimized GEMM kernels instead
- Failed model loads (corrupt file, bad native libs) are retried only after a 300s cooldown so a broken state can't spawn a loader thread per embed call

**Embedding backend abstraction** (`EmbeddingBackend` ABC): the public swap seam for future runtimes (Ollama again, remote endpoints, ONNX) and user-defined models. Surface: `model_id`, `dim`, `is_ready()`, `embed()`, `embed_batch()`, `close()`. Consumers (vector memory, knowledge library) depend only on this interface; everything llama.cpp-specific lives in `LlamaCppEmbedder`. Swap flow: `register_embedding_backend(factory)` + `reset_shared_embedder()` replaces the singleton (pass `None` to restore the default). A backend with a different `model_id`/`dim` produces incomparable vectors — the knowledge library's `embed_signature` folds `model_id` in, so a swap automatically triggers the sig-gated knowledge re-embed; vector memory re-embeds via `migrate`.

**Sync embedding cache** (`make_sync_embed_fn()`, no args): The sync callable used by `vector_memory.py` wraps the shared embedder and caches results via `functools.lru_cache` keyed by `(input text, backend model_id)` — after a backend swap, the old model's cached vectors can never be served for the new model. Embeddings are deterministic (same text → same vector for a given model), so caching is safe. Bounded to 128 entries (~4 MB with Python boxed floats). Failures (None) are not cached — a still-downloading model is retried. Cache stats logged every 20 misses. Cache lives per `make_sync_embed_fn()` call — reset on gateway restart. Embedding through the cache never blocks on the model load (kicked in the background); callers get `None` until the model is resident.

### Model Download Manager (`embeddings.py`)

`ModelDownloadManager` (singleton via `model_download_manager()`) downloads the embedding GGUF in the BACKGROUND at gateway startup — boot is never blocked by the 610MB transfer:

**Download flow** (`ensure_model()` / `start_background_model_download()`):
- **Salvage fast-path** (`_salvage_legacy_ollama_blob`): before downloading, checks the legacy Ollama blob store (`~/.ollama/models/blobs/sha256-<digest>`, honoring `$OLLAMA_MODELS`) — Ollama stores layer blobs content-addressed and the Ollama-era GGUF is byte-identical, so migrating users skip the 610MB re-download entirely. The copy is sha256-verified like a real download; any failure falls through to the normal download
- Downloads `qwen3-embedding-0.6b-q8_0.gguf` (Q8_0 quantized, 610MB) over plain HTTPS from the public Kiro Crew CDN — URL resolution order: `KIROCREW_EMBED_MODEL_URL` env var, then the `memory.embed_model_url` config knob, then the built-in `_DEFAULT_MODEL_URL` CDN constant. No git, no cloud SDK. Streaming sha256 is computed while downloading and byte-level progress (`bytes_downloaded`/`bytes_total`) is written to `status` every ~16MB for the dashboard's determinate progress bar
- sha256-verifies the file (`06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439` — the trust anchor for every source: a tampered CDN object or mirror can only fail verification); files under `_GGUF_MIN_BYTES` (1MB) are rejected as truncated
- Installs persistently to `~/.kiro/crew/models/qwen3-embedding-0.6b.gguf` — atomic install: stages into a per-process unique file in the TARGET directory (same filesystem) then `os.replace`, so two concurrent processes (gateway + one-shot CLI) can never interleave writes into a shared staging file
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
- `GET /api/memory/embedding-status` — `enabled` is always `true`; `provider` reports the legacy `"ollama"` token (the shipped frontend hard-checks `provider === "ollama"` — kept until the frontend companion change lands); `setup_step` maps the manager's steps to the legacy vocabulary the shipped polling loop terminates on (`ready`→`done`, `failed`→`error`, `downloading`/`verifying`/`waiting_retry`→`downloading`); the raw step and attempt are additionally exposed as `download_step` + `download_attempt` for newer frontends; `server_healthy` = model file present OR model loaded; `model_id` + `model_dim` disclose the embedding model producing vectors (read live from the shared embedder — e.g. `qwen3-embedding:0.6b` / `1024`) so the Memory tab can show which model runs locally
- `POST /api/memory/embedding-model` — changes the local embedding model at runtime. Two modes, and note which one is the default: `{"path": "...", "validate_only": true}` validates only (returns `size_bytes` without touching the live backend), while **omitting `validate_only` performs the swap** — there is no `apply` flag, so a caller that sends only `path` applies the model. An empty `path` reverts to the bundled model. Refuses with 403 on a restricted session (SEL-audited), 409 while a re-embed is already running (single-flight), and 409 `env_override_active` when `KIROCREW_EMBED_MODEL_PATH` is set, because the env var wins at load and persisting a config path under it would store a path/dim pair the process never uses
- **Apply ordering** (each step gates the next, so a failure rolls back rather than half-applying): build the candidate **gated** (not serving) → install it, retiring the outgoing model in the same step so two ~700MB models never co-reside → `begin_space_change()` → bounded `wait_ready` (600s) → `set_embedding_dim()` → reconcile → **verify the recorded space equals the active signature** → persist config → `activate_shared_embedder()` → backfill in the background. Config is written LAST so a reconcile failure leaves config naming the PREVIOUS model, which is what makes the rollback rebuild that model instead of resurrecting an ungated new one. Every rollback also restores the store's previous vector width, since a store left on the new width rejects every vector against the restored model
- `GET /api/memory/embedding-status` additionally returns a `reembed` snapshot (`step`: `idle`/`applying`/`running`/`done`/`failed`, plus `done`/`total`/`error`) so the dashboard can render background re-embed progress; the card polls only while that step is busy
- `POST /api/memory/disable-embeddings` — **gone**: embeddings are always-on. Kept as a graceful HTTP 410 stub (not a 404) because the shipped frontend still renders a Disable button; remove together with the frontend button

### Model Security & Policy

| Field | Value |
|-------|-------|
| Model | Qwen/Qwen3-Embedding-0.6B (Q8_0 GGUF) |
| License | Apache-2.0 (on approved list for self-approval) |
| Source | public Kiro Crew CDN (`_DEFAULT_MODEL_URL`; sha256-pinned; `KIROCREW_EMBED_MODEL_URL` / `memory.embed_model_url` for mirrors) |
| Runtime | Vendored llama-cpp-python 0.3.34 (MIT license, `kiro_crew/_vendor/`) |
| Data flow | Text → in-process function call → float vectors (no data leaves machine) |
| Policy | Self-approvable under a public dataset / ML model policy |

Conditions met for self-approval:
1. Local use only — model runs locally, no 3P API calls
2. Apache-2.0 license — on approved list
3. Outputs are float vectors — no excluded categories (health, financial, biometric, PII)
4. Not recreating training data — generating embeddings, not content
5. Model weights sourced from the sha256-pinned Kiro Crew release bucket (integrity-verified download at runtime)

### Why llama.cpp (not TEI)

TEI (Text Embeddings Inference) uses the candle Rust framework with a Metal backend that has an [unmerged memory bug](https://github.com/huggingface/candle/pull/3197) causing unbounded GPU buffer allocation on macOS. The process consumes 4+ GB RAM and never becomes healthy. This affects ALL models on TEI/Metal, not just Qwen3. llama.cpp works correctly on all supported platforms (macOS Metal, Linux CPU) — Kiro Crew vendors it directly via llama-cpp-python, which also removes the external Ollama server the previous design depended on.

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
| GET | `/api/memory/embedding-status` | Embedding health + download progress. `enabled` always true; `setup_step` in legacy vocabulary (done/error/idle/downloading); raw `download_step` (idle/downloading/verifying/waiting_retry/ready/failed) + `download_attempt` + `bytes_downloaded`/`bytes_total`; `model_id` + `model_dim` disclose the embedding model + vector dimension; `reembed` reports background re-embed progress (`step` idle/applying/running/done/failed + `done`/`total`/`error`) |
| POST | `/api/memory/enable-embeddings` | Non-blocking: kicks/adopts the background model download and returns `{"ok": true, "status": "downloading"}` when the model is absent; wires embeddings + updates config when present |
| POST | `/api/memory/embedding-model` | Change the embedding model. `{"path", "validate_only": true}` validates only; **omitting `validate_only` applies** (no `apply` flag exists). Empty path reverts to bundled. 403 restricted session, 409 while re-embedding, 409 `env_override_active` under `KIROCREW_EMBED_MODEL_PATH` |
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
2. **Re-embed sweep** (gated on model readiness, independent of the migrated flag): once the model file is present (awaits the background download task if still in flight — safe, we are our own task), `VectorMemory.backfill_missing_embeddings()` embeds any episodic rows written with a NULL vector and rebuilds the FAISS index. Self-healing across boots and across a download that failed then later succeeded.
   - **Two producers of NULL-vector rows**, not just one: rows migrated before the model landed, and rows written by a bulk writer that passed `write_episodic(defer_embedding=True)` — the foreign-agent importer does this so its apply request is not held for minutes by per-chunk inference (see `docs/system-specs/modules/onboarding-import.md`). Import schedules its own sweep, so this boot sweep is the standing retry, not the only path.
   - The sweep needs **numpy only, not faiss**. Faiss is an optional accelerator and not a declared dependency, so requiring it made the sweep a silent no-op on a stock install. Only the index rebuild is faiss-gated; `search_episodic` falls back to `_sqlite_vector_search` (stdlib cosine over the stored blobs), so the vectors are useful either way.

The backend `POST /api/memory/migrate` endpoint and the `kirocrew memory migrate` CLI remain as a manual escape hatch, but the dashboard no longer calls them.

### Cross-Platform

macOS (Apple Silicon and Intel), Linux (x86_64, arm64/Graviton), and Windows supported. All paths use `pathlib.Path`. GGUF model downloaded over sha256-pinned HTTPS from the Kiro Crew CDN. No runtime install step — native llama.cpp libraries are vendored per platform in `_vendor/llama_cpp_libs/` and selected via `LLAMA_CPP_LIB_PATH` (the old Docker fallback is gone).

| Platform | Vendored libs | GPU | Notes |
|----------|--------------|-----|-------|
| macOS (Apple Silicon) | `macos_arm64/` | Metal (shader embedded in dylib) | Fastest |
| macOS Intel (x86_64) | `macos_x86_64/` | CPU (Metal OFF) | Built from the pinned 0.3.34 sdist for the universal desktop app's x64 slice |
| Linux x86_64 | `linux_x86_64/` | CPU | manylinux2014 (glibc ≥ 2.17) — AL2 and AL2023 both work |
| Linux aarch64/Graviton | `linux_aarch64/` | CPU | manylinux2014 (glibc ≥ 2.17) — AL2 and AL2023 both work |
| Windows x86_64 | `win_amd64/` | CPU | DLLs found via `os.add_dll_directory` |

The model download requires only outbound HTTPS (no git/git-lfs) on all platforms.

### Foreign-agent memory import

The full import contract — scope, destination mapping, dry run, conflict
strategies, and per-source assumptions — lives in
`docs/system-specs/modules/onboarding-import.md`. This section covers only the
memory-side invariants the destination writers enforce.

The selectable `memories` category covers durable memories and preferences from
supported foreign agents. It is not a raw file-copy path. Imported values pass
through the same Kiro Crew memory writers, key allowlists, per-entry size/count
limits, injection screening, conflict resolution, deduplication, audit events,
and active-entry caps described above. Existing Kiro Crew memories/preferences
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

The importer cannot turn a foreign system prompt, tool transcript, credential, or
runtime record into memory. Items that cannot be represented within the
destination writers and limits are reported as unsupported or skipped rather than
copied around those writers.

User-authored **instruction** documents (`CLAUDE.md`, `AGENTS.md`,
`~/.claude/rules/*.md`, a workspace's own `CLAUDE.md`) and the directive body of
a **persona** document (`SOUL.md`) ARE in scope, and are rewritten into
Kiro Crew's own tiers by the `instructions` category: each directive paragraph
becomes a `Lesson(category="preference")` in `lessons.jsonl` — the highest-priority
durable tier — while narrative knowledge continues to go to episodic memory via
the `memories` category. A **foreign memory row the source types as a
`directive`** is also an instruction, not a fact, so it lands in the same lesson
tier (`_add_db_directive`) under the same identity guard and ceiling rather than
being dropped. Import contributes at most 50 lessons
(`_MAX_IMPORTED_LESSONS`) because `LessonStore` prunes oldest-first at 200; an
unbounded import would silently evict the user's own accumulated corrections. What is excluded
is the persona *role*: a foreign persona document never becomes Kiro Crew's
persona (that surface is theme-pack persona, gated by
`capabilities.theme_persona`), and no foreign text is injected as system-prompt
identity. Import MUST NOT write `preferences.md` or `projects.md` — the
consolidator replaces both wholesale, so an import there is silently destroyed.
See `onboarding-import.md` → "Destination mapping".

Markdown and supported database memory values are injection-screened before they
become selectable, then screened again by the destination writer. When an
import operation needs to create its own `VectorMemoryStore`, it wires
`make_sync_embed_fn()` and its lazy factory exactly as the destination runtime
does. The callable remains non-blocking: until the embedding model is ready,
episodic writes persist normally without vectors and continue to use keyword
retrieval.

Episodic import writes are **deliberately deferred** (`defer_embedding=True`) even
when the model IS ready: per-chunk inference costs ~0.4s for a 2000-char chunk and
an import writes hundreds, so embedding inline held the apply request for minutes.
The row is keyword-searchable at once, and the embedding sweep runs afterwards off
the request (the dashboard handler schedules it; a self-owned store sweeps before
closing). Batching is not an alternative — `embed_batch` is measurably slower than
looping `embed` at import chunk sizes. See `onboarding-import.md` → "Deferred
embedding".

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
2. **JSONL fallback** (`~/.kiro/crew/lessons.jsonl`): only used when vector memory is not initialized. Read-only migration source once vector memory is active.

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

Categories: `tool`, `preference`, `knowledge`. Injected as a `[Learned corrections]` block, at most 50 lessons (`get_lessons(limit=50)` in the vector path, `_MAX_LESSONS_IN_CONTEXT = 50` in the JSONL path). The JSONL store itself retains `_MAX_LESSONS_TOTAL = 200` and prunes oldest-first beyond that, so "50 in context" and "200 on disk" are different numbers.

### Conflict resolution: which layer wins

Priority, highest first. A lower layer never overrides a higher one:

1. **Lessons** (`lesson.*`, `user_explicit`, confidence 1.0)
2. **Semantic memory, user-explicit writes**
3. **Semantic memory, automated writes** (confidence ≥ 0.8 required)
4. **Preferences / projects** (consolidation-generated Markdown)
5. **Episodic memory** (relevance-scored fragments)
6. **Recent history** (time-decayed summaries)

Lessons top the ladder by wording, not by ordering: the block header reads
"ALWAYS follow these. They override default behavior.", which is what makes a
lesson beat a contradicting preference in the same prompt.

| Conflict | Resolution | Code path |
|----------|------------|-----------|
| Lesson contradicts a preference | Lesson wins via the `[Learned corrections]` framing | `context.py` |
| Two semantic writes to one key | `user_explicit` overrides all; else higher confidence; confidences within 0.1 count as equal so newer wins | `vector_memory._write_semantic()` |
| Duplicate lessons | Substring dedup, then topic-overlap dedup (≥50% of the smaller keyword set → newer replaces older), then embedding dedup (cosine > 0.85 → longer text wins) | `vector_memory.write_lesson()` |
| Contradicting episodic fragments | No explicit resolution: time decay plus MMR surfaces the newer/more relevant fragment | `vector_memory.search_episodic()` |
| A semantic value is superseded | `_retire_stale_episodic()` tombstones episodic rows that quote the old value | `vector_memory._write_semantic()` step 9 |

### Memory across surfaces and channels

All surfaces share ONE memory store. `ContextBuilder.get_memory_for()` hands
every non-default workspace the default workspace's `VectorMemoryStore`, so
semantic, episodic, and lesson rows are global: a lesson taught in a Slack DM
applies in the dashboard and vice versa. The Markdown layers
(`preferences.md`, `projects.md`, `history/`) and the JSONL `LessonStore` are
per-workspace-directory, so those ARE isolated when channels are configured onto
different workspaces.

What differs per channel is what gets *recorded* and what reaches the model:

| Surface | Activation | What lands in the `ChannelHistory` buffer | Consolidation | Episodic extraction |
|---------|-----------|--------------------------------------------|---------------|---------------------|
| Slack DM (`D`-prefixed id) | `always` (`slack_dm_activation` default) | every authorized message, though it is largely redundant with ACP native session history | yes, both paths | yes |
| Group channel | `mention` (default for an unlisted channel) | ONLY the messages the bot acts on (a mention, or a reply in a thread it already has a session for); a plain bystander message returns before the push | yes, on the turns it answers | yes |
| Group channel | `observe` | every authorized message, mention or not, which is the point of the mode | yes, on the turns it answers | yes |
| Group channel | `off` | nothing: the handler returns before any push. The `!channel` owner command is the one exception it lets through, so the channel can be re-enabled | no | no |
| Dashboard tab | n/a | no channel buffer (no `channel_id`); ACP native session history covers it | yes, both paths | yes |

The `mention` row is the easy one to get wrong: the buffer is NOT a passive
recording of channel traffic in that mode. The activation gate returns before
`channel_history.push`, so the depth the bot can see is the depth of its own
prior involvement.

Buffer limits, per `ChannelHistory`:

| Mode | Entries | TTL | Clock | Durability |
|------|---------|-----|-------|------------|
| default (`mention`) | `_DEFAULT_MAX_ENTRIES` = 50 | `_DEFAULT_TTL_SECS` = 300s | monotonic | in-process only, lost on restart |
| `observe` | `OBSERVE_MAX_ENTRIES` = 200 | `OBSERVE_TTL_SECS` = 604800s (1 week) | wall clock (required for persistence) | JSONL on disk |

The observe pair is operator-tunable: `slack/gateway.py` constructs
`ChannelHistory` with `observe_max_entries=observe_max_messages` (default 200)
and `observe_ttl_secs=observe_ttl_hours × 3600` (default 168.0 hours). The
default 50/300s pair has no config knob.

A channel quiet for longer than the 5-minute default TTL presents an empty
buffer even though the bot was there. `observe` buffers persist to
`~/.kiro/crew/history/<channel_id>.jsonl` (path-validated: refused if it escapes
the history root or hits `is_sensitive_path`) and are lazily compacted on load,
dropping entries past the TTL and rewriting the file. `set_observe()` /
`unset_observe()` re-`deque` an existing buffer to the other `maxlen`, and
`unset_observe()` deletes the JSONL file.

**The `_user_authorized` injection gate.** `slack/events.py` resolves
`_user_authorized = is_allowed_user(sender_id)` before anything observable
happens. No unauthorized sender's text ever reaches the buffer, via two distinct
mechanisms:

- The **observe** push happens EARLY (before the activation gates, since observe
  mode records non-mentions), so it carries its own explicit predicate:
  `should_record_observe_history(channel_history, _user_authorized)`, defined in
  `security.py` so the rule lives with the other security controls.
- The **non-observe** push happens late, after `if not _user_authorized: return`,
  so it is covered by that early return rather than by a second predicate.

This is a prompt-injection control, not a courtesy: the buffer is injected
verbatim into a later turn's context, so a recorded stranger's message would
become instructions the model reads on the next authorized `@mention`. For the
same reason the ordering is load-bearing: the auth check, the message
interceptor, and the activation-off/governance gates all run BEFORE the first
push, transcription, or file download, because content that reaches the buffer
has already bypassed every later gate. The ephemeral "not authorized" reply is
deliberately deferred until after the activation checks so observe/mention
channels are not spammed with rejections, but the SEL `denied` event is emitted
immediately at the auth check, so the audit trail is complete either way.

Even when recorded, channel context is treated as untrusted: `build_message()`
passes `context_for()` output through `_neutralize_structural_markers()` so
other users' text cannot forge a prompt boundary, and each formatted line is
truncated to 300 chars.

## Skills (`skills.py`)

Markdown files at `~/.kiro/crew/skills/{name}/SKILL.md` with optional YAML frontmatter (`name`, `description`, `always`).

Supports nested directories (e.g. `skills/utils/tiny-url/SKILL.md`). The skill name is the relative path from the skills root (e.g. `utils/tiny-url`).

**Source precedence** (project-level wins): `$KIROCREW_PROJECT_DIR/skills/` → `builtin_skills/` (bundled). Auto-copied to `~/.kiro/crew/skills/` on first run. Copies entire skill directories (scripts, assets, etc.).

**Loading:**
1. **Always-on**: skills with `always: true` have full content injected every new session
2. **On-demand**: skill summaries (name + description + dir path) in session context; LLM can `cat` the file when relevant

Skills with auxiliary files (scripts, assets) include `dir` path so the LLM can `cd` and run them.

**Lazy-load (`skills.lazy_load`, default false — loader `SkillsConfig`):** controls how `get_context(budget)` (`skills.py`) injects the on-demand set.
- **OFF** (`get_context(budget=None)`): the byte-for-byte legacy full dump — every on-demand skill summarized, unranked and untruncated, under the flat 165k `_CONTEXT_BUDGET_BASE`.
- **ON** (`get_context(budget)`): `always: true` pinned skills are injected in full, plus a usage-ranked **top-K** of on-demand skills filled up to `budget`. Ranking is by `_rank_key` (`skills.py`) — `(usage_hits, effective_recency)` from the `SkillUsageLedger`, with a recency boost so freshly-added skills escape cold start. The long tail is left discoverable via the `skill_search` tool, the `$skillname` inline token, `cat`, and the per-message trigger auto-loader.

**Usage ledger (`skill_usage.py`, `SkillUsageLedger`):** in-memory per-skill hit tally with debounced, atomic persistence to `skill-usage.json` (`SKILL_USAGE_FILENAME`, co-located with the Kiro Crew home). Entries older than a 30-day TTL (`_MAX_AGE_SECS`) are dropped on load/flush so a stale skill stops occupying a top-K slot. Hits are recorded in two places: the **body-delivery loop** in `context.py` (`_record_use`, called only after `load_skill` succeeds and the body is appended to the prompt) and in `resolve_dollar_skills`. However, since `max_triggered` defaults to 0 the body-delivery recorder is inactive in stock config — `$skillname` is the only source of hits, so lazy-load ranking is effectively recency-only unless the trigger matcher is re-enabled (`max_triggered > 0`). A trigger match alone does NOT earn a hit — only actual delivery does, so pointer-only skills and false-positive matches do not inflate the ranking. Best-effort: ledger init failure falls back to recency-only / unweighted ranking without breaking skill loading.

**`skill_search` MCP tool (`kirocrew-core`):** greps skill name/description then, only on a metadata miss, the skill body (bounded, tool-call only — never per message). Schema in `mcp_core.py`, validated against `SKILL_SEARCH_SCHEMA` (`validation.py`). Does NOT record usage — searching is not using. Scope is **locally installed skills only**.

**Direct reads.** The model reaches most skills by reading `SKILL.md` itself — a
file-read tool, or `cat` in a shell — which bypasses the loader and so recorded
nothing. Unrecorded, the ledger described one access route only, pushing
search-discovered skills permanently down the ranking and making them harder to
find still: a self-reinforcing bias, not a flat undercount.

Crediting is two-phase, because the ledger's hits mean *a body reached the
model*. `SkillsLoader.resolve_tool_read_keys(tool_name, raw_params, command)`
resolves which served skills a tool call would deliver, recording nothing;
`credit_skill_reads(keys)` records once the read is known to have happened.

**Only content-delivering reads qualify.** A tool call that merely *names* a
skill path earns nothing — `rm`, `mv`, `cp`, `wc`, `chmod`, `stat`, and `grep`
(which emits matching lines, not the body) are all excluded. Crediting a mention
would re-create the very mention-as-use conflation that keeps the searches tally
out of `score()`, and would let a skill-maintenance session push an unread skill
up the ranking. The shell path attributes a verb **per command segment**
(`_shell_segments_reading_content`), so `cat a.txt && rm x/SKILL.md` does not
read as a `cat` of the skill; the structured path allowlists content-returning
tools (`_CONTENT_READ_TOOLS`), so an edit or grep tool carrying a `path` is not
mistaken for a delivery.

Reads are attributed through `_served_key_by_realpath()`, which applies the same
canonical rule as `resolve_ledger_aliases` (real file beats symlink, then
alphabetical), so a read through a symlinked skill lands on the key the Context
Budget screen displays instead of splitting one file's cost.

Observation sits in the **ACP client**, registered process-wide via
`set_global_skill_read_observer` — the same module-level-slot pattern as
`get_global_hook_store`. That layer is the only one that sees every surface's
tool calls (dashboard, Slack, subagents, task runner); wiring it per surface
would have left subagent reads uncounted, which is a skewed ledger rather than a
partial one. The per-surface permission gate (`HookManager.on_tool_call`) is NOT
usable here: file reads are auto-approved and never reach it.

Registration goes through one helper, `register_skill_read_observer` in
`skill_usage.py` — a leaf module, so no runtime imports another surface just to
register. Called from every runtime that owns a `ContextBuilder`:
`start_dashboard`, `start_api_server`, and the CLI in `cli_server.py`. Crediting
must not vary by entry point: route-dependent visibility is precisely the bias
this exists to remove, so a runtime that recorded nothing would ship a smaller
version of the same defect. The helper takes several candidates and installs the
first exposing a loader, because the API-server path builds its state **without**
a `context_builder` and reaches the loader through `task_runner._ctx`; it returns
whether it installed one so that path can log a miss instead of silently
recording nothing.

The read-intent allowlists (`_CONTENT_READ_TOOLS`, `_SHELL_READ_VERBS`) encode
the provider's current tool spellings, so a rename would silently restore the
pre-existing undercount. A call whose arguments clearly name a `SKILL.md` yet
yields no candidate is therefore logged at debug — the one signal that separates
tool-name drift from a legitimately non-reading call.

`_maybe_note_skill_read` resolves at the tool call and **offloads to a thread** —
resolution walks the skills tree after cache expiry and resolves every served
skill, which on the event loop would stall every session in the gateway. Both the
initial `tool_call` and its `tool_call_update` refinement are observed, since
which one carries `rawInput` is provider-specific, deduped by `tool_call_id`.
`_maybe_credit_skill_read` then records only on a `status == "completed"` result
(`tool_final`), so a read that was denied, errored, or never ran leaves no
delivery; that call is in-memory and safe inline. A cheap `SKILL.md` substring
gate runs before the offload, so a tool call touching no skill costs a substring
scan; observer failures in either phase are logged and swallowed.

**Registry discovery — `skill_discover` / `skill_fetch` MCP tools (`kirocrew-core`).**
The agent-facing twins of the dashboard's Skills → Discover panel, covering the
skills that are *not* on disk. Both are read-only and reach the existing
`skill_providers/` registry (skills.sh today) through the gateway rather than the
network directly, so provider timeouts, the 1 MiB response cap, the SSRF
denylist, and `_redact_external` all still apply:

| Tool | Endpoint | Returns |
|------|----------|---------|
| `skill_discover(query, limit=10≤50, provider?)` | `GET /api/skills/-/discover` | Candidate list — id, name, description, provider, author, install count, and an `installed` flag resolved against the local catalog. Each entry carries a ready-to-paste `skill_fetch(...)` call so the `owner/repo/skill` id survives verbatim. Publisher-controlled fields are clamped per-entry and labelled untrusted in the **header**. |
| `skill_fetch(id, provider="skillsh")` | `GET /api/skills/-/discover/preview` | The skill's instruction file, usable immediately with **no install step**, capped at `_SKILL_FETCH_MAX_CHARS` (32 KiB) for the context budget, prefixed with an untrusted-content warning. |

Both paths are on `server._MIXED_INTERNAL_API_PATHS` (the Skills page calls the
same two routes with cookie auth, so mixed rather than strict).

**Egress redaction.** `query` and `id` are LLM-supplied and, unlike
`skill_search`'s local grep, the gateway forwards them to a **third-party host**
— so both are passed through `redact_exfiltration_urls` + `redact_credentials`
before the request is built. A credential the model happened to include in a
search term would otherwise be disclosed to skills.sh and logged there. A
legitimate query or `owner/repo/skill` id matches no credential shape, so this is
a no-op on every real call; when it does fire the search returns nothing, which
is the correct fail-safe.

**No install tool, by design.** For a knowledge skill, fetch-and-use is the whole
workflow — the install step exists for humans who want the skill to *persist*
into the catalog (trigger auto-loading, `$token` resolution, usage ranking,
`always: true` pinning) and for bundles whose steps shell out to sibling files.
Because the mixed-path admission is prefix-matched it also reaches
`/discover/install`, so `api_skills_discover_install` refuses an `internal_auth`
caller outright (403 `code: "human_only"`) — that handler guard is the SOLE
enforcement point, not one of two layers, and installation stays a deliberate
dashboard action. Registry skills ARE bundles: `skill_fetch` returns only the
instruction file and reports the sibling file list so the agent knows when the
in-context copy is not sufficient rather than trying and failing.

**Both tools label their output untrusted**, because a registry publisher's text
reaches the model verbatim: `skill_fetch` prefixes the body, and `skill_discover`
leads with the label. The gateway's `_redact_external` scrubs credential shapes
and exfiltration URLs but cannot tell imperative prose from a description, so the
label is the only signal — and it must **lead**, not trail. `sanitize_response`
drops the TAIL at `MAX_RESPONSE_LEN` (100k) and `SkillSearchResult` puts no bound
on `id` / `name` / `author`, so a trailing label could be padded off the end by
the very publisher it warns about. `skill_discover` additionally clamps those
fields per entry (name 120, id 200, author 80, description 240) so one padded
entry cannot crowd the other candidates out of the response.

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

A match injects the skill's **full body, by default and unchanged.** What is new
is a per-skill way out: `inject_on_trigger: false` in a skill's frontmatter
reduces its contribution to a single `[Relevant skills for this message]` line —
name, truncated description, `SKILL.md` path, containing dir — rendered by
`trigger_hint()`, and the agent reads the file if the skill applies, the same
affordance `## Available Skills` already directs it to. `split_triggered()`
partitions one match into bodies and pointers, so a mixed match emits both.

Why the knob is worth having: a body is 8k–34k chars, and word-overlap matching
pulls in large unrelated skills often enough that body price per match makes
`loaded_skill` the largest single block of assembled context — ~48% of it on a
measured instance, with about half of that being verbatim resends of a body ACP
already replays from native history. Opting a skill out reclaims its full size on
every match.

Why the default is nevertheless the expensive one: a pointer makes delivery
**voluntary**. A skill authored to be *obeyed* the moment its topic appears — a
mandatory pre-flight check, for instance — would be silently skipped by an agent
that declines to read it, and a silent miss has no signal to catch it. Defaulting
to pointer would make *forgetting* the field fail open, and failing open on a
mandate is worse than spending the bytes. Opting out is therefore an explicit
per-skill statement that the skill is an offer rather than a mandate, which only
its author can make. Absent or malformed, the field means inject.

The `false` value carries no new privilege surface: it can only reduce what a
skill delivers, and foreign-imported skills are refused for declaring `triggers`
at all (`onboarding_import.py`), so an import cannot reach either path.

**Setting it from the dashboard.** `POST /api/skills/-/inject-on-trigger` (body
`{name, inject}`) edits that one frontmatter line server-side via
`SkillsLoader.set_inject_on_trigger()`, mirroring `set_pinned()` — atomic write,
caches invalidated so the next match sees the change rather than a stale parse.
`inject: true` REMOVES the key instead of writing `true`, because injecting is the
default and an absent key is the honest way to say "unchanged". It refuses any
skill whose file resolves **outside the loader's own skills dir**: `_resolve_path`
also reaches `skills.extra_paths` and the kiro-cli user/workspace dirs so the
listing can show those skills, but rewriting a `SKILL.md` Kiro Crew does not own —
possibly not even writable — is a side effect nobody asked for. Ownership is
checked before the write rather than left to the UI, which does gate on source but
does not stand between the endpoint and a direct caller. A skill with no
frontmatter block returns False
rather than silently succeeding, so the UI shows a failed toggle instead of a
no-op it reports as applied. The key it strips before rewriting is matched at
column 0 only: an indented `inject_on_trigger:` sits inside a block scalar (a
description that documents the flag, say), and deleting that line would rewrite
the skill's prose while changing a setting. Every outcome is SEL-audited, rejections included —
turning injection off changes what the agent is guaranteed to see, so "who made
this skill advisory, and when" has to be answerable.

`list_skills()` carries `inject_on_trigger`, `size_bytes` and `deliveries` so the
Skills page can show the cost behind the choice (cost = size × deliveries).
`deliveries` counts bodies that **reached a prompt**, not trigger matches: the
ledger records on delivery only, so a false-positive match, a pointer-only skill
and an undelivered match all count zero. Two consequences a surface must not
paper over — a skill already opted out **stops accruing**, so its figure is
historical and frozen (the Skills page says so in the cost line rather than
showing a number that silently stopped moving), and the field measures what was
SPENT, never how often the skill was relevant. `deliveries` is `None` when
untracked, which is NOT zero — an entry can also age out of the 30-day window.
Consumers must also join against live skill keys: the ledger retains keys for
skills that have since moved or been removed, and ranking naively by them puts a
nonexistent skill first.

It also carries `owned` — whether the `SKILL.md` sits under the directory
Kiro Crew owns. A skill reached through `skills.extra_paths` still reports
`source: kirocrew`, so source alone cannot gate the toggle; the UI hides the
control when `owned` is `false` instead of offering one the writer always
refuses. The listing's check is deliberately syscall-free (a path comparison, no
`resolve()`), because `list_skills()` also feeds the session-start skill index on
the event loop; the authoritative resolved check stays at the write boundary in
`set_inject_on_trigger`. A path differing only by a symlink therefore reads as
owned in the listing and is still refused on write — the failure mode is a toggle
that reports an error, never a foreign file being rewritten. For the same reason
`size_bytes` reuses the stat the frontmatter cache already needed for its mtime,
so the listing costs the same one stat per skill it did before the field existed.

The dashboard's structured skill editor rebuilds the frontmatter block from its
own fields, so it must carry every key it does not model. It re-emits those keys'
**original source lines verbatim** rather than reserializing a parsed value: the
form does not know a field's YAML type, so any value it invents can change the
type (a list or nested map becomes a block scalar, a folded `>` becomes literal
`|`). A field's block is defined as everything from its key line up to the next
top-level key — the inverse of the key test, not a list of accepted continuation
shapes, so indented lines, interior blank lines, indentless `- item` entries and
comments are all covered without enumerating them. That verbatim rule applies to
PRESERVATION only: the scalar view the form reads its own five fields from keeps
the narrower "indented lines continue a value" rule, because a top-level comment
after `always: true` is part of the block but not part of the value — folding it
in made the flag read as unset and the form dropped the pin. A comment attached to
one of the five modelled keys is not preserved, for the same reason their original
spacing is not: the form owns those and re-emits them from its own state. The
invariant to preserve when touching this code: editing a modelled field leaves
every unmodelled field byte-identical.

The auto-skill (`auto/*`) write paths rebuild frontmatter from the generator's
template rather than editing it, so each lifecycle key they must not lose is
carried forward explicitly from the LIVE skill: `version` (dropping it makes the
next approval overwrite an existing `.versions/` snapshot), `pinned` (dropping it
removes the archival exemption), and `inject_on_trigger` (dropping it restores
full-body injection on a skill the user made pointer-only). This applies to both
`update_auto_skill` (auto-refine) and `approve_pending_update` — a candidate never
declares any of the three, so live is authoritative. A new per-skill frontmatter
setting that the runtime reads must be added to that carry list, or an unrelated
approval will silently undo it.

Unchanged: `always: true` pinned skills (skipped by the matcher entirely) and the
explicit `$skillname` token. `skills.max_triggered` defaults to 0 (disabled): the
trigger matcher does not fire in stock config, so the agent relies only on the
index, `$skillname`, and `skill_search`. Set to a positive integer to re-enable. The
pointer block is attributed as `skill_hint` in the per-turn context breakdown, so
it is never folded into whatever precedes it.

**Why a per-skill opt-out rather than per-session dedup.** Injecting the body on
first match and a pointer thereafter would capture the measured resend waste
without any per-skill declaration, and it was considered. It was not chosen here
because it needs correct re-arming on compaction, `/new`, agent switch, model
switch, and `SKILL.md` mtime change — and a missed re-arm fails unsafe, leaving
the agent believing it holds instructions compaction has since dropped. The
compaction signal is also single-slot (`SessionManager.set_compact_callback`
refuses a second registration) and already claimed by
`DashboardState.wire_session_compact_callback`, so wiring it is not free. The
opt-out is stateless and has neither failure mode. Dedup remains a legitimate
future addition — it is orthogonal, since re-sending a body ACP already replays
does nothing for enforcement even on a skill that must be enforced.

**What `_record_use` counts.** Actual body delivery — the call now sits in the body-delivery loop in `context.py`, after `load_skill` confirms the content and the body is appended to the prompt. Only skills whose body is actually injected earn a hit; pointer-only skills (`inject_on_trigger: false`) and undelivered false positives contribute nothing to the ranking. The `resolve_dollar_skills` path also records, since `$skillname` is an intentional user action. With `max_triggered` defaulting to 0 in stock config, this recorder is inactive — only `resolve_dollar_skills` contributes hits unless the trigger matcher is re-enabled. This ensures the lazy-load hotness ledger ranks by actual utility to the agent, not by how often the word-overlap matcher fires on common words.

**CRUD operations** (via `SkillsLoader`):

**Context Budget endpoint.** `GET /api/skills/-/budget` returns the 30-day
per-skill injection cost with alias folding across renamed/aliased ledger keys.
Response shape: `{window_days, total_chars, rows: [{key, name, size_bytes,
deliveries, chars, inject_on_trigger, always, owned, source, idle_days,
folded_from?}]}`. `deliveries` is `null` when untracked (no ledger entry),
distinct from `0` (entry exists but zero hits). `chars = size_bytes *
(deliveries ?? 0)`. `folded_from` lists alias ledger keys whose `SKILL.md`
resolves (via symlink) to the same real file as the canonical key; their hits are
summed into `deliveries`. Unresolvable ledger keys (orphaned after relocation)
are dropped, not guessed. `idle_days` is days since last delivery, `null` when
untracked. `total_chars` equals the sum of all row `chars`. The fold logic lives
in a dedicated handler (`skill_budget.py`), NOT in `list_skills()`, because it
requires per-ledger-key path resolution and `list_skills()` must remain O(skills)
on the event loop. The endpoint offloads all blocking work to `discovery_executor`
(same pattern as `GET /api/skills`). The alias map is cached on the ledger's key
set so repeat calls don't re-resolve.

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
3. `~/.kiro/settings/mcp.json` and `~/.kiro/crew/mcp.json` (scanned at startup and on-demand)

**Startup behavior**: gateway calls `_init_mcp_discovery()` which runs `discover_servers_to_sync()` + `sync_to_agent_config()` to auto-add new servers from mcp.json, then logs all configured servers. Discovery/sync failures are caught independently so `list_servers()` always runs. Additionally, `server.py` fires `_bg_mcp_probe()` as a background task at startup to populate the probe cache.

**sync_to_agent_config()**: registers servers via `kiro-cli mcp add` in parallel (all Popen spawned at once, then waited), followed by a single config patch pass for `tools`/`allowedTools`. Atomic write (tmp + rename) prevents corrupted config. Checks returncode, logs stderr on failure, separate timeout handling. Falls back to direct JSON edit if kiro-cli unavailable.

**On-demand discovery** (dashboard): same `discover_servers_to_sync()` + `sync_to_agent_config()` triggered by "Discover & Sync" button.

**Command divergence** (`_commands_diverged`): an existing server is only re-synced when its `mcp.json` command differs from the one recorded in the agent config. The two legitimately differ in spelling because `agent._resolve_command` stores the `shutil.which` result while `mcp.json` keeps the bare name, so the comparison folds path resolution:

- A basename match is only accepted when one side is a **rooted path** and the other a **bare name** (no separator), since PATH lookup is what produced the rooted form. Two distinct rooted paths sharing a basename (`/opt/a/srv` vs `/opt/b/srv`) and a CWD-relative path (`bin/srv` vs `/usr/bin/srv`) each name a specific different file, so both stay divergent.
- On Windows the keys are `normcase`+`normpath` folded (paths are case-insensitive and accept either separator), and a trailing `PATHEXT` suffix is stripped from the **rooted side only** — `shutil.which("npx")` returns `...\npx.CMD`, which would otherwise read as divergent from `npx` on every cycle and re-sync + reset every session at each startup. Stripping both sides would wrongly collapse distinct executables (`foo.bat` vs `foo.cmd`).
- A leading separator with no drive letter (`/usr/bin/srv`) counts as rooted on Windows even though `ntpath.isabs` rejects it, so an `mcp.json` authored on macOS/Linux is read identically on every host.

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
scan/apply output or written to Kiro Crew config. If the destination
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
server aliases and reserves names from every effective source: the Kiro Crew
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

Hermes-style autonomous skill creation from completed sessions. **Opt-in, and STAGED for approval** — generation is **off by default** (`skills.auto_create_from_sessions` defaults **false**; enable via `kirocrew config set skills.auto_create_from_sessions true` or dashboard Settings → Skills). When on, candidates land in a pending-approval queue (`skills.approval_required` defaults **true**) and nothing goes live unattended. Pipeline: detect (during consolidation) → generate → metadata dedupe → pending queue → human approval → live → archive-if-unused.

Key v2 elements (all under `skills.*`):
- **Staged approval:** new skills route to `auto/.pending/<slug>/`; approve promotes to `auto/<slug>/` (dashboard: Skills → Pending review). Auto-approve for prose-only is opt-in via `approval_required=false`; **script-bearing candidates always require approval**.
- **Scripts:** deterministic procedures may ship a validated **Python** helper (`generate_scripts`, default true); statically validated (regex denylist + AST policy: no dynamic exec/import, destructive fs, process exec, network egress, ≤4 KB) and re-validated at the approve choke point.
- **Bounding:** archive-not-delete lifecycle `active→stale(`stale_after_days`,30)→archived(`archive_after_days`,90)`, `max_auto_skills` (100) backstop, pin + cron-referenced exemptions, never-used grace floor; pending TTL `pending_ttl_days` (30).
- **Dedupe:** embedding-free metadata comparison over all generated skills (`judge_model`).
- **On-demand:** the `crystallize` builtin skill stages a candidate from the current session.

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
| `skills.auto_create_from_sessions: true` | Config flag, default **off** (opt-in; when on, candidates STAGED, not live) |
| `skills_loader` instance passed | Wired from `slack/gateway.py` + `cli.py` |
| `include_history=True` | Idle path only, not prefs-only |
| `≥ skills.auto_min_tool_calls` messages with non-empty `tools` | Default 5 |
| No tool in the session referenced `~/.aws`, `~/.ssh`, IMDS, etc. | `_SENSITIVE_TOOL_PATTERNS` |

### Namespace

Auto-generated skills live under `~/.kiro/crew/skills/auto/<slug>/SKILL.md`. Slug validated against `^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$`. The `auto/` prefix:
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
    "max_triggered": 0,
    "auto_create_from_sessions": false,
    "approval_required": true,
    "auto_refine_on_deviation": false,
    "auto_min_tool_calls": 5,
    "auto_similarity_threshold": 0.85,
    "max_auto_skills": 100,
    "stale_after_days": 30,
    "archive_after_days": 90,
    "pending_ttl_days": 30,
    "generate_scripts": true,
    "judge_model": "claude-haiku-4.5"
  }
}
```

### CLI

No new command. Users interact via the existing skill management surface:

- Off by default (opt-in). Enable: `kirocrew config set skills.auto_create_from_sessions true` (or dashboard Settings → Skills); auto-approve prose-only: `kirocrew config set skills.approval_required false`
- Review pending candidates: dashboard Skills → Pending review, or `GET /api/skills/-/pending`
- List auto skills: filter `kirocrew` skill listings to those under `auto/`, or use `SkillsLoader.list_auto_skills()` in code
- Remove unwanted auto skill: `rm -rf ~/.kiro/crew/skills/auto/<slug>` (or dashboard skill delete when UI lands)
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

### Script hooks (`ScriptHook`, `run_script_hook`) — the shell per platform

A script hook's `command` is a single shell command line stored in
`~/.kiro/crew/hooks.json`. It runs in that platform's native shell language, and
a hook is therefore **not portable across platforms**:

| | Shell | Env var in a command | Quote grouping |
|---|---|---|---|
| POSIX | `/bin/sh -c <command>` | `$KIROCREW_HOOK_EVENT` | `'…'` and `"…"` |
| Windows | `%ComSpec% /c "<command>"` | `%KIROCREW_HOOK_EVENT%` | `"…"` only (cmd.exe gives `'` no meaning) |

Both platforms receive the same `KIROCREW_HOOK_EVENT` / `KIROCREW_HOOK_CONTEXT`
env vars and the same hook-event JSON on stdin.

**Windows spawns through `asyncio.create_subprocess_shell`, not an argv.** cmd.exe
must receive the operator's command line verbatim: an argv spawn of
`["cmd", "/c", command]` routes it through `subprocess.list2cmdline`, which
backslash-escapes every quote the operator wrote, so an ordinary
`"C:\Program Files\Python\python.exe" -c "print(1)"` reaches cmd.exe as
`\"C:\Program Files\…\"` and fails with *"is not recognized as an internal or
external command"*. `create_subprocess_shell` formats `%ComSpec% /c "<command>"`
with no argv escaping — the same parse the operator gets typing the line at a
prompt, and the only form under which both `%VAR%` and a literal `%` behave as
written. The shell spawn is guarded on `wrap_argv` + `cgroup_scope_argv` having
been no-ops; if a wrapper ever prepends anything the code falls back to the argv
path, choosing isolation over quoting fidelity.

On Windows both wrappers are pass-throughs whenever they return at all — there is
no sandbox backend and no cgroup v2 — but `wrap_argv` **fail-closes** rather than
passing through unless `agent.sandbox_allow_unsandboxed_exec` is set, so a
Windows script hook needs that opt-in (the same one script crons and Papyrus
need). Without it the hook's `SandboxUnavailableError` surfaces as the result's
`error`, naming the setting.

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
`removedTools` precedent — a raw key in `~/.kiro/crew/config.json` read by
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
- Runtime identity is turn-aware rather than key-only. Channel and dashboard dispatchers pass trusted `runtime_source` metadata to `build_message()`. New sessions use it for `[RUNTIME]`; follow-up turns refresh `[RUNTIME]` outside the one-time session context. This is required because a stable `dashboard:*` session can be resumed from Discord and `messaging.dm_scope="unified"` intentionally removes the originating channel from the session key. When trusted metadata is absent, namespaced keys (`discord:*`, `telegram:*`, `wecom:*`, `weixin:*`, `webex:*`, `teams:*`, `slack:*`) are recognized directly; bare unknown keys keep the legacy Slack fallback.
- Thread history is injected only at session start (via `build_session_context`). Within the same ACP session, kiro-cli manages conversation history natively — duplicate injection wastes context window and accelerates compaction.
- `_CRITICAL_RULES` injected for ALL agents (including custom) at session start — ensures diff rendering and OPTIONS buttons work universally
- Switchable context groups (see below) let a spawning parent drop whole sections for one sub-agent.
- Cap: `_CONTEXT_BUDGET_BASE` = 165,000 chars (~55k tokens). Which ceiling applies depends on `skills.lazy_load`: OFF (the default) uses `caps.base` as one flat shared pool; ON uses `caps.max_context`, the SUM of the independent per-section caps (190,575 chars at the reference window), so skills/steering can never eat into memory/lessons space. Note the per-section caps are computed and passed to every section either way; `lazy_load` changes the *global* ceiling and the skills block's shape (full dump vs usage-ranked top-K), not whether sections have caps.

#### Per-section caps (reference window)

Every value below is `int(165_000 × fraction)`, so the fraction is the source of
truth and the char count is derived. `_resolve_caps(window)` rescales all of them
(see the next subsection); the numbers here apply at the 1M reference window.

| Section | Constant | Fraction | Chars | Overflow behavior |
|---------|----------|----------|-------|-------------------|
| Thread history, LLM-compressed | `_COMPRESSED_HISTORY_CAP` | 27% | 44,550 | head/tail verbatim around a compressed middle |
| Lessons | `_LESSONS_CAP` | 22.6% | 37,290 | injects a `[CRITICAL ERROR — LESSONS FILE TOO LARGE]` block instructing the model to tell the user and offer `learn_remove`, logs at ERROR, then appends the truncated lessons with `…[lessons truncated]`. Shown lessons stay in effect; only over-cap content is dropped. |
| Thread history, truncation fallback | `_HISTORY_BUDGET_CHARS` | 21% | 34,650 | raw truncation when compression is unavailable |
| Daily history | `_MEMORY_HISTORY_CAP` | 16% | 26,400 | oldest tiers already compressed by the decay walk, then truncated |
| Skills | `_SKILLS_CAP` | 15% | 24,750 | top-K under `lazy_load`; tail behind `skill_search` |
| Steering | `_STEERING_CAP` | 10% | 16,500 | truncated with a marker |
| Semantic memory | `_SEMANTIC_MEMORY_CAP` | 7.7% | 12,705 | lowest-scoring entries omitted |
| Episodic memory | `_EPISODIC_MEMORY_CAP` | 7.7% | 12,705 | clamped further by `_EPISODIC_INJECT_CAP` (3,000) at the live call site |
| Projects | `_MEMORY_PROJECTS_CAP` | 3.9% | 6,435 | truncated |
| Preferences | `_MEMORY_PREFS_CAP` | 2.6% | 4,290 | truncated |
| Preamble headroom | `_PREAMBLE_HEADROOM` | 3% | 4,950 | fixed rules/identity/workspace/docs/date |
| Global ceiling (lazy_load ON) | `_MAX_CONTEXT_CHARS` | Σ above | 190,575 | newline-boundary truncation, last resort only |

`_PER_MESSAGE_CAP` = 8,000 is a within-history bound (truncate one oversized
message on the fallback path), not an additive section, so it is excluded from
the sum.

Beyond Kiro Crew's own assembly, kiro-cli manages its own context window:
`_kiro.dev/compaction/status` notifications signal that it summarized older turns,
and Kiro Crew resets its context-usage accounting at that chokepoint. Separately,
`SessionManager` trips a circuit breaker after `_CIRCUIT_BREAKER_THRESHOLD` = 5
consecutive turn FAILURES for a session key and resets the session; that counter
tracks failures, not compactions.

#### Dynamic budget scaling (per active model context window)

The `_CONTEXT_BUDGET_BASE` (165k) and its derived per-section caps above are the **1M-reference** values — the base was hand-tuned for a 1M-token window, so each section has a fixed *share of that window*. When a session runs on a **smaller-window** model (e.g. Opus 4.8 200K), injecting the same absolute char counts would consume ~5× the proportional share and accelerate compaction. `build_session_context()` / `build_message()` / `compress_thread_history()` / `build_session_replay()` therefore take an optional `model_window` (tokens); `_resolve_caps(window)` re-derives every cap against a base scaled linearly to that window (`base = _CONTEXT_BUDGET_BASE × window / _REFERENCE_WINDOW_TOKENS`, `_REFERENCE_WINDOW_TOKENS`=1,000,000). This keeps each section's **share of the window invariant across models** — a section that is 20% of a 1M window stays 20% of a 200K window (i.e. one-fifth the chars). Results are `functools.lru_cache`d per distinct window; `_ResolvedCaps.max_context` is a computed property, and the module constant `_MAX_CONTEXT_CHARS` is *derived* from `_resolve_caps(_REFERENCE_WINDOW_TOKENS)` so the section-sum lives in one place.

- **Every char cap scales, not just the memory sections:** the memory caps (prefs/projects/history/semantic/episodic), lessons, skills, steering, compressed-history, the fallback history budget, AND the per-message cap (`caps.per_message`) all scale together. The per-message cap is additionally clamped to `min(caps.per_message, budget)` at its call site so one large recent message can never exceed the scaled history budget and drop *all* history. The episodic block injected in `build_message` (the only live episodic path — `build_session_context` passes no query, so its `episodic_cap` never fires) is bounded by `min(_EPISODIC_INJECT_CAP, caps.episodic)`. The dashboard's `build_session_replay` budget (`_REPLAY_BUDGET_CHARS`, injected *outside* the capped context) scales by the same factor.
- **Reference identity:** at the reference window the scale factor is exactly 1.0, so resolved caps are byte-for-byte the module constants — the caps are derived *from* those constants (single source of the fractions), not a re-listing.
- **Fail-safe fallbacks (`resolve_model_window(model)`):** delegates to the central `model_registry.model_window(model)` authority (kiro-list cache > registry > supplementary id map > `[1m]` heuristic > `None`). `""`/`None`/`"auto"` and any genuinely-unknown id resolve to `None` ⇒ the 1M reference — so ONLY a model with a confidently-known smaller window scales the budget down; an unknown/auto window never silently shrinks the default deployment (`provider=acp` + `model="auto"` runs a 1M model). The central authority returns `None` (not a silent 200K) for unknown ids, so this fail-safe is now the authority's own contract rather than a special case here. **A context window is a property of the model, not the serving provider** — so `resolve_model_window` takes NO provider arg and `model_window` is provider-independent.
- **Floor:** `_MIN_CONTEXT_BUDGET_BASE` (20% of base ≈ the 200K tier) clamps a pathologically small/misreported window so caps can't collapse to ~0. Known limitation: below 200K every window collapses to this same floored base (forward-compat only — the registry's smallest real window is 200K), and the **fixed preamble** (`_CRITICAL_RULES` + identity/workspace/date, ~3k chars) does NOT scale, so on a small window it consumes a larger *fixed* fraction than the linear model implies. Linear scaling is intentional per the design (window-share parity); a reserve-fixed-overhead curve is a possible future refinement.
- **Callers:** dashboard (`chat_runner`), Slack (`handler`), and subagents (`subagent`) all resolve the window from the live session client via `window_for_provider_client(client)` — which prefers the provider's public `context_window_tokens()` accessor (0 until a turn completes; at `is_new` it falls through) and otherwise derives from the resolved model id via `resolve_model_window`. Background/cron paths that don't resolve a model pass `None` (reference). See `context.py` `_resolve_caps` / `resolve_model_window` / `window_for_provider_client` and the central `model_registry.model_window()` / `has_known_window()`.

### Switchable context groups (sub-agents)

A spawning parent decides which of three groups its sub-agent inherits, via `include_memory` / `include_lessons` / `include_project` on `spawn_run` and `spawn_sub_agents`. All default to `true`, so a caller that passes nothing produces byte-identical context: `build_session_context(context_groups=None)` — what every non-sub-agent caller passes — and an all-on `frozenset` are equivalent by construction.

| Group | Sections | Switchable |
|---|---|---|
| conduct | `_CRITICAL_RULES`, `[CURRENT DATE]`, agent identity + `[RUNTIME]`, UI language, `[WORKSPACE IDENTITY]`, skills index | no |
| `memory` | preferences, projects, `## Recent History`, `[Semantic Memory]`, `[Episodic Memory]`, `## Recent Session Context` | yes |
| `lessons` | `[Learned corrections]` (global + workspace), `[USER PROFILE]` | yes |
| `project` | `[DOCUMENTATION]` pointer, steering resources (CC backend only), `[PROJECT]` directory line | yes |

The steering row carries a backend caveat: the steering block is injected only on the Claude Code backend (`is_cc`), because on the ACP/kiro backend `kiro-cli --agent` loads the agent's own `resources` natively. `include_project=false` therefore suppresses steering on CC only — an ACP sub-agent still receives it, and nothing in Kiro Crew can prevent that from this call site.

conduct is not switchable because every member is an output contract or a capability pointer: a sub-agent without the skills index cannot discover what it can do, and one without `_CRITICAL_RULES` cannot format what it reports back.

Omitting a group **skips its sections** rather than capping them to zero — `MemoryStore.get_context()`'s `_cap(text, 0)` returns a `…[truncated]` marker, not an empty string, so a zero cap emits headers with no content behind them.

A sub-agent that had a group withheld is told so by name (`[CONTEXT SCOPE]`, built by `_build_context_scope_section`), so it reports the gap instead of inventing what it cannot see. That is what makes an aggressive opt-out recoverable: a wrong `false` surfaces as a question rather than a fabrication.

The flags resolve once at spawn and live on `SubagentInfo`. Every path that re-materializes a run from stored fields carries them — the stagger queue entry and `POST /api/spawn/{id}/retry` — so a queued or retried run sees the scope its caller chose. `spawn_continue` does not accept the flags but **inherits** them (`_inherited_context_groups`): a continuation rebuilds session context, because `get_or_create` returns `is_new=True` even when it restores the session via `session/load` (`resumed` is a separate flag and gates only thread history), so an un-inherited continuation would silently regain a withheld group. The live record wins; the run's persisted `context_groups` is the fallback, and a run predating the field records no scope at all — distinguishable from "all withheld" and defaulting to all-on. `GET /api/spawn` reports `context_withheld` only when something was withheld, and `_run_inner` logs the resolved set with the resulting context length.

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
