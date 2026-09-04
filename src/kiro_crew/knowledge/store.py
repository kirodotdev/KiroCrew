"""KnowledgeStore -- SQLite backed knowledge graph with lightweight in-memory graph."""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import uuid4

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

from kiro_crew.on_loop_db import STORE_STRICT_ENV, OnLoopDBGuard

from .._sqlite_compat import fts5_cjk_match_groups, fts5_segment_for_index

logger = logging.getLogger(__name__)

# Marker in a source row's properties for a source Kiro Crew created itself rather
# than the user registering it by hand. Written today by the aggregate row the
# agent's "add document" tool owns, and carried by legacy rows the removed folder
# auto-registration paths left behind. Two readers: the ingestion pipeline, which
# redacts what an auto-added source ingests, and `is_auto_registered` below, which
# the scan funnel consults before it will walk a directory. Lived in the removed
# autosource module until those paths were deleted.
AUTO_ADDED_PROP = "auto_added"

# Marker recording that a row Kiro Crew registered itself has been adopted by the
# user, the feature that registered it no longer existing. Written by
# `retire_auto_registered_folder` when the scan funnel refuses such a row, and by the
# confirm and resume endpoints when the user adopts one; its presence is what keeps a
# later refusal from undoing that decision.
AUTO_REGISTRATION_RETIRED_PROP = "auto_registration_retired"


def is_auto_registered(props: dict) -> bool:
    """True when *props* belong to a source Kiro Crew registered itself, unadopted.

    Both markers are compared with ``is True`` rather than tested for truthiness.
    ``properties`` is user-editable JSON that also arrives through ``import_bundle``,
    and the string ``"false"`` is truthy: on the retired marker that direction is
    FAIL-OPEN -- the row would read as already adopted and skip the refusal below --
    while on the auto-added marker it would retire a folder the user added by hand.
    Every writer in-tree stores a real boolean, so nothing legitimate is excluded.
    """
    return (props.get(AUTO_ADDED_PROP) is True
            and props.get(AUTO_REGISTRATION_RETIRED_PROP) is not True)


# Source types whose scan walks a directory tree, and therefore the only ones
# retirement applies to. The ``agent`` aggregate also carries AUTO_ADDED_PROP -- it is
# auto-added in exactly that sense -- and must not be touched: it holds documents
# handed over one at a time and walks nothing, so gating it would put a Confirm
# control in front of a source that is not a directory. (The ``artifact`` aggregate is
# registered with empty properties and never carries the marker at all; it is excluded
# by type here for the same structural reason.)
_WALKING_SOURCE_TYPES = ("local_folder", "obsidian_vault")

# Every query in this module funnels through the ``db`` property, so one check
# there covers every caller at any stack depth -- including the ones a lexical
# ``async def`` scan cannot see, which is why this guard exists (#7078, the
# interprocedural half of #3057).
#
# Both narrowings below are temporary and exist for the same reason: this store
# still has 85 recorded on-loop callers -- the whole of
# ``.github/sync-io-in-async-baseline.txt``, all of it knowledge paths, owned by
# the cleanup at #7019.
#
# * ``strict_env=STORE_STRICT_ENV`` keeps this store off the SHARED
#   ``KIROCREW_STRICT_ON_LOOP_PERSIST`` switch, which ``setup.py``'s ``test_e2e``
#   and ``ci.yml`` already export into the e2e gateway for history's clean
#   surface. On the shared flag, the on-loop ``/api/knowledge/stats`` and
#   ``/api/knowledge/namespaces`` handlers would raise and 500 the e2e run.
# * ``dev_mode_arms_strict=False`` keeps a developer gateway from raising on that
#   same backlog, which would report tracked work as a regression and push the
#   developer to unset ``KIROCREW_DEV_MODE`` -- silencing history.py's guard too.
#
# When #7019 empties that baseline, delete both arguments and this store joins
# the shared switch.
_ON_LOOP_DB_GUARD = OnLoopDBGuard(
    label="knowledge store",
    remedy=(
        "Offload the call (await asyncio.to_thread(...), or a named lane from "
        "kiro_crew.executors) so the busy wait runs off the loop."
    ),
    strict_env=STORE_STRICT_ENV,
    dev_mode_arms_strict=False,
)

# Bumped when the *term representation* stored in ``items_fts`` changes, which a
# schema probe cannot detect: the CREATE statement is identical either way, only
# the text handed to the index differs. Version 1 segments CJK characters
# (fts5_segment_for_index) so a word inside a spaceless run is addressable.
FTS_INDEX_VERSION = 1


class KnowledgeBundleError(ValueError):
    """A bundle value would commit a corrupt JSON column.

    Raised by :meth:`KnowledgeStore.import_bundle` before any INSERT binds a
    ``sources.properties`` / ``entities.aliases`` value that is not the JSON
    text every reader ``json.loads()`` back.  The dashboard import handler is
    the store's only production caller today; the invariant lives here, with
    the writer, so any future caller (an MCP tool, a CLI import, an app
    backend) is safe by construction instead of depending on one HTTP path's
    pre-validation.
    """


