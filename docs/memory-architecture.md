# Memory Architecture

How KiroCrew builds, stores, retrieves, and manages memories across sessions.

## Table of Contents

1. [Memory Types](#memory-types)
2. [How Memories Get Built](#how-memories-get-built)
3. [How Memories Get Used](#how-memories-get-used-context-assembly)
4. [Memory Across Channels](#memory-across-channels)
5. [Fading Mechanism](#fading-mechanism)
6. [Conflict Resolution](#conflict-resolution-lessons-vs-other-memories)
7. [Context Overflow Handling](#context-overflow-handling)
8. [Heartbeat — Background Maintenance](#heartbeat--memorys-background-maintenance-engine)
9. [Gaps & Suggested Features](#gaps--suggested-features)

---

## Memory Types

KiroCrew has 6 distinct memory layers, each serving a different purpose:

```
┌─────────────────────────────────────────────────────────────┐
│                    Context Window (55K cap)                  │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐ │
│  │  Preferences  │  │   Projects   │  │  Recent History   │ │
│  │ (preferences  │  │ (projects.md)│  │ (history/*.md)    │ │
│  │    .md)       │  │              │  │ 3-tier decay      │ │
│  └──────┬───────┘  └──────┬───────┘  └───────┬───────────┘ │
│         │                 │                   │             │
│  ┌──────┴─────────────────┴───────────────────┴───────────┐ │
│  │              Semantic Memory (key-value)                │ │
│  │  pref.*, project.*, user.* — confidence-gated writes   │ │
│  └────────────────────────┬───────────────────────────────┘ │
│                           │                                 │
│  ┌────────────────────────┴───────────────────────────────┐ │
│  │              Episodic Memory (past events)             │ │
│  │  FAISS vector search + time-decay + MMR reranking      │ │
│  └────────────────────────┬───────────────────────────────┘ │
│                           │                                 │
│  ┌────────────────────────┴───────────────────────────────┐ │
│  │              Lessons (learned corrections)              │ │
│  │  lesson.* keys — user-explicit always wins             │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 1. Preferences (`preferences.md`)

User habits, tool preferences, communication style. Replaced wholesale by the consolidator — not append-only.

| Property    | Value                                              |
|-------------|----------------------------------------------------|
| Source      | `~/.kirocrew/workspace/memory/preferences.md`      |
| Injected    | Every new session via `get_context()`               |
| Updated     | Every 30 messages by `HistoryConsolidator`          |
| Context cap | 4,250 chars                                        |

Example content:
```markdown
- Prefers Slack for communication and monitoring
- Uses the standard Python build system (setuptools/pip) for package management
- Prefers deep code analysis with hidden critical information uncovered
- Wants diagrams in documentation for complex flows
```

### 2. Projects (`projects.md`)

Active work context — CRs, packages, branches, status.

| Property    | Value                                              |
|-------------|----------------------------------------------------|
| Source      | `~/.kirocrew/workspace/memory/projects.md`         |
| Injected    | Every new session                                  |
| Updated     | Every 30 messages by `HistoryConsolidator`          |
| Context cap | 6,400 chars                                        |

Example content:
```markdown
## KiroCrew Zoom Fix (REVISION 4)
- Revision 4
- Changes: useZoom.ts CSS custom properties, ChatInput effectiveVh() fix
- Status: Published, awaiting AutoSDE review
```

### 3. Recent History (`history/{date}.md`)

Daily conversation summaries with 3-tier natural decay:

| Age        | Detail Level       | Example                                                |
|------------|--------------------|--------------------------------------------------------|
| 0–13 days  | Full entries       | `[2026-04-05 10:32] Fixed zoom bug in ChatInput.tsx…`  |
| 14–60 days | First entry + count| `[2026-03-15] Fixed zoom bug (+3 more entries)`        |
| 61–180 days| Date + count only  | `2026-02-10: 5 entries`                                |
| 181–364 days| Not loaded        | Retained on disk but excluded from context             |
| 365+ days  | Deleted            | Pruned from disk by heartbeat                          |

| Property    | Value                                              |
|-------------|----------------------------------------------------|
| Source      | `~/.kirocrew/workspace/memory/history/`            |
| Injected    | Every new session                                  |
| Updated     | On 3h idle per session (history consolidation)     |
| Context cap | 26,600 chars                                       |
| Pruned      | Daily via heartbeat (`_PRUNE_TICKS = 1440`)        |

### 4. Semantic Memory (structured key-value)

Structured facts stored as key-value pairs in SQLite. Always on — embeddings activate automatically once the model downloads.

| Property           | Value                                                    |
|--------------------|----------------------------------------------------------|
| Storage            | SQLite table `semantic_memory` + optional FAISS index    |
| Enabled by default | Yes — always-on; keyword-only retrieval until the embedding model lands |
| Key prefixes       | `pref.*`, `project.*`, `user.*` only (+ user-configurable extras) |
| Context cap        | 12,000 chars                                             |
| Retrieval          | Hybrid: `0.6 × vector_score + 0.4 × keyword_score` (keyword-only fallback without embeddings) |

Example entries:
```
user.dev_desktop_host_current: dev-host.example.com
project.kirocrew.zoom_fix_implemented: True
pref.prefers_configregions_over_null_guards: True
```

Confidence gating prevents hallucinated writes:
- LLM writes require confidence ≥ 0.8
- User-explicit writes always win regardless of confidence
- Higher confidence wins on conflict; same confidence → newer wins

### 5. Episodic Memory (past events)

Short text snippets capturing specific past events from conversations — things like "fixed the zoom bug by adding CSS custom properties" or "user prefers pytest-asyncio strict mode". Think of them as searchable bookmarks into past conversations.

| Property           | Value                                                    |
|--------------------|----------------------------------------------------------|
| Storage            | SQLite table `episodic_memories` + optional FAISS index  |
| Enabled by default | Yes — always-on; keyword fallback until the embedding model lands |
| Text length        | 10–2,000 chars per entry                                 |
| Dedup              | FAISS cosine > 0.88 rejects near-duplicates              |
| Context cap        | 3,000 chars, top-8 results per query                     |
| Max entries        | 10,000 (lowest-importance oldest pruned when exceeded)   |

Example entries:
```
[importance=0.7] Fixed rev 2 comments: useZoom cleanup, effectiveVh caching
[importance=0.4] User asked how to point to Java 17 for CoreRecsCradle build
[importance=0.8] C360 Embedding test failure: score DecimalType(38,18) vs expected (38,0)
```

Search uses decay scoring (see [Fading Mechanism](#fading-mechanism)) with MMR diversity reranking (Jaccard-based, λ=0.6) to avoid redundant results. Two-stage filtering: a raw-cosine pre-filter (`_EPISODIC_RELEVANCE_THRESHOLD = 0.55`, relaxed to `_EPISODIC_LONG_TEXT_THRESHOLD = 0.42` for entries longer than `_EPISODIC_LONG_TEXT_CHARS = 300` chars, since long texts dilute cosine scores) removes irrelevant matches, then decay-adjusted scoring ranks the survivors.

**FAISS embeddings are always-on** (`embedding_provider` coerces every value — including legacy `"ollama"`/`"none"` — to `"llama_cpp"`). The flow:
1. On gateway startup, the `Qwen/Qwen3-Embedding-0.6B` model (~610MB) downloads in the background over HTTPS from the KiroCrew CDN (sha256-verified, retried with backoff; `KIROCREW_EMBED_MODEL_URL` or `memory.embed_model_url` overrides the URL for mirrors) to `~/.kirocrew/models/`
2. Embeddings run in-process via the vendored llama-cpp-python runtime — no server or install step
3. Once the model lands, all future episodic writes get FAISS embeddings for vector search (no restart needed)
4. While the model is absent or not yet loaded, episodic search falls back to keyword matching (OR logic, LIKE on text + tags) — embeddings are always-on and cannot be disabled; keyword fallback is automatic

### 6. Lessons (learned corrections)

User-taught rules that override default behavior. Created when you say "always do X" or "remember that Y".

| Property    | Value                                                    |
|-------------|----------------------------------------------------------|
| Storage     | `lesson.<md5hash>` semantic entries (confidence 1.0)     |
| Dedup       | Substring match + topic-overlap (>50% keyword → replace) |
| Context cap | 37,250 chars, max 50 lessons                             |
| Injected as | `[Learned corrections]` block, separate from semantic    |

Example:
```
- Dashboard auto-scroll should only trigger when user is near bottom (within 80px).
- Spec generation is optional for task planning. Can go directly from raw text → steps.
- Task Runner resets the agent session after each step, so the agent can't carry context forward.
```

---

## How Memories Get Built

```
User Message
    │
    ├──► learn_add MCP tool ──► write_lesson() ──► Immediate lesson save
    │    (user says "remember X" or agent is corrected)
    │
    ├──► 30 messages ──► HistoryConsolidator (prefs path)
    │                    ├── Updates preferences.md (wholesale replace)
    │                    ├── Updates projects.md (wholesale replace)
    │                    └── Extracts semantic entries (max 20)
    │
    ├──► 3h idle ──► HistoryConsolidator (history path)
    │                ├── Appends to history/{date}.md
    │                ├── Extracts episodic entries (max 10)
    │                └── Extracts implicit lessons (corrections without "remember")
    │
```

### Two Independent Consolidation Paths

| Path                 | Trigger      | What it updates                                | Offset tracking                    |
|----------------------|--------------|------------------------------------------------|------------------------------------|
| Preferences/projects | 30 messages  | `preferences.md`, `projects.md`, semantic entries | In-memory `_prefs_offset` dict   |
| History + lessons    | 3h idle      | `history/{date}.md`, episodic, implicit lessons | Persisted `last_consolidated`      |

The prefs path does NOT advance the persisted `last_consolidated` marker — history consolidation always covers all messages, even if prefs consolidation fired earlier.

### Lesson Extraction from Chat

The history consolidation prompt includes a `"lessons"` key that extracts only implicit correction patterns — corrections the user made without explicitly saying "remember". Example:

- User says: "No, don't cache that — the value changes per request" → extracted as implicit lesson
- User says: "Remember to always use pytest-asyncio strict mode" → already saved immediately via `learn_add`, NOT re-extracted

All lesson writes go through `write_lesson()` which provides substring dedup and topic-overlap dedup.

---

## How Memories Get Used (Context Assembly)

`context.py` assembles all sources into the prompt. Different content is injected at different times:

### At Session Start (`build_session_context`)

Injected once when a new session begins:

| Component                  | Cap          | Source                                |
|----------------------------|--------------|---------------------------------------|
| Critical rules             | ~500 chars   | Hardcoded (diff rendering, OPTIONS)   |
| Current date/time          | ~50 chars    | `datetime.now()`                      |
| Agent system prompt        | Variable     | `agents/prompt.md` or custom agent    |
| Thread conversation history| 45,000 chars | `ConversationLog` (LLM-compressed or truncated) |
| Preferences                | 4,250 chars  | `preferences.md`                      |
| Projects                   | 6,400 chars  | `projects.md`                         |
| Recent history             | 26,600 chars | `history/*.md` (multi-tier decay)     |
| Skills                     | Variable     | Always-on full content, on-demand summaries |
| Lessons                    | 37,250 chars | `[Learned corrections]` block         |
| Semantic memory            | 12,000 chars | Structured key-value facts (vector)   |

Note: `build_session_context` calls `memory.get_context()` **without** a `query`, so its episodic branch never fires — episodic memory is injected separately by `build_message` (see below), not at plain session start.

### Per Message (`build_message`)

Injected on every follow-up message:

| Component                  | Source                                |
|----------------------------|---------------------------------------|
| Episodic memory            | `get_episodic_context(query_text=text, cap=3000)` — new-session messages only (skipped on follow-ups, which rely on ACP native history); top-8 relevant fragments |
| Channel history            | `ChannelHistory.context_for()` (group channels only) |
| Triggered skills           | On-demand skills matching message keywords |
| Hook context               | Config-driven context rules           |

Budget: **opt-in** via `skills.lazy_load` (default **off**, like MCP `prewarm_count=0`).

- **Off (default)** — legacy behavior, unchanged from before the lazy-load
  feature: one flat 165,000-char ceiling (`_CONTEXT_BUDGET_BASE`) shared by all
  sections, and the skills block is the full unranked dump of every on-demand
  skill.
- **On** — each section gets its own char cap, expressed as a percentage of the
  165,000-char base — memory ~37.9%, lessons 22.6%, thread history 21–27%,
  skills 15%, steering 10%. Sections are truncated to their own caps
  independently; the global ceiling (`_MAX_CONTEXT_CHARS`) is the **sum** of the
  section caps (~190k chars / ~63k tokens), so skills/steering can never eat
  into memory/lessons space. The skills block becomes a usage-ranked top-K with
  the tail behind the `skill_search` tool. The global truncation (newline-
  boundary safe) is a last-resort backstop that only fires if a section
  overflows its own cap.

---

## Memory Across Channels

KiroCrew extracts and uses memory differently depending on the channel type and activation mode:

### Channel Types and Memory Behavior

| Channel Type          | Activation Mode | History Buffer | Memory Consolidation | Episodic Extraction |
|-----------------------|-----------------|----------------|---------------------|---------------------|
| DM (D-prefix)         | `always`        | Session-based (ACP native) | ✅ Yes — prefs + history paths | ✅ Yes |
| Group channel          | `mention`       | In-memory, 50 entries, 5-min TTL | ✅ Yes (when @mentioned) | ✅ Yes |
| Group channel          | `observe`       | Disk-persisted JSONL, 200 entries, 1-week TTL | ✅ Yes (when @mentioned) | ✅ Yes |
| Group channel          | `off`           | None | ❌ No | ❌ No |
| Dashboard tab          | N/A             | Session-based (ACP native) | ✅ Yes | ✅ Yes |

### How Channel History Feeds Into Context

When KiroCrew is @mentioned in a group channel, the channel history buffer provides conversational context about what was being discussed:

```
#oncall-channel (observe mode):
  alice (5m ago): The pipeline is broken, seeing 5xx errors
  bob (3m ago): I see it too, us-west-2 is affected
  carol (1m ago): @kirocrew what's going on with the pipeline?
                    │
                    ▼
  context_for("C0123ONCALL") injects:
  [Recent channel messages for context:]
    alice (5m ago): The pipeline is broken, seeing 5xx errors
    bob (3m ago): I see it too, us-west-2 is affected
  [End of channel context]
```

Key differences between modes:

- **`mention` mode**: Only the last 50 messages within a 5-minute window are available. If nobody talked for 6 minutes, the buffer is empty when you @mention the bot. History is in-memory only — lost on restart.
- **`observe` mode**: Up to 200 messages within a 1-week window, persisted to `~/.kirocrew/history/<channel_id>.jsonl`. Survives restarts. Lazy compaction removes expired entries on load.
- **Security**: Only messages from authorized users (owner + allowlist) are recorded in observe mode. Non-authorized messages are silently dropped to prevent prompt injection.

### All Channels Share the Same Memory Store

Regardless of which channel a conversation happens in, all sessions write to the same memory store (`~/.kirocrew/workspace/memory/`). A lesson learned in a DM is available when responding in a group channel, and vice versa.

The exception is **workspace-scoped lessons** — if different channels use different workspaces (via per-channel config), their workspace-scoped lessons are isolated. Global lessons are always shared.

---

## Fading Mechanism

Three independent decay mechanisms prevent stale memories from consuming context:

### 1. History Decay (time-based tiers)

Implemented in `memory.py:read_recent_history()`. As history ages, it progressively loses detail:

| Age        | What's kept                    | Why                                    |
|------------|--------------------------------|----------------------------------------|
| 0–13 days  | Full entries with timestamps   | Recent work needs full context         |
| 14–60 days | First entry per day + count    | Enough to jog memory, saves space      |
| 61–180 days| Date + entry count only        | "Something happened" marker            |
| 181–364 days| Not loaded into context       | Retained on disk as backup             |
| 365+ days  | Deleted from disk              | Too old to be useful                   |

### 2. Episodic Decay (exponential time-decay scoring)

Implemented in `vector_memory.py:search_episodic()`:

```
score = cosine_sim × (0.7 + 0.3 × importance) × exp(-0.03 × days_old)
```

| Component                    | Effect                                          |
|------------------------------|------------------------------------------------|
| `cosine_sim`                 | Semantic relevance to current query             |
| `0.7 + 0.3 × importance`    | High-importance memories decay slower           |
| `exp(-0.03 × days_old)`     | Exponential decay: 50% at ~23 days, 10% at ~77 days |

Example: A memory from 30 days ago with importance=0.8 and cosine_sim=0.9:
```
Pre-filter: cosine_sim = 0.9 ≥ 0.55 → passes
Ranking:    0.9 × (0.7 + 0.3 × 0.8) × exp(-0.03 × 30) = 0.9 × 0.94 × 0.407 = 0.344
```
It passes the pre-filter but ranks low (0.344) — likely pushed out of the top-8 results by newer, higher-scoring memories. A memory with cosine_sim=0.4 would be filtered out entirely at the pre-filter stage.

### 3. Episodic Cap Enforcement

`history.py:_enforce_episodic_cap()` prunes when over 10,000 entries — removes lowest-importance oldest entries first.

---

## Conflict Resolution: Lessons vs Other Memories

Lessons have the highest priority in the memory hierarchy:

```
Priority (highest → lowest):
  1. Lessons (user-explicit, confidence 1.0)
  2. Semantic memory (user-explicit writes)
  3. Semantic memory (LLM writes, confidence ≥ 0.8)
  4. Preferences/projects (consolidation-generated)
  5. Episodic memory (relevance-scored fragments)
  6. History (time-decayed summaries)
```

### Conflict Scenarios

| Conflict                                          | Resolution                                                                 | Code path                          |
|---------------------------------------------------|---------------------------------------------------------------------------|-------------------------------------|
| Lesson says "always use dark mode" but preference says "light mode" | Lesson wins — injected in `[Learned corrections]` block which says "ALWAYS follow these. They override default behavior." | `context.py` injects lessons after memory |
| Two semantic entries for same key                 | Higher confidence wins; same confidence → newer wins; `user_explicit` overrides all | `vector_memory.py:write_semantic()` |
| Duplicate lessons                                 | Substring dedup + topic-overlap dedup (>50% keyword overlap → newer replaces older) | `vector_memory.py:write_lesson()`   |
| Episodic entries contradict each other            | MMR reranking + time-decay naturally surfaces newer, more relevant fragments | `vector_memory.py:search_episodic()`|

---

## Context Overflow Handling

When the 165K char (~55k token) context cap is approached, each layer has its own soft cap (individually truncated before assembly). No single component exceeds 30% of the hard cap to prevent any one category from dominating.

| Layer            | Soft Cap     | Overflow behavior                                |
|------------------|------------|--------------------------------------------------|
| Thread history (compressed) | 45,000 chars | LLM-compressed (head/tail verbatim + compressed middle) |
| Thread history (fallback)   | 35,000 chars | Raw truncation when compression unavailable             |
| Recent history   | 26,600 chars | Older entries dropped first (multi-tier decay)   |
| Projects         | 6,400 chars  | Truncated                                        |
| Lessons          | 37,250 chars | Over-cap injects a `[CRITICAL ERROR — LESSONS FILE TOO LARGE]` block (model must tell the user the file is over the cap and help shrink it via `learn_remove`; shown lessons stay in effect, only over-cap content is dropped), logs at ERROR, then appends truncated lessons with `…[lessons truncated]` as fallback |
| Semantic memory  | 12,000 chars | Lower-confidence entries dropped                 |
| Preferences      | 4,250 chars  | Truncated                                        |
| Episodic memory  | 3,000 chars  | Top-8 results only, relevance-scored (injected per new-session message via `get_episodic_context`) |
| **Hard total**   | **165,000 chars (~55k tokens)** | **Truncation at newline boundary after assembly** |

Beyond KiroCrew's context assembly, kiro-cli has its own context window management:
- **ACP-level compaction**: when kiro-cli's context window fills, it summarizes older conversation turns
- **Circuit breaker**: trips at 5 consecutive compactions — session is reset to prevent degraded responses

---

## Heartbeat — Memory's Background Maintenance Engine

The heartbeat service (`heartbeat.py`) ticks every 60 seconds and orchestrates all background memory maintenance:

```
Heartbeat tick (every 60s)
    │
    ├──► Every tick: check_idle_sessions()
    │    └── Has any session been idle > 3h with unconsolidated messages?
    │        └── Yes → fire history consolidation
    │
    ├──► Every 15 ticks (15 min): rebuild FTS index
    │    └── Rebuilds ~/.kirocrew/memory_index.db (SQLite FTS5, porter stemming)
    │
    └──► Every 1440 ticks (24h): prune old history + SEL
         ├── Delete history files older than history_max_days (default 365)
         └── Prune SEL events per retention policy
```

### How Heartbeat Drives Consolidation

The consolidator has two paths (prefs and history), but neither has its own timer:

- **Prefs path** (30-message trigger): checked inline during `maybe_consolidate()` on every message — not by heartbeat
- **History path** (3h idle trigger): driven by heartbeat calling `check_idle_sessions()` every tick

### HEARTBEAT.md — User-Facing Task Queue

Separate from memory maintenance, the heartbeat also processes `~/.kirocrew/workspace/HEARTBEAT.md`:

```markdown
# Heartbeat Tasks
- [ ] Check the open review for new AutoSDE comments. If found, fix and push.
      If none, remove this item and notify user.  <!-- deliver:D0OWNER_DM -->
- [ ] Monitor pipeline KiroCrew-pipeline for failures. Alert if blocked.
```

| Lifecycle step | Behavior                                                |
|----------------|---------------------------------------------------------|
| Read           | Each tick → read file → extract tasks (one per line)    |
| Execute        | All tasks run in parallel via `asyncio.gather`          |
| Success        | Task cleared from file                                  |
| Failure        | Task written back for retry on next tick                |
| Routing        | `<!-- deliver:C0123CHANNEL -->` routes results to specific Slack channel |

### Heartbeat Constants

| Constant           | Value       | Purpose                          |
|--------------------|-------------|----------------------------------|
| `_DEFAULT_INTERVAL`| 60s         | Tick frequency                   |
| `_FTS_REBUILD_TICKS`| 15         | FTS rebuild every 15 min         |
| `_PRUNE_TICKS`     | 1440        | History prune every 24h          |
| `history_idle_secs`| 10800 (3h)  | Consolidation idle threshold     |
| `history_max_days` | 365         | Max history retention            |

---

## Knowledge Library

The Knowledge Library is a curated document store (separate from episodic/semantic memory) for ingesting external content (files, folders, URLs) and making it searchable by the agent.

### Storage and Ingestion

- **Sources**: local files (`local_file` type), local folders (`local_folder` type), URLs (fetched via `agent_fetch`)
- **Ingestion pipeline**: `IngestionPipeline` handles chunking (`HeadingAwareChunker`), entity extraction, and embedding generation
- **Database**: SQLite with `items` table (title, summary, content, embedding, status, source_id) and `folder_file_state` table for per-file ingestion state within folder sources
- **Embeddings**: vector embeddings for semantic search via the in-process llama.cpp runtime (shared with vector memory; local only)

### Folder Watch (`folder_watcher.py`)

Recursive directory scanning with:
- Default ignores for OS temp/lock/junk files (`._*` AppleDouble, `~$*` Office locks, `.DS_Store`, `*.tmp`, `*.swp`, `*~`, `*.crdownload`, …) via `DEFAULT_IGNORE_GLOBS` (basename, case-insensitive) — keeps these from being discovered, failing ingestion, and permanently stalling a source — plus per-source `ignore_patterns` and a file cap
- Per-source locks, TOCTOU protection, crash recovery
- Pause/resume/retry/skip flow control
- Confirmation flow (always-on for folders) with inline progress view
- `folder_file_state` table tracks per-file ingestion state (sub-grouping in UI)
- Scan completion invalidates knowledge-items via `refetchInterval` + `wasSyncingRef`
- API endpoints: add/confirm/pause/resume/retry/skip + files listing

### Search

- **MCP tool**: `local_knowledge_search` in `mcp_core.py` — strict trigger rules (explicit user signals only), `MIN_SCORE=0.012` confidence threshold, default limit=3 (hard max=5), clean output format (source + content only), graceful degradation when knowledge DB not configured
- **Embedding search**: vector similarity when embeddings are available; keyword fallback otherwise
- **Security**: redaction of credentials/exfiltration URLs in results; SEL audit events for all outcomes (success, no_results, not_configured)

### Embedding Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/knowledge/embedding/status` | Reports enabled state, model info, total vs. embedded item counts |
| `POST /api/knowledge/embedding/generate` | Batch-embed unembedded items (or `rebuild: true` to re-embed all, 200 items per batch) |
| `GET /api/knowledge/search` | Search-for-context endpoint with embedding-based ranking |

### Navigation

Knowledge Library is a built-in surface (positioned in sidebar after Autopilot, before App Store) — no longer an App Store app. Startup cleanup removes stale installed knowledge app directory.

---

## Gaps & Suggested Features

### Gap 1: No Cross-Session Conflict Detection
**Problem**: If two concurrent sessions write conflicting semantic entries, last-write-wins with no notification.
**Suggestion**: Add optimistic locking (version field) to semantic entries. Surface conflicts in dashboard with merge UI.

**Thread-safety today (intra-process)**: Within a single gateway process, `VectorMemoryStore` serializes all sqlite + FAISS mutations under a `threading.RLock` (`vector_memory.py`). The lock guards three critical sections — the semantic `SELECT → conflict-resolve → UPSERT` (`_write_semantic`), the episodic FAISS-add + `_faiss_id_map` append + `INSERT` (`write_episodic`), and the episodic FAISS-search + id_map lookup (`search_episodic`) — so writer worker threads and event-loop readers can't corrupt the shared connection or the non-thread-safe FAISS index. The lock is deliberately **never** held across the blocking Ollama embed (embeds run before the locked region), and every embed-bearing write is offloaded off the event loop: `HistoryConsolidator._consolidate` wraps `_write_structured_memory` in `asyncio.to_thread` (`history.py`) and the dashboard memory handlers do the same (`dashboard/handlers/memory.py`), so a slow/hung embedding endpoint no longer freezes the gateway loop. This serialization is **per-process only** — Gap 1 stays open because the RLock adds no conflict detection/notification and does not coordinate across separate KiroCrew processes.

### Gap 2: No Memory Importance Feedback Loop
**Problem**: Episodic importance is set at write time and never updated. Frequently-retrieved memories should gain importance.
**Suggestion**: Track retrieval count per episodic entry. Boost importance on retrieval: `importance = min(1.0, base + 0.05 × retrievals)`.

### Gap 3: Lesson-Memory Contradiction Detection
**Problem**: A lesson might say "never use Python 2" while a stale preference says "prefers Python 2". No automated detection.
**Suggestion**: During memory consolidation, cross-reference lessons against preferences/semantic entries. Flag contradictions for user review in dashboard.

### Gap 4: No Memory Sharing Between Instances
**Problem**: Each KiroCrew instance has isolated memory. No way to share learned lessons or project context across team members' instances.
**Suggestion**: Export/import memory subsets (lessons, project context) as portable JSON bundles. See [persistent-agent-channels](system-specs/modules/persistent-agent-channels.md) for multi-agent collaboration features.

### Gap 5: No Selective Memory Forget
**Problem**: Users can delete individual entries but can't say "forget everything about project X" — requires manual cleanup across all memory layers.
**Suggestion**: Add `kirocrew memory forget "project X"` CLI command that cascades deletion across semantic, episodic, history, and preferences.

### Gap 6: History Consolidation Can Miss Short Sessions
**Problem**: 3h idle trigger means a quick 5-minute session that answers a critical question may never get consolidated if the user starts a new session before the idle timer fires.
**Suggestion**: Add a message-count trigger (e.g., 10 messages) as an alternative to the idle timer for the history path, similar to how the prefs path uses 30 messages.

### Gap 7: No Embedding Model Hot-Swap
**Problem**: Changing the embedding model requires re-embedding all entries.
**Status**: Partially addressed. The `EmbeddingBackend` ABC + `register_embedding_backend()` in `embeddings.py` provide the swap seam: a backend with a different `model_id`/`dim` automatically changes the knowledge library's `embed_signature`, triggering its sig-gated re-embed, and the sync embed cache is keyed by `(text, model_id)` so old-model vectors are never served post-swap. Vector memory still requires an explicit `migrate`/re-embed.
**Remaining suggestion**: Store model version per episodic entry and re-embed vector memory in the background on swap, with keyword fallback for un-migrated entries.

### Gap 8: Observe-Mode History Not Consolidated Into Long-Term Memory
**Problem**: Observe-mode channel history has a 1-week TTL and is never consolidated into episodic or semantic memory. Important team decisions discussed in channels are lost after 7 days.
**Suggestion**: Add periodic observe-mode summarization. Extract key decisions and facts into episodic memory with `source: channel_observe` tag. Allow pinning via `!pin` command or 📌 emoji reaction.
