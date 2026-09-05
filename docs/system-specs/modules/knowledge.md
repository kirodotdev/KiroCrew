# Knowledge Module

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

### LLM worker-pool policy

Knowledge ingestion and URL-content acquisition use separate long-lived worker
pools. The extraction pool uses `knowledge.extraction_pool_size` and the
URL-fetch pool has one worker. Each pool's reasoning effort is
operator-configurable and resolves through the same chain (`llm_pool._get_workload_effort`):
`knowledge.extraction_effort` / `knowledge.fetch_effort` →
`agent.role_efforts.background` → fallback (`DEFAULT_EXTRACTION_EFFORT` = `high`
for extraction as the last resort; provider default for fetch). An invalid or
typed-wrong key falls through to the role chain rather than the hard default, so
a typo cannot silently raise cost. `""` on either key means inherit; the keys
are dashboard-editable (`_EDITABLE_CONFIG` enums `["", *EFFORT_LEVELS]`), with
the controls on the Knowledge tab's settings (model, both efforts, pool size —
the pool size still requires a restart). Both pools drive the same
`kirocrew-knowledge` agent
and preserve the existing model resolution: `knowledge.extraction_model` →
`agent.model` → provider/`auto`.

For the Kiro ACP backend, the worker applies the resolved level through the
`/effort` command, gated first by `model_supports_effort` on the client's model
so an unsupported model (`auto`, non-reasoning families) skips the push with an
INFO log instead of warning per spawn; Claude ACP keeps its
`supports_config_option` gate and advertised-level descent (the shared ladder
arithmetic lives in `effort.select_effort_level`). A rejected command still
falls back to provider default.

Separate pools make the different workload policies structural for long-lived
sessions rather than relying on a worker being reused by only one workload by
convention. There is no `knowledge_llm_pool` compatibility alias and no
`use_config_pool_size` flag: a pool binds an explicit
`config_pool_size_key` (extraction only), so `knowledge.extraction_pool_size`
cannot resize the fetch or auto_research pools.

## Role & boundary

The Knowledge Library is the agent's **precise-recall complement to memory** — it is defined as much by the two things it is *not*:

- **Not memory.** The memory subsystem (see `memory-skills-hooks.md`) carries the small, distilled, always-on picture and is **injected into every prompt** by `ContextBuilder`; it is lossy by design (it dedups, decays, and paraphrases). The Knowledge Library instead holds the durable, **verbatim, cited** detail that memory can only approximate, and it is **pulled on demand**: the LLM reaches it only through the `local_knowledge_search` MCP tool, never as per-turn context injection (§4). Its job is to surface the exact chunk — with a citation back to source (§4, "Citation enrichment") — precisely when memory's recall is imprecise or absent.
- **Not a workspace.** It is not scratch space for the current task's files or state; it is the durable, source-owned record that outlives any single task or session. A project directory enters the Library only as a `local_folder` source the user registers by hand, and is *ingested* read-only rather than adopted as a working set.

This is the boundary the agent's write path (§2b) captures against: verbatim, long-tail durable detail that memory would only paraphrase belongs here; small, always-shaping, distilled knowledge belongs in memory; transient current-task state belongs in neither. How well the KB fills that role is measured against the criteria in "Success criteria" below.

## Success criteria

The Knowledge Library's job — surface the exact, cited chunk when memory's recall falls short — is judged on **two tiers**. Tier 1 now has a deterministic in-tree harness (`kirocrew bench kb-retrieval`, see "What is measured today"); no Tier 2 harness exists in-tree yet. This section defines the target so a retrieval change (recency weighting, a reranker, content-typed TTL) can be judged against a fixed bar rather than by eye.

### Tier 1 — intrinsic retrieval quality

Against a **frozen golden set** of `(query → the chunk(s) that should answer it)`, does retrieval fetch the right chunk and rank it high? Definitions follow the IR / RAG canon:

| Metric | Definition | Reads |
|--------|------------|-------|
| **recall@k** | `|relevant ∩ retrieved@k| / |relevant|` — fraction of relevant chunks that land in the top-k | coverage / completeness |
| **precision@k** | `|relevant ∩ retrieved@k| / k` — fraction of the top-k that is relevant | signal-to-noise |
| **MRR** | mean of `1 / rank_of_first_relevant` over queries | how early the first hit lands |
| **nDCG@k** | graded relevance with a log-rank discount, normalized to the ideal ordering | rank quality when relevance is graded, not binary |
| **hit@k** | binary: did *any* relevant chunk make the top-k | cheap "did retrieval work at all" gate |

RAGAS names the rank-aware pair **context precision** / **context recall** (the latter needs a reference answer); they are the same two ideas applied to the retrieved context.

### Tier 2 — extrinsic task-lift

Does that recall change the outcome? Measured A/B — the same task set run with the KB **on** vs **off** (or vs a baseline), scored on **task success**, not retrieval position. Recall without task-lift means the KB retrieves the wrong thing well. This mirrors how mature agent harnesses gate on outcome rather than retrieval — GAIA2 (pass@1 against a write-action verifier), SWE-bench (fail-to-pass test execution), τ-bench (grounded end-state diff) — and π-Bench's practice of scoring "used the right context" as an axis distinct from "task completed." A companion generation check, **faithfulness** (fraction of the answer's claims actually supported by the retrieved chunk; RAGAS, reference-free), guards against a cited-but-unsupported answer.

### Query classes the golden set must cover

A clean teach→recall set overstates quality: memory/KB systems break on the *hard* classes. The set must enumerate them explicitly (taxonomy adapted from the LobsterAIAgent memory harness and LongMemEval):

