# Knowledge Module

Last Updated: 2026-07-13 (Initial spec: FileReader/SUPPORTED formats incl. `.org` plain-text routing, FolderWatcher `_walk` discovery, sweep-shielded LLMPool workers, FTS5 + graph + vector RRF retrieval)

## Overview

The Knowledge Library is KiroCrew's personal knowledge graph: a local, SQLite-backed corpus that ingests documents (folders, uploads, artifacts, fetched URLs), chunks and entity-extracts them via a bounded LLM worker pool, and serves hybrid retrieval (FTS5 keyword + graph traversal + optional vector) to the LLM through the `local_knowledge_search` MCP tool. All ingestion and search stay on-host; the only external calls are the extraction/URL-fetch worker's ACP LLM turns and the local Ollama embedding endpoint.

```
files / uploads / artifacts / URLs
   → FileReader (read + extract text)
   → HeadingAwareChunker (chunk)
   → EntityExtractor / agent_fetch (LLMPool workers)
   → KnowledgeStore (SQLite: items + items_fts + graph)
   → HybridRetriever (FTS5 + graph + vector, RRF fusion)
   → local_knowledge_search (MCP) / dashboard Knowledge tab
```

## Key Files

| File | Responsibility |
|------|----------------|
| `knowledge/readers.py` | `FileReader` — per-extension text extraction; `SUPPORTED` format set |
| `knowledge/folder_watcher.py` | `FolderWatcher` — recursive directory scan, per-file state, change/deletion detection |
| `knowledge/llm_pool.py` | `LLMPool` / `Worker` / `AcpWorker` — bounded pool of long-lived, sweep-shielded ACP workers |
| `knowledge/extractor.py` | `EntityExtractor` — LLM entity/relation extraction over the pool |
| `knowledge/agent_fetch.py` | `fetch_url_content()` — agent-assisted URL fetch over the pool (tools opt-in via `KIROCREW_KNOWLEDGE_FETCH_TOOLS`) |
| `knowledge/chunker.py` | `HeadingAwareChunker` — text/markdown/code/slide chunking |
| `knowledge/embedder.py` | `OllamaEmbedder` — local embedding via Ollama |
| `knowledge/store.py` | `KnowledgeStore` — SQLite schema, items/entities/graph, FTS5 sync |
| `knowledge/retrieval.py` | `HybridRetriever` — FTS5 + graph + vector search fused with RRF |
| `knowledge/ingestion.py` | `IngestionPipeline` — read → chunk → extract → store orchestration |
| `knowledge/dedup.py` | Cross-source deduplication |
| `knowledge/connectors/` | `BaseConnector`, `local_folder` source connectors |
| `mcp_core.py` | `local_knowledge_search` MCP tool + cached store/embedder |
| `dashboard/handlers/knowledge.py` | Dashboard Knowledge-tab API (sources, ingest, search) |
| `agent.py:_install_knowledge_agent` | Installs the `kirocrew-knowledge` kiro-cli agent used by the pool |

## Constants

| Constant | Value | Location | Purpose |
|----------|-------|----------|---------|
| `FileReader.SUPPORTED` | see below | `readers.py` | Extensions the watcher/reader will ingest |
| `DEFAULT_POOL_SIZE` | `3` | `llm_pool.py` | Worker count in a pool |
| `DEFAULT_TIMEOUT` | `60.0` | `llm_pool.py` | Per-message worker timeout (extraction) |
| `FETCH_TIMEOUT` | `120.0` | `llm_pool.py`, `agent_fetch.py` | URL-fetch worker timeout |
| `AGENT_NAME` | `"kirocrew-knowledge"` | `llm_pool.py` | kiro-cli agent the ACP worker drives |
| `_VALID_SANDBOX_MODES` | `{auto, standard, strict, cc, off}` | `llm_pool.py` | Accepted `agent.sandbox` values |
| `HARD_SKIP_DIRS` | `{.git, node_modules, __pycache__, .venv, venv}` | `folder_watcher.py` | Directories never walked |
| `DEFAULT_MAX_FILES` | `5000` | `folder_watcher.py` | Per-source file cap (newest-first) |
| `CHUNK_TOKEN_SIZE` | `800` | `chunker.py` | Target chunk size (words) |
| `CHUNK_OVERLAP` | `200` | `chunker.py` | Chunk overlap |
| `VECTOR_RRF_WEIGHT` | `2.0` | `retrieval.py` | Weight of the vector leg in RRF fusion |
| `DEFAULT_MODEL` | `"qwen3-embedding:0.6b"` | `embedder.py` | Ollama embedding model |
| `TIMEOUT` | `10` | `embedder.py` | Per-request embed timeout (s). Overridable via `knowledge.embed_timeout_secs` (positive-only; 0/unset/negative → this default) |
| `_EMBED_CONTENT_BUDGET` | `(CHUNK_TOKEN_SIZE + CHUNK_OVERLAP) * 10` | `embedder.py` | Chunk-content fold budget (chars). Overridable via `knowledge.embed_content_budget` (positive-only; folded into the embed signature so a change re-embeds) |

