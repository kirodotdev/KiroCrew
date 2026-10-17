"""The schema: table DDL, the per-open column migrations, search indexes and vector generation.

``_init_schema`` and ``_migrate`` run in the constructor of every writable store; the
migrations are per-column ``ALTER``s and one-time repairs, so an open costs O(schema)
once they have run. The search indexes and the vector-generation triggers are kept
here with the DDL they extend.

A function here whose first parameter is ``self`` is a ``KnowledgeStore`` method:
the class binds it under the same name in ``knowledge/store.py``, so it reads the
store's fields and calls its sibling methods through ``self``.

The ``KnowledgeStore`` fields this module uses. Writes: none. Reads: none; every query
goes through ``self.db``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.knowledge.store_parts.fulltext_index import FTS_INDEX_VERSION
from kiro_crew.knowledge.store_parts.source_rows import _without_sync_status

if TYPE_CHECKING:
    from kiro_crew.knowledge.store import KnowledgeStore


def _init_schema(self: KnowledgeStore):
    self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                uri TEXT UNIQUE NOT NULL,
                properties TEXT DEFAULT '{}',
                last_synced TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                item_type TEXT NOT NULL,
                source_id TEXT REFERENCES sources(id),
                chunk_index INTEGER DEFAULT 0,
                namespace TEXT DEFAULT 'default',
                summary TEXT,
                tags TEXT DEFAULT '[]',
                embedding BLOB,
                status TEXT DEFAULT 'active',
                content_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_items_source_id ON items(source_id);
            CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);

            CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
                title, content, tags, content=items, content_rowid=rowid
            );

            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                description TEXT,
                aliases TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

            CREATE TABLE IF NOT EXISTS entity_relations (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES entities(id),
                target_id TEXT NOT NULL REFERENCES entities(id),
                relation_type TEXT NOT NULL,
                description TEXT,
                weight REAL DEFAULT 1.0,
                source_item_id TEXT REFERENCES items(id),
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_entity_relations_source_id ON entity_relations(source_id);
            CREATE INDEX IF NOT EXISTS idx_entity_relations_target_id ON entity_relations(target_id);

            CREATE TABLE IF NOT EXISTS mentions (
                item_id TEXT NOT NULL REFERENCES items(id),
                entity_id TEXT NOT NULL REFERENCES entities(id),
                context TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (item_id, entity_id)
            );

            CREATE TABLE IF NOT EXISTS source_locations (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL REFERENCES items(id),
                source_id TEXT NOT NULL REFERENCES sources(id),
                chunk_range TEXT,
                section_title TEXT,
                anchor TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (item_id, source_id)
            );

            -- Every reader filters on item_id, and deletion now asks "does another
            -- source still hold this item?" on the same key.
            CREATE INDEX IF NOT EXISTS idx_source_locations_item_id
                ON source_locations(item_id);
            CREATE INDEX IF NOT EXISTS idx_source_locations_source_id
                ON source_locations(source_id);

            CREATE TABLE IF NOT EXISTS ingestion_jobs (
                id TEXT PRIMARY KEY,
                source_id TEXT REFERENCES sources(id),
                status TEXT DEFAULT 'pending',
                items_total INTEGER DEFAULT 0,
                items_processed INTEGER DEFAULT 0,
                items_failed INTEGER DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- ``attempts`` counts CONSECUTIVE non-terminal ingest attempts on a row,
            -- i.e. how many times it has been left in 'scanning'. It is what bounds
            -- crash recovery: every retry re-chunks the file and pays for one model
            -- extraction call per chunk, so a file that never completes has to be
            -- retired rather than retried on every sweep. Reset to 0 by any terminal
            -- write ('done', 'deduped', 'failed').
            -- merged_into_source_id names the SOURCE whose copy of this document
            -- survived a de-duplication collapse. It is deliberately a source id and
            -- never an item id: item_ids must keep meaning "the items this row owns",
            -- because dedup derives a document's hash and embedding from whatever
            -- item_ids points at, and delete authority follows the same list. A row
            -- naming another source's items would therefore be enumerated as a second
            -- document over one physical item set, and collapsing that pair deletes
            -- the surviving copy. The marker records the relationship instead, so
            -- deleting the surviving source can clear it and let this row re-ingest.
            CREATE TABLE IF NOT EXISTS folder_file_state (
                source_id TEXT NOT NULL REFERENCES sources(id),
                file_path TEXT NOT NULL,
                content_hash TEXT,
                text_hash TEXT,
                mtime REAL,
                item_ids TEXT DEFAULT '[]',
                last_seen TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                merged_into_source_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (source_id, file_path)
            );

            -- The real paths an AsciiDoc includer inlined when it was last read
            -- (normcased JSON list), so removing one of them re-reads exactly that
            -- includer. Kept apart from folder_file_state, whose many INSERT OR
            -- REPLACE writers would otherwise each have to carry it; no row means
            -- the file inlined nothing.
            CREATE TABLE IF NOT EXISTS folder_include_targets (
                source_id TEXT NOT NULL REFERENCES sources(id),
                file_path TEXT NOT NULL,
                targets TEXT NOT NULL,
                PRIMARY KEY (source_id, file_path)
            );

            CREATE TABLE IF NOT EXISTS artifact_item_state (
                source_id TEXT NOT NULL REFERENCES sources(id),
                slug TEXT NOT NULL,
                content_hash TEXT,
                item_ids TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL,
                name TEXT,
                status TEXT DEFAULT 'active',
                merged_into_source_id TEXT,
                kind TEXT,
                PRIMARY KEY (source_id, slug)
            );

            -- Per-document item-group tracking for the aggregate "Auto-added"
            -- source the agent writes to, keyed by a stable per-document slug.
            -- Same shape and role as artifact_item_state: it is what lets one
            -- aggregate source hold many independently-replaceable documents,
            -- and what gives de-duplication a per-document unit to act on
            -- instead of the whole source. source_uri is the document's own
            -- REDACTED locator, kept so a search hit can cite the document it
            -- came from rather than the aggregate's control uri (agent://);
            -- NULL on rows written before the column existed.
            CREATE TABLE IF NOT EXISTS agent_item_state (
                source_id TEXT NOT NULL REFERENCES sources(id),
                slug TEXT NOT NULL,
                content_hash TEXT,
                item_ids TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL,
                name TEXT,
                status TEXT DEFAULT 'active',
                merged_into_source_id TEXT,
                source_uri TEXT,
                PRIMARY KEY (source_id, slug)
            );

            -- Positive crash-residue evidence for the agent ingest path, held
            -- SEPARATELY from the ownership row above. An ingest records one of
            -- these before it commits any item and deletes it on finalize, so a
            -- row surviving into a drained maintenance window is proof that an
            -- ingest of exactly this content started and never finished. It is a
            -- distinct table, not a status on agent_item_state, for two reasons
            -- the single-row design cannot meet: a re-add that REPLACES a live
            -- group has an active ownership row for its slug at the same time as
            -- its own in-flight intent, and two interrupted attempts at one slug
            -- (a crash, then an edited retry that also crashes) must each leave
            -- their own evidence rather than overwrite the first. Keying on
            -- content_hash as well as (source_id, slug) lets both coexist, so
            -- the sweep can reap EITHER crashed attempt by its own hash.
            CREATE TABLE IF NOT EXISTS agent_ingest_intent (
                source_id TEXT NOT NULL REFERENCES sources(id),
                slug TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                started_at TEXT NOT NULL,
                PRIMARY KEY (source_id, slug, content_hash)
            );

            -- Tombstones for auto-discovered sources the user deleted. Keyed by
            -- URI (not source_id) and deliberately NOT touched by
            -- delete_source_cascade: auto-discovery's only idempotency marker is
            -- the source row, so without a tombstone that survives deletion a
            -- deleted auto-source would be re-created (and re-ingested) on the
            -- next watcher sweep while the folder still exists on disk.
            CREATE TABLE IF NOT EXISTS dismissed_auto_sources (
                uri TEXT PRIMARY KEY,
                dismissed_at TEXT NOT NULL
            );

        """)
    self.db.commit()