- **Clean-fact recall** — baseline single-hop lookup.
- **Multi-hop** — the answer requires joining two or more chunks (the entity graph's reason to exist, §4).
- **Time-bound / freshness** — a fact true only "as of" a date; the correct *version* must win.
- **Correction** — a later chunk fixes an earlier stated fact; the corrected value must be surfaced.
- **Contradiction** — two chunks conflict; retrieval must surface the conflict, not silently pick one.
- **Retraction** — a withdrawn fact must stop being recalled.
- **Reinforcement / corroboration** — repeated independent sources should raise confidence, not merely duplicate.
- **Hypothetical-exclusion** — speculative or conditional statements must NOT return as settled fact.
- **Abstention** — when the answer is genuinely absent, retrieval should return nothing above the score floor rather than a false near-match (LongMemEval scores this explicitly).
- **Citation-fidelity** — the returned chunk must actually support the claim it is cited for (§4, "Citation enrichment").

### What is measured today

The code computes and floors a retrieval **score**, but no Tier-1/Tier-2 metric and none of the hard query classes above:

- `HybridRetriever.search` fuses the keyword + graph + vector legs by RRF (`_rrf_fuse`, k=60; vector leg weighted `VECTOR_RRF_WEIGHT = 2.0`), tie-broken by `updated_at` recency — a secondary sort key, **not** a decay weight (`retrieval.py`, §4).
- Results below `min_score = 0.012` are dropped by the tool caller (`mcp_tools/knowledge.py`), not inside the retriever.
- `kirocrew eval` ships four scenarios (`smoke_test`, `memory_recall_basic`, `lesson_application`, `context_accumulation`) scored per-assertion (`contains` / `regex` / `judge`) with an optional 1–5 LLM judge (`eval/judge.py`, pass ≥ 3.0). All four are clean single-fact teach→recall or accumulate→summarize flows; none exercises correction / contradiction / retraction / time-bound / reinforcement / hypothetical, and none reports recall@k, MRR, or task-lift.

- `kirocrew bench kb-retrieval` is the Tier-1 ruler: it scores a frozen golden set (`eval/bench/data/kb_golden_v1.json`, hand-authored, covering the query classes above) against a real `KnowledgeStore` + `HybridRetriever` and reports recall@k / MRR@k / nDCG@k per class, plus `abstention_rate`. It measures **bare `HybridRetriever.search`** — the tool-caller `min_score` floor above is deliberately not applied — so its numbers describe the raw retriever, not the agent-visible MCP surface; a floor change at the tool caller will not move them.

The remaining gap is therefore an **A/B task-lift harness** (Tier 2), plus a floor-aware variant of the Tier-1 ruler if the agent-visible surface is ever to be scored directly — the precondition for tuning recency, adding a reranker, or content-typed TTL against evidence rather than intuition.

## Key Files

| File | Responsibility |
|------|----------------|
| `knowledge/readers.py` | `FileReader` — per-extension text extraction; `SUPPORTED` format set |
| `knowledge/folder_watcher.py` | `FolderWatcher` — recursive directory scan, per-file state, change/deletion detection |
| `knowledge/watcher.py` | `KnowledgeWatcher` — polls registered sources for changes; sig-gated self-heal re-embed (single-flight, off-loop DB access) |
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
| `dashboard/handlers/knowledge.py` | Dashboard Knowledge-tab API (sources, ingest, search, source-scoped list + `/source-counts`) |
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
| `HARD_SKIP_DIRS` | `{.git, node_modules, __pycache__, .venv, venv, dist, build, out, target, cdk.out}` | `folder_watcher.py` | Directories never walked |
| `_LARGE_REBUILD_WARN_THRESHOLD` | `1000` | `watcher.py` | Stale-item count at which the self-heal rebuild logs a prominent WARNING (usually an embedder-sig change invalidating the whole corpus) |
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
'.csv', '.log', '.json', '.jsonl', '.ndjson', '.yaml', '.yml', '.sh', '.rb', '.ps1', '.psm1', '.psd1', '.c', '.cpp', '.h'
```

It includes markdown/plain-text (`.md`/`.txt`/`.org`), source-code extensions, and the two binary formats with declared optional deps (`.pdf` → pdfplumber, `.docx` → python-docx).

**Dispatch (`_DISPATCH`, `readers.py`)** routes only `.pdf`/`.pptx`/`.docx`/`.html`/`.htm` to specialized readers. Anything else — including `.org`, `.txt`, `.md`, and every source-code extension — falls through to the generic `_read_text` path and into the generic chunker downstream. So `.org` is treated as plain text; there is no Org-mode-specific parser. Text decoding (shared by `_read_text` and `_read_html` via `_decode_text_bytes`) is a single-open buffer decode: BOM-sniffed UTF-16 LE/BE first, otherwise UTF-8 with a latin-1 fallback. The UTF-16 branch is extension-agnostic — any text format arriving as BOM'd UTF-16 decodes correctly, not only the PowerShell files (Windows tooling writes UTF-16LE) that motivated it.

**`.pptx` is intentionally out of `SUPPORTED`** even though `_read_pptx` exists: python-pptx is not declared in `setup.cfg`, so the format is kept off the allowlist (the comment at `readers.py` documents this). Reachable only if `.pptx` were re-added to `SUPPORTED`.

**Binary/optional-dep readers** degrade gracefully — a missing optional import returns an `{'format': 'error'}` meta with an install hint rather than raising:
- `_read_pdf` — pdfplumber; concatenates per-page `extract_text()`, records `page_count`,
  and releases each page immediately after extraction so its parsed-layout cache does not
  remain resident until the whole document closes (`Page.close()` when available, with
  `flush_cache()` compatibility for pdfplumber 0.10).
- `_read_docx` — python-docx; converts `Heading N` paragraph styles to `#`-prefixed markdown (`content_type: 'markdown'`), records `paragraph_count`.
- `_read_html` — html2text when importable (`ignore_images=True`, `ignore_links=False`); otherwise a regex fallback strips `<script>`/`<style>` and tags.

Base metadata always carries `format`, `title` (file stem), `file_size`, `extension`, and a computed `line_count`.

## 2. Folder discovery & scan (`folder_watcher.py`)

**Namespace routing**: folder/vault sources read their target namespace from `properties["namespace"]` (default `"default"`). The `add_source` handler accepts namespace either as a top-level body field or inside `properties`; a top-level `namespace` is folded into `properties` (if dict and not already set) before storage. This ensures FolderWatcher and Watcher resolve the correct namespace at scan time via `props.get("namespace", "default")`.
`FolderWatcher.scan_source(source)` scans a folder-type source under a per-`source_id` `asyncio.Lock` (one scan per source at a time) and returns `{new, changed, deleted, skipped, capped, failed}`.

**`_walk(root, ignore_patterns, extra_skip_dirs)`** (run via `asyncio.to_thread`) is the discovery step:
- Prunes `HARD_SKIP_DIRS`, any per-source-type extra skip dirs (`SOURCE_TYPE_SKIP_DIRS["obsidian_vault"] = {.obsidian, .trash}`), and **all** dotfiles/dot-dirs in place during `os.walk`. Build-output trees are in `HARD_SKIP_DIRS` because a rebuild presents hundreds of regenerated files as changed content; `cdk.out` (AWS CDK synth output) needs its own entry because its name only *contains* a dot, so the dot-prefix rule never prunes it.
- Skips files that are never documents by case-insensitive basename match against `DEFAULT_IGNORE_GLOBS`. Two classes:
  - OS junk / editor and Office lock files (macOS AppleDouble `._*`, `.ds_store`, Office `~$*`, LibreOffice `.~lock.*`, `thumbs.db`, `desktop.ini`, `*.tmp`, editor swap/backup, partial downloads). A discovered-but-unreadable file carrying a supported extension fails ingestion, is never auto-retried, and would leave a source permanently stalled below 100%.
  - Dependency lock files (`package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`, `pnpm-lock.yaml`, `bun.lockb`, `bun.lock`, `poetry.lock`, `uv.lock`, `pipfile.lock`, `cargo.lock`, `gemfile.lock`, `composer.lock`, `packages.lock.json`, `gradle.lockfile`, `flake.lock`). These ingest successfully, which is the problem: they are large machine-generated resolution output, each chunk costs one extraction call, and regenerating one bills it again on the next sweep.
- Applies the source's `ignore_patterns` (fnmatch against the relative path).
- **`.kiroignore` (`kiroignore.py`)**: when the SOURCE ROOT holds a `.kiroignore`, its rules are compiled once per sweep and applied on top of everything above. Matching **directories are pruned in place** during `os.walk`, so a generated tree (`cdk.out`, coverage output) is never descended rather than filtered file by file. The rule file itself is never indexed — its extensionless name is in `FileReader.SUPPORTED`, so it would otherwise be ingested as a document.
  - Syntax is a documented SUBSET of gitignore: `#` comments, blank lines, trailing `/` (directories only), leading `/` (root-anchored), any other embedded `/` (also anchored; a separator-free pattern matches that basename at any depth), `*` / `?` / `**`, and `!` negation where the last matching rule wins. NOT supported: `[a-z]` character classes (brackets match literally), backslash escapes other than a leading `\#` / `\!`, nested `.kiroignore` files in subdirectories, and re-including a path underneath an excluded directory (as in git).
  - **Defensive by contract**: the file is user-authored and re-read every sweep, so an unreadable, oversized (> `MAX_FILE_BYTES` = 64 KiB) or undecodable one degrades to "no extra exclusions" and never raises mid-scan; a single unusable pattern is dropped and the rest of the file still applies. Rules are capped at `MAX_RULES` = 1000.
  - **`.gitignore` is deliberately NOT read as a fallback.** A scan treats a file that stops being discovered as DELETED and archives its items (`_handle_deleted`), so honouring `.gitignore` implicitly would, on the next sweep, drop already-indexed documents out of every existing folder source whose root happens to be a repository. Creating a `.kiroignore` is an explicit act, so the same removal is the user's intent.
- **Extension filter**: keeps a file only if its lower-cased suffix is in `FileReader.SUPPORTED` **or** equals `.canvas` (Obsidian canvas files).
- **Sensitive-path guard**: resolves each candidate (`Path.resolve()`) and skips it if `is_sensitive_path()` matches.
- Returns `[(full_path, mtime)]`.

**Scan bookkeeping (`_do_scan`)**:
- Discovered files above `props["max_files"]` (default `DEFAULT_MAX_FILES` = 5000) are capped **newest-first** (sort by mtime desc); the surplus count is reported as `capped`.
- Deletion detection uses the **full** discovered set (pre-cap) so capping never triggers false deletions; a vanished file's items are archived via `_handle_deleted` → `store.delete_items_batch`.
- Change detection is mtime-then-content-hash: unchanged mtime → `last_seen` bump only; changed mtime but identical SHA-256 → state refresh, no re-ingest.
- Batched `last_seen` flush/commit and stale-claim release run off the asyncio
  event loop; each flush and its commit stay within one worker hop so they share
  the thread-local SQLite connection.
- Per-file state lives in the `folder_file_state` table with `status` ∈ `{done, scanning, skipped, failed, deduped}`. `scanning` is written **before** ingest so a crash mid-file is recoverable; `skipped`/`failed`/`deduped` files are not auto-retried (user must retry).
- **TOCTOU defense**: `_ingest_file` re-resolves symlinks and re-checks `is_sensitive_path` at ingest time; a block writes `status='failed'` and emits an SEL `knowledge.source.file.ingest_denied` (`outcome="denied"`, `reason=sensitive_path_toctou`) audit event.
- After a successful scan, each newly ingested/changed file gets a **targeted** cross-source dedup (`dedup_document(..., apply=True)`) — O(k·n) over the k changed files rather than a full O(n²) corpus sweep — so a folder copy collapses any matching one-shot upload.
- **The dedup unit is always the DOCUMENT, never the source.** For a folder source a document is a `folder_file_state` row; for everything else it is one `content_hash` group of `items` (`enumerate_docs` groups by `(source_id, content_hash)`), so an aggregate source holding many documents (`artifact`, `agent`) dedups per document like any other. `_delete_doc` drops that document's items and marks its owning state row `deduped` so nothing re-ingests it; it removes the source row only once the source is provably empty (no items, no state rows, and not a folder/vault), which is what keeps a collapsed one-shot upload from lingering as an empty row. There is no source-level unit, so the former `_AGGREGATE_SOURCE_TYPES` carve-out — whose cost was that aggregate documents were never deduped at all — is gone.
- **Scheduled sweeps.** `KnowledgeWatcher._maybe_dedup_sweep` runs a full `dedup_sweep` every `knowledge.dedup_every_n_sweeps` sweeps (default 12, ~hourly at the 300s interval; 0 disables). The targeted per-ingest call and the pre-ingest exact-hash gate cannot catch a near-duplicate or a pre-existing one, so the periodic pass is required for duplicates to actually be collapsed.
- **One document, several locations.** A document held by two sources is ONE stored copy with a `source_locations` row per source, not two copies where one is destroyed. A collapse attaches the loser's source as a location of the winner's items, deletes the loser's redundant copy, and records `merged_into_source_id` on the loser's state row. Three consequences follow, and each closes a way the previous design lost data: `delete_source_cascade` re-points `items.source_id` to a surviving holder instead of deleting a document another source holds (`reassign_item_source` is the only path that moves ownership, since `_ITEM_COLUMNS` deliberately excludes the column); deleting the winner clears the marker so the document is ingested again rather than stranded; and "empty source" now means holding nothing by location either, in both the dedup check and the boot-time orphan sweep, because reaping a source would delete the very rows recording co-ownership. The marker names a SOURCE and never the winner's item ids: `item_ids` means "the items this row owns", and dedup derives a document's hash and embedding from whatever it points at, so a row naming the winner's items would be enumerated as a second document over one physical item set — and collapsing that pair deletes the surviving copy. `_match_reason` refuses any pair whose `item_ids` overlap for the same reason. Per-source counts report what a source HOLDS, while the Library total counts documents, so a shared document is visible under both sources without inflating the total.

- **Pre-ingest duplicate gate.** `IngestionPipeline._skip_as_duplicate` refuses a write whose whole-text `content_hash` already exists in another source, on every ingest path, recording a terminal `ingestion_jobs` row with `status='skipped_duplicate'`. Refusing is not the same as doing nothing: the items the call was going to REPLACE are deleted first, because the document's content changed to something already stored elsewhere and its previous items are now superseded. Leaving them would keep the old text searchable and — since the state row is then recorded with an empty group — unreachable by the deleted-file path. A folder file refused this way is marked `deduped` rather than `done`; an artifact or agent document gets the same marker in its item-state row so the owning sync does not retry a write the gate will refuse again. The gate is not order-blind: it consults the same `PERSISTENT_SOURCE_TYPES` ranking `pick_winner` uses, so an incoming **persistent** source (folder / vault / wiki) is allowed to land when the current holder is **transient** (a one-shot upload or chat capture), and the post-ingest sweep then collapses the pair keeping the persistent copy. Refusing on arrival order alone inverted that ranking: the folder copy was marked `deduped`, the only searchable copy stayed inside the upload, and deleting the upload left none. Equal rank still refuses, which is the cheap path — it skips the chunking and extraction the sweep would immediately undo. Exact-hash only — the fuzzy tier needs embeddings and cannot run inline. The whole gate is ONE `BEGIN IMMEDIATE` transaction that re-reads the holder **under the write lock** and declines to dedupe when it is gone (a cheap unlocked probe runs first so the common not-a-duplicate answer does not serialize every ingest): it makes the incoming source DEPEND on the holder, so a concurrent `delete_source_cascade` must not cascade that copy away in between. The scan's terminal `deduped` write (`FolderWatcher._record_deduped_state`) takes the same lock and derives the file's item group from **its own row** instead of assuming it empty, because a cascade landing after the gate committed reassigns the surviving item to this source and can adopt it into that row; predicting `[]` there would erase the adoption and leave the last copy owned but named by no row — unreachable by the deleted-file path and undeletable. The derivation is row-scoped, never by `(source_id, content_hash)`: two documents in one source may legitimately hold identical text, and a hash-scoped read would name one physical item into both rows, destroying it on the first delete of either. **All three doc-state tables do this**, through the one primitive `KnowledgeStore.surviving_group_in_txn(table, source_id, key)` — `folder_file_state` keyed by `file_path`, `artifact_item_state` and `agent_item_state` by `slug` (`_DOC_STATE_KEY_COL`). An aggregate row that ends up owning items is written `active`, not `deduped`, because `find_document_by_hash` only matches `active` and a row owning content while reporting `deduped` would let the same text in again under a second slug. Every one of the three takes `BEGIN IMMEDIATE` and therefore runs off the event loop through `run_to_completion`; `test_deduped_state_writes_are_never_called_on_the_event_loop` is the ratchet. **Known gap, pre-existing and folder-only:** for a transformed file (PDF/DOCX/HTML) the adoption itself matches nothing, because `_adopt_reassigned_item` keys on `COALESCE(text_hash, content_hash)` and a refused folder row derives `text_hash` from a byte-identical sibling row it may not have — so nothing lands in the row for the terminal write to preserve. The aggregate tables store the text hash in `content_hash` directly and are unaffected. Closing it needs the incoming document's text hash carried out of the gate rather than derived from a sibling.
- **Legacy items with a null `content_hash` are not exact-matchable.** The column arrived by `ALTER TABLE`, so rows written before it are null, and tier-1 requires both sides non-null — on a real Library that was 435 of 526 folder items. Those documents reach de-duplication only through the filename+embedding tier. Backfilling is NOT done here: the extracted text is not retained, so the pipeline's hash cannot be reproduced, and any derived value has to be grouped per DOCUMENT (`folder_file_state` / item-state rows) rather than per source — grouping by source gives every file in a folder one identical key, which the sweep then reads as an exact match and collapses. What IS enforced is that every ingest path stamps the column, asserted by test, so the gap cannot grow.

## 2b. The agent's write path (`agent_source.py`)

One path adds documents without the user registering a source by hand, and it is the
agent's: the `knowledge_add_document` MCP tool, gated off by default on
`knowledge.auto_add_documents`. **Nothing registers a file or folder on its own.**

There were two folder-registration paths before — a workspace drop folder
(`autosource.py`) and each worked-in project's documents (`project_docs.py`, filtered by
`doc_filter.py`) — and both are **removed**, along with their config keys
(`knowledge.auto_discover_folder`, `knowledge.auto_discover_dirname`,
`knowledge.auto_register_project_docs`, `knowledge.auto_ingest_chunk_budget`,
`knowledge.max_sources`). Both were opt-in and off by default, but once enabled they
registered directories the user had never named and spent LLM extraction on them with no
confirmation step, which is the property that was removed rather than the default. A
folder enters the Library one way now: the user adds it, and confirms it.