def _validated_json_column(value: object, *, field: str, default: str,
                           shape: type, shape_name: str) -> tuple[str, Any]:
    """Return ``(text, parsed)`` to bind for a store JSON column, or raise.

    ``None`` (and an absent key, which callers pass as ``None``) falls back
    to ``default`` -- the same value the column's schema DEFAULT would
    supply.  Anything present must be JSON text whose parsed value is a
    ``shape`` instance: several readers parse the raw column with
    ``json.loads()`` and no shape guard (source detail handlers index the
    parsed dict; ``find_entity()`` calls ``.lower()`` on each parsed alias),
    so a non-string, an empty string, or the wrong parsed shape commits a
    row that crashes a later, unrelated read.  ``json.loads`` raises
    ``RecursionError`` (not ``ValueError``) on deeply nested input, so it
    is caught alongside.  A lone-surrogate escape (``"\\ud800"``) in the
    outer request JSON decodes to text that ``json.loads`` accepts but the
    SQLite driver cannot UTF-8-encode at bind time, so encodability is
    checked here too -- otherwise the bind raises ``UnicodeEncodeError``
    past the typed-error contract.
    """
    if value is None:
        return default, shape()
    if not isinstance(value, str):
        raise KnowledgeBundleError(f"'{field}' must be a JSON {shape_name} string or null")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise KnowledgeBundleError(f"'{field}' must be valid UTF-8 text") from None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        raise KnowledgeBundleError(f"'{field}' must be valid JSON") from None
    if not isinstance(parsed, shape):
        raise KnowledgeBundleError(f"'{field}' must be a JSON {shape_name}")
    return value, parsed


def _validated_properties(value: object) -> str:
    """``sources.properties``: JSON text parsing to an object, or NULL."""
    text, _ = _validated_json_column(
        value, field="sources.properties", default="{}", shape=dict, shape_name="object")
    return text


def _validated_aliases(value: object) -> str:
    """``entities.aliases``: JSON text parsing to an array of strings, or NULL."""
    text, parsed = _validated_json_column(
        value, field="entities.aliases", default="[]", shape=list, shape_name="array")
    if not all(isinstance(alias, str) for alias in parsed):
        raise KnowledgeBundleError("'entities.aliases' must be a JSON array of strings")
    return text


def _without_sync_status(properties):
    """*properties* with any ``sync_status`` key removed.

    The ``sources.sync_status`` COLUMN is the single source of truth for a
    source's sync state: the dashboard list, the watcher's pre-scan skip and
    ``SyncScheduler.sync_all`` all read it. A ``sync_status`` key inside the
    properties JSON is a SECOND store that only the writer touching it observes
    -- the divergence that let a paused folder go on being walked every sweep
    and a vanished file go on rendering 'synced'.

    Callers may still STATE a status in properties at INSERT time (that is the
    channel ``_initial_sync_status`` reads, under an allowlist); it is dropped
    from what gets persisted, so no row carries two answers. After insert the
    column is written explicitly or not at all: a status found in a properties
    write is discarded rather than applied, because a blob read off a legacy row
    carries a value that is stale by definition, and honouring it would let the
    watcher stamp 'missing' back onto a file it had just re-ingested.

    The input is never mutated. A value that is not a JSON object (legacy
    imports hold arrays), an unparsable blob, and a blob without the key all
    pass through unchanged.
    """
    if isinstance(properties, str):
        try:
            parsed = json.loads(properties)
        except (ValueError, TypeError, RecursionError):
            # RecursionError is a RuntimeError, so it needs naming: json.loads
            # recurses per nesting level, and this helper sits on every insert
            # and update path. A pathologically nested blob is left exactly as
            # it was rather than failing the write.
            return properties
        if not isinstance(parsed, dict) or "sync_status" not in parsed:
            return properties
        return json.dumps(_without_sync_status(parsed))
    if not isinstance(properties, dict) or "sync_status" not in properties:
        return properties
    return {k: v for k, v in properties.items() if k != "sync_status"}


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