## 1. FileReader & supported formats (`readers.py`)

`FileReader.read(path)` returns `(text, meta)`. It first refuses sensitive paths — `is_sensitive_path(path)` raises `PermissionError("Refusing to read sensitive path: …")` before any open — then dispatches by lower-cased extension.

`FileReader.SUPPORTED` (`readers.py`) is the ingestion allowlist:

```
'', '.md', '.txt', '.org', '.py', '.java', '.ts', '.js', '.rs', '.go',
'.html', '.htm', '.docx', '.pdf',
'.csv', '.log', '.json', '.yaml', '.yml', '.sh', '.rb', '.c', '.cpp', '.h'
```

It includes markdown/plain-text (`.md`/`.txt`/`.org`), source-code extensions, and the two binary formats with declared optional deps (`.pdf` → pdfplumber, `.docx` → python-docx).

**Dispatch (`_DISPATCH`, `readers.py`)** routes only `.pdf`/`.pptx`/`.docx`/`.html`/`.htm` to specialized readers. Anything else — including `.org`, `.txt`, `.md`, and every source-code extension — falls through to the generic `_read_text` path (UTF-8 with a latin-1 fallback) and into the generic chunker downstream. So `.org` is treated as plain text; there is no Org-mode-specific parser.

**`.pptx` is intentionally out of `SUPPORTED`** even though `_read_pptx` exists: python-pptx is not declared in `setup.cfg`, so the format is kept off the allowlist (the comment at `readers.py` documents this). Reachable only if `.pptx` were re-added to `SUPPORTED`.

**Binary/optional-dep readers** degrade gracefully — a missing optional import returns an `{'format': 'error'}` meta with an install hint rather than raising:
- `_read_pdf` — pdfplumber; concatenates per-page `extract_text()`, records `page_count`.
- `_read_docx` — python-docx; converts `Heading N` paragraph styles to `#`-prefixed markdown (`content_type: 'markdown'`), records `paragraph_count`.
- `_read_html` — html2text when importable (`ignore_images=True`, `ignore_links=False`); otherwise a regex fallback strips `<script>`/`<style>` and tags.

Base metadata always carries `format`, `title` (file stem), `file_size`, `extension`, and a computed `line_count`.

## 2. Folder discovery & scan (`folder_watcher.py`)

**Namespace routing**: folder/vault sources read their target namespace from `properties["namespace"]` (default `"default"`). The `add_source` handler accepts namespace either as a top-level body field or inside `properties`; a top-level `namespace` is folded into `properties` (if dict and not already set) before storage. This ensures FolderWatcher and Watcher resolve the correct namespace at scan time via `props.get("namespace", "default")`.
`FolderWatcher.scan_source(source)` scans a folder-type source under a per-`source_id` `asyncio.Lock` (one scan per source at a time) and returns `{new, changed, deleted, skipped, capped, failed}`.

**`_walk(root, ignore_patterns, extra_skip_dirs)`** (run via `asyncio.to_thread`) is the discovery step:
- Prunes `HARD_SKIP_DIRS`, any per-source-type extra skip dirs (`SOURCE_TYPE_SKIP_DIRS["obsidian_vault"] = {.obsidian, .trash}`), and **all** dotfiles/dot-dirs in place during `os.walk`.
- Skips OS junk/lock files by case-insensitive basename match against `DEFAULT_IGNORE_GLOBS` (macOS AppleDouble `._*`, `.ds_store`, Office `~$*`, LibreOffice `.~lock.*`, `thumbs.db`, `desktop.ini`, `*.tmp`, editor swap/backup, partial downloads). This exists because a discovered-but-unreadable file that carries a supported extension fails ingestion, is never auto-retried, and would otherwise leave a source permanently stalled below 100%.
- Applies the source's `ignore_patterns` (fnmatch against the relative path).
- **Extension filter**: keeps a file only if its lower-cased suffix is in `FileReader.SUPPORTED` **or** equals `.canvas` (Obsidian canvas files).
- **Sensitive-path guard**: resolves each candidate (`Path.resolve()`) and skips it if `is_sensitive_path()` matches.
- Returns `[(full_path, mtime)]`.