def _ensure_search_indexes_if_empty(self: KnowledgeStore) -> None:
    """Build the search indexes now when the library is empty (after ``_migrate``).

    An empty library gets them for free. A populated one does not: building
    them is O(data) (seconds on a cold 59k-entity library) and this runs
    before the gateway listener binds. The gateway builds them post-bind via
    ensure_search_indexes(); until then a search returns the same rows, only
    slower. Every indexed table must be empty: entities can outlive their
    mentions until the orphan sweep runs, and items exist before any entity
    is extracted from them. Runs after ``_migrate`` because an index names
    columns that ``_migrate`` adds to an older table.
    """
    if (
        self.db.execute("SELECT 1 FROM mentions LIMIT 1").fetchone() is None
        and self.db.execute("SELECT 1 FROM entities LIMIT 1").fetchone() is None
        and self.db.execute("SELECT 1 FROM items LIMIT 1").fetchone() is None
    ):
        self.ensure_search_indexes()


#: Indexes that only make knowledge search faster. Results never depend on
#: them, so a store may serve queries before they exist.
_SEARCH_INDEXES = (
    # find_entity's case-insensitive fallback; without it every query term
    # that misses the exact name scans the whole table.
    "CREATE INDEX IF NOT EXISTS idx_entities_name_lower ON entities(LOWER(name))",
    # The graph leg counts mentions by entity_id; the primary key leads with
    # item_id, so without this every search scans every mention.
    "CREATE INDEX IF NOT EXISTS idx_mentions_entity_item ON mentions(entity_id, item_id)",
    # The watcher's stale-embedding count (ingestion.count_stale_items) reads
    # only these columns, so with this index it never touches the item rows.
    # Without it the count read the whole items table at every boot, beside
    # memory preparation, and slowed both.
    "CREATE INDEX IF NOT EXISTS idx_items_embed_state "
    "ON items(status, embedding_sig, embedded_at)",
)


