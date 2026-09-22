# Memory, Skills & Hooks Modules

## Overview

Memory V2 is a development-stage replacement with one stable `member_id` mapped
to one stable `store_id` and one managed SQLite database. Facts, learned rules,
episodes, learned history, source spans, revisions, full-text search, embeddings
and vector validity belong to that database. Manual persona, rules, briefing,
preference/project anchors and project guides remain owner-managed documents.
Global V1 and legacy named V1 retain their existing files, algorithms and
retention policy; startup admission is bounded as described below.

The frozen `ExecutionContext(member_id, store, selection_kind, template_id,
memory_mode, app)` in the owning session, run or scheduled-job record is the
routing authority. Admission resolves it once before awaiting preparation.
Display names, provider templates and project paths cannot select a store.
New member IDs exclude both live member IDs and retained store owner IDs, so
deleting and recreating a name cannot reinterpret an earlier execution's identity.
Manual context resolves captured member IDs strictly; only explicitly named member
inputs resolve configured aliases, never as a fallback for a missing captured ID.
Cross-member `target_member` dispatch follows existing tool, delegation and app
permissions; omitted targets inherit the parent, and continuations retain their
saved context. A provider conversation requires a new session to change member.

Member memory is not an adversarial same-host confidentiality boundary. Prompt
instructions and filesystem allow/deny policy reduce mistakes; managed database,
WAL and SHM files cannot be edited through agent file tools, and glob omits managed
state. There is no separate memory binding registry, session/run grant, PID proof,
HMAC proof, hidden filesystem view or private-memory platform refusal. Ordinary
transport authentication, owner/app authorization, credentials, SEL integrity,
mandatory enterprise policy and host sandbox controls still apply.

Startup admission keeps stable preferences and applicable rules in the first
turn while earlier activity (daily history, project notebooks, old-task facts and
episodes) is retrieved explicitly through `memory_recall`, as specified below.
This applies to Global V1 and named V1 without converting them into member stores.

Built-in named-store writes run gateway-side. The ordinary Linux/macOS sandbox
therefore exposes `memory_stores/` read-only, while allowing cross-member reads.
For Linux's directory mount, an absent root is created empty without provisioning
a database or changing member configuration. Existing named V1 root links remain
supported and Global V1 paths are untouched. This is an integrity rule only where
the ordinary sandbox is active, not a memory confidentiality or universal write
guarantee; sandbox-off code, external host tools and pre-existing writable aliases
remain outside it. No per-member sandbox machinery is introduced.

Manual member essentials resolve directly from canonical member identity without
opening SQLite. The optional learned-rule block can report member-memory
unavailable while manual essentials remain usable. Incognito allows reads and
blocks all learned writes; Temporary neither reads nor writes memory, including
lessons. Ordinary V2 recall has no persistent side effects: it does not touch
access timestamps, reconcile metadata, rebuild indexes or repair model signatures.
Explicit maintenance owns derived-vector repair. Validity and exact content/model
checks remain necessary before publishing a vector.

`create_member_database(path, member_id=..., store_id=...)` exclusively provisions
a new database. `open_member_database` uses SQLite `mode=rw`, validates the stored
identity, format and required schema, and never creates or migrates a database.
`read_member_database_identity` inspects an existing database read-only. Missing,
corrupt, mismatched and unsupported files produce an error and retain their bytes.
Admission normalizes missing or corrupt database errors from the selected SQLite
driver to member-memory unavailability, without falling back to Global memory.
Admission uses the identity reader's selected driver, including its stdlib fallback
when a bundled `pysqlite3` package is incomplete.
The identity check is an integrity check of canonical admission, not another
authorization system. This format has no old-V2 compatibility, migration or
feature rollback framework. Real existing data is preserved. Routine online
backup, staged restore, startup recovery and live-handle coordination remain.

Opening V1 retains its established additive metadata reconciliation and latest-20
accepted revision retention. V2 retains all fact/rule/episode revisions and learned
daily history, without duplicating the full daily body on each history edit.
Consolidation publishes its accepted records, proposals, history, FTS projection
and durable source-span receipt in one SQLite transaction. A retry after a lost
transcript acknowledgement reads that receipt and acknowledges only the committed
span, even if later messages have arrived. Receipts retain the original total,
message count and canonical-content SHA256 rather than another transcript copy;
an edited or shortened prefix refuses acknowledgement. Receipts are not pruned.

### Canonical routing and storage module map

- `execution_context.py` owns frozen `ExecutionContext` and `MemoryStoreRef`,
  decoding the owning record, member ID lookup, session capture and inherited
  mode tightening. It carries routing data; ordinary auth remains independent.
- `memory_stores.py` resolves configured store paths and explicit member creation.
  `members.py` owns persisted member IDs and manual member documents.
- `memory_schema.py` defines the authoritative member tables. `vector_memory.py`
  owns exclusive creation, strict open, learned reads/writes, FTS and vectors;
  `memory_record_metadata.py` maintains revisions, proposals and provenance.
- `memory.py` is the facade for manual documents and learned history. V2 learned
  operations delegate to its admitted SQLite handle; V1 keeps its file layout.
- `history_consolidation.py` publishes a member span atomically and recovers its
  receipt. `context.py` and `member_essential_context.py` assemble manual context
  from the captured member, with an optional SQLite learned-rule component.
- `member_memory_backup.py` and `memory_startup.py` coordinate ordinary backup,
  staged restoration and connection lifetime without a second identity authority.

### V1 accepted-revision retention

V1 automatically retains the latest 20 accepted snapshots per record, ordered
by monotonic revision-row ID, in the same transaction as each accepted write.
The bound uses per-record ordered `LIMIT` queries rather than window functions,
and metadata reconciliation uses an update followed by a plain insert rather
than the newer UPSERT form. Neither form is newly required to open V1;
other store operations retain their existing SQLite requirements.
Opening an existing V1 journal reconciles its latest accepted snapshot before
applying this bound in that transaction. The cap applies to both Global and
named V1 stores, including named stores with the newer physical schema; it does
not change live memory, retrieval, current metadata, revision counters, CAS or
any pending, rejected or other non-accepted proposal. V2 skips this automatic
removal entirely. No injection audit, memory event or SEL chain is pruned.
This bounds accepted editor history per record, not total storage: record and
proposal counts can grow, deleted SQLite pages are reusable, and no automatic
`VACUUM` shrinks the database file. Historical scans can no longer inspect
automatically removed snapshots; export needed evidence before upgrading.

There is no command that removes accepted snapshots; the cap above is the only
history pruning, and it is automatic. The SEL has its own existing retention and
integrity-chain rules. The V1 accepted-history cap does not bound total storage, and
V2 has no automatic history retention limit.

### The six memory layers

V1 has six distinct storage layers, each with its own store and write path. V2
unifies learned layers in SQLite. Fresh V1 context includes complete stable
preferences, a short activity index and applicable lessons; daily history,
project notebooks and old-task facts/episodes stay behind explicit
`memory_recall`. Warm follow-ups retain native conversation history without
repeating startup injection. V2 session context reads essential anchors and
query-free scoped lessons; its semantic and episodic fragments require an
explicit `memory_recall` operation. The nesting below is source-of-truth
ordering (a later layer can override an earlier one), not a storage hierarchy
and not everything sent on each turn:

```
Memory storage layers (not a model-input or token budget)

  Preferences            Projects            Recent history
  (preferences.md)       (projects.md)       (history/{date}.md)
  V1: consolidator-      V1: consolidator-   V1: multi-tier decay
      replaced              replaced        V1 daily files only
  V2: manual anchors    V2: manual anchors
        |                     |                     |
        +---------------------+---------------------+
                              |
    Semantic memory (SQLite key-value)
    pref.* / project.* / user.* keys, confidence-gated writes
                              |
    Episodic memory (past conversation fragments)
    FAISS (or stdlib) vector search + MMR (time decay only in V1)
                              |
    Lessons (learned corrections)
    lesson.* keys at confidence 1.0, user-explicit always wins
```