**Scan bookkeeping (`_do_scan`)**:
- Discovered files above `props["max_files"]` (default `DEFAULT_MAX_FILES` = 5000) are capped **newest-first** (sort by mtime desc); the surplus count is reported as `capped`.
- Deletion detection uses the **full** discovered set (pre-cap) so capping never triggers false deletions; a vanished file's items are archived via `_handle_deleted` → `store.delete_items_batch`.
- Change detection is mtime-then-content-hash: unchanged mtime → `last_seen` bump only; changed mtime but identical SHA-256 → state refresh, no re-ingest.
- Per-file state lives in the `folder_file_state` table with `status` ∈ `{done, scanning, skipped, failed, deduped}`. `scanning` is written **before** ingest so a crash mid-file is recoverable; `skipped`/`failed`/`deduped` files are not auto-retried (user must retry).
- **TOCTOU defense**: `_ingest_file` re-resolves symlinks and re-checks `is_sensitive_path` at ingest time; a block writes `status='failed'` and emits an SEL `knowledge.source.file.ingest_denied` (`outcome="denied"`, `reason=sensitive_path_toctou`) audit event.
- After a successful scan, each newly ingested/changed file gets a **targeted** cross-source dedup (`dedup_document(..., apply=True)`) — O(k·n) over the k changed files rather than a full O(n²) corpus sweep — so a folder copy collapses any matching one-shot upload.

## 3. LLMPool workers (`llm_pool.py`)

Both entity extraction (`EntityExtractor`) and internal-URL fetch (`agent_fetch.fetch_url_content`) acquire workers from a shared `LLMPool` — a provider-agnostic, bounded pool (`DEFAULT_POOL_SIZE` = 3) of **long-lived** ACP workers. A `Worker` ABC has two concrete paths:

- **Default (kiro-cli)** — `AcpWorker` drives the `kirocrew-knowledge` agent over ACP (`AGENT_NAME`). That agent is installed by `agent.py:_install_knowledge_agent` (model `claude-haiku-4.5`, kirocrew-core tools only — no internal MCP wiring in the OSS fork).
- **`agent.provider="claude_code"` (legacy seam)** — `CCWorker` drives a long-lived `claude` CLI subprocess over stream-json I/O (haiku model, `bypassPermissions`); URL-fetch tools are opt-in via `KIROCREW_KNOWLEDGE_FETCH_TOOLS`. KiroCrew's provider enum is `["acp"]`, so this branch is dormant in practice.

### Sweep shielding + audit source

`AcpWorker.start()` wires two protections that matter for a long-lived pool worker (`llm_pool.py`):