def has_pending_one_time_work(self: KnowledgeStore) -> bool:
    """Whether a search index is missing or the FTS index predates the current format.

    Both are one-time rebuilds that read the whole library; once done this is
    two metadata reads.
    """
    names = {
        row[0] for row in self.db.execute("SELECT name FROM sqlite_schema WHERE type = 'index'")
    }
    wanted = {stmt.split(" IF NOT EXISTS ", 1)[1].split()[0] for stmt in self._SEARCH_INDEXES}
    if not wanted <= names:
        return True
    version = self.db.execute("PRAGMA user_version").fetchone()[0]
    return int(version or 0) < FTS_INDEX_VERSION


def has_embed_state_index(self: KnowledgeStore) -> bool:
    """Whether the stale-embedding count can be answered from the index alone."""
    return (
        self.db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'index' AND name = 'idx_items_embed_state'"
        ).fetchone()
        is not None
    )


def ensure_search_indexes(self: KnowledgeStore) -> None:
    """Create the search-only indexes if missing. O(data) the first time, then a no-op."""
    for stmt in self._SEARCH_INDEXES:
        self.db.execute(stmt)
    self.db.commit()


#: Columns the vector leg's candidate query reads or filters on. A write that
#: touches none of them (a title or content edit, a job row) leaves every
#: cached vector matrix valid.
_VECTOR_ITEM_COLUMNS = "id, embedding, embedding_sig, status, source_id, namespace"


def _ensure_vector_generation(self: KnowledgeStore) -> None:
    """Keep ``vector_generation.gen`` changing with every vector-relevant write.

    The vector leg caches its candidate matrix keyed on this value, read in
    the same snapshot as the rows, so the cache follows the rows that feed it
    rather than the database file: a write to an unrelated table keeps it,
    and a write from any connection or process invalidates it. Each bump
    stores a fresh ``random()`` instead of incrementing, so a library file
    replaced by another (a restored copy, a new empty library) does not
    repeat a value a cache already holds for different rows.
    """
    db = self.db
    bump = "BEGIN UPDATE vector_generation SET gen = random() WHERE id = 1; END"
    triggers = (
        ("vector_gen_items_ins", "AFTER INSERT ON items"),
        ("vector_gen_items_del", "AFTER DELETE ON items"),
        ("vector_gen_items_upd", f"AFTER UPDATE OF {self._VECTOR_ITEM_COLUMNS} ON items"),
        ("vector_gen_locations_ins", "AFTER INSERT ON source_locations"),
        ("vector_gen_locations_del", "AFTER DELETE ON source_locations"),
        ("vector_gen_locations_upd", "AFTER UPDATE OF item_id, source_id ON source_locations"),
    )
    # Every open runs this, and a write here would make each open wait on
    # any writer in another process; once everything exists it only reads.
    present = {
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'vector_generation' "
            "OR (type = 'trigger' AND name LIKE 'vector_gen_%')"
        ).fetchall()
    }
    if (
        len(present) == len(triggers) + 1
        and db.execute("SELECT 1 FROM vector_generation WHERE id = 1").fetchone()
    ):
        return
    db.execute(
        "CREATE TABLE IF NOT EXISTS vector_generation ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), gen INTEGER NOT NULL)"
    )
    db.execute("INSERT OR IGNORE INTO vector_generation (id, gen) VALUES (1, random())")
    for name, event in triggers:
        db.execute(f"CREATE TRIGGER IF NOT EXISTS {name} {event} FOR EACH ROW {bump}")


