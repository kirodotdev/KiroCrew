# Memory, Skills & Hooks Modules

## Overview

Persistent memory, skill system, and config-driven hooks. Assembled by
`ContextBuilder` and injected into ACP prompts.

Private V2 execution and consolidation also require the enforced Crew OS
filesystem sandbox: Linux/WSL namespaces or macOS outer Seatbelt, with the
spawn layer's resolved mode and the member DM's selected backend. The persisted
enablement is `agent.sandbox=auto`. Off/unconfined execution, unavailable
isolation and Kiro internal delegation refuse with a specific reason; native
Windows member execution directs the user to the WSL/Linux gateway. Owner
dashboard memory management stays available. Global V1 retains its own data,
original memory tables and existing prompt retrieval. Optional tool-driven recall,
revision metadata and the record editor apply to both versions without
converting V1 into a member store or replacing its eager session recall.

Opening an existing V1 database creates shared record-metadata and revision
tables and reconciles record identities even before any V2 opt-in. This changes
the SQLite file; preserving V1 means retaining its original memory rows, lineage,
algorithms and context behavior, not byte-for-byte file identity. V1 retains
only the latest 20 accepted revision snapshots per record, including catch-up
when an existing journal opens. Every proposal and current record remain intact.
V2 revision history has no automatic purge.

Private provider startup requires `ACP_BACKENDS_PRIVATE_MEMORY_MCP` membership.
Kiro, Claude Code and KAS qualify; unknown or merely selectable backends do not.
This direct-tool capability and the OS sandbox checks are both required.

