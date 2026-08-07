"""KnowledgeStore -- SQLite backed knowledge graph with lightweight in-memory graph."""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections import defaultdict
from datetime import datetime
from uuid import uuid4

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

logger = logging.getLogger(__name__)


class _NodeView:
    """Minimal node-attribute view supporting get, subscript, iteration, and len."""

    def __init__(self, data: dict[str, dict], lock: "threading.RLock | None" = None):
        self._data = data
        self._lock = lock

    def get(self, nid: str, default: dict | None = None) -> dict:
        return self._data.get(nid, default if default is not None else {})

    def __getitem__(self, nid: str) -> dict:
        return self._data[nid]

    def __contains__(self, nid: str) -> bool:
        return nid in self._data

    def __iter__(self):
        # Snapshot under the lock: a concurrent add_node/clear on the loop
        # thread must not mutate the dict while an mc-embed reader iterates it
        # ("dictionary changed size during iteration").
        if self._lock is not None:
            with self._lock:
                return iter(list(self._data))
        return iter(list(self._data))

    def __len__(self) -> int:
        return len(self._data)


class _EdgeView:
    """Minimal edge view supporting iteration and subscript access."""

    def __init__(self, fwd: dict[str, dict[str, dict]], lock: "threading.RLock | None" = None):
        self._fwd = fwd
        self._lock = lock

    def __getitem__(self, key: tuple[str, str]) -> dict:
        u, v = key
        return self._fwd[u][v]

    def __call__(self, *, data: bool = False):  # noqa: ARG002
        # Materialize a snapshot under the lock before yielding — see _NodeView.
        if self._lock is not None:
            with self._lock:
                snapshot = [
                    (u, v, attrs)
                    for u, targets in self._fwd.items()
                    for v, attrs in targets.items()
                ]
        else:
            snapshot = [
                (u, v, attrs)
                for u, targets in self._fwd.items()
                for v, attrs in targets.items()
            ]
        yield from snapshot


class SimpleDiGraph:
    """Minimal directed graph replacing networkx.DiGraph for the subset of API we use."""

    def __init__(self) -> None:
        # Guards all reads/writes of the three dicts. KnowledgeStore mutates the
        # graph inline on the event-loop thread (ingest _store_entities,
        # dedup -> _load_graph clear()+rebuild), while HybridRetriever.search()
        # traverses it on an mc-embed executor thread (get_neighbors ->
        # successors/predecessors/nodes). Without this, concurrent iterate +
        # clear/insert on the same dict raises "dictionary changed size during
        # iteration" (HTTP 500) or returns a half-rebuilt graph. Re-entrant
        # because a single logical op (e.g. get_neighbors) takes it repeatedly.
        self._lock = threading.RLock()
        self._node_attrs: dict[str, dict] = {}
        self._fwd: dict[str, dict[str, dict]] = defaultdict(dict)
        self._rev: dict[str, dict[str, dict]] = defaultdict(dict)
        self.nodes = _NodeView(self._node_attrs, self._lock)
        self.edges = _EdgeView(self._fwd, self._lock)

    def clear(self) -> None:
        with self._lock:
            self._node_attrs.clear()
            self._fwd.clear()
            self._rev.clear()

    def add_node(self, nid: str, **attrs: object) -> None:
        with self._lock:
            self._node_attrs[nid] = attrs

    def add_edge(self, u: str, v: str, **attrs: object) -> None:
        with self._lock:
            self._fwd[u][v] = attrs
            self._rev[v][u] = attrs

    def has_edge(self, u: str, v: str) -> bool:
        with self._lock:
            return v in self._fwd.get(u, {})

    def has_node(self, nid: str) -> bool:
        with self._lock:
            return nid in self._node_attrs

    def degree(self, nid: str) -> int:
        with self._lock:
            return len(self._fwd.get(nid, {})) + len(self._rev.get(nid, {}))

    def successors(self, nid: str):
        # Snapshot the neighbor keys under the lock so the caller can iterate
        # freely while the loop thread mutates the graph (see __init__).
        with self._lock:
            return iter(list(self._fwd.get(nid, {})))

    def predecessors(self, nid: str):
        with self._lock:
            return iter(list(self._rev.get(nid, {})))


# The per-document state tables, each with the status that means "this row owns a
# live item group". The vocabularies genuinely differ and are NOT interchangeable:
# a folder file is scan-driven, so it moves pending -> done and can be re-walked,
# while an aggregate document is push-driven with no scanner to revive it and is
# either 'active' or 'deduped'. Writing 'done' to an aggregate row hides it from
# ``find_document_by_hash``, which matches on 'active' -- and an invisible row lets
# identical content in under a second uri as a duplicate.
_DOC_STATE_TABLES: tuple[tuple[str, str], ...] = (
    ("folder_file_state", "done"),
    ("artifact_item_state", "active"),
    ("agent_item_state", "active"),
)

# Which column on each state table holds a hash in the SAME DOMAIN as
# ``items.content_hash``, for lookups that have to relate a state row to items.
#
# Two different quantities are both called a content hash, and they are not
# interchangeable:
#
# * ``folder_file_state.content_hash`` is sha256 over the file's RAW BYTES. It
#   answers "has this file changed on disk?" and is compared before any
#   extraction runs -- deriving it from extracted text would force an extraction
#   pass on every scan, which is the cost the mtime/hash gate exists to avoid.
#   The pre-ingest duplicate gate is in the same domain for the same reason: it
#   also runs before extraction.
# * ``items.content_hash`` is sha256 over the EXTRACTED TEXT.
#
# For .md/.txt the two coincide, so a cross-domain comparison appears to work.
# For anything the reader transforms -- PDF, DOCX, HTML -- they differ and the
# comparison silently matches nothing, which is how ownership bookkeeping came to
# be inert for exactly those documents. Folder rows therefore carry the text hash
# separately, in ``text_hash``; the aggregate tables already store a text hash in
# ``content_hash`` and need no second column.
_OWNERSHIP_HASH_COL: dict[str, str] = {
    "folder_file_state": "COALESCE(text_hash, content_hash)",
    "artifact_item_state": "content_hash",
    "agent_item_state": "content_hash",
}
# Folder rows COALESCE so a legacy row -- written before ``text_hash`` existed, and
# deliberately never backfilled -- keeps behaving exactly as it does today: for the
# plaintext files whose two hashes are equal its ownership lookups still match, which
# they would stop doing if the new column were consulted alone. A rescan populates
# ``text_hash`` and the row becomes correct for transformed documents too.