# Which column identifies ONE document within a doc-state table. Ownership has to
# be derived per document, and the hash cannot do it: two documents in one source
# may legitimately hold identical text, so a hash-scoped read names one physical
# item into two groups and the first delete of either destroys it. An allowlist
# rather than a caller-supplied column name, because these identifiers are
# interpolated into SQL.
_DOC_STATE_KEY_COL: dict[str, str] = {
    "folder_file_state": "file_path",
    "artifact_item_state": "slug",
    "agent_item_state": "slug",
}

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
        # The FTS index rebuild is deliberately NOT done here. This constructor
        # runs on the event-loop thread (see the note above), and a rebuild is
        # data-scaled, so doing it here would stall the gateway at boot for the
        # length of a full reindex. It is triggered instead by the first reader
        # -- `ensure_fts_index_current` -- which by the same note always runs on
        # a worker thread.
        #
        # Guards the rebuild ONLY, so two reader threads in this process do not
        # each start one. Deliberately not taken on the FTS write path: the
        # rebuild acquires this and then SQLite's writer lock, so a writer that
        # held SQLite's and waited on this one would invert the order and
        # deadlock until busy_timeout. Writes are serialized by SQLite alone --
        # see `_fts_terms_segmented`.
        self._fts_lock = threading.Lock()
        self._fts_index_current = False
        # Which term representation `items_fts` currently holds: True once it is
        # known to be CJK-segmented, None while unknown. Never cached as False --
        # see `_fts_terms_segmented`.
        self._fts_segmented: bool | None = None
        self.graph = SimpleDiGraph()
        # This constructor runs on the event-loop thread by documented design
        # (see the thread-affinity note above). It is not an edge case:
        # `setup_knowledge_routes()` reads the gateway's lazy `knowledge_store`
        # property at route registration, which `start_dashboard` runs BEFORE
        # the socket binds, so construction happens on the loop on every
        # launch. The take is deliberate, so the on-loop guard -- which exists
        # to police reader/writer query paths -- warned spuriously on every
        # boot (#8231). Deliberate is not free, though: `_migrate()` runs an
        # unconditional writer-locked orphan sweep and `_load_graph()`
        # full-scans two tables, both data-scaled (only the FTS rebuild is
        # deferred to the first off-loop reader). Moving that work off the
        # boot path is #8329; suppressing the diagnostic for the sanctioned
        # take is all this block does. The suppression ends with the block:
        # the six non-constructor `_load_graph()` call sites and every query
        # path stay fully guarded.
        with _ON_LOOP_DB_GUARD.allow_on_loop():
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
        _ON_LOOP_DB_GUARD.check()
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
                kind TEXT,
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
        # insert and update), but `import_bundle` used to store a bundle's
        # properties text verbatim, so a row imported before this change can
        # still hold one.
        blob_copies = self.db.execute(
            "SELECT id, properties, sync_status FROM sources").fetchall()
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
            if (row["sync_status"] == "pending" and isinstance(copied, str)
                    and copied != "pending" and copied in self._INITIAL_SYNC_STATUSES):
                self.db.execute(
                    "UPDATE sources SET sync_status = ? "
                    "WHERE id = ? AND sync_status = 'pending' AND properties = ?",
                    (copied, row["id"], row["properties"]))
            self.db.execute(
                "UPDATE sources SET properties = ? WHERE id = ? AND properties = ?",
                (_without_sync_status(row["properties"]), row["id"], row["properties"]))
        if "summary_topic" not in src_cols:
            self.db.execute("ALTER TABLE sources ADD COLUMN summary_topic TEXT")
        if "summary_themes" not in src_cols:
            self.db.execute("ALTER TABLE sources ADD COLUMN summary_themes TEXT")
        # Backfill columns on the document-state tables. Each table itself is
        # created by ``_init_schema``, which runs first on every construction, so
        # only the per-column ALTERs belong here.
        ffs_cols = {r[1] for r in self.db.execute(
            "PRAGMA table_info(folder_file_state)").fetchall()}
        if "status" not in ffs_cols:
            self.db.execute(
                "ALTER TABLE folder_file_state ADD COLUMN status TEXT DEFAULT 'pending'")
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
        # artifact_item_state -- per-artifact item-group tracking for the
        # aggregate "Artifacts" KB source, keyed by artifact slug.
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
        self.db.execute("BEGIN IMMEDIATE")
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
            self._prune_orphan_entities()
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _prune_orphan_entities(self) -> None:
        """Delete entities nothing references any more -- no mention, no relation.

        Every path that removes items or a source has to run this, because an
        entity is only reachable through the rows those paths delete. It takes no
        transaction of its own -- each call site is already inside an open write
        transaction -- and it does not reload the in-memory graph, which the sweep
        can leave holding dropped entities; that stays with whoever owns the
        transaction.
        """
        self.db.execute("""
            DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM mentions)
            AND id NOT IN (SELECT source_id FROM entity_relations)
            AND id NOT IN (SELECT target_id FROM entity_relations)
        """)

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
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO items (id, title, content, item_type, source_id, chunk_index, namespace, summary, tags, embedding, content_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, title, content, item_type, source_id, chunk_index, namespace, summary, tags_json, embedding, content_hash, now, now))
            # Sync FTS: get the rowid of the inserted item
            rowid = self.db.execute("SELECT rowid FROM items WHERE id = ?", (item_id,)).fetchone()[0]
            self._fts_index(rowid, title, content, tags_json)
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
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(f"UPDATE items SET {cols} WHERE id = ?", (*vals, item_id))  # noqa: S608
            # Sync FTS: delete with OLD values, insert with NEW values
            if old_row:
                self._fts_unindex(old_row["rowid"], old_row["title"],
                                  old_row["content"], old_row["tags"])
                new_row = self.db.execute(
                    "SELECT title, content, tags FROM items WHERE id = ?", (item_id,)
                ).fetchone()
                self._fts_index(old_row["rowid"], new_row["title"],
                                new_row["content"], new_row["tags"])
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _delete_item_cascade(self, item_id):
        """Delete item and its dependents without commit/graph reload (for batch use)."""
        row = self.db.execute("SELECT rowid, title, content, tags FROM items WHERE id = ?", (item_id,)).fetchone()
        if row:
            self._fts_unindex(row["rowid"], row["title"], row["content"], row["tags"])
        self.db.execute("DELETE FROM source_locations WHERE item_id = ?", (item_id,))
        self.db.execute("DELETE FROM mentions WHERE item_id = ?", (item_id,))
        self.db.execute("DELETE FROM entity_relations WHERE source_item_id = ?", (item_id,))
        self.db.execute("DELETE FROM items WHERE id = ?", (item_id,))

    def delete_item(self, item_id):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._delete_item_cascade(item_id)
            self._prune_orphan_entities()
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
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.delete_items_batch_in_txn(item_ids, owner_source_id)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self._load_graph()

    def delete_items_batch_in_txn(self, item_ids: list[str],
                                  owner_source_id: str | None = None):
        """The body of :meth:`delete_items_batch`, for a caller already in a write txn.

        Same semantics, minus the transaction and the graph reload, so a caller
        that must delete and then record something ATOMICALLY can put both inside
        one ``BEGIN IMMEDIATE`` -- otherwise the delete commits on its own and a
        concurrent writer can act on the gap. Such a caller owns two duties:
        commit the transaction, and call :meth:`reload_graph` afterwards, because
        the orphan sweep below drops entities the in-memory graph still holds.
        """
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
        self._prune_orphan_entities()

    def reload_graph(self) -> None:
        """Rebuild the in-memory entity graph from the tables.

        For a caller that ran :meth:`delete_items_batch_in_txn` and therefore owes
        the reload that :meth:`delete_items_batch` would have done for it.
        """
        self._load_graph()

    def source_count(self) -> int:
        """Total number of registered sources (all types)."""
        row = self.db.execute("SELECT COUNT(*) AS cnt FROM sources").fetchone()
        return int(row["cnt"]) if row else 0

    def surviving_group_in_txn(self, table: str, source_id: str, key: str) -> list[str]:
        """Items a doc-state row already names and this source still owns.

        The caller must already hold a write transaction, and must not be on the
        event loop: this issues sync sqlite reads whose result is only meaningful
        under that lock.

        Exists because the terminal write for a document the pre-ingest gate
        REFUSED cannot predict its own group. The gate commits before returning,
        so a concurrent ``delete_source_cascade`` on the holder can land in
        between: it reassigns the surviving item to this source and
        :meth:`_adopt_reassigned_item` names it in this very row. Writing an empty
        group afterwards -- which "the gate refused, so this document owns
        nothing" predicts -- erases that, leaving the last copy owned by the
        source but named by no row: unreachable by the delete path, and
        undeletable.

        Row-scoped, never by content hash. Two documents in one source may
        legitimately hold identical text, so a hash-scoped read hands this row the
        OTHER document's items; both rows then name one physical item and deleting
        either destroys it. ``_adopt_reassigned_item`` refuses an ambiguous hash
        for that reason and this must not reintroduce it.

        Filtered to ids that still exist under this source, because the row is
        still carrying the group the gate just deleted. What survives is an
        adoption that landed here.

        An unreadable ``item_ids`` RAISES rather than reporting an empty group: the
        caller writes whatever comes back as the row's terminal state, so mapping
        corruption to "owns nothing" would overwrite a recoverable value and
        orphan every item it named.
        """
        key_col = _DOC_STATE_KEY_COL[table]
        row = self.db.execute(
            f"SELECT item_ids FROM {table} "  # noqa: S608
            f"WHERE source_id = ? AND {key_col} = ?",
            (source_id, key)).fetchone()
        if not row:
            return []
        raw = row["item_ids"]
        if raw in (None, ""):
            return []
        try:
            ids = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{table} item_ids unreadable for {key!r} in source {source_id} "
                f"({exc})") from exc
        if not isinstance(ids, list) or not ids:
            return []
        # Bounded by chunker.MAX_CHUNKS_PER_FILE, so the bind count cannot reach
        # SQLITE_MAX_VARIABLE_NUMBER.
        placeholders = ",".join("?" for _ in ids)
        return [r["id"] for r in self.db.execute(
            f"SELECT id FROM items WHERE id IN ({placeholders}) AND source_id = ?",  # noqa: S608,E501
            (*ids, source_id)).fetchall()]

    def delete_source_cascade(self, source_id):
        """Delete a source and all its items in a single transaction (batch SQL).

        No tombstone is written. One used to be, so a recurring discovery sweep could
        not re-create the auto source a user had just deleted; with both discovery
        loops removed nothing re-creates a source behind the user, so recording the
        deletion would be a write nothing reads. The ``dismissed_auto_sources`` table
        is left in place unused rather than dropped, so no schema migration rides
        along with a feature removal.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
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
                    self._fts_unindex(row["rowid"], row["title"],
                                      row["content"], row["tags"])
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
            self._prune_orphan_entities()
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self._load_graph()

    _FTS_REBUILD_BATCH = 500

    def ensure_fts_index_current(self) -> None:
        """Rebuild ``items_fts`` if it holds a stale term representation.

        Called by each of the three FTS readers before it matches --
        :meth:`search_items_fts`, ``HybridRetriever._keyword_search``, and the
        dashboard's entity-items lookup. Deliberately NOT called
        from ``__init__``: the constructor runs on the event-loop thread, and a
        rebuild is proportional to corpus size, so migrating there would stall
        the gateway at boot for a large legacy library. All three readers run on
        a worker thread
        (``run_in_embed_pool`` / ``asyncio.to_thread``), so the one-time cost
        lands on the first search instead of on startup.

        Steady state is a single boolean check. The first caller takes the lock
        and does the work; concurrent readers wait rather than each starting
        their own rebuild.

        **A migration that cannot get the writer lock is not a search failure.**
        The rebuild opens ``BEGIN IMMEDIATE``, so a concurrent long import can
        hold the lock past ``busy_timeout`` and raise ``OperationalError``.
        Every caller is a reader whose own query already degrades to "no keyword
        hits" on that error, and letting it escape from here instead turns a
        transient lock into an HTTP 500 -- one that the dashboard's entity lookup
        does not even guard. So a lock failure leaves the index at its old
        representation and returns: the read that follows finds legacy terms,
        which costs the CJK recall this PR restores for that one request and
        nothing else, and ``_fts_index_current`` stays False so the next reader
        retries. Only lock/contention errors are absorbed -- a corrupt database
        raises ``DatabaseError``, which is not caught here.
        """
        if self._fts_index_current:
            return
        with self._fts_lock:
            if self._fts_index_current:
                return
            try:
                self._migrate_fts_index()
            except sqlite3.OperationalError:
                logger.warning(
                    "knowledge: FTS index migration could not take the writer lock; "
                    "serving the legacy index for now and retrying on the next read",
                    exc_info=True)
                return
            self._fts_index_current = True

    def _retire_one_in_txn(self, row, props: dict) -> None:
        """Retire ONE candidate row. Caller holds the write lock and has vetted props.

        The marker is written for every candidate; the status moves only for a row
        that could still be scanned, so a paused row keeps the pause the caller read.
        """
        retired = dict(props)
        retired[AUTO_REGISTRATION_RETIRED_PROP] = True
        if row["sync_status"] == "paused":
            self.db.execute(
                "UPDATE sources SET properties = ? "
                "WHERE id = ? AND properties = ? AND sync_status = 'paused'",
                (_without_sync_status(json.dumps(retired)), row["id"],
                 row["properties"]))
        else:
            self.db.execute(
                "UPDATE sources SET sync_status = 'pending_confirmation', "
                "properties = ? WHERE id = ? AND properties = ? AND sync_status = ?",
                (_without_sync_status(json.dumps(retired)), row["id"],
                 row["properties"], row["sync_status"]))
            logger.info(
                "Knowledge source %s was registered automatically by a feature that "
                "no longer exists; it now needs confirmation before it is scanned "
                "again", row["id"],
            )

    def retire_auto_registered_folder(self, source_id: str) -> bool:
        """Retire ONE auto-registered walking source by id. True when it moved.