What survives from those paths is generic and still reachable from a hand-added source:
the `confine_to_root` property (a file whose resolved path lands outside the registered
root is skipped — `os.walk` does not descend a directory symlink, but a file symlink IS
followed on open), the `max_files` cap, and `folder_chunk_budget` pacing.

**Agent-added documents (`agent_source.py`).** The `knowledge_add_document` MCP tool
lands documents in one aggregate `agent://` source named "Auto-added", with per-document
groups in `agent_item_state` keyed by a slug derived from the document's `source_uri` and
never from its content, so an edit replaces the group rather than accumulating copies. The
identity must not be the title alone: two unrelated documents are both routinely called
"README", and since a matching key means "same document, replace it", a title-keyed group
lets the second add delete the first document's items. It routes
through `IngestionPipeline.ingest_file` — one ingestion path — and content and title are
redacted before they cross into the store. Adds are serialised by a module lock, because
new items are attributed to a document by diffing the source's item ids around the
ingest.

**Ownership is recorded from inside the ingest, not after it returns.** The items become
durable during `ingest_file`; `ingest_file` therefore takes an `on_committed` callback and
invokes it in its finalize hop, on the success branch, right after the superseded group is
deleted. The ids it hands over are **collected at each `add_item`**, not inferred from a
before/after comparison of the source: `import_bundle` writes into the same aggregate in
its own transaction and under no shared lock, so a comparison would attribute anything it
committed meanwhile to whichever document happened to be ingesting — giving that document
delete authority over knowledge it never created, and destroying it on the next edit. Writing the group
afterwards instead would leave several awaits — the temp-file cleanup, the job-status read
— between the items existing and the record that makes them replaceable, and each is a
cancellation point on the gateway loop. Interrupted there, nothing names the items, and
**both** of the aggregate's duplicate defences read that same row: `get_state` reports no
previous group, so the next add replaces nothing and `find_document_by_hash` cannot see
the content either. The document is then stored twice, and because replacement is what
carries delete authority, an edited re-add leaves the superseded version searchable
permanently. Running inside the hop gives the ownership write the same run-to-completion
guarantee as the delete it belongs with. The `deduped` marker is written by the caller
instead, because the duplicate gate returns before the hop ever runs.

