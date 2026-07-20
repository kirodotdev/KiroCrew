"""KnowledgeStore -- SQLite backed knowledge graph with lightweight in-memory graph."""

from __future__ import annotations

import base64
import json
import threading
from collections import defaultdict
from datetime import datetime
from uuid import uuid4

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3


class _NodeView:
    """Minimal node-attribute view supporting get, subscript, iteration, and len."""

    def __init__(self, data: dict[str, dict]):
        self._data = data

    def get(self, nid: str, default: dict | None = None) -> dict:
        return self._data.get(nid, default if default is not None else {})

    def __getitem__(self, nid: str) -> dict:
        return self._data[nid]

    def __contains__(self, nid: str) -> bool:
        return nid in self._data

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class _EdgeView:
    """Minimal edge view supporting iteration and subscript access."""

    def __init__(self, fwd: dict[str, dict[str, dict]]):
        self._fwd = fwd

    def __getitem__(self, key: tuple[str, str]) -> dict:
        u, v = key
        return self._fwd[u][v]

    def __call__(self, *, data: bool = False):  # noqa: ARG002
        for u, targets in self._fwd.items():
            for v, attrs in targets.items():
                yield u, v, attrs


class SimpleDiGraph:
    """Minimal directed graph replacing networkx.DiGraph for the subset of API we use."""

    def __init__(self) -> None:
        self._node_attrs: dict[str, dict] = {}
        self._fwd: dict[str, dict[str, dict]] = defaultdict(dict)
        self._rev: dict[str, dict[str, dict]] = defaultdict(dict)
        self.nodes = _NodeView(self._node_attrs)
        self.edges = _EdgeView(self._fwd)

    def clear(self) -> None:
        self._node_attrs.clear()
        self._fwd.clear()
        self._rev.clear()

    def add_node(self, nid: str, **attrs: object) -> None:
        self._node_attrs[nid] = attrs

    def add_edge(self, u: str, v: str, **attrs: object) -> None:
        self._fwd[u][v] = attrs
        self._rev[v][u] = attrs

    def has_edge(self, u: str, v: str) -> bool:
        return v in self._fwd.get(u, {})

    def has_node(self, nid: str) -> bool:
        return nid in self._node_attrs

    def degree(self, nid: str) -> int:
        return len(self._fwd.get(nid, {})) + len(self._rev.get(nid, {}))

    def successors(self, nid: str):
        return iter(self._fwd.get(nid, {}))

    def predecessors(self, nid: str):
        return iter(self._rev.get(nid, {}))


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

            CREATE TABLE IF NOT EXISTS source_locations (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL REFERENCES items(id),
                source_id TEXT NOT NULL REFERENCES sources(id),
                chunk_range TEXT,
                section_title TEXT,
                anchor TEXT,
                created_at TEXT NOT NULL
            );

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

            CREATE TABLE IF NOT EXISTS folder_file_state (
                source_id TEXT NOT NULL REFERENCES sources(id),
                file_path TEXT NOT NULL,
                content_hash TEXT,
                mtime REAL,
                item_ids TEXT DEFAULT '[]',
                last_seen TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                PRIMARY KEY (source_id, file_path)
            );

            CREATE TABLE IF NOT EXISTS artifact_item_state (
                source_id TEXT NOT NULL REFERENCES sources(id),
                slug TEXT NOT NULL,
                content_hash TEXT,
                item_ids TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL,
                name TEXT,
                PRIMARY KEY (source_id, slug)
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
                    mtime REAL,
                    item_ids TEXT DEFAULT '[]',
                    last_seen TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT,
                    PRIMARY KEY (source_id, file_path)
                )
            """)
        else:
            ffs_cols = {r[1] for r in self.db.execute("PRAGMA table_info(folder_file_state)").fetchall()}
            if "status" not in ffs_cols:
                self.db.execute("ALTER TABLE folder_file_state ADD COLUMN status TEXT DEFAULT 'pending'")
            if "error_message" not in ffs_cols:
                self.db.execute("ALTER TABLE folder_file_state ADD COLUMN error_message TEXT")
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
                    PRIMARY KEY (source_id, slug)
                )
            """)
        else:
            ais_cols = {r[1] for r in self.db.execute(
                "PRAGMA table_info(artifact_item_state)").fetchall()}
            if "name" not in ais_cols:
                self.db.execute("ALTER TABLE artifact_item_state ADD COLUMN name TEXT")
        # Clean orphan sources (no items), entities (no mentions/relations), and stale relations
        self.db.execute("BEGIN")
        try:
            orphan_sources_q = "SELECT id FROM sources WHERE id NOT IN (SELECT DISTINCT source_id FROM items WHERE source_id IS NOT NULL) AND id NOT IN (SELECT source_id FROM ingestion_jobs WHERE status IN ('pending', 'processing')) AND id NOT IN (SELECT DISTINCT source_id FROM folder_file_state) AND id NOT IN (SELECT DISTINCT source_id FROM artifact_item_state)"
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

    def delete_items_batch(self, item_ids: list[str]):
        """Delete multiple items in a single transaction with one graph reload."""
        if not item_ids:
            return
        self.db.execute("BEGIN")
        try:
            for item_id in item_ids:
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

    def delete_source_cascade(self, source_id):
        """Delete a source and all its items in a single transaction (batch SQL)."""
        self.db.execute("BEGIN")
        try:
            # Batch FTS cleanup
            rows = self.db.execute(
                "SELECT rowid, title, content, tags FROM items WHERE source_id = ?", (source_id,)
            ).fetchall()
            for row in rows:
                self.db.execute(
                    "INSERT INTO items_fts (items_fts, rowid, title, content, tags) VALUES ('delete', ?, ?, ?, ?)",
                    (row["rowid"], row["title"], row["content"], row["tags"]))
            # Batch delete dependents
            self.db.execute("DELETE FROM source_locations WHERE item_id IN (SELECT id FROM items WHERE source_id = ?)", (source_id,))
            self.db.execute("DELETE FROM mentions WHERE item_id IN (SELECT id FROM items WHERE source_id = ?)", (source_id,))
            self.db.execute("DELETE FROM entity_relations WHERE source_item_id IN (SELECT id FROM items WHERE source_id = ?)", (source_id,))
            self.db.execute("DELETE FROM items WHERE source_id = ?", (source_id,))
            self.db.execute("DELETE FROM ingestion_jobs WHERE source_id = ?", (source_id,))
            self.db.execute("DELETE FROM folder_file_state WHERE source_id = ?", (source_id,))
            self.db.execute("DELETE FROM artifact_item_state WHERE source_id = ?", (source_id,))
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
        lid = str(uuid4())
        now = datetime.now().isoformat()
        self.db.execute(
            "INSERT INTO source_locations (id, item_id, source_id, chunk_range, section_title, anchor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lid, item_id, source_id, chunk_range, section_title, anchor, now))
        self.db.commit()

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