Called by ``FolderWatcher.scan_source`` when it refuses such a row, which is
        the one funnel every scan goes through. Retiring at scan time rather than at
        store open is what makes the coverage complete AND keeps the write off the
        startup path: a row can arrive at any moment -- :meth:`import_bundle` restores
        a bundle's source rows verbatim, so a bundle from an install that had
        auto-registration enabled re-creates one while the gateway is already up --
        and this takes the write lock, which a constructor-time caller could be
        holding the event loop for.

        Synchronous and takes the write lock, so callers on the event loop hand it to
        ``asyncio.to_thread``. False means nothing moved -- not a candidate, or the
        lock was unavailable -- and the sweep refuses to scan the row either way.
        """
        try:
            self.db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            logger.warning(
                "Could not take the write lock to retire knowledge source %s; the "
                "sweep skips it and the next sweep retries", source_id, exc_info=True)
            return False
        try:
            row = self.db.execute(
                "SELECT id, properties, sync_status, source_type FROM sources WHERE id = ?",
                (source_id,)).fetchone()
            moved = False
            if row and row["source_type"] in _WALKING_SOURCE_TYPES:
                try:
                    props = json.loads(row["properties"] or "{}")
                except (ValueError, TypeError, RecursionError):
                    props = None
                if (isinstance(props, dict)
                        and is_auto_registered(props)):
                    self._retire_one_in_txn(row, props)
                    moved = True
            self.db.execute("COMMIT")
            return moved
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _migrate_fts_index(self) -> None:
        """Re-index ``items_fts`` when its stored term representation is stale.

        Gated on ``PRAGMA user_version`` rather than a schema probe, because the
        ``CREATE VIRTUAL TABLE`` text is identical before and after: what changed
        is the text handed to the index, which SQLite does not record anywhere.
        ``user_version`` is otherwise unused by this database.

        Ordering matters. The version is bumped only after the whole rebuild
        commits, so a crash or a kill part-way through leaves the marker at its
        old value and the next open starts over. A partially rebuilt index is
        therefore always transient, never a resting state.
        """
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= FTS_INDEX_VERSION:
            self._fts_segmented = True
            return
        rows = self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if rows:
            logger.info(
                "knowledge: re-indexing %d item(s) for FTS index format v%d "
                "(CJK-segmented terms)", rows, FTS_INDEX_VERSION)
        # IMMEDIATE so the writer lock is held for the whole rebuild: that is what
        # stops a concurrent writer from reading the old representation and then
        # writing terms the migrated index cannot match.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # 'delete-all' is the documented reset for an external-content table:
            # it drops the index without touching `items`, which holds the data.
            self.db.execute("INSERT INTO items_fts (items_fts) VALUES ('delete-all')")
            # Declared before the re-insert, not after: the rows below are written
            # through _fts_index, which asks _fts_terms_segmented what to write,
            # and PRAGMA user_version is still the old value until this
            # transaction commits.
            self._fts_segmented = True
            last = 0
            while True:
                batch = self.db.execute(
                    "SELECT rowid, title, content, tags FROM items "
                    "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (last, self._FTS_REBUILD_BATCH)).fetchall()
                if not batch:
                    break
                for row in batch:
                    self._fts_index(row["rowid"], row["title"], row["content"], row["tags"])
                    last = row["rowid"]
            self.db.execute(f"PRAGMA user_version = {FTS_INDEX_VERSION:d}")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            # The index is back to whatever it held before, so the declaration
            # has to go back to unknown rather than to False -- another process
            # may have migrated the same database meanwhile.
            self._fts_segmented = None
            raise

    def _fts_terms_segmented(self) -> bool:
        """Whether ``items_fts`` currently holds CJK-segmented terms.

        A writer must use the representation the index already holds, because
        FTS5's ``'delete'`` subtracts the exact terms it is handed: handing
        segmented terms to a not-yet-migrated index raises
        ``DatabaseError: database disk image is malformed``. A legacy database
        has legitimate writers before any reader can migrate it -- the orphan
        reclaim in ``_migrate`` runs inside the constructor, and the startup
        watcher sweep can update or delete an item before the first search --
        so "segment unconditionally" is not available.

        **Serialized by SQLite's writer lock, not by a Python lock.** Every
        caller reads this from inside a ``BEGIN IMMEDIATE`` transaction, and the
        rebuild flips it from inside one too. SQLite admits one writer at a time,
        so a reader of this value already excludes the only thing that can change
        it -- across processes as well as threads, which a Python lock could not
        do. Taking a Python lock here instead would invert against SQLite's:
        a writer holding SQLite's lock would wait on Python's while the
        rebuilding reader holds Python's and waits on SQLite's.

        A True answer is latched, since ``user_version`` only ever increases, so
        the steady state costs nothing. A False answer is deliberately NOT
        cached: another process (an MCP tool server on the same database) may
        migrate it at any time, and a cached False would have this process keep
        writing raw terms into a migrated index.
        """
        if self._fts_segmented:
            return True
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= FTS_INDEX_VERSION:
            self._fts_segmented = True
        return bool(self._fts_segmented)

    def _fts_terms(self, title, content, tags) -> tuple[str, str, str]:
        """The three column values as this database's index represents them."""
        values = (title or "", content or "", tags or "")
        if not self._fts_terms_segmented():
            return values
        return (
            fts5_segment_for_index(values[0]),
            fts5_segment_for_index(values[1]),
            fts5_segment_for_index(values[2]),
        )

    def _fts_index(self, rowid, title, content, tags) -> None:
        """Index one item's row in the representation the index holds.

        The single write path into ``items_fts``. Centralised because the
        representation is not a per-call-site choice: an index built from
        segmented text and probed with un-segmented text does not match, and the
        reverse raises.

        Holds no Python lock, by design -- see ``_fts_terms_segmented``. Callers
        must already own SQLite's writer lock (``BEGIN IMMEDIATE``).
        """
        self.db.execute(
            "INSERT INTO items_fts (rowid, title, content, tags) VALUES (?, ?, ?, ?)",
            (rowid, *self._fts_terms(title, content, tags)))

    def _fts_unindex(self, rowid, title, content, tags) -> None:
        """Remove one item's row from ``items_fts``.

        FTS5's ``'delete'`` command subtracts the terms it is given, so it has to
        be given the same text that was indexed. Passing the wrong
        representation either leaves the original terms in the index -- which
        keeps serving deleted or superseded content as live hits, and which
        ``'integrity-check'`` does not flag -- or raises
        ``database disk image is malformed`` outright.

        Holds no Python lock, by design -- see ``_fts_terms_segmented``. Callers
        must already own SQLite's writer lock (``BEGIN IMMEDIATE``).
        """
        self.db.execute(
            "INSERT INTO items_fts (items_fts, rowid, title, content, tags) "
            "VALUES ('delete', ?, ?, ?, ?)",
            (rowid, *self._fts_terms(title, content, tags)))

    def search_items_fts(self, query, limit=10, offset=0) -> list:
        self.ensure_fts_index_current()
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

    @staticmethod
    def _sanitize_fts5(query: str) -> str:
        """Escape user input for FTS5 MATCH, ANDing the query's tokens.

        Tokens stay individually quoted so the user's input can never contribute
        FTS5 operators. CJK runs expand to their adjacent-character phrases
        (``fts5_cjk_match_groups``) because a spaceless run is one whitespace
        token but several words; non-CJK input is unchanged. The join is AND:
        this is the store's direct-search surface, where every typed word is
        taken as deliberate.
        """
        return " AND ".join(fts5_cjk_match_groups(query))

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

    # States a sources row may legitimately START in: the DURABLE ones, which a
    # caller (or a restored bundle) can assert about a source before any work has
    # run. The transient and outcome states -- syncing/synced/error/missing --
    # are claims about work, so only the operation that did the work may write
    # them: persisting a caller-supplied 'syncing' would make the sync endpoint
    # report a conflict forever for a source whose sync never started.
    _INITIAL_SYNC_STATUSES = frozenset({"pending", "pending_confirmation", "active", "paused"})

    @staticmethod
    def _initial_sync_status(properties) -> str:
        """The sync_status column value a new sources row starts with.

        The dashboard reads the sync_status COLUMN (list_sources serves
        SELECT s.*), while callers express the intended initial state inside
        the properties JSON. Both insert paths persist the column from the
        same value so a freshly-added source renders the control matching its
        state: a column left at its 'pending' default while properties says
        'pending_confirmation' hides the Confirm button that starts the scan.
        Values outside the initial-state allowlist fall back to 'pending'.
        """
        if isinstance(properties, dict):
            return KnowledgeStore._initial_status_or_default(properties.get("sync_status"))
        return "pending"

    @staticmethod
    def _initial_status_or_default(status) -> str:
        """*status* if a row may legitimately start there, else 'pending'.

        The allowlist itself, shared by every insert path so a status arriving
        through the properties blob and one restored from a bundle's column are
        held to the same rule.
        """
        if isinstance(status, str) and status in KnowledgeStore._INITIAL_SYNC_STATUSES:
            return status
        return "pending"

    def add_source(self, name, source_type, uri, **kwargs) -> str:
        sid = str(uuid4())
        now = datetime.now().isoformat()
        properties = kwargs.get("properties", {})
        stored = _without_sync_status(properties)
        self.db.execute(
            "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, name, source_type, uri, json.dumps(stored),
             self._initial_sync_status(properties), now, now))
        self.db.commit()
        return sid

    def get_source_by_uri(self, uri):
        row = self.db.execute("SELECT * FROM sources WHERE uri = ?", (uri,)).fetchone()
        return dict(row) if row else None

    _SOURCE_COLUMNS = {"name", "source_type", "uri", "properties", "last_synced", "sync_status", "updated_at"}

    def update_source(self, source_id, **fields):
        """Write *fields* to a sources row.

        ``if_sync_status`` makes the write a compare-and-set on the status
        column: the row is written only while it still reads that value. A caller
        deriving a status from a SNAPSHOT it took earlier must pass it, because
        the row can move in between -- a sweep that observed 'missing' and then
        writes 'synced' would otherwise overwrite the 'error' a manual sync
        recorded in the meantime. A caller writing the outcome of something that
        just happened has current information and does not need it.
        """
        expected = fields.pop("if_sync_status", None)
        if not fields:
            return
        if "properties" in fields:
            # The blob is not a place a status can live. Dropping it here means a
            # legacy row's second copy disappears the first time anything writes
            # its properties, and no caller can mint a new one. It is DROPPED,
            # not applied to the column: a blob read off a legacy row carries a
            # stale value, so honouring it would let the watcher stamp 'missing'
            # back onto a file it had just re-ingested. A transition passes
            # sync_status= explicitly.
            fields["properties"] = _without_sync_status(fields["properties"])
        fields["updated_at"] = datetime.now().isoformat()
        safe = {k: v for k, v in fields.items() if k in self._SOURCE_COLUMNS}
        if not safe:
            return
        cols = ", ".join(f"{k} = ?" for k in safe)
        vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in safe.values()]
        sql = f"UPDATE sources SET {cols} WHERE id = ?"  # noqa: S608
        params: list = [*vals, source_id]
        if expected is not None:
            # IS, not =, so a NULL column compares as a value rather than
            # silently matching nothing.
            sql += " AND sync_status IS ?"
            params.append(expected)
        self.db.execute(sql, params)
        self.db.commit()

    def add_source_location(self, item_id, source_id, chunk_range=None, section_title=None, anchor=None):
        """Record that *source_id* holds *item_id*, at an optional position within it.

        ``OR IGNORE`` against ``UNIQUE (item_id, source_id)``: a document reachable
        from two sources has one row per source, and re-attaching a pair that already
        exists is a no-op. Attaching a second source is what keeps the item alive when
        the first is deleted -- see ``sources_holding_item``.
        """
        self.add_source_location_in_txn(
            item_id, source_id, chunk_range=chunk_range,
            section_title=section_title, anchor=anchor)
        self.db.commit()

    def add_source_location_in_txn(self, item_id, source_id, chunk_range=None,
                                   section_title=None, anchor=None):
        """:meth:`add_source_location` without the commit, for a caller in a write txn.

        The connection runs in autocommit mode, so ``db.commit()`` inside an
        explicit ``BEGIN IMMEDIATE`` would END that transaction early and hand a
        concurrent writer the very gap the caller took the lock to close.
        """
        lid = str(uuid4())
        now = datetime.now().isoformat()
        self.db.execute(
            "INSERT OR IGNORE INTO source_locations "
            "(id, item_id, source_id, chunk_range, section_title, anchor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lid, item_id, source_id, chunk_range, section_title, anchor, now))

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
        entity_id_set = set(entity_ids)
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
                if r["id"] in seen_ids:
                    continue
                # A relation whose OTHER endpoint isn't among this item's
                # mentioned entities, or that was recorded under a different
                # item's observation (source_item_id), would re-import
                # referencing an entity/item this single-item bundle never
                # carries -- an FK violation on the receiving end. Only keep
                # relations fully contained in what this bundle exports.
                if r["source_id"] not in entity_id_set or r["target_id"] not in entity_id_set:
                    continue
                if r["source_item_id"] not in (None, item_id):
                    continue
                seen_ids.add(r["id"])
                relations.append(r)
        locations = [dict(r) for r in self.db.execute(
            "SELECT * FROM source_locations WHERE item_id = ?", (item_id,))]
        mentions = [dict(r) for r in self.db.execute(
            "SELECT * FROM mentions WHERE item_id = ?", (item_id,))]
        source_ids = {sid for sid in (item.get("source_id"), *(loc["source_id"] for loc in locations)) if sid}
        sources = []
        for sid in source_ids:
            row = self.db.execute("SELECT * FROM sources WHERE id = ?", (sid,)).fetchone()
            if row:
                sources.append(dict(row))
        return {
            "items": [item],
            "sources": sources,
            "entities": entities,
            "relations": relations,
            "source_locations": locations,
            "mentions": mentions,
        }

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
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for src in bundle.get("sources", []):
                # Restore the status from the COLUMN, which ``export_all`` ships
                # (it serializes SELECT * FROM sources). Reading the blob copy
                # instead would land every bundle exported from a fixed store at
                # the 'pending' default -- there is no copy there any more -- and
                # silently resume a folder the user had paused. A bundle written
                # before this change has the blob copy and no column, so fall
                # back to it. Both go through the same allowlist as the other
                # insert paths: a bundle is untrusted input, and a restored
                # 'syncing' would report a conflict forever for a sync that
                # never started.
                #
                # The blob is then stripped like every other insert path: after
                # an insert the column is the only place a status lives, and a
                # value the allowlist just refused has no business surviving
                # inside the row it was refused from. The migration would retire
                # such a key on the next open without ever promoting it, so this
                # is the boundary holding, not a second line of defence.
                props_text = _validated_properties(src.get("properties"))
                restored = src.get("sync_status")
                if not isinstance(restored, str) or not restored:
                    restored = json.loads(props_text or "{}").get("sync_status")
                # A source whose scan WALKS A TREE is never restored as scannable,
                # whatever the bundle says. The allowlist above admits 'active', and a
                # bundle is untrusted input that names its own uri -- so restoring the
                # claimed status would let an imported row point at any readable
                # directory and have the next sweep walk it and spend extraction calls
                # on it, with nobody having asked for that folder. The user chose to
                # import the bundle; they did not thereby choose each directory inside
                # it. 'pending_confirmation' is the same state the add-source endpoint
                # uses, so the row keeps its items and its properties and waits behind
                # the same Confirm control. Aggregate and single-file sources are
                # unaffected: they walk nothing.
                if (src.get("source_type") in _WALKING_SOURCE_TYPES
                        and restored != "paused"):
                    restored = "pending_confirmation"
                self.db.execute(
                    "INSERT OR IGNORE INTO sources (id, name, source_type, uri, properties, "
                    "sync_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (src["id"], src["name"], src["source_type"], src["uri"],
                     _without_sync_status(props_text),
                     self._initial_status_or_default(restored),
                     src.get("created_at", now), now))
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
                        self._fts_index(row[0], item["title"], item["content"],
                                        item.get("tags", "[]"))
            for ent in bundle.get("entities", []):
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO entities (id, name, entity_type, description, aliases, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ent["id"], ent["name"], ent["entity_type"], ent.get("description"),
                     _validated_aliases(ent.get("aliases")),
                     ent.get("created_at", now), now))
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