The tool takes the document TEXT and **never opens a file**. A path opened here on
behalf of whatever supplied it is exactly the case where a component can be swapped for a
link to a credential file between the check and the open, and a path pointing at a binary
crashes the decode. Text the agent has already read carries no such window: it was read
through the agent's own file tools, under their approval and audit. Documents that
arrive fetched are text to begin with, and documents in the user's project reach the
Library through a folder source the user registers, which scans through the guarded
folder path.

`source_uri` is an **opaque identity label**, not a read instruction: it is redacted,
capped, stored and hashed, and never opened, resolved, stat-ed or fetched. It is
**required**, because a title does not identify a document. The identity is hashed from
the RAW uri while only the redacted form is stored or audited — redaction is lossy, so two
uris differing only in a same-length credential-shaped segment reduce to the same string,
and hashing that would merge two documents into one group. A caller needing the bytes at
that location reads them itself and passes `content`.

This replaces the never-built server-side doc-link scanner. Rather than Kiro Crew
regex-matching links in chat and fetching them unattended, the agent reads the document
with its own tools under its own approval and hands over text. Kiro Crew fetches nothing,
so `knowledge.doc_ingest_hosts` — whose default is `[]` = deny-all — must NOT gate this
path, or the feature would ingest nothing on a default config while its toggle read on.