class KnowledgeStore:
    def __init__(self, db_path: str):
        self._db_path = db_path
        # One connection PER THREAD. sqlite3 connections carry
        # thread affinity (check_same_thread=True by default), but callers
        # like HybridRetriever.search() run on worker threads via
        # run_in_embed_pool / asyncio.to_thread while the store is created
        # on the event-loop thread. A shared connection raises
        # sqlite3.ProgrammingError from those workers (HTTP 500 on
        # /api/knowledge/search-for-context). WAL mode (below) supports
        # concurrent readers alongside a single writer, and busy_timeout
        # serializes rare cross-thread writes.
        self._thread_local = threading.local()
        self.graph = SimpleDiGraph()
        self._init_schema()
        self._migrate()
        self._load_graph()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    @property
    def db(self) -> sqlite3.Connection:
        """The calling thread's connection, created lazily on first use."""
        conn = getattr(self._thread_local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._thread_local.conn = conn
        return conn

    def _init_schema(self):
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

            -- merged_into_source_id names the SOURCE whose copy of this document
            -- survived a de-duplication collapse. It is deliberately a source id and
            -- never an item id: item_ids must keep meaning "the items this row owns",
            -- because dedup derives a document's hash and embedding from whatever
            -- item_ids points at, and delete authority follows the same list. A row
            -- naming another source's items would therefore be enumerated as a second
            -- document over one physical item set, and collapsing that pair deletes
            -- the surviving copy. The marker records the relationship instead, so
            -- deleting the surviving source can clear it and let this row re-ingest.
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

            CREATE TABLE IF NOT EXISTS artifact_item_state (
                source_id TEXT NOT NULL REFERENCES sources(id),
                slug TEXT NOT NULL,
                content_hash TEXT,
                item_ids TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL,
                name TEXT,
                status TEXT DEFAULT 'active',
                merged_into_source_id TEXT,
                PRIMARY KEY (source_id, slug)
            );

            -- Per-document item-group tracking for the aggregate "Auto-added"
            -- source the agent writes to, keyed by a stable per-document slug.
            -- Same shape and role as artifact_item_state: it is what lets one
            -- aggregate source hold many independently-replaceable documents,
            -- and what gives de-duplication a per-document unit to act on
            -- instead of the whole source.
            CREATE TABLE IF NOT EXISTS agent_item_state (
                source_id TEXT NOT NULL REFERENCES sources(id),
                slug TEXT NOT NULL,
                content_hash TEXT,
                item_ids TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL,
                name TEXT,
                status TEXT DEFAULT 'active',
                merged_into_source_id TEXT,
                PRIMARY KEY (source_id, slug)
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

    def _migrate(self):
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
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_content_hash ON items(content_hash)")
        # source_locations predates being an identity table: pre-existing DBs have
        # neither the (item_id, source_id) uniqueness nor any index. De-duplicate
        # first so the unique index can be created, then add both lookup indexes.
        self.db.execute("""
            DELETE FROM source_locations WHERE id NOT IN (
                SELECT MIN(id) FROM source_locations GROUP BY item_id, source_id
            )
        """)
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_source_locations_item_source "
            "ON source_locations(item_id, source_id)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_source_locations_item_id "
            "ON source_locations(item_id)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_source_locations_source_id "
            "ON source_locations(source_id)")
        job_cols = {r[1] for r in self.db.execute("PRAGMA table_info(ingestion_jobs)").fetchall()}
        if "items_failed" not in job_cols:
            self.db.execute("ALTER TABLE ingestion_jobs ADD COLUMN items_failed INTEGER DEFAULT 0")
        src_cols = {r[1] for r in self.db.execute("PRAGMA table_info(sources)").fetchall()}
        if "sync_status" not in src_cols:
            self.db.execute("ALTER TABLE sources ADD COLUMN sync_status TEXT DEFAULT 'pending'")
        if "summary_topic" not in src_cols:
            self.db.execute("ALTER TABLE sources ADD COLUMN summary_topic TEXT")
        if "summary_themes" not in src_cols:
            self.db.execute("ALTER TABLE sources ADD COLUMN summary_themes TEXT")
        # Migrate: folder_file_state table
        tables = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "folder_file_state" not in tables:
            self.db.execute("""
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
                )
            """)
        else:
            ffs_cols = {r[1] for r in self.db.execute("PRAGMA table_info(folder_file_state)").fetchall()}
            if "status" not in ffs_cols:
                self.db.execute("ALTER TABLE folder_file_state ADD COLUMN status TEXT DEFAULT 'pending'")
            if "error_message" not in ffs_cols:
                self.db.execute("ALTER TABLE folder_file_state ADD COLUMN error_message TEXT")
            if "merged_into_source_id" not in ffs_cols:
                self.db.execute(
                    "ALTER TABLE folder_file_state ADD COLUMN merged_into_source_id TEXT")
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
                    "ALTER TABLE folder_file_state "
                    "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        # Migrate: artifact_item_state table -- per-artifact item-group tracking
        # for the aggregate "Artifacts" KB source, keyed by artifact slug.
        if "artifact_item_state" not in tables:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS artifact_item_state (
                    source_id TEXT NOT NULL REFERENCES sources(id),
                    slug TEXT NOT NULL,
                    content_hash TEXT,
                    item_ids TEXT DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    name TEXT,
                    merged_into_source_id TEXT,
                    PRIMARY KEY (source_id, slug)
                )
            """)
        else:
            ais_cols = {r[1] for r in self.db.execute(
                "PRAGMA table_info(artifact_item_state)").fetchall()}
            if "name" not in ais_cols:
                self.db.execute("ALTER TABLE artifact_item_state ADD COLUMN name TEXT")
            if "status" not in ais_cols:
                self.db.execute(
                    "ALTER TABLE artifact_item_state ADD COLUMN status TEXT DEFAULT 'active'")
            if "merged_into_source_id" not in ais_cols:
                self.db.execute(
                    "ALTER TABLE artifact_item_state ADD COLUMN merged_into_source_id TEXT")
        # Migrate: agent_item_state table -- per-document item-group tracking for
        # the aggregate "Auto-added" KB source the agent writes to.
        if "agent_item_state" not in tables:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS agent_item_state (
                    source_id TEXT NOT NULL REFERENCES sources(id),
                    slug TEXT NOT NULL,
                    content_hash TEXT,
                    item_ids TEXT DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    name TEXT,
                    status TEXT DEFAULT 'active',
                    merged_into_source_id TEXT,
                    PRIMARY KEY (source_id, slug)
                )
            """)
        else:
            agent_cols = {r[1] for r in self.db.execute(
                "PRAGMA table_info(agent_item_state)").fetchall()}
            if "status" not in agent_cols:
                self.db.execute(
                    "ALTER TABLE agent_item_state ADD COLUMN status TEXT DEFAULT 'active'")
            if "merged_into_source_id" not in agent_cols:
                self.db.execute(
                    "ALTER TABLE agent_item_state ADD COLUMN merged_into_source_id TEXT")
        # Clean orphan sources (no items), entities (no mentions/relations), and stale relations
        #
        # Folder sources are EXCLUDED: a watched folder with zero discovered
        # files is legitimately empty, not orphaned. Deleting it here loses
        # user-set state -- notably a paused empty folder would be dropped on
        # restart and then re-created as active by auto-discovery, silently
        # un-pausing it. The row is user-registered configuration, not derived
        # data, so only its items are reclaimable.
        self.db.execute("BEGIN")
        try:
            orphan_sources_q = (
                "SELECT id FROM sources WHERE id NOT IN (SELECT DISTINCT source_id FROM items WHERE source_id IS NOT NULL) "
                "AND source_type NOT IN ('local_folder', 'obsidian_vault', 'quip') "
                "AND id NOT IN (SELECT source_id FROM ingestion_jobs WHERE status IN ('pending', 'processing')) "
                "AND id NOT IN (SELECT DISTINCT source_id FROM folder_file_state) "
                "AND id NOT IN (SELECT DISTINCT source_id FROM artifact_item_state) "
                "AND id NOT IN (SELECT DISTINCT source_id FROM agent_item_state) "
                # A source can hold documents it does not OWN: after a duplicate
                # collapse it is a location of the surviving copy. Reaping it here
                # would delete the very rows that record co-ownership, on every
                # gateway start, and the document would stop being reachable from it.
                "AND id NOT IN (SELECT DISTINCT source_id FROM source_locations)"
            )
            self.db.execute(f"DELETE FROM source_locations WHERE source_id IN ({orphan_sources_q})")
            self.db.execute(f"DELETE FROM ingestion_jobs WHERE source_id IN ({orphan_sources_q})")
            self.db.execute(f"DELETE FROM sources WHERE id IN ({orphan_sources_q})")
            self.db.execute("DELETE FROM entity_relations WHERE source_id NOT IN (SELECT id FROM entities) OR target_id NOT IN (SELECT id FROM entities)")
            self.db.execute("""
                DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM mentions)
                AND id NOT IN (SELECT source_id FROM entity_relations)
                AND id NOT IN (SELECT target_id FROM entity_relations)
            """)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def find_doc_by_content_hash(
        self, content_hash: str, exclude_source_id: str | None = None
    ) -> dict | None:
        """The first document already holding this exact content, or ``None``.

        Every chunk of a document carries the document's whole-text
        ``content_hash``, so a hit means this text is already in the Library.
        Uses ``idx_items_content_hash``.

        ``exclude_source_id`` skips a source, so re-ingesting a document into the
        source that already owns it is not mistaken for a duplicate -- that is a
        content replacement and must proceed.
        """
        if not content_hash:
            return None
        sql = ("SELECT i.source_id, s.source_type, s.name AS source_name "
               "FROM items i JOIN sources s ON s.id = i.source_id "
               "WHERE i.content_hash = ?")
        params: list[str] = [content_hash]
        if exclude_source_id:
            sql += " AND i.source_id != ?"
            params.append(exclude_source_id)
        row = self.db.execute(sql + " LIMIT 1", tuple(params)).fetchone()
        return dict(row) if row else None

    def _load_graph(self):
        self.graph.clear()
        for row in self.db.execute("SELECT id, name, entity_type FROM entities"):
            self.graph.add_node(row["id"], name=row["name"], entity_type=row["entity_type"])
        for row in self.db.execute("SELECT id, source_id, target_id, relation_type, weight FROM entity_relations"):
            self.graph.add_edge(row["source_id"], row["target_id"],
                                id=row["id"], relation_type=row["relation_type"], weight=row["weight"])

    def add_item(self, title, content, item_type, source_id=None, chunk_index=0,
                 summary=None, tags=None, embedding=None, namespace="default",
                 content_hash=None) -> str:
        item_id = str(uuid4())
        now = datetime.now().isoformat()
        tags_json = json.dumps(tags or [])
        self.db.execute("BEGIN")
        try:
            self.db.execute(
                "INSERT INTO items (id, title, content, item_type, source_id, chunk_index, namespace, summary, tags, embedding, content_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, title, content, item_type, source_id, chunk_index, namespace, summary, tags_json, embedding, content_hash, now, now))
            # Sync FTS: get the rowid of the inserted item
            rowid = self.db.execute("SELECT rowid FROM items WHERE id = ?", (item_id,)).fetchone()[0]
            self.db.execute(
                "INSERT INTO items_fts (rowid, title, content, tags) VALUES (?, ?, ?, ?)",
                (rowid, title, content, tags_json))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return item_id

    def get_item(self, item_id):
        row = self.db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._serialize_item(row) if row else None

    @staticmethod
    def _serialize_item(row) -> dict:
        d = dict(row)
        raw = d.get("embedding")
        if isinstance(raw, bytes):
            d["embedding"] = base64.b64encode(raw).decode("ascii")
        return d

    _ITEM_COLUMNS = {"title", "content", "item_type", "summary", "tags", "embedding", "status", "namespace", "updated_at"}

    def update_item(self, item_id, **fields):
        if not fields:
            return
        fields["updated_at"] = datetime.now().isoformat()
        safe = {k: v for k, v in fields.items() if k in self._ITEM_COLUMNS}
        if not safe:
            return
        # Read old FTS values BEFORE the update
        fts_fields = {"title", "content", "tags"} & set(fields)
        old_row = None
        if fts_fields:
            old_row = self.db.execute(
                "SELECT rowid, title, content, tags FROM items WHERE id = ?", (item_id,)
            ).fetchone()
        cols = ", ".join(f"{k} = ?" for k in safe)
        vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in safe.values()]
        self.db.execute("BEGIN")
        try:
            self.db.execute(f"UPDATE items SET {cols} WHERE id = ?", (*vals, item_id))  # noqa: S608
            # Sync FTS: delete with OLD values, insert with NEW values
            if old_row:
                self.db.execute("INSERT INTO items_fts (items_fts, rowid, title, content, tags) VALUES ('delete', ?, ?, ?, ?)",
                                (old_row["rowid"], old_row["title"], old_row["content"], old_row["tags"]))
                new_row = self.db.execute(
                    "SELECT title, content, tags FROM items WHERE id = ?", (item_id,)
                ).fetchone()
                self.db.execute("INSERT INTO items_fts (rowid, title, content, tags) VALUES (?, ?, ?, ?)",
                                (old_row["rowid"], new_row["title"], new_row["content"], new_row["tags"]))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _delete_item_cascade(self, item_id):
        """Delete item and its dependents without commit/graph reload (for batch use)."""
        row = self.db.execute("SELECT rowid, title, content, tags FROM items WHERE id = ?", (item_id,)).fetchone()
        if row:
            self.db.execute("INSERT INTO items_fts (items_fts, rowid, title, content, tags) VALUES ('delete', ?, ?, ?, ?)",
                            (row["rowid"], row["title"], row["content"], row["tags"]))
        self.db.execute("DELETE FROM source_locations WHERE item_id = ?", (item_id,))
        self.db.execute("DELETE FROM mentions WHERE item_id = ?", (item_id,))
        self.db.execute("DELETE FROM entity_relations WHERE source_item_id = ?", (item_id,))
        self.db.execute("DELETE FROM items WHERE id = ?", (item_id,))

    def delete_item(self, item_id):
        self.db.execute("BEGIN")
        try:
            self._delete_item_cascade(item_id)
            # Remove orphan entities (no mentions and no relations)
            self.db.execute("""
                DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM mentions)
                AND id NOT IN (SELECT source_id FROM entity_relations)
                AND id NOT IN (SELECT target_id FROM entity_relations)
            """)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self._load_graph()

    def _adopt_reassigned_item(self, item_id: str, new_source_id: str) -> None:
        """Let the new owner's state row know it owns *item_id*.

        Reassignment moves ``items.source_id``, but a state row's ``item_ids`` is the
        only list its own delete path consults. A recipient that owns an item its row
        never names cannot delete it: removing that document drops an empty group and
        the content stays searchable, which is the strand this whole model exists to
        prevent. So the row that describes this content adopts the item.

        Matched on ``content_hash`` because that is what identifies the document
        independently of which source holds it. Appends rather than replaces, so a
        multi-item group (a chunked file) is not truncated to one, and clears any
        deferral marker: a row that owns an item is no longer deferring to anyone.

        A hash is only an identifier while it picks out ONE row. Two distinct
        documents in one source may legitimately hold identical text, and writing the
        item into both would put one physical item in two groups -- then removing
        either document deletes it and takes the other's indexed content with it. So
        an ambiguous hash adopts nothing: an un-adopted row leaves a stale claim,
        which is visible and recoverable, whereas a cross-wired group destroys
        content on the next delete.
        """
        row = self.db.execute(
            "SELECT content_hash FROM items WHERE id = ?", (item_id,)).fetchone()
        content_hash = row["content_hash"] if row else None
        if not content_hash:
            return
        matches: list[tuple[str, str, object]] = []
        for table, healthy in _DOC_STATE_TABLES:
            hash_col = _OWNERSHIP_HASH_COL[table]
            for st in self.db.execute(
                    f"SELECT rowid, item_ids FROM {table} "  # noqa: S608
                    f"WHERE source_id = ? AND {hash_col} = ?",
                    (new_source_id, content_hash)).fetchall():
                matches.append((table, healthy, st))
        if len(matches) != 1:
            if matches:
                logger.warning(
                    "Not adopting item into source %s: %d documents there share this "
                    "content, so the hash does not say which one owns it",
                    new_source_id, len(matches))
            return
        table, healthy, st = matches[0]
        try:
            ids = json.loads(st["item_ids"] or "[]")
        except (TypeError, ValueError):
            ids = []
        if item_id not in ids:
            ids.append(item_id)
            self.db.execute(
                f"UPDATE {table} SET item_ids = ?, status = ?, "  # noqa: S608
                "merged_into_source_id = NULL WHERE rowid = ?",
                (json.dumps(ids), healthy, st["rowid"]))

    def detach_source_location_by_hash(self, source_id: str, content_hash: str) -> int:
        """Drop this source's CLAIM on a document it no longer has a copy of.

        The counterpart to :meth:`_adopt_reassigned_item`. A source that lost a dedup
        holds no items for that document -- its state row is 'deduped' with an empty
        group -- yet it IS still a location of the winner's items, which is what keeps
        the document reachable if the winner goes away. When the losing copy is
        genuinely removed (its file deleted from that folder), the claim has to go too,
        or the source stays a candidate to inherit a document it no longer has and the
        content resurfaces there as searchable text with no file behind it.

        Identified by ``content_hash`` because that is the only handle such a row has:
        it owns no item ids, and the winner's ids must never be written into it -- a
        row naming another source's items becomes a second document over one physical
        item set, which self-collapses and deletes the survivor.

        Only the location rows are removed. The items belong to the winner and are
        left untouched. Returns the number of claims dropped.

        Like adoption, this acts only when the hash picks out ONE document here. If a
        second document in this source holds identical text, the claim is shared and
        dropping it would strand that other document when the winner goes away. On
        ambiguity the claim is kept: a stale claim can resurface content that exists
        elsewhere, which is recoverable, while a released one destroys a document.
        """
        if not content_hash:
            return 0
        claimants = 0
        for table, _healthy in _DOC_STATE_TABLES:
            hash_col = _OWNERSHIP_HASH_COL[table]
            hit = self.db.execute(
                f"SELECT COUNT(*) AS n FROM {table} "  # noqa: S608
                f"WHERE source_id = ? AND {hash_col} = ?",
                (source_id, content_hash)).fetchone()
            claimants += int(hit["n"] or 0) if hit else 0
        if claimants > 1:
            logger.warning(
                "Keeping source %s's claim: %d documents there share this content, so "
                "releasing it could strand one of them", source_id, claimants)
            return 0
        cur = self.db.execute(
            "DELETE FROM source_locations WHERE source_id = ? AND item_id IN "
            "(SELECT id FROM items WHERE content_hash = ?)",
            (source_id, content_hash))
        return cur.rowcount or 0

    def release_stale_claim(self, source_id: str, prev_hash: str | None,
                            new_hash: str, prev_item_ids: list[str],
                            prev_text_hash: str | None = None) -> int:
        """Release a claim made for content this source no longer has.

        A source that lost a dedup owns no items but IS a location of the winner's,
        and that claim is specific to the content it was made for. When the source's
        copy is EDITED, the claim becomes a claim on the wrong document: deleting the
        holder would then hand this source the superseded text, which stays searchable
        with nothing behind it.

        The rule lives here rather than at each ingest path because all three paths
        (folder file, artifact, agent document) can be edited and all three would
        otherwise have to re-derive it. Only fires when the row owned NOTHING -- a row
        with a live group replaces its own items through the normal delete-and-reingest
        path -- and only when the hash actually moved. Returns claims dropped.

        Two domains are in play and both are needed. *prev_hash*/*new_hash* decide
        whether the file CHANGED, which is a question about the bytes on disk. The
        detach then has to name a document in the ITEM domain, which is what
        *prev_text_hash* carries for folder rows. Callers whose ``content_hash`` is
        already a text hash (artifacts, agent documents) pass nothing and the
        fallback uses *prev_hash* unchanged. A legacy folder row with no text hash
        also falls back, and matches nothing exactly as it does today.
        """
        if prev_item_ids or not prev_hash or prev_hash == new_hash:
            return 0
        return self.detach_source_location_by_hash(
            source_id, prev_text_hash or prev_hash)

    def delete_items_batch(self, item_ids: list[str], owner_source_id: str | None = None):
        """Delete multiple items in a single transaction with one graph reload.

        Pass *owner_source_id* when the caller means "this SOURCE's copy of these
        documents is gone" -- a folder file removed from disk, a replaced document, a
        collapsed duplicate. An item another source also holds is then DETACHED
        rather than destroyed: ownership moves to a surviving holder and only the
        calling source's location row is dropped. Without the argument the items are
        destroyed outright, which is correct only when the caller means the document
        itself is going.
        """
        if not item_ids:
            return
        self.db.execute("BEGIN")
        try:
            for item_id in item_ids:
                if owner_source_id:
                    others = self.sources_holding_item(
                        item_id, exclude_source_id=owner_source_id)
                    if others:
                        self.reassign_item_source(item_id, others[0])
                        self._adopt_reassigned_item(item_id, others[0])
                        self.db.execute(
                            "DELETE FROM source_locations "
                            "WHERE item_id = ? AND source_id = ?",
                            (item_id, owner_source_id))
                        continue
                self._delete_item_cascade(item_id)
            self.db.execute("""
                DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM mentions)
                AND id NOT IN (SELECT source_id FROM entity_relations)
                AND id NOT IN (SELECT target_id FROM entity_relations)
            """)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self._load_graph()

    def dismiss_auto_source(self, uri: str) -> None:
        """Record that the user deleted an auto-discovered source at ``uri``.

        Survives ``delete_source_cascade`` on purpose -- see the table comment.
        Idempotent: re-dismissing keeps the original timestamp semantics simple
        by overwriting, which is fine since only presence is consulted.
        """
        self.db.execute(
            "INSERT OR REPLACE INTO dismissed_auto_sources (uri, dismissed_at) VALUES (?, ?)",
            (uri, datetime.now().isoformat()),
        )
        self.db.commit()

    def is_auto_source_dismissed(self, uri: str) -> bool:
        """True when ``uri`` was previously dismissed via ``dismiss_auto_source``."""
        row = self.db.execute(
            "SELECT 1 FROM dismissed_auto_sources WHERE uri = ?", (uri,)
        ).fetchone()
        return row is not None

    def undismiss_auto_source(self, uri: str) -> None:
        """Clear a dismissal so auto-discovery may register ``uri`` again."""
        self.db.execute("DELETE FROM dismissed_auto_sources WHERE uri = ?", (uri,))
        self.db.commit()

    def create_auto_source_unless_dismissed(
        self, name: str, source_type: str, uri: str, properties: dict,
    ) -> tuple[str | None, bool]:
        """Atomically: reuse, refuse-if-dismissed, or insert an auto source.

        Returns ``(source_id, created)``, or ``(None, False)`` when ``uri`` is
        tombstoned. The tombstone check, the existing-row check and the INSERT
        all happen inside ONE ``BEGIN IMMEDIATE`` transaction so a concurrent
        ``delete_source_cascade(..., dismiss_uri=uri)`` cannot interleave: either
        the delete's tombstone is visible here and nothing is created, or this
        insert lands first and the delete then removes it and tombstones the URI.
        Doing the check and the insert as two transactions would leave exactly
        the window that lets a deleted auto source come back.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            dismissed = self.db.execute(
                "SELECT 1 FROM dismissed_auto_sources WHERE uri = ?", (uri,)
            ).fetchone()
            if dismissed:
                self.db.execute("COMMIT")
                return None, False
            existing = self.db.execute(
                "SELECT id FROM sources WHERE uri = ?", (uri,)
            ).fetchone()
            if existing:
                sid = existing["id"]
                self.db.execute("COMMIT")
                return sid, False
            sid = str(uuid4())
            now = datetime.now().isoformat()
            self.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, name, source_type, uri, json.dumps(properties), now, now),
            )
            self.db.execute("COMMIT")
            return sid, True
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def delete_source_cascade(self, source_id, dismiss_uri: str | None = None):
        """Delete a source and all its items in a single transaction (batch SQL).

        ``dismiss_uri`` writes an auto-discovery tombstone for that URI inside
        the SAME transaction as the delete. That atomicity is required, not a
        convenience: auto-discovery runs on a recurring watcher sweep, so a
        tombstone written in a separate transaction after the cascade leaves a
        window where a sweep observes neither a source row nor a tombstone and
        re-creates (and re-ingests) the source the user just deleted.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if dismiss_uri:
                self.db.execute(
                    "INSERT OR REPLACE INTO dismissed_auto_sources (uri, dismissed_at) "
                    "VALUES (?, ?)",
                    (dismiss_uri, datetime.now().isoformat()),
                )
            # A document reachable from another source SURVIVES this deletion. Its
            # ownership moves to one of those sources and only this source's location
            # row is dropped; the item, its text, embedding, FTS row and graph edges
            # are untouched. Only items this source solely holds are destroyed.
            owned = [r["id"] for r in self.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]
            doomed: list[str] = []
            for item_id in owned:
                others = self.sources_holding_item(item_id, exclude_source_id=source_id)
                if others:
                    self.reassign_item_source(item_id, others[0])
                    # Same obligation as the item-level path: a recipient must never
                    # own an item its own row does not name. The revive loop below
                    # only reaches rows that DEFERRED to this source, and a row can
                    # hold a location without ever carrying a marker -- the pre-ingest
                    # gate writes exactly that shape -- so adoption belongs here too.
                    self._adopt_reassigned_item(item_id, others[0])
                else:
                    doomed.append(item_id)

            # This source stops being a location of everything it held, whether the
            # item survived under a new owner or is about to be deleted.
            self.db.execute("DELETE FROM source_locations WHERE source_id = ?", (source_id,))

            if doomed:
                q = ",".join("?" for _ in doomed)
                # FTS is external-content, so the old column values must be handed to
                # the 'delete' command BEFORE the rows go -- and only for the rows going.
                for row in self.db.execute(
                        f"SELECT rowid, title, content, tags FROM items WHERE id IN ({q})",  # noqa: S608
                        doomed).fetchall():
                    self.db.execute(
                        "INSERT INTO items_fts (items_fts, rowid, title, content, tags) "
                        "VALUES ('delete', ?, ?, ?, ?)",
                        (row["rowid"], row["title"], row["content"], row["tags"]))
                self.db.execute(
                    f"DELETE FROM source_locations WHERE item_id IN ({q})", doomed)  # noqa: S608
                self.db.execute(f"DELETE FROM mentions WHERE item_id IN ({q})", doomed)  # noqa: S608
                self.db.execute(
                    f"DELETE FROM entity_relations WHERE source_item_id IN ({q})", doomed)  # noqa: S608
                self.db.execute(f"DELETE FROM items WHERE id IN ({q})", doomed)  # noqa: S608

            # Documents that deferred to this source need their marker cleared, or the
            # collapse outlives its reason and they stay stranded. But HOW they are
            # revived depends on whether the surviving copy just became theirs: the
            # reassignment above may have moved it into the very source that deferred
            # to this one. Marking such a row 'pending' would re-ingest a document the
            # source already owns and leave it holding two copies.
            for table, healthy in _DOC_STATE_TABLES:
                deferred = self.db.execute(
                    f"SELECT source_id, content_hash, rowid FROM {table} "  # noqa: S608
                    "WHERE merged_into_source_id = ?", (source_id,)).fetchall()
                for row in deferred:
                    adopted: list[str] = []
                    if row["content_hash"]:
                        adopted = [r["id"] for r in self.db.execute(
                            "SELECT id FROM items WHERE source_id = ? AND content_hash = ?",
                            (row["source_id"], row["content_hash"])).fetchall()]
                    if adopted:
                        # The document is present and owned here now: adopt the items
                        # rather than re-ingesting. Its own items, so no foreign group.
                        # The status must be this table's own healthy value -- see
                        # _DOC_STATE_TABLES for why they are not interchangeable.
                        self.db.execute(
                            f"UPDATE {table} SET merged_into_source_id = NULL, "  # noqa: S608
                            "status = ?, item_ids = ? WHERE rowid = ?",
                            (healthy, json.dumps(adopted), row["rowid"]))
                    elif table == "folder_file_state":
                        # Scan-driven: clearing the marker to 'pending' makes the next
                        # walk re-ingest the file, which is the whole point of reviving.
                        self.db.execute(
                            "UPDATE folder_file_state SET merged_into_source_id = NULL, "
                            "status = 'pending' WHERE rowid = ?", (row["rowid"],))
                    else:
                        # Push-driven with no scanner to revive it, and the content is
                        # genuinely gone -- so the row keeps its 'deduped' status and
                        # only loses the marker. Promoting it to 'active' while it owns
                        # nothing would make find_document_by_hash refuse a re-add of
                        # content the Library does not actually hold.
                        self.db.execute(
                            f"UPDATE {table} SET merged_into_source_id = NULL "  # noqa: S608
                            "WHERE rowid = ?", (row["rowid"],))
            self.db.execute("DELETE FROM ingestion_jobs WHERE source_id = ?", (source_id,))
            self.db.execute("DELETE FROM folder_file_state WHERE source_id = ?", (source_id,))
            self.db.execute("DELETE FROM artifact_item_state WHERE source_id = ?", (source_id,))
            self.db.execute("DELETE FROM agent_item_state WHERE source_id = ?", (source_id,))
            self.db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            # Remove orphan entities
            self.db.execute("""
                DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM mentions)
                AND id NOT IN (SELECT source_id FROM entity_relations)
                AND id NOT IN (SELECT target_id FROM entity_relations)
            """)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self._load_graph()

    def search_items_fts(self, query, limit=10, offset=0) -> list:
        safe = self._sanitize_fts5(query)
        if not safe:
            return []
        try:
            rows = self.db.execute(
                "SELECT i.*, fts.rank FROM items_fts fts "
                "JOIN items i ON i.rowid = fts.rowid "
                "WHERE items_fts MATCH ? ORDER BY fts.rank LIMIT ? OFFSET ?",
                (safe, limit, offset)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [self._serialize_item(r) for r in rows]

    def search_items_fts_count(self, query) -> int:
        safe = self._sanitize_fts5(query)
        if not safe:
            return 0
        try:
            row = self.db.execute(
                "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH ?",
                (safe,)).fetchone()
            return row[0] if row else 0
        except sqlite3.OperationalError:
            return 0

    @staticmethod
    def _sanitize_fts5(query: str) -> str:
        tokens = query.split()
        return " ".join('"' + t.replace('"', '""') + '"' for t in tokens if t)

    def add_entity(self, name, entity_type, description=None, aliases=None) -> str:
        eid = str(uuid4())
        now = datetime.now().isoformat()
        self.db.execute(
            "INSERT INTO entities (id, name, entity_type, description, aliases, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, name, entity_type, description, json.dumps(aliases or []), now, now))
        self.graph.add_node(eid, name=name, entity_type=entity_type)
        self.db.commit()
        return eid

    def find_entity(self, name):
        row = self.db.execute("SELECT * FROM entities WHERE name = ?", (name,)).fetchone()
        if row:
            return dict(row)
        row = self.db.execute("SELECT * FROM entities WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()
        if row:
            return dict(row)
        for row in self.db.execute("SELECT * FROM entities"):
            aliases = json.loads(row["aliases"]) if row["aliases"] else []
            if any(a.lower() == name.lower() for a in aliases):
                return dict(row)
        return None

    def merge_entities(self, keep_id, merge_id):
        self.db.execute("UPDATE entity_relations SET source_id = ? WHERE source_id = ?", (keep_id, merge_id))
        self.db.execute("UPDATE entity_relations SET target_id = ? WHERE target_id = ?", (keep_id, merge_id))
        # Remove self-loops created by the merge
        self.db.execute(
            "DELETE FROM entity_relations WHERE source_id = ? AND target_id = ?",
            (keep_id, keep_id))
        # Delete mentions that would conflict, then update the rest
        self.db.execute(
            "DELETE FROM mentions WHERE entity_id = ? AND item_id IN (SELECT item_id FROM mentions WHERE entity_id = ?)",
            (merge_id, keep_id))
        self.db.execute("UPDATE mentions SET entity_id = ? WHERE entity_id = ?", (keep_id, merge_id))
        self.db.execute("DELETE FROM entities WHERE id = ?", (merge_id,))
        self.db.commit()
        self._load_graph()

    def add_entity_relation(self, source_id, target_id, relation_type,
                            description=None, weight=1.0, source_item_id=None) -> str:
        rid = str(uuid4())
        now = datetime.now().isoformat()
        self.db.execute(
            "INSERT INTO entity_relations (id, source_id, target_id, relation_type, description, weight, source_item_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, source_id, target_id, relation_type, description, weight, source_item_id, now))
        self.graph.add_edge(source_id, target_id, id=rid, relation_type=relation_type, weight=weight)
        self.db.commit()
        return rid

    def add_mention(self, item_id, entity_id, context=None):
        now = datetime.now().isoformat()
        self.db.execute(
            "INSERT OR IGNORE INTO mentions (item_id, entity_id, context, created_at) VALUES (?, ?, ?, ?)",
            (item_id, entity_id, context, now))
        self.db.commit()

    def add_source(self, name, source_type, uri, **kwargs) -> str:
        sid = str(uuid4())
        now = datetime.now().isoformat()
        self.db.execute(
            "INSERT INTO sources (id, name, source_type, uri, properties, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, name, source_type, uri, json.dumps(kwargs.get("properties", {})), now, now))
        self.db.commit()
        return sid

    def get_source_by_uri(self, uri):
        row = self.db.execute("SELECT * FROM sources WHERE uri = ?", (uri,)).fetchone()
        return dict(row) if row else None

    _SOURCE_COLUMNS = {"name", "source_type", "uri", "properties", "last_synced", "sync_status", "updated_at"}

    def update_source(self, source_id, **fields):
        if not fields:
            return
        fields["updated_at"] = datetime.now().isoformat()
        safe = {k: v for k, v in fields.items() if k in self._SOURCE_COLUMNS}
        if not safe:
            return
        cols = ", ".join(f"{k} = ?" for k in safe)
        vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in safe.values()]
        self.db.execute(f"UPDATE sources SET {cols} WHERE id = ?", (*vals, source_id))  # noqa: S608
        self.db.commit()

    def add_source_location(self, item_id, source_id, chunk_range=None, section_title=None, anchor=None):
        """Record that *source_id* holds *item_id*, at an optional position within it.

        ``OR IGNORE`` against ``UNIQUE (item_id, source_id)``: a document reachable
        from two sources has one row per source, and re-attaching a pair that already
        exists is a no-op. Attaching a second source is what keeps the item alive when
        the first is deleted -- see ``sources_holding_item``.
        """
        lid = str(uuid4())
        now = datetime.now().isoformat()
        self.db.execute(
            "INSERT OR IGNORE INTO source_locations "
            "(id, item_id, source_id, chunk_range, section_title, anchor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lid, item_id, source_id, chunk_range, section_title, anchor, now))
        self.db.commit()

    def sources_holding_item(self, item_id: str, exclude_source_id: str | None = None) -> list[str]:
        """Ids of EXISTING sources that hold *item_id*, optionally excluding one.

        The reference count deletion consults: an item is destroyed only when this
        comes back empty. Joins ``sources`` so a location row left pointing at an
        already-deleted source cannot keep a dead item alive.
        """
        sql = ("SELECT sl.source_id FROM source_locations sl "
               "JOIN sources s ON s.id = sl.source_id WHERE sl.item_id = ?")
        params: list[str] = [item_id]
        if exclude_source_id:
            sql += " AND sl.source_id != ?"
            params.append(exclude_source_id)
        return [r["source_id"] for r in self.db.execute(sql, params).fetchall()]

    def reassign_item_source(self, item_id: str, new_source_id: str) -> None:
        """Re-point which source OWNS *item_id*.

        ``update_item`` deliberately cannot do this -- ``source_id`` is absent from
        ``_ITEM_COLUMNS``, so an ordinary update silently drops it. Ownership moves
        only here, and only when the owning source is being deleted while another
        source still holds the document.
        """
        self.db.execute("UPDATE items SET source_id = ? WHERE id = ?",
                        (new_source_id, item_id))

    def get_neighbors(self, entity_id, depth=1) -> list:
        visited = set()
        frontier = {entity_id}
        for _ in range(depth):
            next_frontier = set()
            for nid in frontier:
                for neighbor in self.graph.successors(nid):
                    if neighbor not in visited and neighbor != entity_id:
                        next_frontier.add(neighbor)
                for neighbor in self.graph.predecessors(nid):
                    if neighbor not in visited and neighbor != entity_id:
                        next_frontier.add(neighbor)
            visited |= frontier
            frontier = next_frontier
        visited |= frontier
        visited.discard(entity_id)
        result = []
        for nid in visited:
            data = self.graph.nodes.get(nid, {})
            result.append({"id": nid, "name": data.get("name"), "entity_type": data.get("entity_type")})
        return result

    def get_entity_subgraph(self, entity_id, depth=2) -> dict:
        visited = set()
        frontier = {entity_id}
        for _ in range(depth):
            next_frontier = set()
            for nid in frontier:
                for neighbor in self.graph.successors(nid):
                    next_frontier.add(neighbor)
                for neighbor in self.graph.predecessors(nid):
                    next_frontier.add(neighbor)
            visited |= frontier
            frontier = next_frontier - visited
        visited |= frontier
        nodes = []
        for nid in visited:
            data = self.graph.nodes.get(nid, {})
            nodes.append({"id": nid, "name": data.get("name"), "type": data.get("entity_type")})
        edges = []
        for u, v, data in self.graph.edges(data=True):
            if u in visited and v in visited:
                edges.append({"source": u, "target": v, "type": data.get("relation_type"), "weight": data.get("weight")})
        return {"nodes": nodes, "edges": edges}

    def get_stats(self) -> dict:
        return {
            "items": self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            "entities": self.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            "relations": self.db.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0],
            "sources": self.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
        }

    def export_item(self, item_id) -> dict:
        item = self.get_item(item_id)
        if not item:
            return {}
        mentions = self.db.execute("SELECT entity_id FROM mentions WHERE item_id = ?", (item_id,)).fetchall()
        entity_ids = [m["entity_id"] for m in mentions]
        entities = []
        for eid in entity_ids:
            row = self.db.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
            if row:
                entities.append(dict(row))
        relations = []
        seen_ids = set()
        for eid in entity_ids:
            for row in self.db.execute(
                    "SELECT * FROM entity_relations WHERE source_id = ? OR target_id = ?", (eid, eid)):
                r = dict(row)
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    relations.append(r)
        locations = [dict(r) for r in self.db.execute(
            "SELECT * FROM source_locations WHERE item_id = ?", (item_id,))]
        return {"item": item, "entities": entities, "relations": relations, "source_locations": locations}

    def export_all(self, namespace: str | None = None) -> dict:
        if namespace:
            items = [self._serialize_item(r) for r in self.db.execute(
                "SELECT * FROM items WHERE namespace = ?", (namespace,))]
            item_ids = {i["id"] for i in items}
        else:
            items = [self._serialize_item(r) for r in self.db.execute("SELECT * FROM items")]
            item_ids = None
        if item_ids is not None:
            items_subq = "SELECT id FROM items WHERE namespace = ?"
            relations = [dict(r) for r in self.db.execute(
                f"SELECT * FROM entity_relations WHERE source_item_id IS NULL OR source_item_id IN ({items_subq})",  # noqa: S608
                (namespace,))]
            source_locations = [dict(r) for r in self.db.execute(
                f"SELECT * FROM source_locations WHERE item_id IN ({items_subq})",  # noqa: S608
                (namespace,))]
            mentions = [dict(r) for r in self.db.execute(
                f"SELECT * FROM mentions WHERE item_id IN ({items_subq})",  # noqa: S608
                (namespace,))]
        else:
            relations = [dict(r) for r in self.db.execute("SELECT * FROM entity_relations")]
            source_locations = [dict(r) for r in self.db.execute("SELECT * FROM source_locations")]
            mentions = [dict(r) for r in self.db.execute("SELECT * FROM mentions")]
        return {
            "items": items,
            "entities": [dict(r) for r in self.db.execute("SELECT * FROM entities")],
            "relations": relations,
            "sources": [dict(r) for r in self.db.execute("SELECT * FROM sources")],
            "source_locations": source_locations,
            "mentions": mentions,
        }

    def import_bundle(self, bundle: dict) -> dict:
        items_imported = 0
        entities_created = 0
        relations_rebuilt = 0
        now = datetime.now().isoformat()
        self.db.execute("BEGIN")
        try:
            for src in bundle.get("sources", []):
                self.db.execute(
                    "INSERT OR IGNORE INTO sources (id, name, source_type, uri, properties, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (src["id"], src["name"], src["source_type"], src["uri"],
                     src.get("properties", "{}"), src.get("created_at", now), now))
            for item in bundle.get("items", []):
                raw_emb = item.get("embedding")
                if isinstance(raw_emb, str) and raw_emb:
                    try:
                        raw_emb = base64.b64decode(raw_emb)
                    except Exception:
                        raw_emb = None
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO items (id, title, content, item_type, source_id, chunk_index, namespace, summary, tags, embedding, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item["id"], item["title"], item["content"], item["item_type"],
                     item.get("source_id"), item.get("chunk_index", 0), item.get("namespace", "default"), item.get("summary"),
                     item.get("tags", "[]"), raw_emb, item.get("status", "active"),
                     item.get("created_at", now), now))
                if cursor.rowcount > 0:
                    items_imported += 1
                    row = self.db.execute("SELECT rowid FROM items WHERE id = ?", (item["id"],)).fetchone()
                    if row:
                        self.db.execute(
                            "INSERT INTO items_fts (rowid, title, content, tags) VALUES (?, ?, ?, ?)",
                            (row[0], item["title"], item["content"], item.get("tags", "[]")))
            for ent in bundle.get("entities", []):
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO entities (id, name, entity_type, description, aliases, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ent["id"], ent["name"], ent["entity_type"], ent.get("description"),
                     ent.get("aliases", "[]"), ent.get("created_at", now), now))
                if cursor.rowcount > 0:
                    entities_created += 1
            for rel in bundle.get("relations", []):
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO entity_relations (id, source_id, target_id, relation_type, description, weight, source_item_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (rel["id"], rel["source_id"], rel["target_id"], rel["relation_type"],
                     rel.get("description"), rel.get("weight", 1.0), rel.get("source_item_id"),
                     rel.get("created_at", now)))
                if cursor.rowcount > 0:
                    relations_rebuilt += 1
            for loc in bundle.get("source_locations", []):
                self.db.execute(
                    "INSERT OR IGNORE INTO source_locations (id, item_id, source_id, chunk_range, section_title, anchor, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (loc["id"], loc["item_id"], loc["source_id"], loc.get("chunk_range"),
                     loc.get("section_title"), loc.get("anchor"), loc.get("created_at", now)))
            for m in bundle.get("mentions", []):
                self.db.execute(
                    "INSERT OR IGNORE INTO mentions (item_id, entity_id, context, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (m["item_id"], m["entity_id"], m.get("context"), m.get("created_at", now)))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self._load_graph()
        return {"items_imported": items_imported, "entities_created": entities_created, "relations_rebuilt": relations_rebuilt}

    def close(self):
        """Close the calling thread's connection (other threads' connections
        are released when their thread or the store is garbage-collected)."""
        conn = getattr(self._thread_local, "conn", None)
        if conn is not None:
            conn.close()
            self._thread_local.conn = None
