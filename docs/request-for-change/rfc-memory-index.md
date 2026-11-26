---
title: Memory index -- a pluggable vector and word index so recall and memory search stop scanning every row
status: in-progress
author: buluoray, with kirocrew-worker
created: 2026-11-26
last-audited: 2026-11-26
audited-at: 39a999654fd
doc-pr: 42916
implementation-prs: [42927]
tracking-issues: [42910]
supersedes: []
superseded-by: []
---

# RFC: Memory index

- Status: in-progress. Phases 1 and 2 are in [#42927](https://github.com/kirodotdev/KiroCrew/pull/42927), behind `memory.index.mode` (default `off`).
- Tracking issue: [#42910](https://github.com/kirodotdev/KiroCrew/issues/42910).
- Code read at `a720d40f925`. Every number below names how it was measured
  (section 2). All of them were taken at host load 29-38 on 32 cores, so treat
  absolute latencies as upper bounds.

## 1. Summary

Every memory read scans the whole store. Recall takes about 1 s on a
15-23k-row store and about 6.5 s at 100k, and busy bot stores grow by
1,000-1,900 rows a day. The vector math is not the cost: a full f32 mat-vec is
1.1 ms at 23k and 9.9 ms at 100k. The time goes to reading every row out of
SQLite and tokenizing it in Python on every query.

This RFC adds a **memory index**:

- **`MemoryIndex`**, an interface with a backend registry
  (`memory.index.backend`). Recall, the Memory page searches and the graph
  search call only this interface.
- **A local default backend.** usearch (HNSW) stores the vectors. SQLite FTS5
  stores the words: `trigram` for substring search, which also covers Chinese,
  and a term table for recall's keyword rule. Stores below a row threshold are
  scanned exactly.
- **Correctness in core, the same for every backend.** SQLite triggers write a
  change log. A live-id filter at query time means a deleted memory never comes
  back. Builds run aside and are swapped in. Rollout goes `off` → `shadow` → `on`.
- **Plug-in points** for pgvector, OpenSearch and S3 Vectors, plus a contract
  test that any backend must pass. No cloud backend is built here.

## 2. Measurements

Stores:
- a read-only SQLite backup copy of one real V1 store: 23,010 live rows with
  vectors, 13,010 facts and 10,000 episodes;
- synthetic V2 and V1 stores built by cloning real rows, at 23k and 100k rows.

The embedder is the bundled 1024-dimension model. FAISS was not installed, so
every vector path measured "today" is the numpy fallback.

### 2.1 Today

Recall is `VectorMemoryStore.recall(q, cap=3000)`, wall clock, embedding the
query included:

| | V2 15k (real) | V2 23k | V2 100k | V1 23k (real) | V1 100k |
|---|---|---|---|---|---|
| warm recall | 0.95-1.07 s | 1.17-1.50 s | 5.9-6.6 s | 0.66-0.84 s | 6.1-6.4 s |
| store RSS added | 131 MB | 183 MB | 440 MB | 326 MB | 896 MB |

A profiled 100k V2 recall took 12.6 s. Of that, `memory_v2.terms` and
`lexical_evidence` took 5.7-6.1 s cumulative, and the numpy cosine took 1.3 s.

### 2.2 Vector libraries

Package metadata was read from PyPI on 2026-11-26:

| | usearch 2.26.6 | hnswlib 0.8.0 | sqlite-vec 0.1.9 | faiss-cpu 1.16.0 |
|---|---|---|---|---|
| License | Apache-2.0 | Apache-2.0 | MIT / Apache-2.0 | MIT |
| Wheels: Linux x86-64 / arm64 | yes / yes (manylinux 2.28, musl) | **none, sdist only** | yes / yes | yes / yes |
| Wheels: macOS x86-64 / arm64 | yes / yes (10.9+, 11.0+) | none | yes / yes | yes / yes, **macOS 15+ only** |
| Wheels: Windows x86-64 / arm64 | yes / yes | none | yes / **no arm64** | yes / yes |
| Wheel size | 0.3-2.4 MB | builds from source | 0.1-0.3 MB | 5.3-20.2 MB |
| Releases in the last 12 months | 12 | 0 (last 2023-12) | 12 | 7 |
| Approximate search | HNSW; add and remove; memory-mapped `view`; f16 and i8 storage | HNSW | **none: `vec0` KNN scans every row** | IVF, HNSW |
| Extra runtime dependencies | `numkong` (Apache-2.0), `tqdm`, `numpy` | | | `numpy` |

- **hnswlib** is ruled out. It ships no wheel, so every install would need a C++
  compiler, and it has had no release since 2023.
- **sqlite-vec** is ruled out. Its KNN is exact, which is the scan this RFC
  removes.
- **faiss-cpu** would work. It costs 5-20 MB wheels and needs macOS 15 on a Mac.
- **usearch** is chosen for the optional `memory-index` extra
  (`usearch>=2.26.6,<3`), not `install_requires`: its Linux wheels need glibc
  2.28+, while core installs retain their manylinux2014 wheel closure. Its
  installed size is 11 MB, plus 46 MB for `numkong` and 0.6 MB for `tqdm`.
  With the `local` backend, `on` and `shadow` need this extra. Without it the
  backend raises `MemoryIndexError` with install advice naming usearch itself;
  `StoreIndex` records the failure, logs one WARNING per instance and recall
  keeps scanning.

**usearch quality on real vectors.** The 200 queries are held-out real memory
vectors, searched against the rest of the real store. The 100k corpus is the
23k real rows plus 77k jittered copies of them. M = 16, f16 storage, 4 build
threads:

| n | build | file | query, k = 64 | top-10 vs exact | top-64 vs exact | query, k = 2048 |
|---|---|---|---|---|---|---|
| 23k | 2.1-2.8 s | 48 MB | 0.9-2.1 ms | 0.997-1.000 | 0.999-1.000 | 4.3 ms |
| 100k | 14-23 s | 209 MB | 1.5-3.1 ms | 0.995-0.997 | 0.999 | 10.1 ms |

- i8 storage halves the file (26 MB at 23k), but top-10 falls to 0.946.
- A hand-written numpy IVF (k-means lists) reached top-10 0.96 while scanning
  13% of the rows. It was dropped in favour of the library.

### 2.3 Words

These are SQLite FTS5 tables on the same real store. "Same rows" compares the
hit count with today's substring scan over the NFKC-casefolded text:

| Query | Path | Hits | Index | Scan today | Same rows |
|---|---|---|---|---|---|
| `rebase`, `worktree`, `4179` | trigram | 1824, 2060, 1 | 0.2-1.2 ms | 17-21 ms | yes |
| `memory recall` (two words, AND) | trigram | 34 | 0.8 ms | 15 ms | yes |
| `工作树`, `自动合并` | trigram | 3, 5 | 0.1 ms | 14-16 ms | yes |
| `slack 线程`, `KAS 回归` | trigram on the long word, then `instr` on its hits | 0, 0 | 0.6-1.5 ms | 18-22 ms | yes |
| `测试`, `合并`, `回归` (two Chinese characters) | capped `instr` scan in the prototype | 78, 68, 6 | 23-30 ms | 15-17 ms | yes, but no faster; section 4.3 moves these to the term table |
| `我`, `a`, `QA` | capped `instr` scan | 166, 2001 (cap), 102 | 1-28 ms | 10-27 ms | yes |

- At 92k rows (four copies), trigram `rebase` takes 3.2 ms against a 71 ms
  scan, and `工作树` takes 0.1 ms against 47 ms.
- The contentless trigram table is 42.5 MB at 23k and about 175 MB at 92k.
- The term table (`detail=none`) is 2.1 MB at 23k and 7.5 MB at 92k. Its build
  takes 1.8 s at 23k and 7.4 s at 92k.
- Recall's keyword rule needs only the candidate set described in section 4.3.
  At 23k that set is 0.8-17% of the rows, and the lookup takes 0.1-2.1 ms.

## 3. Motivation

What scans every row today:

| Path | Today, per query |
|---|---|
| V2 fact recall (`vector_memory_runtime/semantic.py` `semantic_candidates_v2`) | `SELECT *` of every live fact with its blob, then `visible_json` and `memory_v2.terms()` on each row |
| V2 episode recall (`vector_memory_runtime/episodic_search.py` `search_episodic_v2`) | the same, over every live episode |
| V1 facts, lessons and episodes | resident `_SemanticScoringSet` and `_EpisodicScoringSet`: one mat-vec and one `numpy.isin` over all rows |
| Memory page search by meaning | reads every stored vector and scores each one |
| Memory page word search (`memory_edit._rows`) | substring match over the normalized text of every row |
| Memory graph search (`dashboard/handlers/memory_graph.py`) | regex per row |

The existing FAISS accelerator (`vector_memory_runtime/faiss_index.py`) is
optional, covers V1 episodes only, and uses `IndexFlatIP`, which is itself a
full scan.

## 4. Design

### 4.1 Layers

```
recall / Memory page searches / graph search
        |  call only this
        v
IndexedReader   core: change log, overlay, live-id filter, shadow compare
        |
        v
MemoryIndex     interface; backend chosen by memory.index.backend
   |-- local   (default) usearch vectors + FTS5 words; exact scan under a threshold
   |-- exact   scan only: today's behaviour, and the reference in tests
   `-- others  hosted backends (section 4.6); public registration waits for the first one
```

A backend only generates candidates. Every guarantee in section 4.4 lives in
`IndexedReader`, which is the same code for every backend.

### 4.2 The `MemoryIndex` interface

Module `kiro_crew/memory_index/contract.py`, a `typing.Protocol`:

```python
class MemoryIndex(Protocol):
    name: ClassVar[str]                     # registry key, e.g. "local"
    capabilities: ClassVar[frozenset[str]]  # subset of {"vector", "terms", "substring", "filters"}

    def open(self, store: StoreRef, space: EmbeddingSpace, config: Mapping[str, Any]) -> None: ...
    def upsert(self, docs: Sequence[IndexDoc], *, seq: int) -> None: ...    # idempotent per ident
    def remove(self, idents: Sequence[Ident], *, seq: int) -> None: ...     # idempotent
    def search(self, query: IndexQuery) -> list[Hit]: ...        # candidates, best first
    def rebuild(self, source: Iterable[IndexDoc], *, build_seq: int) -> BuildHandle: ...
    def swap(self, handle: BuildHandle) -> None: ...             # atomic replace
    def status(self) -> IndexStatus: ...
    def close(self) -> None: ...

@dataclass(frozen=True)
class IndexDoc:
    ident: Ident              # ("items", memory_items.id) on V2;
                              # ("semantic", key) or ("episodic", id) on V1
    kind: str                 # fact | directive | episode
    vector: bytes | None      # f32, L2-normalized by core before the call
    text: str                 # NFKC-casefolded search text
    terms: str                # " ".join(sorted(memory_v2.terms(text)))
    filters: Mapping[str, str]  # kind, scope, crew, surface

@dataclass(frozen=True)
class IndexQuery:
    mode: Literal["vector", "terms", "substring"]
    vector: bytes | None
    text: str | None
    k: int
    filters: Mapping[str, str]

@dataclass(frozen=True)
class IndexStatus:
    backend: str
    state: Literal["ready", "building", "missing", "stale", "failed"]
    built_seq: int            # change-log seq the last build covered
    applied_seq: int          # highest seq upsert/remove applied; swap resets it to build_seq
    rows: int
    space_signature: str      # embedding_space_signature(model_id, dim) at build
    format_version: int
    detail: str               # plain reason when not ready
```

The registry, `memory_index/registry.py`, names the built-in backends (`exact`,
`local`). Phase 2 ships no public `register_index_backend`: the public hook waits
for the first real second backend, so a caller shapes its contract.

| Config key | Default | Meaning |
|---|---|---|
| `memory.index.mode` | `off` | `off`: today's scans. `shadow`: build the index, answer from the scan, and count differences. `on`: answer from the index |
| `memory.index.backend` | `local` | registry key |
| `memory.index.exact_rows` | `20000` | below this many live vectors, `local` scans exactly |
| `memory.index.<backend>.*` | - | backend settings. Secrets are SecretVault names, never values |

The default moves from `off` to `on` only after `shadow` meets the bar in
section 4.5 on real stores.

### 4.3 The local backend

**Vectors.** usearch HNSW: cosine metric, M = 16, `i8` scalars, saved to
`vectors.usearch`.

- Recall re-scores every candidate from the store's own float32 vector, so the
  index's `i8` scalars only choose candidates. On the 23k store `i8` takes
  25.7 MB against 48.2 MB for f16; M = 8 saves only 1.4 MB more and loses
  quality, so M stays 16.
- Below `memory.index.exact_rows` live vectors the search compares every vector
  (usearch `exact=True`). Most member stores are this small.

**Words.** FTS5 in one sidecar file, `words.db`. FTS5 is already required by the
knowledge store and probed by `_sqlite_compat`.

| Table | Tokenizer | Serves | Phase |
|---|---|---|---|
| `words_terms` (`detail=none`, `columnsize=0`) over a term-string column | `ascii` keeps each `memory_v2.terms()` token whole, including non-ASCII marks | recall's keyword rule, and 2-character Chinese queries | 2 |
| `words_tri` (contentless) | `trigram remove_diacritics 0` over the NFKC-casefolded text | Memory page word search and graph search, for words of 3+ characters. This is an exact substring match in any language, so Chinese works and today's any-part matching stays | with the Memory page wiring: recall does not use it, and it doubles `words.db` |

`words.db` stores no memory text. Recall reads rows from the store.

How recall finds keyword candidates without a scan:
- The rule admits a row that matches `k = max(min(2, |Q|), ceil(|Q| / 2))` of
  the query's terms.
- Such a row must hold at least one of the `|Q| - k + 1` rarest query terms.
  Document counts come from an `fts5vocab` table.
- So one `MATCH` on those rarest terms returns every row the rule can admit.
  `lexical_evidence` then checks each candidate against its stored term string,
  exactly as today.

Queries too short for the trigram index:

| Query | Path | Bound |
|---|---|---|
| 2 CJK characters | `words_terms` lookup. `memory_v2.terms()` stores every adjacent CJK pair, so this is exact | exact |
| 1 CJK character, or 1-2 Latin letters or digits | `instr` over the newest 20,000 rows (`updated_at` order, `LIMIT 2001`) | bounded. The page says the match covers the newest 20,000 memories, and that 3 or more characters searches all of them |
| long and short words mixed | trigram on the long words, then `instr` on their hits | bounded by the trigram hits |

### 4.4 Correctness in core

**Change log.** It lives in the store's own SQLite file, so every writer feeds
it: other processes, the CLI, cron, subagents, older Kiro Crew versions and bulk
edit.

```sql
CREATE TABLE IF NOT EXISTS memory_index_log (
    seq   INTEGER PRIMARY KEY AUTOINCREMENT,
    rel   TEXT NOT NULL,   -- 'items' (V2) | 'semantic' | 'episodic' (V1)
    ident TEXT NOT NULL    -- the row's id or key, never its value
);
-- AFTER INSERT, AFTER DELETE, and AFTER UPDATE OF
--   key, value_json, text, embedding, is_deleted, kind, tags, scope, crew
-- on memory_items (V2) and on semantic_memory / episodic_memories (V1):
--   INSERT INTO memory_index_log (rel, ident) VALUES (<rel>, NEW.<id>)   -- OLD.<id> on delete
```

- It is keyed by the natural id, not the rowid, because `VACUUM` can renumber
  the rowids of a table without an `INTEGER PRIMARY KEY`.
- An update to `last_accessed_at` alone does not log.
- The trigger only inserts into its own table. That is the shape
  `snapshot_redact._refuse_update_triggers_that_destroy_rows` allows, and a
  test pins it.
- `MAX(seq)` is the store's **change counter**: one cheap read that also sees
  writes from other processes. The indexes use it as their freshness token, and
  so can other caches, such as the Memory page counts.
- The maintainer trims log rows below the lowest seq that any index still
  needs. With no maintainer running (after a downgrade), the log grows by about
  40 bytes per write.
- It is a schema migration in `MIGRATIONS_CREW` and in the V1 `_MIGRATIONS`.

**Query flow** in `IndexedReader`:

1. **Overlay.** Read `MAX(seq)`. Log entries above the backend's `applied_seq`
   form the overlay. Their idents are masked out of the backend's hits, and
   their live rows are read from SQLite and scored exactly. The cost grows with
   the number of changes, not with the store.
2. **Candidates.** Backend hits, plus overlay rows, plus `pref.*` facts. The
   prefs come from a key range scan, `key >= 'pref.' AND key < 'pref/'`, which
   uses `idx_mi_key_live`.
3. **Live-id filter.** Only the candidate rows are fetched:
   `SELECT ... WHERE id IN (...) AND is_deleted = 0`, plus the lineage guard.
   No backend ever returns a row by itself, so a deleted memory cannot come back
   from any backend.
4. **Ranking.** Today's admission and ranking code runs unchanged on those rows,
   scoring each from its stored float32 vector, so the index's scalars never
   decide an admission (section 4.5 says why the candidates are complete).
5. **Shadow.** In `shadow`, the exact path also runs and its answer is used.
   Differences in the first N rows are counted and shown in `kirocrew doctor`.

**Build, then swap.**

| Event | Action |
|---|---|
| Write in this process | after commit, its ident joins the overlay. The maintainer calls `upsert` / `remove` in batches, off the event loop |
| Write from another process | seen through the log at the next query |
| Overlay over max(2,000, 5% of rows); wrong format or space signature; digest mismatch; `built_seq` above `MAX(seq)` (after a restore); log trimmed past `applied_seq` | the index is `stale`: queries take the exact path and a rebuild is scheduled |
| Live backend or `exact_rows` setting changes | close and replace the `StoreIndex` under the store's lock; reopen the saved generation with the new settings. A mode-only switch between `on` and `shadow` reuses the instance |
| Rebuild | a spawned child, the same shape as `memory_reconcile_child.py` (`nice 10`, 2 BLAS threads, one build per gateway, never on the event loop), writes a new generation directory; the owning process adopts it and `swap()` repoints `current` with one rename. Log entries newer than `build_seq` are applied next. Measured: a build on a thread of the recalling process made a recall beside it 2.2x (23k) to 2.9x (100k) slower, because tokenizing holds the interpreter lock; beside a child a recall costs what it costs with no build |
| Log recreated (the index was off for a while) | the index rebuilds: writes made while no log existed reached no index |

**Embedding space.** The embedding provider is already pluggable
(`memory.embedding_provider`, `register_embedding_backend`).
- Every build records `embedding_space_signature(model_id, dim)` and the
  dimension in its manifest.
- On open, `IndexedReader` compares that signature with
  `active_embedding_space_signature()`. When they differ, the vector side is
  `stale` and queries use the exact path. The rebuild runs once
  `reconcile_embedding_space` has finished re-embedding.
- The word tables do not depend on the model, so they keep serving through a
  model change.

### 4.5 The quality bar

- V2 admits a row on its cosine (at least 0.57, the lower of its two floors) or
  on enough shared terms. So recall asks the index for exactly those two sets:
  every vector at or above the floor (less a margin for `i8`), deepening until
  the last hit is under it, and every row holding the keyword rule's term
  count. Then admission, ranking and MMR see the scan's rows at any depth.
- A search that reaches a limit (16,384 vectors, 4,096 term hits) goes back to
  the scan and is counted, rather than answering with a partial set.
- **Bar:** recall's output equals the scan's: overlap 1.000 at N = 64 and
  N = 256. Measured on 23k and 100k stores (search only, English, Chinese and
  mixed queries): facts top-64 and top-256 1.000, episodes top-10 1.000, with
  fact recall 593 → 70 ms at 23k and 2,140 → 241 ms at 100k.
- Word search must return the same rows as today's substring scan.

Counts:
- The search by meaning shows exact rows up to k. Past k, its count reads
  "200+" instead of an exact number.
- Word and filter counts stay exact.

### 4.6 Other backends (documented, not built)

| Backend | Vectors | Words | Ident mapping | Config keys | Auth |
|---|---|---|---|---|---|
| pgvector | `vector(dim)` column, HNSW index, `<=>` cosine | `tsvector`, with `pg_trgm` for substring | table keyed by `(store_id, rel, ident)` | `memory.index.pgvector.dsn_secret`, `.schema`, `.table` | the DSN is a SecretVault name, read through `secrets.vault` at open |
| OpenSearch | `knn_vector` (HNSW) | `match`, with an `ngram` analyzer for CJK and substring | document `_id` = `<store_id>:<rel>:<ident>` | `.endpoint`, `.index_prefix`, `.auth` (`sigv4` or `basic`), `.credential_secret` | SigV4 through the user's AWS profile, or basic auth from the vault; no keys in config |
| S3 Vectors | one vector index per store, cosine | none: it lacks `terms` and `substring`, so `local` serves words | vector key `<rel>:<ident>`, metadata `kind`, `scope`, `crew` | `.bucket`, `.index_prefix`, `.region`, `.profile` | the user's AWS profile and its credential process; no stored keys |

Rules for every non-local backend:
- `store_id` is the store's own `memory_meta` id.
- Memory text leaves the machine only for a backend that declares `terms` or
  `substring`, and only after the user turns that on for that backend.
- A slow or failing backend never fails a turn. `IndexedReader` takes the exact
  path and marks the status `failed`.
- A capability a backend lacks is served by `local`.

**Conformance kit.** `kiro_crew.memory_index.conformance.run_conformance(factory)`
runs the checks every backend must pass, each reported pass, fail or skip (a
capability the backend does not declare). CI runs it on `local` and `exact`, and
a third party runs it on their own backend's factory. It checks that:
- after an upsert, search finds the doc; after a remove, it does not;
- upserting the same ident twice leaves one doc, and removing an unknown ident
  does nothing;
- during `rebuild` + `swap`, a search sees either the old content or the new,
  never a mix;
- `status` reports the `built_seq`, `applied_seq` and `space_signature` it was
  given;
- filters are applied, or `capabilities` leaves out `filters`;
- substring search on English, Chinese and mixed text returns the same rows as
  `exact`;
- top-10 overlap with `exact` is >= 0.95 on a fixed 2,000-row fixture.

### 4.7 Files on disk (local backend)

`<store file>.index/` holds only derived files. Deleting it loses nothing, and
backups and `memory_stores` snapshots skip it.

| File | 23k rows | 100k rows | Source |
|---|---|---|---|
| `vectors.usearch` (`i8`) | 25.7 MB | ~110 MB | measured at 23k |
| `words.db`, `words_terms` + term strings | 11.0 MB | ~50 MB | measured at 23k |
| `memory_index_log` (in the store) | < 1 MB after trim | < 1 MB | estimate |
| **Total, phase 2** | **38 MB** | **164 MB** | measured |
| `words.db`, `words_tri` (with the Memory page) | +34 MB | ~+150 MB | measured at 23k |

The first phase-2 layout (f16 vectors, trigram and stored text) measured 98 MB at
23k and 413 MB at 100k.

RAM: the owning process holds the usearch index in memory (about the size of
`vectors.usearch`, since `upsert` must write to it), and the FTS pages through
SQLite's page cache. In `on` mode, today's resident scoring sets (183 MB at 23k and 440 MB at 100k
per V2 store) are no longer built.

### 4.8 Shipping usearch (decision)

Decided for phase 2:

- **usearch is the optional `memory-index` extra, not a core dependency.** Its
  Linux wheels are `manylinux_2_28` only (glibc 2.28+), with no
  `manylinux_2_17` (manylinux2014) wheel. Core installs keep the manylinux2014
  wheel closure that `test_install_requires_closure.py` checks, so a core
  dependency on usearch would break installs on older glibc hosts.
- **Every installer and the in-app update add the extra by default, best
  effort.** `install.sh`, `install.ps1`, both `cli.sh` branches, the macOS and
  Windows desktop build, and the update's shadow tree
  (`platform/wheel_engine_extras.py`) install `[memory-index]` from wheels only.
  A host with no wheel gets a warning and the install or update carries on.
  Default-on is deliberate: an operator who later sets `memory.index.mode` gets
  the fast path at once, with no install step, and a sealed desktop bundle
  cannot add it later at all. The cost is about 57 MB installed (section 2.2)
  on hosts where the mode stays `off`.
- **`kirocrew doctor` reports a missing extra.** When `memory.index.mode` is
  `on` or `shadow` with the `local` backend and usearch does not import, doctor
  prints one note under Vector Memory, with the install command where pip can
  run.
- **Recall never depends on it.** Without usearch, `local` fails to open, the
  store keeps no change log, and recall runs the exact scan it runs with the
  index `off`.

Installing the extra the first time the mode is switched on, instead of at
install time, is a follow-up; it cannot reach a desktop bundle, which is
read-only.

## 5. Goals and non-goals

Goals:
- No recall, Memory page search or graph search reads every row of a store
  that is over `memory.index.exact_rows`.
- A deleted memory is never returned, whatever the backend.
- The index can be swapped for another backend without touching any caller.

Non-goals:
- A cloud backend.
- Changing V2's admission rule or V1's scoring.
- Embedding backfill, backup compression, the Memory page count cache, usage
  tracking and data-quality reports. Each is a separate change.

## 6. Failure modes

| Failure | Handling |
|---|---|
| usearch or `numkong` fails to import on a platform | `local.open` fails cleanly; recall scans, the change log is stripped, `StoreIndex` records the failure with one WARNING per instance, and doctor says so (section 4.8) |
| The build child crashes or runs out of memory | the old index or the exact path answers. A leftover `.partial` is removed by age, as `STALE_STAGE_SECONDS` does for backups |
| A corrupt index file | the digest mismatches: exact path, then rebuild. Never a user-facing error |
| A backend returns a deleted or unknown id | the live-id filter drops it |
| A remote backend is slow or down | a per-call timeout, then the exact path, and the status shows `failed` |
| The disk fills during a build | the build fails; the exact path continues; doctor says the index is not built because the disk is full |
| An older Kiro Crew opens the store, or nothing trims the log | the triggers keep logging, about 40 B per write, but never past `changelog.MAX_ROWS` (4,096): at the bound a trigger empties the log, leaving a gap that makes the next catch-up rebuild |

## 7. Migration plan

Phases 1 and 2 ship together, so the interface lands with its first caller.

1. **Interface and exact backend.** `MemoryIndex`, the registry, the `exact`
   backend, the conformance kit, and `memory.index.mode` (default `off`).
   - Exit: the kit passes on `exact`. With `mode: off` nothing changes, which
     is pinned by the existing recall tests.
2. **Change log, local backend, V2 recall.** The triggers, `local` (usearch +
   the FTS5 term table), the child-process rebuild, and `shadow` and `on` for V2
   fact and episode recall. Adds `usearch` to the optional `memory-index` extra,
   not to `install_requires`.
   - Exit: the kit passes on `local`. On 23k and 100k stores recall in `on`
     returns the scan's rows (section 4.5) for English, Chinese and mixed
     queries.
3. **Memory page and graph search.** The trigram table, the Memory page's
   search by meaning and by words, and graph search, once the open Memory
   page changes that rewrite those paths land.
   - Exit: word search returns the same rows as today's substring scan.
4. **Default on.** Flip `memory.index.mode` to `on`.
   - Exit: doctor's shadow counters on real stores show the section 4.5 bar
     over one release.

## 8. Backward compatibility and undo

- `memory.index.mode: off` sends every read back to today's code at once.
- A new `index drop [--store] [--schema]` verb under the memory CLI (added in
  phase 2, not registered yet) deletes `*.index/`. With
  `--schema` it also drops the triggers and the log, as a down-migration.
- The index files are derived. Deleting them loses nothing.
- An older version ignores the sidecar. The only trace it sees is the log table
  and its triggers.

## 9. Security considerations

- The log copies row ids and keys, never values. Snapshot redaction handles it
  like any other copied column, and the trigger shape is pinned against
  `snapshot_redact`'s refusal.
- The index directory sits beside its store and is created with the store's
  owner-only mode (`owner_only_files`).
- The build child runs inside the same sandbox as the gateway.
- Remote backends take credentials only by SecretVault name or AWS profile.
  Memory text goes out only to a backend that declares `terms` or `substring`,
  and only after the user turns it on.

## 10. Alternatives considered

- **A resident in-process term and vector index for V2, like V1's scoring
  sets.** This still scans every row, in numpy, costs RAM per process, and
  cannot be swapped for a remote store.
- **A hand-written numpy IVF.** It adds no dependency, but measured top-10 was
  0.96 at 13% of rows scanned (6.7 ms at 23k), against usearch's 0.997 or
  better in 0.9-2.1 ms. It would also be ours to maintain.
- **sqlite-vec.** Its KNN is exact.
- **FAISS as the default.** Larger wheels, and macOS 15 only on a Mac.
- **A `unicode61` word index for the page search.** It cannot search Chinese,
  which has no word boundaries, and it loses any-part matching.

## 11. Open questions

- Does `expansion_search` apply to an index restored with `view=True`? The
  measurements show the same results across settings. Phase 2 checks it.
- Trigram size and the Chinese share on a store that holds more Chinese text
  than the measured one, where 4% of rows contain Chinese.