**Chunk budget.** `folder_watcher._do_scan` orders discovered files newest-first
unconditionally and stops once a sweep has ingested its budget of chunks. Files not
reached keep (or lack) their `folder_file_state` row, so the next sweep resumes from
them — the existing `status` column already carries the resume point. Every folder
source is budgeted by `knowledge.folder_ingest_chunk_budget` (resolved by
`folder_watcher.folder_chunk_budget`, overridable per source via a `chunk_budget`
property, 0 = unbounded), capped in turn by the watcher's global
`knowledge.sweep_chunk_budget`. A folder still gets ingested in full; the budget only
decides how fast. It applies to the confirm- and resume-triggered scans as well as the sweep,
because the confirm scan is the largest burst — nothing is ingested yet, so every
discovered file is new.

**Cost visibility.** `POST /api/knowledge/sources` walks a folder before ingesting
anything and returns `file_count`, `capped_file_count`, `estimated_chunks`,
`estimated_llm_calls` and `chunk_budget_per_sweep` alongside the
`pending_confirmation` status. The walk uses `folder_watcher.walk_filters`, the same
filter set the sweep applies, so the count describes the files that would actually be
ingested. The chunk figure is derived from the chunker's target size and file bytes
(`_estimated_chunks`), never measured: it exists to show order of magnitude before the
user confirms, and no code path treats it as a bound.

## 3. LLMPool workers (`llm_pool.py`)

Entity extraction (`EntityExtractor`) and internal-URL fetch (`agent_fetch.fetch_url_content`) acquire workers from **workload-isolated** `LLMPool` instances — provider-agnostic, bounded pools of **long-lived** ACP workers (the extraction pool sized by `knowledge.extraction_pool_size`, default `DEFAULT_POOL_SIZE` = 3; the fetch pool has one worker). A `Worker` ABC has two concrete paths:

- **Default (kiro-cli)** — `AcpWorker` drives the `kirocrew-knowledge` agent over ACP (`AGENT_NAME`). That agent is installed by `agent.py:_install_knowledge_agent` (model `claude-haiku-4.5`, kirocrew-core tools only — no internal MCP wiring in the OSS fork).
- **`agent.provider="claude_code"` (legacy seam)** — `CCWorker` drives a long-lived `claude` CLI subprocess over stream-json I/O (haiku model, `bypassPermissions`); URL-fetch tools are opt-in via `KIROCREW_KNOWLEDGE_FETCH_TOOLS`. KiroCrew's provider enum is `["acp"]`, so this branch is dormant in practice.

### Sweep shielding + audit source

`AcpWorker.start()` wires two protections that matter for a long-lived pool worker (`llm_pool.py`):