In V1, layers 1 to 3 are Markdown files and layers 4 to 6 are database rows;
its existing JSONL lesson fallback remains. In V2 only manual preference/project
anchors remain files. Learned history, facts, episodes and lessons share one
SQLite authority with no JSONL fallback. Global V1 and each configured named
store are separate destinations selected by the owning execution context.
See [Memory across surfaces and channels](#memory-across-surfaces-and-channels).

## Memory (`memory.py`)

Global V1 uses structured files under `~/.kiro/crew/workspace/memory/`:
- `preferences.md` — learned user preferences (V1 legacy consolidation may replace the file; V2 is owner-managed)
- `projects.md` — active project context (V1 legacy consolidation may replace the file; V2 is owner-managed). Its `# Active Projects` header contract is owned by `memory.normalize_projects_document(content, *, today=)`, which `MemoryStore.write_projects`, `MemoryStore.write_private_profile_validated` and the dashboard's `_validate_private_profile_update` all call before writing. The three used to carry their own copy, and the dashboard one lives in a different package from the two store ones, so a change to either pair could not see the other. `today` is a parameter rather than read inside, so each write keeps its own single clock read. The two branches trim ASYMMETRICALLY, and that is the shipped contract rather than an oversight: an already-headed document is written as `content.strip() + "\n"`, while an unheaded one wraps the RAW content, so surrounding whitespace survives in exactly one of the two branches.
- `history/{date}.md` — V1 daily conversation summaries. V2 stores current daily content in `memory_history` in its database.

V2 `MemoryStore` is a facade over an attached, prepared `VectorMemoryStore` for
learned history and search. Its initialization creates no files. Manual profiles
use the existing guarded filesystem reader and owner write path; they are not
copied into the learned search index. A missing database is an explicit service
failure, never an empty learned store or a JSONL fallback.

### A store's three paths, and where the index actually lives

The separate Markdown/history and `memory_index.db` layout below is the V1 contract. V2 has `memory.db` and optional manual profiles only; `memory_fts` lives in that database.

A memory store's markdown tree, vector file and FTS index are three separate on-disk
paths, and which one a store name resolves to is owned by `memory_stores.py` — see
[config](config.md#named-memory-stores-memory_storespy) for the resolvers, the
store-name shape rule and canonical member/store identity validation.

| Path | Holds | `"default"` |
|---|---|---|
| markdown root (`memory_store_dir_for`) | `memory/preferences.md`, `memory/projects.md`, `memory/history/*.md` | `~/.kiro/crew/workspace/` |
| vector file (`resolve_store_path`) | semantic, episodic and lesson rows | `~/.kiro/crew/memory.db` |
| FTS index (`memory_index_path_for`) | the FTS5 virtual table | `~/.kiro/crew/memory_index.db` |

**Two roots, not one, and conflating them is the sharpest hazard here.** The markdown
root for `"default"` is `memory.workspace_dir()` = `config_dir()/"workspace"`, NOT the
data home: answering with the data home would take every existing install's
`preferences.md` out from under both the consolidator and `kirocrew memory search`.
The vector file for `"default"` is `config_dir()/"memory.db"`, byte-exact with what
`VectorMemoryStore()` already defaults to. Both default answers are the paths already
on disk, which is what makes the default path a rename-free no-op.

**The default store's index does not sit inside the markdown tree it describes**, and
that is deliberate rather than tidy: `~/.kiro/crew/memory_index.db` is the spelling
the off-store consumers hold — the snapshot `memory` component, `portability`'s
export/import zip, `scripts/sync-to-remote.sh` — so relocating it drops the index
from every backup while a restore writes a copy nothing reads. A NAMED store's index
does live beside its own markdown, which is what makes the index per-store and puts
it behind the `memory_stores/` fence. The snapshot `memory` component and
`portability`'s export both carry the whole `memory_stores/` tree (see
[Named stores and backup paths](#named-stores-and-backup-paths)), so a
named store's index rides beside its markdown; `scripts/sync-to-remote.sh` still
names only the root paths.

### Named stores and backup paths

Named V1 keeps its existing Markdown, JSONL and separate derived FTS layout.
V2 member recovery uses `member_memory_backup` and the managed database plus
manual preference/project anchors. Derived FAISS files may be rebuilt from
SQLite and are not another learned authority. The database's stable member and
store identity is checked before snapshot creation, staging and activation.

SQLite online backup provides a transaction-consistent snapshot including
committed WAL content. A raw copy of `memory.db` is insufficient while its WAL is
live. Namespace admission and store-lifetime coordination prevent a directory
replacement while a writer retains a handle. Startup recovery runs before memory
consumers open the selected generation. Failures retain the old data and report
unavailability; no read creates a replacement database.

`MemoryStore` keeps Global V1 index placement unchanged:
`MemoryStore()` uses `<home>/memory_index.db`, while an explicitly supplied
workspace retains its existing workspace-relative path. V1 FTS supports literal
quoted-token search, incremental file indexing and explicit/periodic rebuilds.
Its corrupt derived index can be rebuilt from the source files. V2 FTS belongs
to its authoritative database and has no delete-and-recreate recovery path.

### Knowledge library duplicate ownership

Folder ingestion tracks two identities for each file: `content_hash` is the hash
of the file's raw bytes, while `text_hash` is the hash of the text extracted by
the reader and stored on knowledge items. They are equal for plain text but not
for transformed formats such as PDF, DOCX, and HTML. The pre-ingest duplicate
gate passes its exact extracted-text hash to the caller's in-transaction
`on_duplicate` finalizer. `FolderWatcher` stores that value on the deduped state
row before the gate commits, so a later source deletion can reassign and adopt
the surviving item into the correct file row. Deriving the value only from a
byte-identical sibling is a fallback for older direct state writes, not the
ingestion contract.

### History reads (`read_recent_history`)

V1 history context uses natural decay: recent days in full detail, older days
progressively compressed. `_read_recent_history_uncached` walks a fixed 181-day
window (`range(181)`) and picks a rendering per day by age.

| Age | What is kept | Why |
|-----|--------------|-----|
| 0–13 days (`i < days`, `days=14`) | Full entries with timestamps | Recent work needs full context |
| 14–60 days (`i < 61`) | Day header + first entry + `…N more entries` | Enough to jog memory at a fraction of the chars |
| 61–180 days | Date + `#### ` count only | Existence marker: "something happened then" |
| 181–364 days | Not read into context | Still on disk as a backup |
| 365+ days | Deleted from disk by heartbeat prune | Too old to be worth the scan |

V2 history reads select full daily content from `memory_history`, bounded to the
newest 366 stored daily entries and 8 MiB per response, with no age cutoff or
stored-content decay. The facade returns structured entries with logical
`history:YYYY-MM-DD` paths. SQLite serialization and compare-and-swap protect daily
edits; edits replace that day's body rather than retaining duplicate full copies.
SQLite checks each day's byte length before returning its body to Python. A day
larger than 8 MiB is refused without truncating or deleting it; editable-history
GET and PUT retain the existing `store_unavailable` (503) error response.
Ordinary V2 message context does not scan learned history.

`MemoryStore.get_context()` retains `history_cap=25_000` as its default for
programmatic readers. V1 `ContextBuilder` calls it with the scaled history cap
when building fresh session context. V2 reads preference/project anchors without
this history scan. Timestamps use local timezone.

V1 session context and explicit readers invoke `read_recent_history`; V2 prompt
construction does not. A V1 read stats and reads up to 181 daily files synchronously;
V2 uses the bounded full-entry snapshot described above. The V1 assembled string is
TTL-cached (`_HISTORY_CACHE_TTL_SECS = 5.0`) on the `MemoryStore` instance,
keyed on `(days, today)` so the decay window shifting at midnight invalidates
naturally; `append_history` and `prune_history` call `_invalidate_history_cache()`
so a new or pruned entry is visible immediately.

### History Pruning

For V1, `prune_history(keep_days)` deletes daily files older than `keep_days` (default 365). It runs once per day via heartbeat (`_PRUNE_TICKS = 1440`), parses `YYYY-MM-DD.md` filenames and skips non-date files. For V2 it returns zero without deleting anything, regardless of the age setting.

V2 preference/project reads preserve the guarded manual-file reader. A refused
source remains an error, not an empty document. V2 FTS rebuild is an explicit
transaction over current database facts, rules, episodes and history; it reads no
learning sidecars and creates no separate database. Every normal content mutation
updates its FTS projection in the same transaction. Recall checks current record
status and validity before returning matches. V1 keeps its file reader and
self-healing derived-index behavior.

### Consolidation (`history.py` `HistoryConsolidator`)

For V2, `VectorMemoryStore.apply_consolidation` is the publication boundary.
The original transcript snapshot determines a stable source span; the caller
revalidates it after extraction. One immediate SQLite transaction publishes facts,
correction proposals, episodes, lessons, history revisions, FTS and its receipt.
Storage failure rolls back the whole pass. Verified corrections compare their
pre-extraction record revision in that transaction. Embeddings are deferred for
maintenance and cannot hold the write lock during provider inference. Before a
retry calls a provider, a committed receipt recovers a lost transcript progress
acknowledgement and leaves later appended messages pending.

The following file-oriented flow and independent writes describe V1.

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
    |                             - preferences.md  (V1 legacy replace only)
    |                             - projects.md     (V1 legacy replace only)
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
| Preferences/projects | 30 messages (per session, `_CONSOLIDATION_THRESHOLD`) | Semantic entries; V1 legacy mode also updates `preferences.md` and `projects.md` | In-memory `_prefs_offset` dict |
| Daily history + lessons | 3h idle (per session, `history_idle_hours` = 3.0) | V1: daily Markdown and vector/JSONL lessons. V2: history, episodes and lessons in one SQLite transaction | Transcript `last_consolidated`; V2 also retains an atomic source-span receipt |

Per-consolidation extraction caps (`vector_memory_constants.py`, also
interpolated into the LLM prompt so the model is told the same numbers):
`_MAX_SEMANTIC_PER_CONSOLIDATION = 20`, `_MAX_EPISODIC_PER_CONSOLIDATION = 10`,
`_MAX_LESSONS_PER_CONSOLIDATION = 10`. The lessons cap exists because each
`write_lesson()` can perform up to 6 blocking embeds (1 rule plus
`_MAX_BACKFILLS_PER_CALL = 5` lazy backfills), so an uncapped LLM array could
occupy a worker thread for minutes.

The `preferences_update` / `projects_update` prompt keys and whole-file writes
are enabled only for V1 when `memory.migrated` is false. V2 always treats its
current preference/project documents as read-only extraction context, regardless
of that global migration setting. New facts and proposed corrections use the
structured revision-aware path; background consolidation cannot remove core
material by replacing a manual member document.

Both versions freeze a deep copy of the extraction transcript and revalidate
its generation, original message prefix and new user turns after the model
returns, before any memory write. Edited/deleted source messages or a new user
turn refuse that pass and leave it pending without charging a different span's
retry budget. Appended assistant acknowledgments can remain pending while the
unchanged original span is committed. This source check and each record's
revision check protect against stale background extraction; they are separate
checks, not a cross-file transcript/database transaction.

The prefs path does NOT advance the persisted `last_consolidated` marker — only the history path does. This ensures history consolidation always covers all messages, even if prefs consolidation fired earlier.

Idle detection: `_last_activity[key]` updated on every `maybe_consolidate()` call. `check_idle_sessions()` called every heartbeat tick (60s), fires history consolidation when `now - last_activity > history_idle_secs` and there are unconsolidated messages.

**Both paths write to the session's captured store.** `_consolidate` captures the
canonical execution context before its first await and refuses incognito or
temporary sessions before reading their transcripts. V2 uses that context's exact
member store and commits learned records, history and the retry receipt in one
SQLite transaction. V1 retains `context.store_of_session(log, key)` and its
Markdown and lesson fallback behavior. See [Memory across surfaces and channels](#memory-across-surfaces-and-channels).

Neither path owns a timer. The prefs path is checked inline on every
`maybe_consolidate()`; the history path is driven entirely by the heartbeat
calling `check_idle_sessions()`. Every embed-bearing step
(`_write_structured_memory`, `_save_lessons`, `append_history`) is dispatched
through `run_in_embed_pool` (the bounded `mc-embed` bulkhead) because
`_consolidate` runs on the gateway event loop, and a slow or hung embed inline
would stall heartbeats, Slack, and the dashboard.

Structured `[Monitor wake]` turns never call `maybe_consolidate()`: their prompt
and resulting action are automation evidence, not user-authored memory. Monitor
admission also refuses restricted dashboard sessions, so a persisted loop cannot
outlive the incognito or temporary boundary that prohibits derived memory.

### Lesson Extraction from Chat

The history consolidation prompt includes a `"lessons"` key that extracts only implicit correction patterns — corrections the user made without explicitly saying "remember" (those are already saved immediately via `learn_add`). V1 lesson writes use `write_lesson()` with substring and topic-overlap dedup (shared keywords ≥ 50% of the LARGER of the two keyword sets → newer replaces older), or `LessonStore.save()` when vector memory is inactive. V2 publishes lessons inside the consolidation transaction and never constructs a JSONL fallback.

### Configuration

`~/.kiro/crew/config.json` → `"memory"` section:
```json
{"history_idle_hours": 3.0, "history_max_days": 365}
```

Exposed on dashboard: Overview → Memory tab → Memory Settings card. Changes apply immediately to running consolidator via `PUT /api/memory/settings`.

A plain `config.json` write reaches the same instance. `HistoryConsolidator`
subscribes to `skills`, `memory.history_idle_hours` and `memory.migrated`, and
`reconfigure(cfg)` re-copies the idle window, the migrated flag and the ten
`skills.*` auto-skill settings onto the live object — the dashboard route and the
config watcher call the SAME method, so neither path reverts the other. These
values only gate the NEXT consolidation pass or the next auto-skill judgement, so
a pass already running finishes on the values it read and the next one uses the
new ones.

## Vector Memory (`vector_memory.py`)

The semantic and episodic list endpoints accept optional `q` text search, capped
at 2,000 characters. Filtering occurs inside the selected store before
`LIMIT`/`OFFSET`; deleted rows and other members' data remain excluded. Matching
uses literal Unicode NFKC/casefold substring comparison over semantic keys and
decoded JSON values, or episode text and decoded tags. `%` and `_` are literal
characters, not SQL wildcards. Query filtering does not change retrieval ranking,
store provenance, or the existing V1 list behavior when `q` is absent.

Structured memory system backed by SQLite + FAISS + in-process embeddings (vendored llama-cpp-python). Embeddings are ALWAYS-ON: `_coerce_embedding_provider` (config/loader.py) coerces EVERY `embedding_provider` value — including legacy `"ollama"` and `"none"` — to `"llama_cpp"`, so there is no config knob to disable them. While the model is still downloading or absent, memory degrades gracefully to keyword/FTS search and the lazy-rebind machinery in `vector_memory._try_embed` picks embeddings up when the model lands — no restart. Per-store overrides (`MemoryStoreConfig.embedding_provider`, enum `["", "llama_cpp"]`) can only inherit or restate the default — per-store disable is not supported, and the value reaches nothing: `context._build_store_vectors` configures a named store's `VectorMemoryStore` from top-level `cfg.memory`, and the embedder beneath it is the process-wide `get_shared_embedder()` singleton, so two stores cannot run two backends without two resident models and two incomparable vector spaces.

### Live reconfiguration (`VectorMemoryStore.reconfigure`)

The store subscribes to the `memory` section in `__init__`, and `reconfigure(memory_cfg)` pushes the retrieval settings onto the running instance: `semantic_confidence_threshold`, `episodic_dedup_threshold`, `episodic_max_results`, `episodic_max_count`, the `decay_rates` table (re-sanitized, not copied) and the `semantic_keys` prefix list (rebuilt from the built-ins plus the configured extras). Everything the class copies out of config at construction is covered, so none of it waits for a gateway restart. Changing a decay rate also invalidates the resident episodic scoring set, which carries the rates it was built with and would otherwise keep ranking on the old curve with no row having moved.

Embedding width is deliberately NOT touched. Changing `memory.embedding_dim` invalidates every stored vector, which is a re-embed rather than a value swap, so it stays boot-only and its apply path is the dashboard's embedding-model route.

### Vector publication across processes

Managed inference pins the actual shared backend before recording database
provenance. Its signature must match the observed database signature, and a
pending explicit rebuild refuses inference. Cache hits and coalesced requests
use that same pinned backend. Results retain their signature and handled request
through vector normalization; publication rechecks them under SQLite write
admission. The installation config lock covers managed publication too, so a
new explicit request cannot land between the final request check and commit.
Acquire that config lock before the store's process-local database lock, including
primary episodic writes and lazy/explicit backfills, so a config waiter does not
hold up database readers. `_vector_commit` owns both locks through commit or
rollback; callers must not wrap it in an earlier database-lock hold.
Recall checks the same result provenance before publishing its response.

| Caller | Durable content and vector failure contract |
|---|---|
| `set_semantic` | Content commits first; failed vector admission, update or commit is logged and leaves the content saved. |
| `write_lesson` | The rule's primary write must succeed; its vector tail is best effort. |
| Lazy lesson backfill | A failed derived update does not prevent the primary lesson operation. |
| `write_episodic` | Content and optional vector share the primary transaction; obsolete vectors become NULL, but primary transaction failures propagate. |
| Three explicit backfills | Each vector has one transaction; failure propagates for repair reporting, with earlier committed rows retained. |
| `recall` | No vector write; changed provenance discards the partial result and retries lexical retrieval. |

`_vector_commit` owns derived transaction admission, commit and rollback. Its
callers do not commit inside it. Failed admission never rolls back a transaction
it did not start. Rollback failure closes the uncertain connection, retaining
previously committed content and refusing further use of that connection.
Resident vector indexes are discarded after rollback. This is per-record repair,
not a transaction across every database or a guarantee against filesystem failure.

### Thread safety (`_db_lock`, `threading.RLock`)

One `VectorMemoryStore` instance **per `db_path`** is shared by the gateway event
loop (readers) and several worker threads (writers: consolidation via
`run_in_embed_pool`, the dashboard memory handlers via `asyncio.to_thread`). One
per path is an invariant, not an optimization — two instances over one file do not
share `_db_lock`, which voids everything below — and it is why `ensure_store`
closes the loser of a construction race rather than keeping both. It holds ONE
`sqlite3` connection and ONE FAISS index, and neither is thread-safe: `sqlite3` caches
prepared statements per connection, so two threads stepping a statement at the
same time corrupt each other's row iteration (observed as
`DatabaseError("another row available")`, and on Windows CI as a `None` value for
a column the `WHERE` clause excluded), while a concurrent FAISS `add` during a
`search` can corrupt the C++ index outright. `self._db_lock` (a reentrant
`threading.RLock`, so a locked method may call another locked method)
serializes every statement on that connection. The structural regression guard
recognizes explicit lock scopes and `_vector_commit`, checks the latter's actual
`ExitStack.enter_context(_db_lock)` acquisition, and fails if that acquisition
is removed. Its negative fixtures retain checks before acquisition, after scope
exit, on failed admission and in deferred nested functions. Generation rechecks
must be lexically inside a lock-owning scope, not merely later in source order.
The critical sections that matter most:

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
  row fetch — or the scoring-set build that replaces it — is locked; the
  ranking then works on materialized data outside the lock.

An async caller offloads every operation that can reach `_db_lock`, including
`close()`, so contention never stalls the event loop. The source gate derives
the ordinary method set from the store call graph and separately tracks
constructor-bound receivers for the generic lifecycle names `init` and `close`.

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

### Two schema lineages

One engine drives two schema lineages, and which one a vector file is on is a property of
that FILE for the life of the file. A **crew silo created from this point forward** is a
`memory_items` file on `schema_version` 1001 — one row table carrying three kinds plus the
carve facets, with v1's relation names re-presented over it as views. **Every other vector
file, above all `config_dir()/"memory.db"`, is the v1 lineage**, frozen at
`schema_version` `{1, 2, 3}` with `semantic_memory` and `episodic_memories` as real
tables. Two lineages, one engine: no cutover, no dual write, no backfill, and no
`CONTRACT_VERSION` bump. `memory_schema.py` owns the crew lineage; `vector_memory.py`
keeps owning v1's `_MIGRATIONS`.

**`memory_events` and `memory_meta` have ONE definition, and it is not per-lineage.**
They are the only product tables the engine reaches with no relation indirection —
`_log_event`, `get_events`, `rotate_events` and `_read_meta`/`_write_meta` name them
literally on both lineages — so their DDL lives once, as `memory_schema.MEMORY_EVENTS_SQL`
and `MEMORY_META_SQL`, and `vector_memory._SCHEMA_V1` / `_MEMORY_META_TABLE` and
`CREW_SCHEMA_SQL` all compose those constants. A second hand copy is the shape that fails
silently: `MIGRATIONS_CREW` is one frozen entry, so a fourth `_MIGRATIONS` entry adding a
column would reach every v1 file and NO silo, and the shared INSERT would then fail on
silos alone — with v1, the lineage the rest of the suite exercises, still green.

What each `init()` leaves on disk:

| | every v1 file, incl. the default store | a new crew silo |
|---|---|---|
| `schema_version` | `{1, 2, 3}` | `{1001}` |
| tables | `semantic_memory`, `episodic_memories`, `memory_events`, `memory_meta`, `schema_version` | `memory_items`, `memory_events`, `memory_meta`, `schema_version` |
| views | none | `semantic_memory`, `episodic_memories` |
| triggers | none | none |
| `memory_meta` stamp rows | none | `schema_lineage`, `store_name` |

**Which lineage a file gets is decided once, structurally, inside `init()` — and the
ordering there is the load-bearing part.** `memory_schema.detect_lineage(db)` reads
SQLite's own schema table FIRST: a file holding `memory_items` as a table is crew, a file holding
either v1 relation as a table is v1, and only a file with no product table at all answers
`None`. A path is consulted only for that third case, through
`memory_stores.named_store_of_db`. Every vector file that exists on any install today
holds `semantic_memory` as a real table, so it answers v1 before the path predicate is
reached — which is what makes "the operator's running memory is untouched" a property of
the code path rather than a claim a test asserts. There is no route from a populated file
to `MIGRATIONS_CREW` at all, so a later edit to the path predicate cannot reach that file
either. `detect_lineage` matches `type='table'` deliberately: on the crew lineage
`semantic_memory` exists as a VIEW, and a check that accepted either kind would answer v1
for a crew file and then run the v1 migrations against views.

**The predicate is POSITIVE membership, not a negation.** `named_store_of_db(path)`
answers the name of the named store whose vector file `path` is, or `""` — the inverse of
`resolve_store_path`, and the only spelling of "this file is a crew silo". Written as
`db_path != config_dir()/"memory.db"` it would be TRUE of four real non-silo paths and
hand the crew schema to each: the eval runner's `ws/"vector_memory.db"`, the bench ingest
path, the onboarding importer's `destination/"memory.db"`, and every `tmp_path` in the
suite. The predicate answers `""` for all of those, for the literal
`memory_stores/default/`, and for a malformed store name. Its containment test is
IDENTITY (`parent.resolve() == memory_stores_root().resolve() / name`) rather than a
resolved-parent check, for the reason `_named_store_dir` already refuses aliasing: with
`memory_stores/acme` symlinked at `memory_stores/finance`, a parent check still sees the
root and would answer `"acme"` for a file that physically belongs to `finance`.

**The gate lives inside `init()` because no call-site check could cover the call sites.**
`VectorMemoryStore` is constructed from many places, and one of them —
`security.scan_memory` — sits outside the memory subsystem entirely and reaches the
default store as a bare `VectorMemoryStore()`, so a per-construction decision would have
to be re-derived by callers that know nothing about lineages.

**The three kinds are a queryable column, not a dispatch axis.** `memory_items.kind` is
`CHECK`-constrained to `directive` (behavioural preferences plus lessons), `fact`
(projects, semantic and user facts) and `episode` (daily history plus episodic), and is
stamped from the key prefix at write time by `kind_for_key` — `lesson.*` is a directive,
every other semantic key is a fact. Nothing dispatches on it: the engine keeps
discriminating lessons with `key LIKE 'lesson.%'` exactly as it does on v1, so `kind`
never becomes a second, divergent notion of what a lesson is.

**`semantic_memory` and `episodic_memories` survive as READ-ONLY VIEWS presenting v1's
exact columns in v1's exact order.** The 36 read statements naming them in
`vector_memory.py` are therefore unchanged, and `SELECT *` still hands `sqlite3.Row`
the columns the engine expects. Splitting by `kind` is also what keeps the vector scorers
partitioned by RELATION: episodic blobs are L2-normalized at write and semantic and
lesson blobs are not, and three of the four places that score a stored blob take a bare
dot product (the FAISS `IndexFlatIP` search, `_sqlite_vector_search`, and the promotion
clusterer) while only `_stored_similarity_scorer` divides both norms out. One undivided
`embedding` column would put un-normalized rows in front of the three that assume unit
length.

**The views must NEVER be given an `INSTEAD OF` trigger.**
`snapshot_redact._refuse_update_triggers_that_destroy_rows` refuses a database whose
UPDATE trigger writes a relation other than the trigger's own, and its exemption requires
`target == tbl_name` — which an `INSTEAD OF` trigger on a view can never satisfy, because
its `tbl_name` IS the view while its body names the physical table. The refusal keys on
the trigger's name and rejects the whole DATABASE rather than the one relation, so a file
carrying such a trigger can never be proven redacted and any bundle staging it refuses
instead of uploading — permanently, since the trigger is part of the schema.

Writes therefore name the physical table: `semantic_relation(lineage)` /
`episodic_relation(lineage)` resolve it and
`semantic_guard` / `episodic_guard` supply the trailing `AND kind …` clause that keeps a
semantic write off an episode sharing the table; both render EMPTY on v1, so the 15
interpolated write statements are byte-identical to the literals they replace. The four
that differ in their COLUMN LIST — semantic insert, semantic upsert, and the two episodic
inserts — carry two spellings plus a param builder in `memory_schema`, side by side so a
change to one is visibly a change to the other. The one writer outside the engine that
names a view directly is the bench ingest harness's `created_at` backdate, and it is safe
only because a bench path is never a silo.

**The facet columns are deliberately ABSENT from both views, which is what makes them
carve axes rather than ranking signals.** `scope`, `surface`, `crew`, `session_key` and
`derived_from` exist on `memory_items` and appear in neither view, so no existing ranker
can read one. "A facet partitions, it never scores" is thereby a fact about the relation
instead of a convention someone has to police.

#### Who stamps a facet

`memory_schema.MemoryFacets` is a frozen dataclass whose five fields all default to `""`,
matching the columns' `NOT NULL DEFAULT ''`: for a carve, "absent" and "not applicable"
are the same answer, and a nullable axis would make every future filter spell
`IS NULL OR = ''`. It is a keyword argument on `set_semantic`, `write_episodic` and
`write_lesson` — the additive-with-a-safe-default shape — so a caller threads identity
once and both lineages accept the call. `VectorMemoryStore._stamp_facets` applies it
through `FACET_STAMP_SQL` and returns immediately on v1, where the columns do not exist.

**A stamp is additive, and never fails a write.** Each axis is written through a
`CASE WHEN ? = '' THEN <column> ELSE ? END`, so a second writer that knows only the
surface cannot blank a scope the first established. `_stamp_facets` swallows every
exception and logs, because a raise would land inside `HistoryConsolidator._consolidate`'s
try while `billed` is still `False` — the attempt would be recorded as never having
happened and all four consolidation entry points would re-arm on every idle tick, forever,
with no backoff. A lost facet costs one carve filter; nothing else reads the column.

**Two writers hold real identity, and only those two thread it.** The history
consolidator builds the facets in `_session_facets(meta, key)` and passes them to both
`_write_structured_memory` and `_save_lessons`: `crew` from the session's `agent`
metadata — the **crew alias**, never the kiro-cli agent template, whose namespace is
disjoint and whose use here would resolve `default` for exactly the crew that configured
otherwise — `surface` from `messaging.link.telemetry_channel_of`, which returns a bounded
label and never the raw session key, and `session_key` verbatim. `promote_episodic_patterns`
stamps `derived_from` with the canonical episode's id, which is the only surviving trace
of provenance because that method tombstones the cluster it promoted. `write_lesson`
mirrors its existing `repo_scope` onto `scope` when the caller named none.

`scope` is an INDEX-ONLY PROJECTION: `value_json` stays authoritative and the lesson
reader keeps reading it, because two sources of truth for a scope is how a carve silently
widens. The doc's `repo_url` / `code_path` / `package` trio is deliberately not built —
no deterministic source for it exists here (the git-origin probe answers `None` for every
worktree and spawns a subprocess per call, the branch helper returns an egress-redacted
value that will not compare equal later, and the manifest the doc reads for `package` is
not part of this build), so `scope` carries the one repository axis that has a live
evaluator and a live carve.

Every other writer — the CLI, the dashboard memory routes, the task runner, the channel
gateways — stamps nothing, and its rows carry the `''` defaults. Those callers hold no
crew or surface identity at the point of the write, so a stamp there would be an invented
value rather than a recorded one.

#### Who reads a facet

Two methods on `VectorMemoryStore`, and they are the only readers: `list_by_facets` pages
the rows matching a carve, and `count_by_facet` answers "what is actually in this store's
memory" — how much each crew, surface, scope, session or kind contributed. Both take
`filters` as a **mapping from facet name to exact value**, ANDed together, plus an optional
`kind`.

A mapping and deliberately not a `MemoryFacets`: the dataclass spells absence and "not
applicable" identically (both `""`), which is right for a stamp and would make one carve
unaskable here. **An axis the mapping omits is unconstrained; an axis mapped to `""`
selects the rows no writer attributed** — and "which rows did nothing stamp" is the first
question an operator asks when a carve comes back short. Live rows only, like every other
reader in the engine, which is also what lets the query use the `(column, is_deleted)`
indexes.

**The SQL lives in `memory_schema`, not in the engine, and that placement is load-bearing
twice over.** It names `memory_items`, a relation only the crew lineage has, so the same
literal inside `vector_memory.py` would be a statement that raises on every v1 file — which
is exactly what `test_memory_lineage_drift`'s "every relation the module names exists in
both lineages" refuses. And it keeps the one place a facet NAME is spliced into SQL beside
the dataclass those names come from.

**Names come from an allowlist derived from the dataclass; values are bound.**
`FACET_NAMES` is `tuple(field.name for field in fields(MemoryFacets))`, and the builders
iterate THAT tuple, consulting the caller's mapping for membership only — so the identifier
reaching a statement is always one of the module's own literals, however a caller spells
its key. An unknown key, an unknown group axis, and an unknown `kind` all raise
`UnknownFacet` rather than being dropped, because a silently ignored filter WIDENS a carve:
a caller asking for one crew would be handed every crew's rows under a heading naming
theirs. `GROUPABLE_COLUMNS` is `FACET_NAMES` plus `kind` — `kind` is the row type rather
than a stamped attribution, so it stays out of the filter allowlist's derivation while
remaining a legitimate group axis.

**A facet query on the v1 lineage REFUSES**, with `memory_schema.FacetsUnsupported`, and
the refusal is the same at every surface. An empty page there would say "this crew has no
memories" about `config_dir()/"memory.db"` holding thousands of unfaceted rows — the one
wrong answer this seam can give, since an operator reads it as a writer bug. The
discrimination is `self._lineage`, resolved once in `init()` from the file's own schema:
never a `hasattr` probe and never a `try`/`except` around `no such column`.

**Three of the five axes are indexed, and the docstring says which.** `scope`, `crew` and
`surface` each have a `(column, is_deleted)` index and seek; `kind` alone rides
`idx_mi_kind_live`; `session_key` and `derived_from` have **no index** and scan. That is
left as it is on purpose: both are high-cardinality identifiers reached from a row the
operator already has in hand, so they are needle lookups rather than store-wide aggregates,
and two more indexes on a write-heavy table are paid for by every consolidation pass.
SQLite may scan the creation-time index to satisfy ordering; that remains a full
scan rather than a facet seek. Pairing an unindexed axis with an indexed one recovers
the seek. The plans are pinned by
test, so the claim cannot rot into a wrong promise.

Both reads are bounded by the builder rather than by each surface: `MAX_FACET_PAGE` rows per
page and `MAX_FACET_GROUPS` distinct values per count, the latter because `session_key`
cardinality is unbounded. The count is ordered by population, so the truncation drops the
least populous tail. Paging orders `created_at DESC, id` — the tie-break on the primary key
is what makes a page stable, since one `created_at` tie is enough to show a row twice and
hide another.

**Two surfaces, and the store they read is answered differently.** `kirocrew memory carve`
takes `--store`, one flag per facet, `--kind`, `--count-by`, `--limit` and `--offset`; it
dispatches BEFORE `_memory_cmd`'s shared store is opened, for the same reason the backup
verbs do — that store is hardwired to the default store's path. `GET /api/memory/carve`
routes through the shared `?store=` resolver every store-scoped memory route uses (see
[Which store a dashboard route reads](#which-store-a-dashboard-route-reads-store)):
with the parameter ABSENT it reads the global store, preserving the dashboard's default
behavior. An unverified `X-Session-Key` must never select a silo. With it PRESENT the
request takes the owner gate, so naming another crew's silo requires the dashboard owner's own
identity and is unavailable to an agent or an MCP tool, which have none to present. What
must not grow here is a store dimension that skips that resolver — a hand-read `?store=`
in this handler would be exactly the cross-silo read the file boundary exists to prevent.
An absent parameter reads the global v1 store and gets the
refusal, `409` with `code: "facets_unsupported"`. An unknown `count_by` or `kind` is `400`
`unknown_facet`; a silo whose vector tier cannot be stood up is `503` `store_unavailable`,
reported rather than answered from the global store.

An unrecognized *query key* is ignored rather than refused, and that asymmetry with the
store's `UnknownFacet` is intended: a request legitimately carries keys that are not filters
(`?token=` among them), so a route that 400'd on those would break query-token auth. The
route never forwards a caller key as a column name, so the enumerable inputs — `count_by`
and `kind` — are the two it validates.

**Neither read is an MCP tool, and that is a judgement rather than an omission.** No verb
in the `kirocrew memory` group has an MCP twin: the facets are attribution metadata about
who wrote a row, not recallable content, and an agent already receives its memory through
context injection and `learn_list`. There is also nothing for a model to do with the answer,
and the safe shape of the capability — read only the caller's own bound store — is precisely
the shape that makes it useless as a tool, since an agent cannot ask about a store it is not
in.

**There is no `embedding_dim` column, although the design asks for one.** Its stated
purpose — "store the dim, do not assume 1024" — is already met without storing anything:
a vector's width **is** `length(embedding) / 4`, derivable from the blob whenever it is
wanted, and the two comparability checks read the blob's byte length rather than any
column. As stored data the width could only DRIFT from the vector it describes, and it
would: six lazy-backfill and repair statements set `embedding` alone, so a backfilled row
would carry a fresh vector beside a stale or NULL width. A write-only column that can
disagree with its own subject is worse than no column. A `GENERATED ALWAYS` column would
make the drift impossible, and is still rejected: it requires SQLite 3.31+, this build
documents no SQLite floor, and no generated column exists anywhere else here — an
undeclared version floor is a poor price for a column nothing reads. Note the unrelated
config key `memory.embedding_dim`, which is the live embedder's width and IS read.

**The two version series are disjoint on purpose** (`{1, 2, 3}` against `{1001}`) and
`init()` applies migrations by set membership, so neither lineage's DDL is reachable
through the other's loop. That is the third barrier, and it fails LOUD rather than
silently: were detection ever bypassed on a crew file, v1's
`CREATE TABLE IF NOT EXISTS semantic_memory` silently no-ops against the view and then
`_migrate_v2`'s `ALTER TABLE semantic_memory ADD COLUMN embedding` raises
`Cannot add a column to a view`, which `_migrate_v2` re-raises because it swallows only
`duplicate column`. That happens inside `init()`, before any write.

**Timestamps are TEXT ISO-8601, never the `REAL` a numeric schema would reach for.**
Seven sites rank `created_at` / `updated_at` by lexicographic string comparison and two
more parse them with `datetime.fromisoformat`; one of the seven is
`_enforce_episodic_cap`, the episodic CAP EVICTION, where a wrong order tombstones the
wrong memories. SQLite also sorts REAL before TEXT, so a mixed column is worse than
either choice on its own.

**`UNIQUE (key)`, not per-kind uniqueness.** v1 spells this `key TEXT PRIMARY KEY` — one
row per key, period — and per-kind uniqueness is strictly WEAKER: it would let
`pref.color` exist as both a directive and a fact, and the `semantic_memory` view would
then return two rows where every statement in the engine expects at most one. SQLite
treats NULLs as distinct in a unique index, so episodes (`key IS NULL`) stay
unconstrained and are identified by `id` alone. A semantic row's `id` is deterministic
from its key (`semantic_item_id`, the `key:` namespace), which is why no writer needs
`last_insert_rowid()` — the engine has no such call.

**V1 keeps its existing lineage.** New member stores are created explicitly with
the crew row schema, record metadata/revisions, history/revisions, consolidation
receipts and FTS. The `member_database` singleton records `format_version=1`,
`member_id` and `store_id`. The expected stable identity comes from canonical
admission. Ordinary opens validate it and the schema without repair or migration.
Legacy unowned crew-lineage files retain V1 behavior. Unsupported old private V2
files are refused unchanged; no path reinterprets them as V1.

**Whole-install portability includes named stores.** Snapshot and export/import
carry member SQLite databases and manual profiles, using SQLite backup to include
committed WAL contents without shipping WAL or SHM files. Merge keeps an existing
store whole; replace coordinates namespace and active-handle locks. Host-local
execution logs and backup directories remain outside the bundle. Dedicated member
recovery is described under [Automatic backups](#automatic-backups-memory_backuppy).
The injection audit `scan_memory` opens declared stores, including learned history,
revisions and consolidation evidence, and labels each finding with its store.

### Semantic Memory

The scoring descriptions in this section and Episodic Memory describe Global
**V1**. Newly created member stores use the separate
[Member V2 retrieval policy](#member-v2-retrieval-policy). A nondefault filename
alone does not enable V2; canonical admission must supply the stable member/store identity to the explicit database open.

SQLite table `semantic_memory` — structured key-value store with:
- **Allowed keys**: `_BUILTIN_PREFIXES` is `pref.*`, `project.*`, `user.*`, `lesson.*` (+ user-configurable `extra_prefixes`). The first three are the fact prefixes the consolidation prompt offers the LLM; `lesson.*` is the lessons tier writing into the same table.
- **Key format**: `^[a-z][a-z0-9_.]*[a-z0-9]$`, max 100 chars; value JSON max 4,096 bytes. The lower bound is a decoded one: a value that is null, empty, or only whitespace is refused as `VALUE_EMPTY`, so no row can hold a value that reads as absent.
- **Confidence gating**: writes whose source is not `user_explicit` require confidence ≥ `_DEFAULT_CONFIDENCE_THRESHOLD` (0.8); `user_explicit` bypasses the threshold
- **V1 conflict resolution**: `user_explicit` replaces an existing value; an automated source cannot replace an active user-explicit fact, unless the stored value is itself degenerate (null, empty, or only whitespace) — that row holds nothing for precedence to protect, and only an automated writer would ever repair it, so such a write is allowed and logged. Otherwise higher confidence wins, or the newer value wins when the confidence difference is less than 0.1. Tombstones can be recreated. V1 consolidation retains direct stale-key deletion and its semantic prompt, while extracted lessons keep their automatic consolidation source. An LLM confidence claim is not user evidence. Reaffirmation still refreshes confidence/source and reaches the original embedding and retirement paths. Owner edits retain shared revision checks. A rejected write logs a best-effort `conflict_skip` event; an unavailable event log does not prevent a V1 data write.
- **Injection detection**: the `_INJECTION_PATTERNS` regex set (14 patterns, `vector_memory_constants.py`) is scanned on every value write
- **Write-time embedding**: `_write_semantic()` embeds `"<key> <value_json>"` after the upsert (outside `_db_lock`, at `PRIORITY_BULK` — nothing blocks on it and the tail is reached from consolidation/import loops; same space-generation contract as `write_lesson`) and persists the struct-packed, un-normalized vector into the row's `embedding` column. The upsert's conflict clause keeps the stored vector when the value is unchanged (a re-affirmation — the tail then skips the redundant embed) and clears it when the value changed, so a row never ranks by a vector for text it no longer holds. `lesson.*` keys are excluded (`write_lesson` owns their vector — raw rule text). `set_semantic_if_absent()` (bulk import) defers embedding to the backfill sweep, like `write_episodic(defer_embedding=True)`. Rows missed while the model was absent — plus rows cleared by `reconcile_embedding_space()` — are repaired by `_backfill_semantic_kv_embeddings()` inside `backfill_missing_embeddings()`.
- **Audit trail**: `memory_events` table logs every create/update/delete with old+new values, bounded at `_MAX_EVENTS = 10_000`. The dashboard events API recursively redacts credentials and unsafe URLs on response for Global V1, named V1 and private V2. Stored events and their identities remain unchanged.

Retrieval formats `key: value` pairs in a `[Semantic Memory]` block and excludes `lesson.*` keys. With a query it uses `_SEMANTIC_VECTOR_WEIGHT` 0.6 × vector_score + `_SEMANTIC_KEYWORD_WEIGHT` 0.4 × keyword_score; `_stored_similarity_scorer` embeds the query once and reads stored vectors. When the query vector is available, a row without a vector contributes zero on that term; without embeddings, retrieval uses keyword scoring. Explicit identity terms supplement keys and values. V1 startup reads only eligible `pref.*` rows through `get_preferences_context`, with the DATA-only wrapper and no query embedding. Other semantic facts are retrieved explicitly through `memory_recall` or the activity-enabled Python reader. V2 also leaves fragment retrieval to `memory_recall`, which has its own total response cap.

The keyword half's ROW side — the regex scan, set build, and Snowball expansion over a row's key and value — depends only on that row's own text, so it is memoized by `_row_stem_tokens`, bounded at `_ROW_STEM_CACHE_SIZE` entries. The memo is keyed on the TEXT rather than on a row key or rowid: an updated value hashes to a different entry, so no write path has an invalidation step to forget and a stale token set can never be served for text the row no longer holds. Only the row side goes through it — query text has one distinct value per user message, so memoizing it would evict the bounded row population the memo exists to keep. This is a separate memo from the per-word `_stem_one` cache (`_STEM_CACHE_SIZE`), which the row memo populates on a miss.

**A scan wider than the cache bypasses the memo, by design.** Both row-side callers (`get_semantic_context` and `_rank_lessons`) ask `_row_stem_tokens_for_scan()` for the form to use, passing the number of entries the pass will touch — two per row for the semantic scan, one per lesson — and get `_row_stem_tokens_uncached` when that exceeds `_ROW_STEM_CACHE_SIZE`. A repeated full-table scan is LRU's worst case: past the bound every lookup evicts the entry the next one needs, so the hit rate is not degraded but exactly zero, and the memo costs the wrapper plus the retained frozensets while returning nothing. Nothing caps `semantic_memory` — only `_MAX_SEMANTIC_PER_CONSOLIDATION` per run, and `promote`/`import`/`migrate` bulk-write — so a store crosses that width on its own, which is why the width is checked per scan rather than assumed. Note also that the bound is in ENTRIES and therefore does not bound bytes: an entry retains its text plus a frozenset of words and stems, so a filled cache spans roughly 9 MiB for ordinary values to ~296 MiB for `_MAX_VALUE_BYTES` values of short words, held for the process's life. Size the constant against that ceiling.

### Episodic Memory

SQLite table `episodic_memories` — conversation fragments with optional embeddings:
- **Write**: text validation (10-2000 chars), **prompt-injection screening** (`_contains_injection`, same pattern set as the semantic-KV path), tag sanitization, importance clamping (0-1), FAISS dedup (cosine > `_DEFAULT_DEDUP_THRESHOLD` = 0.88, configurable via `memory.episodic_dedup_threshold` — the production stores (`slack/gateway.py`, `cli_server.py`, the dashboard's standalone fallback in `dashboard/handlers/memory.py`) pass it as `dedup_threshold`; deferred writers skip this check entirely so they have no threshold to honour, and `eval/bench/ingest.py` sweeps its own value). The dedup scan **skips tombstoned ("ghost") matches**: tombstone paths (merge, dashboard delete, cap eviction, stale retirement) set `is_deleted=1` but leave the vector in `_faiss_index`/`_faiss_id_map`, so a high-similarity hit may map to a deleted row. `_get_episodic()` filters `is_deleted=0` and returns `None` for those; the write loop `continue`s past a `None` match (mirroring `search_episodic`'s `if not mem or mem["is_deleted"]: continue`) instead of treating it as a conflict — otherwise a new memory matching a deleted one was silently rejected (data loss).
- **Injection screening (XPIA defense-in-depth)**: episodic text is derived from conversation transcripts, so a poisoned turn could persist steering instructions that get re-injected into future contexts. `write_episodic()` runs `_contains_injection()` (before the embed call) and, on match, drops the entry and emits an auditable `injection_blocked` event with `memory_type='episodic'`. The stored audit snippet is scrubbed with `redact_exfiltration_urls()` + `redact_credentials()` first as defense in depth; `/api/memory/events` also redacts all returned events. This mirrors the semantic-KV screen at `validate_semantic()`. **Residual (accepted risk)**: this is a best-effort regex screen: a determined owner can still steer their own long-term memory with phrasing that evades the patterns; long-term memory poisoning is an accepted residual. The screen raises the bar against accidental/opportunistic XPIA persistence, not against a motivated self-owner.
- **Search**: FAISS vector similarity with decay scoring: `cosine_sim × (0.7 + 0.3×importance) × exp(-rate×days_old)`, then MMR diversity reranking (Jaccard-based, `_MMR_LAMBDA` = 0.6). The decay rate is `_DEFAULT_DECAY_RATE` = 0.03/day, configurable per tag via `memory.decay_rates` (`_decay_rate_for`): keys are tags (case-insensitive, matching `_matches_tags`), the reserved `default` key replaces the built-in fallback, a multi-tag row uses the SLOWEST matching rate (smallest = maximum retention, so a broad tag can never age out a long-retention one), values are clamped to [0, 10] and non-numeric entries are dropped with a warning by `_sanitize_decay_rates`, which runs on every config apply as well as at construction, so a hand-edited rate is clamped and a garbage entry dropped identically either way. Both vector rungs (FAISS and the stdlib fallback) resolve the rate through the same helper; the keyword rung does no decay scoring at all.
- **MMR reranking**: Maximal Marginal Relevance balances relevance with diversity. Greedy iterative selection penalizes candidates similar to already-selected results. Prevents redundant episodic fragments from consuming the context budget. Configurable via `mmr=False` parameter to disable. The candidate pool is deliberately NOT truncated toward `limit` (that tail pick is the point of MMR); the only bound is the recall-safe `_MMR_MAX_POOL` = 1000 ceiling for pathological inputs.
- **Relevance threshold**: `_EPISODIC_RELEVANCE_THRESHOLD` = 0.55 cosine required for context injection, relaxed to `_EPISODIC_LONG_TEXT_THRESHOLD` = 0.42 for entries longer than `_EPISODIC_LONG_TEXT_CHARS` = 300 chars, on the reasoning that long texts dilute cosine scores. **Neither value is tuned, and the two classes it separates overlap** — measured, both are looser than the best achievable cut and the long-text relaxation is about twice the dilution it compensates for. Do not read 0.55 as a discovered boundary: [The admission gate is a loose cut, not a tuned one](#the-admission-gate-is-a-loose-cut-not-a-tuned-one) carries the measurement and the harness that produced it. The threshold reads the RAW `cosine_sim`, not the decay-adjusted score, so age and importance affect ordering but never admission. Admission runs BEFORE the decay ranking, MMR, and the `limit` cut: `get_episodic_context()` calls `search_episodic(relevance_filter=True)`, which drops sub-threshold candidates first, so a highly relevant but old memory cannot be ordered past `limit` by a cluster of recent-but-irrelevant rows that the gate would then remove — a case that otherwise returned empty context while an exact match sat in the store. `search_episodic()` defaults to `relevance_filter=False` and returns the full ranked set for dashboard/API/CLI use. The keyword fallback is unaffected because those rows carry no `cosine_sim` key at all.
- **Fallback ladder**: FAISS (needs faiss + numpy) → `_sqlite_vector_search`, cosine over the stored blobs → FTS5/LIKE keyword search (OR logic on text + tags) when there is no query embedding at all. The middle rung matters: faiss is an optional accelerator, not a declared dependency, so a stock install still gets vector recall from the stored vectors. Inside that rung the per-row dot product itself has two rungs, guarded by `_HAS_NUMPY` exactly as `_stored_similarity_scorer` is: the query vector is converted once outside the row loop, then numpy does the products where it is installed and `struct.unpack` + `sum` does them where it is not. The numpy resident and per-call rungs dot in float32, matching the stored dtype. The FAISS path verifies each candidate against its current SQLite vector and recomputes cosine with Python arithmetic and norm division; these paths share the admission policy but do not promise bit-identical floating-point results.
- **Resident scoring set (the middle rung, with numpy)**: scoring reads only the embedding, `tags`, `importance`, `created_at` and the text LENGTH, and none of that changes between two searches with no write in between — so `_EpisodicScoringSet` holds those columns as numpy arrays and the search resolves row BODIES (`text`, `conversation_id`, `last_accessed_at`) for the ranked pool only, through the same `_get_episodic_batch` the FAISS path uses. Decay is a vectorized expression over the cached arrays, not a per-row Python dict build. Filtering still runs across the FULL population before `limit` — `tag_filter` and the relevance gate are masks over the cached arrays, never a top-k window, because a tag matching few rows would otherwise miss the pool entirely and return nothing where it returns hits today. The pool handed to MMR stays `_MMR_MAX_POOL`-bounded rather than `limit`, since the rerank reads each candidate's text.
- **Scoring-set invalidation**: the validity token is `(in-process generation, PRAGMA data_version)`. `_invalidate_episodic_scoring()` bumps the generation and is called by **every** writer that changes which rows are scored or what they score as — `write_episodic`, `delete_episodic`, `_delete_episodic_row`, `_enforce_episodic_cap`, `_retire_stale_episodic`, `reconcile_embedding_space`, and `backfill_missing_embeddings`. Two of those are traps a naive append-only cache falls into: the backfill rebuilds the FAISS index only `if _HAS_FAISS`, which is False on exactly the install this rung serves, and a body lookup can never repair it (it drops ids that vanished but cannot surface ids that appeared, so recall degrades with no error); and `PRAGMA data_version` is the only in-band signal that a SECOND PROCESS committed to the same file, and both the scoring cache and FAISS search check it. Persisted FAISS loading additionally verifies database and index-file digests. `_touch_last_accessed` is deliberately NOT a writer here — `last_accessed_at` is never scored and is re-read per search with the bodies. A ratchet test (`test_every_episodic_writer_invalidates_the_scoring_set`) fails on a new `episodic_memories` writer that skips the hook. The set is bounded by `_EPISODIC_SCORING_MAX_BYTES` (64 MiB, ~10 MiB for 2,600 rows at dim 1024) and is disabled outright on an sqlite with no `data_version` pragma; either way the rung falls back to reading the population per call.
- **V1 cap**: `_DEFAULT_EPISODIC_MAX` = 10,000 active entries, overridden by `memory.episodic_max_count`. For V1, `_enforce_episodic_cap()` tombstones `ORDER BY importance ASC, created_at ASC` (lowest-importance oldest first) on write once the count reaches the cap. The gateway passes the configured value as `episodic_max` when it builds the store, and `reconfigure` re-pushes it, so raising the cap stops evicting on the next write and lowering it trims on the next one — the key was parsed and dropped before, which silently pinned every install to the built-in 10,000. V2 bypasses capacity eviction and retains the stored episodes.

Episodic context retains `_DEFAULT_EPISODIC_LIMIT` = 8 results for explicit readers. Neither fresh nor warm V1/V2 session construction automatically queries episodic fragments. Agent retrieval uses `memory_recall`, whose response includes only rows fitting the tool's total cap, including wrappers. Explicit Python callers may still request episodic context through `MemoryStore.get_context(include_activity=True, query=...)`.

### Read-volume counters (`_ReadCounters`, `read_counters()`)

Six monotonic per-store-instance integer totals recording how much the store READ. They exist because a whole-population scan is otherwise **unobservable from outside the process**: a SELECT moves neither `PRAGMA data_version` nor the WAL, so a second process cannot tell one materialized row from a thousand, and wall-clock timing is not admissible evidence of a read-volume claim. Counting is unconditional (a method call and a few integer adds per SELECT) — only the EXPOSURE is a surface decision.

| Counter | Counts |
|---|---|
| `statements_executed` | SELECTs routed through `_fetch_all_locked` / `_fetch_one_locked`, all tables |
| `rows_read` | rows those SELECTs materialized, all tables |
| `semantic_rows_read` | rows materialized by whole-population **semantic retrieval** scans |
| `semantic_full_scans` | how many such semantic scans ran |
| `episodic_rows_read` | rows materialized by whole-population **episodic retrieval** scans |
| `episodic_full_scans` | how many such episodic scans ran |

The marked scan sites (`_fetch_all_locked(..., scan=...)`, plus one direct `record()` inside `_build_episodic_scoring_set`'s own locked block) are exactly the whole-population reads #8971 names: the V1 and V2 semantic candidate helpers used by `get_semantic_context`, `get_lessons()` unbounded (what the `_stored_similarity_scorer` callers score over), `_sqlite_vector_search`'s per-call read, the resident scoring-set build, and the V2 episodic candidate scan. A bounded read — V1 `get_semantic_context` with no query, `get_lessons(limit=N)`, any keyed lookup — contributes to the all-tables totals only, so a rising `*_full_scans` always means a population was re-read. On the episodic side both rungs land on the same counter, so `episodic_full_scans` rising with WRITES rather than with searches is what the resident set (#8956) looks like from outside; the semantic surface has no such set yet, which is #8971.

Semantics that matter to a caller: per instance and per process (two processes over one file report their own reads independently, never a shared total), never persisted, never reset, and no timing metric is recorded or derived. Every increment happens under `_db_lock`, so `read_counters()` — which takes the same lock — returns an untorn snapshot and no count is lost to a concurrent reader. Two identical `GET /api/memory/observability?q=…` calls with no write in between, compared field by field, are the intended probe.

#### The admission gate is a loose cut, not a tuned one

`_EPISODIC_RELEVANCE_THRESHOLD` is a binary classifier over (query, fragment)
pairs, so the only honest description of it carries both error rates and the two
cosine distributions it has to separate. Measured over the real
Qwen3-Embedding-0.6B GGUF and a real `VectorMemoryStore`, against the committed
50-topic corpus in `src/kiro_crew/eval/bench/admission_corpus.py` — each topic
stating one fact twice, once under the 300-char cutoff and once above it, so both
branches of the gate are scored on the same facts:

| | relevant cosine (n=50) | irrelevant cosine (n=2,450) | at the shipped gate |
|---|---|---|---|
| short, ≤300 ch, gate 0.55 | min 0.555 · p50 0.750 · p90 0.826 · p99 0.875 · max 0.875 | min 0.136 · p50 0.367 · p90 0.475 · p99 0.550 · max 0.617 | P 0.649 · R 1.000 · **F1 0.787** |
| long, >300 ch, gate 0.42 | min 0.452 · p50 0.671 · p90 0.755 · p99 0.840 · max 0.840 | min 0.143 · p50 0.337 · p90 0.434 · p99 0.512 · max 0.570 | P 0.132 · R 1.000 · **F1 0.234** |

Pooled, that is P 0.220 · R 1.000 · F1 0.360, over all 5,000 pairs. Recall is
1.000 in every view — the gate drops nothing relevant — while it admits 27 of
2,450 irrelevant short fragments (1.1%) and 328 of 2,450 irrelevant long ones
(13.4%). It is loose, not selective.

**The two distributions overlap, so no threshold value separates them.** Irrelevant
short cosines reach 0.617 while relevant ones start at 0.555; irrelevant long reach
0.570 while relevant start at 0.452. Every cut therefore either admits irrelevant
fragments or drops relevant ones, and the constant is choosing a point on that
trade-off rather than applying a boundary someone located. The best single value on
a 0.01 grid is 0.62 short and 0.57 long, both F1 0.958 at P 1.000 · R 0.920 —
perfect precision bought by dropping 4 of 50 relevant fragments. **That is a
finding, not a pending change**: moving the constant changes what is admitted on
every existing install, which is a behaviour change and is deliberately out of
scope for the measurement.

**An F1 near 0.98 is reproducible here and is not evidence of tuning.** Give each
query exactly one distractor — the 1:1 shape a "50 relevant + 50 irrelevant"
protocol describes — and the shipped gate scores F1 0.976 pooled (0.990 short,
0.962 long). But under that same balance *every* threshold from 0.51 to 0.58 scores
F1 ≥ 0.98 on short fragments, so such a number is consistent with any value in an
eight-step band and cannot have selected 0.55. A 1:1 benchmark is the wrong
instrument for this constant: it asks the gate to beat one distractor, while
`search_episodic` scores the query against every embedded row in the store. Report
the distributions, or the headline hides the overlap.

**The long-text relaxation over-corrects, and is the larger of the two errors.**
Dilution is real but small — the same fact stated long scores 0.079 lower at the
median (0.671 vs 0.750). The 0.13 relaxation is roughly twice that, and it ignores
the irrelevant class shifting down by a similar amount (median 0.337 vs 0.367, max
0.570 vs 0.617). The per-branch optima differ by 0.05, not 0.13, which is why long
fragments' precision is a fifth of short fragments'.

**The overlap is only a real finding if the labels are**, so the highest
wrongly-admitted pairs were reviewed by hand. The top short ones are a password-reset
window matched by a credential-rotation period (0.617) and a page-escalation timing
matched by an app-store review time (0.615): same shape, "how long until X",
different fact. That is the confusion a cosine gate cannot resolve, and the reason
a high cosine must not be read as relevance.

Every number above moves with the embedding model, so they are printed by the
harness rather than asserted by a test; a model upgrade is a reason to re-measure,
not a red build. The figures were produced with the `sqlite_cosine` backend, faiss
absent — the two vector rungs agree to float64 epsilon, so the rung does not move
the numbers. The measurement CLI that produced them is not kept in the tree; the
same pair cosines are scored by the `member_v2` harness (`ci.yml`'s member-recall
step, `scripts/ci-member-memory-benchmark.py`), whose report carries the admission
confusion per mode. The corpus itself is pinned in the default suite:
`test/test_episodic_admission_bench.py` runs `validate_corpus` over
`src/kiro_crew/eval/bench/admission_corpus.py`, refusing any fragment
`write_episodic` would drop (length bounds, the 300-char branch cutoff, injection
patterns, the 80-character text-hash dedup). Nothing substitutes the toy embedder
for the real model, which would turn a semantic threshold measurement into a
term-overlap one while still printing a plausible F1.

### Member memory experience and lifecycle

Global Memory is **V1**. Explicit member creation allocates a unique empty **V2**
store before publication. Automatic discovery registers agents on Global V1.
Existing members retain their exact V1 binding. Only explicit new member
creation provisions a member database; member edits never migrate a V1 binding
or create a replacement for missing memory. Existing V1 data is preserved.
The crew editor describes each member's memory ownership and states that V2 is
available only when creating a new crew member. Existing members retain their
current version and have no migration or provisioning action in the editor.
Canonical configuration records the immutable member/store IDs, and the database
identity validates those exact IDs. Display names, templates and workspaces do
not select the store; a member database cannot be shared or rebound. Ownership validation is specified in
[config](config.md#named-memory-stores-memory_storespy).

Both versions record accepted changes in additive revision metadata. Content
equality ignores the physical `updated_at` refresh, so an otherwise identical
semantic write does not append another revision. Full audit snapshots retain
both raw timestamps, and `created_at` remains part of record identity. Changed
content or metadata, and an explicit proposal resolution, still advance the
revision. V1's physical write and timestamp refresh behavior remain unchanged.

CLI and dashboard updates pass only their requested `changed_fields` to
`persist_member_config`, which merges them over the current member record under
the config lock. Concurrent edits to other fields survive. A newly provisioned
binding must be included and still passes all ownership checks. Creation refuses
every occupied member key, including null or malformed entries, with
`MemberAlreadyExists`; the dashboard preserves its `agent_exists` conflict response.

Member identity controls product routing. It does not provide an OS filesystem
confidentiality boundary. Owner-selected copying leaves the source unchanged;
raw agent file writes cannot modify managed SQLite state.

| User flow | Memory behavior |
|---|---|
| Ordinary assistant without a selected member | Existing Global Memory V1 behavior |
| Member DM, without Crew Mode | The owning execution record's member/store IDs; existing V1 bindings stay V1, explicit newly created members use V2 |
| Crew Mode | Each delegate retains its own binding. A target member is explicit and admitted by ordinary delegation/tool/app policy; omitted targets inherit the parent |
| Scheduled work | `member_id` pins the member separately from its provider template. Creation, firing and resumed chat validate the pinned store |
| Restart or continuation | The owning record retains the same frozen execution context |
| Unavailable or corrupt memory | Learned-memory operations report unavailability; manual essentials remain usable. No global fallback or replacement empty database |

The Memory tab has separate global and member views. Member links address
`/settings/overview?view=memory&store=<store>`; every member data request carries
that explicit owner-authorized store. The global settings and global vector
browser do not mount inside a member view. The member view supplies its own
compact identity header and a single Overview back action. It uses the app's
theme colors, the owning member's avatar, category
icons, three-line card previews and
reduced-motion-aware transitions. Full content and readable source/copy
provenance open in a detail dialog rather than filling the browsing surface.
The store list returns `owner_member` and `owner_avatar` from the same config
snapshot. Header, store picker, copy source and provenance reuse `CrewAvatar`
with the exact member name and validated override used by the roster. Uploaded
pictures keep their versioned member URL; generated avatars use the member name,
never the store UUID. Global V1 retains a separate database icon.

Memories, Profile and Recovery are separate tabs. Users can search and page
through facts, rules and episodes, inspect recall evidence, correct semantic
values, forget individual items, and edit that member's preference and project
documents. Search filters the entire selected store before pagination; the
browser retains the server's Unicode matching results. Rule correction preserves
structured rule metadata; structured facts require valid JSON. Unreadable
content is an error, not an empty editable document. Visited tabs stay mounted
to preserve drafts. Store switching, app navigation and browser unload guard
unsaved member work; a successful save removes that guard. Recovery keeps the
backup browser available even when the active member directory is missing.

Bulk editing pages every changed record in 25-row slices from the same signed
preview. Each page re-collects the signed selection and operation and must match
the original digest and expiry; it creates no server-side cursor or renewed
approval window. Paging failures keep the reviewed page and selection visible
but disable apply until the page succeeds or the selection is refreshed. Apply
always submits the original preview token. Forget actions name the operation and
selected count before the destructive request.
Forget includes Cancel before and after preview. It closes the operation without
applying it or clearing the current selection, and is disabled during a request.

Copy memories explains source preservation inside the dialog before selection,
keeps a visible Search label after the user types in its source filter, and
confirms the result after completion. Forget keeps its recovery explanation
visible before and after preview: there is no direct Undo in the editor, and a
backup containing the records restores the whole store after a gateway restart.
Provenance translates known bare source tags while preserving exact copied-item
keys and concrete conversation references. Legacy members show Memory V1.
Explicit new member creation provisions an empty SQLite database and preserves
all existing member and Global V1 bytes. Existing native context does not switch
member identity in place; a new selection requires a new session. Existing
members cannot opt into a replacement store through an update. No old V2
compatibility, migration or downgrade mechanism is provided.

Global and named V1 summary counts retain Semantic and Episodic labels. Member
V2 summaries show Facts and lessons and Experiences. Manage memory gives a
visible reason while unsaved edits disable navigation. The shared record editor
labels directives Lessons; the stored kind and API value remain `directive`.

Missing or mismatched database identity makes learned memory unavailable.
The canonical member still supplies manual persona, rules and project context;
it never selects Global or another member's learned memory as a fallback.

Run `kirocrew doctor` on the gateway host to diagnose an unavailable member
binding. Its Member Memory Bindings section checks every configured member
with the existing runtime binding validator, prints the member, store and
concrete refusal reason, and continues checking other members. Failed bindings
contribute to the command's issue summary and unsuccessful exit. This section
does not initialize, provision or repair memory; valid binding means the binding
checks passed, not a full database health or backend-capability assessment.
The command's existing configuration-loading behavior is unchanged.
SQLite identity reads can create transient WAL coordination files while
preserving the database and any committed WAL content.
The same section covers every V2 store still lacking `owner_member_id` (see
the upgrade below). One that the upgrade will repair is reported as pending
through its one bound member — the binding line reads "no member identity yet;
the next gateway start or CLI command upgrades it automatically" and the runtime
validator, which would refuse it today, is not run on it — so it contributes no
issue and a repairable store alone leaves doctor's exit clean. One the upgrade
refuses is listed as an issue and carries the upgrade's own reason and
`LEGACY_MEMBER_STORE_REMEDY`. Doctor never runs the upgrade itself.

#### Pre-identity member stores are upgraded at start

An earlier member-store layout recorded ownership as the `owner_member` label,
a `member-memory.json` manifest beside `memory.db`, and `owner_member`,
`private_memory_version` and `store_name` rows in `memory_meta`; the database
held the crew tables (`memory_items`, `memory_events`, `memory_meta`, the
revision tables and `schema_version`) but no `member_database` row, no
`memory_history`, `memory_consolidations` or `memory_fts`, and the store had no
`memory/` documents. Today's resolvers require `owner_member_id`, `member_id`
and the `member_database` row, so that shape is refused everywhere and no
runtime action can repair it.

`memory_stores.migrate_legacy_member_stores(config)` is the one repair. It runs
from `repair_legacy_member_stores()` at process start on both surfaces — the CLI
prologue in `cli.main` for every CLI subcommand (except `doctor`, which only
reports, and the `mcp-*` stdio servers, which are children of a gateway that
already ran it) and, for the gateway, its memory preparation worker after the
dashboard socket is accepting requests, before pending restores are activated
and before any consumer resolves a member. The `gateway` subcommand is exempt
from the prologue on purpose: the gateway boot path admits no new work before
readiness (see `AUTOSDE.yaml`, `no-new-work-on-gateway-boot-path`), and a
legacy store's lock and SQLite work would otherwise delay the moment the
dashboard is usable. Both calls are idempotent:
an install with no `memory_version: 2` record lacking `owner_member_id` takes no
lock and opens no file, and a repaired install finds nothing on the next start.
A config whose memory section degraded is left alone.

Under the store namespace lock, for each V2 record with an empty
`owner_member_id`, the upgrade proceeds only when every one of these holds, and
otherwise logs one warning naming the store, the reason and the remedy, and
skips it without guessing:

- exactly one Crew Member has `memory_store` set to the store, and that
  member's `member_id` is empty;
- the record's `owner_member`, when set, equals that member's alias, and
  `member-memory.json` is absent or its `owner_member` equals that alias; a
  malformed manifest refuses. Both labels are writer-populated and a rebinding
  can leave them naming another member, so two labels that agree with each
  other but not with the bound member are refused rather than adopted;
- `memory.db` exists, is a regular file with exactly one name (`st_nlink == 1`:
  a hard link would make the same inode another store's database too, and
  writing identity through this name would relabel that one) and holds
  `memory_items`; its own `memory_meta` stamps, when present, name this store
  (`store_name`) and the bound member (the old layout's `owner_member`), so a
  database copied or restored into another store's directory is refused by the
  labels it carried in; it either has
  no `member_database` row or has one whose `store_id` is this store and whose
  `member_id` is held by no other member or store (an interrupted earlier run
  resumes with that id); a row naming another store refuses. The file check is
  repeated immediately before the write, since the read and the write are not
  one open.

For an admitted store it allocates the `member_id` exactly as member creation
does (`_allocate_member_id`: the alias slug, uuid-suffixed on collision with
any `member_id` or `owner_member_id`), then in one SQLite transaction creates
each missing `MEMBER_SCHEMA_SQL` table, runs `record_meta.ensure_schema`,
ensures `schema_version` carries the crew version, inserts the
`member_database` row and makes the `schema_lineage` stamp `crew`; existing
`memory_items` rows are untouched, so lessons learned on the old build stay
readable through `open_member_database`. It then reads the identity back,
creates `memory/preferences.md` and `memory/projects.md` when missing, and
publishes `agents.<alias>.member_id` and
`memory_stores.<name>.owner_member_id` through `update_config_locked`,
re-checking the on-disk document (binding unchanged, no other identity
published, the id unclaimed) and keeping `owner_member`. The in-memory config
it was handed receives the same values. `member-memory.json` and the old
`memory_meta` rows are left in place. One store's failure is logged and never
blocks start or another store.

V2 labels owner changes as Edit and retained older experiences as Replaced
experiences. Recall explains which context the member would receive; the record
list remains available for browsing and editing. Included rules have a summary
and a details disclosure rather than an empty badge. Recovery attributes removed
older backups to the configured backup limit. The member header shows its member
ownership without repeating the picker's version badge.

The list defines Facts as saved details, Rules as working guidance, and
Experiences as recallable events. Narrow Explore memory results use wrapping
cards that retain memory type, content, member and channel. Restore keeps its
original control visible while a separate warning, Confirm and Cancel appear.

Choose starting knowledge is an explicit owner operation. The owner selects a
source and up to 50 fact, directive or episode identities. The server validates
the complete selection before writing, copies without overwriting target
identities, and reports imported/skipped outcomes with reasons and provenance.
No row is selected automatically. This is selective copying, not V1 migration.

V1 fresh-session context keeps complete preferences and eligible project-scoped
lessons. Project notebooks, decayed daily history and other semantic/episodic
facts are on demand through the store-bound `memory_recall` route; startup does
not invoke the three query-embedding paths. Warm follow-ups do not repeat startup
memory injection. The prompt-build embedding deadline remains a compatibility
guard for other contributors, not evidence that default memory performs inference.
The synchronous `ContextBuilder.build_message` call remains off the event loop
in the bounded `mc-embed` pool.
V2 context includes essential preference/project anchors and query-free,
project-scoped lessons. V2 prompt construction performs no embedding search or
episodic/semantic retrieval. Its runtime tells the agent to call `memory_recall`
for a changed topic or prior decision and to
use `learn_add` for corrections. The agent prompts (`config/prompt.md`,
`config/prompt-orchestrator.md`) give both versions the same order for a question
about the past: the injected block and lessons, then `memory_recall`, then
`search_chat_history` for verbatim transcript text. Both versions retrieve facts
and episodes explicitly instead of relying on activity ranked against a first
message. Retrieval is reference material and does not
override the current user's instruction. Forgetting removes a row from future
long-term recall; it does not erase text already in an active conversation.
Backup and staged restoration cover the entire member memory bundle, as
specified under [Automatic backups](#automatic-backups-memory_backuppy).

### Member V2 essential context

`member_essential_context.py` separates essential material from on-demand
fragment recall. The frozen execution context supplies the canonical member ID
directly; `member_config_for_id()` finds its unique configuration record without
opening the database or deriving identity from a store, template or display name.
The selected member and execution template both contribute their manual context.
V1 and unowned legacy stores retain their existing context path.

The owner persona and execution prompt share one project-first template resolver.
A distinct execution template contributes its admitted prompt, resources and
context settings to the same complete snapshot and digest. Its changes or source
removals refresh that snapshot without changing the canonical member. Sources
shared by both templates occur once; a source changing between their reads refuses
preparation. Conditional guides on a framework without native selectors have
an explicit activation path: a user `#guide` reference or a matching file path
loads the guide's full current text into the snapshot. A supplied user-text span
excludes generated prefixes from selection; a modify hook's replacement is the
effective request. Other conditional guides remain guarded discovery pointers.
Auto relevance and file paths first discovered through tools remain agent-driven:
the pointer tells the agent to read the complete file when its condition holds,
not to apply every conditional body unconditionally.
A project override of a template takes precedence over its global copy. A
relative `file://` prompt uses the project root when its template comes from the
project's agents directory, and the user home when it comes from the global
agents directory, even with a project bound. Both readers share the same path
validator: resolve symlinks and require the result to remain inside that resolved
root. Relative execution prompts retain that root through the no-follow byte
reader, which checks the opened descriptor's path against it. An ancestor swap
between resolution and opening cannot redirect that read outside the root.
UTF-8 decoding normalizes CRLF and CR like the essential reader; unreadable or
over-50-MiB execution prompt files are skipped, never truncated.
A `..` segment that stays inside is valid; an escaping traversal or symlink
is skipped with a debug log. Sensitive-path checks remain in force, and the
essential reader also retains its managed-memory source refusal. Absolute
`file://` prompts retain each reader's existing rules. An absolute declared
resource is accepted in either spelling of its declared root: the root is
compared lexically first and then in the resolved spelling the reader already
walks and reads under, so a resource recorded in its real path
(`/local/home/<user>/...`) is admitted under a `$HOME` reached through a symlink
(`/home/<user>`). The declaration itself is never resolved, and containment is
still required against one root. Neither relative reader
depends on the gateway process's working directory. When the
execution template is the owner's template, its custom persona appears only in
the per-turn essential envelope, not again in the session-start prompt. A
different execution template still supplies its task instructions. An inherited
exact product-prompt URI stays in the product session-start path, not in
essentials. Essential sources are validated on every member turn; their
complete snapshot is submitted only when the conversation needs it.

`ContextBuilder` injects the owner's identity, current permanent rules and
bound custom-template persona on fresh, warm, resumed, post-compaction and
minimal turns, including delegated and cron turns with no DM member argument.
An execution-template override supplies task instructions; it does not replace
the memory owner's persona. The generic product prompt retains its existing
provider/session-start path, including when a member's fork inherits it. The
loader recognizes the exact current `file://` URI selected by `_prompt_path()`,
not a template name or file basename. Package installs outside the user home,
development prompt overrides and the global user prompt override therefore do
not become project essentials or spend the essential envelope's budget. A
custom persona, including one on a template named `kirocrew`, remains essential
and passes the same file-admission checks as other declared sources. The member's
working briefing keeps its existing bounded reader; it is not
unbounded archival memory.

Admitted project essentials are the active project's root `AGENTS.md` and
`SOUL.md`, default/`always` documents under `.kiro/steering`, and Markdown file
resources explicitly declared by the template. Native `manual`, `auto` and
`fileMatch` steering retain their trigger semantics. A custom template's
declared prompt may be inline or a file source. Missing optional root files
are allowed; an unreadable declared source or malformed/shadowed template
fails with its name instead of silently substituting a different persona.
Template resources cannot import Global V1 memory or another member's state;
the owner's preferences/projects use the separately validated manual-document reader.
Declared globs have bounded enumeration and do not follow linked directories.
Wildcard-matched entries classified by the existing managed-source check are
excluded before descent or content reads. A broad `*/AGENTS.md` resource therefore
keeps ordinary project guides without scanning the workspace's managed memory or
lessons. Literal managed prefixes and explicitly named managed files still refuse;
other admission and read failures are not swallowed. Directory names alone do
not exclude an ordinary project outside the configured managed workspaces.
Containment is judged on resolved paths on both sides: a declared root (the
project root, or the owner's home for a resource outside it) is normalized the
same way an admitted document is, so a root reached through a symlink -- a
symlinked `$HOME` -- admits its documents, while a document that is, or sits
under, a link below the root is still refused. The managed-state isolation
(`_refuse_managed_source`) compares its admin and workspace roots in the same
resolved spelling, so it fires for resolved candidates on symlinked-home hosts
exactly as it does elsewhere.

These essentials are read and validated on every member turn. The builder
stages a complete snapshot on the actual serving provider, which suppresses its
wire envelope after successful consumption while its content, source list and
scope remain unchanged. Fresh, resumed and post-compaction conversations receive
a snapshot; changed content or scope receives one complete replacement, explicitly
superseding sources absent from its source list. Ordinary member chats, member
DMs, messaging, cron and delegated runs use the same provider receipt contract
specified in [providers](providers.md#essential-context-delivery-contract).
A provider-parametrized wire test covers both dedicated and shared adapters:
fresh delivery, warm suppression, observable clear/compaction, one complete
retransmission and renewed suppression. Only the model transport is simulated;
this pins the adapter/receipt pairing, not unobservable native history behavior.
Canonical identity and permanent-rule checks are not cached by a
receipt. A missing declared source still refuses; missing optional root guides
change the snapshot instead. There is no mtime-only content cache or automatic
retrieval on the warm path.

Structural-marker scanning copies contiguous ASCII segments without per-character
Unicode normalization. Non-ASCII characters retain the same normalization rules;
matching, original-text coordinates and span-local rewriting are unchanged. This
is a per-call computation optimization, not a cache of content or admission.

The receipt is an observable-event contract, not a native-history guarantee.
Suppression ends only on something the gateway can observe: a changed snapshot
content or scope, an invalidating event the provider yielded (compaction, clear,
agent switch), a history-discarding command the gateway itself dispatched, or a
client/process replacement. A native history loss that the backend performs
without emitting any of those is not detected. Unchanged warm turns then keep
omitting the snapshot, with no bound on how many turns, until one of the
conditions above requires delivery again. The gateway does not claim that such a
silent loss recovers on the next turn, does not periodically resend the snapshot
on a schedule or turn count, and does not assert that every native trim path
emits a notification; which paths do is recorded per backend in
[providers](providers.md#native-harness-notifications-relied-on).

They have a separate 64,000-character envelope, including wrappers and identity;
an over-budget or refused essential aborts context construction with a named
reason rather than truncating its tail. Ordinary session context yields space
first. On a small model window, this envelope can exceed the smaller ordinary
context allocation; it is not a promise that arbitrary-size documents fit any
model. No query embedding or episodic/semantic search runs during construction.
Owner profile saves validate the candidate preferences/projects together with
the member persona and configured workspace guides before replacing a file.
Both profile documents share a save lock, preventing concurrent saves from each
assuming the other's old size. An invalid save keeps the current files intact.
If the store disappears from configuration during validation, the save returns
`503 store_unavailable` and preserves the current document.
Turn-time validation still catches subsequently edited project files and names
the three largest sources when the complete envelope is too large.
Current member preferences/projects are included when memory context is
allowed. Explicit memory/project context exclusions and temporary-session read
restrictions continue to withhold their respective materials; permanent conduct
and owner identity still apply.

### Member V2 retrieval policy

`VectorMemoryStore.algorithm_version` reports `v1` or `v2`; `policy_revision`
reports `member-v2` for owned member stores. `memory_store_version()` selects
the policy from the canonical member/store configuration. The database integrity
identity must match that admission. Global and unowned legacy files keep V1;
there is no implicit creation, migration or schema reinterpretation.

Only V2 uses the automatic conflict proposal policy. A changed inferred value or
metadata patch becomes a durable proposal; model confidence cannot authorize an
overwrite. A new fact can carry metadata in its initial revision. Consolidation
can accept a correction only when the complete newest user message matches a
supported standalone replacement and the pre-extraction revision still matches.
Negation, hypothetical text, quotations, code and mixed prose cannot be stripped
from the evidence. Accepted English or Chinese replacements retain their actual
consolidation source. V2 deletion requests become proposals, and learned lessons
keep their consolidation source. Owner correction and revision controls remain
available in both versions.

V2 uses `memory_v2.py` for admission, task-term evidence and age-neutral
ranking. Its cosine cuts are **0.62 for short fragments and 0.57 above 300
characters**, the precision-first points in the committed real-model experiment
above. That experiment measured P 1.000 / R 0.920 at each cut; it does not prove
those figures for other models or real conversations. These are provisional,
model-dependent operating points, not universal calibrated boundaries.

The private episodic scan evaluates the complete active population
before applying the result limit and MMR. This avoids top-k starvation when a
tag filter or relevance gate matches only a few rows. It combines stored-vector
evidence with lexical evidence, including rows whose embedding is still NULL.
Lexical admission requires at least half the meaningful query terms and at
least two distinct matches when the query has two or more terms. NFKC and
casefold normalize terms; English function words are excluded; Chinese,
Japanese and Korean runs produce adjacent pairs so an entire sentence does not
become one indivisible token. Keywords can recover a row below the cosine cut,
so the complete policy must be measured separately from the cosine cut alone.
V2 semantic values are JSON-decoded before lexical matching and rendered with
readable Unicode in bounded recall context, including nested values; storage
escapes do not hide Chinese facts or consume their budget as escape sequences.

Ranking combines cosine and query coverage with weights of 0.7 and 0.3,
and modest importance weighting. A missing vector contributes zero cosine;
its lexical contribution still has weight 0.3. Age never
changes admission or score, including when the shared V1 decay configuration
is nonzero. V2 does not automatically evict existing episodes at the V1 capacity
threshold; writes and explicit copies retain their content. Persisted count can
grow, while result limits, MMR pools and recall context remain bounded. These
weights are explicit product policies,
not measured optimal parameters. Each result includes `retrieval` evidence:
policy revision, admission reason, cosine, applied floor, matched terms,
coverage and age; source and copy provenance remain available on the row.

V2 recall also returns `retrieval.operating_point`, including for empty results.
Its status is always `provisional`: the 0.62 short-text and 0.57 long-text floors
are corpus-informed settings, not a calibrated guarantee. It reports the floor
values and 300-character boundary, plus reference, active and stored embedding
signatures. `stored_matches_active` compares declared model ID and dimension
only; it does not establish identical weights or retrieval quality.

Qualification is `reference_identity` only for a bundled factory backend whose
declared identity matches the measured Qwen3 0.6B/1024 reference. Custom models,
registered backends and caller-supplied embedding functions remain
`custom_or_registered`, even if they declare the reference identity. Missing
identity is `unknown`. The owner UI explains that custom or unknown models have
not validated these recall settings. This snapshot reads the existing backend's
identity without constructing a model, reading model files, checking readiness
or running inference. It neither changes admission nor loads an embedding model
for diagnostics. The metadata stays within the existing transport budget, and
V1 keeps its existing response shape.

Semantic V2 context retains private `pref.*` directives and requires relevant
evidence for query-selected facts. Overlong entries are skipped so smaller
useful entries still fit. Lesson exact-rule enrichment remains available, but
V2 does not delete a distinct rule on substring, topic overlap or cosine alone;
corrections to such rules explicitly address their key. Owner-selected seeds
are protected from automated overwrites, as explicit owner facts are.
Episode writes deduplicate complete identical text; sharing a prefix or a high
cosine never rejects or merges a distinct V2 episode. Retrieval MMR handles
redundancy without destroying source information.

Explicit forgetting, validity intervals and evidence-backed correction
supersession remain effective. They represent user intent or a fact's actual
validity, rather than an inference that old information is unimportant.
The V2 lesson writer also skips the background model contradiction sweep:
a model's guessed contradiction cannot delete a distinct existing rule. V1
keeps its existing sweep; exact-identity proposals and explicit V2 correction
and review continue through the revision-aware write path.

`recall(query_text, cap=3000, project_dir=None)` is the on-demand entry point
for both versions, using the selected store's retrieval policy. Each nonempty
recall computes its query embedding at most once and passes success or failure
to fact, episode and lesson retrieval. The result carries the store's
vector-space generation and recorded signature across those reads. Checks run
under the store lock before vector-bearing reads and before publication;
inference never runs while holding that lock. A changed space discards the
complete partial result and performs one keyword-only retry, without another
inference. Failure is retained only for that call, not in a negative cache.

The store and dashboard return bounded semantic, episodic and lesson context
plus selected snippets and retrieval evidence, never unabridged source rows or
vector blobs. Truncated evidence is marked; full content remains available to
the owner editor. The character cap includes wrappers; an empty query or zero
cap yields no context. A separate 16 KiB transport budget counts JSON escaping
and the MCP TextContent envelope, with 1 KiB reserved for ordinary RPC
framing/request IDs. Caller-controlled arbitrarily large JSON-RPC IDs are
outside this memory-data bound. The MCP boundary uses a model-facing projection:
each body appears only once in its trusted reference context, evidence retains
identifiers, scores, provenance and truncation flags without another body, and
previews are omitted. Dashboard/UI payloads retain their existing shape. The
final serializers recheck their actual representation after redaction,
shortening snippets or omitting whole tail records and regenerating matching
contexts and counts when necessary. Omitted records are reported in retrieval
metadata. Episodic references carry their stable memory ids. The MCP transport
derives the store from the caller's trusted binding; it accepts no model-selected
store argument.

HTTP recall has one nine-second server-side monotonic work deadline, shorter
than its ten-second MCP client timeout. Retrieval uses a bounded `mc-recall`
pool and separate admission from prompt preparation's `mc-embed` pool. The work
budget follows the worker into native inference. Expired or cancelled queued
jobs are removed without inference; a claimed native call cannot be interrupted
in-process and retains its executor worker and admission until it completes.
This change does not add process isolation or guarantee prompt progress when a
prompt itself requires the same wedged native model. The registered
`api_memory_recall` handler applies `memory_recall_deadline` before authorization,
cold store opening, admission and retrieval; expiry returns HTTP 504 with
`memory_recall_timeout` without changing caller-authority checks.
`get_context_preview()` uses the same result for V2 and keeps V1's response.
The `member_v2` harness report also carries the V2 classifier's admission confusion
per mode on the same measured pair cosines, separately from its ranking and
context-budget results.

The separate `python -m kiro_crew.eval.bench.member_v2 --model-path <existing.gguf>
--json <report.json>` harness measures the complete shipped candidate scan,
ranking, MMR and bounded recall against all 100 fragments of the 50-topic
committed corpus. It uses an isolated data home and disables downloads. The
archived [historical V2 snapshot](https://github.com/kirodotdev/KiroCrew/blob/7d14fbf73706e3b659be5840c382960553a40425/temp-screenshots/memory-v2/pre-retention/member-v2-hybrid-qwen3.json)
predates the retention-policy change and records the corpus/model SHA-256, policy revision,
per-query selected evidence and limitations. Its product labels are normalized
to `member-v2`; provenance preserves the original artifact SHA-256 and explicitly
states that measured values and original source seals are unchanged. Its exact
source blob `5d41bdd7486b8548cfa01d1d4c93db57488e5689` is preserved in the evidence
branch; it is not duplicated beside the executable benchmark. The rerun after the
retention change is not checked in: the `backend-test-sandbox` job's member-recall
step (`scripts/ci-member-memory-benchmark.py`, `ci.yml`) publishes each run's
`member-v2-hybrid-qwen3.json`, `provenance.json` and `run.log` as the CI artifact
`member-recall-qwen-<sha>` with 30-day retention;
run metadata and source hashes distinguish snapshots without creating product
subversions. The execution metadata was added during publication, separately
from the measured payload. Original Windows CRLF seals and canonical Git LF
hashes describe distinct byte representations. The following figures are
historical: changes to missing-vector ranking or transport budgeting require a
fresh run and cannot inherit these scores. With the
existing 1,024-dimensional Qwen3 model, full vectors gave admission precision
93.94%, fragment recall 93%, context macro precision 93.67%, topic hit 98% and
nDCG@8 0.9274. With all 50 short-fragment vectors absent, topic hit was 96%;
without any embeddings it was 74%. All 900 context-cap checks passed. Separate
Chinese/Japanese/Korean fact and episode fixtures and forgotten-row exclusion
passed. The archived JSON records the earlier oversized-row skipping check; the
live harness requires a bounded snippet with its ID and explicit truncation
flag. CI run 34340819757 on `60fccbad` passed both oversized structural scenarios,
each with all eight predicates, and reported no structural failures. Its complete
payloads and source provenance are retained in the
[evidence archive](https://github.com/kirodotdev/KiroCrew/tree/09968d7f5bcae6080b0f9df7066335ba0be93812/temp-screenshots/memory-v2/ci-60fccbad-benchmark);
the algorithm effectiveness
report's section 14 records the verified current-run counts separately from the
historical figures above. This is corpus-informed retrieval evidence,
not held-out calibration, generated-answer quality or a production guarantee;
partial/no-vector runs quantify degradation, and the CJK cases are structural
checks rather than a multilingual quality benchmark.

`seed_item_if_absent()` copies only the owner-selected row into a private V2
destination. Semantic/directive writes and provenance stamp commit together;
episodes use preserve-existing deferred writes and refuse success without a
saved provenance stamp. Source vectors are not copied. `derived_from` stores
the source store, item id, kind, source classification and copy timestamp; the
new row's source is `user_seed`. Existing and tombstoned target identities are
not overwritten. API authorization precedes this store-local helper. V2 semantic
and episodic list readers return the canonical row's source and provenance
facets, so the dashboard retains copy origins across pagination and restarts;
the V1 list response shape remains unchanged.

Copying validates the complete source selection before its first write. Each
item then commits independently. If storage fails mid-batch, the response keeps
earlier `imported`/`existing` results, marks the failing item `unconfirmed` (its
commit may already have happened), marks remaining selections `not_attempted`,
and returns `partial: true` with per-item reasons. The owner can refresh and
retry without overwriting target memory; a generic failure never hides earlier
completed copies.

Episode copy retries compare persisted source identity even after the owner
corrects or forgets that copy. Retrying the original selection cannot restore
the previous text or resurrect a forgotten row. Owner corrections use
`POST /api/memory/bulk/preview` and `/api/memory/bulk/apply` for both V1 and V2.
A single episode's content edit retains its id, creation time, tags, importance
and provenance. Apply records the before/after event and revision atomically,
sets the V2 source to `user_explicit`, and clears the stale vector when text
changes. Invalid text and stale record revisions refuse without mutation. The
signed preview binds the store and selection; retrying an applied preview
returns its persisted receipt without another edit.

### Supersession retirement, and why it is bounded

`_retire_stale_episodic` dispatches by algorithm version. Global V1 retains its
original similarity/exact-phrase heuristic and its original audit values; the
heuristic decides WHICH episodes a write may retire. Two bounds are shared by
both versions: the per-write ceiling below, and the trigger itself -- an
unchanged semantic value (value-level JSON equality) retires nothing on either
algorithm, because the episodes it would retire restate the still-current
value. On V1 the ceiling is ONE budget across the vector arm and the text
fallback, spent by the vector arm first (its `limit=50` pool is a search width,
not a retirement width), and the fallback's `LIMIT` fetches only what the
remaining budget can retire. The remaining bounds below apply to private V2.

Three bounds make it acceptable, and each is pinned by
[`test/test_episodic_retirement.py`](../../../test/test_episodic_retirement.py)
(V2) and the V1 cases in
[`test/test_member_memory_algorithm.py`](../../../test/test_member_memory_algorithm.py):

- **An assertion linked to the full semantic key.** A candidate clause must
  start with the full key and its value assignment, for example `pref.color:
  red` or `pref.color is red`. A shared attribute alone cannot identify whose
  fact changed: `Bob's color is red`, `project.color: red` and an unqualified
  `color: red` remain active when the owner changes `pref.color`. Normalization
  ignores case and JSON quotes; word boundaries keep `redwood` from matching
  `red`. Negation, historical markers and uncertain paraphrases stay active.
  This conservative rule does not call an embedding model.
- **A per-write ceiling**, `_MAX_EPISODIC_RETIRED_PER_WRITE` = 3, on both versions. A
  candidate beyond the
  cap stays **alive**: a stale episode is outranked by the newer semantic row that
  contradicts it, while a wrongly retired one is invisible to every reader, so the
  overflow direction is "keep" and the cap drops the DELETE rather than deferring it.
- **Reversibility, which is the only reason a heuristic may delete at all.** Nothing in
  the module ever hard-deletes an episode — the sole hard `DELETE` is on `memory_events`
  — so a tombstoned row keeps its id, text and vector. `get_retired_episodic()` lists
  them newest-first with the semantic key that superseded each (carried in the
  `conflict_retire` event's `new_value`, since `memory_key` must hold the episode's id
  for the listing to join on it), and `restore_episodic()` clears the tombstone in
  place. Restoring rather than re-inserting is deliberate: a new row would look like a
  new memory and would re-enter the similarity dedup that may have removed it.
  Both V1 and V2 invalidate resident NumPy scoring and FAISS populations after
  the restore commits, so the same running store recalls the restored row even
  when its caches were built while the row was retired.
  The dashboard also invalidates the restored store's live record queries.
  Global uses the canonical `default` query key even when its API store argument
  is empty, so its vector browser and statistics refresh after restoration.

The listing keys on the `conflict_retire` / `semantic_update` event pair rather than on
`is_deleted` alone, so a user's own delete is **not** offered for restoration — the two
deletions mean different things and only one of them was a guess.

Reversibility is only worth what its surfaces reach. `GET /api/memory/retired` and
`POST /api/memory/retired/restore` take the shared `?store=` / `"store"` field, so the
operator can undo a retirement in a SILO — the store where a wrong retirement is least
visible, because nothing else reads that file. `kirocrew memory retired` has no
`--store` and runs on the default store alone: it opens `_memory_cmd`'s shared store,
which is hardwired to the default path, so the two surfaces have deliberately different
reach and the CLI is not the recovery path for a silo. `memory export` and
`memory import` are the exception, and the only one: they take `--store`, reach a silo,
and resolve their own path rather than reading the one the shared open composes.

### Automatic backups (`memory_backup.py`)

**Member V2 backup covers its database and manual anchors.**
`member_memory_backup.py` creates an ordinary ZIP snapshot containing an online
SQLite backup of `memory.db` plus present `memory/preferences.md` and
`memory/projects.md`. All learned state, FTS and vectors are transaction-consistent
inside the database. Manual files are individually read and hashed. No learned
Markdown history, JSONL lessons, separate FTS database, old manifest or derived
FAISS files belong to the bundle. Configuration and unrelated files are excluded.

The versioned snapshot manifest records store identity, exclusive owner,
creation time and SHA-256 for each allowed file. Backups live in
`memory_stores/.member-backups/<store>/`, inside managed storage
but outside the member directory that restoration replaces. The ordinary
retention count and interval apply to ZIP snapshots independently per member.
Unique names preserve multiple snapshots taken at the same instant. Listing,
display timestamps and retention also recognize older second-resolution ZIP
and Global V1 database names.

V2 restoration validates the owner, format, file inventory, size bounds,
checksums and SQLite integrity before publishing a pending restore. Unsafe
paths, duplicate ZIP entries, links, undeclared files and foreign-store
databases are refused. Extraction writes allowlisted files into a fresh private
stage rather than using archive path extraction. Limits are 8,192 files and
1 GiB uncompressed; the manifest itself is bounded separately.
The database format and stable member/store identity must match before snapshot
creation, staging or activation. Unsupported old formats are refused.

**A restore request changes no live memory.** It returns `pending: true` and
`restart_required: true`. Only `apply_pending_member_restores()` at the gateway
startup barrier, before any memory is opened or served, activates it. Ordinary
CLI reads and `VectorMemoryStore.init()` do not activate pending restores.
`kirocrew memory restore --store <member-store>` reports that the restore is
staged and requires a gateway restart; it does not report live restoration.
Activation preserves the entire old member directory under a unique
`superseded-*` name, switches the staged directory into place, and rolls back
a failed switch. Preserved `superseded-*` directories are recovery copies outside
`backup_keep`; they require explicit owner cleanup after recovery is confirmed.
Their UUID names do not establish a safe automatic deletion order. An external
journal allows startup to finish an interrupted
switch or recognize one completed before journal cleanup. Unrecoverable
activation keeps the affected store unavailable with an actionable error; the
worker continues restoring other stores. Once preparation finishes, healthy
Global and member stores remain usable. The dashboard remains available for
owner recovery. It never substitutes global memory or
serves an incomplete restore. A second pending restore is refused so it
cannot silently replace the owner's earlier choice.

The dashboard socket and `KIROCREW_READY` boundary are published before this
potentially data-sized preparation pass runs. The gateway first publishes its
single tracked preparation task onto `DashboardState`, emits READY without an
intervening await, installs shutdown signal handlers, and supervises that task
alongside the owner shutdown event before arming cron, heartbeat,
automatic memory work or channel transports. Persisted Crew work and restored
legacy channel agents resume after that wait. Dashboard status and recovery
routes remain available, while memory content routes return their existing
fail-closed 503 during preparation. Every agent-backed dashboard turn waits up to
30 seconds on the same task at the central pre-turn admission seam, before expiring controls,
publishing the turn identity, resolving memory ownership, allocating a provider
or writing metadata. A deadline returns a retryable `memory_unavailable` refusal
without recording a session failure or consuming queued intent. The wait is
shielded: a deadline or the user's Stop action cannot cancel shared preparation.
A later admitted turn retains normal first-turn memory context. Local dashboard commands
that return before turn admission remain available. Preparation keeps restore
activation, store opening and the full Global FTS rebuild in one barrier; a
completed store-scoped failure releases healthy stores and retains the failed
store's permanent refusal rather than recording a transient failed turn.
Completed structural failure is not readiness: memory-dependent consumers stay
dormant and the owner recovery shell remains available. Owner shutdown, including
signals, uses the existing bounded cleanup path without awaiting the restore
thread. The worker's filesystem or SQLite call itself is not interruptible;
its fence and late cleanup remain authoritative until it exits, with process
hard exit as the shutdown backstop. This bounds turn admission, not restore duration.
During this wait, recovery uses the known dashboard address or `kirocrew token`;
automatic browser launch still follows successful preparation.

`GET /api/memory/backups` persists this state across browser remounts via
`pending`, `restart_required` and `pending_restore: {backup_name, staged_at}`
(or null). Owner-only `POST /api/memory/restore/cancel` with `{store}`, or
`kirocrew memory restore --store <member-store> --cancel-pending`, revokes a
not-yet-activated restore without changing live memory or deleting its backup.
The pending surface names Kiro Crew (the gateway), says that a restart finishes
restoring the backup, and distinguishes the whole memory store from its individual
memories. It also says that restarting briefly interrupts active conversations
and scheduled work, and groups the restart link and cancellation action with
visible spacing.
No pending restore is an idempotent success. The shared activation lock excludes
racing startup; cancellation refuses after a tree has been displaced. Removing
the journal is the atomic cancellation point, followed by best-effort cleanup
of the verified stage; a cleanup failure can leave an inert temporary
tree but cannot leave a partially deleted snapshot scheduled for activation.
After ordinary cancellation or completed startup, GET returns false/false/null.
If recovery already failed at startup, cancellation clears the pending intent
but retains `activation_failed`, `restore_error` and `restart_required: true`.
That store stays unavailable until restart. The owner can stage a known-good
backup after preparation completes, including when the retained database is
unreadable. Staging does not activate the backup or clear the failure. Status
and cancellation remain available while content is fenced. A Global failure
also leaves the dashboard status shell available, with an unavailable lesson
count represented as null. Configuration failures that prevent a safe restore
pass retain the installation-wide preparation fence.

If the entire member directory is missing, explicit owner restore may recover
it using exclusive configuration ownership plus the matching backup manifest.
The journal records whether a prior directory existed when restoration was
staged; only an explicitly missing original permits activation without a
preserved prior tree. An existing directory with a missing or mismatched database identity is refused. This recovery exception never relaxes ordinary memory reads.
Restore refusal responses retain the concrete reason (`restore_refused`), including
an already-pending restore, rather than labeling every refusal as corruption.

**Global V1 keeps its single-DB backup format. Restore activation is staged for
both versions so a live writer never loses its database or WAL.**

Both pending-restore journals use the shared `atomic_write` helper with
`restrict_to_owner=True`: a unique temporary file is restricted before any JSON
is written, then atomically replaces the journal. Permission failure publishes
no new restore intent and leaves any existing journal and live memory intact.

Memory is the only data here that cannot be rebuilt from another source: config can be
retyped and sessions replayed, but a superseded preference nobody remembers stating is
gone. Every active store gets a daily rotating hot copy: the default store first, then
declared named V1 stores and actively owned member V2 stores. The default store is
never left to a manual copy, because it is the one every install has and typically
the largest. The heartbeat schedules its
first pass at the first eligible tick after memory readiness, then uses its
existing daily tick cadence and per-store freshness checks. One tracked task
runs the serial copy pass in `maintenance_executor`; a tick never waits for a
whole archive and cannot start overlapping passes. Shutdown signals the worker
to stop before another store or pruning. An atomic copy already in progress
may finish before the worker exits.

**SQLite's online backup API, never a file copy**, and the difference is the whole
feature. The gateway holds the store open under WAL, so copying `memory.db` alone
captures a file whose committed tail lives in a `-wal` sibling that was not taken — and
the result *parses*, so nothing complains; it is simply missing recent writes. The backup
API walks a consistent snapshot with the writer still running and emits one
self-contained file with no WAL to pair.

- **Placement**: beside the store they came from, in a `backups/` directory, so a silo's
  backups inherit the fence that silo already sits behind and no new sensitive-path entry
  is needed. Files are owner-only.
- **Naming**: `<stem>.<UTC microsecond stamp>-<UUID>.db`; older
  `<stem>.<UTC second stamp>.db` names remain listable and restorable. Retention
  orders both formats by their parsed stamp rather than mtime. A copied or restored
  file carries a new mtime while its name still says when its contents were taken.
- **Atomicity**: each call writes its own hidden UUID `.partial` and renames it, so an
  interrupted or concurrent run cannot delete another call's stage or leave a
  truncated file that looks like a backup.
- **Retention**: `memory.backup_keep` (default 7), clamped to at least 1. A retention
  policy that can empty the directory is a scheduled deletion, not retention.
  The loader preserves this value and `memory.backup_enabled` (default true)
  across reload/save, so disabling automatic backups or extending recovery
  retention survives a gateway restart. Automatic retention prunes only the
  backup directory of the store it just copied and never touches unreferenced retained stores.
  Manual dashboard backups use the same configured retention in their worker.
- **Enumeration**: the heartbeat and the `kirocrew memory backup` command share one
  helper that visits the default store, declared named V1 stores and actively owned
  V2 stores. Neither uses a glob of `memory_stores/`: a glob
  would adopt an abandoned or restored directory the operator never declared and then
  copy it forever. Each resolved path is confirmed to belong to the store that asked for
  it, independently of strict binding resolution.
  Unreferenced retained files remain untouched. Routine maintenance excludes
  stores without their active canonical member binding. Restore requires the
  configured member/store identity; no retirement marker or archive reattachment
  framework exists.
- **Fail soft per store**: one unreadable store must not prevent another eligible
  store's backup, so the loop counts failures instead of propagating them.

**V1 restore is staged and recoverable.** `restore_from_backup` requires the
source's structural V1 lineage for Global. Named V1 also accepts the established
unowned crew-schema shape. Both refuse a `member_database` identity, empty
databases and unrelated SQLite files without publishing a restore journal or
changing current memory. It uses SQLite's online backup API to stage a
self-contained copy, verifies integrity and the same source admission on that snapshot, and
publishes its checksum in `pending-v1-restore.json` in the store's backup
directory. Live memory stays at its original path and continues accepting writes
until shutdown. The same
startup barrier used by V2 activates V1 before opening memory: it preserves
the current database and any `-wal`/`-shm` together under
`memory.db.superseded.<unique-id>` and matching sidecar names, then installs the
stage. This preserves every acknowledged write before shutdown, including
uncheckpointed WAL commits and writes made after staging. A corrupt current
database is preserved byte-for-byte for recovery too.

The journal records each required original component before any move. Failed
installation rolls the originals back; an interrupted move or completed
installation whose journal remains is recoverable at the next startup.
After a complete rollback, retry refreshes the current main/WAL/SHM inventory
because a normal SQLite open can checkpoint and remove sidecars. It does this
only while the stage is intact and no original components remain displaced;
partial moves retain their recorded inventory and a missing prior main refuses
activation.
Activation failure fences the affected store, retaining the journal, stage and
prior data. The dashboard stays available and healthy stores become usable after
the preparation pass completes.
V1 uses the same pending/restart-required response, persisted status and
owner-only cancel action as V2. Cancellation is allowed before activation
starts and does not change current memory or the source backup. The CLI reports
staging and the required gateway restart for either version.
Cancelling an unreadable V1 journal accepts a canonical home-parent alias while
still refusing redirected database or journal leaves. Current database integrity
and the absence of displaced originals remain required before cancellation.

**Three surfaces, one set of primitives.** The heartbeat's own tick, the
`kirocrew memory backup` / `backups` / `restore` verbs, and the dashboard's
`POST /api/memory/backup`, `GET /api/memory/backups?store=` and
`POST /api/memory/restore` all call `back_up_all_stores` / `list_backups` /
`restore_from_backup` rather than reimplementing the copy, the retention order or the
integrity check. The dashboard surface adds exactly two rules of its own, both because
its caller is a browser: a backup is named, never pathed (a path discloses the
data-home and `memory_stores/` layout), and the name is resolved inside that store's
own `backups/` directory with the resolved path re-checked for containment, so a
caller-supplied filename cannot walk out of it. Which store each surface may address is
[the shared `?store=` rule](#which-store-a-dashboard-route-reads-store) on the
dashboard and `--store` on the CLI.

`kirocrew memory backup` / `backups` / `restore` are dispatched **before** the vector
store is opened, and that ordering is the point: `store.init()` runs
`PRAGMA journal_mode=WAL`, which raises `file is not a database` on exactly the corrupt
file these verbs exist to recover. Opening first would make the recovery path unreachable
in the only situation it is for. `carve` dispatches ahead of it too, for the other reason a
verb can need to: it opens the store NAMED on the command line, and the shared open is
hardwired to the default store's path.

**A named store's path is admitted by `_admitted_store_path`, and every new call site
must take it.** The cause of silent recreation is `VectorMemoryStore.init`, which creates
whatever path it is handed, so a guard at one caller protects that caller alone. Any
component that opens a store by name — a future CLI verb, a route, a worker — is its own
recreation hole until it resolves through `_admitted_store_path(store, cfg, may_create=)`,
whose `may_create=False` is the read contract: a name whose database is absent raises
rather than bringing one into being. Moving the must-exist check into the store open or
the resolver, so the guarantee holds for callers that forget, is tracked in #11777.

### V1 fading: three independent decay mechanisms

These mechanisms apply only to V1. V2 retains full history and episodic content
without age-based downranking or automatic capacity eviction.

Three unrelated mechanisms keep stale memory out of the V1 context budget. They do
not coordinate, so reason about them separately:

1. **History decay (time tiers)**: `memory.py` `read_recent_history()`, table
   above. Cheap, deterministic, no scoring.
2. **Episodic decay (exponential, at query time)**: the score formula above.
   At the default rate, `exp(-0.03 × days_old)` halves at ~23 days and reaches
   ~10% at ~77 days; a per-tag rate from `memory.decay_rates` shifts that curve
   per memory (0 = never ages out of retrieval ranking, 1 = out of retrieval
   within about a day — ranking only: cap eviction below still applies);
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

Embeddings run in-process via the vendored llama-cpp-python 0.3.34 runtime (`kiro_crew/_vendor/llama_cpp`) — no external server, no HTTP hop, no runtime pip install. There is no remote embedding URL, so no URL validation or SSRF hardening is needed on this path; see `../post-launch-removals.md` for why a network embedding client must not come back.

- `LlamaCppEmbedder.embed(text)` / `embed_batch(texts)` → returns 1024-dim vectors or `None` on any failure (graceful degradation)
- **Non-blocking model load**: the GGUF load runs on a background daemon thread (`_kick_background_load()`, thread name `kc-embed-load`) — `embed()`/`embed_batch()` NEVER block on the load. When the model isn't in memory yet, the call kicks the background load and returns `None` immediately; memory degrades to keyword search until the load lands. The gateway/dashboard event loop is never stalled by embedding work. `wait_ready(timeout)` exists for sync contexts (tests, one-shot CLI flows) that legitimately want to block — never call it from an event-loop thread
- The underlying `Llama` object is NOT thread-safe — inference on a loaded model is serialized behind a lock (tens of ms per short text)
- `get_shared_embedder()` — process-wide singleton (~700MB RSS when loaded), shared by vector memory AND the knowledge library; `close()` unloads the model to free RSS
- **Bounded llama.cpp scratch memory**: the accepted context and logical batch remain 2,048 tokens, while the physical decode micro-batch (`n_ubatch`) is 512. llama.cpp splits a long input across those physical batches before applying last-token pooling, so the complete context still contributes to one vector. Against the shipped Qwen model, a maximum 6,000-character input produced byte-identical 1,024-dimensional vectors at 512 and 2,048 (`cosine=1.0`, max absolute difference `0.0`); 512 reduced Linux peak/resident RSS by approximately 419 MiB for that pass. Do not lower `n_ctx` or `n_batch` as a memory shortcut: either would reduce the semantic input the model can accept.
- Per-platform native libs live in `_vendor/llama_cpp_libs/{linux_x86_64,linux_aarch64,macos_arm64,macos_x86_64,win_amd64}`, selected at import time via `LLAMA_CPP_LIB_PATH` (upstream-supported override; an operator-set value wins, enabling e.g. a GPU build). Before loading the bundled Linux x86_64 runtime, `_load_llama_class()` intersects the `flags` reported for every visible processor in `/proc/cpuinfo` and requires the baseline compiled into the shipped upstream wheel (AVX, AVX2, BMI2, F16C, FMA, SSE3, SSSE3). A missing or unreadable feature list refuses the native runtime before it can raise an uncatchable SIGILL; memory stays available through keyword search. The gate does not apply to an operator-set `LLAMA_CPP_LIB_PATH`, because that directory may contain a lower-baseline build. Unsupported platforms, incompatible bundled CPUs, and import failures all degrade to keyword-only memory search. See `_vendor/README.md`
- **The shipped closure is declared, not inferred.** `_REQUIRED_VENDORED_LIBS` names the exact files each platform must carry, and `verify_vendored_libs(root=None)` returns `{platform: [missing…]}` (empty when complete) against a source tree, an unpacked sdist, or an installed wheel. `_load_llama_class()` consults it before importing, so an incomplete install is reported as a **packaging defect naming the absent files** rather than surfacing as ctypes' `Shared library with base name 'llama' not found` — which reads as an unsupported architecture and misdirected the real-world diagnosis of this bug. `kirocrew doctor` prints the same detail. The check is **skipped when `LLAMA_CPP_LIB_PATH` is set**: the libs then load from the operator's directory, so the bundled tree's contents no longer determine whether the runtime works, and refusing on them would disable the documented override for exactly the users an incomplete wheel stranded (the warning names the env var as a remedy for that reason). Each packaging lane selects these files by a different mechanism (MANIFEST.in for the sdist, `package_data` for the wheel — which the desktop bundle inherits, since it pip-installs the project into its bundled interpreter), so each is guarded independently in `test/test_vendored_llama_payload.py`, and both `build.yml` (every PR) and `build-wheel.yml` (release/nightly) re-check the built wheel **and** sdist against the same declaration via the shared `scripts/verify_vendored_payload.py` (one script for both lanes, so they cannot drift into a gate that stops guarding without failing) — the sdist explicitly, because `python -m build --wheel` never evaluates `MANIFEST.in` and so cannot see an sdist regression at all. Linux ships no BLAS backend by design: upstream publishes none in its Linux CPU wheels (macOS gets `libggml-blas` only via the system Accelerate framework), and the Linux `libggml-cpu` carries the optimized GEMM kernels instead
- **The artifact verifier needs only the Python standard library.** `scripts/verify_vendored_payload.py` reads `_LIBS_DIR_NAME` and `_REQUIRED_VENDORED_LIBS` from the source with `ast.parse` and `ast.literal_eval`. It never imports the embedding runtime or its config dependencies. Both constants must stay literal top-level assignments; a missing or computed declaration fails the gate. Tests run the real script with `python -I -S`, checking complete archives and missing members in the wheel, sdist, or both.
- Failed model loads (corrupt file, bad native libs) are retried only after a 300s cooldown so a broken state can't spawn a loader thread per embed call

**Embedding backend abstraction** (`EmbeddingBackend` ABC): the public swap seam for future runtimes (Ollama again, remote endpoints, ONNX) and user-defined models. Surface: `model_id`, `dim`, `is_ready()`, `embed()`, `embed_batch()`, `close()`. Consumers (vector memory, knowledge library) depend only on this interface; everything llama.cpp-specific lives in `LlamaCppEmbedder`. Swap flow: `register_embedding_backend(factory)` + `reset_shared_embedder()` replaces the singleton (pass `None` to restore the default). A backend with a different `model_id`/`dim` produces incomparable vectors — the knowledge library's `embed_signature` is derived from `embedding_space_signature` and so folds BOTH in, meaning a swap (including a width change at a constant model id) automatically triggers the sig-gated knowledge re-embed; vector memory re-embeds via `migrate`.

**Shared embedding budget.** Native inference has one shared worker and model.
The normal interactive default is four native threads, capped at one core below
the host's CPU count and never below one thread, so the event loop keeps a core
wherever there is one to spare; background bulk work defaults to one. A normal-thread value equal to that four-thread default reads as
the default policy rather than as operator intent, because a whole-document config
save materializes it. Any other explicit normal or bulk thread setting is honored
within the available CPU count and the existing configuration range of 1–256; a
bulk value of 0 inherits the ordinary thread setting. At most eight pending native
jobs are retained, with
two slots reserved for interactive queries; overflow returns `None`, leaving
unembedded writes eligible for ordinary backfill. Native batch calls contain
at most eight texts, and bulk cooldown belongs to the shared worker so parallel
stores cannot each consume a separate duty budget.

**Sync embedding cache** (`make_sync_embed_fn()`, no args): all store callables
share one bounded 128-entry cache and coalesce concurrent identical requests.
Backend instance changes invalidate the cache even when the model id is the
same. Failures are not cached. Loading remains asynchronous: callers receive
`None` until the shared model is resident. These are resource ceilings, not a
claim of measured latency or RSS improvement on every supported platform.

Embedding cache stripes protect only short cache and full-key in-flight
bookkeeping. No stripe is held during model inference or another caller's wait.
At most 128 in-flight keys are retained. Same-key callers share one inference;
non-bulk work without an explicit budget expires after 30 seconds. This stops
queued work and coalesced waits, but cannot interrupt an in-flight native call.
Bulk inference and bulk coalescing have no implicit deadline, so shared duty-cycle
cooldowns cannot expire unattended repair work. An explicit caller budget still
applies to every priority, including recall's nine seconds. Expired work leaves
vectors NULL and eligible for the next repair; failures are not cached.
Model loading remains asynchronous and is not charged to this queue-wait budget.
An interactive caller promotes an existing queued bulk job. Promotion cannot
interrupt an already-running job or extend an explicit deadline. A shared
producer whose own budget expires while the native call is running still
publishes and caches the vector that call returned; only its own return value is
the unavailable-vector fallback, and each waiter's own budget decides whether it
receives the vector. A producer that obtained no vector yields the fallback to
every waiter.

A custom model's identity is `<label>:sha256:<digest>` of its weights, even when
the operator supplies a model label. `memory.embed_model_id` contributes only
the label part; it is not an identity override and cannot pin a vector space
across a change of weights. Explicit apply persists `memory.embed_model_id`
and `memory.embed_model_stamp` together; the stamp contains device, inode, byte
size, modification nanoseconds and change nanoseconds. An unchanged file reuses
that digest at startup without reading its weights, even in a fresh process.
A changed or missing stamp starts one deduplicated verification worker. Event-loop
readers report a transient unverified model and never hash weights or cache its
placeholder as the shared backend. The worker and synchronous resolvers verify
the digest and persist its identity/stamp pair through the locked config writer,
only while the configured path, identity and file generation still match.
Subsequent polls construct the real backend without operator action, and the next
process start reuses the persisted stamp. Default backend construction does not
hold the shared-backend lock while hashing, so loop readers remain responsive.
Applying also computes the digest off-loop and persists the new digest/stamp pair.
Model identity and vector width define the vector space.
The embedding-status response reports `server_healthy=false` while a configured
custom model is unverified or has a validation error, even if its file exists or
an older backend is loaded. Successful verification restores normal health
reporting without requiring the operator to apply the model again.

The first verified custom-model stamp and `memory.embed_model_legacy_ids` are
written in one locked config update when the stamp is absent or empty and the
configured identity contains no digest. The stored list names the basename/size
identity and any explicit label superseded by that verified digest. A ready
custom backend may re-stamp a matching V1 or V2 store without clearing vectors,
rebuilding indexes, changing generation or submitting embedding work, including
stores opened lazily in another process. The backend path, digest identity and
file stamp must match the persisted configuration, and the store width must
match the backend. Unrelated signatures do not qualify. An existing stamp alone
cannot grant compatibility; the persisted legacy list is also required. A missing
or unreadable current model stamp is a mismatch, so alignment uses normal
reconciliation rather than aborting the caller or preserving unverified vectors.

A same-name/same-size weight replacement, or reuse of an explicit identity label,
made before the first verified stamp cannot be detected from the old metadata;
this is the pre-upgrade identity limit. Weight changes after that stamp get a new
digest, clear the compatibility list and invalidate vectors normally. Explicit
model apply always removes the compatibility list and commits a fresh
`memory.embed_rebuild_generation` alongside the verified model settings. Every
explicit apply requests a rebuild, including the same file and digest with no
legacy labels left. Untouched upgrades have an empty generation and retain the
compatibility behavior above.

Each store records its handled generation in `memory_meta.embedding_rebuild_generation`.
Alignment checks it before legacy restamping and equal-signature shortcuts.
The store advances its in-process vector generation before invalidation, clears
vectors through the existing physical relations, removes stale FAISS files and
commits the signature and handled generation with the invalidation. SQLite
immediate admission serializes the request check across connections. An index
removal failure may leave cleared vectors but never acknowledges the request;
a transaction failure preserves the previous database state. Existing NULL-only
backfill repairs the cleared rows without resetting completed rows on retry.

The request survives config rollback, partial failure and restart. Conditional
rollback checks both this apply's model settings and request identity, preserves
unrelated edits, and never erases the repair request. A competing model edit
refuses rollback and leaves the candidate gated. Cached and late-opened V1/V2
stores share alignment; non-mutating staleness checks also include the request,
so pending stores use lexical retrieval rather than score stale vectors.

`embedding-status` retains English `setup_warning` and `setup_error` diagnostics
and adds stable `setup_warning_code`, `setup_error_code` and parameter objects.
Known codes render fully localized copy that names a next step and never
interpolates the backend's English exception: `model_verification_failed` keeps
only `{{path}}`, `model_download_failed` takes no parameter, and both keep the raw
text reachable through a collapsed "View details" block beside the notice
(`embeddingSetupDiagnostic`), rendered `translate="no"`. Missing and unknown
codes still fall back to their diagnostic prose as the body, so a new backend
code is never swallowed. `model_active` reports the serving loaded backend
separately. A known model is shown as active or configured-but-inactive; the
latter retains a neutral provenance badge. An unknown model identity shows the
localized fallback without a bundled/custom badge, so provenance does not read
as a claim that the model is known. The Vector Memory card's Embeddings stat tile
agrees with that header: once `setup_step` is idle or done nothing is
progressing, so `model_active` (or, for an older backend without it,
`model_available` then `server_healthy`) reads `false` as a muted `not active`,
an absent answer as a muted `unknown`, and only `true` as the success-coloured
`active`; it never guesses "model loading". A progressing or failed
`setup_step` keeps precedence and shows its localized step label. `repair` names its `open_stores` scope, pending invalidations,
remaining live NULL vectors, deferred closed/unavailable stores and unknown scope.
It does not open closed stores or claim they are repaired. Public re-embed status
is `deferred` rather than `done` while that scope is incomplete. Progress counts
cover open-store work, not a promise of whole-install completion. The dashboard
renders the standing rebuild ONCE, on the Embedding Model card (the card that
owns Apply), in user vocabulary with the three counts kept apart:
`pending_vectors` is memory entries still waiting for a vector,
`pending_invalidation` is open STORES still holding old vectors, and
`deferred_stores` is closed or unavailable stores rebuilt when next opened. They
are never summed, and a state where only the invalidation count is non-zero is
still shown as pending. Only the non-zero units are named: the sentence
(`repair_pending`) takes one `{{items}}` slot filled by the surviving clauses
joined through the `fmtList` seam (`Intl.ListFormat` for the UI language), so an
omitted unit never leaves a `0 …` clause or a dangling separator, and each
clause still selects its own localized plural form. The sentence names its
scope (`across all memory stores`): the counts cover every active store (open
ones inspected, closed ones deferred) while the Vector Memory card's stat tiles
count only the store shown, so without the scope the two sets of numbers
cannot be reconciled on the page. All three zero renders
nothing; `unknown_scope` wins over any counts. `unknown_scope` copy names what actually happens (the
check retries automatically while the page is open; a closed store is rebuilt
when it next opens; persistent display means read the gateway log) rather than
promising that an unavailable store is repaired by opening it. The unknown-scope
notice links directly to the Logs page in a separate tab, preserving an unsaved
model-path edit while the operator inspects diagnostics. The Vector Memory
card keeps its 30-second refetch while a rebuild is pending but does not repeat
the line. Each count selects its own localized plural form. Deferred repair is
announced as status text, without a progress bar; applying and running retain
their existing progress semantics. Setup and download labels translate known
states, with a localized model-loading fallback for unknown tokens.

Both Memory-tab cards observe the same React Query embedding-status snapshot.
The status tile is labeled `Embedding status` (localized), distinct from the
`Embedded` vector count and the `Embedding Model` configuration card.
The Embedding Model card no longer copies a response into local status and then
cancels/overwrites the other card's query. Overlapping reads deduplicate, and a
new shared response updates both views without replacing a typed path draft.
Polling retains the two-second active / thirty-second deferred cadence; an idle
Embedding Model card requests no automatic focus/reconnect read. Its existing
conditional missing-path focus refresh explicitly fetches fresh shared data.

While `_apply_embedding_model` backfills, each store's
`backfill_missing_embeddings` receives a per-store progress adapter
(`_store_progress_adapter`) that offsets the store's own `(done, total)` stream by
the rows earlier stores completed, folds the lesson-to-episode phase reset into a
running offset instead of moving the bar backward, and never reports a total below
what has been counted. The exact per-store count is still reconciled from the
store's NULL count after it finishes, so final `done`/`total`, failures and the
multi-store total are unchanged; the adapter only keeps the bar moving during a
single store's sweep.

The prior legacy labels are captured by the config writer's locked mutation.
Inheritance requires a non-empty list whose every item is a string. Status
warnings and legacy alignment use the same validator; malformed scalar or
mixed-list values grant no compatibility and show no inheritance warning.
First verification emits one WARNING about inherited vectors;
embedding status keeps the same text in `setup_warning` until explicit apply.
The warning is written for the operator, not in code vocabulary: the vectors
predate the record of which model file produced them, and a changed file needs
the model reapplied. The Memory tab's vector card renders it as a status notice
with a link that moves focus to the Embedding Model card's path field, so the
fix is reachable from the notice. When the same status also carries a
`model_path_*` error code (the configured file is missing, unreadable or not a
file), the warning swaps its "reapply" clause for "fix the model path first, then
apply" (`legacy_vectors_warning_path_error`). When the path field is mounted,
this warning owns the only settings link and suppresses the separate path-error
pointer. Without the legacy warning the pointer remains: one sentence naming the
fault and its cost (keyword search meanwhile), with the adjacent
`Open embedding model settings` link as the only statement of where the fix
lives, so the destination is not said twice. Without the field the
Vector Memory card retains the complete error. Non-path errors remain complete.
The Embedding Model card echoes
the localized path error under its path field and keeps Apply disabled while the
field still shows the configured path: submitting it would fail with the same
error. That gate is DERIVED from the current status plus the field's live-check
state, never stored, and it yields only to a VERDICT, never to a keystroke:
editing the path keeps the notice (and `aria-invalid` / `aria-describedby` on
the field) and keeps Apply disabled, because nothing is known about the new
path yet and an enabled Apply beside an unchecked path is the state a reader
would not trust. Blurring the field runs the live validate call, whose verdict
replaces the status-derived one: a readable file clears the notice and enables
Apply, a failed check shows its own message and keeps Apply off, and an
emptied field still reverts to the bundled model without a request. A field
whose configured path is healthy keeps the ordinary edit semantics (editing
enables Apply; the blur check still runs).
A file restored IN PLACE is re-checked without a blur: while that status-derived
notice is showing for the untouched configured path, the card re-reads
`embedding-status` when the window regains
focus (the endpoint re-validates the configured path on every call through
`resolve_custom_model`, so one read is the whole re-check). A restored file
clears the notice and re-enables Apply; a still-missing file re-reports the same
code and changes nothing; the listener exists only while the notice describes
the untouched configured path, so an
idle card without a path error stays quiet and a field the user has edited or
live-checked is never refetched over (a status read still in flight when typing
starts lands without touching the draft, and a stale error status landing after a
passed live check does not re-disable Apply, because the live verdict owns the
gate). A stale status can therefore never block a
correction or a valid reapply of the same file. The button's LABEL is decided
separately from that gate, by the path it will submit: when the trimmed field
equals the configured `model_path` (including an edit typed and then restored,
and an empty field while the bundled model is configured) it reads
`Rebuild memory vectors`: the configured path is unchanged and its vectors are
rebuilt (the file behind that path may have been replaced, which the explicit
apply supports; the label claims nothing about the weights);
any other path reads `Apply model`; `Applying…` while the request is in flight.
`touched` is not the signal — a restored edit is touched and still a reapply.
A reapply uses the ordinary button treatment, not the primary accent, so an
idle healthy card does not visually urge a full rebuild. A changed path keeps
the primary Apply treatment; both actions retain their confirmation and gates.
The confirm modal follows the same reading: a reapply borrows the card's own
neutral title (`Embedding Model`) and confirms with `Rebuild memory vectors`, a path change
keeps `Change the embedding model?` / `Change model`. The reapply body describes
reloading the configured model and clearing and rebuilding its stored vectors,
without asserting that a new model exists or that the file has changed. A path
change retains the different-model comparison warning. The disabled
guards (no status, busy, applying, checking, failed live check, status path
error) apply identically under either label, so an unchanged-path reapply is enabled
whenever a change would be. While a rebuild is applying, running or deferred,
the card shows the muted `Keyword search still works. Safe to leave this page.`
line once, under the progress block; a failed rebuild shows the failure hint
instead, with the raw backend text in its tooltip only. This compatibility choice preserves unchanged custom installations but
does not prove the provenance of vectors created before any weight digest.
Signatures retain the original SHA-256 encoding of `model_id|dim`, truncated to
16 hex characters, so unchanged bundled vectors need no rebuild. Custom-model
tests derive expected
identities with `_custom_model_id(path, configured_label)` and spaces with
`embedding_space_signature`, including an independent SHA-256 check of the
weight bytes rather than a basename/size or label-only assertion.

Store opening, model application and standing repair share
`align_store_embedding_space`. It snapshots one ready backend, validates a
positive width no greater than 65,536, then aligns width, generation and
signature under the store lock. Replacing an existing signature advances the
generation even when the width is unchanged, so a write already embedding
against the outgoing model cannot commit that vector after reconciliation.
First attribution of an unstamped store with unchanged width does not advance
the generation. Backend replacement is serialized against this operation.
A loaded candidate waits for its configured identity and width before aligning
stores, and cannot serve vectors until reconciliation completes. Digest and
configuration-write failures occur before vector clearing. The validated,
expanded path is shared by candidate construction and persistence, including
`~/...` input. A later alignment failure restores only the model settings that
apply wrote, preserving unrelated config edits; if those model settings changed
concurrently or rollback fails, the candidate remains gated and reports the
failure. Late-opened stores are collected again after persistence and do not
combine an old configuration width with a candidate signature.
The legacy unready bundled-model attribution path remains non-clearing; strict
alignment requires readiness.

### Model Download Manager (`embeddings.py`)

`ModelDownloadManager` (singleton via `model_download_manager()`) downloads the embedding GGUF in the BACKGROUND at gateway startup — boot is never blocked by the 610MB transfer:

**Where the transfer lives.** The streamed-and-verified HTTPS transfer itself is `asset_downloader.download_to` (shared with hosted feature-video media, `feature-videos.md`): it owns connecting, hashing while streaming, the atomic install and the wording of each failure. `ModelDownloadManager` keeps everything that is about the MODEL — which url to resolve, the sha/size pins, the Ollama salvage, the retry ladder, and turning byte counts into the `status` dict. A second private downloader would be a second place for "did we verify this before installing it?" to be answered differently.

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
- `POST /api/memory/enable-embeddings` — never blocks on the download: if the model is absent it kicks (or adopts an already-in-flight) background download with `DOWNLOAD_ATTEMPTS_INTERACTIVE` (3) attempts and returns immediately (`{"ok": true, "status": "downloading"}`); the frontend polls `embedding-status` for progress and keeps the same polling lifecycle across non-terminal `setup_step` transitions. When the model is present it installs faiss-cpu if missing, wires the embed function, and persists config. The dashboard no longer surfaces a proactive "Start Embedding Engine" button (embeddings auto-start at boot) — this endpoint now backs only the error-state **Retry** affordance
- On failure: status resets to `idle` with error message, frontend shows error + Retry button
- Prevents concurrent setup attempts (409 if already in progress)
- `can_retry` flag in status response for frontend retry button
- `GET /api/memory/embedding-status` — `enabled` is always `true`; `provider` reports the legacy `"ollama"` token for older frontend compatibility; `setup_step` maps the manager's steps to the legacy vocabulary the shipped polling loop terminates on (`ready`→`done`, `failed`→`error`, `downloading`/`verifying`/`waiting_retry`→`downloading`); the raw step and attempt are additionally exposed as `download_step` + `download_attempt` for newer frontends; `server_healthy` requires a present or loaded model and no custom-model validation error; `setup_warning` exposes inherited legacy-vector identity until explicit model apply; `model_id` + `model_dim` disclose the embedding model producing vectors (read live from the shared embedder — e.g. `qwen3-embedding:0.6b` / `1024`) so the Memory tab can show which model runs locally
- `POST /api/memory/embedding-model` — changes the local embedding model at runtime. Two modes, and note which one is the default: `{"path": "...", "validate_only": true}` validates only (returns `size_bytes` without touching the live backend), while **omitting `validate_only` performs the swap** — there is no `apply` flag, so a caller that sends only `path` applies the model. An empty `path` reverts to the bundled model. Refuses with 403 on a restricted session (SEL-audited), 409 while a re-embed is already running (single-flight), and 409 `env_override_active` when `KIROCREW_EMBED_MODEL_PATH` is set, because the env var wins at load and persisting a config path under it would store a path/dim pair the process never uses
- **Apply ordering**: build the gated candidate, install it while retiring the outgoing model, advance the store generation, wait for readiness (600s bound), persist the verified model configuration, retarget and reconcile stores, verify every recorded signature, activate, then backfill. Configuration-write failure preserves stored vectors. Alignment failure conditionally restores the prior model settings before resetting the candidate and restoring widths; rollback failure leaves the candidate gated with an actionable error. Unrelated configuration fields are not rolled back.
- `GET /api/memory/embedding-status` additionally returns a `reembed` snapshot (`step`: `idle`/`applying`/`running`/`done`/`failed`, plus `done`/`total`/`error`) so the dashboard can render background re-embed progress; the card polls only while that step is busy
- `POST /api/memory/disable-embeddings` — **gone**: embeddings are always-on. Kept as a graceful HTTP 410 compatibility stub (not a 404) for older clients; the current frontend does not call it

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
- Key: `lesson.<md5_of_rule>` when the lesson is global (dedup via hash). A lesson
  carrying `repo_scope` folds the scope into the hash, so the same rule scoped to two
  repositories is two rows and an unscoped row keeps its historical key byte-for-byte.
- Value: a mapping `{"rule": ..., "category": ..., "negative": ...}` — the NOT-clause
  — plus `"repo_scope": ...` when the lesson is restricted to one repository. A
  `"applies"` key is present only when the writer named a tier (`"always"` or
  `"on_topic"`); absent means unstated and is the byte-identical legacy shape. The key
  is absent for a global lesson, so no migration was needed. A `repo_scope` that is
  present but not a usable string is withheld from injection rather than read as
  global, and is refused at every write surface.
  is its own field, so a rule containing the separator literal round-trips. Legacy
  rows written as `"rule text"` or `"rule text — NOT: negative text"` stay readable
  (read-time fallback, no migration); they upgrade to the mapping shape only when a
  re-submit rewrites them anyway. Renderers go through `_lesson_display_text()`;
  embeddings use `_lesson_embed_text()` (the bare rule, matching the write path).
- Confidence: 1.0 for `user_explicit`, 0.9 for `migration`
- Methods: `write_lesson()`, `get_lessons()`, `delete_lesson()`, `get_lessons_context()`
- Context: injected as `[Learned corrections]` block, separate from `[Semantic Memory]`
- Allowlist: `lesson.*` prefix in `_BUILTIN_PREFIXES`

A lesson's final embedding commit and deferred lazy backfills match the exact
`value_json` that was embedded, require `is_deleted = 0` and `embedding IS NULL`,
and recheck the embedding-space generation under the store lock. An owner edit,
including a revision-checked edit, a tombstone or a completed newer backfill
cannot be overwritten by an older writer's vector tail. The body remains
committed when its obsolete vector is discarded; ordinary backfill can fill a
remaining NULL vector.

Model: `Qwen/Qwen3-Embedding-0.6B` Q8_0 GGUF (610MB). Apache-2.0 licensed. Served in-process via the vendored llama-cpp-python runtime on all supported platforms.

### Consolidation Integration

`HistoryConsolidator._consolidate()` now extracts structured data alongside existing fields:
- `"semantic"` array → `_write_semantic()` for each (max 20 per consolidation), always under `source="consolidation:<key>"`. An LLM confidence claim never grants `user_explicit` authority, so the conflict rule protects genuine user-stated facts. Extracted lesson writes likewise retain their automatic consolidation source in both versions.
- `"episodic"` array → `write_episodic()` for each (max 10 per consolidation)
- V1 alone retains Markdown dual writes while `config.memory.migrated` is false. V2 publishes all learned changes and its source-span receipt in one SQLite transaction.

The store's `algorithm_version` selects the update policy. V1 keeps its existing
confidence and source precedence, direct consolidation deletion, same-value
refresh and automatic lesson source. Semantic consolidation keeps its automatic
source even at confidence 1.0. V1's audit log remains best effort.
Member V2 keeps inferred changes and deletions as review proposals unless a
correction has matching revision and transcript evidence. Its consolidator
retains the actual automatic source and supplies record metadata and correction
evidence fields. Only explicit member database admission selects V2; an existing
legacy named store continues to use V1. Consolidation captures the session
execution context before yielding and refuses incognito or temporary learning
before reading transcript bodies, opening learned memory or billing a model.

### Dashboard Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/memory/preferences` | Read the markdown preferences document |
| PUT | `/api/memory/preferences` | Overwrite it (gated — see below) |
| GET | `/api/memory/projects` | Read the markdown projects document |
| PUT | `/api/memory/projects` | Overwrite it (gated) |
| GET | `/api/memory/history` | Read today's editable V2 document or V1 recent daily summaries |
| PUT | `/api/memory/history` | Overwrite today's summary file (gated) |
| GET | `/api/memory/semantic` | List all semantic entries |
| PUT | `/api/memory/semantic` | Create/update (validates key, allowlist, injection; gated) |
| DELETE | `/api/memory/semantic/{key}` | Tombstone + log event (gated) |
| GET | `/api/memory/events` | Recent audit trail |
| GET | `/api/memory/carve` | Filter or count one crew store by carve facet. Query: the five facet names, `kind`, `count_by`, `limit`, `offset`, plus the shared `?store=`. Absent, it reads the global store; present, it is owner-gated like every other store-scoped route. 409 `facets_unsupported` on the v1 lineage, 400 `unknown_facet`, 400 `invalid_pagination`, 503 `store_unavailable`. Every answer echoes the `store` it read. See [Who reads a facet](#who-reads-a-facet) |
| GET | `/api/memory/episodic` | Paginated episodic list |
| GET | `/api/memory/episodic/search?q=` | Search episodic memories |
| DELETE | `/api/memory/episodic/{id}` | Tombstone episodic entry (gated) |
| GET | `/api/memory/stats` | Counts, index size, provider status |
| GET | `/api/memory/embedding-status` | Embedding health + download progress. `enabled` always true; `setup_step` in legacy vocabulary (done/error/idle/downloading); raw `download_step` (idle/downloading/verifying/waiting_retry/ready/failed) + `download_attempt` + `bytes_downloaded`/`bytes_total`; `model_id` + `model_dim` disclose the embedding model + vector dimension; `reembed` reports background re-embed progress (`step` idle/applying/running/done/failed + `done`/`total`/`error`) |
| POST | `/api/memory/enable-embeddings` | Non-blocking: kicks/adopts the background model download and returns `{"ok": true, "status": "downloading"}` when the model is absent; wires embeddings + updates config when present. The persisted `memory.embedding_dim` is the width of the **live** backend (`get_shared_embedder().dim`), never a literal — a width that cannot be read, or is not positive, is a 500 `embedding_dim_unreadable` that persists nothing, because `_load_model` refuses a model whose `n_embd` disagrees with the stored width and a wrong value leaves that model unloadable on every later restart |
| POST | `/api/memory/embedding-model` | Change the embedding model. `{"path", "validate_only": true}` validates only; **omitting `validate_only` applies** (no `apply` flag exists). Empty path reverts to bundled. 403 restricted session, 409 while re-embedding, 409 `env_override_active` under `KIROCREW_EMBED_MODEL_PATH` |
| POST | `/api/memory/disable-embeddings` | HTTP 410 compatibility stub for older clients — embeddings are always-on; the current frontend does not call it |
| POST | `/api/memory/migrate` | Migrate markdown → structured memory (gated) |
| POST | `/api/memory/import` | Import from JSON export (gated) |
| POST | `/api/memory/promote` | Promote repeated episodic patterns to semantic facts, tombstoning the rows folded in (gated) |
| POST | `/api/memory/consolidate` | Trigger consolidation for one session (restricted-mode check only) |
| GET | `/api/memory/context-preview?q=` | Preview injected semantic + episodic context |
| GET | `/api/memory/observability?q=` | `stats` + `rejections` + `context_preview`, plus `reads` — the read-volume counters (see above). `reads` is resolved LAST, so it INCLUDES the reads this request itself performed; that is what lets a caller issue the same `q` twice and compare the two objects |
| GET | `/api/memory/recall?q=` | V2 task recall with evidence. Explicit `store` requires the dashboard owner; the authenticated MCP path uses the owning session's canonical execution context and requires memory reads to be allowed. Invalid or unavailable member memory returns an explicit error |
| POST | `/api/memory/seed` | Owner-only selective copy. Body: destination `store`, `source_store`, and 1–50 unique `{kind, id}` items. Destination must be V2; V1 and V2 sources are read-only inputs. Each response item reports imported, existing or rejected with its reason |

**The memory-mutation gate ("gated" above).** Every route that writes durable
memory runs one two-step cascade, `_memory_write_gate` in
`dashboard/handlers/memory.py`, in this order:

1. `_recognize_session(state, sk, operation, blocks_persisted_mode=is_incognito_transcript)`
   — the shared session-recognition probe documented in
   [learn-cron-dashboard](learn-cron-dashboard.md). 400 `missing_session_key` with no
   header, 400 `unknown_session` for a key that matches no live slot, restricted-key
   entry, channel namespace, or persisted transcript.
2. `_is_restricted_session(state, request)` — 403 `restricted_session` for an
   incognito or temporary slot.

Both halves are load-bearing and neither substitutes for the other: the
restricted-mode check answers `False` for a key it has never seen, so on its own a
forged or never-established `X-Session-Key` reaches the write. Every refusal emits a
SEL `log_api_access` record (`outcome="denied"`, `source="dashboard"`, `resources`
`missing_session_key` / `unknown_session` / `restricted_session_block`) under the
route's own operation name (`preferences.write`, `projects.write`, `history.write`,
`semantic.write`, `semantic.delete`, `episodic.delete`, `memory.migrate`,
`memory.import`, `memory.promote`), and every non-2xx body carries a machine-readable
`code`.

Gateway-created child context retains privacy mode in the same canonical execution
record as member/store identity. Temporary skips database opening and lesson reads;
Incognito permits reads but refuses durable learned changes. A continuation retains
its recorded mode and member; it never infers them from a provider template.

The matching GET on each markdown route is a **read** path and is deliberately
ungated — gating it would blank the Memory tab for every session the probe cannot
recognise.

All three document GETs redact credential and unsafe-URL shapes before returning
`content`, with `content_redacted` identifying any transformation. A transformed
document is read-only in the dashboard so a whole-document Save cannot overwrite
hidden source bytes with display placeholders. Existing stored content stays intact.
The server also refuses its replacement with `409 memory_document_redacted`.
Clean documents stay editable. V1 preferences/projects reuse their in-lock baseline
comparison; V2 performs admission inside its existing private-profile validation
lock. V2 history GET returns today's database content, and PUT compares that
exact target under the same cross-process append lock as consolidation. V1 keeps
its recent-history aggregate and uncached aggregate comparison. Both replace
only today's file. Retained V2 aggregate reads remain available through
`read_recent_history` and the structured history reader; daily editing does not
copy earlier files into today or change their bytes. Previously saved duplicate
content is not automatically removed. A changed baseline returns
`409 memory_document_changed` without writing. The editor preserves drafts on a
refusal and disables writes during a failed or pending confirming read. These are
shared dashboard safety changes; V1 context, retention and consolidation policy
remain separate from this document response contract.

V2 owner document writes preserve submitted line endings after existing project
document normalization. Unchanged GET/PUT round trips cannot accumulate carriage
returns on Windows. V1 retains its existing text-write newline behavior.

### Which store a dashboard route reads (`?store=`)

These routes take an optional `?store=<name>`: the three markdown GET/PUT pairs,
semantic GET/PUT/DELETE, episodic list/search/DELETE, `stats`, `events`, `carve`,
`import`, `context-preview` and `observability`. One resolver answers for them,
`resolve_requested_memory_store(request, state, operation)` in `handlers/_shared.py`,
and the two tiers it hands back are `markdown_memory_for_store` (preferences,
projects, daily history, FTS) and `vector_memory_for_store` (semantic, episodic,
lessons). `migrate` validates the selector and refuses named stores because legacy
Markdown migration belongs to Global V1. `promote` selects the requested store
and refuses V2, whose experiences are not automatically promoted or retired.
`consolidate` follows the canonical execution record of its target session and verifies
the caller's authority over that session; a store selector cannot retarget it.
Embedding configuration and `settings` remain installation-wide controls.

| Caller sends | Store read | Gate |
|---|---|---|
| no `?store=` | the global store, preserving the existing dashboard default | private internal callers cannot borrow Global authority |
| `?store=default` | the global store | owner |
| `?store=<declared silo>` | that silo | owner |
| `?store=<undeclared or malformed>` | nothing — 404 `unknown_memory_store` | owner |

Parameter presence requires the owner gate. An absent parameter selects the
global store and still verifies private internal caller authority; an unverified `X-Session-Key`
cannot select a named store through another session's binding. Agent context injection
and consolidation resolve their own trusted session metadata separately from this HTTP
contract. A present parameter explicitly selects a store and takes
`require_owner_dashboard_request`, which needs the dashboard-user claim
(`request["app"] == ""`, which refuses an App Kit token too) and a non-empty
`request["user"]` that is the configured owner. `token_auth_middleware` publishes that
key on the cookie/query-token path ONLY and
never on its `X-Internal-Secret` branch, so the gate excludes an agent POSITIVELY:
kiro-cli, the MCP servers and subagents authenticate as the installation and carry no
identity to present, so they fail a check for "the caller proved it is the dashboard
owner" rather than being recognised and refused. Gating on presence rather than on
"the name differs from my binding" is deliberate: `?store=default` names the
operator's own global memory, which a mismatch rule would wave through for any unbound
caller. Full argument, with the audit record: [security](security.md).

**An undeclared name is a 404 and never a degrade.** `resolve_store_path` degrades an
unknown name onto the default store, so answering it would render the operator's own
preferences, semantic rows and stats under the label of a store that does not exist,
and the response would look like it worked. A malformed name gets the same 404 as an
unknown one, because telling them apart would report whether a given name is declared
to a caller that has not passed the gate.

`vector_memory_for_store` answers `None` for a silo whose vector tier cannot be stood
up, and every route reports that as 503 `store_unavailable`. Never a fall back to the
global store: serving the operator's own memory under a crew's name is invisible in
the response, which is the one failure the file boundary exists to prevent.

Adding the parameter changes no write authorization. Every PUT/POST still runs
`_memory_write_gate`, so a store-scoped write is gated twice — the session cascade above
decides whether this caller may write durable memory at all, the owner gate decides
whether it may aim that write at a store it is not bound to. On a PUT the store is
resolved FIRST, and that precedence is deliberate: naming another store is the
operator's question, so a caller that may not ask it should not have its body read
either. With no `?store=` the resolver cannot refuse at all, so such a PUT still meets
the write gate first, unchanged. Because a refusal is audited under the route's own
operation, the READ paths now carry names too (`preferences.read`, `projects.read`,
`history.read`, `semantic.read`, `episodic.read`, `events.read`, `stats.read`,
`carve.read`) — the GET itself stays ungated, and the name exists for the denial the
owner gate can emit on it.

The dashboard document editors enable editing and Save only after their own store-scoped
read succeeds. A pending or failed read is not an empty document and must never become
a replacement write. A successfully loaded empty document remains editable. The textarea
stays editable during a save; completing that save clears only the draft actually sent,
so newer keystrokes survive the response. Switching stores remounts the editor.
The carve, retired and backup cards each use a distinct card-and-store React key,
preserving that remount behavior without duplicate sibling keys.

The agent-facing `/api/lessons` routes use a separate, authenticated session-binding
contract: `resolve_lesson_memory_store` permits a named store only for requests carrying
the middleware-established `internal_auth is True` marker or a verified dashboard owner.
Non-owner dashboard and App Kit tokens cannot select another session's silo through
`X-Session-Key`, including for listing, creating and deleting lessons. A raw internal-secret
header is not authentication evidence. Global/workspace lessons keep their existing gates.

### Store administration (`memory_admin.py`)

`memory.py` serves the CONTENTS of one store; this module answers the operator's
questions ABOUT the stores. **Every route here takes the owner gate
UNCONDITIONALLY, not on the parameter's presence**, because each one either
enumerates every silo or mutates a store — and because a route gated on presence
alone would become reachable by a non-owner simply by omitting `?store=`. The
routes that also take a store still run it through
`resolve_requested_memory_store`, so the declared-name rule has one implementation;
the unconditional gate is the stronger check layered in front of it.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/memory/stores` | Enumerate every store for the picker: `name`, `is_default`, `lineage` (`v1`/`crew`), `exists`, `semantic_count`, `episodic_count`, `lessons_count`, `facets_supported`, `backup_count`, `newest_backup`. Takes no `?store=` — it answers for all of them. One exception: a crew (V2) store whose `owner_member_id` no living member holds — a deleted member's retained store — is omitted, because no content route can read it (every request fails the member lookup) and listing it would give the picker a row whose every click is a 503. The record itself stays in `memory_stores` (its member id is reserved, and `kirocrew memory scan` / `doctor` still report it) |
| GET | `/api/memory/retired?store=&limit=&offset=` | Episodes a semantic write superseded, newest first: `id`, `text`, `superseded_by`, `retired_times`, `ts`. 400 `invalid_pagination` |
| POST | `/api/memory/retired/restore` | Body `{"id", "store"}` — clears the tombstone in place. 400 `invalid_episode_id`, 404 `unknown_retired_episode` for an id that is not a restorable retirement in that store |
| GET | `/api/memory/backups?store=` | That store's hot copies: `name`, `size_bytes`, `taken_at` |
| POST | `/api/memory/backup` | Body `{"store"}` — take one now; `{"backed_up", "skipped", "pruned", "failed"}` |
| POST | `/api/memory/restore` | Body `{"store", "name"}` returns `{"ok": true, "superseded": null, "pending": true, "restart_required": true}` for both versions; startup performs activation. 400 `invalid_backup_name` for a name outside the store's backup directory, 404 `backup_not_found`, 409 `restore_refused` for integrity or pending-restore conflicts. Staging changes no live memory |

- **A POST names its store in the BODY, under the same `store` key the query string
  uses.** One spelling for both transports, read only by `_admin_store`: a second
  spelling is how one route starts answering for a store the seam never resolved. The
  body is consulted first and the query resolver handles the absent case, so a POST that
  names no store still lands on the global store.
- **The store list is best-effort PER STORE.** A store whose file is missing or
  unreadable reports `exists: false` with NULL counts and does not fail the response,
  because one damaged silo would otherwise hide every healthy one from the picker —
  the same fail-soft-per-store rule the injection audit and the backup sweep follow.
  `exists` answers "are the counts beside this real", so it is false both for a store
  nobody has written yet and for a file carrying no product table; nulls rather than
  zeros, because three zeros read as "this crew remembers nothing" and send the
  operator looking for the memory instead of for the file. The backup summary is
  independent of the counts, so an unreadable `backups/` directory costs only that
  half. Order comes from `declared_store_names()` (default first, then sorted) and is
  not re-sorted here: two passes over one install that disagree on order are how the
  picker and a report stop describing the same list.
- **The probe opens each store READ-ONLY, and resolves-then-confirms its path.** A
  read-only URI (built by `memory_backup`'s own builder, so a `?` or `#` in a data-home
  path cannot truncate it into a different database) because opening a silo read-write
  to count it would CREATE and migrate the very file whose absence the row is
  reporting; and `owned_store_path` rather than a bare `resolve_store_path`, which
  degrades an unknown name onto the default store and would count the operator's own
  memory under a silo's name.
- **`lineage` and `facets_supported` are read from the FILE, never from the name or the
  config.** They come from `detect_lineage` over that store's own schema (see
  [Two schema lineages](#two-schema-lineages)), so the picker cannot offer a carve the
  file cannot serve: a silo whose `memory.db` predates the crew lineage is v1 forever,
  and `facets_supported: false` is what tells the UI to say so instead of rendering an
  empty carve that reads as "this crew remembers nothing".
- **A backup is addressed by NAME, never by path.** The listing returns the stamped
  basename, currently `memory.<stamp>-<UUID>.db` and also the supported legacy
  `memory.<second-stamp>.db`; a filesystem path would disclose the
  data-home layout, and for a silo the `memory_stores/<name>/` layout the fence exists
  to keep out of the browser. `POST /api/memory/restore` resolves that name INSIDE the
  store's own `backups/` directory, in the validate-then-re-check-after-composition
  pairing `memory_stores._named_store_dir` uses: the name must be a SINGLE path segment
  (checked on both separators, and refused BEFORE the join, because
  `Path(dir) / "/etc/passwd"` is `/etc/passwd` — an absolute right-hand side overrides
  the base), and the resolved path must then be EXACTLY the composed one. **Identity,
  not containment**, and the difference is the attack that survives a containment test:
  a link planted at the name is refused when it escapes the directory and ACCEPTED when
  it redirects inside it, which is enough to restore another store's file under this
  store's name. The directory itself is resolved first, so a root reached through a
  symlinked ancestor (`/tmp` on macOS) still passes.
- **Creating a store is a config write, taken under the repo's locked writer**
  (`run_config_write` → `update_config_locked`), the pattern the `settings` PUT
  already uses, so it serializes against the CLI and every other writer generation and
  none of it runs on the gateway loop. The name is validated by
  `memory_store_name_defect` — one predicate, in the module that owns the shape rule —
  and a defect is 400 `invalid_memory_store_name`, an existing name 409
  `memory_store_exists`.
- **There is deliberately NO delete route.** Undeclaring a store orphans its markdown
  tree, index and vector file, or destroys them, and a store's contents are the one
  thing here that cannot be rebuilt from another source. That needs explicit operator
  direction on the host, not a dashboard button whose confirmation dialog is the only
  thing between a mis-click and a crew's whole memory.

### CLI

The canonical command and flag inventory is in the [CLI spec](cli.md#member-memory-commands); this section records the memory-side behavior behind those commands:
- `carve --store <name>` — filter or count one store's rows by their carve facets; see [Who reads a facet](#who-reads-a-facet). Dispatched before the shared vector store is opened, because it opens the store NAMED on the command line rather than the default one
- `show [preferences|projects|history]` — read the markdown layer through `MemoryStore` (all three targets when none given); `--format md|json` (json entries carry `path`, `updated_at` mtime in UTC ISO-8601, `content`), `--since YYYY-MM-DD` filters history days. Missing/empty files print as empty rather than erroring
- `search <query>` — searches BOTH memories and labels each section: the vector store's episodic recall, then keyword hits from the markdown layer's FTS5 index (`MemoryStore.search`, over `preferences.md` / `projects.md` / every `history/*.md`). `--layer vector|history|all` (default `all`); `--layer vector` reproduces the previous vector-only output exactly, and `--layer history` skips constructing the vector store entirely, the same way `show` does. The two indexes answer different questions — "where did I write this word" versus "what does this mean like" — so they are reported separately rather than merged into one ranking. `search_episodic` text-searches whenever `query_embedding` is None and does not auto-embed, so the vector section embeds the query in-process, blocking once on the model load (`_SEARCH_MODEL_LOAD_TIMEOUT_SECS`, 120 s) — a one-shot read cannot lean on the gateway's boot re-embed sweep the way a WRITE can. It degrades to keyword matching, naming the reason on **stderr** (stdout shape is unchanged), when the model is not downloaded (a one-shot CLI never kicks the download), when the store's vectors were produced by a different model, or when the model fails to load; and when the semantic pass returns nothing it retries the keyword leg once before reporting "No episodic memories found.", because the vector legs score only rows with a non-NULL embedding and deferred/imported/re-embed-pending rows are keyword-searchable only until the gateway's sweep reaches them
- `stats` — counts, embedded coverage, FAISS accelerator status, audit event count, and a **`Reads (this process)`** block from `read_counters()` (rows + statements, then the semantic/episodic population-scan tallies). Labelled per-process because the CLI constructs its own store, so the totals describe only what this invocation read; the gateway's totals are the `reads` object on `GET /api/memory/observability`
- `export [--store <name>]` — vector-store collections; `--include-markdown` opts in a `markdown` collection (`preferences`/`projects` entries + per-day `history` list from `MemoryStore.markdown_snapshot()`) without changing the default payload shape. `--store` names one store, and as a READ it is admitted only against a database that already exists: a name with no database is refused rather than answered with an empty payload, and nothing is created. `--include-markdown` together with a named store is refused, because that tree is read through a fence this verb does not carry (`hooks.safe_read_file_bytes_nolink` refuses the `memory_stores/` subtree and answers None, which `_guarded_entry` shapes exactly like a missing file), so the payload would report an empty `content` for a `preferences.md` that is on disk and non-empty. The rows still export on their own
- `migrate` — one-time markdown → structured migration (preferences.md → semantic, history/*.md → episodic)
- `import <file> [--store <name>]` — restore from JSON export with full validation. `--store` is the one verb here permitted to CREATE a named store's database, so admission, the destination's version check, the absence check and the removal of a database this run created are one hold of `memory_store_namespace_lock`. When this run creates that file and no row lands, the file is removed and the refusal says the store still has no database; when the run is interrupted, nothing is deleted and the file is named, since rows may have been committed before the abort
- `kirocrew security audit` also scans vector memory for injection patterns

### Keyword search over the markdown layer

**Query escaping.** The query is treated as literal words, not FTS5 expression syntax. Tokens are quoted by `fts5_quote_tokens` in `_sqlite_compat.py`, the single escaping dialect shared with knowledge retrieval. Unquoted, `-` and `.` and a bare `AND` are FTS5 operators, so `PROJ-123` or `hooks.py` raises inside the driver and `MemoryStore.search`'s `except` turns it into `[]`, a silent "never written" for the likeliest queries. The join differs by surface on purpose: memory ANDs every token (a hand-typed query is deliberate), knowledge drops stopwords and ORs (natural-language recall).

**CJK segmentation is a knowledge-only behaviour today.** `_sqlite_compat.py` also exports `fts5_segment_for_index` and `fts5_cjk_match_groups`, the pair that makes a word inside a spaceless CJK run addressable (see `knowledge.md` §4), plus the two primitives both search surfaces share: `is_cjk_char` (the character ranges) and `script_runs` (the same-script split). Those two live there because session search needs them as well, and the hand-maintained second copies had already drifted apart -- `history_search.py` now re-uses both rather than restating them. Three product FTS5 tables share the root cause and only `items_fts` is fixed. `memory_fts` does **not** use them: it is created `tokenize='porter unicode61'` and still matches through `fts5_quote_tokens`, so a spaceless CJK memory query is one token matched against one token and recalls only an exact whole-run hit. `preferences_fts` (`apps/builtins/personal_shopper/backend/store.py`) has the same gap and is likewise unfixed. The helpers live in the shared module rather than under `knowledge/` precisely so these surfaces can adopt them; each needs its own index rebuild and its own decision about AND-vs-OR semantics, which is why neither is done here.

**Empty index is not absence.** `MemoryStore.index_row_count()` returns the FTS row count, or `None` when the index cannot be read, so a caller can separate three states that `search` collapses into one empty list: unreadable, empty, genuinely no match. An unbuilt or unreadable index is reported as such rather than as "no match".

**No agent-facing tool.** The index is reachable from the CLI only. An MCP tool that reads memory on demand would have to enforce the temporary-session read boundary itself, and that boundary is not readable from an out-of-process stdio server: `memory_mode` lives on the dashboard's `SessionSlot`, while Slack and Telegram carry it in `privacy_mode.is_temporary`, and a temporary Slack thread writes no transcript metadata at all. Exposing the index to agents needs a governance capability scope, the way `learn_add` gates durable writes through `capabilities.memory_writes`, and that is left to separate work.

### Migration (`migrate_from_markdown`)

Parses legacy markdown files into structured memory:
- `preferences.md`: bullet points with `key: value` → semantic entries (confidence 0.85, source "migration"). Bare prefix keys get `.default` suffix.
- `projects.md`: project names → `project.name` semantic entries, details → episodic
- `history/*.md`: daily summaries → episodic entries (importance 0.4)
- **Embedding during migration**: when the model file is present, the caller sets `store.embed_fn` before calling migration. Each episodic entry is embedded in-process and stored with its FAISS vector, enabling vector search immediately after migration.
- Idempotent: re-running skips existing semantic entries (conflict resolution), episodic dedup via FAISS when available

**Automatic migration (`GatewayOrchestrator._auto_migrate_memory`)**: migration is fully automatic; there is no dashboard "Migrate" button. After the deferred memory initialization worker completes recovery and opens the readiness barrier, the gateway schedules a task retained in `_background_tasks` and cancelled on shutdown. It runs two idempotent phases with blocking work offloaded to the maintenance executor:
1. **Migrate** (gated on `memory.migrated == False`): detects legacy content via the shared `memory.legacy_memory_present()` helper (also used by `/api/memory/stats`), runs `migrate_from_markdown()`, then flips `memory.migrated=True` for **everyone** — fresh installs with zero legacy entries included, so all users land in vector-only mode. Syncs the live `consolidator._migrated`, and **acknowledges** with a `migration` audit event (`memory_events`, visible in the dashboard Audit tab, `source="auto"`, counts in `new_value`) plus a `logger.info` line. On error: logs and leaves `migrated=False` so the next boot retries.
2. **Re-embed sweep** (independent of the migrated flag): awaits the background model download if one is still in flight (safe — we are our own task), then `VectorMemoryStore.backfill_missing_embeddings()` embeds any episodic rows written with a NULL vector and rebuilds the FAISS index. Self-healing across boots and across a download that failed then later succeeded.
   - **The sweep probes before it loads.** `wait_ready()` kicks the GGUF load, so asking the model to be ready is not a free question — it costs ~1GB of RSS for the process's lifetime (measured: `VmRSS` +1069 MiB, of which `RssAnon` +455 MiB is private KV/compute buffers and `RssFile` +614 MiB is the mmap'd weights). A steady-state boot has nothing to embed, so the sweep asks two **non-loading** questions first and returns 0 when both say no: `store.has_pending_embeddings()` (three `SELECT 1 … LIMIT 1` reads over the same predicates the three sub-sweeps use) and `store_embedding_space_is_stale(store)` (a signature comparison over `model_id`/`dim`, which are set when the backend is *constructed*). Only when there IS work does it wait on readiness, reconcile, and sweep — so a stale vector space still reconciles and re-embeds, and rows deferred with `defer_embedding=True` are still picked up on a later boot. The non-mutating probe is used deliberately rather than `reconcile_store_embedding_space()`, which is destructive and refuses to clear against an unready backend. A store that does not implement the probe keeps the old always-load behaviour rather than silently losing its sweep.
   - **The model still loads lazily on the first real embedding need.** `_start_embeddings()` binds `embed_fn`/`embed_fn_factory` without loading anything: `make_sync_embed_fn()` returns a closure, and the load is kicked inside `embed_batch()` the first time it finds `_llm is None` (returning `None` so that caller degrades to keyword search).
   - **Two producers of NULL-vector rows**, not just one: rows migrated before the model landed, and rows written by a bulk writer that passed `write_episodic(defer_embedding=True)` — the foreign-agent importer does this so its apply request is not held for minutes by per-chunk inference (see `docs/system-specs/modules/onboarding-import.md`). Import schedules its own sweep, so this boot sweep is the standing retry, not the only path.
   - The sweep needs **numpy only, not faiss**. Faiss is an optional accelerator and not a declared dependency, so requiring it made the sweep a silent no-op on a stock install. Only the index rebuild is faiss-gated; `search_episodic` falls back to `_sqlite_vector_search` (cosine over the stored blobs, numpy-accelerated when present and stdlib otherwise), so the vectors are useful either way.

The backend `POST /api/memory/migrate` endpoint and the `kirocrew memory migrate` CLI remain as a manual escape hatch, but the dashboard no longer calls them.

The active Global store and cached named V1/V2 stores share one gateway repair
loop for pending vectors. Each 30-second pass takes one ready store, revalidates
named declarations and ownership, and handles at most 16 rows of each kind with
a fair cursor across stores. It only uses an already loaded model, shares the
normal bounded embedding worker, pauses during model replacement and checks
shutdown before committing. Global joins the rotation only after its boot
migration and full repair sweep finish, and the loop never opens a store. Seeded
or temporarily deferred rows can therefore gain vectors while the gateway is
running, including V1 rows left vectorless by a saturated shared queue. The
repair does not change either memory version's retrieval, admission, decay,
consolidation or capacity rules.

Consolidation failures before a provider call, including invalid member memory
or essential context, record a durable environment backoff. They do not consume
the billed-attempt budget or mark unread transcript spans as processed. Repeated
environment failures widen the retry interval to its existing ceiling instead
of producing a traceback on every idle tick.

### Cross-Platform

macOS (Apple Silicon and Intel), Linux (x86_64, arm64/Graviton), and Windows supported. All paths use `pathlib.Path`. GGUF model downloaded over sha256-pinned HTTPS from the Kiro Crew CDN. No runtime install step — native llama.cpp libraries are vendored per platform in `_vendor/llama_cpp_libs/` and selected via `LLAMA_CPP_LIB_PATH` (the old Docker fallback is gone).

Before the vendored runtime becomes usable, `embeddings._load_llama_class()`
reconfigures llama-cpp-python's import-time stdout/stderr null streams to UTF-8
with backslash replacement. The upstream suppressor temporarily installs those
streams process-wide while the GGUF loads on `kc-embed-load`; keeping the same
handles preserves its native fd suppression while preventing unrelated Unicode
gateway output from failing under a locale encoding such as Windows cp1252.

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
(`_MAX_IMPORTED_LESSONS`) because `LessonStore` prunes at 200 (by tier, findings
first, then unstated rows, then authored rules — each oldest-first); an
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
change the inode being queried. The lineage scanner's 10,000-row scan limit applies
to the aggregate active rows across its supported semantic and episodic tables and
is checked before either table contributes an item. Episodic text deduplication is
rechecked under the native store write lock before insertion, preventing a
concurrent native write from being duplicated.

## Lessons (`learn.py` → `vector_memory.py`)

User-taught corrections ("always do X", "never do Y"). Single write path through `vector_memory.write_lesson()`:

1. **Vector memory** (primary): stored as `lesson.<md5hash>` semantic entries with `confidence=1.0, source=user_explicit`. The value is a mapping `{"rule", "category", "negative"}`, plus `"repo_scope"` when the lesson is restricted to one repository — the NOT-clause is a separate field; legacy in-band `"rule — NOT: negative"` rows stay readable without migration. Injected via `get_lessons_context()` — separate from `[Semantic Memory]` block. A scoped lesson is gated by `project_scope.project_scope_satisfied` against the session's active project BEFORE the shown/omitted counts are computed, using the same rule as a skill's `repo_scope`.
2. **V1 JSONL fallback** (`~/.kiro/crew/lessons.jsonl`): only used when vector memory is not initialized. Read-only migration source once vector memory is active.

**V1 priority**: vector lessons override JSONL. V2 never constructs a JSONL lesson store or falls back to one; an empty SQLite lesson table is a valid empty result. The fallback is keyed on whether the
vector store holds any renderable lesson at all (`has_any_lesson()`), NOT on whether
the rendered block came back empty. The two are different: no rows means the JSONL
store is still the authority (the first-boot migration window), while rows that exist
but are all out of scope for this project means the vector store already answered, so
falling back would resurrect lessons the user deleted and ignore the scope gate. A row
whose `repo_scope` is present but unusable counts as neither.

**Single write path** — all lesson writes go through `write_lesson()` which provides:
- Substring dedup, and it is ASYMMETRIC. A submitted rule contained in a stored one is
  declined and nothing is mutated (`deduped` / `substring_covered`): "use dark mode"
  won't duplicate "always use dark mode". A submitted rule that CONTAINS a stored one
  deletes the stored row instead — "longer wins" — so teaching "when a release is in
  progress, never force push to a shared branch" retires a stored "never force push to
  a shared branch". Note the direction of that trade: attaching a condition to a rule
  makes its text longer and its guidance NARROWER, so the row that survives can be the
  one that applies in fewer cases.
- Topic-overlap dedup: "use light mode" replaces "use dark mode" (shared keywords ≥ 50% of the LARGER keyword set → newer wins)
- Allowlist validation, injection scanning, audit logging

Substring-delete and topic-overlap are not independent: verbatim containment at word
boundaries makes the stored rule's keyword set a subset of the submitted rule's, so
overlap scores 100% and the topic rule would delete the same row the substring rule
did. Suppressing either one alone does not keep both lessons — which is why
`write_lesson` REPORTS its deletions (below) rather than declining to make them, and
why a caller that must never replace an existing lesson routes to
`set_semantic_if_absent` instead (see `onboarding_import`, whose comment records that a
foreign directive could otherwise delete a correction the user taught the agent).

**A call that stores nothing deletes nothing.** Every branch above QUEUES its
supersedes; the queue drains only after the semantic write commits the submission. The
ordering carries the whole guarantee, because the branches read rows newest-first and
each of them can be reached before a later row declines the write: deleting where a
branch decides would let a row retired early in the scan be destroyed by a call that
goes on to return `deduped` / `substring_covered`, leaving one stored lesson gone, the
submission unstored, and a result naming the surviving lesson as still in effect beside
the deletion. Draining after the write extends the same guarantee to a value the
semantic write itself rejects.

**What a write reports.** `write_lesson()` returns a `LessonWriteResult` naming WHICH
outcome occurred: `inserted` / `enriched` / `unchanged` / `deduped` / `refused`, plus a
short reason code (a `SemanticRejectCode` value for a refusal, the dedup rule's name for
a dedup, `kept_stored_clause` for the one `unchanged` case that is not a byte-identical
re-submit), plus `superseded` — the rules this call DELETED. The outcome vocabulary is
shared with `LessonStore.save_or_enrich()`, which already returned the first three
words, so both stores describe the same events the same way — but only the vocabulary is
shared, not the dedup policy: the JSONL store matches on exact rule text plus scope and
has no rule that supersedes, so it keeps both a general rule and the narrower rule
containing it.

The distinction matters because two outcomes mean "your lesson did not land"
(`refused`, `deduped`) while two mean "your lesson is fine, there was nothing to do"
(`unchanged`, and the kept-clause variant) — a caller reading only a bool cannot tell
them apart, and the `learn add` CLI guessed wrong, writing a second `lessons.jsonl`
record on every one of them.

`superseded` exists because every other field describes what happened to the SUBMITTED
lesson, so a write that tombstoned a stored rule reported a bare `inserted` with
`reason=None` and the caller was told its lesson was saved with nothing naming the cost.
The result is the only channel that can carry it: the deleted row is a tombstone, so by
the time the caller looks it is absent from `get_lessons()`, from `learn_list` and from
the injected lessons block. It is empty on every path that deleted nothing —
`enriched` (decided in pass 1, which skips the dedup scan) and every `deduped` or
`refused` outcome, none of which reach the drain — is forwarded by
`/api/lessons` as a JSON array, and is rendered in full — not counted, not truncated —
by the `learn add` CLI and the `learn_add` tool, because that text is the last readable
copy of the removed rule.

**The result's truth value is the old bool, deliberately.** `bool(result)` is `wrote`,
byte-for-byte the predicate the previous `-> bool` return answered, so the three callers
that only branch on success (`history.py` consolidation counting, the
`vector_memory` migration loop, the task runner discarding it) and ~55 bare
`assert store.write_lesson(...)` assertions are semantically unchanged. That is what
allowed the bool to be REPLACED rather than kept beside a second reporting method:
without `__bool__`, an ordinary return object is truthy by default, so every positive
bare assertion would keep passing while asserting nothing — a silent hazard mypy cannot
flag, since a bare `if` on any object is legal. `stored` is the separate property for
"is my lesson in the store" (true for a no-op re-submit, which is NOT a write). Surfaces
that report to a human or a model — the `learn add` CLI, the `POST /api/lessons` response
(`ok` / `outcome` / `reason` / `superseded`), the `learn_add` tool result — read `outcome`
and `reason`, and name the `superseded` rules when there are any.
The dashboard Memory tab clears its draft and refreshes the list only for `inserted`
or `enriched`; `unchanged` clears the draft but reports that it was already stored,
while `deduped` and `refused` preserve the draft and surface the reason so it can be
reworded instead of presenting a rejected write as success.

**Write sources**:
1. **`learn_add` MCP tool** (immediate): user says "remember X" → LLM calls tool → `POST /api/lessons` → `write_lesson()`
2. **Task runner** (on failure): step fails → LLM extracts lesson → `write_lesson(source="task_runner")`
3. **Consolidation** (background): extracts corrections not already saved via `learn_add`. V1 and V2 call `write_lesson(source="consolidation")` at confidence 0.9.
4. **Dashboard/CLI** (manual): `POST /api/lessons` → `write_lesson()`

**Durable lesson volatile-fact boundary.** The primary `write_lesson()` writer and the
JSONL `LessonStore` fallback call one shared predicate before persistence. It refuses
exactly two classes in either the `rule` or `negative` field, in every category:
runtime model-identity assertions recognized by `_VOLATILE_MODEL_FACT_RE`, and
concrete-ID model-selection imperatives recognized by `_BEHAVIORAL_MODEL_PIN_RE`.
A `running as` assertion belongs to the identity class only when its object is an
unambiguous model noun (`model`, `model backend`, or `backend model`), a qualified
`backend` that ends its clause, or a concrete model ID. Service-account and process
wording such as `running as the active backend service account` stays durable.
The imperative can start the field or follow `.`, `!`, `?`, or a newline, with optional
`please` / `kindly`, emphatic `do`, and bounded `for ... ,` / `when ... ,` prefixes.
It is refused only when a recognized verb directly selects a concrete model ID, with
optional short determiners, qualifiers, and a `model`, `backend`, or `provider` noun
around that ID. The matcher consumes the complete ID-shaped token. After an optional
`model`, `backend`, or `provider` noun, the selected object must end its clause at the
end of the field, a newline, punctuation (`.`, `,`, `;`, `:`, `!`, `?`, `)`, or `]`),
or before one connector from this closed class: `for`, `when`, `whenever`, `if`,
`unless`, `in`, `on`, `at`, `to`, `over`, `instead`, `rather`, `and`, `or`, `but`,
`as`, `with`, `without`, `because`, `since`, `by`, `until`, `while`, `so`, `only`,
`from`, `during`, `before`, `after`, `except`, `via`, `per`, or `not`. A following
plain noun such as `tokenizer`, `endpoints`, `wrapper`, or `flag` makes the ID a
qualifier of a durable tooling object rather than the selected model.
The concrete-ID scope is deliberately limited to the registry families encoded by
`MODEL_ID_LITERAL_PATTERN`. The trusted review workflow keeps an exact literal copy
pinned by a test, so IDs from other backends are not lesson-refused. This grammar is
best-effort for free-form wording. Future phrasing misses are handled by the
`learn_add` tool-description instruction, never by new regex branches; callers must
not disguise either refused class. A model-version literal by itself is not volatile.
Durable compatibility, tooling, and preference text can name a version in any category or in a NOT-clause.
The vector writer returns `outcome="refused", reason="volatile_session_fact"` before
embedding or deduplication; the JSONL route maps the same refusal to that wire outcome
and reason.
Automatic JSONL callers read the returned outcome before counting, notifying, ledgering,
or reporting an imported lesson. Onboarding applies the same predicate before either its
vector or JSONL instruction branch, so a rejected directive is never reported as
imported. Both context renderers apply the predicate again. Mapping rows expose their
fields directly. Legacy vector strings use the row's MD5-derived key to prove which
in-band separator splits the rule from its NOT-clause; rows keyed by another writer
remain one rule rather than being guessed apart. A legacy volatile row stays
available to listing and manual deletion, never reaches a prompt, and carries
`withheld_reason="volatile_session_fact"` in the lessons API so `learn_list` marks it
`WITHHELD`. Vector population checks use the same renderability predicate, so a store
containing only withheld rows does not suppress the V1 JSONL lesson fallback.
The `learn_add` MCP handler, task runner, consolidation, dashboard POST route, headless
`--slack-only` route, and direct writers therefore enforce the same boundary. The MCP
handler renders the reason as `Error: volatile_session_fact: ...` and asks for a reusable
behavioral rule instead. An imperative concrete model choice belongs in
`agent.role_models.<role>`. Model-family guidance and plain model-version references
remain durable.

**V1 migration**: `migrate_from_markdown()` reads `lessons.jsonl` and writes each entry as `lesson.*` semantic key with `source=migration, confidence=0.9`. User-explicit lessons (confidence 1.0) can't be overwritten by migration. V2 refuses this importer.

Categories: `tool`, `preference`, `knowledge`. Startup injects lessons as TWO blocks with TWO separate, window-INDEPENDENT budgets, split on an authored `applies` field rather than on anything a reader could infer. `applies: "always"` is a standing rule and renders in `[Learned corrections]` under the `lessons` budget (22.6% of the 33,000 base); `applies: "on_topic"` is a past finding and renders in `[Learned experience]` under the separate, smaller `lesson_experience` budget (5%). The startup rule tier is bounded by a window-INDEPENDENT allowance, `caps.lessons_startup` (`_LESSONS_STARTUP_CAP` = 37,000 characters), passed as the four startup renderers' `directive_budget`. That allowance is `min`'d only against the model-safe ceiling each store hands its directive tier — the vector store's `hard_cap` and the JSONL store's `cap`, which the JSONL renderers pass that same ceiling into (about 500,000 characters at the 1M reference window, and smaller on a smaller model) — so 37,000 binds the rule block on both paths whenever the ceiling exceeds it. (The two stores name that outer bound differently: `get_lessons_context`'s `hard_cap` and `get_context`'s `cap` are the same role, the ceiling the authored tier is taken smaller than, not the ordinary `caps.lessons` budget.) The allowance is deliberately NOT a share of the 33,000 base: standing rules are not discretionary background, and pinning the base to its smallest-window value (`#11996`) had shrunk the derived `lessons` cap about fivefold, so binding the authored tier to it dropped nearly every standing rule from startup. 37,000 restores the allowance a 1M-window session shipped with before that base change (165,000 × 0.226 = 37,290); it does not scale with the model window. The write surfaces that state it today are the `learn_add` MCP tool (whose schema carries the enum and whose description tells the model to decide from what the user said, not from wording), `POST /api/lessons`, and the CLI reaching that route; consolidation states none, so its extracted rows stay unstated and take the rule tier. A row with NO `applies` value — every lesson written before the field existed, and any unrecognized stored value — reads as **unstated** and is served in the rule block, because demoting a real standing rule is the costlier mistake. Within the rule block, authored `always` rows are ordered AHEAD of unstated rows: ranked together on recency or relevance, a crowd of untagged rows displaces exactly the rules the user was most explicit about. The two budgets are separate rather than one shared allowance so a user with many findings cannot crowd out their own rules and vice versa; they are not added into a larger total, and a wider model window does not enlarge either one. The model-safe protected ceiling, `max(3 * 33,000, floor(model_window_tokens * 4.0 * 0.125))` characters, still bounds the two blocks TOGETHER, with one deliberate exception: a block's MANDATORY frame — its label plus the omission notice — is rendered even when the remaining ceiling is too small to hold it, so the two blocks can exceed the ceiling by those frames. That is the same trade as the preferences exception below: a silently absent block is indistinguishable from "this user has no rules", and the frame is a bounded constant while the entries it announces are not. The rule block is served first so a finding yields to a rule rather than the two sharing a shortfall. V2 keeps its essential-delivery and scope gates. **A findings tier is WITHHELD when the request matches none of it.** Ordering alone left a bare greeting spending the entire findings allowance on the newest rows, which are unrelated to the request by construction; `on_topic` means "worth having when the task touches it", so a request that touches none of them gets none of them. The test is the SAME tokenization each store's own ranking sorts with — `any_request_overlap` (unstemmed) for JSONL, `VectorMemoryStore._any_lesson_overlap` (stemmed, the keyword half of `_rank_lessons`) for the vector store — because ordering and admission must read one signal or a tier is suppressed for a match its own ranking found; the vector store stems, so it matches strictly more and must not borrow the unstemmed answer. This costs no recall that filler provided: newest-first never rescued a near-miss either, since it surfaces the newest rows rather than the closest. It applies to findings ONLY — a standing rule, and an untagged row served as one, arrives whatever the message is about. Two boundaries: a request is needed for the test to mean anything, so a caller that supplies NO `query_text` keeps the previous behaviour and stays byte-identical, and a tier with no findings at all renders nothing rather than an empty block. `render_withheld_tier` renders the frame with a notice naming the withheld total, stating that this is not a budget limit, and pointing at `memory_recall`/`learn_list` — a silently absent block reads as "this user has no findings", which is the reading that stops anyone from going to look, and the notice is also what lets the NEXT turn act on findings the greeting turn did not carry. Measured on a 10,000-finding store: a greeting's findings tier costs 298 characters with 0 findings injected instead of filling the 1,650-character allowance, while a matching request still receives 6.

**Startup selection is not re-run per turn, and the withheld notice is what carries the gap.** A session that opens with a greeting and then states its real task on the next turn keeps the greeting turn's lesson selection: the warm path does not reselect. That predates the budgets, and no per-turn retrieval trigger is added here — one would need its own trigger condition, its own budget, and its own coverage across resume, fork and compaction, which is a second mechanism rather than a bound on this one. What the budgets change is that the gap is now VISIBLE where it was not: the greeting turn renders the withheld notice naming the total and the tool, so the follow-up turn is looking at an explicit pointer instead of at filler that silently displaced the finding it needed. Closing it properly is deliberately left to a change that owns the retrieval trigger.

Crossing a budget trims only complete lesson entries, never a partial rule, and an entry too large for the room that remains is SKIPPED rather than ending the selection: the kept set is then a superset of what stopping would give, since an oversized entry is dropped either way and continuing can only add later entries that do fit, never displacing one already accepted. Stopping wasted room a shorter, more relevant entry could have used — five in-budget rows rendered 1,408 of 1,650 characters while omitting a later 76-character line with 242 characters free. Preferences and safety rules are never sliced to make space; a preferences file that alone exceeds the ceiling is the one exception, kept from its head with an in-prompt notice naming the omitted character count and the file to read. Vector lessons keep the lexical relevance order already computed for the request. The JSONL fallback orders EVERY tier the same way (`order_by_request_relevance`, length>=3 word tokens, no stemming and no embedding), and orders them PER TIER so authored rules keep their precedence over untagged rows: newest-first alone drops a row the task is actually about while newer irrelevant ones fit, measured on a 199-lesson store for the findings tier and on a 100-row all-untagged store for the RULE tier — every pre-field row lands in the rule tier, so leaving that tier unordered lost an exact-topic match the vector store kept, since the vector path ranks its whole eligible set before partitioning. Ordering is NOT admission: it decides only which rows survive a truncation that is going to happen anyway, so no row is dropped for being irrelevant that the budget would otherwise have kept. The residual is real and is why the omission notice exists — inside an OVERFLOWING rule tier a standing rule unrelated to this message sorts last and can be the omitted one, which the notice names and points at `learn_list` for; ordering cannot both favour the current task and ignore it. An empty query, or no overlap anywhere, leaves the stored order untouched and renders byte-identically. Both tiers' findings header reads "relevant ones first", which is what each renders: the sort runs before the block is built and deliberately lets an OLDER relevant finding outrank newer irrelevant ones, so a header reading "newest first" would describe the pre-sort order and be false for exactly the crowded store the budget exists to serve. The sort is stable, so equal-overlap rows still read newest-first underneath. **An overflow is always reported and never silent**: each block's notice names its own shown/omitted counts, names the budget it hit, states that the limit is a budget rather than a judgement that the rules stopped applying, and points at `learn_list` (rules) or `memory_recall` (findings). A budget too small for even one entry still renders the labelled block with that notice, because an empty block is indistinguishable from "this user has no rules". Startup spends no embedding inference on either tier. Content below both budgets is byte-identical to the previous behaviour, and a caller that names no budget (every reader predating them) is unchanged: for it the rule block keeps the two-stage contract — COMPLETE below the model-safe ceiling whatever `caps.lessons` says, which is the pinned retain-every-eligible-in-scope-rule invariant, and only an overflow, where that invariant is already unmet, trims to the ordinary lessons budget (`min(caps.lessons, ceiling)`, and the ceiling alone when no lessons budget is named). Naming a tier budget is what asks for a window-independent bound instead, so the two mechanisms never both decide one block, and the rule tier's overflow notice names `learn_list` and `memory_recall` because both reach the omitted rules. Explicit lesson readers can use hybrid relevance and fill the caller's character budget, reporting shown and omitted counts. The JSONL store retains `_MAX_LESSONS_TOTAL = 200`, and prunes **by tier**: findings first, then unstated rows, then authored rules, each oldest-first within its class. A tier-blind `[-200:]` deleted the user's oldest authored rules from disk as cheap findings accumulated, which is strictly worse than a prompt omission — an omission is announced and readable through `learn_list`, a pruned row is gone. Authored rules are evicted only when they alone exceed the cap. A submission the prune itself removes is reported as `refused`, never `inserted`: at the cap a new `on_topic` row is the first eviction candidate, and reporting success for a row that is not in the file is a silent false success the caller cannot recover from. That refusal carries its OWN reason, `store_at_capacity`, and not the content refusal's `volatile_session_fact`: the JSONL store answers both of its refusals with the same bare `refused` string, so `POST /api/lessons` re-derives the cause by re-running the volatile-text predicate — exact rather than a guess, since those are the store's only two refusal paths, and the same re-derivation the listing surface already performs for its withheld marker. Telling a user whose store is full to reword a rule is advice that cannot succeed, so `learn_add` renders a distinct message naming the row cap and `learn_remove` as the action that frees a row. Only the JSONL store raises it; the vector store has no row cap and names its own reasons. The vector store's dedup scan carries the matching guard on the other side: its three delete branches decide on text alone, so an `on_topic` write may retire ONLY another `on_topic` row (the reverse is allowed, since promoting guidance to a standing rule is the direction the user is asking for). Unstated rows are protected too, not just `always` ones: an unstated row is served AS a standing rule at injection, and on an install whose lessons predate the field every row is unstated, so a guard keyed on `always` alone would protect nothing on exactly the stores holding the user's real corrections. Read side and write side classify a row the same way. The `POST /api/lessons` route's model-judged contradiction sweep is a SECOND deletion path ending in `delete_semantic`, and it filters its candidates by the same rule, because guarding one path and not the other leaves a standing rule deletable by a finding. That filter reads the PERSISTED tier, which `write_lesson` reports as `LessonWriteResult.applies`, never the submitted one: the tier is write-once, so a clause-only re-submit of a stored finding — the ordinary enrichment this route documents — omits `applies`, which arrives as `None` while the row keeps `on_topic`. Gating on the submitted value therefore skipped the guard on exactly that input and let the sweep retire a contradictory standing rule, with no recovery, since the sweep's self-heals-next-time note covers a MISSED sweep rather than a wrong deletion. `find_contradiction_candidates` therefore carries each candidate's authored tier on the row it returns: the filter can only apply the guard to a tier it can read, and a candidate without one reads as unstated for EVERY row, which turns the narrowing into a blanket no-op that judges no contradiction at all on a finding submission. It carries each candidate's stored `value_json` too, and the sweep hands it back as `expect_value_json` so its delete is a COMPARE-AND-DELETE. Without it the tier filter is defeatable by timing rather than by tier: the candidates are a write-time snapshot, the delete runs after a per-candidate LLM verdict, and `_lesson_key` keys on rule text plus scope alone — so re-tiering that rule in between (a delete plus a re-add, the documented way to change a tier) puts a DIFFERENT row under the same key, and an unconditional delete tombstones the replacement, including when the replacement is the `always` row the filter exists to protect. A body that changed is reported as kept, not as an error: the row this decided to retire is gone, and a contradiction against whatever replaced it is re-nominated by the next write touching the topic. This is the same guard the inline dedup pass already applies to its own deferred supersedes. The guard gates deletion only, so an `on_topic` rule already contained in an `always` rule is still declined as `substring_covered`. The DECLINE carries the MIRROR of the same rule, because tiering is what makes "already covered" conditional: a submission is covered only while the covering row arrives at least as often as the submission would, and a covering `on_topic` row arrives only when the request is about it. So a STANDING submission (`always`, or unstated) contained in an `on_topic` row is NOT declined — declining it would drop the rule from every unrelated turn while reporting `substring_covered` for a row that does not cover it there, and a re-submit is declined identically so nothing recovers it. In that one direction the submission is stored instead, which can leave a standing rule beside the longer finding containing it; that duplicate is the price of not silently refusing to create the rule. `migrate_from_markdown` carries the authored tier across for the same reason the adjacent lines carry `repo_scope`, and in the same direction: an omitted `applies` reads as unstated, which every read path serves AS a standing rule, so dropping it would promote a row the user filed as a past finding into one injected in every session. It normalizes READ-safely (`authored_lesson_applies`, not the write path's raising form) because that loop reads `lessons.jsonl` directly, so a hand-edited or future-schema value migrates the row as unstated rather than aborting the migration. The listing surface is bounded too — `GET /api/lessons` returns one `limit`/`offset` window and carries `total` and `truncated` so `learn_list` can say `Showing N of M`; `VectorMemoryStore.get_lessons(limit, offset)` honours the offset only on the bounded read, and the unbounded read the scorers use ignores it. That route also reports each row's tier, and `learn_list` marks a finding as `(applies: on_topic)` beside the rule: every overflow notice sends the reader to these surfaces, so a row filed as a finding has to be legible there or a rule the model misfiled stops arriving with nothing to show why, and re-tiering (a delete plus a re-add) cannot even be diagnosed. Only `on_topic` is marked and only an AUTHORED tier is reported — a standing rule and an unstated row are both injected every session, so marking one and not the other would name a difference the injection path does not make. Contract: [learn-cron-dashboard](learn-cron-dashboard.md).

**`applies` is AUTHORED, never derived, and that is the whole reason it is stored.** No property of a stored row separates "a rule the user wants enforced in every session" from "something we worked out once". `source` does not: the consolidator can extract a real safety correction, and a human can type a note about last week's outage. `category` does not: its three values describe subject matter. Keyword shape does not: "always" appears in both classes and reliably in neither. A reader that guessed from any of those would re-decide the user's intent on every turn, and would fail silently in the direction that costs most. So `normalize_lesson_applies` accepts only the two literals, RAISES on a misspelling at the write path (there is no safe clamp — one choice injects a note into every session, the other demotes a standing rule, and both are silent), and treats an omitted value as the supported choice to leave the row unstated. The read path never raises: a hand-edited or future-schema value reads as unstated so one bad row cannot abort prompt assembly for every other lesson. The field is additive and absent when unset in BOTH stores -- the vector writer omits the key, and the JSONL writer drops a `None` through `_serializable` rather than emitting `"applies": null`, which would rewrite every legacy line on the next save and make the two stores disagree about what absence means -- so an unstated row is byte-identical to what the writer produced before the field existed; on the enrich path it is WRITE-ONCE like `category` and `repo_scope`, so enrichment cannot silently demote a rule and re-tiering is a delete + re-add.

**The compatibility limit is real and is not papered over.** A store whose lessons all predate `applies` gets its reduction from the budget alone, not from classification: every row is unstated, so they all compete for the rule budget and the overflow notice is what tells the user which ones did not fit. Classification only starts helping as new lessons are written with a stated value. "Retain an unbounded number of unstated rules" and "a small fixed startup budget" cannot both hold; this is the side that was chosen, and the notice is the mechanism that keeps the trade visible instead of silent. No existing row is rewritten, reclassified or deleted by this change.

Vector scoring builds one scorer per query (`_stored_similarity_scorer`) so the query vector and its norm are derived once instead of once per lesson — the same hoisting `_sqlite_vector_search` does for episodic rows. There is a numpy path and a stdlib fallback, because numpy is guarded by `_HAS_NUMPY`; both produce the same ranking. Stored lesson vectors are un-normalized (unlike episodic vectors, which are L2-normalized for FAISS inner-product scoring), so both norms are divided out per row rather than assuming unit length. A row whose vector has a different dimensionality than the query — a row written under a previous embedding model — is incomparable and scores 0.0, matching `_sqlite_vector_search` and `HybridRetriever._cosine_similarity`, rather than being truncated against the query's leading elements.

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
| Two semantic writes to one key in V1 | User-explicit writes win; automated writes cannot replace a user-explicit fact, and otherwise use the existing confidence precedence | `vector_memory._write_semantic()` |
| Two semantic writes to one key in V2 | Owner correction or verified transcript correction with matching revision replaces the fact; other changed automated assertions remain reviewable proposals | `vector_memory._write_semantic()` |
| Duplicate lessons in V1 | Substring dedup (contained-in-stored declines; contains-a-stored-one deletes it, longer wins, regardless of source), then topic-overlap dedup (shared keywords cover at least 50% of the LARGER keyword set; newer replaces older regardless of source), then embedding dedup (cosine > 0.85; newer replaces older unless a stored near-duplicate outranks the write: `user_explicit` over a lower-authority source, or strictly higher stored confidence). A non-mutating authority pre-pass decides semantic-match refusals before the scan; it mirrors the earlier substring/topic branches, which remain source-blind. Every branch's deletions are deferred and execute only after the semantic write commits, so a call that stores nothing deletes nothing: a `deduped` verdict from any branch and a `refused` from the semantic write each preserve every live lesson row and report empty `superseded` (a lazy embedding backfill for a row the call read may still have flushed, which changes a vector and no lesson). `delete_semantic` takes an optional `expect_value_json`, carrying the comparison inside its own UPDATE, so a queued row is tombstoned only while its stored body is still the version the scan read and one a competing writer changed is skipped -- the same compare-and-write contract the lazy embedding backfill applies, atomic against another process, and what covers the write's own key once `set_semantic` has committed under it. A drain that stops partway (raised error, killed process) keeps the submission and leaves the rows it had not reached: the residue is a duplicate, never a lost lesson, and the next write matching those rows retires them. Every completed deletion is named in `LessonWriteResult.superseded` | `vector_memory.write_lesson()` |
| Distinct lessons in V2 | Different rule text coexists without substring, topic or embedding deduplication. Exact-rule enrichment remains; key-targeted corrections use the owner/revision machinery | `vector_memory.write_lesson()` |
| Contradicting episodic fragments | No explicit resolution: time decay plus MMR surfaces the newer/more relevant fragment | `vector_memory.search_episodic()` |
| A semantic value is superseded | `_retire_stale_episodic()` tombstones episodic rows that quote the old value | `vector_memory._write_semantic()` step 9 |

### Memory across surfaces and channels

`ExecutionContext` is the immutable value passed across dashboard, channel,
subagent, schedule and workflow dispatch. It carries stable member identity,
`MemoryStoreRef(store_id, member_id)`, selection kind, provider template, privacy
mode and app ownership. The owning session/run/job record persists it. There is
no second memory-only registry to synchronize. A context is resolved before
awaited preparation and passed unchanged through queues and continuations.

Member names and provider template IDs are separate namespaces. A member selects
its exact configured store; changing a display name, template or project cannot
retarget that database. The global value is explicit `store_id="default"` with
no member ID. Invalid or unavailable member selection does not fall back to it.
Cross-member dispatch uses explicit `target_member` and ordinary delegation,
application and tool permissions. Without a target, child work inherits the
parent. A provider session with native context cannot be relabeled as another
member; create a new session.

`ContextBuilder.ensure_store` prepares the database in a worker thread and caches
only a validated handle. It calls `open_member_database` for V2 and the existing
V1 initializer for V1. It configures the embedding callable without reconciling
V2 storage on a read. Initialization, failed construction, cancellation and
cache retirement retain their existing handle ownership and locking rules.

V2 manual essentials resolve directly from canonical member configuration and
use guarded document reads without database preparation. Optional SQLite lessons
are included only when reads are permitted and the database is available. A
missing learned service produces an explicit diagnostic while manual essentials
remain usable. No JSONL/global learned fallback exists. Temporary performs no
learned-memory reads. Incognito permits ordinary read-only recall and denies
writes, corrections, lesson deletion and consolidation.

V2 `MemoryStore` delegates history and search to its attached vector store;
preferences/projects remain manual documents. Consolidation, record edits and
learned-rule operations address the exact prepared SQLite service. V1 retains
workspace Markdown, JSONL fallback and separate FTS; its startup context is the
bounded admission described above, with earlier activity behind `memory_recall`.

## Skills (`skills.py`)

Markdown files at `~/.kiro/crew/skills/{name}/SKILL.md` with optional YAML frontmatter (`name`, `description`, `always`).

Builtin skill bodies remain independently usable by custom and lite agents that
may not receive the default base prompt or deferred tool descriptions. Condense
repeated prose within a skill, using local section references for shared steps,
but retain its executable syntax, schemas, consent/refusal rules and resources.
Do not replace these contracts with a pointer to unseen base instructions or
introduce fragment-loading machinery solely to deduplicate prose.

Frontmatter is parsed line-by-line (`_parse_frontmatter`): only a column-0 `key: value` line is a field. A value that is a bare block-scalar indicator (`>`, `|`, optionally chomped with `-`/`+`) is resolved from the indented lines that follow — folded (`>`) folds single breaks to spaces while preserving blank-line counts and more-indented line breaks, literal (`|`) preserves newlines — so a multi-line `description` still routes. Explicit indentation indicators (`>2`) are not supported. The other frontmatter readers stay reconciled with this resolution: the onboarding import gate treats a bare indicator as an activating `always` value (fail-closed), the auto-skill update path's `history._frontmatter_value` resolves block scalars the same way, so a live skill's block-scalar `description`/`triggers` survive the staged-candidate round-trip instead of collapsing to the indicator character, and the skill-provider preview endpoint (`dashboard/handlers/discover.py`) parses SKILL.md with the loader's own grammar, so the previewed name/description match what the installed skill will show.

Supports nested directories (e.g. `skills/utils/tiny-url/SKILL.md`). The skill name is the relative path from the skills root (e.g. `utils/tiny-url`).

Cold discovery overlaps filesystem I/O through a bounded worker pool. Global
directory probes are committed in sorted depth-first order, preserving the same
winner for aliases and pruning cycles before descent. Confined project walks
retain their descriptor-pinned traversal. Metadata reads use bounded batches,
carry the caller's context variables, and join before returning; catalog size
does not determine the number of threads or queued futures. The usage ledger
stays on the calling thread. Prompt construction derives pinned instructions
from the admitted metadata rows instead of scanning the catalog again. It still
discovers every eligible skill and delivers required instructions on the first
turn; a cold catalog is not silently treated as empty. Metadata term replacement
uses a path index, so refreshing one skill does not scan every other skill's
terms. Opening an existing cache adds this index without discarding its rows.
Debug catalog logs separate snapshot loading, directory scanning, metadata
assembly and index persistence. This reduces cold latency without promising a
constant-time scan of an arbitrary filesystem.

**Source precedence** (project-level wins): `$KIROCREW_PROJECT_DIR/skills/` → `builtin_skills/` (bundled). Auto-copied to `~/.kiro/crew/skills/` on first run. Copies entire skill directories (scripts, assets, etc.).

**Retired generated skill cleanup.** `skills.remove_retired_conductor_skill()`
removes `skills/conductor/SKILL.md` only when a descriptor-pinned, capped read has
a CRLF-normalized SHA-256 matching one of the static generator outputs. Linked
conductor directories, linked final names, oversized files, and identity changes
return false. Read and unlink errors reach its best-effort callers to log or print;
empty-directory prune errors are ignored. Platforms without descriptor-relative
opens keep the final-name and size checks while the
ancestor and unlink identity checks degrade to by-name checks. Setup and gateway
startup both invoke it; startup runs it on every boot so a package upgrade needs
no separate setup command.

**Project skills (`<project>/.kiro/skills`) — a different source from the one above.**
`$KIROCREW_PROJECT_DIR/skills/` is a *sync* source: its contents are copied into
`~/.kiro/crew/skills/` and thereafter are ordinary local skills. `<project>/.kiro/skills`
is *discovered in place* for the session whose slot is bound to that project, and is
never copied. A skill found there is reported with source `kiro-workspace`.

The project reaches the loader through its public entry points (`_iter`,
`get_triggered_skills`, `get_context`, `load_skill`, `resolve_dollar_skills`,
`list_skills`), not through `SkillsLoader.__init__`. There are a dozen construction
sites, none of which knows a session's project; threading the constructor would have
required every one of them to learn about a concept only the chat paths have. A caller
that wants project skills passes `project_dir`; every other caller is unchanged and
sees exactly the previous behaviour. The `_iter` cache is keyed per project, so two
chats on different projects cannot serve each other's skills from a shared entry.

**Consent (`skill_trust.py`).** A SKILL.md is prose, but it enters the agent's context
and can instruct the agent to run anything, so loading one out of whatever repository
happens to be open is an execution-adjacent decision. Project skills are therefore
gated on an explicit per-directory grant, recorded at
`<data home>/trust/project-skills.json` (mode `0o600`). That directory is a
whole-directory entry on the keystone deny list, so the agent's own file tools can
neither read the store nor forge a grant; like every other keystone reader, the module
opens the path directly rather than through the agent file gate. Creating the trust
directory is followed by a fail-loud owner-only lockdown; a platform ACL or permission
failure refuses store access rather than leaving a permissive directory usable.

Grants are keyed on the **canonical** directory (`os.path.realpath`), because the
directory *is* the resource. Keying on a softer identity would leave the unkeyed
component forgeable: a second name aliasing one directory would carry its own trust,
and a rename would orphan the record. A symlink therefore resolves to the same grant as
its target, and cannot manufacture a new one.

The grant store is bounded. An idempotent grant for an existing directory still
succeeds at the bound, but a new directory is refused rather than evicting an older
consent silently; the operator must revoke a stored grant first. The API reports this
as HTTP 409 with `code: "skill_trust_store_full"`.

Every unknown resolves toward untrusted: an unreadable store, a malformed store, a
schema version newer than this build, a relative path, a path that does not exist, and a
path naming a file all yield no grant. Refusing to load a skill costs a click; loading
one the operator never consented to cannot be undone. The enforcement memo keys on
content time, metadata-change time, size, inode, and mode, so a permission or ACL change
invalidates cached grants and exercises the unreadable-store path again.

Grant and revoke writes normalize filesystem, atomic-replace, and owner-lockdown failures
to the same unreadable-store error as lock and read failures. The dashboard therefore
returns HTTP 409 with `code: "skill_trust_store_unreadable"` instead of an unstructured
500 when the trust volume is full, read-only, or cannot enforce its owner-only ACL.

`skills.project_skills_enabled` (`SkillsConfig`, default true) is the operator's hard off
switch — independent of any grant, so a directory carrying one still loads nothing when
it is false. Only a missing value or the boolean `true` enables the feature; malformed
truthy values such as the string `"false"`, and a malformed `skills` section itself,
fail closed to disabled. A present `config.json` or `config.local.json` that cannot be
read, parsed, or interpreted as an object also disables project skills: an unreadable
source may contain the operator's hard-off switch and cannot be treated as absent.

**Trust verbs.** `GET/POST/DELETE /api/skills/-/trust`, registered before the
`/api/skills/{name}` catch-all. All three require the configured dashboard owner: the
read reveals consented filesystem paths, while grant and revoke are human security
decisions that authenticated non-owners and app tokens cannot make. A successful owner
authorization emits an allowed dashboard API-access event to the SEL. A refusal is HTTP
403 with `code: "dashboard_owner_required"` and emits the corresponding denied event.
The grant derives its directory from the
requesting chat slot, never from a client-supplied path, so no caller can consent on
behalf of a directory the operator never opened. `DELETE` accepts an explicit `path` so
a grant whose directory has since disappeared stays revocable —
`list_trusted_projects` reports stored rows rather than the enforced set for the same
reason, since an invisible grant could not be withdrawn. The consent snapshot returns
both the readable project path and its canonical `project_key`. The dialog displays the
former and must echo the latter as `expected_key`; grant canonicalizes the current slot
project once inside the grant primitive, requires an exact match, and persists that same
resolution without resolving even the canonical name again. Missing keys fail closed.
Client-supplied text is never resolved, so a UNC/device key cannot trigger a Windows
network probe, while a project symlink retargeted between GET and POST — or a canonical
directory name replaced after comparison — cannot redirect consent to an unreviewed
directory. Revoke first matches
the supplied text against stored keys, so a vanished network grant remains removable;
an unmatched UNC/device path is rejected before any filesystem resolution.

**One project-resolution rule, and it is the strict one.** The catalog
(`GET /api/skills`), the trust read and the grant all resolve their directory with
`requesting_slot_project()` — the project bound to *that* chat slot, with no
cross-slot fallback — because that is what `SkillsLoader` resolves from
(`slot.project` verbatim). The neighbouring `active_project_dir()` additionally falls
back to "the single project some open slot has", which is right for a global settings
page and wrong here in two ways: a grant issued from a chat with no project would
record consent against *another* chat's project, and the catalog would advertise a
skill whose `$token` expands to nothing because the loader sees no project. Revoke
keeps the permissive helper, since revoking only ever narrows what loads. The loader
is deliberately the strict side: teaching it the fallback would inject one project's
skills into a chat not bound to it.

**Consent is confined to the consented directory.** A grant names one directory, and
the project walk never resolves a descendant by path. On platforms with POSIX
directory-descriptor support, the canonical project root and every component down
through `.kiro/skills` are opened one at a time with `O_DIRECTORY | O_NOFOLLOW`, each
relative to the prior handle. Descendants are scanned by directory descriptor and
opened relative to that same pinned handle, so a directory swapped for a link between
enumeration and descent fails the open without resolving its target. Linked directories
and linked `SKILL.md` files are excluded even when their targets remain inside the
project. Traversal stops after 64 directories below `.kiro/skills`; files at that depth
remain eligible, while deeper paths are ignored so hostile nesting cannot exhaust the
Python call stack for a chat turn. Global provider trees retain link traversal for app
registration.

Python does not expose an equivalent handle-relative no-reparse traversal on Windows.
Project skills therefore fail closed as unsupported there: canonicalization returns no
project key before touching the supplied path, so catalog, consent, and loading cannot
initiate SMB authentication through a raced UNC junction. This is intentionally a
capability check, not a best-effort `lstat` sequence; a pre-check followed by a path-based
scan leaves the same swap window. Project skills remain available on macOS and Linux,
where every traversed component stays pinned to a no-follow directory descriptor.

**One enforcement point for every enumerated read.** Enumeration is cached, and now
also PERSISTED across processes (see *The catalog snapshot*), so a path vetted while
genuine can be replaced by a link out of the granted directory before anything reads
it — over a longer window than an in-memory TTL alone implied — and the root that made
it acceptable is only known at enumeration
time. So `_iter_uncached` records, per path, the root it was vetted against, and
`SkillsLoader._read_enumerated_skill_bytes` is the only place an enumerated skill file is
read: it re-checks that root on the *descriptor it opened* (`O_NOFOLLOW` + `fstat`), not
on the path string. Both the body read and the frontmatter/metadata read go through it,
and a guard test fails if either stops doing so.

That guard exists because the two drifted apart once: the body read was hardened while
the metadata read of the same cached paths stayed unchecked, which is not a cosmetic gap
— frontmatter `description` is rendered verbatim into the injected skills index, and
`triggers` / `always` / `inject_on_trigger` decide what loads on every turn. A path with
no recorded root (the global skills dir, `extra_paths`, edition roots) is read
unconfined, which is what keeps an app's registered symlink into its own tree working;
confinement applies to project paths only. An oversized file is skipped with a warning
rather than raised, because the global path applies no cap at all and a chat turn must
not die on a checked-out file. A confined refusal is never reopened: replacement or
removal after enumeration also degrades to no metadata/body rather than propagating an
open error into a chat turn. Confined read-only metadata uses replacement decoding for
malformed UTF-8 so one project skill cannot abort context assembly. Unconfined metadata
reads remain strict because they also serve writers that must never overwrite metadata
they could not decode.

Confined metadata is byte-limited by `PROJECT_SKILL_BODY_CAP` before decoding or
frontmatter caching. An oversized trusted row stays in `list_skills` under its
path-derived key with empty metadata and `size_bytes` set to one byte above the cap;
that sentinel keeps search and context body paths from calling `load_skill`. The
trust-preview catalog omits the row, and direct project `load_skill` applies the same
cap. Oversize and outside-root refusals have distinct log messages. No confined path
stat is added.

No confined project path is rendered into agent-facing context. Startup lists
project skills with a scoped exact-read pointer, and only required project bodies
are read at startup. Explicit reads and trigger delivery retain the descriptor-pinned
reader and project byte cap. This prevents a checkout from replacing an enumerated
file with an escaping link and persuading the agent to reopen that path directly.

The mutable trust-store reader likewise refuses a non-object grant row instead of
filtering it: grant and revoke must never rewrite a partially unknown store and silently
destroy rows a future or hand-edited schema may understand. Read-only enforcement may
still ignore malformed rows because it never writes them back and fails toward no trust.

The dashboard's skill *browse* endpoints are deliberately **not** trust-gated: reading a
`SKILL.md` is how the operator decides whether to grant trust, so requiring the grant to
view the file would make that decision blind. The boundary that matters — an unconsented
project skill never reaching the agent's context — is enforced in `SkillsLoader`.
This does not widen App Kit visibility: an app caller that asks the catalog for a
session-scoped project, or browses a `kiro-workspace` skill, must positively own the slot
named by `X-Session-Key`, and that owned slot must itself name a project. Foreign,
unscoped, projectless, missing, and absent slot identities all return the same 404 and
emit a denied `app_isolation` API-access record. A successful ownership and project-binding
decision emits an allowed `app_isolation` record naming the selected slot. This prevents
the shared-project fallback used by owner dashboard browsing from lending another slot's
project to an app-owned, projectless slot.

**Enforcement is audited, on first use rather than per message.** Granting and revoking
consent are audited `critical=True` where the operator acts. The decision that *uses* that
authority — admitting a project's skills into a session — is audited too, or the log would
show who consented but never that it took effect. It is recorded once per (canonical
directory, outcome) per process, because `_trusted_project_key` runs on every message: one
governance event per message would bury the events that matter and put an SEL write on the
per-message path. A new directory, or the same directory after `project_skills_enabled` is
flipped, is recorded again. Refusals are recorded on the same basis, because "this project's
skills were not loaded" is what an operator debugging a dead `$token` needs. `critical=False`
deliberately: this is a record of an outcome, not an audit-or-deny gate, so an unwritable SEL
must not fail a chat turn — the authority it refers to was already written synchronously when
consent was given. A failed SEL write is not entered into the per-process de-duplication set;
the next enforcement retries it, and only a successful write suppresses later duplicates.

**Untrusted skills are listed, not hidden.** Catalog rows for `kiro-workspace` carry
`trusted: bool`. A silently absent skill is indistinguishable from one that does not
exist, so the picker shows an untrusted project skill with a "needs trust" marker and
choosing it opens the consent dialog instead of inserting a `$token` the loader would
refuse to resolve. The pre-consent catalog asks the loader for a containment-only set
of project-origin names: it does not exercise or audit trust, but it does retain the
normal path validation and first-wins precedence. It also builds the rows and reads
their metadata through the loader's descriptor-pinned confined reader; the legacy
workspace scanner is used only for global Kiro skills, so a linked project target is
never touched merely to construct a row. Genuine untrusted rows therefore remain
visible while escaped paths and project rows shadowed by global skills stay hidden.
Because the description and repository scope are checked-out, untrusted text rendered by
the dashboard, both are passed through the exfiltration-URL and credential redactors before
leaving the backend.
Audit records may retain the canonical path, but a failed audit write never
copies that path into the ordinary application log.
The dialog snapshots the requesting chat slot, current project, and a monotonic request
identity with the selected skill. If the operator switches chats or projects, closes the
dialog, or starts another consent request while a grant is pending, the grant may finish
for its original slot but its stale completion cannot close the newer prompt or insert a
token into the current draft.
The picker and its focus prefetch cache by both slot key and current project, because a
slot may change projects without changing identity; a project switch therefore cannot
serve the prior project's fresh catalog for the cache TTL. Both production composers
provide that project identity. A caller that cannot provide it gets a zero-staleness
fallback, so closing and reopening the picker revalidates the ambiguous cache key.

**Search is session-scoped.** Signed sessions resolve their project and active
agent mapping at the gateway; unsigned MCP fallback remains global-only.

The bundled `session-summaries` skill is on-demand, guidance-only: it explains the
chat session summary panel (see [session-summary](session-summary.md)) — what it
shows, its token cost, and how to make a session summarize well — so the agent can
help a user enable and interpret it. It does not enable the feature or trigger
generation, and holds no runtime-written frontmatter, since a builtin skill is
re-synced by `rmtree` + `copytree` on upgrade.

**`GET /api/skills` coalesces concurrent readers onto one scan, and stores nothing.**
The catalog assembly is filesystem-heavy (`os.walk` plus per-file frontmatter reads, package
path globs, per-skill resolve/read, agent annotation), and the defect this addresses is that
N simultaneous skill-menu opens each paid for their own scan. `_assemble_skills_catalog` in
`dashboard/handlers/prompts.py` fixes that with single-flight coalescing: the first reader
assembles, readers queued alongside it take those rows instead of scanning again. Measured
against a counting assembler, 8-way concurrency goes from 0% to 87.5% redundant-scan
elimination — eight opens cost one scan.

**There is no stored result and no TTL, and that is what makes the invariant cheap.** The
leader's rows are offered only while another reader for the same key is still inside
`_assemble_skills_catalog`; when the last one leaves, they are dropped. So a read that is not
part of a concurrent burst always scans current on-disk state, the base's recorded default
("No result cache: the endpoint always reflects current on-disk state, so freshly
created/installed skills appear immediately") is preserved, and **no mutation path anywhere
owes the catalog an invalidation**.

**The mechanism is one assembly lock per key** (`LoopBoundLock` values in a registry, the shape
#4800 established) — fast path, lock, re-check under it, where the re-check is the join. Per key
rather than global, so readers of different projects still scan in parallel as the base did; the
registry entry is dropped with the waiter count, and a test pins the parallelism.
`_assemble_skills_catalog`'s docstring is authoritative for the contract — which readers can be
served older rows, and the bound — so it stays next to the code it constrains and is spelled once.

**The `?agent=` filter is deliberately NOT part of the key.** It is applied downstream as a
comprehension over the assembled rows, and an end-to-end test drives two agents through the
real endpoint in both orders to keep that true rather than merely currently-true — a join that
ever shared the FILTERED result would fail whichever agent asked second.

The `kirocrew-dev` family is repository-maintainer guidance, not user-project
advice. Its `kirocrew-codebase-refactor` skill owns repository-scale structural
campaigns: hotspot baselines, coherent module ownership, non-overlapping worker
waves, behavior-equivalence evidence, stale-work recovery, and landed structural
metrics. It delegates isolated implementation, test authoring, PR delivery, and
monitoring to `kirocrew-worktree-dev`, `writing-tests`, `prepare-pr`, and
`babysit` respectively, so those contracts remain single-owned.

The bundled `kirocrew-dev/babysit` skill is an on-demand, pointer-on-trigger recipe.
Its explicit trigger vocabulary covers babysit/watch/monitor phrasing for pull
requests, so ordinary requests reach the recipe without placing the whole body in
every prompt. The base prompt points long-lived pull-request readiness requests to
this skill and prefers the structured path whenever typed provider facts fully
determine the objective; `monitoring.prefer_structured_arming` decides whether the
tool descriptions state that as a condition to satisfy or as the default for a
supported pull request.
For a supported GitHub, GitLab, Azure DevOps, or Bitbucket Cloud pull request
with the `review_ready` objective it maps the canonical URL to one exact bounded
`monitor_watch` call and makes retained inspection state authoritative; its
acknowledgement remains pending until the current turn ends, so agent inspection
happens only at the start of a later user/wake turn. It also explains that
reported-token enforcement may be incomplete while runtime and completed-turn
limits remain hard fallbacks. It does not reproduce provider polling policy in
the prompt. Its legacy `monitor_start` recipe is limited to unsupported targets
and requires a positive cadence, cycle cap, and runtime bound while naming the
full-turn/token cost and ordinary approval policy. A supported provider's setup
or authentication refusal never falls back to the costly legacy loop.

**Loading:**
1. **Always-on**: skills with `always: true` have full content injected every new session
2. **On-demand**: skill summaries (name + description + dir path) in session context; LLM can `cat` the file when relevant

Skills with auxiliary files (scripts, assets) include `dir` path so the LLM can `cd` and run them.

**Discovery (`skills.lazy_load`, default true):** startup and post-compaction use
one bounded directory. The default is the usage-ranked index with a family hint
for omitted rows; false selects a shorter search pointer. A `skill://` mapping
restricts availability, not eager body delivery. Directory, search, paginated
list, exact reads and `$full/key` expansion resolve the same project-aware mapping.
Unqualified `$leaf` fallback is permitted only when unique. External mapped files
use stable `mapped/<path digest>/<leaf>` keys; a mapping cannot re-admit disabled
apps or bypass the existing project consent boundary.

Ordinary mapped and project skills are activated on demand. `skill_search` always
provides `action="list"` plus `offset` for complete discovery and `action="read"`
plus the exact `key` for activation, even while the body index is incomplete.
Required `always:true` bodies share `PINNED_SKILL_BODIES_CAP` (99,000 UTF-8 bytes),
including rendered framing. Exceeding the capacity or refusing a required read
raises `SkillContextCapacityError`; no final slice may silently discard required
instructions. Bounded global reads use `safe_read_file_bytes_nolink`, retaining
sensitive-path, descriptor identity and hardlink checks while allowing validated
provider links. Project reads retain descriptor confinement and their byte cap.
The explicit unbudgeted catalog renderer remains available to non-startup callers.

Native Kiro 2.21.2 progressively loads bodies but places every mapped skill's
metadata into startup context. Native CLI launch views omit those skill resources
and suppress implicit native skill inheritance; Crew supplies the bounded directory.
The authored agent spec remains the mapping authority. See
[context management](../../architecture/context-management.md#4-default-agent-vs-other-agents)
for native view and inherited steering behavior.

**Usage ledger (`skill_usage.py`, `SkillUsageLedger`):** in-memory per-skill hit tally with debounced, atomic persistence to `skill-usage.json` (`SKILL_USAGE_FILENAME`, co-located with the Kiro Crew home). Entries older than a 30-day TTL (`_MAX_AGE_SECS`) are dropped on load/flush so a stale skill stops occupying a top-K slot. Hits are recorded in two places: the **body-delivery loop** in `context.py` (`_record_use`, called only after `load_skill` succeeds and the body is appended to the prompt) and in `resolve_dollar_skills`. However, since `max_triggered` defaults to 0 the body-delivery recorder is inactive in stock config — `$skillname` is the only source of hits, so lazy-load ranking is effectively recency-only unless the trigger matcher is re-enabled (`max_triggered > 0`). A trigger match alone does NOT earn a hit — only actual delivery does, so pointer-only skills and false-positive matches do not inflate the ranking. Best-effort: ledger init failure falls back to recency-only / unweighted ranking without breaking skill loading.

**`skill_search` MCP tool (`kirocrew-core`):** supports `search`, `list` and `read`.
A signed session resolves its active template and project at the gateway. An
unreadable or missing custom template fails scope resolution rather than widening
to the global catalog. A custom template with no mapping has an empty scope; only
the default `kirocrew` template without mappings gets the global catalog. An
unsigned caller searches the global installed catalog, without borrowing a session.
Search combines metadata and body term matches: query coverage first, then inverse
term frequency, metadata coverage, usage and stable full key. This prevents common
metadata words from burying a result carrying multiple query words in its body.
Search/list have stable-key results and offset pagination; search does not record
usage. Exact reads share the resolved mapping and the bounded file reader.
An external mapping rooted above a catalog prunes that catalog before descent.
Its entries come only from normal discovery, retaining project consent, no-link
confinement, disabled-app filtering and first-wins precedence.
Each literal external `SKILL.md` mapping scans its own directory, so sibling
skills are not scanned again for every explicit mapping.

**Term index (`skill_search_index.py`, `SkillSearchIndex`):** metadata and body
queries answer from a SQLite term index at `skill_search_index.sqlite3`
(`SKILL_SEARCH_INDEX_FILENAME`, beside the usage ledger), not by reading files. Read
from disk, the fallback cost one file read per skill on every call, so the query that
needs the body most — one matching no metadata — was the one that read every
`SKILL.md` present, at a cost following total body bytes rather than the number of
matches. Global metadata is persisted beside body terms with the same file identity
fingerprint; a new loader can reuse both without reading unchanged skill files.
Metadata also stores a digest of the complete global skill file. Ranked search
uses that digest to collapse byte-identical mirrors without reopening their bodies;
paginated listing and exact-key reads retain every key. Schema changes rebuild this
disposable index. Short-lived consumers close their loader's SQLite handle before
returning or removing an owned temporary data home.
Enumeration and stat checks still run, so zero reads does not mean zero filesystem
work. Cold body refresh is incremental (250 ms per query), with a separately bounded
read fallback and an explicit incomplete flag. Repeating a query advances refresh;
listing and exact reads do not wait for the index. Debug timings distinguish catalog
enumeration, metadata reads and body-index refresh/query work. Confined metadata and
bodies are never persisted in this global index. The index stores each skill's distinct body terms, from the same
`recall_terms` tokenizer the query goes through. Indexed bodies are admitted through
`safe_read_file_bytes_nolink`, so a link, hardlink, sensitive target, non-regular file
or file swapped between validation and open cannot place terms in SQLite. Rows use a
`device:inode:ctime_ns:mtime_ns:size` fingerprint: the added inode identity and change
time distinguish a same-path replacement that preserves modification time and byte
size, while keeping the warm path metadata-only. These properties are load-bearing:

- **Confined project bodies are never indexed.** A project skill is read through the
  descriptor-pinned reader under `PROJECT_SKILL_BODY_CAP`, and its text belongs to
  that checkout; a home-level copy of its terms would outlive the grant and be
  visible to a session that never opened the project. Those skills keep the
  read-at-search path, bounded by the project's own skill count.
- **Prefix, not free substring.** A stored term matches a query term that is its
  prefix, so `deploy` still reaches a body saying `deployment`, and the range scan
  replaces the corpus-sized read. The exclusive upper bound is the term with its
  last code point incremented (skipping the surrogate block, `None` above the
  maximum), not a high sentinel: `\uffff` encodes as `EF BF BF` while an astral
  character starts at `F0`, so a sentinel bound sorted BELOW the astral terms it
  was meant to include. A query term strictly INSIDE a body word stops matching:
  the query is tokenized the same way, so that case is a near-miss rather than a
  hit — `rollback` no longer matches a body whose only occurrence is inside
  `scrollback`.
- **One lock-guarded connection.** The connection is opened with
  `check_same_thread=False` and every public method takes an `RLock`. A skill
  search legitimately arrives on different threads (the dashboard route hands it
  to a thread, the MCP tool runs in its own subprocess), and because a raise
  latches the index unusable, a thread-bound connection would drop every later
  search back to reading files for the life of the process.
- **The tokenizer is part of the key.** Stored terms are `recall_terms` output, so a
  change in how it splits or normalizes leaves rows a new query can no longer match —
  a miss, with nothing raised and nothing logged. `tokenizer_signature()` hashes the
  tokenizer's ANSWER on a fixed probe and sits beside the schema version, so a
  mismatch drops and rebuilds without any future editor having to remember a bump.
  Hashing its source was rejected: a comment or a rename would discard every row for
  no behavioural reason.
- **Both paths score alike.** The direct read tokenizes and prefix-matches through
  `_body_term_hits`, the same rule the index applies, because both can answer inside
  one search. A substring scan on the read side would make a skill's rank depend on
  which side answered for it.
- **A declined body costs only itself.** `sync` answers with the set of keys it
  cannot store. The caller retries those through the bounded safe reader with
  the same admitted root; hardlinks and other unsafe files remain refused. One
  pathological file does not send the whole catalog back to reading every body.
  Stale terms for a refused key are deleted and no fingerprint is stored, so the
  refusal is retried rather than cached.
- **Best effort.** A read-only home or a corrupt file makes methods return `None`;
  search falls back to bounded body reads and reports incomplete recall when its
  work budget expires. Discovery does not require a writable database. A BUSY or
  locked database does not latch the index unusable: another process holding the
  write lock past the two-second timeout says nothing about the file's health.
  Deleting the file costs one re-index; a schema bump drops and rebuilds it.

Ranking orders distinct query-term coverage, then term rarity, metadata coverage,
usage, and the stable key. A term found in metadata and body counts once; metadata
coverage breaks a tie rather than outweighing other words found in the procedure.

Explicit entry provenance distinguishes project, global and external mapped rows;
a legitimate `mapped/...` catalog key never changes its admission or body budget.
External mapped rows retain the canonical root admitted during enumeration for
metadata, indexed/fallback body search, exact reads and `$` activation. A later
ancestor swap must not redefine that root. Approved provider targets retain their
own admitted roots; external mappings retain the global body allowance rather
than the smaller project-body allowance.

Installed exact reads also accept POST `/api/skills/-/discover` JSON with
`scope="installed"`, `action="read"` and `key`. MCP uses this transport so URL
escaping and HTTP request-line limits do not truncate nested keys. GET remains
compatible. Both gateway and MCP accept up to 32,768 key characters; the POST
request envelope is bounded at 512 KiB, and existing body-response limits remain.
The same signed session, app-slot guard, mapping and project consent determine
access. An unresolvable bound agent fails closed with HTTP 409 and
`skill_scope_unavailable`, without falling back to the global catalog.

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
resolution resolves every served skill, which on the event loop would stall every
session in the gateway. It no longer walks the tree after cache expiry (the
snapshot serves the list and expiry only schedules a re-walk), but the resolution
itself is still per-skill filesystem work and stays off the loop. Both the
initial `tool_call` and its `tool_call_update` refinement are observed, since
which one carries `rawInput` is provider-specific, deduped by `tool_call_id`.
`_maybe_credit_skill_read` then records only on a `status == "completed"` result
(`tool_final`), so a read that was denied, errored, or never ran leaves no
delivery; that call is in-memory and safe inline. A cheap `SKILL.md` substring
gate runs before the offload, so a tool call touching no skill costs a substring
scan; observer failures in either phase are logged and swallowed.

**Provider registry — two built-ins.** `_build_registry()` registers `skillsh`
(the public catalog) and `github` (a repository *addressed* rather than searched),
each through the same `admits_registry("skill", name, api_base)` policy gate, with
edition-contributed providers appended after. The network layer is one
implementation: `skill_providers/_http.py` owns the internal-address screen, the
per-provider redirect allowlist and the bounded body read, and each provider binds
its own allowlist and SEL audit label onto it. A provider carrying its own copy of
those checks would drift, and the drift would be found as a bypass — so a new
provider inherits the boundary instead of restating it.

The `github` provider is an **import, not a subscription**, and that posture is
what makes it safe without a review step: `owner/repo[@ref][:path]` resolves the
ref to a commit ONCE, every discovered row's id carries that FULL commit (so the
preview and the install fetch what discovery showed rather than re-resolving a
branch that moved; an abbreviated ref would be re-resolved, and a branch whose
name is hex can shadow a 7-character prefix), the bundle is read from
`raw.githubusercontent.com` pinned to the full commit, and
`.skill-import-source.json` records it beside the installed files. Nothing reads
that record back, so upstream cannot change an imported skill; re-importing is the
update path and goes through the same human-only gate.

**A bundle is complete or it is refused**, which is one rule covering every way a
file could be left out: a failed fetch, a body that is not UTF-8, a per-file or
running-total size ceiling, a file count over the ceiling, two names that collide
where case is ignored, a truncated git-tree response, an install key that the
handler's 64-character `_slugify` would truncate onto another skill's key, and a
directory that merely *contains* skills rather than being one. Each refusal logs
its reason. The alternative — writing the subset — reports success for a skill
missing a file its own instructions reference, so it fails later, elsewhere, as a
puzzle. Paths go through ONE allowlist -- a segment matches
`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, depth at most 4, no two paths equal under
`casefold` -- which is narrower than any filesystem and narrower than the bundle
writer's own `".." in rel_path` guard. An allowlist rather than a refusal list
because a refusal list is something review can keep extending: successive rounds
named Windows-illegal characters, control characters, trailing dots and spaces,
byte-versus-character limits, Unicode normalisation, reserved device names and
component length, each a separate clause. Stating what is permitted ends that.
Three rules survive beside it, for the cases the shape permits: `..` anywhere, a
trailing dot, and a reserved device stem. The cost is real and accepted -- a
repository carrying `my file.md` is refused rather than imported -- and it buys the
property that an import lands complete or names the file it cannot take.
`TestWriterCompatibility` pins both directions. `fetch_skill_bundle` can only answer `None`, so the
reason currently reaches the log rather than the user — an error channel on the
`SkillProvider` Protocol is follow-up work. Branch tracking / re-sync is deliberately out of scope (#746 covers the
adjacent design). Requests are unauthenticated (60/hour/IP), so private
repositories are not reachable. An installed skill's key comes from the provider, not from its id:
`_slugify` lowercases and folds `/`, `@` and `:` all onto `-`, so it is not
injective over these ids and `foo-bar` would share a key with `foo/bar`. The
optional `install_slug` hook (documented on the Protocol in `base.py`, probed by
`_install_slug` in the handler) lets a provider supply
`<label>-<12 hex of sha256(owner/repo:path)>` instead. The digest covers the
case-sensitive identity and excludes the ref, so re-importing at a newer commit
lands on the same key and hits the existing 409 -- an update, not a second copy.
The hook is additive: a provider that omits it, including `skillsh`, keeps the
derived-from-id key exactly, and whatever a provider returns is still slugified
and still gated by `_SAFE_SLUG_RE`, so it names a key without widening what a key
may be.

**Registry discovery — `skill_discover` / `skill_fetch` MCP tools (`kirocrew-core`).**
The agent-facing twins of the dashboard's Skills → Discover panel, covering the
skills that are *not* on disk. Both are read-only and reach the existing
`skill_providers/` registry through the gateway rather than the network directly,
so provider timeouts, the 1 MiB response cap, the SSRF denylist, and
`_redact_external` all still apply:

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

**The catalog snapshot — no turn waits for discovery.** `_iter` answers from one of
three tiers, none of which walks the tree on the calling thread:

1. this loader's in-memory list, inside `_ITER_CACHE_TTL_SECS`;
2. the same list past that deadline. Expiry queues a re-walk on the
   `skill-catalog-refresh` worker and returns the stale list. This is the point of
   the tier: the deadline used to be BLOCKING, so on a large tree the message that
   happened to arrive after expiry paid a full walk — about one message in twelve
   was seconds slower for no reason a user could see;
3. the stored enumeration for this root set (`skill_catalog` / `skill_catalog_scope`
   in `skill_search_index.sqlite3`). A restart lands here, and so does every
   short-lived loader — the unsigned MCP fallback in `mcp_tools/skills.py` builds one
   per call — instead of walking.

The scope key is a digest of the skills root, the resolved `extra_paths` and the
trusted project key (`_catalog_scope_id`), because two loaders can share a project
and still enumerate different trees. Stored rows keep their **ordinal**: enumeration
order IS precedence, so a re-ordered snapshot would hand a caller a different file
for the same skill name than the walk would.

A snapshot records only what EXISTS. It is not an authority on who may read it:
mapping scope, disabled apps, project consent and `repo_scope` are all re-evaluated
live on top of a served list, and every body still goes through
`_read_enumerated_skill_bytes`. A revoked project grant is therefore not merely
unused but unreachable — `_trusted_project_key` turns back into `""`, which selects
a different scope than the one holding that project's rows. It is also not an
authority on CONTENT: the stored stat fingerprints are deliberately never returned
by `_load_catalog_snapshot`, because nothing knows how old they are, and trusting
them would make a restart serve a description for a file edited out of band since. A
row may name a file; it may never vouch for its bytes.

**A stored row is not an admission, and the index is agent-writable.**
`skill_search_index.sqlite3` is a VISIBLE crew-home leaf, so a row is a CLAIM that a
path was once enumerated, never evidence that anything vetted it — and the unconfined
branch of `_read_enumerated_skill_bytes` is a direct `read_bytes()` with no
sensitive-path or UNC screen of its own. Two things therefore stand between a stored
row and a read, split because they cost differently:

* **at adoption, containment AND key/path binding.** An unconfined row must name a
  path under `_snapshot_admitted_roots()`: this loader's skills dir, its resolved
  `extra_paths`, or the same provider roots the mapping walker admits — an app
  symlinks its skills into the tree, so a legitimately admitted target sits outside it
  by construction. The key must also DENOTE that path (`_key_denotes_path`): the two
  fields are consumed by different gates — a mapping is matched against the path while
  the body is delivered by re-resolving the key — so a row pairing one skill's key with
  another's path would pass a mapping admitting the second and serve the first. Both
  checks are lexical, so screening a whole snapshot costs no syscalls. A row that fails
  either refuses the WHOLE snapshot rather than being dropped: a trimmed catalog would
  be served as the complete enumeration, and falling back to the walk loses only speed.
  The same holds for a confined row whose root is not the trusted project key that
  selected the scope, and for a table past `_MAX_CATALOG_ROWS` (100,000 rows) or
  carrying a field wider than `_MAX_CATALOG_FIELD_CHARS` (4,096) — unbounded
  materialization of an adversary-controlled table is an out-of-memory crash on load.
* **at the read, admission.** Containment does not say the file is still a regular
  file in a non-sensitive place, so each surviving unconfined path is recorded in
  `_snapshot_unadmitted` and `_admit_snapshot_path` re-runs `validate_file_path` on it
  before its first read. Admitting retires it from the set, so a repeated read costs
  nothing, and a walk that republishes the scope admitted every path it returned and
  clears the set outright. A path this process walked never pays the check.

That placement is what keeps the cost shape: the O(N) work stays on the walk, and
admission is paid once per row actually read, at the one point every enumerated read
already goes through.

**Refresh is a background re-walk, not a per-file incremental index.** There is no
filesystem watcher: an app-side mutation (`_invalidate_iter_cache`) is the immediate
path, and everything else converges within `_ITER_CACHE_TTL_SECS` /
`_CATALOG_REVALIDATE_AFTER_SECS` through a full re-walk on the worker. A watcher plus
per-file updates would buy a smaller refresh, not a faster turn — the whole walk is
already off the request path — at the cost of a new dependency and a second source of
truth about what the tree holds.

Mutations drop the stored snapshot AND move two counters. Both are needed, for
different races: leaving the snapshot lets the next process serve the pre-mutation
list; leaving the counters alone lets a walk already in flight publish its
pre-mutation answer on top of the clear, an invalidation a background refresh
silently undoes. `_catalog_generation` fences this loader's own worker, and the
index's `skill_catalog_epoch` — read before a walk and re-checked inside
`store_catalog`'s own `IMMEDIATE` transaction, because a deferred one leaves the check
and the insert open to another process's `drop_catalog` landing between them — fences
a walk running in ANOTHER process. A ROOT-SET change is a mutation for the same reason
and takes the same path: `_adopt_extra_paths` invalidates rather than only clearing
the in-memory list, since the stored snapshot belongs to the old root set. And
`_run_catalog_build` captures its scope id BEFORE the walk and refuses to publish when
the root set moved while it ran, so rows enumerated over one configuration never
become another's answer.

**The post-mutation cold window is intended.** An invalidation empties the list rather
than serving the pre-mutation one, so on a tree whose walk outlasts
`_COLD_CATALOG_WAIT_SECS` the next turn re-enters the cold path and carries the
*discovery in progress* notice, with `always: true` bodies not yet injected. Serving
the pre-mutation list instead would contradict the one thing the mutation path
guarantees — that a skill written through the app is visible immediately — and would
do it silently. The invalidation therefore also QUEUES the re-walk rather than waiting
for the next turn to demand it, which bounds that window to the walk's own duration.

A refresh is single-flight per scope, and the worker drains scopes SERIALLY, so
several sessions in different trusted projects cost one walk of the shared global tree
at a time rather than one each. A snapshot read off disk is revalidated only when it is
older than `_CATALOG_REVALIDATE_AFTER_SECS`, so a process that starts, answers one call
and exits does not queue a walk of a tree another process enumerated moments ago.

The worker is a DAEMON thread, started on first need and never joined by `close()`,
which sets `_closed` first so a build already running publishes nothing into a loader
that is going away. `concurrent.futures` was rejected here for one measured reason: it
joins its non-daemon workers at interpreter exit, so a walk this design deliberately
abandoned would delay process exit instead (3.0s vs 0.05s on a 3-second task). A
finished walk still PERSISTS, though: a build in flight keeps the index handle and
closes it on its own way out, because on a host served only by short-lived loaders —
the unsigned MCP fallback closes its loader as soon as one search returns — that store
is the only thing that lets the next call skip the walk, so discarding it makes every
call re-walk forever. `close()` also SETS every outstanding completion event, since a
queued build the worker abandons never reaches the `finally` that would have set it
and a cold caller would otherwise sit out its whole budget.

**The one case that waits, and what it is allowed to withhold.** A scope with no
stored snapshot at all — a machine's first run, or one whose index file was deleted —
waits on the background build for at most `_COLD_CATALOG_WAIT_SECS`. A small tree
finishes inside that budget, and finishing is what makes `always: true` bodies known
and therefore honored. Past the budget the partial answer is served, the scope is
marked in `_catalog_incomplete`, and `catalog_status()` reports `"building"` so a
caller can distinguish "still discovering" from "no skills" — an empty list alone
cannot tell those apart. `search_skills` reports the same thing through the
`search_incomplete` flag the MCP tool and the dashboard already surface.

`get_context` consumes that verdict rather than returning `""`: it emits an explicit
*discovery in progress* notice saying the set may be incomplete and an always-loaded
skill may not have been injected yet, charged against the caller's budget like any
other block. This is the deliberate resolution of a real conflict, not an oversight.
Required instructions must never be SILENTLY dropped (which is why
`SkillContextCapacityError` fails loudly rather than trimming a body), and a first run
cannot know which skills are `always: true` without enumerating. The tradeoff taken is
to state the incompleteness rather than either block the turn on a large tree or
present a truncated set as complete.

**An exact key stays readable while that first walk runs.** `read_scoped_skill` falls
back to `_exact_read_while_building`, which composes the candidate from a root this
loader owns and the COMPLETE key, so `team-b/review` can never resolve
`team-a/review` and a bare leaf resolves nothing. `_safe_name` rejects anything that
is not a plain catalog key, so the key is never a path. The mapping must still admit
the candidate, a disabled app's skill stays hidden, and `repo_scope` is still checked
by the caller. Two limits are deliberate: it is reachable ONLY while
`catalog_status()` is `"building"`, because a complete enumeration is the single
authority and a second resolution path running beside it is how the two drift apart;
and it withholds the confined project tier, whose containment is knowable only from
the enumeration.

**What still costs O(N) per turn, and what no longer does.** `list_skills` used to
stat every unconfined row on the calling thread purely to decide whether its
persisted metadata row was still current. The walk now records those fingerprints
(`_catalog_fingerprints_for`, on the worker) and `list_skills` reuses them, so a turn
on a process that has walked takes no stat at all. A process still serving the STORED
list holds no fingerprints of its own and stats as before — validation, not discovery:
no walk and no body read — because a fingerprint read off disk records what an earlier
process saw. One cost is knowingly left: `_scoped_entries` expands an EXTERNAL mapping
prefix (one that resolves outside the catalog roots) with its own walk, which no
snapshot covers. That walk is bounded by the operator-declared prefix rather than by
the installed-skill count, so it does not grow with the corpus this section is about.


**Trigger matching (`get_triggered_skills`) — per-message hot path.** Runs on
every non-custom-agent message via the context builder, scoring word-overlap of
the message against each skill's `triggers` (negative `!`-prefixed triggers
exclude). To keep it off the per-message filesystem/config hot path:
- the discovered skill-file list is served from the **catalog snapshot** rather
  than a walk (`_iter`; see *The catalog snapshot* above). Expiry of
  `_ITER_CACHE_TTL_SECS` SCHEDULES a re-walk and keeps serving the previous list,
  so no message is ever the one that pays for discovery; it is invalidated
  immediately by `create_auto_skill`;
- the walk that rebuilds it (`_iter_skill_files`, on a worker thread) asks the
  sensitive-path fence through `is_sensitive_resolved_path` against the
  `realpath` it has already computed for loop detection and containment, with
  the fence's anchors resolved on that same thread, so the ~1.4k per-scan
  checks on an install with a few provider packages submit nothing to the
  two-worker `mc-pathres` pool. That pool is FIFO and sized for
  the event loop; a scan flooding it from worker threads queued the loop's own
  resolutions behind the backlog until the loop-stall watchdog fired
  (see [security.md](security.md));
- the `max_triggered` cap is read from the config watcher's snapshot
  (`_max_triggered_now`) — a plain attribute read, so still no
  `KiroCrewConfig.load()` per message, and `kirocrew config set
  skills.max_triggered` applies to the very next message from any writer. With no
  snapshot yet the construction-time value stands, so a loader built from an
  explicitly injected config honours that config rather than resolving a cap the
  injected document never carried (the absent-key default is 0, which would
  suppress every skill);
- `extra_paths` is re-resolved by `reconfigure(cfg)` on a config write, running
  the SAME screening as construction — expanduser, resolve, `is_sensitive_path`
  reject, existence check — and failing closed per entry, so a root added by hand
  to `config.json` can no more reach a credential directory than one present at
  boot. Edition-contributed roots are preserved and stay LAST (lowest
  precedence): they come from the platform context, not config, so a config write
  must not drop them. The discovery cache is cleared, so the next listing walks
  the new roots instead of serving the old set for the rest of the TTL;
- exactly **one** SEL audit event is emitted for the matched set (skipped
  entirely when nothing matched, the common case), not one per skill scanned.

A match injects the skill's **full body, by default and unchanged.** What is new
is a per-skill way out: `inject_on_trigger: false` in a skill's frontmatter
reduces its contribution to a single `[Relevant skills for this message]` line —
name, truncated description, `SKILL.md` path, containing dir — rendered by
`trigger_hint()`, and the agent reads the file if the skill applies, the same
affordance `## Available Skills` already directs it to. `split_triggered()`
partitions one match into bodies and pointers, so a mixed match emits both.

That opt-out applies only to unconfined installed and provider skills. A project skill
always goes through full-body injection even if its frontmatter says
`inject_on_trigger: false`, and the catalog reports that effective behavior. Otherwise
the pointer would invite the agent to reopen a mutable checkout path directly after the
descriptor-confined metadata read, letting a link swap bypass the confined reader.
`split_triggered()` therefore forces every row with a confinement root into the body
partition, and `trigger_hint()` independently refuses to render confined paths.

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

**Disabled-app skill gating.** When an app is disabled (`_disabled_app_names()`),
its bundled skills are withheld across all user-facing surfaces: trigger matching
(`get_triggered_skills`), per-turn index listing and search (`list_skills`,
`get_context`, `search_skills`), always-injected bodies (`get_always_skills`),
and explicit `$skill` token resolution (`resolve_dollar_skills`). An unreadable
app registry fails open so a transient read error never hides enabled skills.
Internal plumbing helpers (`load_skill`, `_served_key_by_realpath`,
`resolve_ledger_aliases`, `_resolve_path_and_root`) remain ungated so
reconciliation and pinned paths function without modification.

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
`size_bytes` reuses the stat the frontmatter cache already needed for an unconfined
skill's mtime, so those rows still cost one stat. A confined project row never stats
its cached path: a checkout can replace that name with a Windows UNC link after
enumeration, and a stat would initiate the outbound connection before confinement ran.
Its size and content-digest cache token instead come from bytes admitted by the
descriptor-pinned no-link reader.

The dashboard's structured skill editor owns five frontmatter fields (`name`,
`description`, `always`, `triggers`, `tags`) and must leave every other byte of the
block alone. It does that by parsing the block with a real YAML parser (the `yaml`
package, `parseDocument`), replacing the **source range** of each field it owns, and
copying every other byte through unchanged.

Two properties of that design are load-bearing, and both were paid for:

- **The parser decides structure, not a line matcher.** What counts as a key, as a
  continuation of a value, or as a comment comes from the YAML grammar. `#1790`
  spent four review rounds proving the alternative cannot be finished — each
  accepted continuation shape revealed another valid one (indented lines → block
  scalars → indented keys → blank lines → indentless `- item` entries) — and the
  case it still left open (`#1825`) was a top-level line that is not a recognized
  `key:` and follows a modelled key. A line-based walk can only attach such a line
  to the preceding key, so re-emitting that key from form state destroyed it: a
  `# comment`, a quoted `"my.key"`, or a dotted key silently vanished during an
  unrelated edit. Source ranges have no such gap — those lines are not
  inside any modelled key's range, so they are copied where they stand.
- **Untouched bytes are COPIED, never re-serialized.** `Document.toString()`
  normalizes: an indentless list comes back indented, a folded `>` scalar comes
  back re-folded. Both are byte changes to a field the form does not own. Splicing
  ranges is what makes the invariant exact rather than approximate. A field the
  form DOES own is copied too when its value was not edited, so its original
  quoting, block-scalar style and inline comment survive as well.

A block the parser does not fully accept — a duplicate key, a tab used as
indentation, an unclosed quote, a non-mapping or flow-mapping root — is **not
spliced at all**, and neither is a block using **anchors or aliases**: a managed
field can carry the anchor an unmodelled field aliases, so re-rendering it would
drop the anchor and leave the alias dangling in a file that no longer parses. The
same applies to any mapping layout whose **top-level keys are not at column 0** —
an explicit key (`? name` then `: value`) puts a marker before the key that
replacing the key's own range would leave behind, and a root-indented mapping would
receive an appended field at a different indentation from its siblings, which is a
YAML error rather than a cosmetic difference. One column check covers both.

A block is also refused when any **managed field shares its line with a comment**.
Four review rounds each found a different way that weaving a new value into such a
line goes wrong (an inline comment lost on drop, a block-scalar header comment lost
on replace and on drop, a trailing comment absorbed into the value once an edit made
it multi-line), and the last of those fixes emitted `description: |- # note`, a form
the BACKEND reader takes as literal text while discarding the content. Every
arrangement of value and comment on one line is its own case, which is the same
unfinishable enumeration this design exists to replace, so the splice declines and
the block is edited raw. A comment on the line ABOVE a key is `commentBefore`, which
the splice never touches, so it does not trigger the refusal.

One refusal is detected in the SOURCE rather than the AST: a YAML document-end marker
(`...` at column 0). The parser drops it, and anything after it belongs to a second
document `parseDocument` never returns, so no AST rule can see it -- while an append,
the path a MISSING managed field takes, would land after the marker where the reader
never looks. Teaching the splice to insert before it would mean re-deriving a position
from a construct the AST does not carry, which is the line arithmetic this design
removes, so the block is edited raw instead.

One more refusal comes from the FORM's own representation rather than from YAML:
`triggers` and `tags` are a single-line input holding a comma-separated list, and YAML
gives that field two legitimate shapes. The requirement is the same for both -- come back
unchanged from what that input can carry -- but it lands differently on each. As a
SCALAR (the `alpha, beta` form the editor itself writes) only a carriage return or
newline is fatal: the input cannot hold one, so the browser strips it and a block-literal
list merges into a single entry; commas there are the field's own separator and
round-trip by design. As a SEQUENCE, read joins the items with `', '` and save splits on
`,`, trims each piece and drops the empties, so an item must additionally be a non-empty
string scalar, equal to its own trimmed text, and free of commas. Anything else is edited
raw. The rule DEFAULTS TO DENY, which is its substance rather than a detail: five earlier
versions were "allow unless a problem is recognised" and each shipped a hole where an
unrecognised node kind fell through -- non-scalar items, empty items, multiline items,
multiline scalars, then a mapping value. The kinds this field can represent are exactly
three (absent, a single-line scalar, a sequence of single-line scalars), so those are
named and everything else is refused, including node kinds a future YAML version adds.
Note that a FOLDED value is fine either
way: folding turns its breaks into spaces, so it is genuinely single-line.

**The reader has the mirror of that rule.** Reading frontmatter with a real YAML parser
is what lets the frontend and the backend DISAGREE about what a file already means:
`description: "first\nsecond"` is one newline to the parser and the two characters
backslash-n to `SKILL_LOADER`, which never unescapes. Main could not diverge this way,
because it read with the same line dialect it wrote with. So a managed scalar whose
backend reading differs from its YAML decoding is not spliceable at all -- adopting one
reading and saving it would silently redefine the file for the code that loads skills.
The comparison skips fields carrying a comment on their line (the comment rule's case,
and the backend does not strip a trailing comment). Block scalars are NOT skipped, and
the history of that decision is worth keeping: three attempts to decide agreement from
the INDICATOR were each wrong -- the reader's six resolvable indicators, then the four
that survive chomping, then the discovery that its fold ends in `.strip()`, which removes
LEADING whitespace as well, something no YAML chomping mode does. So `always: |-` with a
blank first line reads `true` on the backend and newline-then-true in the parser, and
nothing about `|-` says so. Agreement depends on the CONTENT.

The rule therefore SIMULATES rather than predicts. For a bare LITERAL indicator the
reader's fold is short enough to reproduce faithfully (drop trailing blank lines, dedent
by the first non-blank line's indent, join, strip), so the two readings are compared like
any single-line value and the field stays editable when they match. A FOLDED (`>`) form or
an explicit indicator is refused outright: the folding rules for `>` are intricate, and
reproducing them to compare is the cross-language coupling this design exists to avoid.
That refusal narrows what the structured editor accepts relative to the first version of
this change, which could splice a folded value; the trade is a capability for a guarantee. This is the READ direction only: a boundary-quoted value TYPED into the
form is still written, as a block literal, because there the author's intent is
unambiguous.

**The writer is bound by the reader's dialect, not by YAML.** `SKILL_LOADER` removes
one matched level of wrapping quotes (collapsing a single-quoted scalar's `''`) and
resolves bare `|` / `>` block scalars, and does nothing else -- no backslash
unescaping, no explicit indentation indicators. So a managed value is only ever
emitted in a form that dialect decodes: a plain or quoted scalar with no backslash
escape, or a bare block scalar. A value whose OWN TEXT begins or ends with a quote
character also goes to a block scalar. The reader would survive most of those
inline -- it removes exactly the one wrapping level the YAML writer would add -- but
the block literal is the one representation with no quoting subtleties on either
side, so the route is kept as a guarantee rather than a necessity. That rule tests
the value, not the rendered line -- a correctly wrapper-quoted scalar begins and
ends with a quote by construction, and routing those to a block scalar costs a
value its leading whitespace for nothing. A value whose first line begins with
whitespace would
force YAML to emit `|2-`, which the reader would take as the literal value, so the
leading whitespace is dropped instead -- the same bounded loss the previous
line-based assembler had, preferred over losing the whole value.
`parseSkillContent` returns such a block with `raw` set, which opens the raw editor
with the real file text and surfaces the parser's own message where there is one;
the structured form would otherwise have to guess where its fields live in bytes it
could not parse, and a wrong guess rewrites the file. Reading is deliberately more
tolerant than writing: `parseFrontmatter` renders whatever pairs it can from a
malformed block, because a meta strip cannot corrupt anything.

Two ordering rules inside the splice are load-bearing, and both were review
findings rather than foresight:

- **The unchanged check runs before the drop branch.** A managed field whose value
  is legitimately empty in the file (`tags: []`, a bare `triggers:`,
  `always: false`) renders as "absent", so consulting the writer first deleted a
  line the user never edited. `always` also needs its own comparison, because the
  form models it as a boolean: a file saying `false` and a file omitting the key
  are the same form state, and comparing rendered text would read the former as an
  edit.
- **A block value's source range ends past its terminating newline**, unlike a
  plain scalar's or a flow collection's. The end is normalized before use, or
  rewriting a multiline field concatenates the following key onto the new value and
  dropping one deletes the following line. Appending a field likewise inserts
  before any trailing whitespace, so a blank line before the closing fence
  survives.

The invariant to preserve when touching this code: editing a modelled field leaves
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
- `update_skill(name, content)` — REPLACES the SKILL.md inode via `atomic_write()`
  rather than writing through the existing one, so the document survives a write
  that fails part-way. A hardlink to the old inode, or a handle already open on
  it, therefore keeps seeing the pre-update bytes.
  **What the replacement does NOT reproduce**, stated because an inode-replacing
  write is where these get lost silently and the same limits apply to the steering
  and `/api/file-write` update surfaces that adopted `atomic_write` first:
  - **Ownership.** The fresh inode belongs to the gateway's own uid/gid. An
    unprivileged writer cannot give a file away (`chown` to another user needs
    `CAP_CHOWN`), so a *cross-owned* SKILL.md that the gateway can write changes
    owner on save. Permission bits and the POSIX ACL are carried, so the effective
    grant does not widen — the previous owner loses access rather than a new
    principal gaining it — but the change is real and irreversible by this process.
  - **A Windows DACL.** The carry is POSIX xattrs only
    (`ACCESS_CONTROL_XATTRS_SUPPORTED` requires `os.listxattr`/`getxattr`/`setxattr`,
    which Windows lacks), so on Windows the replacement lands on the DACL it
    inherits from the containing directory rather than the one the replaced file
    carried. A file the operator had tightened *below* its directory's inheritance
    is therefore widened back to it. Closing this needs a `platform_compat`
    primitive to READ a DACL — `restrict_to_owner` only writes one — and it belongs
    to `atomic_write`, so it must land for all three surfaces at once rather than
    by reverting one of them to an in-place write that a mid-write failure or a full
    disk would turn into data loss.
- `delete_skill(name)` — removes entire skill directory
- All three address the leaf relative to a descriptor pinning the parent chain
  (`pinned_fs`) where the platform has the descriptor-relative syscalls, so an
  ancestor swapped for a link after resolution cannot redirect the write. Windows
  keeps the by-name floor. `_DIR_FD_SUPPORTED` names exactly the extra
  descriptor-relative calls these branches issue — `os.mkdir` (create, under the
  parent descriptor `create_skill` already walked), `os.unlink` (update, via
  `atomic_write`'s staging cleanup) and `os.stat` (delete, via `stat_at`) — on top of
  `pinned_fs.supports_pinned_walk()`. `os.rmdir` is NOT probed: delete's removal is
  a by-name `shutil.rmtree`, the residual noted below. `update_skill` additionally
  requires `atomic_write.pinned_parent_replace_supported()` (the descriptor-relative
  rename) and takes the by-name floor without it, because `atomic_write` refuses a
  `parent_dir_fd` it cannot publish through rather than quietly writing by name.
- Once the skill directory is pinned, `SKILL.md` is never addressed by name again —
  including the metadata read. `_write_skill_md` passes the descriptor as
  `open_access_control_source(skill_file, dir_fd=…)`, so the mode and the ACL come
  from the inode inside the pinned directory. A by-name open there would let a
  directory replaced at the skill's name supply both while the rename published
  into the pinned original, handing the real skill back with permissions chosen by
  whoever did the replacing.
- `create_skill` resolves the parent chain **once** and addresses everything below it
  through that one descriptor — the leaf directory (`os.mkdir(name, dir_fd=)`), its
  `SKILL.md`, and the rollback that removes both. It deliberately does NOT route the
  leaf through `pinned_fs.create_and_open_dir_pinned`: that helper resolves
  `skill_dir.parent` with its own `realpath` and pins it again, which is a second
  chance for an ancestor swapped since the first resolution to be followed, and which
  would leave the create and the rollback addressing two different directories — the
  skill landing outside the skills root while the rollback reports an identity mismatch
  on an unrelated one. The helper's other two jobs are reproduced at the call site: a
  name that already exists is refused because `os.mkdir` under the pinned parent raises
  `FileExistsError` — the exclusivity is the syscall's, not a flag on a helper — and a
  link or non-directory at the leaf becomes a refusal rather than a raw errno.
- These paths use `open_dir_pinned`, not `pin_parent`, because `self._dir / name` is
  a lexical join nothing canonicalized — so that walk's own `realpath` is the first
  resolution of the chain, not a second one. `pin_parent` is for a caller that
  already holds a `realpath`ed path (the steering and file-write update surfaces);
  used here it would refuse the ordinary symlinks that legitimately sit above the
  skills root, a symlinked `$HOME` being the common one.
- `create_skill` lands `SKILL.md` at the **umask default on both branches**: the
  pinned `O_CREAT` passes `0o666` precisely because that is what the by-name floor's
  `write_text` produces, so the pin changes no permission default and the two
  branches cannot diverge per platform. It is also the mode `prompts.py`'s own
  pinned `O_EXCL` create of user content passes, through the same `pinned_fs` walk.
  A tighter default for user-authored skill bodies is a policy change that has to
  cover both branches and both platforms, so it does not ride this migration.
  The skill DIRECTORY does land at `0o700` on the pinned branch against the floor's
  umask default: the mode is passed at the call site, on `create_skill`'s own
  `os.mkdir`, and it is the same `0o700` `pinned_fs.create_and_open_dir_pinned` gives
  every caller, so the two cannot diverge if a later surface does borrow the helper.
  It is strictly tighter than the floor. `update_skill` preserves the target's
  existing bits either way.
- `update_skill` / `delete_skill` return `False` for a REFUSED target as well as a
  missing one — a parent that cannot be pinned, or an access-control source that
  cannot be opened `O_NOFOLLOW` — which the dashboard reports as its existing 404.
  Callers must not read `False` as "the name does not exist".
- `create_skill`'s `exists()` guard is a by-name check with a window after it, and
  **both branches refuse a rival that wins that window** rather than writing through
  it — the pinned branch because `os.mkdir` under the pinned parent raises
  `FileExistsError`, the by-name floor via `mkdir(parents=True, exist_ok=False)`.
  Both refusals are `mkdir(2)`'s own, which cannot succeed on a name that already
  exists; neither depends on a flag a helper happens to offer. Without the second, two
  concurrent creates on a platform without `openat` would both `write_text` the same
  `SKILL.md` and both report success, losing one submitted body and never producing
  the documented 409.
- `create_skill` is **all-or-nothing**: a failure mid-body (a short write, ENOSPC, an
  interrupt) rolls back the `SKILL.md` *and* the directory the call created, both
  through descriptors, and **both halves verify identity** because both address a NAME
  under a descriptor: the leaf via `pinned_fs.unlink_verified`, which stats through the
  directory's own fd and unlinks only if the inode is still the one the create made, and
  the directory via `pinned_fs.remove_dir_verified`, which stages it aside under the
  pinned parent and re-checks `(st_dev, st_ino)` before removing it. A rival that
  replaced either name inside the failure window therefore keeps its own object, and the
  rollback removes this object or nothing — a bare `unlink`/`rmdir` would delete whatever
  answers to the name, turning a cleanup arm into a data loss.
  Capturing those identities is itself a syscall that can fail (EIO/ESTALE on a network
  filesystem), so **both `os.fstat` probes sit inside the guarded region**: a failure to
  capture is rolled back like any other rather than escaping with a half-made skill that
  answers every retry with 409. The leaf's identity is then asked for **once more through
  the descriptor the call still holds**, because that descriptor is what the close at the
  end of the guarded region takes away and an EIO on a network filesystem is usually
  transient; the re-probe addresses a descriptor rather than a name, so it can never
  answer with another object. **No unlink runs without an identity.** With both probes
  failed the leaf name STAYS: removing it would be removing whatever answers to that
  name, and that is a file this code has never read. The cost is bounded — the identity
  probe precedes the first `os.write`, so a rollback with no identity is one where nothing
  was written, and what is left is a skill with an EMPTY body rather than a truncated one.
  It is listed, and both `update_skill` and `delete_skill` reach it, so the recovery is a
  save rather than a shell. (`remove_dir_verified`'s `rmdir` refuses the now non-empty
  directory and puts the name back, which is what keeps it findable.) Without the
  rollback a half-made skill is permanent rather than untidy: the leftover directory
  makes the `exists()` guard answer False forever, so every retry is a 409 over a
  truncated body `list_skills()` still serves. A rollback that cannot finish is logged
  (with the staging name when one was left) and never masks the original error.
  One arm is deliberately outside that rule, and it is an `os.rmdir` rather than an
  `unlink`: where the DIRECTORY's own `os.fstat` failed there is no identity to verify
  and the `rmdir` under the pinned parent runs anyway. `rmdir(2)` cannot remove a file
  and refuses a non-empty directory, so the most it can destroy is a rival's EMPTY
  directory, while skipping it would strand this call's own directory behind a
  permanent 409 — the harm the whole arm exists to prevent. That bound is what makes
  it the one place a name is removed unverified.
- Path traversal protection: `_safe_name()` rejects `..` and `\` (allows `/` for nesting)

**Foreign-agent import:** only user-authored skills are eligible. Imported
skills are isolated under the `imported/<source>/...` namespace so they cannot
replace built-in, project, existing user, or auto-generated skills. Discovery
and copy are symlink-safe: symlinked skill roots/files, path traversal, and any
resolved path outside the declared source skill root are rejected and reported.
On Windows, reparse points (including directory junctions) are link-like for
both source traversal and destination ancestry checks and are rejected by the
same boundary.

Claude includes global skills and `<workspace>/.claude/skills`; a lineage source
uses workspaces resolved from both `workspace_dir` and `project_dir` pointer files
and scans `<workspace>/skills`, while the source root's own `skills` tree remains
excluded because its user-authored provenance is not reliable. Re-import
deduplicates through provenance instead of overwriting the destination. A package with
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

**Dashboard endpoints**: GET/POST `/api/skills`, GET/PUT/DELETE `/api/skills/{name:.+}`. POST sanitizes name to lowercase + hyphens + slashes. The mutating verbs (POST, PUT, DELETE) are owner-only and SEL-audited — app tokens and non-owner subjects get a 403 before any write — and the same owner gate fronts pending approve/dismiss/dismiss-all, pin, and inject-on-trigger, so every mutating skill endpoint in `prompts.py` refuses non-owner callers (the discover-module install endpoint carries its own internal-secret refusal instead; see learn-cron-dashboard.md's Skills CRUD entry). The two open-standard territories are read-only through this endpoint (`READONLY_SKILL_KEY_PREFIXES` in `handlers/prompts.py`): PUT or DELETE on a `kiro-user/` or `kiro-workspace/` key answers 405 with `Allow: GET` and `code: readonly_skill_prefix`, and a POST whose *sanitized* name lands in either territory answers 400 with `code: reserved_skill_prefix`. Those keys resolve per-machine / per-session on read (`_resolve_skill_root`) while `create/update/delete_skill` join the key onto the core skills root, so a write would edit a different file than the reader was shown; GET is unaffected. GET `/api/skills` discovery (kirocrew `list_skills()` os.walk + frontmatter, `list_kiro_skills`, and the skill→agent annotation) is fully offloaded to the dedicated `discovery_executor` pool (`executors.py`) via `collect_skills_blocking`, so it never stalls the event loop past the loop-stall watchdog on large catalogs. The annotation is O(agents) — `annotate_skills_with_agents` parses the agent JSONs and pre-expands each agent's `skill://` globs once, then matches every skill against that in-memory set. The discovery pool is deliberately separate from the reaper-critical `maintenance_executor` so browser-triggered scans can't starve the orphan sweep. When `?agent=<name>` names an agent whose `skill://` globs are non-empty (the filter is actually applied), the response is the envelope `{"skills": [...], "agent_scoped": true, "agent": <name>}` instead of the bare array; every unscoped path keeps the bare-array shape (#6028 — see the fuller rationale in learn-cron-dashboard.md's Skills CRUD entry).

**Skill browse containment** (`_resolve_skill_root`, `read_skill_file` in `handlers/_shared.py`): the tree (`/api/skills/{name}/-/tree`) and file (`/api/skills/{name}/-/file`) endpoints serve any directory the resolver returns, so the resolver is the containment boundary. A candidate's *parent* must resolve at or under its own root (that is what rejects a symlinked intermediate directory), and so must the RESOLVED candidate itself — with two exceptions, both in `_leaf_is_contained`: `LEAF_SYMLINK_PREFIX` = `kiro-user/`, where an edition may install `~/.kiro/skills/<name>` as a link into its own tree; and a leaf whose resolved target is a directory an app DECLARES as a skill, because `apps.bridges._register_skills` symlinks each declared skill into the kirocrew skills root (flat AND `skills/<app>/` namespaced) with the target in the app's own tree — without that the browse side would be stricter than the loader and list skills in `GET /api/skills` that 404 when opened. The admissible set is the manifest's own `skills` entries, read through `bridges._registration_source` (the immutable package copy for a shipped builtin), NOT the app's root: an app tree also holds that app's data, tokens and rendered configs, and a link planted at `<root>/x -> <app>/data` must not serve them. The `package/` branch returns before that shared block, so it applies the same containment itself against the resolved `_edition_package_roots()` set — an edition packager can plant an escaping link in its own root like any other, and `package/` carries no allowance. Checking the parent alone for every prefix was a whole-filesystem read primitive: `<project>/.kiro/skills/x -> /etc` resolved to `/etc` and the endpoints enumerated up to `SKILL_TREE_MAX_ENTRIES` names and returned up to `SKILL_FILE_MAX_BYTES` per file from it, and `is_sensitive_path` is no backstop there (it is a `$HOME`-anchored credential denylist, not a containment check). A deliberate cross-checkout leaf link (`<project>/.kiro/skills/x -> ~/dotfiles/skills/x`) is indistinguishable from the exfiltration shape and is refused with it; the sanctioned way to browse a skill tree that lives elsewhere is `skills.extra_paths`, which makes that location a root of its own. `enumerate_skill_catalog`/`_collect_skills_under` carry the same per-prefix policy, because a key enumeration offers must be one the resolver accepts. File bytes then come from `hooks.safe_read_file_bytes_nolink(within_root=…)`, so containment holds on the opened descriptor rather than on a path resolved earlier: re-opening by name left a check-to-use window (an ancestor swapped for a symlink after the check) and no hardlink guard — `resolve()` does not follow a hardlink, so a link to a file outside the root passed the path check and was served. The root is passed as `within_root_is_canonical=True`, because it is already resolved: re-`realpath`ing it at read time would let the skill directory replaced by a link redefine the root it is being contained to. `list_skill_tree` walks by name, so a root replaced after admission is enumerated as whatever its name then denotes — filenames only, and the same exposure the rest of the by-name filesystem surface carries; holding the root across a traversal needs the walk itself to be descriptor-relative, which is a separate change. Every descriptor-level refusal answers one message (`access denied`, 403) rather than naming which guard fired. Refusals are already SEL-audited by the endpoints (`api_skill_tree` / `api_skill_file` outcomes), and the read is NOT trust-gated by design — reading a `SKILL.md` is how an operator decides whether to grant project-skill trust (#4777).

**Pending-review endpoints** (`dashboard/routes/skills.py` → `handlers/prompts.py`): GET `/api/skills/-/pending` (list), GET `/api/skills/-/pending/{slug}` (detail), POST `/api/skills/-/pending/{slug}/approve`, POST `/api/skills/-/pending/{slug}/dismiss`, POST `/api/skills/-/pending/-/dismiss-all`. Approve routes on the candidate's `kind` to `approve_pending_skill_checked` / `approve_pending_update_checked`, which raise `PendingApprovalRefused(reason)` instead of returning `None` (the unsuffixed `approve_pending_skill` / `approve_pending_update` wrappers still return `None` on refusal, but no production caller reads that contract any more — only tests do, and their deletion is tracked in #11089), and the handler maps the reason to a coded refusal so the Skills tab can say WHY the click did nothing. The routing itself is guarded at the CONSUMPTION point, not only in the handler: a raising detail read drops `kind` to `None` and the handler defaults to the new-skill path, so `approve_pending_skill_checked` re-reads the candidate's `.meta.json` itself and refuses `kind == "update"` with `kind_mismatch` (a 409 through the generic coded fallback) — promoting an update fresh would create `auto/<candidate-slug>` while its live target stays unchanged, and `not_found` would trigger the dashboard's approved-or-dismissed-elsewhere recovery copy for a candidate that is still pending:

| Status | `code` | `PendingApprovalRefused.reason` | Meaning to a client |
|--------|--------|--------------------------------|---------------------|
| 404 | `pending_skill_not_found` | `not_found` | No candidate at that slug (unsafe slug or no `SKILL.md`). The row is gone; refetch the list. |
| 409 | `live_skill_exists` | `live_exists` | A live `auto/<slug>` already holds the name (new-candidate path only). |
| 422 | `script_validation_failed` | `script_validation_failed` | Body adds `report`: the `validate_scripts` `{filename: [finding, ...]}` map, redaction-scrubbed by `_redact_validation_report` (every filename and finding string through `redact_exfiltration_urls` + `redact_credentials`, redacted BEFORE shortening so a cut cannot leave a credential fragment the scrubber no longer recognises) and BOUNDED at retention: at most `_PENDING_SCRIPT_MAX_ENTRIES` filename entries, `_VALIDATION_REPORT_MAX_FINDINGS` findings each, `_VALIDATION_REPORT_MAX_STRING_CHARS` characters per string. Anything dropped — including entries whose redacted names collide — is counted once under a `<truncated>` key, so a shortened report never reads as a complete one. Raised on the raw scripts, or on the re-validation after in-place redaction. |
| 409 | `pending_approval_refused` | any other | Body adds `reason`: `target_missing` or `stale_base` (update candidates), `invalid_layout`, `redaction_failed`, or `promotion_failed`. |

Success is 200 `{"approved": "<auto/name>"}`; a malformed slug is still an uncoded 400 `invalid slug`, and a non-refusal exception an uncoded 500 `internal error`. Before this contract every refusal collapsed into one uncoded 409 (`not found, a live skill already exists, or script validation failed`). **Dismiss has no 409**: 200 `{"dismissed": slug}`, uncoded 400 `invalid slug`, 404 `pending_skill_not_found` (the same code as approve's not-found, so the dashboard's refetch-and-explain recovery keys on one code), uncoded 500. **Dismiss-all** has no 409 either: it REQUIRES a JSON object body with a non-empty `slugs` string array — 400 `invalid_body`, `invalid_slugs` or `slugs_required` otherwise — and answers 200 `{"dismissed_count": n}` or 500 `internal_error`. The detail 404 (`not found`, uncoded) also covers a candidate `get_pending_skill` refuses to read because it contains a symlink. The `script_validation` verdict the list and detail payloads may carry, and the SEL outcomes of a refused approval, are specified under Auto Skill Creation → Pending review contract.

**LLM tool mechanisms:**
- MCP tools (native): kiro-cli calls directly — **preferred for all LLM-facing operations**. The complete managed-server inventory, emission gates and per-agent opt-in sets are canonical in [MCP architecture](../../architecture/mcp.md#managed-servers).
- Skills are for on-demand knowledge only (not for CLI command wrappers — use MCP tools instead)

## MCP Discovery (`mcp_discovery.py`)

Auto-sync runs at startup and on demand from the dashboard. The authoritative managed-server inventory and its always-on, gated and opt-in rules are in [MCP architecture](../../architecture/mcp.md#managed-servers).

**Server sources** (merged by `list_servers()`):
1. `agents/defaults.json` → `mcpServers` (default: none beyond the managed servers)
2. `~/.kiro/agents/kirocrew.json` → `mcpServers` (installed config, merged)
3. `~/.kiro/settings/mcp.json` and `~/.kiro/crew/mcp.json` (scanned at startup and on-demand)
4. Edition-contributed provider-global scopes from `extra_mcp_scopes()` (the public edition contributes none)

**Startup behavior**: gateway calls `_init_mcp_discovery()` which runs `discover_servers_to_sync()` + `sync_to_agent_config()` to auto-add new servers from mcp.json, then logs all configured servers. Discovery/sync failures are caught independently so `list_servers()` always runs. Additionally, `server.py` fires `_bg_mcp_probe()` as a background task at startup to populate the probe cache.

**sync_to_agent_config()**: delegates entirely to `install_agent()` — the single authoritative merge that reads all source files, resolves commands, normalizes each spec's `env` through `env.emit_env()` (a declared `PATH` is expanded to the full effective one), and atomically writes the agent config. There is deliberately no `kiro-cli mcp add` subprocess: it was an unsynchronized second writer of the same file whose output the rebuild overwrote moments later.

**sync_discovered_servers()**: the one serialized discover→write entry point (`discover` + agent-config rebuild + Claude Code sidecar) shared by `POST /api/mcp/sync` and the sessions-restart pre-sync. A module mutex serializes concurrent callers, closing the read-modify-write race the two handlers used to have.

**On-demand discovery** (dashboard): `sync_discovered_servers()` triggered by "Discover & Sync" button.

**Command divergence** (`_commands_diverged`): an existing server is only re-synced when its `mcp.json` command differs from the one recorded in the agent config. The two legitimately differ in spelling because `agent._resolve_command` stores the `shutil.which` result while `mcp.json` keeps the bare name, so the comparison folds path resolution:

- A basename match is only accepted when one side is a **rooted path** and the other a **bare name** (no separator), since PATH lookup is what produced the rooted form. Two distinct rooted paths sharing a basename (`/opt/a/srv` vs `/opt/b/srv`) and a CWD-relative path (`bin/srv` vs `/usr/bin/srv`) each name a specific different file, so both stay divergent.
- The basename acceptance holds only while the rooted side still names a **runnable file**; a pin that does not resolve is divergence, so a re-sync is proposed instead of leaving the server to fail at spawn time. The probe is `isfile` + `X_OK`, the predicate `agent._resolve_command` applies to an absolute command, not `shutil.which`, which can report a good file as unresolvable inside a user-namespace sandbox. Only the **agent side** is probed: `mcp.json` holds what the user authored, while the agent entry holds what a past resolution pinned, so only the pin can go stale on its own. A path not rooted in a named volume on this host (the other OS's spelling, a driveless Windows root) is never probed, so a portable `mcp.json` is never called stale.
- On Windows the keys are `normcase`+`normpath` folded (paths are case-insensitive and accept either separator), and a trailing `PATHEXT` suffix is stripped from the **rooted side only** — `shutil.which("npx")` returns `...\npx.CMD`, which would otherwise read as divergent from `npx` on every cycle and re-sync + reset every session at each startup. Stripping both sides would wrongly collapse distinct executables (`foo.bat` vs `foo.cmd`).
- A leading separator with no drive letter (`/usr/bin/srv`) counts as rooted on Windows even though `ntpath.isabs` rejects it, so an `mcp.json` authored on macOS/Linux is read identically on every host.

**Probing**: external servers are spawned and must complete both JSON-RPC `initialize` and `tools/list`; managed servers may instead use their in-process declaration. An initialize that answers without a successful tool list is an error, not a usable server. Each result carries `probedAt` (wall-clock) and `probeMode` (`handshake` or `declared`) so the UI can say when and how the status was established. The handshake timeout defaults to 15 seconds and is configurable through `dashboard.mcp_probe_timeout_secs` from 5 to 120 seconds. The stdout buffer is 1 MiB (responses can exceed the default 64 KiB), and subprocess cleanup runs in `finally`. Results are cached in `handlers.py` for 10 minutes; GET `/api/mcp/probe` returns cached results non-blocking, while POST `/api/mcp/probe` forces a fresh probe and updates the cache.

**MCP temp**: the probe and runtime apply one rule through `sandbox.classify_declared_temp_env`. The probe, `gatewayd._spawn`, and `spawn_backend` run that rule off the event loop before honouring a spec-declared `TMPDIR`/`TMP`/`TEMP` (matched case-insensitively). A cleared declaration is re-emitted under canonical uppercase keys with ambient siblings dropped. A refused declaration is dropped as a whole, the managed temp takes over, and one WARNING names each refused key, its path and the cause. `sealed` means the path lies inside `<data home>/run`; the classifier checks the original `realpath`, the lexical spelling, and `(st_dev, st_ino)` identity against the sealed parent. `unclassifiable` means the canonical form cannot be established, or the declaration is relative. Relative values are refused because the daemon would classify them against its cwd while the backend child resolves them against `work_dir`, so one classification cannot describe both paths. A classifier exception refuses every declared key under `check-failed` and names the exception. If managed allocation fails after a refusal, no refused temp value reaches the child. On `win32`, `classify_declared_temp_path` returns `None` without resolving the path because Kiro Crew has no native Windows sandbox backend and nothing seals `run/` there. The probe's managed directory lives under `<data home>/run/mcp-tmp/probe-<id>/tmp`, is carved out of the sandbox seal, and is exported on all three canonical keys. This rule prevents the known sealed-parent conflict. It does not certify that every arbitrarily declared temp directory exists or is writable.

**Enable/Disable**: `POST /api/mcp/toggle` adds/removes `@name` from `tools` and `allowedTools` arrays in installed config (`~/.kiro/agents/kirocrew.json`). Does NOT modify `agents/defaults.json`. Disabled servers stay in `mcpServers` but kiro-cli won't load their tools.

**Sync**: `POST /api/mcp/sync` runs `sync_discovered_servers()` off the event loop, then applies OAuth hints to the kiro-global file and resets all active sessions so kiro-cli picks up the new config (~30s).

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
collision, and Kiro Crew-managed servers (including `kirocrew-core` and
`kirocrew-cron`) are protected from replacement, deletion, or shadowing by an
imported definition. Malformed effective-source JSON or non-object
`mcpServers` values contribute no names and cannot abort an import. Repeated
imports deduplicate through the provenance ledger.

## Auto Skill Creation (`skills.py` + `history.py`)

Hermes-style autonomous skill creation from completed sessions. **Opt-in, and STAGED for approval** — generation is **off by default** (`skills.auto_create_from_sessions` defaults **false**; enable via `kirocrew config set skills.auto_create_from_sessions true` or dashboard Settings → Skills). When on, candidates land in a pending-approval queue (`skills.approval_required` defaults **true**) and nothing goes live unattended. Pipeline: detect (during consolidation) → generate → metadata dedupe → pending queue → human approval → live → archive-if-unused.

Key v2 elements (all under `skills.*`):
- **Staged approval:** new skills route to `auto/.pending/<slug>/`; approve promotes to `auto/<slug>/` (dashboard: Skills → Pending review). Auto-approve for prose-only is opt-in via `approval_required=false`; **a script-bearing candidate never auto-publishes**: with approval enabled it stages (only validator-passed scripts kept), and with approval disabled a candidate whose supplied scripts ALL fail validation is rejected outright (SEL audit, reason `all_scripts_rejected`) rather than staged into a queue the user opted out of or published as disguised prose.
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
            → default or script-bearing path: stage_skill_candidate() writes auto/.pending/<slug>/
            → owner approval promotes the candidate to auto/<slug>/
            → approval_required=false: prose-only candidates may publish directly
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

### Pending review contract (`list_pending_skills`, `get_pending_skill`, `_pending_scripts_verdict`, `approve_pending_*_checked`)

The HTTP statuses and `code` values of the pending endpoints are tabulated under Skills → Dashboard endpoints above; this section fixes what the payloads MEAN.

**`script_validation` is a poll-time PREDICTION, not the verdict on the click.** Every entry of GET `/api/skills/-/pending` and the GET `/api/skills/-/pending/{slug}` detail MAY carry `script_validation: {"ok": bool, "report": {filename: [finding, ...]}}`, computed per candidate by `_pending_scripts_verdict` on every dashboard poll, `report` scrubbed and bounded by `_redact_validation_report` like the approve refusal's (same entry, finding and string caps, same `<truncated>` accounting — `_PENDING_SCRIPT_MAX_ENTRIES` is the ONE named budget the verdict walk and the report both spend, so the two cannot drift apart). It exists so the card can badge a candidate approve is going to refuse WITHOUT the user expanding the row. It fails closed on everything approve refuses: the top-level layout precheck (`_candidate_layout_findings_at`, reading the same `_ALLOWED_CANDIDATE_TOP` set as approve's `_candidate_layout_ok` — a symlinked or non-regular `SKILL.md` / `.meta.json`, a stray top-level entry, more than 16 top-level names) is reported under the `<candidate>` key; a `scripts` entry that is not a real directory, a symlink under it, a script over `MAX_SCRIPT_BYTES` (flagged from its size, its bytes never read), an unreadable or non-UTF-8 script, an entry swapped between stat and open, and an unexpected walk error (`verdict unavailable: unexpected walk error`) are all findings. Small decodable scripts get the real `validate_scripts` run twice — raw, then on in-memory redacted copies — mirroring approve's two-stage check; nothing on disk is touched. The candidate root is resolved exactly ONCE per verdict: a single `pinned_fs.open_dir_pinned` call pins it, the layout scan reads through that retained descriptor, and `scripts` is opened relative to it (`dir_fd`) with an inode identity check against the layout scan's stat — so a candidate root swapped between steps, whether for a symlink or for a different real directory renamed over it, cannot redirect any part of the verdict; everything below `scripts` is likewise stat-ed, opened and read descriptor-relative.

**When the field is OMITTED.** The field is ABSENT, not `ok: false`, whenever `_pending_scripts_verdict` returns `None` — the honest answer is "no verdict". Three paths do that: (1) a platform without descriptor-relative opens (`pinned_fs.supports_pinned_walk()` false), for EVERY candidate, before any by-name layout scan — a link/junction check followed by a by-name scan races with replacement of the candidate root, so even a layout REFUSAL can expose the target's filenames; (2) a platform with pinned opens but without descriptor-relative tree walks (`pinned_fs.supports_pinned_tree_walk()` false), for a candidate whose pinned layout precheck is clean and that has a real `scripts/` directory; (3) a walk-budget breach — more than `_PENDING_SCRIPT_MAX_ENTRIES` entries (files and directories together), a tree deeper than 8 levels, or more than `_PENDING_SCRIPT_MAX_ENTRIES` × `MAX_SCRIPT_BYTES` aggregate bytes — because the walk stops there while approve's `_collect_scripts` is unbudgeted, so `ok: false` would promise a refusal that never comes. **On Windows the pre-click "fails validation" badge disappears entirely, because no trustworthy verdict can be computed without descriptor-relative opens.** **One verdict, two surfaces.** `approve_pending_skill_checked` / `approve_pending_update_checked` CONSULT the same `_pending_scripts_verdict` the badge serves, immediately after their layout guard: a computable `ok: false` verdict refuses the click with `script_validation_failed` carrying that verdict's own scrubbed report, so the badge and the click cannot disagree on any candidate the badge judged — the badge's word is a refusal by construction, not by parallel re-derivation. When the verdict is `None` (either omission platform, or a budget breach), the click path's own machinery — the layout guard, `validate_scripts` on the collected files, redaction and re-validation — decides alone, exactly as before; it also re-runs in full after a passing consult, because the files on disk at click time are what go live. A client MUST treat a missing `script_validation` as unknown — render no badge, let the 422 `report` speak — and never as an all-clear.

**SEL outcome of a refused approval.** `api_skill_pending_approve` writes one `log_tool_invocation` (`agent="api"`, `source="dashboard"`, `tool_kind="skill"`) per request: `ok` with `{slug, name}` on promotion; `not_found` with `{slug, reason: "not_found"}` when the candidate does not exist; `rejected` with `{slug, reason}` for every other `PendingApprovalRefused` (`live_exists`, `kind_mismatch`, `script_validation_failed`, `target_missing`, `stale_base`, `invalid_layout`, `redaction_failed`, `promotion_failed`) and for a malformed slug (`reason: "invalid_slug"`); `error` for an unexpected exception. Previously every refusal was logged as `not_found`, so the audit trail could not tell a missing candidate from a rejected script. Refusals are audited at the handler only; the `*_checked` promotions emit their existing success-side audit.

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
- Remove unwanted auto skill: use the dashboard Skills delete action
- Audit trail: `kirocrew security events -n 20 | grep auto_skill`

## Hooks (`hooks.py`)

Config-driven from `config.json` → `hooks` section:
- **auto_approve_tools** / **auto_deny_tools** — tool patterns (exact, `prefix*`, `*suffix`, `*contains*`). An approve pattern is matched against the display title, except for an MCP-served call whose canonical identity is verified (`mcp_server_name`/`mcp_tool_name` from `_meta.kiro` AND the event's `mcp_identity_trusted` provenance flag, which every permission-path caller threads through — non-emptiness alone is not provenance): there it is matched against that identity as `Running: @server/tool` and `@server/tool` (`mcp_identity_ref`), in place of the title — never the lossy wire form `mcp__server__tool`, under which two identities whose server or tool name contains `__` collide — so a model-authored `description` in the title cannot approve a different tool than the one that executes. An identity that is present but unproven falls back to the title match. Deny patterns keep matching the title, the raw command and the wire `mcp__server__tool` name, and now also the `@server/tool` / `Running: @server/tool` spelling whenever the server name is present (a deny target can only deny), so both lists can be written in one spelling and deny still beats approve on the identity plane. **Migration note:** for an MCP-served call with a verified identity the approve pattern is no longer compared to the title, so an approve pattern written against a title that does not spell the identity (for example one keyed on a tool's `description` text) stops auto-approving and the call shows an approval card; rewrite it as `@server/tool` (or `Running: @server/tool`, kiro-cli's own title for MCP calls). Deny patterns keep matching the title, the raw command, and the canonical `mcp__server__tool` name together.
- **auto_replies** — pattern → direct reply (skip ACP entirely)
- **transforms** — pattern → prefix prepended to message
- **context_rules** — trigger keywords → context injected into message

Hook evaluation order: deny overrides approve; auto-reply → transform → context rules.

**Live reload (`HookManager.watch_config`).** The gateway's primary interactive
manager subscribes to the `hooks` section, so a `config.json` write re-parses the
flat hook keys onto the running manager. Subscription is opt-in rather than
automatic because a DERIVED manager must not follow config: the heartbeat-scoped
manager (`_build_heartbeat_hooks`) deliberately drops the user's
`auto_approve_tools` so `HEARTBEAT_SAFE_TOOLS` is the sole approval authority, and
re-reading the section would hand that widening straight back. It is re-derived
from the primary each cycle, so it inherits the reload without subscribing.

The deny ceiling and the flat hook keys live in different files — the
agent-unwritable `denied_commands.json` and the operator-editable `config.json` —
so whichever one changed, the other's contribution has to survive. Both reload
paths route through one function, `splice_denied_commands(base, denied_state)`,
which takes only `denied_commands_disabled_ids`, `denied_commands_disable_all` and
`denied_commands_user_added` from the keystone: a Settings → Security write splices
fresh keystone state onto the running config, and a `config.json` hooks reload
splices the CURRENT keystone state onto the freshly parsed flat keys. Without it,
one write silently reverts the other half.

Foreign-agent hooks are never imported. Hook scripts, hook commands, matchers,
and hook runtime state are unsupported items: scan/apply may report their
presence, but must not copy or register them.

Webhook `register_hook` captures the calling session's complete execution record
before writing its existing `hooks.json` entry. That entry owns the member/store,
template, app and privacy snapshot alongside its context summary; there is no
separate protected binding registry. Incognito and Temporary callers cannot
register persistent hooks: the tool refuses before writing the summary, lock or
session record. A persistent delivery captures the registration before queueing,
then passes that immutable context to its worker and prompt. Closing the parent
or editing the member's template does not reinterpret an existing registration.
Malformed identity refuses instead of falling back to Global. Existing ordinary
Global hooks retain their behavior, and webhook token, signature, owner/app and
governance checks remain independent of memory routing.

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

**A hook subprocess inherits only an allowlisted slice of the gateway
environment, not the whole of `os.environ`.** The gateway process holds
credentials (provider API keys, tokens) in its environment; copying that wholesale
into every hook command would hand an untrusted shell line those secrets. The
allowlist (`_HOOK_BASE_ENV_KEYS` in `hooks.py`) preserves only what a hook
legitimately needs — `PATH`/`PATHEXT`/`COMSPEC`/`SYSTEMROOT`, the home/profile and
`KIROCREW_HOME` data-home vars, temp-dir and locale vars, and TLS-trust
(`SSL_CERT_*`, `NO_PROXY`) — plus the two `KIROCREW_HOOK_*` metadata vars set last.
`HTTP(S)_PROXY` is deliberately dropped (it commonly embeds userinfo credentials).
The consequence for operators: a hook that relied on an ambient var outside that
set (e.g. `VIRTUAL_ENV`, `PYTHONPATH`, `JAVA_HOME`, `AWS_PROFILE`, nvm/pyenv vars)
runs fine in a terminal but fails once fired as a hook; the fix is to add that key
to `_HOOK_BASE_ENV_KEYS` by name — the allowlist is fail-closed by design.

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
passing through where that is what the host resolves to. On Windows an
undeclared key resolves to allow, so a script hook runs unconfined by default
(as script crons and Papyrus do); where the operator declared
`agent.sandbox_allow_unsandboxed_exec=false`, or a governance
`sandbox.min_level` floor is pinned, the hook's `SandboxUnavailableError`
surfaces as the result's `error`, naming the setting.

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

### Record editing and revisions (V1 and V2)

Global V1 retains its Key/Value/Set form in the shared editor, using the existing
unscoped semantic-write API. The form never renders for a named or member store.
Its pending request disables duplicate submission, failures keep both fields for
retry, and its draft participates in the memory-page navigation guard. Successful
writes refresh the paged record list. Existing-record corrections in either
lineage compare canonical JSON, so boolean/number changes (including nested
values) produce a real preview and revision; object key order remains a no-op.

The global and member editors share `MemoryRecordsEditor`. In the private V2
view it is the primary Memories tab. In Global V1 it is a management disclosure
that mounts only after the user opens it, so an ordinary non-owner visit does
not issue the owner-only records request. The established V1 preference,
project, daily-history, settings, lessons, semantic and episodic browsers remain
visible in the page flow; the bulk editor does not replace or collapse them.
Their paged list
uses `GET /api/memory/records` with `store`, `q`, `kind`, `offset` and
`limit` (1–100). Filtering precedes pagination. The authentication middleware's
`token` query parameter is permitted but never used as a filter or returned in
records. Unknown query fields are refused instead of silently broadening the
selection. Record addresses are readable
text; users enter an address in the ordinary search field. This uses the same
visible query and selection state as other searches. Search
matches normalized query terms against keys, decoded values, tags and fact
classification. Every record includes a content/revision fingerprint and
record metadata. `POST /api/memory/records/refresh` resolves up to 500 exact
identities after a concurrent change without discarding the user's draft. Its
alternative `selection: {query, exclude}` body returns an exact `matched_count`
under the same normalized selector and selection caps as preview; deleted or
nonmatching exclusions do not subtract from the count. The editor refreshes
all-query totals after membership changes and permits a missing record to be
refreshed again after restoration while retaining its draft.

Record metadata retains extracted `email_addresses` for the saved-record editor.
Fresh extension tables omit the redundant `has_email` flag and its category/email
index. Opening an older extension schema removes that unused index while preserving
its extra physical column and all existing revision JSON. Current readers and writers
ignore the old flag. Ordinary record search still matches stored content and supported
metadata. This cleanup does not rebuild memory tables or require a newer SQLite version.

`GET /api/memory/records/history` pages immutable revisions for one exact
identity; the detail view can load older pages and retry a failed page without
discarding already loaded history. V1 automatically keeps the latest 20 accepted
snapshots per record; V2 accepted snapshots and all proposal statuses in either
version have no automatic retention limit. Forgetting a record appends an
accepted deletion snapshot under the same retention policy. Access timestamps and embedding-only
updates are excluded from this history. There is no revision-history purge UI.
A conflict is pending only while its base revision equals the current
revision; accepted correction advances the record while preserving the old
proposal as history. The detail view seeds an editing draft for value proposals
and the existing forget review for deletion proposals. Both require a fresh
preview against the current record identity and revision before applying.
Deletion proposals and accepted deletion history display the removal marker,
including revisions with no after snapshot. Keeping the current
value also has an explicit preview/apply path: it advances the revision and
journals `resolve` without changing content, provenance or vectors. Single-set
previews bind pending proposal IDs; a newly arriving proposal forces a fresh
review instead of being silently dismissed.

Owner-only `POST /api/memory/bulk/preview` accepts one store and either explicit
record identities with revisions (up to 500), or an all-matching filter with
exclusions. Operations are literal text replacement, single-record correction,
and forgetting. Replacement walks JSON string values; it never rewrites object
keys, repository scope, provenance or classification. Preview validates the
entire selection, returns counts and the first 25 changed before/after pairs,
and signs the selector, operation, store and full selection digest. A selection
is capped at 10,000 records and 32 MiB; larger sets require a narrower filter.

`POST /api/memory/bulk/apply` verifies that 15-minute preview and rechecks both
content and membership inside `BEGIN IMMEDIATE`. Any changed, missing or newly
matching record rejects the complete batch with `409 stale_memory_preview`.
Accepted writes, revisions, audit events and an idempotent receipt commit in
one transaction. Retrying the same token returns the original result without
repeating replacement. Expired receipts are pruned; gateway restart invalidates
pending previews. Content edits clear stale vectors and use normal backfill,
so saving an edit does not start native inference.

Episodic content edits also drop derived in-memory FAISS/scoring caches after
commit. Saved FAISS artifacts are verified against current SQLite vectors and
both saved index-file digests recorded in `memory_meta`; restart and external
connection changes cannot pair edited text with an old vector. Saving rebuilds
the derived index under an immediate SQLite transaction. This adds no sidecar
service and no fallible disk write after a successful owner edit commit.

The two additive tables `memory_record_meta` and `memory_revisions` are local
to every store. Existing V1 tables and V2 compatibility views remain intact;
no data crosses stores and no automatic V1-to-V2 migration occurs. Stable
record identity, explicit subject/predicate/scope, category, validity interval,
source reference and revision distinguish a fact's identity from its wording.
Revisions retain before/after evidence and conflict proposals. V1 keeps the
latest 20 accepted snapshots per record; V2 keeps all accepted snapshots. Neither
version automatically removes proposals. Metadata helpers never commit or
embed; each writer owns the transaction with its content.

Research informing these choices: [LongMemEval](https://arxiv.org/html/2410.10813v2)
studies granularity, fact-expanded retrieval keys and temporal filtering;
its results also caution that compressing all evidence into facts loses useful
context, so episodes remain available. [LangChain's memory guide](https://docs.langchain.com/oss/python/concepts/memory)
describes the collection/update tradeoff. [Mem0](https://arxiv.org/html/2504.19413v1)
separates extraction from memory updates. These sources inform design choices;
their reported benchmark gains are not Kiro Crew measurements.

## Context Builder (`context.py`)

Assembles all sources into prompts:
- New session: `_CRITICAL_RULES` (runtime-conditional diff blocks + OPTIONS buttons) + agent prompt + static preference/project anchors + memory tool guidance + skills + scoped lessons + conversation history (last 20 messages, thread history at TOP with explicit framing)
- Every message: channel history, hook transforms, triggered skills, context rules, OPTIONS hint (interactive sessions only). Memory search is an explicit MCP operation; building a message never generates a query embedding.
- Runtime identity is turn-aware rather than key-only. Channel and dashboard dispatchers pass trusted `runtime_source` metadata to `build_message()`. New sessions use it for `[RUNTIME]`; follow-up turns refresh `[RUNTIME]` outside the one-time session context. This is required because a stable `dashboard:*` session can be resumed from Discord and `messaging.dm_scope="unified"` intentionally removes the originating channel from the session key. When trusted metadata is absent, namespaced keys (`discord:*`, `telegram:*`, `wecom:*`, `weixin:*`, `webex:*`, `teams:*`, `slack:*`) are recognized directly; bare unknown keys keep the legacy Slack fallback.
- Thread history is injected only at session start (via `build_session_context`). Within the same ACP session, kiro-cli manages conversation history natively — duplicate injection wastes context window and accelerates compaction.
- `_CRITICAL_RULES` injected by DEFAULT for every agent (built-in `kirocrew` and custom alike) — it is the dashboard/Slack assistant's own output contract (runtime-conditional diff blocks — tool-made edits render as structured diff cards on the dashboard, so ```diff blocks are required only for non-tool edits or non-dashboard runtimes — `[OPTIONS:]` footer, absolute-path rule with a URL exclusion — a backticked URL renders as a click-to-copy chip rather than a link, so URLs must use markdown link syntax instead), so diff rendering and OPTIONS buttons work universally. A **custom** agent can OPT OUT by setting `includeCrewContext: false` in its materialized `~/.kiro/agents/<...>.json`: a custom app agent ships its own system prompt and output contract, so injecting this on top both conflicts with it and, on a safety-tuned model, reads as an identity override the model refuses as prompt injection. The flag is read through the same sensitive-path-gated scan as the agent prompt (matched by declared `name` or filename stem) and memoized by agent name; an absent/non-boolean flag, an unreadable/missing spec, and the built-in `kirocrew` agent all default to injecting (only an explicit boolean `false` on a custom agent suppresses it). The same opt-out also suppresses the dashboard tool nudges (`ask_question` / `suggest_followup`) that `build_message` adds on dashboard sessions, but NOT the provider-agnostic `[OPTIONS:]` reminder. The `[OPTIONS:]`/diff tags still RENDER for any agent that emits them (the dashboard parses them regardless); the gate only stops the host from MANDATING them where an agent has declared it does not want them.
- Switchable context groups (see below) let a spawning parent drop whole sections for one sub-agent.
- Cap: `_CONTEXT_BUDGET_BASE` is a fixed 33,000-character Crew background admission allowance, reused from the former smallest-window tier. Model window size and `skills.lazy_load` cannot enlarge it. Admission reserves complete explicit preferences, rules, pinned instructions, date/runtime identity and steering, then admits optional source blocks whole. A separate model-safe ceiling bounds the protected lesson contribution at `max(3 * _CONTEXT_BUDGET_BASE, floor(model_window_tokens * 4.0 * 0.125))` characters for the complete protected set. Below it, protected bytes are unchanged. Above it, admission falls back to the ordinary lessons budget (`caps.lessons`, still bounded by the ceiling) and complete lessons are omitted in relevance order for vectors or newest-first for JSONL; preferences and safety rules remain whole, the prompt reports the omitted count, and a warning is emitted. Preferences are never trimmed to make room for anything else, but they cannot cross the ceiling themselves: an agent-grown preferences file larger than the ceiling is kept from its head, and the prompt carries a `[Context budget: omitted N chars of preferences ...]` notice naming the file to read, so the overflow is visible in-prompt rather than only in a log line. The 33,000-character allowance remains independent and is not falsely reported as a full-input ceiling. Agent contract, outer replay, following-interaction blocks and the current request are measured separately. The request is never budget-truncated.

Startup V1 context retains complete preference documents and eligible `pref.*`
records. A required activity index (at most 1,800 characters) lists project
headings/first entries and the last three days' headings or first lines. Each
source has a share, so project overflow cannot hide recent task names. The
existing bounded `Recent Session Context` source snippets remain injected:
those snippets need not exist in vector memory. Index and recalled content are
reference data, not instructions. Larger notebook bodies and non-preference
facts/episodes require explicit `memory_recall`.

Recall uses the authenticated session's bound store and workspace, never a
request-supplied path or another active slot. The V1 notebook query reuses
`memory_v2.terms` for CJK pairs and identifiers, strips question filler, and
prefers lines matching at least half the remaining topic terms; when no line
reaches that bar it admits lines sharing at least two distinct terms (one when
the query has a single term), ranked by matches, so an older notebook line that
the old first turn showed unconditionally stays reachable from a natural
question. A single shared word never admits a line on a multi-term query.
English terms use quoted
FTS matches; Chinese pairs match original index content without rebuilding it.
At most five snippets are selected, each at most 1,000 characters. Ordinary
`MemoryStore.search` retains literal AND semantics; explicit `match_any=True`
uses topic coverage. Because recall is now the only road to the notebook, an empty or unreadable
index is treated as a fault to repair, not a degraded search: the recall handler
rebuilds the V1 FTS index once from the files it mirrors (preferences, projects,
history) and retries the query, reporting `markdown_status_repair: rebuilt`. Only
when the rebuild yields no rows or the query still fails does it report
`index_unavailable`, never a claim that no memory exists. V2 does not use the
Markdown fallback. Both recall paths preserve truncated, cited evidence when
a record exceeds its share. Repeated lessons do not reserve recall capacity.
Merged V1 facts and episodes share query-coverage ranking, retaining a floor
for already-admitted semantic evidence; lower-ranked tails are omitted first.
The complete response remains bounded to 3,000 context characters and 16,384
transport bytes. This is lexical recovery, not translation or a guarantee that
the model will call the tool. Notebook/history writes maintain the index;
out-of-band edits still need the existing rebuild. No store binding, privacy
mode, or private essential-delivery gate changes.

Startup lessons retain every eligible, in-scope rule completely while protected context is below the model-safe ceiling, without a background embedding call. Neither vector `source=consolidation`/`promotion` nor the tool/knowledge category proves optionality: consolidation extracts explicit always/never user corrections through the same path. JSONL likewise has no reliable explicit/inferred provenance. When the ceiling forces omission, only complete lesson entries are removed and the prompt directs explicit recovery through `memory_recall`; wording, lexical mismatch and PR numbers never justify dropping a rule below that ceiling. Above the ceiling, where the retain-everything invariant cannot be met because the full set already exceeds it, admission falls back to the ordinary lessons budget (`caps.lessons`, still bounded by the ceiling) rather than filling to the ceiling with ranked rules; the surviving subset is the one relevant to the request and the omission notice still directs the model to `memory_recall`. This overflow choice is not a statement about what to keep below the ceiling, where the block stays complete.

Explicit recall deduplicates only identical selected evidence with the same stable record ID, independently within facts and episodes. Different IDs, revisions or provenance remain separate. The projection does not mutate input or storage.

#### Per-section admission

Section constants bound optional activity and discovery; they do not add to
the 33,000-character discretionary allowance. Complete preferences, applicable
lessons, steering, memory navigation, and skill discovery are protected. Omission
notices are outside that allowance and name omitted sources. Thread continuity
has an independent window-scaled allowance: 6,930/34,650 characters at 200K/1M,
with per-message caps of 1,600/8,000 and compression thresholds of 8,910/44,550.
Long history blocks keep framing and the newest tail rather than disappearing.

Confined project bodies, pinned or not, retain a separate 24,750-character
allowance and descriptor-pinned byte-limited reads. First-turn and post-compaction
skill injection both split protected bodies from discovery. The default discovery
entry lists up to eight usage-ranked names and short purposes and requests short
keywords. Scoped search filters `repo_scope` and byte-identical duplicates just as
the catalog does, and returns confined project bodies through the same reader,
never an unconfined live path. UI language, date/runtime identity, withholding, member mode and stop notes
remain mandatory. Protected lesson overflow is counted and reported; other protected
content remains whole. Outer replay retains its separate allowance. Counts are
characters/UTF-8 bytes, not model token or cost estimates.

Beyond Kiro Crew's own assembly, kiro-cli manages its own context window:
`_kiro.dev/compaction/status` notifications signal that it summarized older turns,
and Kiro Crew resets its context-usage accounting at that chokepoint. Separately,
`SessionManager` trips a circuit breaker after `_CIRCUIT_BREAKER_THRESHOLD` = 5
consecutive turn FAILURES for a session key and resets the session; that counter
tracks failures, not compactions.

On the dashboard, confirmed provider-native and manual `/compact` completion
arms `SessionManager.mark_needs_reinjection` for the effective session key.
The next dashboard turn consumes that one-shot flag to restore the skills
context. Failed deferred compaction does not arm it. This completion hook does
not add skills reinjection to messaging surfaces or the task runner.

#### Model-window metadata

`_resolve_caps(model_window)` returns fixed Crew activity/discovery limits and
independent, window-scaled thread history/message/compression limits.
`resolve_model_window` and `window_for_provider_client` resolve provider metadata
for those thread limits and replay; these values do not grant more old-activity
capacity. The native model input, provider-owned resources and external MCP
serialization are outside this boundary and remain UNKNOWN, not estimated from
the Crew string.

The full agent contract is still injected through the existing path. Native
prompt/resource configuration alone is not proof that the native provider received
the same effective substituted content. Until that equivalence can be established,
no duplicate contract is removed. Tool Search thresholds and README loading are
unchanged; neither has controlled evidence supporting a change here.

### Switchable context groups (sub-agents)

A spawning parent decides which of three groups its sub-agent inherits, via `include_memory` / `include_lessons` / `include_project` on `spawn_run` and `spawn_sub_agents`. All default to `true`, so a caller that passes nothing produces byte-identical context: `build_session_context(context_groups=None)` — what every non-sub-agent caller passes — and an all-on `frozenset` are equivalent by construction.

| Group | Sections | Switchable |
|---|---|---|
| conduct | `_CRITICAL_RULES`, date, agent/runtime, UI language, workspace identity, bounded skill discovery | no |
| `memory` | complete preferences, activity index, memory tool guidance, `Recent Session Context` source snippets; V2 essential anchors | yes |
| `lessons` | `[Learned corrections]` (global + workspace), `[USER PROFILE]` | yes |
| `project` | `[DOCUMENTATION]` pointer, steering resources (CC backend only), `[PROJECT]` directory line | yes |

The steering row carries a backend caveat: the steering block is injected only on the Claude Code backend (`is_cc`), because on the ACP/kiro backend `kiro-cli --agent` loads the agent's own `resources` natively. `include_project=false` therefore suppresses steering on CC only — an ACP sub-agent still receives it, and nothing in Kiro Crew can prevent that from this call site.

conduct is not switchable because it supplies the output contract and capability
entry points. Default skill discovery is a small name/purpose list plus
`skill_search`, not the full installed directory. V1 project notebook bodies are
not a conduct block: their navigation belongs to `memory` and their bodies are
recalled on demand.

Omitting a group **skips its sections** rather than capping them to zero — `MemoryStore.get_context()`'s `_cap(text, 0)` returns a `…[truncated]` marker, not an empty string, so a zero cap emits headers with no content behind them.

A sub-agent that had a group withheld is told so by name (`[CONTEXT SCOPE]`, built by `_build_context_scope_section`), so it reports the gap instead of inventing what it cannot see. That is what makes an aggressive opt-out recoverable: a wrong `false` surfaces as a question rather than a fabrication.

The flags resolve once at spawn and live on `SubagentInfo`. Every path that re-materializes a run from stored fields carries them — the stagger queue entry and `POST /api/spawn/{agent_id}/retry` — so a queued or retried run sees the scope its caller chose. `spawn_continue` does not accept the flags but **inherits** them (`_inherited_context_groups`): a continuation rebuilds session context, because `get_or_create` returns `is_new=True` even when it restores the session via `session/load` (`resumed` is a separate flag and gates only thread history), so an un-inherited continuation would silently regain a withheld group. The live record wins; the run's persisted `context_groups` is the fallback, and a run predating the field records no scope at all — distinguishable from "all withheld" and defaulting to all-on. `GET /api/spawn` reports `context_withheld` only when something was withheld, and `_run_inner` logs the resolved set with the resulting context length.

### Session Resume (`resumed=True`)

`build_message(resumed=True)` uses slim resume after native `session/load`.
It does not reinject the original full memory, lessons, skills or agent prompt.
A direct `build_session_context(resumed=True)` call only skips thread history;
it is not the public turn's slim-resume path.

| Block | Slim resume |
|---|---|
| Thread history | Native provider history retained; no duplicate block |
| Memory, lessons, skills, agent prompt | No full reinjection |
| Date, runtime, UI language | Refreshed minimal header |
| Member essentials/rules | Existing lifecycle delivery retained |
| Critical rules | Existing restored context; no full duplicate |
| Cross-tab history | Not injected |

Post-compaction reinjection separately refreshes bounded skill discovery,
protected skill bodies, memory navigation, reply preferences and member identity.

The owner copy dialog names its destination member. Recovery distinguishes
**Restore experience** from whole-store **Restore backup**. A dirty store switch
offers **Keep editing** and **Discard changes**; keeping the draft leaves the
original member and document mounted. Old initialization-error metadata is
displayed as historical diagnostic text and does not suppress retry of a valid
V1 conversation.

### Member workflow execution

Dynamic workflows persist their canonical execution context in the run record.
Worker creation and reuse, restart and subtree replay retain that identity.
Provider templates select task roles, not memory owners. Ordinary authenticated
MCP calls use the worker session record; no memory-specific process proof or
hidden run-payload directory is involved. See [workflows](workflows.md) for
workflow ownership and execution permissions.