Private internal HTTP recall, lesson operations and explicit consolidation use
the protected process/session binding under `member-memory-bindings/pids/`, or
a fresh delegated MCP proof validated against that binding. A shared local
secret and caller-provided session header alone cannot select a member's store.
`/api/memory/recall` is a mixed internal/browser route: MCP's internal secret
reaches the same protected-session handler, while dashboard requests use their
normal cookie. Adding the transport route does not grant store authority.
Unverifiable calls return `403 member_session_unverified`; they never access
global memory instead. See [security](security.md#overview) for the filesystem
and caller-proof boundary.

The hidden proof-signing key is staged as 32 owner-only bytes and fsynced before
atomic publication without replacement. Concurrent creators adopt the first
valid key. A crash before publication leaves the final name absent; corrupt
committed key material refuses proof issuance and is never rotated automatically.
Atomic publication requires hard-link support in the data-home filesystem. A
filesystem without that capability refuses key creation without publishing a
partial key; ordinary POSIX filesystems and NTFS support this operation.

Channel-visible member-memory refusals remove local paths, credentials and
exfiltration URLs before truncation. The shared messaging dispatcher and the
Slack, Discord and Telegram dispatchers use the same redaction layers while
retaining the actionable refusal. They do not start a Global provider on failure.
Existing members keep their exact declared Global or named Memory V1 binding,
including a member selected by `default_agent`. The UI labels that memory V1.
The owner can choose Create private memory in Crew Manager, the member's
Workspace · Memory pane. That explicit choice creates an empty V2 store and
preserves the V1 source; Copy memories transfers only selected records.
New members receive V2 automatically. An existing V2 member never becomes V1
because its configuration, manifest, database or protected session record is
missing or damaged. Such failures require recovery and cannot initialize a
replacement or silently change the conversation's authority.

Dashboard creation, discovery sync and explicit V1-to-V2 setup, plus CLI creation
and `--provision-memory`, check the member DM's effective backend and OS sandbox
before allocating private files or publishing its binding. Unsupported execution
returns an actionable refusal (`409 member_memory_unavailable` on HTTP, exit 1
on CLI); it cannot create a new V1 member instead. The check also precedes
retirement of existing V1 providers. Ordinary V1 edits and management of an
already-owned V2 store remain available. Runtime admission repeats the check,
since configuration and OS capabilities can change after creation; Crew tasks
and consolidation separately require their configured default backend to support V2.

`memory.private_provisioning_enabled` defaults to true and controls admission
of new private allocations through the same creation guard. During a provisioning
or storage incident, setting it to false stops new private stores
and one-way V1 opt-ins while keeping established members available. Deleting a
member archives that member; it neither pauses discovery of other members nor
blocks another creation request, so deletion cannot serve this operator policy.
The owner sets this central policy through the existing typed
`PATCH /api/config/kirocrew` or configuration
file. Dashboard create, discovery sync with new members, CLI create and explicit
V1-to-V2 setup then refuse before allocation and before V1 provider retirement;
they never substitute a new V1 binding. The next admission reads the current
configuration, without requiring a gateway restart. A present non-boolean field
loads as false, while an absent field retains the true default; API writes require
an actual JSON boolean. Existing V2 execution, management, backup and recovery
retain their normal ownership and isolation gates, and idempotent setup of an
already-owned V2 store remains available. Existing V1 bindings remain usable.
This operator control neither cancels an already-admitted operation nor disables
memory preparation or withdraws the shared startup and dispatch changes.
The creation guard also refuses the loader's degraded `memory` or whole-file
marker, so unreadable settings cannot authorize creation through default values.
Advisory schema validation preserves a malformed `memory` section for the loader
to record; the same refusal applies with or without the optional validator.

If private allocation completes but member configuration publication fails, the
creator checks that exact new generation under the config sidecar lock. Only an
undeclared store with no agent reference is retired; its files remain preserved.
A completed or competing publication keeps the store active. Dashboard workers
finish before cancellation cleanup inspects the result, and an idempotent request
for an existing V2 store never offers it for this cleanup. Unreadable configuration
preserves the allocation and reports the original failure; cleanup cannot guess
that the current configuration leaves the store unreferenced. A retired failed allocation does not block a new
owner retry from the unchanged V1 binding.

The proof gate also applies to attempts to address Global V1 from a private
process, including headerless requests. Private authority comes from the
process-protected store, not a caller-selected session or editable transcript.
Trusted member assignment pins an immutable session/store record under the
read-only binding root before preparation. Dashboard member selection, protected
subagent dispatch and the scheduler publish these assignments. An unsigned
channel transcript cannot mint one; metadata downgrade or reassignment fails
after restart.
Session-control creation resolves the effective agent's memory and workspace
binding off the event loop. Resolution failures return `agent_unverifiable`
before allocation; the final live-caller authorization still follows all awaited
preparation.
Async turn admission, vector-store preparation and member consolidation perform
protected binding reads, store validation, initial SQLite/FAISS construction and
profile reads in worker threads. Store cache generation and retirement checks
still surround construction; no cache lock is held across an await. Global
consolidation uses the same off-loop profile-read scheduling with unchanged
content and write policy.
Private consolidation allocates an ephemeral member-bound provider session,
records its own billing and removes that provider after release. It never uses
the shared V1 background provider. The default background path is unchanged.

Memory content endpoints honor the selected store for import, context preview
and observability. Legacy Markdown migration is Global V1 only and refuses a
named store; private members copy selected starting knowledge through the owner
workflow. Automatic episode promotion refuses V2, where changes and forgetting
remain explicit. Embedding configuration continues to describe the installation.

Global audit diagnostics show failed fetches through `ErrorNotice` with retry.
A failed next page keeps the events already loaded. An empty-history message
appears only after a successful fetch.
Global episodic delete failures retain the row and submitted search, show
`ErrorNotice` beside the failed action, and offer retry for that same record.
Semantic write errors and rejected embedding setup restarts also use
`ErrorNotice`; unsaved drafts stay in place and these notices do not navigate to
an agent. A rejected restart stops its pending indicator and keeps Retry available.

Owner controls use Fact, Rule and Experience consistently. Starting-knowledge
copy explains that the source stays unchanged before the user selects it. A
rejected copy always shows a failure notice, including responses without a reason.
Bulk controls explain replace and forget before selection and require a preview.
The empty list has no page-selection checkbox. Recovery explains preserved copies
before restore; its manual backup summary distinguishes empty stores from failures.
Advanced memory groups have translated labels while their stored identifiers stay
unchanged. Global V1 keeps its ordinary browsers visible; its record editor opens
from Manage memory.
The store picker has no generic creation modal; members obtain their stores
through member creation or the explicit legacy initialization action. A Global
store without source groups shows one muted capability explanation. Actual
analysis failures remain errors. Pending proposals display their own recorded
source and provenance, with equal visual weight for accepting or keeping the
current value.

### Emergency withdrawal of private execution

The allocation pause above is a live admission control, not an execution stop.
The following is a draft maintenance patch for withdrawing private execution
while retaining V1 and the shared repairs. It is not applied to the normal build
and has no deployment or CI validation claim. A maintainer must adapt and validate
it against the release being withdrawn before deploying it.

Replace only `_private_memory_mcp_failure` in `member_memory_auth.py` with:

```python
def _private_memory_mcp_failure(backend: str) -> str:
    """Maintenance withdrawal: admit no new private ACP execution."""
    return (
        "Private member execution and new private memory creation are paused "
        "by this maintenance build. Keep the existing memory assignments. "
        "Install a repaired build and restart the gateway to resume private members."
    )
```

The block above is illustrative. No test compares it against the shipped
`_private_memory_mcp_failure`, so a change to either side is not caught
automatically; a maintainer applying it must re-read the current helper and
validate the replacement on a populated temporary home (Global and named V1,
member creation and execution refusals, preserved private files and bindings,
and admission after restoring the normal helper) before deploying it. That
validation does not establish that a real installation's process fleet was
stopped or a maintenance release was deployed; those operator checks remain
required.

The private creation guard, context preparation and member consolidation consult
this helper through the private execution check. Direct `AcpClient`, `AcpRuntime`
and `AcpProvider.prepare_private_memory` also check it before publishing a private
provider. V1 skips this private-only admission. The refusal retains the private
assignment and cannot substitute Global or a colleague's store.

Deployment requires a stopped installation:

1. Set `memory.private_provisioning_enabled=false`, then stop every gateway,
   provider, CLI writer and scheduled/task process using the home. Disable service
   auto-restart. Check every host sharing the data; a disconnected dashboard does
   not prove that child processes stopped. Stop if ownership is ambiguous.
2. Preserve and verify an owner-restricted offline archive of the complete home
   and all external memory/profile roots, including config, manifests, protected
   identities, databases and SQLite sidecars, journals/staged restores, backups
   and diagnostic keys/logs. Resolve or deliberately defer pending restores;
   the maintenance patch retains startup recovery.
3. Validate the maintenance build in CI and deploy that exact build to every
   installation using the home. From a stopped state, verify ordinary V1 remains
   usable and V2 creation, direct chat, Crew/child work, schedules and resume
   refuse before provider startup. These are required checks, not reported results.
4. Keep all member/store assignments and private directories intact. To resume,
   stop the installation again, install the reviewed repaired build and restart
   with those assignments. Re-enable allocation only when intended.

This patch cannot revoke an already-running provider in an old process. Trusted
raw storage scripts must not bypass product admission. Owner management,
recovery, backups and storage repair remain available, so private storage is not
read-only. No private data is deleted or converted by the patch. V1 auxiliary
tables and shared initialization remain in place; this is not a schema rollback
or a remedy for a defect in shared initialization. Never run a pre-V2 binary over
the home or repoint a private member to V1. A complete feature revert loses both
the private identity interpretation and shared repairs; the stopped maintenance
build preserves them.

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

Six distinct storage layers, each with its own store and write path. A fresh V1
session reads preferences, projects, decayed daily history, semantic memory and
query-ranked episodic memory and lessons. Warm V1 follow-ups retain native
conversation history without repeating this injection. V2 session context
reads essential anchors and query-free scoped lessons; its semantic and
episodic fragments require an explicit `memory_recall` operation. The
nesting below is source-of-truth ordering (a later layer can override an earlier
one), not a storage hierarchy:

```
Context window (reference budget 165,000 chars, ~55k tokens)

  Preferences            Projects            Recent history
  (preferences.md)       (projects.md)       (history/{date}.md)
  V1: consolidator-      V1: consolidator-   V1: multi-tier decay
      replaced              replaced        V2: full retention
  V2: owner-managed     V2: owner-managed
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

Layers 1 to 3 are Markdown files under a memory store's markdown root; layers 4
to 6 are rows in that store's `memory.db` behind a `VectorMemoryStore` (lessons
fall back to `lessons.jsonl` only when that store is absent or holds no lessons
yet). There is ONE such store on the default path — the global one every
install already runs — plus a private named store for each Crew Member. Which
surfaces reach which is in
[Memory across surfaces and channels](#memory-across-surfaces-and-channels), and
the distinction is whether the caller carries a crew or session store binding. Each layer
is detailed in its own section below, with a single conflict ladder in "Conflict
resolution: which layer wins".

## Memory (`memory.py`)

Structured files under `~/.kiro/crew/workspace/memory/`:
- `preferences.md` — learned user preferences (V1 legacy consolidation may replace the file; V2 is owner-managed)
- `projects.md` — active project context (V1 legacy consolidation may replace the file; V2 is owner-managed)
- `history/{date}.md` — daily conversation summaries (append-only; heartbeat age pruning applies only to V1)

### A store's three paths, and where the index actually lives

A memory store's markdown tree, vector file and FTS index are three separate on-disk
paths, and which one a store name resolves to is owned by `memory_stores.py` — see
[config](config.md#named-memory-stores-memory_storespy) for the resolvers, the
store-name shape rule and strict private ownership validation.

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
it behind the `memory_stores/` fence; those three consumers name no per-store path,
so a named store's index is simply outside their reach until one of them learns a
store name.

**A named store starts EMPTY.** Nothing is copied from the default store and nothing
is inferred from it: no preferences, no projects, no history, no semantic or episodic
rows, no lessons. A crew bound to a fresh store therefore knows nothing on its first
turn, which is the point — the isolation is the file boundary, so there is no
migration, no cutover and no `CONTRACT_VERSION` bump.

`MemoryStore` takes the index path as `index_db=` instead of deriving it, so store
policy stays in one place. Omitting it keeps the pre-existing derivation and its
quirk: a bare `MemoryStore()` names `<home>/memory_index.db` while
`MemoryStore(workspace=workspace_dir())` names `<home>/workspace/memory_index.db`,
though both share one markdown tree — and both forms are live (`cli.py`, `context.py`).
That costs a duplicated rebuild, not a wrong answer, because the index is fully
DERIVED: `rebuild_index` regenerates it from `preferences.md`, `projects.md` and
`history/*.md` and reads no index state. Only the root copy is in the snapshot, so
closing the quirk means giving EVERY caller a store name. A named store already gets
one — `get_memory_for` passes `index_db=memory_index_path_for(store)` explicitly — but
the two default construction forms are deliberately untouched, so the quirk is still
open on the default path and
`TestIndexIsPerStore::test_the_two_default_construction_forms_still_disagree` still
pins it.

FTS5 search via that `memory_index.db` (SQLite via `pysqlite3-binary` on Linux for FTS5/UPSERT compat, stdlib `sqlite3` on macOS). The virtual table is created with `tokenize='porter unicode61'`, so keyword matching is porter-stemmed inside SQLite. (This is a different stemmer from the `snowballstemmer` pass used by the vector store's keyword-fallback *scoring* in `vector_memory.py`; two independent code paths, do not conflate them.) Self-healing: corrupted DB auto-rebuilt. Incremental updates on writes, full rebuild on gateway startup and every `_FTS_REBUILD_TICKS = 15` heartbeat ticks (~15 min at the 60s default interval). Connection leak prevention: all FTS methods use try/finally.

Context injection includes source citations per section. Agent can update memory files via kiro-cli's file tools.

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

V2 history reads retain full daily content without age tiers or a date cutoff.
The explicit reader uses the existing bounded snapshot (366 entries / 8 MiB),
so response size is limited without deleting, summarizing or de-indexing older
files. Ordinary V2 message context does not read these history files. Named-store
constructors pass the resolved `memory_version`; attaching a prepared V2 vector
tier also enables retention and invalidates any previously decayed cache.
One bounded snapshot validates its private store once and reuses that result
only within the call. Every file open still checks containment, links, the
opened inode and size limits. Dashboard profile and history reads run off the
event loop.

`MemoryStore.get_context()` retains `history_cap=25_000` as its default for
programmatic readers. V1 `ContextBuilder` calls it with the scaled history cap
when building fresh session context. V2 reads preference/project anchors without
this history scan. Timestamps use local timezone.

V1 session context and explicit readers invoke `read_recent_history`; V2 prompt
construction does not. A V1 read stats and reads up to 181 daily files synchronously;
V2 uses the bounded full-entry snapshot described above. The assembled string is
TTL-cached (`_HISTORY_CACHE_TTL_SECS = 5.0`) on the `MemoryStore` instance,
keyed on `(days, today)` so the decay window shifting at midnight invalidates
naturally; `append_history` and `prune_history` call `_invalidate_history_cache()`
so a new or pruned entry is visible immediately.

### History Pruning

For V1, `prune_history(keep_days)` deletes daily files older than `keep_days` (default 365). It runs once per day via heartbeat (`_PRUNE_TICKS = 1440`), parses `YYYY-MM-DD.md` filenames and skips non-date files. For V2 it returns zero without deleting anything, regardless of the age setting.

V2 preference/project reads and FTS rebuilds share the guarded private reader.
An existing refused source raises a reason instead of being treated as empty;
owner preference/project routes expose that failure as HTTP 503 with a named
store and reason, while removing host paths and credentials from the response. A rebuild
considers every valid dated history file, including files older than the
snapshot's 366-file window, and streams bounded file bodies into one SQLite
transaction. If any source or database operation fails, rollback preserves the
previous visible index. This bounds body buffering without imposing a storage
age limit. V1 retains its original reader and rebuild behavior.

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
| Daily history + lessons | 3h idle (per session, `history_idle_hours` = 3.0) | `history/{date}.md`, episodic entries, `lessons.jsonl` (or `lesson.*` in vector store) | Persisted `last_consolidated` in JSONL metadata |

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
material by replacing a private Markdown document.

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

**Both paths write to the store the SESSION names, not the one the consolidator was
constructed with.** `_consolidate` resolves the session's store through
`context.store_of_session(log, key)` — the same resolver every turn-running surface
reads with — and takes markdown, lessons and vectors from it; an absent
`memory_store` key means the global store. Full rules, including why absence is the
signal and why the key is slot-owned, are in
[The write path](#the-write-path).

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

The history consolidation prompt includes a `"lessons"` key that extracts only implicit correction patterns — corrections the user made without explicitly saying "remember" (those are already saved immediately via `learn_add`). All lesson writes go through `write_lesson()` which provides substring dedup and topic-overlap dedup (shared keywords ≥ 50% of the LARGER of the two keyword sets → newer replaces older). When vector memory is not active, falls back to `lessons.jsonl` via `LessonStore.save()`.

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

**Existing V1 files are not migrated by this implementation.** `detect_lineage`
answers from the schema table, so a silo whose `memory.db` predates the crew
lineage keeps its existing schema. The product direction is eventual, explicit
V1-to-V2 migration after private V2 is validated and tuned. Maintaining both
policies is a transition, not a commitment to permanent parallel algorithms.
The future **V2 migration sign-off** is the decision gate: supported-platform
isolation and recovery checks pass, a held-out retrieval evaluation supports the
chosen thresholds, and the owner can review a migration preview with a tested
rollback path. That gate permits proposing a migration; it does not authorize
automatic conversion. The migration implementation and owner approval remain
separate work.
This PR implements no migration or cutover. Initializing an existing member provisions a separate
empty private V2 store and retains the old data; it does not reinterpret or
upgrade that old file. Newly provisioned stores start empty (see
[A store's three paths](#a-stores-three-paths-and-where-the-index-actually-lives)).

**Lineage metadata and private identity have different authority.** The
`schema_lineage` row is advisory because `detect_lineage` reads the schema table.
An unowned, markerless crew-schema file remains the supported legacy V1 case. A
store opened with a positively validated private V2 manifest atomically records
`private_memory_version`, `store_name`, and `owner_member` in the existing
`memory_meta` table. From that point onward a raw `VectorMemoryStore(path)` open
requires the external manifest to exist and match those durable values. A lost or
mismatched manifest, including after restoring a V2 database backup, fails closed
instead of downgrading the same file to V1. None of these rows is written on the
default v1 lineage, because adding rows to the operator's own `memory.db` is the
one thing this seam exists to avoid.

**Whole-install portability remains separate from member recovery.** The existing
snapshot and export/import components enumerate the global files and workspace
trees; they do not include per-member stores. Members now have dedicated full
bundle backup and staged restore under
[Automatic backups](#automatic-backups-memory_backuppy). The injection audit
`scan_memory` also opens every declared store and labels each finding with its
store. Whole-install export redaction does not scan member files that the export
does not include.

### Semantic Memory

The scoring descriptions in this section and Episodic Memory describe Global
**V1**. Owned member stores opt into the separate
[Member V2 retrieval policy](#member-v2-retrieval-policy). A nondefault filename
alone does not enable V2; the member ownership manifest must identify version 2.

SQLite table `semantic_memory` — structured key-value store with:
- **Allowed keys**: `_BUILTIN_PREFIXES` is `pref.*`, `project.*`, `user.*`, `lesson.*` (+ user-configurable `extra_prefixes`). The first three are the fact prefixes the consolidation prompt offers the LLM; `lesson.*` is the lessons tier writing into the same table.
- **Key format**: `^[a-z][a-z0-9_.]*[a-z0-9]$`, max 100 chars; value JSON max 4,096 bytes
- **Confidence gating**: writes whose source is not `user_explicit` require confidence ≥ `_DEFAULT_CONFIDENCE_THRESHOLD` (0.8); `user_explicit` bypasses the threshold
- **V1 conflict resolution**: `user_explicit` replaces an existing value; an automated source cannot replace an active user-explicit fact. Otherwise higher confidence wins, or the newer value wins when the confidence difference is less than 0.1. Tombstones can be recreated. V1 consolidation retains direct stale-key deletion and its semantic prompt, while extracted lessons keep their automatic consolidation source. An LLM confidence claim is not user evidence. Reaffirmation still refreshes confidence/source and reaches the original embedding and retirement paths. Owner edits retain shared revision checks. A rejected write logs a best-effort `conflict_skip` event; an unavailable event log does not prevent a V1 data write.
- **Injection detection**: the `_INJECTION_PATTERNS` regex set (14 patterns, `vector_memory_constants.py`) is scanned on every value write
- **Write-time embedding**: `_write_semantic()` embeds `"<key> <value_json>"` after the upsert (outside `_db_lock`, at `PRIORITY_BULK` — nothing blocks on it and the tail is reached from consolidation/import loops; same space-generation contract as `write_lesson`) and persists the struct-packed, un-normalized vector into the row's `embedding` column. The upsert's conflict clause keeps the stored vector when the value is unchanged (a re-affirmation — the tail then skips the redundant embed) and clears it when the value changed, so a row never ranks by a vector for text it no longer holds. `lesson.*` keys are excluded (`write_lesson` owns their vector — raw rule text). `set_semantic_if_absent()` (bulk import) defers embedding to the backfill sweep, like `write_episodic(defer_embedding=True)`. Rows missed while the model was absent — plus rows cleared by `reconcile_embedding_space()` — are repaired by `_backfill_semantic_kv_embeddings()` inside `backfill_missing_embeddings()`.
- **Audit trail**: `memory_events` table logs every create/update/delete with old+new values, bounded at `_MAX_EVENTS = 10_000`. The dashboard events API recursively redacts credentials and unsafe URLs on response for Global V1, named V1 and private V2. Stored events and their identities remain unchanged.

Retrieval formats `key: value` pairs in a `[Semantic Memory]` block and excludes `lesson.*` keys. With a query it uses `_SEMANTIC_VECTOR_WEIGHT` 0.6 × vector_score + `_SEMANTIC_KEYWORD_WEIGHT` 0.4 × keyword_score; `_stored_similarity_scorer` embeds the query once and reads stored vectors. When the query vector is available, a row without a vector contributes zero on that term; without embeddings, retrieval uses keyword scoring. Explicit identity terms supplement keys and values. V1 `build_session_context()` calls this query path with the current message. V2 leaves fragment retrieval to `memory_recall`, which has its own total response cap.

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

Episodic context retains `_DEFAULT_EPISODIC_LIMIT` = 8 results. Fresh V1 sessions query episodic memory with the current message and inject at most `min(_EPISODIC_INJECT_CAP, caps.episodic)`, where `_EPISODIC_INJECT_CAP` = 3,000; warm follow-ups do not repeat that injection. V2 session construction never automatically queries or injects episodic fragments. Its `memory_recall` response includes only rows fitting the tool's total cap, including wrappers.

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

Global Memory is **V1**. New members receive a unique empty **V2** store before
creation is published. Existing members keep their exact V1 binding until the
owner chooses private memory. Store ownership and algorithm version are recorded
in config, the protected manifest and the database. Private stores cannot be
shared or rebound. Choosing V2 preserves the V1 source and imports nothing
automatically. Ownership validation is specified in
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

Private execution also withholds Global V1 at the OS filesystem boundary, including
the database, sidecars, temporary/superseded files, global markdown memory and
backups. Direct shell access cannot bypass the explicit selected-copy operation.
The member's own structured memory remains gateway-managed through its trusted
binding. The [security module](security.md) describes private administrative-root
views, live project/runtime directories and isolated execution logs. Ordinary
Global V1 assistant processes keep their existing filesystem access.

| User flow | Memory behavior |
|---|---|
| Ordinary assistant without a selected member | Existing Global Memory V1 behavior |
| Member DM, without Crew Mode | The member's exact V1 binding until owner opt-in; its exclusive V2 store after opt-in and for new members |
| Crew Mode | Each delegate retains its own binding. V2 delegates cannot use Global, legacy V1 or a peer's store by proxy; handoffs share explicit tasks and results |
| Scheduled work | `member_id` pins the member separately from its provider template. Creation, firing and resumed chat validate the pinned store |
| Restart or continuation | Trusted persisted identity restores the same store; subagent transcripts cannot replace protected run identity |
| Unavailable or corrupt memory | Execution reports the reason before submitting the task. No automatic global fallback or creation of a replacement empty database |

The Memory tab has separate global and member views. Member links address
`/settings/overview?view=memory&store=<store>`; every member data request carries
that explicit owner-authorized store. The global settings and global vector
browser do not mount inside a member view. The private view supplies its own
compact identity header and a single Overview back action. It uses the app's
theme colors, the owning member's avatar with a private lock badge, category
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
keys and concrete conversation references. Legacy members show Memory V1 and
an optional private-memory action. A V2 member requires its valid private
identity before work and cannot use Global or another store on failure.
Global and named V1 summary counts reuse the Semantic and Episodic labels from
the existing V1 diagnostic tiles. Private V2 summaries retain Facts and lessons
and Experiences.

The Crew Manager notice distinguishes new private members from existing V1
members. Passive V1 notes describe only the current memory. V1-to-V2 opt-in
shows a separate confirmation that the next chat starts fresh and the member
cannot return to V1; existing data and chats stay. Cancelling keeps the binding.
Manage memory shows a visible reason while unsaved edits disable navigation.
During opt-in, Creating private memory is a `role=status` announcement tied to
the current member and editor opening; unrelated busy work does not announce it.
The Global V1 inline editor opens under Edit saved memories and retains its
existing Lessons terminology. The shared record editor labels directive records
Lessons, while the stored kind and API value remain `directive`.

V1-to-V2 opt-in clears the member's cached thread destination. The next Open
member action obtains the server's fresh private conversation key; old V1 history
and native context are not copied. A mismatched or unavailable private identity
is shown as unavailable, never as a usable legacy store.

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

V2 labels owner changes as Edit and retained older experiences as Replaced
experiences. Recall explains which context the member would receive; the record
list remains available for browsing and editing. Included rules have a summary
and a details disclosure rather than an empty badge. Recovery attributes removed
older backups to the configured backup limit. The member header shows its private
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

V1 retains its existing fresh-session context: bounded preferences/projects,
decayed daily history, semantic and query-ranked episodic memory, plus
query-ranked project-scoped lessons. Warm follow-ups do not repeat that recall.
V2 context includes essential preference/project anchors and query-free,
project-scoped lessons. V2 prompt construction performs no embedding search or
episodic/semantic retrieval. Its runtime tells the agent to call `memory_recall`
for a changed topic or prior decision and to
use `learn_add` for corrections. The agent prompts (`config/prompt.md`,
`config/prompt-orchestrator.md`) give both versions the same order for a question
about the past: the injected block and lessons, then `memory_recall`, then
`search_chat_history` for verbatim transcript text. That order is what keeps a V2
member — whose injected block carries no facts or episodes — from falling through
to a transcript keyword search, and tells a V1 session that the block was ranked
once against its first message. Retrieval is reference material and does not
override the current user's instruction. Forgetting removes a row from future
long-term recall; it does not erase text already in an active conversation.
Backup and staged restoration cover the entire member memory bundle, as
specified under [Automatic backups](#automatic-backups-memory_backuppy).

### Member V2 essential context

`member_essential_context.py` separates essential material from on-demand
fragment recall. `member_for_store()` derives the owner from a positively
owned V2 store and validates its exclusive binding. A supplied member that
disagrees with that owner is an error. V1 and unowned legacy stores retain
their existing context path.

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
working briefing keeps its existing bounded/platform-gated reader; it is not
unbounded archival memory.

Admitted project essentials are the active project's root `AGENTS.md` and
`SOUL.md`, default/`always` documents under `.kiro/steering`, and Markdown file
resources explicitly declared by the template. Native `manual`, `auto` and
`fileMatch` steering retain their trigger semantics. A custom template's
declared prompt may be inline or a file source. Missing optional root files
are allowed; an unreadable declared source or malformed/shadowed template
fails with its name instead of silently substituting a different persona.
Template resources cannot import Global V1 memory or another member's state;
the owner's preferences/projects use the separately validated private reader.
Declared globs have bounded enumeration and do not follow linked directories.
Containment is judged on resolved paths on both sides: a declared root (the
project root, or the owner's home for a resource outside it) is normalized the
same way an admitted document is, so a root reached through a symlink -- a
symlinked `$HOME` -- admits its documents, while a document that is, or sits
under, a link below the root is still refused. The managed-state isolation
(`_refuse_managed_source`) compares its admin and workspace roots in the same
resolved spelling, so it fires for resolved candidates on symlinked-home hosts
exactly as it does elsewhere.

These essentials are refreshed from the current source on every member turn.
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
Current private preferences/projects are included when memory context is
allowed. Explicit memory/project context exclusions and temporary-session read
restrictions continue to withhold their respective materials; permanent conduct
and owner identity still apply.

### Member V2 retrieval policy

`VectorMemoryStore.algorithm_version` reports `v1` or `v2`; `policy_revision`
reports `member-v2` for owned member stores. `memory_store_version()` selects
the policy from the private store's ownership manifest. The global filename and
unowned legacy files keep V1. A V2 manifest over a V1 database is refused: there
is no implicit migration or schema reinterpretation.

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
for both versions, using the selected store's retrieval policy. It returns bounded semantic, episodic and lesson context plus
the selected fact/episode snippets and their compact retrieval evidence, never
unabridged source rows or vector blobs. Truncated episode evidence is marked;
full source content remains available to the owner editor. The character cap
includes wrappers; an empty query or zero cap yields no context. A separate
16 KiB transport budget counts JSON escaping, previews, evidence and the MCP
TextContent envelope, with 1 KiB reserved for ordinary RPC framing/request IDs.
Caller-controlled arbitrarily large JSON-RPC IDs are outside this memory-data
bound. The final HTTP/MCP serializers recheck the budget after redaction,
omitting whole tail records and regenerating matching contexts, previews and
counts when necessary. Omitted records are reported in the retrieval metadata.
Episodic references carry their stable memory ids. The MCP transport derives the store
from the caller's trusted binding; it accepts no model-selected store argument.
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
original similarity/exact-phrase heuristic, including its original audit values
and absence of a retirement count cap. The bounds below apply to private V2.
An unchanged V2 semantic value does not trigger retirement.

Three bounds make it acceptable, and each is pinned by
[`test/test_episodic_retirement.py`](../../../test/test_episodic_retirement.py):

- **An assertion linked to the full semantic key.** A candidate clause must
  start with the full key and its value assignment, for example `pref.color:
  red` or `pref.color is red`. A shared attribute alone cannot identify whose
  fact changed: `Bob's color is red`, `project.color: red` and an unqualified
  `color: red` remain active when the owner changes `pref.color`. Normalization
  ignores case and JSON quotes; word boundaries keep `redwood` from matching
  `red`. Negation, historical markers and uncertain paraphrases stay active.
  This conservative rule does not call an embedding model.
- **A per-write ceiling**, `_MAX_EPISODIC_RETIRED_PER_WRITE` = 3. A candidate beyond the
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
reach and the CLI is not the recovery path for a silo.

### Automatic backups (`memory_backup.py`)

**Private V2 backup covers the whole member memory.**
`member_memory_backup.py` creates `memory.<UTC microsecond timestamp>-<UUID>.zip` containing a
SQLite online copy of `memory.db` plus the present `memory/preferences.md`,
`memory/projects.md`, dated `memory/history/*.md` and `lessons.jsonl` files.
Derived FTS/FAISS indexes are rebuilt; configuration, arbitrary files and other
members are excluded. Each file is read once and hashed from the archived
bytes. SQLite is transaction-consistent; the Markdown/JSONL files are individual
read snapshots, not one transaction synchronized with all database writes.

The versioned snapshot manifest records store identity, exclusive owner,
creation time and SHA-256 for each allowed file. Backups live in
`memory_stores/.member-backups/<store>/`, inside the existing protected tree
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
When a database has durable private-memory markers, its V2 version and member
owner must also match before snapshot creation, staging or activation. Older
snapshots without these markers remain compatible through their validated
outer store and owner manifest.

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
of the verified protected stage; a cleanup failure can leave an inert temporary
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
preserved prior tree. An existing directory with a missing or mismatched owner
marker is refused. This recovery exception never relaxes ordinary memory reads.
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
gone. Active member V2 stores get a daily rotating hot copy. Global and named V1
stores are copied only when the owner requests a manual backup. The heartbeat schedules its
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
  across reload/save, so disabling automatic V2 backups or extending recovery
  retention survives a gateway restart. Automatic retention never visits V1.
  Manual dashboard backups use the same configured retention in their worker.
- **Enumeration**: the heartbeat passes `private_only=True` and visits only
  actively owned V2 stores. The explicit all-store backup helper retains Global,
  declared named V1 stores and active V2 stores. Neither uses a glob of `memory_stores/`: a glob
  would adopt an abandoned or restored directory the operator never declared and then
  copy it forever. Each resolved path is confirmed to belong to the store that asked for
  it, independently of strict binding resolution.
  Archived files and backup listings remain available for owner inspection, but
  archived stores are excluded from these passes. Restore requires an active
  exclusive binding; there is no archive reattachment UI.
- **Fail soft per store**: one unreadable store must not prevent another eligible
  store's backup, so the loop counts failures instead of propagating them.

**V1 restore is staged and recoverable.** `restore_from_backup` requires the
source's structural V1 lineage for Global. Named V1 also accepts the established
unowned crew-schema shape. Both refuse either private ownership marker, empty
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
- Failed model loads (corrupt file, bad native libs) are retried only after a 300s cooldown so a broken state can't spawn a loader thread per embed call

**Embedding backend abstraction** (`EmbeddingBackend` ABC): the public swap seam for future runtimes (Ollama again, remote endpoints, ONNX) and user-defined models. Surface: `model_id`, `dim`, `is_ready()`, `embed()`, `embed_batch()`, `close()`. Consumers (vector memory, knowledge library) depend only on this interface; everything llama.cpp-specific lives in `LlamaCppEmbedder`. Swap flow: `register_embedding_backend(factory)` + `reset_shared_embedder()` replaces the singleton (pass `None` to restore the default). A backend with a different `model_id`/`dim` produces incomparable vectors — the knowledge library's `embed_signature` is derived from `embedding_space_signature` and so folds BOTH in, meaning a swap (including a width change at a constant model id) automatically triggers the sig-gated knowledge re-embed; vector memory re-embeds via `migrate`.

**Shared embedding budget.** Native inference has one shared worker and model.
The normal interactive default is four native threads; background bulk work
defaults to one. Explicit normal and bulk thread settings are honored within
the available CPU count and the existing configuration range of 1–256; a bulk
value of 0 inherits the ordinary thread setting. At most eight pending native
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
- `POST /api/memory/enable-embeddings` — never blocks on the download: if the model is absent it kicks (or adopts an already-in-flight) background download with `DOWNLOAD_ATTEMPTS_INTERACTIVE` (3) attempts and returns immediately (`{"ok": true, "status": "downloading"}`); the frontend polls `embedding-status` for progress and keeps the same polling lifecycle across non-terminal `setup_step` transitions. When the model is present it installs faiss-cpu if missing, wires the embed function, and persists config. The dashboard no longer surfaces a proactive "Start Embedding Engine" button (embeddings auto-start at boot) — this endpoint now backs only the error-state **Retry** affordance
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
- Key: `lesson.<md5_of_rule>` when the lesson is global (dedup via hash). A lesson
  carrying `repo_scope` folds the scope into the hash, so the same rule scoped to two
  repositories is two rows and an unscoped row keeps its historical key byte-for-byte.
- Value: a mapping `{"rule": ..., "category": ..., "negative": ...}` — the NOT-clause
  — plus `"repo_scope": ...` when the lesson is restricted to one repository. The key
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

Model: `Qwen/Qwen3-Embedding-0.6B` Q8_0 GGUF (610MB). Apache-2.0 licensed. Served in-process via the vendored llama-cpp-python runtime on all supported platforms.

### Consolidation Integration

`HistoryConsolidator._consolidate()` now extracts structured data alongside existing fields:
- `"semantic"` array → `_write_semantic()` for each (max 20 per consolidation), always under `source="consolidation:<key>"`. An LLM confidence claim never grants `user_explicit` authority, so the conflict rule protects genuine user-stated facts. Extracted lesson writes likewise retain their automatic consolidation source in both versions.
- `"episodic"` array → `write_episodic()` for each (max 10 per consolidation)
- Dual-write mode: when `config.memory.migrated` is False, also writes markdown files (backward compat)

The store's `algorithm_version` selects the update policy. V1 keeps its existing
confidence and source precedence, direct consolidation deletion, same-value
refresh and automatic lesson source. Semantic consolidation keeps its automatic
source even at confidence 1.0. V1's audit log remains best effort.
Private V2 keeps inferred changes and deletions as review proposals unless a
correction has matching revision and transcript evidence. Its consolidator
retains the actual automatic source and supplies record metadata and correction
evidence fields. A named store with the legacy schema still follows V1 unless
its validated private marker selects V2.

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
| POST | `/api/memory/disable-embeddings` | HTTP 410 stub — embeddings are always-on; kept only until the frontend removes its Disable button |
| POST | `/api/memory/migrate` | Migrate markdown → structured memory (gated) |
| POST | `/api/memory/import` | Import from JSON export (gated) |
| POST | `/api/memory/promote` | Promote repeated episodic patterns to semantic facts, tombstoning the rows folded in (gated) |
| POST | `/api/memory/consolidate` | Trigger consolidation for one session (restricted-mode check only) |
| GET | `/api/memory/context-preview?q=` | Preview injected semantic + episodic context |
| GET | `/api/memory/observability?q=` | `stats` + `rejections` + `context_preview`, plus `reads` — the read-volume counters (see above). `reads` is resolved LAST, so it INCLUDES the reads this request itself performed; that is what lets a caller issue the same `q` twice and compare the two objects |
| GET | `/api/memory/recall?q=` | V2 task recall with evidence. Explicit `store` requires the dashboard owner; the MCP path requires a positive process/session proof and one recognized, non-temporary session, then uses its trusted recorded private binding. A shared internal secret plus session header is insufficient. Invalid or unavailable private memory returns an explicit error |
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
lock. V2 history GET returns today's guarded document, and PUT compares that
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
`consolidate` follows the protected binding of its target session and verifies
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
| GET | `/api/memory/stores` | Enumerate every store for the picker: `name`, `is_default`, `lineage` (`v1`/`crew`), `exists`, `semantic_count`, `episodic_count`, `lessons_count`, `facets_supported`, `backup_count`, `newest_backup`. Takes no `?store=` — it answers for all of them |
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

`kirocrew memory {list,search,show,stats,audit,export,migrate,import,carve}` — manage memory from the command line:
- `carve --store <name>` — filter or count one store's rows by their carve facets; see [Who reads a facet](#who-reads-a-facet). Dispatched before the shared vector store is opened, because it opens the store NAMED on the command line rather than the default one
- `show [preferences|projects|history]` — read the markdown layer through `MemoryStore` (all three targets when none given); `--format md|json` (json entries carry `path`, `updated_at` mtime in UTC ISO-8601, `content`), `--since YYYY-MM-DD` filters history days. Missing/empty files print as empty rather than erroring
- `search <query>` — searches BOTH memories and labels each section: the vector store's episodic recall, then keyword hits from the markdown layer's FTS5 index (`MemoryStore.search`, over `preferences.md` / `projects.md` / every `history/*.md`). `--layer vector|history|all` (default `all`); `--layer vector` reproduces the previous vector-only output exactly, and `--layer history` skips constructing the vector store entirely, the same way `show` does. The two indexes answer different questions — "where did I write this word" versus "what does this mean like" — so they are reported separately rather than merged into one ranking. `search_episodic` text-searches whenever `query_embedding` is None and does not auto-embed, so the vector section embeds the query in-process, blocking once on the model load (`_SEARCH_MODEL_LOAD_TIMEOUT_SECS`, 120 s) — a one-shot read cannot lean on the gateway's boot re-embed sweep the way a WRITE can. It degrades to keyword matching, naming the reason on **stderr** (stdout shape is unchanged), when the model is not downloaded (a one-shot CLI never kicks the download), when the store's vectors were produced by a different model, or when the model fails to load; and when the semantic pass returns nothing it retries the keyword leg once before reporting "No episodic memories found.", because the vector legs score only rows with a non-NULL embedding and deferred/imported/re-embed-pending rows are keyword-searchable only until the gateway's sweep reaches them
- `stats` — counts, embedded coverage, FAISS accelerator status, audit event count, and a **`Reads (this process)`** block from `read_counters()` (rows + statements, then the semantic/episodic population-scan tallies). Labelled per-process because the CLI constructs its own store, so the totals describe only what this invocation read; the gateway's totals are the `reads` object on `GET /api/memory/observability`
- `export` — vector-store collections; `--include-markdown` opts in a `markdown` collection (`preferences`/`projects` entries + per-day `history` list from `MemoryStore.markdown_snapshot()`) without changing the default payload shape
- `migrate` — one-time markdown → structured migration (preferences.md → semantic, history/*.md → episodic)
- `import <file>` — restore from JSON export with full validation
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
change the inode being queried. The lineage scanner's 10,000-row scan limit applies
to the aggregate active rows across its supported semantic and episodic tables and
is checked before either table contributes an item. Episodic text deduplication is
rechecked under the native store write lock before insertion, preventing a
concurrent native write from being duplicated.

## Lessons (`learn.py` → `vector_memory.py`)

User-taught corrections ("always do X", "never do Y"). Single write path through `vector_memory.write_lesson()`:

1. **Vector memory** (primary): stored as `lesson.<md5hash>` semantic entries with `confidence=1.0, source=user_explicit`. The value is a mapping `{"rule", "category", "negative"}`, plus `"repo_scope"` when the lesson is restricted to one repository — the NOT-clause is a separate field; legacy in-band `"rule — NOT: negative"` rows stay readable without migration. Injected via `get_lessons_context()` — separate from `[Semantic Memory]` block. A scoped lesson is gated by `project_scope.project_scope_satisfied` against the session's active project BEFORE the shown/omitted counts are computed, using the same rule as a skill's `repo_scope`.
2. **JSONL fallback** (`~/.kiro/crew/lessons.jsonl`): only used when vector memory is not initialized. Read-only migration source once vector memory is active.

**Priority**: vector lessons override JSONL. The fallback is keyed on whether the
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
the injected lessons block. It is empty on every path that deleted nothing (including
`enriched`, which is decided in pass 1 and skips the dedup scan), is forwarded by
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

**Migration**: `migrate_from_markdown()` reads `lessons.jsonl` and writes each entry as `lesson.*` semantic key with `source=migration, confidence=0.9`. User-explicit lessons (confidence 1.0) can't be overwritten by migration.

Categories: `tool`, `preference`, `knowledge`. Injected as a `[Learned corrections]` block. V1 session context retains query-ranked, project-scoped lessons; V2 selects bounded, project-scoped lessons without a query embedding. Explicit lesson readers can use hybrid relevance and fill the caller's character budget, reporting shown and omitted counts; the JSONL path caps at `_MAX_LESSONS_IN_CONTEXT = 50`. The JSONL store retains `_MAX_LESSONS_TOTAL = 200` and prunes oldest-first beyond that.

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
| Duplicate lessons in V1 | Substring dedup (contained-in-stored declines; contains-a-stored-one deletes it, longer wins, regardless of source), then topic-overlap dedup (shared keywords cover at least 50% of the LARGER keyword set; newer replaces older regardless of source), then embedding dedup (cosine > 0.85; newer replaces older unless a stored near-duplicate outranks the write: `user_explicit` over a lower-authority source, or strictly higher stored confidence). A non-mutating authority pre-pass decides semantic-match refusals before the scan; it mirrors the earlier substring/topic branches, which remain source-blind. Semantic-match deletions are deferred until the scan completes without refusal; substring/topic branches retain their delete-as-you-go behavior. The final semantic write follows those deletions and reports any superseded rows even if it refuses. A write declined on authority deletes nothing and reports empty `superseded`; every completed deletion is named in `LessonWriteResult.superseded` | `vector_memory.write_lesson()` |
| Distinct lessons in V2 | Different rule text coexists without substring, topic or embedding deduplication. Exact-rule enrichment remains; key-targeted corrections use the owner/revision machinery | `vector_memory.write_lesson()` |
| Contradicting episodic fragments | No explicit resolution: time decay plus MMR surfaces the newer/more relevant fragment | `vector_memory.search_episodic()` |
| A semantic value is superseded | `_retire_stale_episodic()` tombstones episodic rows that quote the old value | `vector_memory._write_semantic()` step 9 |

### Memory across surfaces and channels

**A private V2 store belongs to exactly one Crew Member.** Its trusted binding
follows direct conversation, Crew delegation, scheduled work and restart.
Unbound ordinary sessions use the existing global V1 store; an unverifiable
private session fails instead of falling back to that global store. Legacy
named V1 stores retain their own files. `test/test_memory_v1_golden.py` pins
the preserved V1 policies and eager session recall. Shared metadata, record
editing and optional MCP recall do not convert V1 or replace its prompt path.

Deleting or package-pruning a Crew Member retires its private store by removing
the member binding while retaining the ownership record and files for explicit
recovery. A later member with the same display name is a new generation and is
provisioned with a fresh random store identity; it never inherits the retired
generation's history implicitly. The retained store consequently fails normal
private-store authorization while it is unbound. Sync publishes the new member
and its ownership record together so a successful sync cannot expose an agent
whose private store is undeclared.
After committed deletion or pruning, context and dashboard caches release that
store's Markdown, lesson, SQLite, FAISS and scoring handles. A cache generation
check prevents an in-flight constructor from republishing an evicted store.

On the **default path** a workspace splits three of the six layers and no more:
`get_memory_for()` hands every non-default *workspace* the default workspace's
`VectorMemoryStore`, so semantic, episodic and lesson rows are global — a lesson taught
in a Slack DM applies in the dashboard and vice versa. The Markdown layers
(`preferences.md`, `projects.md`, `history/`) and the JSONL `LessonStore` are
per-workspace-directory, so those ARE isolated when channels are configured onto
different workspaces.

On a **named store** all six layers are isolated, because all three handles are that
store's own: its markdown tree, its FTS index, and its `memory.db` holding its semantic,
episodic and lesson rows. **A named store must never be handed the global
`VectorMemoryStore`.** Handing it one is what reduces the crew editor's Memory Store
control to a read-side illusion: markdown splits, and every crew's semantic, episodic and
lesson rows still land in one table. A silo whose `memory.db` already exists stays on the
v1 lineage for the life of that file — only a newly created one gets the crew schema (see
[Two schema lineages](#two-schema-lineages)).

The JSONL lessons tier is per-target for the same reason. When the resolved store has no
vector lessons to answer with, a named store reads its OWN `lessons.jsonl` through
`get_lessons_for(workspace, memory_store)`; the default and workspace paths read
`self.lessons`, the global store the builder was constructed with. Without that split a
crew's `[Learned corrections]` block is the operator's global corrections, which is the
one thing a silo exists to prevent.

The three `/api/lessons` routes (`handlers/cron.py`) are bound by the same rule and reach
it the same way: `_session_memory_store` reads the caller's binding off its session
metadata, that binding picks the vector tier, and `_lesson_jsonl_store` picks the JSONL
tier — **the destination follows the BINDING, never the population.** A named store starts
empty unless the owner explicitly copies selected knowledge, so "this store holds no lesson rows" is the
ordinary state of a freshly bound crew, and the answer to it is that store's own
`lessons.jsonl`. Key the fallback on population instead and an empty silo lists the
operator's lessons, substring-deletes one of them, and files the crew's own correction into
the one file every other crew is injected with. A silo also takes no workspace union or
`scope: "workspace"` arm: a store name and a workspace name are separate namespaces and the
store is the tighter scope, exactly as `_target_key` resolves them.

#### Two namespaces, two arguments

`get_memory_for(workspace=None, memory_store=None)` and
`get_lessons_for(workspace=None, memory_store=None)` take a workspace and a store
SEPARATELY, and must keep doing so. A store name and a workspace name are different
namespaces, so a single key cannot hold both: collapse them and a crew bound to store
`acme` alongside a workspace also called `acme` shares one cache slot and one path
resolution, letting whichever is built first decide where the other one reads.
`_target_key(workspace, memory_store)` is the only thing that mints cache keys, and
there are three shapes:

| Key | Means | Vectors |
|---|---|---|
| `"default"` | the global store; seeded eagerly in `ContextBuilder.__init__` | the global `VectorMemoryStore` |
| `"ws:<name>"` | a named workspace on the default path | the global one, shared |
| `"store:<name>"` | a named memory store | that store's own; mandatory for private V2, optional for legacy V1 |

`:` cannot appear in a store name (`validate_memory_store_name`), so the prefixes
cannot collide with each other or with `"default"`.

#### The five resolution cases

`_resolved_store_name(memory_store)` answers with a non-default store name, or `""`
meaning "use the default path". Only an absent/empty argument or the literal
`"default"` selects that path. A supplied invalid identity is an error:

| Caller passes | Resolves to | Cache key |
|---|---|---|
| `None` or `""` | `""` | `"default"`, or `"ws:<name>"` when a workspace is given |
| `"default"` | `""` — short-circuited before any config read | as above |
| a declared, usable non-default name | that name | `"store:<name>"` |
| an **undeclared** name | `UnknownMemoryStore` from `require_memory_store` | no cache entry |
| a **malformed** name | `UnknownMemoryStore`; no name repair or global fallback | no cache entry |

Validation checks the declared store, private ownership and readable database
before using a named cache entry. A missing or unreadable member store is never
replaced with the global store or an automatically created empty database.
The opened database inode must have exactly one filesystem link; a hard-linked
alias is refused because path resolution alone cannot show that two store paths
share one SQLite file.

#### `ensure_store` is async, and separate on purpose

`await ContextBuilder.ensure_store(name)` stands up a named store's own
`VectorMemoryStore` once and caches it in `_vector_stores`, keyed by resolved store name.
It returns `None` for the default store (whose vector store is the global one, wired at
startup). A private V2 failure raises; a declared legacy V1 store may return
`None` and retain its own Markdown/keyword path.

It cannot live inside `get_memory_for`. That method is synchronous, is called
unconditionally on every context build, and holds `_stores_lock`; `VectorMemoryStore.init()`
is blocking file IO end to end (owner-only sweeps, `sqlite3.connect`, the WAL pragma,
three migrations, a FAISS load) whose documented caller contract is to offload it. A
blocking init there would stall the event loop for every caller that builds context
inline and serialize every embed worker on a store's first touch. `init()` also has no
idempotence guard — it reassigns `self._db` — so a lazily-initializing sync resolver is
exactly the shape that leaks a connection. One instance per `db_path` is likewise an
invariant rather than an optimization: two instances over one file do not share
`_db_lock`, which voids the serialization the store's own writes depend on, so a
construction race closes the loser.

**Private V2 requires `ensure_store` before context assembly.** An unprepared
private tier raises with the member store's identity. A legacy V1 named store
may still use its own Markdown/keyword path, without borrowing global rows.
`prepare_store_vectors` translates preparation failures into a concrete private
memory error before a turn-running surface starts provider execution.

#### How a turn-running surface names its store

There are exactly two ways a call site answers "which silo does this turn read", and
which one applies is decided by what identity the surface holds:

- **A crew alias in scope** → `resolve_agent_bindings(cfg, alias, project).memory_store_name`.
  This is the dashboard chat turn (`chat_runner`) and a `spawn_run(crew=…)` subagent,
  where the crew is what the caller was asked for.
- **Only a session key in scope** → `context.store_of_session(conversation_log, key)`,
  which validates the recorded `meta["memory_store"]`. Protected subagent run
  identity is authoritative before transcript metadata is consulted. This
  is every channel surface. `context.session_store_for_turn(ctx_builder, key)` is the
  pair a turn needs — that resolution, then `prepare_store_vectors` — and it is what
  Slack (native and transport), Discord, Telegram, the shared `messaging/dispatch`
  pipeline, auto-nudge, and both subagent-completion injections call.

`store_of_session` is also what `history_consolidation._consolidate` resolves the write
side with, which is the point: one conversation's reads and its
consolidations name one silo. `dashboard/handlers/_shared._session_memory_store` is a
thin adapter over it (a `DashboardState` rather than a log), not a second implementation
— two copies is how the dashboard's answer and a channel's answer drift apart for one
session. An absent key, blank or literal `default` retains global V1. Ordinary
legacy calls without a log also remain global. A non-string recorded identity,
unreadable metadata or failed named-store validation raises with its reason;
the read never falls back to global memory.

**Never derive the store from `agent`.** On every channel surface that field is a
kiro-cli agent name — a namespace disjoint from `cfg.agents` — so a store derived from
one resolves to `default` for exactly the crew that configured otherwise, silently, and
toward the operator's own memory. `scripts/check_memory_store_seam.py`'s
`store-not-derived-from-agent` rule fails the build for it.

#### The write path

A consolidation learns its store from the session's OWN metadata, not from the
consolidator's constructor:

- `session_control.create_session` records `memory_store` in birth metadata **only when
  the resolved binding is not the default store.** ABSENCE means global, so a default
  user's metadata line stays byte-identical and a session carrying no such key is
  unambiguously global rather than "global as of whenever it was saved". The birth dict is
  the only record for a session that is created and then sits idle.
- `memory_store` is in `history.SLOT_OWNED_META_KEYS`, so current slot metadata
  owns its presence or absence instead of carrying a stale historical value
  forward. Private member bindings themselves are immutable.
- `context.store_of_session(log, key)` answers `""` for no key, the literal default or a
  blank value. Every other value passes strict named-store validation; invalid
  or unavailable identity aborts that consolidation without a global write.
- `_consolidate` then resolves all three handles for a named store — markdown via
  `get_memory_for(memory_store=…)`, lessons via `get_lessons_for(memory_store=…)`,
  vectors via `await ensure_store(…)` — and passes them into
  `_write_structured_memory(result, key, vector_store)` and
  `_save_lessons(raw, vector_store, lesson_store)`. Omitting them keeps the global
  handles, which is what the workspace and default arms want.

The riskiest read on that path is `get_all_semantic`: those rows go into the
consolidation prompt and the prompt instructs the model to update and DELETE them, so a
global fetch under a crew's consolidation would show crew B the operator's own semantic
table and let its turn delete it. That fetch reads the resolved store, never
`self._vector_store`.

#### Routing a task to a crew

Two tools, one grammar. `route_crew(task)` RANKS the crews whose `triggers` match and
reports each one's score, `description` and resolved `memory_store`; `select_crew`
returns the roster for the model to judge. They exist together because the questions
differ — the same task should reach the same crew when a caller wants determinism, and a
model should weigh prose when it does not.

Scoring lives in `trigger_match`, shared with `SkillsLoader.get_triggered_skills`. One
definition on purpose: two would agree on the easy phrasings and diverge on the ones that
decide a route, and the symptom would be a task handled by the wrong crew — the leak a
per-crew silo exists to prevent, arriving through the router rather than through the
store. A crew with no `triggers` is not a candidate, which is the operator's opt-out, and
no match returns NOTHING rather than the closest crew.

Acting on a route means `spawn_run(crew=…)`, which is the only spawn form that carries a
crew's store and template together. `spawn_run(agent=<crew name>)` is accepted and runs
against the DEFAULT store, because `agent` is a template namespace — the reason
`select_crew`'s guidance names `crew=` explicitly.

#### What is NOT isolated yet

Member-bound execution names its private store. The following unowned V1
surfaces and shared tools remain outside that per-member scope:

- **Unowned unattended and offline surfaces read the global store.** A scheduled
  job with `member_id` now validates and uses that member's private store on
  creation, firing and resume. Jobs without member ownership, the heartbeat,
  the webhook agent runner
  (`dashboard/handlers/hooks.py`), the task runner's planner and executor, and
  `eval/runner.py` pass no store. That is the correct answer for each rather than a
  pending fix: an unowned job's `agent_id` / `agent_sequence` entries are provider
  template names rather than member identities; the heartbeat is one process-wide key on the fixed
  `kirocrew-heartbeat` template; the webhook's `agent` is validated against INSTALLED
  kiro templates and its `hook:` session is ephemeral, so it records no binding; the
  task runner's per-step session keys are synthesized, and giving a run the store of
  the conversation it was started FROM is a design decision about whose memory a task
  run belongs to rather than a resolution of identity in scope; the eval harness runs a
  synthetic key in a throwaway workspace. The omission stays visible in
  `test_memory_store_seam.EXPECTED_BACKLOG` rather than being papered over with a
  keyword that changes nothing.

  What IS covered: the dashboard chat turn and `spawn_run` resolve a crew alias through
  `resolve_agent_bindings`; Slack (native and transport), Discord, Telegram, the shared
  `messaging/dispatch` pipeline, auto-nudge, and both subagent-completion injections
  resolve the session's own recorded binding through `context.session_store_for_turn`.
  The store is RESOLVED or CARRIED, never derived from `agent`.

  Delegated runs are likewise covered: `SubagentInfo` carries a `memory_store`, and
  `spawn_run(crew=…)` resolves a named crew's store through `resolve_agent_bindings`.

  A channel conversation reaches a silo exactly when its session records one — a thread
  taken over from (or resumed into) a crew-bound dashboard session, or a channel slot
  whose crew was switched from the dashboard. A channel that was never bound to a crew
  records no key and runs the v1 path, which is nearly every channel conversation.
- **The dashboard Memory panel reaches any DECLARED store, for the OWNER only.** The
  markdown documents, semantic rows, episodic rows, events, carve and stats are read
  and edited per store through the owner-gated `?store=`, and the retired, backup,
  and restore routes are store-scoped in the same way (see
  [Which store a dashboard route reads](#which-store-a-dashboard-route-reads-store)).
  What is still GLOBAL-store-only is every route that carries no store parameter and
  opens the gateway's own handles: the memory graph, `observability`,
  `context-preview`, `promote`, `migrate` and `import`. So a silo can be browsed and
  edited from its private UI while promotion and the graph remain separate global
  tools. Those global controls do not mount inside the private member view.
  `consolidate` is the exception that
  needs no parameter: it triggers one SESSION's consolidation, and the consolidator
  resolves that session's own store from its metadata. A non-owner dashboard session
  keeps reading the global store, exactly as before.
- **`security.scan_memory` DOES scan a named store** — it is the one reader on this list
  that reaches one. It enumerates the declared table through `usable_store_names`, opens
  each store's `resolve_store_path` directly, and attributes every finding with a `store`
  key; see [security](security.md). What it still does not reach is a silo that is not
  DECLARED, which is deliberate rather than pending.
- **The markdown export surfaces cannot read a named store.** `markdown_snapshot` and
  `read_history_entries` go through `_guarded_entry` →
  `hooks.safe_read_file_bytes_nolink`, whose resolved-path check calls `is_sensitive_path`
  — True for anything under the `memory_stores/` fence — so a named store answers with
  empty entries. Nothing reaches it today: both callers are `kirocrew memory` CLI verbs
  anchored on `_markdown_memory_store()`, the default store. The ordinary context read
  path does plain reads and is unaffected, and so are the store-scoped dashboard
  markdown routes — `read_preferences` / `read_projects` / `read_recent_history` read
  plainly and never enter `_guarded_entry`, which is why a silo's documents are
  editable from the Memory panel while `markdown_snapshot` still answers empty for it.
- **The skills catalog remains shared.** `_run_skill_detection` /
  `_process_auto_skills` write through `SkillsLoader` into the single skills root.
  Private V2 consolidation skips that export so a member's private experience
  cannot automatically become a skill visible to other members. V1 behavior is
  unchanged.
- **Per-store `embedding_provider` has no effect, and cannot be given one as written.**
  `MemoryStoreConfig.embedding_provider` is merged into `effective_memory_config` by
  `resolve_memory_store_config`, but `_build_store_vectors` reads top-level `cfg.memory`,
  and the embedder underneath is `get_shared_embedder()` — a process-wide singleton
  holding one ~700MB model. Two stores on two backends would mean two resident models and
  two incomparable vector spaces, so this stays inherit-or-restate.

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

Frontmatter is parsed line-by-line (`_parse_frontmatter`): only a column-0 `key: value` line is a field. A value that is a bare block-scalar indicator (`>`, `|`, optionally chomped with `-`/`+`) is resolved from the indented lines that follow — folded (`>`) folds single breaks to spaces while preserving blank-line counts and more-indented line breaks, literal (`|`) preserves newlines — so a multi-line `description` still routes. Explicit indentation indicators (`>2`) are not supported. The other frontmatter readers stay reconciled with this resolution: the onboarding import gate treats a bare indicator as an activating `always` value (fail-closed), the auto-skill update path's `history._frontmatter_value` resolves block scalars the same way, so a live skill's block-scalar `description`/`triggers` survive the staged-candidate round-trip instead of collapsing to the indicator character, and the skill-provider preview endpoint (`dashboard/handlers/discover.py`) parses SKILL.md with the loader's own grammar, so the previewed name/description match what the installed skill will show.

Supports nested directories (e.g. `skills/utils/tiny-url/SKILL.md`). The skill name is the relative path from the skills root (e.g. `utils/tiny-url`).

**Source precedence** (project-level wins): `$KIROCREW_PROJECT_DIR/skills/` → `builtin_skills/` (bundled). Auto-copied to `~/.kiro/crew/skills/` on first run. Copies entire skill directories (scripts, assets, etc.).

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

**One enforcement point for every enumerated read.** Enumeration is TTL-cached, so a
path vetted while genuine can be replaced by a link out of the granted directory before
anything reads it — and the root that made it acceptable is only known at enumeration
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

No confined project path is rendered into agent-facing context. Both the legacy and
budgeted initial skills blocks inject admitted project skills as bodies through
`load_skill(..., project_dir)` and reserve path summaries for unconfined skills. The
trigger split and pointer-hint renderer enforce the same rule later in a turn. This
prevents a checkout from replacing an already-enumerated `SKILL.md` with an escaping link
and persuading the agent to reopen it directly after the descriptor-confined read. Session
start and post-compaction callers also pass the skills section cap as a confined-body
budget even when lazy loading is off. Bodies that fit are injected whole; bodies that do
not fit are omitted rather than exposed as unsafe paths. The loader checks the enumerated
size before opening and passes the remaining budget into the descriptor-pinned read, so a
replacement race or many large project skills cannot materialize more body text than the
section can retain.

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

**`search_skills` stays project-blind.** Only a session key reaches that boundary and
resolving a project from it needs a seam that does not exist yet, so the MCP tool
continues to search locally installed skills only.

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

The bundled `kirocrew-dev/babysit` skill is an on-demand, pointer-on-trigger recipe.
Its explicit trigger vocabulary covers babysit/watch/monitor phrasing for pull
requests, so ordinary requests reach the recipe without placing the whole body in
every prompt. The base prompt points long-lived pull-request readiness requests to
this skill and prefers the structured path whenever typed provider facts fully
determine the objective.
For a public GitHub pull request with the `review_ready` objective it gives the
agent one exact bounded `monitor_watch` call and makes retained inspection state
authoritative; its acknowledgement remains pending until the current turn ends,
so agent inspection happens only at the start of a later user/wake turn. It also
explains that reported-token enforcement may be incomplete while runtime and
completed-turn limits remain hard fallbacks. It does not reproduce provider
polling policy in the prompt. Its legacy `monitor_start` recipe is limited to
unsupported targets and requires a positive cadence, cycle cap, and runtime
bound while naming the full-turn/token cost and ordinary approval policy.

**Loading:**
1. **Always-on**: skills with `always: true` have full content injected every new session
2. **On-demand**: skill summaries (name + description + dir path) in session context; LLM can `cat` the file when relevant

Skills with auxiliary files (scripts, assets) include `dir` path so the LLM can `cd` and run them.

**Lazy-load (`skills.lazy_load`, default false — loader `SkillsConfig`):** controls how `get_context(budget)` (`skills.py`) injects the on-demand set.
- **OFF** (`get_context(budget=None)`): the legacy global-skill dump — every unconfined on-demand skill summarized, unranked and untruncated, under the flat 165k `_CONTEXT_BUDGET_BASE`; confined project bodies retain their independent skills-section cap.
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

**The writer is bound by the reader's dialect, not by YAML.** `SKILL_LOADER` strips
quote characters and resolves bare `|` / `>` block scalars, and does nothing else --
no unescaping, no explicit indentation indicators. So a managed value is only ever
emitted in a form that dialect decodes: a plain or quoted scalar with no backslash
escape, or a bare block scalar. A value whose OWN TEXT begins or ends with a quote
character also goes to a block scalar: the reader unquotes with `value.strip("\"'")`,
which cannot tell a wrapping quote from one belonging to the text, so
`description: Runs "build"` would read back as `Runs "build`. That rule tests the value,
not the rendered line -- a correctly wrapper-quoted scalar begins and ends with a quote
by construction, and routing those to a block scalar costs a value its leading
whitespace for nothing. A value whose first line begins with whitespace would
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

**Dashboard endpoints**: GET/POST `/api/skills`, GET/PUT/DELETE `/api/skills/{name:.+}`. POST sanitizes name to lowercase + hyphens + slashes. The two open-standard territories are read-only through this endpoint (`READONLY_SKILL_KEY_PREFIXES` in `handlers/prompts.py`): PUT or DELETE on a `kiro-user/` or `kiro-workspace/` key answers 405 with `Allow: GET` and `code: readonly_skill_prefix`, and a POST whose *sanitized* name lands in either territory answers 400 with `code: reserved_skill_prefix`. Those keys resolve per-machine / per-session on read (`_resolve_skill_root`) while `create/update/delete_skill` join the key onto the core skills root, so a write would edit a different file than the reader was shown; GET is unaffected. GET `/api/skills` discovery (kirocrew `list_skills()` os.walk + frontmatter, `list_kiro_skills`, and the skill→agent annotation) is fully offloaded to the dedicated `discovery_executor` pool (`executors.py`) via `collect_skills_blocking`, so it never stalls the event loop past the loop-stall watchdog on large catalogs. The annotation is O(agents) — `annotate_skills_with_agents` parses the agent JSONs and pre-expands each agent's `skill://` globs once, then matches every skill against that in-memory set. The discovery pool is deliberately separate from the reaper-critical `maintenance_executor` so browser-triggered scans can't starve the orphan sweep. When `?agent=<name>` names an agent whose `skill://` globs are non-empty (the filter is actually applied), the response is the envelope `{"skills": [...], "agent_scoped": true, "agent": <name>}` instead of the bare array; every unscoped path keeps the bare-array shape (#6028 — see the fuller rationale in learn-cron-dashboard.md's Skills CRUD entry).

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

**sync_to_agent_config()**: delegates entirely to `install_agent()` — the single authoritative merge that reads all source files, resolves commands, normalizes each spec's `env` through `env.emit_env()` (a declared `PATH` is expanded to the full effective one), and atomically writes the agent config. There is deliberately no `kiro-cli mcp add` subprocess: it was an unsynchronized second writer of the same file whose output the rebuild overwrote moments later.

**sync_discovered_servers()**: the one serialized discover→write entry point (`discover` + agent-config rebuild + Claude Code sidecar) shared by `POST /api/mcp/sync` and the sessions-restart pre-sync. A module mutex serializes concurrent callers, closing the read-modify-write race the two handlers used to have.

**On-demand discovery** (dashboard): `sync_discovered_servers()` triggered by "Discover & Sync" button.

**Command divergence** (`_commands_diverged`): an existing server is only re-synced when its `mcp.json` command differs from the one recorded in the agent config. The two legitimately differ in spelling because `agent._resolve_command` stores the `shutil.which` result while `mcp.json` keeps the bare name, so the comparison folds path resolution:

- A basename match is only accepted when one side is a **rooted path** and the other a **bare name** (no separator), since PATH lookup is what produced the rooted form. Two distinct rooted paths sharing a basename (`/opt/a/srv` vs `/opt/b/srv`) and a CWD-relative path (`bin/srv` vs `/usr/bin/srv`) each name a specific different file, so both stay divergent.
- On Windows the keys are `normcase`+`normpath` folded (paths are case-insensitive and accept either separator), and a trailing `PATHEXT` suffix is stripped from the **rooted side only** — `shutil.which("npx")` returns `...\npx.CMD`, which would otherwise read as divergent from `npx` on every cycle and re-sync + reset every session at each startup. Stripping both sides would wrongly collapse distinct executables (`foo.bat` vs `foo.cmd`).
- A leading separator with no drive letter (`/usr/bin/srv`) counts as rooted on Windows even though `ntpath.isabs` rejects it, so an `mcp.json` authored on macOS/Linux is read identically on every host.

**Probing**: spawns each MCP server, sends JSON-RPC `initialize` + `tools/list` handshake, reports status + tool names. **Both calls must succeed for `ok`** — an initialize that answers and a tools/list that does not is a server no session can get a tool out of, so it reports as an error rather than certifying an unusable server. Each result carries `probedAt` (wall-clock) and `probeMode` (`handshake`, or `declared` for a managed server served from its in-process declaration) so the UI can say when and how the status was established. 30-second timeout, 1MB stdout buffer (an MCP server's responses exceed the default 64KB). Cleanup via `finally` block (no zombie processes). Results cached in `handlers.py` with 10-min TTL; GET `/api/mcp/probe` returns cached results non-blocking, POST `/api/mcp/probe` forces a fresh probe and updates cache.

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
collision, and KiroCrew-managed servers (including `kirocrew-core` and
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
unscoped semantic-write API. The form never renders for a named or private store.
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
| Daily history (V1 session context) | `_MEMORY_HISTORY_CAP` | 16% | 26,400 | truncated; V2 prompt construction does not read daily history |
| Skills | `_SKILLS_CAP` | 15% | 24,750 | top-K under `lazy_load`; tail behind `skill_search` |
| Steering | `_STEERING_CAP` | 10% | 16,500 | truncated with a marker |
| Semantic memory (V1 session context) | `_SEMANTIC_MEMORY_CAP` | 7.7% | 12,705 | bounded query-ranked context; V2 recall uses its own total response cap |
| Episodic memory (V1 session context) | `_EPISODIC_MEMORY_CAP` | 7.7% | 12,705 | capped further at 3,000 chars; V2 recall uses its own total response cap |
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

- **Every char cap scales:** lessons, skills, steering, static anchors, compressed-history, fallback history and `caps.per_message` scale together. The per-message cap is additionally clamped to the available history budget. Legacy history/semantic/episodic cap fields remain available to explicit readers; they do not cause memory search during message construction. The dashboard's `build_session_replay` budget scales by the same factor.
- **Reference identity:** at the reference window the scale factor is exactly 1.0, so resolved caps are byte-for-byte the module constants — the caps are derived *from* those constants (single source of the fractions), not a re-listing.
- **Fail-safe fallbacks (`resolve_model_window(model)`):** delegates to the central `model_registry.model_window(model)` authority (kiro-list cache > registry > supplementary id map > `[1m]` heuristic > `None`). `""`/`None`/`"auto"` and any genuinely-unknown id resolve to `None` ⇒ the 1M reference — so ONLY a model with a confidently-known smaller window scales the budget down; an unknown/auto window never silently shrinks the default deployment (`provider=acp` + `model="auto"` runs a 1M model). The central authority returns `None` (not a silent 200K) for unknown ids, so this fail-safe is now the authority's own contract rather than a special case here. **A context window is a property of the model, not the serving provider** — so `resolve_model_window` takes NO provider arg and `model_window` is provider-independent.
- **Floor:** `_MIN_CONTEXT_BUDGET_BASE` (20% of base ≈ the 200K tier) clamps a pathologically small/misreported window so caps can't collapse to ~0. Known limitation: below 200K every window collapses to this same floored base (forward-compat only — the registry's smallest real window is 200K), and the **fixed preamble** (`_CRITICAL_RULES` + identity/workspace/date, ~3k chars) does NOT scale, so on a small window it consumes a larger *fixed* fraction than the linear model implies. Linear scaling is intentional per the design (window-share parity); a reserve-fixed-overhead curve is a possible future refinement.
- **Callers:** dashboard (`chat_runner`), Slack (`handler`), and subagents (`subagent`) all resolve the window from the live session client via `window_for_provider_client(client)` — which prefers the provider's public `context_window_tokens()` accessor (0 until a turn completes; at `is_new` it falls through) and otherwise derives from the resolved model id via `resolve_model_window`. Background/cron paths that don't resolve a model pass `None` (reference). See `context.py` `_resolve_caps` / `resolve_model_window` / `window_for_provider_client` and the central `model_registry.model_window()` / `has_known_window()`.

### Switchable context groups (sub-agents)

A spawning parent decides which of three groups its sub-agent inherits, via `include_memory` / `include_lessons` / `include_project` on `spawn_run` and `spawn_sub_agents`. All default to `true`, so a caller that passes nothing produces byte-identical context: `build_session_context(context_groups=None)` — what every non-sub-agent caller passes — and an all-on `frozenset` are equivalent by construction.

| Group | Sections | Switchable |
|---|---|---|
| conduct | `_CRITICAL_RULES`, `[CURRENT DATE]`, agent identity + `[RUNTIME]`, UI language, `[WORKSPACE IDENTITY]`, skills index | no |
| `memory` | static preferences/projects, memory tool guidance, `## Recent Session Context` | yes |
| `lessons` | `[Learned corrections]` (global + workspace), `[USER PROFILE]` | yes |
| `project` | `[DOCUMENTATION]` pointer, steering resources (CC backend only), `[PROJECT]` directory line | yes |

The steering row carries a backend caveat: the steering block is injected only on the Claude Code backend (`is_cc`), because on the ACP/kiro backend `kiro-cli --agent` loads the agent's own `resources` natively. `include_project=false` therefore suppresses steering on CC only — an ACP sub-agent still receives it, and nothing in Kiro Crew can prevent that from this call site.

conduct is not switchable because every member is an output contract or a capability pointer: a sub-agent without the skills index cannot discover what it can do, and one without `_CRITICAL_RULES` cannot format what it reports back.

Omitting a group **skips its sections** rather than capping them to zero — `MemoryStore.get_context()`'s `_cap(text, 0)` returns a `…[truncated]` marker, not an empty string, so a zero cap emits headers with no content behind them.

A sub-agent that had a group withheld is told so by name (`[CONTEXT SCOPE]`, built by `_build_context_scope_section`), so it reports the gap instead of inventing what it cannot see. That is what makes an aggressive opt-out recoverable: a wrong `false` surfaces as a question rather than a fabrication.

The flags resolve once at spawn and live on `SubagentInfo`. Every path that re-materializes a run from stored fields carries them — the stagger queue entry and `POST /api/spawn/{agent_id}/retry` — so a queued or retried run sees the scope its caller chose. `spawn_continue` does not accept the flags but **inherits** them (`_inherited_context_groups`): a continuation rebuilds session context, because `get_or_create` returns `is_new=True` even when it restores the session via `session/load` (`resumed` is a separate flag and gates only thread history), so an un-inherited continuation would silently regain a withheld group. The live record wins; the run's persisted `context_groups` is the fallback, and a run predating the field records no scope at all — distinguishable from "all withheld" and defaulting to all-on. `GET /api/spawn` reports `context_withheld` only when something was withheld, and `_run_inner` logs the resolved set with the resulting context length.

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

The owner copy dialog names its destination member. Recovery distinguishes
**Restore experience** from whole-store **Restore backup**. A dirty store switch
offers **Keep editing** and **Discard changes**; keeping the draft leaves the
original member and document mounted. Old initialization-error metadata is
displayed as historical diagnostic text and does not suppress retry of a valid
V1 conversation.