- **Sweep shielding via `register_protected_pid`** — pool workers are direct `AcpClient` sessions, **not** `SessionMap` sessions or warm-pool providers, so the gateway's periodic orphan sweep cannot see them via `_collect_active_pids` and would SIGKILL a *busy* worker mid-task (surfacing as `ACP process exited (code=1)`). After `ensure_ready()`, `AcpWorker` registers the worker's kiro-cli PID in the sweep-protected set via `register_protected_pid` (`session_pid.py`), and unregisters it in `shutdown()` and on respawn — so the orphan sweep treats it like a live session (the same `register_protected_pid` mechanism the shared `WorkerPool` engine in `acp/worker_pool.py` applies to `ReviewPool`'s `AcpReviewWorker`).
- **`audit_source="subagent"`** — pool workers run tools without passing through `chat_runner` or `SubagentManager`, so their tool calls would otherwise never reach the security audit log. With `audit_source` set, `AcpClient` emits an SEL `log_tool_invocation` record (`source="subagent"`, `outcome="auto_approved"`) per auto-approved tool call; the emit is offloaded to `subprocess_executor()` and bounded by `asyncio.wait_for` so a hung SEL backend never stalls tool dispatch. `None` (chat/subagent clients) never double-logs.

### Sandbox parity

`_get_sandbox_mode()` reads `agent.sandbox` (default `"off"` → defers isolation to kiro-cli's internal agent sandbox; set `"auto"` for standard OS-level confinement). It distinguishes two fallback cases so a config error can never silently disable sandboxing: an **absent** value defaults to `"off"` (the intended default), while a **present but unrecognised** value falls back to `"auto"` (fail-secure) rather than reaching `wrap_argv` as an unknown mode. Knowledge workers honour the same `agent.sandbox` setting as chat/Slack providers (parity), so the default flows through here too.

### Pool mechanics

`LLMPool.start()` reads config once off the event loop and spawns all workers; `acquire()` blocks on a semaphore when all workers are busy and transparently replaces a dead worker (`is_alive()` false) on acquire. `send()` is the acquire→send→release convenience; `send_batch()` runs prompts concurrently bounded by pool size. A failed spawn during `start()` tears down all already-started workers.

**Untrusted-chunk delimiters (CWE-94).** `EntityExtractor` wraps each untrusted chunk in **per-request nonce-suffixed** delimiters (`<<<BEGIN_UNTRUSTED_CHUNK_{nonce}>>>` / `<<<END_UNTRUSTED_CHUNK_{nonce}>>>`, `nonce = uuid4().hex`), so content that embeds a legacy static delimiter cannot forge the boundary and inject instructions. Both `extract` (single) and `extract_batch` (the ingestion path) apply this, and the batch path mints a **distinct nonce per chunk**.

## 4. FTS5 + graph + vector retrieval (`retrieval.py`, `store.py`)

`HybridRetriever.search(query, limit)` runs three legs and fuses them with Reciprocal Rank Fusion.

**Store (`KnowledgeStore`, `store.py`)** — SQLite (prefers `pysqlite3`, falls back to stdlib `sqlite3`), WAL journal, `foreign_keys=ON`. Items live in `items` (with an `embedding BLOB` and `status`), keyword search is served by the FTS5 virtual table `items_fts(title, content, tags, content=items, content_rowid=rowid)`, and the entity graph is an in-memory `SimpleDiGraph` (a minimal networkx-free replacement). The store keeps `items_fts` in sync on every insert/update/delete of the FTS-backed columns.

- **Keyword leg (`_keyword_search`)** — FTS5 `MATCH` over `items_fts`, ordered by `rank`. `_sanitize_fts5_query` double-quotes each token (doubling internal quotes) so user input never contributes FTS5 operators (parameterized quoting), drops `_STOPWORDS`, and OR-joins the remainder so a natural-language query no longer requires every literal token to match. An all-stopword query falls back to the raw tokens rather than dropping the leg.

- **CJK recall (`fts5_segment_for_index` + `fts5_cjk_match_groups`, `_sqlite_compat.py`)** — FTS5's `unicode61` tokenizer classifies CJK ideographs as letters, so it stores an entire spaceless run as ONE token: a whole clause becomes a single term and no query can address a word inside it. Both sides used to tokenize that way and therefore agreed, which is why the recall loss was invisible — the vector leg rescued the result set. The **index copy** of `title`/`content`/`tags` is now written with a boundary around each CJK character, so the tokenizer emits one term per character; a **query** run expands to its overlapping adjacent-character pairs as FTS5 phrases, OR-ed. A phrase over per-character terms is exact substring matching, so a four-character query for "memory leak" matches both a document spelling the run verbatim and one spelling "memory" and "leak" apart, while a document that merely reuses those characters in other words ("internal", "to save", "relief valve", "water leak") is excluded — the same adjacency floor the session search applies (`history.md`). Non-CJK input produces byte-identical text and the byte-identical expression `fts5_quote_tokens` produced, so Latin matching is unchanged; a token mixing Latin and CJK ANDs its runs. Hangul is deliberately excluded, because modern Korean is space-separated.
  - Only the index copy is transformed. `items_fts` is an external-content table (`content=items`), so `snippet()`/`highlight()` and every read of the item still see the original text.
  - **Every** writer must route through `KnowledgeStore._fts_index` / `_fts_unindex`, which write in the representation the database currently declares (`_fts_terms_segmented`, read from `user_version`). FTS5's `'delete'` command subtracts the exact terms it is handed, so the wrong representation either leaves the old terms in place — serving deleted or superseded content as live hits, which `'integrity-check'` does **not** flag — or raises `database disk image is malformed` outright. Unconditional segmentation is not available because a legacy database has writers that run before any reader can migrate it: the orphan reclaim in `_migrate` (inside the constructor) and the startup watcher sweep. The declaration is latched only in the True direction, since `user_version` only increases and another process on the same database may migrate it at any time — a cached False would keep writing raw terms into a migrated index.
  - **The declaration is serialized by SQLite's writer lock, not a Python one.** Every FTS-touching transaction is `BEGIN IMMEDIATE`, so a reader of the declaration already excludes the rebuild that can change it — across processes as well as threads, which a Python lock cannot do. The FTS write path therefore takes **no** Python lock: taking one there inverts against SQLite's, because a writer holding SQLite's lock would wait on Python's while the rebuilding reader holds Python's and waits on SQLite's — a deadlock that resolves only when `busy_timeout` expires, returning 500 from an event-loop write. The surviving `_fts_lock` guards the rebuild alone (so two reader threads in one process do not both start one) and is always acquired *before* SQLite's, never after.
  - There are exactly **three** FTS readers, and each calls `ensure_fts_index_current` first: `KnowledgeStore.search_items_fts`, `HybridRetriever._keyword_search`, and the dashboard's entity-items lookup (`_entity_items_rows`). The last builds its own query and matches the name as ONE phrase over the segmented text, which is what an entity name is — a contiguous string, not a bag of words. For a name with no CJK that is byte-identical to quoting the name directly, so a multi-word ASCII entity (`New York`) still requires those words adjacent; for a CJK name the segmentation is what lets the phrase address the characters the index stores. It runs under `asyncio.to_thread`, like the other knowledge readers in that module.
  - The term representation is versioned by `FTS_INDEX_VERSION` and `PRAGMA user_version`, not by a schema probe: the `CREATE VIRTUAL TABLE` text is identical before and after, so nothing in the schema records which representation a database holds. `_migrate_fts_index` rebuilds in batches of `_FTS_REBUILD_BATCH` and bumps the marker only after the whole rebuild commits, so an interrupted rebuild restarts on the next open rather than resting half-built.
  - **The migration is one-way, and downgrading needs the index rebuilt.** A build from before this change knows nothing about `user_version`, so it writes and deletes raw terms against an already-segmented index — which by the rule above either leaves stale terms behind or raises `database disk image is malformed`, and the old code has no rebuild path to recover. Rolling back past this change therefore requires dropping and recreating `items_fts` (or deleting `knowledge.db`, which is a derived cache of ingested sources). Forward upgrades need nothing: the first search migrates.
  - **The rebuild is triggered by readers, never by the constructor.** `KnowledgeStore.__init__` runs on the event-loop thread (`setup_knowledge_routes` reads the lazy `state.knowledge_store` property during dashboard startup), while its FTS readers run on worker threads (`run_in_embed_pool` / `asyncio.to_thread`) — the store's own threading contract, and why it hands each thread its own connection. A data-scaled reindex in `__init__` would therefore stall the gateway at boot for the length of a full reindex: measured at 4,000 CJK items it is ~137 ms of work, and it grows linearly with the corpus. `ensure_fts_index_current` moves that cost onto the first search instead; it is lock-guarded so concurrent readers wait rather than each starting a rebuild, and steady state is a single boolean check (~1 us).
  - **Known limitation:** `add_item` persists tags with `json.dumps` at its default `ensure_ascii=True`, so a CJK tag is stored with its characters backslash-escaped and reaches the index as terms like `u6a21`/`u578b`. A CJK tag is unsearchable for that reason, which no query-side change can reach.
- **Graph leg (`_graph_search`)** — resolves query words and adjacent word-pairs to entities, expands via graph neighbors (depth 2), and ranks items by mention count. A spaceless CJK run is one whitespace word but several entity names, so an entity named for a word *inside* the run is unreachable by the split alone; `_cjk_subruns` adds the run's own contiguous CJK substrings as extra candidates, longest first (so the most specific entity name is tried before a shorter prefix of it) and bounded by `_CJK_SUBRUN_MAX_LEN` / `_CJK_SUBRUN_MAX_CANDIDATES`, since each candidate costs a `find_entity` query. This is the second of the three whitespace-splitting sites issue #3691 enumerates; a query with no CJK gets exactly the candidate list it did before.
- **Vector leg (`_vector_search`)** — brute-force cosine over `items` with `embedding IS NOT NULL AND status='active'`; returns `None` when no embedder is wired. Items whose stored embedding dimension differs from the query vector are **skipped** (not scored 0.0), with a single per-search WARNING so a model swap / stale index surfaces as "re-index needed". `_cosine_similarity` returns 0.0 for differing-length or zero vectors.
- **RRF fusion (`_rrf_fuse`, k=60)** — per-leg weights align positionally with `(keyword, graph, vector) = (1.0, 1.0, VECTOR_RRF_WEIGHT=2.0)` so semantically-strong matches dominate when the keyword leg returns literal junk. Results are tie-broken by recency (`updated_at`), and each result's `match_type` records which legs it appeared in (`keyword+graph+vector`).

**Citation enrichment** — `_attach_source_locations` batch-fetches `source_locations` (adds `section_title`, `chunk_range`, `anchor`); `_attach_citation_sources` adds `source_type`/`source_name`/`source_uri` plus the most specific per-document locator: `file_path` for folder/vault sources (from `folder_file_state`), `artifact_slug`/`artifact_name` for the aggregate artifact source (deep-links `/artifacts/<slug>`). Missing/unmapped sources degrade cleanly (extra keys simply absent).

### On-loop connection guard (`on_loop_db.py`)

`KnowledgeStore.db` is the single accessor every query in the store funnels through, so it carries the runtime half of the off-loop discipline (#7078, remedy A of #3057). `OnLoopDBGuard.check()` runs at the top of the property, before the cached-connection fast path — a second query on an already-open connection must not escape it:

- **Off the loop** (worker thread, `executors.py` lane, CLI, cron, subagent) — no-op. This is the sanctioned path.
- **On the loop, strict** — raises `OnLoopStoreError`, so an un-offloaded call-site fails a test instead of shipping.
- **On the loop, otherwise** — a throttled WARNING with `stack_info` (once per 60s per guard) and the call proceeds. Production deliberately does not raise: that would convert a slow query into a failed request.

**Construction is the one vetted on-loop take** (#8231). `setup_knowledge_routes()` reads the gateway's lazy `knowledge_store` property at route registration, which `start_dashboard` runs before the socket binds — so `__init__` (schema init, migrations, graph load) runs on the loop on every launch, by the constructor's documented design. The constructor wraps exactly those calls in `OnLoopDBGuard.allow_on_loop()`, a `ContextVar`-scoped opt-out (mirroring `history.allow_on_loop_persist`) that ends with the `with` block: the six non-constructor `_load_graph()` call sites and every query path stay fully guarded, and `test_knowledge_store_onloop_db.py::TestConstructionOnLoopIsSanctioned` pins both directions. Sanctioned is not the same as free: `_migrate()` runs an unconditional writer-locked orphan sweep and `_load_graph()` full-scans `entities`/`entity_relations` (only the FTS rebuild is deferred to the first off-loop reader), so on a large knowledge profile that boot can still stall the loop — moving that work off the boot path is #8329; this opt-out only removes the spurious diagnostic for the take that is deliberate.

This complements `scripts/check_sync_io_in_async.py` rather than duplicating it. The gate rejects a blocking call written *lexically* inside an `async def`; it cannot see an `async def` that reaches the store through a plain synchronous helper one frame down (`store.get_item(...)`), and no name-based AST scan can without whole-program type inference. The guard fires for every caller regardless of stack depth. `test_knowledge_store_onloop_db.py::test_static_gate_is_blind_to_that_same_call` asserts the blind spot against the real gate, so if a future gate learns to see the interprocedural form the overlap gets re-judged deliberately instead of silently.

**Strictness** comes from `strict_enabled()`, the single parser of the truthy/falsy rules, which takes the env var name as a parameter — because "strict" means "this surface is fully offloaded, so enforce it", a property of a *surface* rather than of the process. Two surfaces can sit at different stages of the same cleanup, so each gets its own switch:

| Surface | Switch | Dev mode arms strict? |
|---|---|---|
| `history.py` session mutations | `KIROCREW_STRICT_ON_LOOP_PERSIST` | yes |
| auto_research campaigns DB | `KIROCREW_STRICT_ON_LOOP_PERSIST` (shared default) | yes |
| knowledge store | `KIROCREW_STRICT_ON_LOOP_STORE` | **no** |

The knowledge store's two narrowings exist for one reason and are both temporary: it still carries the 85 recorded on-loop callers in `.github/sync-io-in-async-baseline.txt` that #7019 owns. The separate switch is load-bearing because `KIROCREW_STRICT_ON_LOOP_PERSIST` is **already exported** into the e2e gateway by `setup.py`'s `test_e2e` and `.github/workflows/ci.yml`, scoped when written to history's clean surface — on that switch the on-loop `/api/knowledge/stats` and `/api/knowledge/namespaces` handlers would raise and 500 the e2e run. Excluding the dev-mode arm matters for the same backlog: raising on tracked work reports it as a regression, and the developer's rational response (unsetting `KIROCREW_DEV_MODE`) would silence `history.py`'s guard too. When #7019 empties that baseline, both arguments go and this store joins the shared switch — `test_knowledge_store_onloop_db.py::TestSharedSwitchCannotArmThisStore` asserts the CI export against the real workflow file, so that flip has to be deliberate.

### `local_knowledge_search` MCP tool (`mcp_core.py`)

The LLM reaches retrieval through the `kirocrew-core` MCP tool `local_knowledge_search`:
- DB path: `config_dir()/workspace/knowledge/knowledge.db`; a missing DB returns "Knowledge Library is not configured…" (SEL `not_configured`).
- `_get_knowledge_search` caches the `(KnowledgeStore, embedder)` pair across calls and rebuilds only when the knowledge DB (or its `-wal`) or `config.json` changes — avoiding the per-call schema DDL / migrate / graph-load and the Ollama availability probe.
- Default `limit` is 3; results below `min_score = 0.012` are dropped. Output is run through `redact_exfiltration_urls()` + `redact_credentials()` before returning, and every call emits an SEL audit event (`success` / `no_results` / `not_configured` / `unknown_source`). Input is validated against `LOCAL_KNOWLEDGE_SEARCH_SCHEMA` (`validation.py`).
- Optional `source_id` scopes the SEED legs only (FTS5 keyword + vector similarity, via parameterized WHERE clauses in `HybridRetriever`); the graph leg stays unfiltered so cross-source entity connections still contribute traversal context. Scope membership is ownership OR location — `items.source_id` or a `source_locations` row, so an item surviving a cross-source dedup collapse still belongs to the losing source's scope (the same rule as `/api/knowledge/graph`'s filter). Omitting it keeps the unscoped behavior. A nonexistent id returns a guidance message naming `knowledge_list_sources` (SEL `unknown_source`), not an exception.
- The companion tool `knowledge_list_sources` (no arguments; `KNOWLEDGE_LIST_SOURCES_SCHEMA`) returns one `name — id (N item(s))` line per source, counting **active** items only (superseded/deduped copies would overstate a source's coverage) — so agents discover valid `source_id` values instead of guessing.
- **The response is written through a private stdout descriptor, not fd 1.** The first search's availability probe (`InProcessEmbedder.is_available` → `embed`) kicks the background GGUF load, and the vendored llama-cpp wraps that load in `suppress_stdout_stderr`, which `dup2`s **fd 1 process-wide to `/dev/null`** for the duration (~0.7s) *and* rebinds the `sys.stdout` object. Because the probe returns `None` immediately, the search answers keyword-only in milliseconds — so its JSON-RPC response raced that window and was silently destroyed: no exception, no short write, SEL still logging `success`, and the client hanging until the ACP tool-stall watchdog (`acp/client.py::_TOOL_STALL_TIMEOUT`, 600s) killed the turn. `mcp_shared.run_mcp_stdio_loop` now takes an `os.dup(1)` snapshot (`snapshot_stdout_fd`) at server startup before any tool can run, and `respond()` writes through it under a lock, so responses (and `ping` / `tools/list` replies, which were equally exposed) always reach the client. Falls back to `sys.stdout` when stdout is not fd-backed. Note that "has `sys.stdout` been swapped?" is *not* a usable guard — the suppressor swaps the object too, so it reads as swapped exactly inside the window that must be survived.

The dashboard Knowledge tab uses the same store via a lazily-initialized `KnowledgeStore` on `DashboardState` (`dashboard/state.py`).

## 5. Source-scoped list API (`dashboard/handlers/knowledge.py`)

The dashboard Knowledge list view is **source-first**: it renders one collapsed
row per source and pages *within* a source, rather than paging all items globally
and grouping whatever landed on the page. Two pieces of API surface support this.

### `GET /api/knowledge/items?source_id=<id>`

Scopes the page to a single source. Composes with the existing `type`, `status`,
`namespace`, `q`, `page`, and `limit` params.

- The reported `total` is **scoped to that source**, not the global count. The
  in-group pager derives its page count from it, so a global total would break
  the pager math.
- `source_id=__none__` selects items with no source (`source_id` NULL or empty).
- Applied in both branches of `list_items`: as a SQL predicate in the list
  branch, and via `_matches_source` after ranking in the hybrid-search branch.
- Because the hybrid-search branch filters *after* the retriever has ranked
  globally, a scoped search escalates its candidate pool
  (`_search_until_exhausted`: `_SCOPED_SEARCH_START` doubling to
  `_SCOPED_SEARCH_MAX`) until the retriever short-reads, so the scoped total is
  exact rather than truncated by a fixed window. At the cap the total may
  understate. `HybridRetriever.search` now accepts `source_id` (seed-scoped —
  see §4); adopting it in this branch is the remaining follow-up. Unscoped
  searches keep the cheap `limit * 3` window.

### `GET /api/knowledge/source-counts`

Returns the item count per source **under the active filters**:

```json
{ "counts": { "<source_id>": 42, "__none__": 3 }, "total": 45 }
```

- Accepts `type`, `status`, and `namespace`; the counts reflect them, which is
  why the list view uses this rather than `/sources.item_count` (a source's
  unfiltered, all-namespace total that would over-report when filtered).
- Sourceless items are reported under the `__none__` key.
- The list view derives its rows from these counts, which is what guarantees
  every source is visible at once regardless of relative size.

## Invariants

- **`sources.properties` / `entities.aliases` well-formedness is enforced at the writer** — `store.import_bundle()` validates that any present value is UTF-8-encodable JSON text parsing to an object / array of strings (absent/`null` falls back to the schema defaults `'{}'`/`'[]'`), raising `KnowledgeBundleError` before the INSERT. The dashboard import handler is the store's only production caller today; enforcing at the writer makes any future caller (MCP tool, CLI import, app backend) safe by construction. Several readers parse the raw column with `json.loads()` and no shape guard (source detail handlers index the parsed dict; `find_entity()` calls `.lower()` on each parsed alias), so a corrupt committed row would crash a later, unrelated read. The dashboard import handler maps the typed error to a 400 (`code: malformed_knowledge_bundle`).
- **Sensitive paths never ingested** — `FileReader.read`, `FolderWatcher._walk`, `_hash_file`, and `_ingest_file` all gate on `is_sensitive_path()` (with symlink re-resolution at ingest time for TOCTOU).
- **`.org` and unknown-but-supported extensions are plain text** — only `_DISPATCH` extensions get specialized readers; everything else in `SUPPORTED` flows through `_read_text` → generic chunker.
- **Pool workers are long-lived and must be sweep-shielded** — any direct `AcpClient` worker that outlives a chat turn (not tracked in `SessionMap`/warm pool) must register its PID via `register_protected_pid`, or the orphan sweep will kill it mid-task.
- **LLM-derived text is redacted before storage and before return** — ingestion redacts extracted text (`ingestion._redact`), and `local_knowledge_search` redacts its assembled output.
- **FTS query input is parameterized** — user query tokens are always double-quoted literals; the user never injects FTS5 operators.
- **Embedding-dimension mismatches are skipped, not scored** — vector search excludes incomparable-dimension items so a model swap cannot fill the top-K with all-zero ghosts.
- **Taking `store.db` on the event loop is a diagnosable event, not a silent one** — the accessor is guarded (`on_loop_db.OnLoopDBGuard`): strict raises `OnLoopStoreError`, production logs a throttled WARNING with a stack and proceeds. This is what covers callers the lexical `check_sync_io_in_async` gate cannot see, so the two must both stay — neither alone closes #3057.
- **The self-heal rebuild path never touches SQLite on the event loop** — `_maybe_reembed_stale`'s stale COUNT, `rebuild_embeddings`' total COUNT / page SELECTs / batch progress commits, and the success-path job finalize all run via `asyncio.to_thread` (`store.db` is a per-thread connection, so each worker thread uses its own connection to the same WAL db). On a large KB (observed: ~1.3GB after an embedder-sig change) an inline COUNT can stall past the 25s loop-watchdog threshold and crash-loop the gateway. The one deliberate exception is the CancelledError finalize in `_run_reembed_job`, which stays inline so cancellation cannot pre-empt the single-flight finalize. When the stale count exceeds `_LARGE_REBUILD_WARN_THRESHOLD` the watcher logs a prominent WARNING before starting the full re-embed.
- **`__none__` is a shared wire contract** — the no-source sentinel is defined as `_NO_SOURCE` in `dashboard/handlers/knowledge.py` and mirrored as `NO_SOURCE` in `website/src/pages/knowledge/SourceGroup.tsx`. Both sides must change together; it is effectively un-renameable once shipped.
- **A source-scoped `total` is scoped, never global** — `/items?source_id=` reports the count for that source alone, because the per-source pager computes its page count from it.
- **Per-source badge counts are filter-aware** — list-view badges come from `/source-counts` (which honours `type`/`status`/`namespace`), not from `/sources.item_count`, so a badge never disagrees with the group's contents under a filter.
- **The search branch's candidate load runs off the event loop** — a scoped search escalates its candidate pool, so `_load_items_by_id` (batch `SELECT` plus per-row serialization) and the `source_counts` aggregate both run via `asyncio.to_thread`. `store.db` is a per-thread connection, so each worker thread uses its own. Run inline, either can stall the loop past the watchdog threshold on a large KB.
- **Frontend selection is bounded to on-screen items** — in source-first mode item data lives in per-`SourceGroup` caches, so bulk actions read the items each expanded group reports as rendered, and selected IDs are pruned when a group collapses or pages away. Reading the react-query cache directly would let a bulk Delete reach a retained cache for a source the user can no longer see.
- **Per-source caches are keyed under the `knowledge-items` prefix** — `['knowledge-items', 'source-items', ...]` and `['knowledge-items', 'source-counts', ...]` so every existing `invalidateQueries(['knowledge-items'])` call site reaches them. Consequently any `setQueriesData` on that prefix must guard on the payload shape, since the counts entry has no `items` array.