- **Sweep shielding via `register_protected_pid`** — pool workers are direct `AcpClient` sessions, **not** `SessionMap` sessions or warm-pool providers, so the gateway's periodic orphan sweep cannot see them via `_collect_active_pids` and would SIGKILL a *busy* worker mid-task (surfacing as `ACP process exited (code=1)`). After `ensure_ready()`, `AcpWorker` registers the worker's kiro-cli PID in the sweep-protected set via `register_protected_pid` (`session_pid.py`), and unregisters it in `shutdown()` and on respawn — so the orphan sweep treats it like a live session (the same `register_protected_pid` mechanism the shared `WorkerPool` engine in `acp/worker_pool.py` applies to `ReviewPool`'s `AcpReviewWorker`).
- **`audit_source="subagent"`** — pool workers run tools without passing through `chat_runner` or `SubagentManager`, so their tool calls would otherwise never reach the security audit log. With `audit_source` set, `AcpClient` emits an SEL `log_tool_invocation` record (`source="subagent"`, `outcome="auto_approved"`) per auto-approved tool call; the emit is offloaded to `subprocess_executor()` and bounded by `asyncio.wait_for` so a hung SEL backend never stalls tool dispatch. `None` (chat/subagent clients) never double-logs.

### Sandbox parity

`_get_sandbox_mode()` reads `agent.sandbox` (default `"auto"` → standard confinement); an out-of-set value falls back to `"auto"` rather than reaching `wrap_argv` as an unknown mode. Knowledge workers are wrapped by the same OS-level sandbox as chat/Slack providers — the earlier hardcoded `"off"` (initial Knowledge Library commit) was the only KiroCrew code path bypassing that setting, and reading config here restores least-privilege parity.

### Pool mechanics

`LLMPool.start()` reads config once off the event loop and spawns all workers; `acquire()` blocks on a semaphore when all workers are busy and transparently replaces a dead worker (`is_alive()` false) on acquire. `send()` is the acquire→send→release convenience; `send_batch()` runs prompts concurrently bounded by pool size. A failed spawn during `start()` tears down all already-started workers.

## 4. FTS5 + graph + vector retrieval (`retrieval.py`, `store.py`)

`HybridRetriever.search(query, limit)` runs three legs and fuses them with Reciprocal Rank Fusion.

**Store (`KnowledgeStore`, `store.py`)** — SQLite (prefers `pysqlite3`, falls back to stdlib `sqlite3`), WAL journal, `foreign_keys=ON`. Items live in `items` (with an `embedding BLOB` and `status`), keyword search is served by the FTS5 virtual table `items_fts(title, content, tags, content=items, content_rowid=rowid)`, and the entity graph is an in-memory `SimpleDiGraph` (a minimal networkx-free replacement). The store keeps `items_fts` in sync on every insert/update/delete of the FTS-backed columns.

- **Keyword leg (`_keyword_search`)** — FTS5 `MATCH` over `items_fts`, ordered by `rank`. `_sanitize_fts5_query` double-quotes each token (doubling internal quotes) so user input never contributes FTS5 operators (parameterized quoting), drops `_STOPWORDS`, and OR-joins the remainder (Mesh-1676) so a natural-language query no longer requires every literal token to match. An all-stopword query falls back to the raw tokens rather than dropping the leg.
- **Graph leg (`_graph_search`)** — resolves query words and adjacent word-pairs to entities, expands via graph neighbors (depth 2), and ranks items by mention count.
- **Vector leg (`_vector_search`)** — brute-force cosine over `items` with `embedding IS NOT NULL AND status='active'`; returns `None` when no embedder is wired. Items whose stored embedding dimension differs from the query vector are **skipped** (not scored 0.0), with a single per-search WARNING so a model swap / stale index surfaces as "re-index needed". `_cosine_similarity` returns 0.0 for differing-length or zero vectors.
- **RRF fusion (`_rrf_fuse`, k=60)** — per-leg weights align positionally with `(keyword, graph, vector) = (1.0, 1.0, VECTOR_RRF_WEIGHT=2.0)` so semantically-strong matches dominate when the keyword leg returns literal junk (Mesh-1676). Results are tie-broken by recency (`updated_at`), and each result's `match_type` records which legs it appeared in (`keyword+graph+vector`).

**Citation enrichment** — `_attach_source_locations` batch-fetches `source_locations` (adds `section_title`, `chunk_range`, `anchor`); `_attach_citation_sources` adds `source_type`/`source_name`/`source_uri` plus the most specific per-document locator: `file_path` for folder/vault sources (from `folder_file_state`), `artifact_slug`/`artifact_name` for the aggregate artifact source (deep-links `/artifacts/<slug>`). Missing/unmapped sources degrade cleanly (extra keys simply absent).

### `local_knowledge_search` MCP tool (`mcp_core.py`)

The LLM reaches retrieval through the `kirocrew-core` MCP tool `local_knowledge_search`:
- DB path: `config_dir()/workspace/knowledge/knowledge.db`; a missing DB returns "Knowledge Library is not configured…" (SEL `not_configured`).
- `_get_knowledge_search` caches the `(KnowledgeStore, embedder)` pair across calls and rebuilds only when the knowledge DB (or its `-wal`) or `config.json` changes — avoiding the per-call schema DDL / migrate / graph-load and the Ollama availability probe.
- Default `limit` is 3; results below `min_score = 0.012` are dropped. Output is run through `redact_exfiltration_urls()` + `redact_credentials()` before returning, and every call emits an SEL audit event (`success` / `no_results` / `not_configured`). Input is validated against `LOCAL_KNOWLEDGE_SEARCH_SCHEMA` (`validation.py`).

The dashboard Knowledge tab uses the same store via a lazily-initialized `KnowledgeStore` on `DashboardState` (`dashboard/state.py`).

## Invariants

- **Sensitive paths never ingested** — `FileReader.read`, `FolderWatcher._walk`, `_hash_file`, and `_ingest_file` all gate on `is_sensitive_path()` (with symlink re-resolution at ingest time for TOCTOU).
- **`.org` and unknown-but-supported extensions are plain text** — only `_DISPATCH` extensions get specialized readers; everything else in `SUPPORTED` flows through `_read_text` → generic chunker.
- **Pool workers are long-lived and must be sweep-shielded** — any direct `AcpClient` worker that outlives a chat turn (not tracked in `SessionMap`/warm pool) must register its PID via `register_protected_pid`, or the orphan sweep will kill it mid-task.
- **LLM-derived text is redacted before storage and before return** — ingestion redacts extracted text (`ingestion._redact`), and `local_knowledge_search` redacts its assembled output.
- **FTS query input is parameterized** — user query tokens are always double-quoted literals; the user never injects FTS5 operators.
- **Embedding-dimension mismatches are skipped, not scored** — vector search excludes incomparable-dimension items so a model swap cannot fill the top-K with all-zero ghosts.