def vector_generation(self: KnowledgeStore) -> int | None:
    """The current ``vector_generation.gen``, or ``None`` when this library has none.

    A read-only store opened on a file no migrating open has touched yet has
    no generation table; its caller must not cache on it.
    """
    try:
        row = self.db.execute("SELECT gen FROM vector_generation WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else int(row[0])


def _migrate(self: KnowledgeStore):
    """Add columns that may be missing in older databases."""
    cols = {r[1] for r in self.db.execute("PRAGMA table_info(items)").fetchall()}
    if "namespace" not in cols:
        self.db.execute("ALTER TABLE items ADD COLUMN namespace TEXT DEFAULT 'default'")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_items_namespace ON items(namespace)")
    # Embedding provenance: which embed setup produced the stored vector, and when.
    # NULL on existing rows -> treated as stale, re-embedded on the next sig-gated
    # rebuild (manual or watcher self-heal).
    if "embedding_sig" not in cols:
        self.db.execute("ALTER TABLE items ADD COLUMN embedding_sig TEXT")
    if "embedded_at" not in cols:
        self.db.execute("ALTER TABLE items ADD COLUMN embedded_at TEXT")
    # Whole-doc extracted-text hash, the cross-source de-dup key (knowledge/dedup.py).
    # NULL on legacy rows -> they fall back to the fuzzy (embedding) dedup tier.
    if "content_hash" not in cols:
        self.db.execute("ALTER TABLE items ADD COLUMN content_hash TEXT")
    # Index created here (not in the CREATE TABLE DDL) so it runs only after the
    # column is guaranteed to exist: on a pre-existing DB the DDL block's
    # CREATE TABLE IF NOT EXISTS is a no-op and the column is added by the ALTER
    # above; IF NOT EXISTS keeps it idempotent for fresh DBs too.
    self.db.execute("CREATE INDEX IF NOT EXISTS idx_items_content_hash ON items(content_hash)")
    # source_locations predates being an identity table: pre-existing DBs have
    # neither the (item_id, source_id) uniqueness nor any index. De-duplicate
    # first so the unique index can be created, then add both lookup indexes.
    # The de-dup is a full GROUP BY over the table, so it is gated on the
    # unique index NOT existing yet: once the index is in place duplicates
    # are impossible, and the scan would run on every open for nothing.
    has_unique = self.db.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'index' "
        "AND name = 'idx_source_locations_item_source'"
    ).fetchone()
    if has_unique is None:
        self.db.execute("""
                DELETE FROM source_locations WHERE id NOT IN (
                    SELECT MIN(id) FROM source_locations GROUP BY item_id, source_id
                )
            """)
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_source_locations_item_source "
            "ON source_locations(item_id, source_id)"
        )
    self.db.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_locations_item_id ON source_locations(item_id)"
    )
    self.db.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_locations_source_id "
        "ON source_locations(source_id)"
    )
    job_cols = {r[1] for r in self.db.execute("PRAGMA table_info(ingestion_jobs)").fetchall()}
    if "items_failed" not in job_cols:
        self.db.execute("ALTER TABLE ingestion_jobs ADD COLUMN items_failed INTEGER DEFAULT 0")
    self._ensure_vector_generation()
    src_cols = {r[1] for r in self.db.execute("PRAGMA table_info(sources)").fetchall()}
    if "sync_status" not in src_cols:
        self.db.execute("ALTER TABLE sources ADD COLUMN sync_status TEXT DEFAULT 'pending'")
    # ONE pass over the rows that still carry a blob copy of the status:
    # repair the column where it was never written, then retire the copy.
    # After this pass no row has a copy at all, so on a store that has
    # already opened once the scan matches nothing.
    #
    # An INITIAL state is repaired onto a column still at its un-written
    # 'pending' default (rows inserted before the column was written on
    # INSERT). The dashboard picks the row's control from the column, so a
    # divergent row renders Pause instead of Confirm and the source cannot be
    # started. Only 'pending' rows are candidates: any row a handler has
    # transitioned already had its column written, so a repair never
    # overwrites a live state.
    #
    # A LIFECYCLE value in the blob is deliberately NOT promoted, not even
    # 'error'. It cannot be ordered against the column: a pre-column
    # ``_record_failure`` wrote 'error' to the blob alone, and a later
    # successful re-ingest wrote 'synced' to the column alone, so the two
    # copies carry no evidence of which happened last. Promoting would mark a
    # recovered source errored and, since the copy is retired in the same
    # pass, nothing would correct it. Not promoting costs at most ONE sync
    # attempt: ``_record_failure`` reads ``consecutive_failures`` from the
    # blob, which such a row already has at or above its threshold, so the
    # first attempt that fails writes the column and quiesces the source for
    # good -- while an attempt that SUCCEEDS is the right outcome for a source
    # that had recovered. The column is authoritative; a value that cannot be
    # ordered against it does not get to overrule it.
    #
    # The copy is then RETIRED. This runs on EVERY open, so leaving the key in
    # place would make the repair above a standing reader of a value that goes
    # stale the moment a column-only writer moves the row. Retiring makes it a
    # one-time repair instead.
    #
    # The repair is compare-and-set on the row as READ -- the blob AND the
    # column -- so a concurrent writer wins and the row is converged by the
    # next open instead. The retirement predicates on the blob ALONE, which
    # is the only field it writes: a column-only transition is what every
    # live writer does, and requiring the column to be unmoved would abandon
    # the copy for exactly the transitions that are expected to happen.
    # No SQL prefilter on the blob text. A raw substring match cannot decide
    # membership here: JSON escapes are legal inside a KEY, so a blob stored
    # as {"sync_\u0073tatus": "paused"} parses to the very key this pass
    # converges while `properties LIKE '%sync_status%'` never matches it. The
    # key only exists once decoded, so the decision has to be made on the
    # PARSED value. `sources` holds one row per knowledge source, so parsing
    # each one is bounded and cheap -- and after this pass no row carries a
    # copy at all, so later opens parse and skip.
    #
    # Nothing in-tree can write that escaped form any more (`json.dumps`
    # never escapes ASCII, and `_without_sync_status` re-serializes on every
    # insert and update), but a row imported by an early `import_bundle` --
    # which stored properties text verbatim -- can still hold one.
    blob_copies = self.db.execute("SELECT id, properties, sync_status FROM sources").fetchall()
    for row in blob_copies:
        try:
            props = json.loads(row["properties"] or "{}")
        except (ValueError, TypeError, RecursionError):
            # RecursionError (a RuntimeError, so not covered by ValueError):
            # json.loads recurses per nesting level, and this runs on EVERY
            # open, so one pathologically nested legacy blob would otherwise
            # abort every store construction -- a gateway that cannot start.
            continue
        if not isinstance(props, dict) or "sync_status" not in props:
            continue
        copied = props["sync_status"]
        if (
            row["sync_status"] == "pending"
            and isinstance(copied, str)
            and copied != "pending"
            and copied in self._INITIAL_SYNC_STATUSES
        ):
            self.db.execute(
                "UPDATE sources SET sync_status = ? "
                "WHERE id = ? AND sync_status = 'pending' AND properties = ?",
                (copied, row["id"], row["properties"]),
            )
        self.db.execute(
            "UPDATE sources SET properties = ? WHERE id = ? AND properties = ?",
            (_without_sync_status(row["properties"]), row["id"], row["properties"]),
        )
    if "summary_topic" not in src_cols:
        self.db.execute("ALTER TABLE sources ADD COLUMN summary_topic TEXT")
    if "summary_themes" not in src_cols:
        self.db.execute("ALTER TABLE sources ADD COLUMN summary_themes TEXT")
    # Backfill columns on the document-state tables. Each table itself is
    # created by ``_init_schema``, which runs first on every construction, so
    # only the per-column ALTERs belong here.
    ffs_cols = {r[1] for r in self.db.execute("PRAGMA table_info(folder_file_state)").fetchall()}
    if "status" not in ffs_cols:
        self.db.execute("ALTER TABLE folder_file_state ADD COLUMN status TEXT DEFAULT 'pending'")
    if "error_message" not in ffs_cols:
        self.db.execute("ALTER TABLE folder_file_state ADD COLUMN error_message TEXT")
    if "merged_into_source_id" not in ffs_cols:
        self.db.execute("ALTER TABLE folder_file_state ADD COLUMN merged_into_source_id TEXT")
    # The extracted-text hash, in the same domain as items.content_hash --
    # see _OWNERSHIP_HASH_COL. Deliberately NOT backfilled: it can only be
    # derived from a row's own items, and a legacy row that owns nothing has
    # nothing to derive it from. Left NULL, such a row behaves exactly as it
    # does today (its ownership lookups match nothing) and is populated the
    # next time the file is scanned. A backfill that guessed instead would be
    # the data-loss shape this feature already had to remove once.
    if "text_hash" not in ffs_cols:
        self.db.execute("ALTER TABLE folder_file_state ADD COLUMN text_hash TEXT")
    # Consecutive non-terminal attempt count, the bound on crash recovery --
    # see the CREATE TABLE comment. Existing rows start at 0, including any
    # already stuck in 'scanning': that is deliberate, so a database carrying
    # a file that cannot be ingested spends the same small retry budget as a
    # fresh one and then retires the row, instead of re-ingesting it (and
    # paying for its extraction calls) on every sweep for as long as the
    # source exists.
    if "attempts" not in ffs_cols:
        self.db.execute(
            "ALTER TABLE folder_file_state ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
        )
    # artifact_item_state -- per-artifact item-group tracking for the
    # aggregate "Artifacts" KB source, keyed by artifact slug.
    ais_cols = {r[1] for r in self.db.execute("PRAGMA table_info(artifact_item_state)").fetchall()}
    if "name" not in ais_cols:
        self.db.execute("ALTER TABLE artifact_item_state ADD COLUMN name TEXT")
    if "status" not in ais_cols:
        self.db.execute("ALTER TABLE artifact_item_state ADD COLUMN status TEXT DEFAULT 'active'")
    if "merged_into_source_id" not in ais_cols:
        self.db.execute("ALTER TABLE artifact_item_state ADD COLUMN merged_into_source_id TEXT")
    # The artifact kind AS INGESTED. Reconcile needs it to tell an
    # artifact whose kind changed while sync was off (stale chunks, must
    # be reaped) from one the user merely excluded by narrowing
    # `auto_ingest_artifact_kinds` (still live, must NOT be reaped).
    # Legacy rows carry NULL, which reconcile treats as "cannot tell"
    # and leaves alone; the next ingest of that artifact backfills it.
    if "kind" not in ais_cols:
        self.db.execute("ALTER TABLE artifact_item_state ADD COLUMN kind TEXT")
    # agent_item_state -- per-document item-group tracking for the aggregate
    # "Auto-added" KB source the agent writes to.
    agent_cols = {r[1] for r in self.db.execute("PRAGMA table_info(agent_item_state)").fetchall()}
    if "status" not in agent_cols:
        self.db.execute("ALTER TABLE agent_item_state ADD COLUMN status TEXT DEFAULT 'active'")
    if "merged_into_source_id" not in agent_cols:
        self.db.execute("ALTER TABLE agent_item_state ADD COLUMN merged_into_source_id TEXT")
    # The document's own REDACTED locator, attached to agent-source search
    # hits so a citation names where the document came from instead of the
    # aggregate's control uri. Legacy rows carry NULL, which citation
    # enrichment treats as "unknown" and falls back to agent://; the next
    # add of that document backfills it.
    if "source_uri" not in agent_cols:
        self.db.execute("ALTER TABLE agent_item_state ADD COLUMN source_uri TEXT")
    # The orphan sweep is NOT here any more -- see `reclaim_orphans`. The
    # constructor runs on the event loop before the socket binds, and the
    # sweep is data-scaled and writer-locked, so on a large store it
    # stalled boot long enough for runtime timeouts to kill the gateway.
    # `start_dashboard` kicks it from a worker thread once the listener is
    # up (`_kick_knowledge_orphan_reclaim`).
    # Last, because a search index names columns added above.
    self._ensure_search_indexes_if_empty()
