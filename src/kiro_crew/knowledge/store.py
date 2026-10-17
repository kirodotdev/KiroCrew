"""KnowledgeStore -- SQLite backed knowledge graph with lightweight in-memory graph."""

from __future__ import annotations

# Bound for tests that patch ``kiro_crew.knowledge.store.json.loads``.
import json  # noqa: F401
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kiro_crew.on_loop_db import STORE_STRICT_ENV, OnLoopDBGuard
from kiro_crew.owner_only_files import prepare_owner_only_sqlite

from .._sqlite_compat import fts5_cjk_match_groups, fts5_segment_for_index, sqlite3  # noqa: F401
from ..sqlite_quarantine import (
    SidecarNotMovable,
    is_damaged_database_error,
    quarantine_sqlite_file,
)
from .store_parts import bundle_ownership as _bundle_ownership
from .store_parts import bundle_transfer as _bundle_transfer
from .store_parts import content_stats as _content_stats
from .store_parts import entity_graph as _entity_graph
from .store_parts import fulltext_index as _fulltext_index
from .store_parts import item_ownership as _item_ownership
from .store_parts import item_rows as _item_rows
from .store_parts import maintenance_sweeps as _maintenance_sweeps
from .store_parts import schema_migrations as _schema_migrations
from .store_parts import source_rows as _source_rows
from .store_parts.bundle_ownership import _BUNDLE_STATE_CARRIED_COLS  # noqa: F401
from .store_parts.bundle_ownership import _BUNDLE_STATE_REQUIRED_COLS  # noqa: F401
from .store_parts.bundle_ownership import _BUNDLE_STATE_RESTORED_TABLES  # noqa: F401
from .store_parts.bundle_ownership import _MAX_SQL_PARAMS  # noqa: F401
from .store_parts.bundle_ownership import _bundle_state_restores  # noqa: F401
from .store_parts.bundle_ownership import _bundle_state_text  # noqa: F401
from .store_parts.bundle_ownership import _sql_param_chunks  # noqa: F401
from .store_parts.bundle_transfer import _MAX_ITEM_GROUP_MEMBERS  # noqa: F401
from .store_parts.bundle_transfer import _MAX_ITEM_ID_CHARS  # noqa: F401
from .store_parts.bundle_transfer import BUNDLE_STATE_KEY_COL  # noqa: F401
from .store_parts.bundle_transfer import KnowledgeBundleError  # noqa: F401
from .store_parts.bundle_transfer import _bundle_item_group  # noqa: F401
from .store_parts.bundle_transfer import _validated_aliases  # noqa: F401
from .store_parts.bundle_transfer import _validated_embedding_sig  # noqa: F401
from .store_parts.bundle_transfer import _validated_json_column  # noqa: F401
from .store_parts.bundle_transfer import _validated_properties  # noqa: F401
from .store_parts.content_stats import ContentStats  # noqa: F401
from .store_parts.content_stats import SourceContentStats  # noqa: F401
from .store_parts.entity_graph import SimpleDiGraph  # noqa: F401
from .store_parts.entity_graph import _EdgeView  # noqa: F401
from .store_parts.entity_graph import _NodeView  # noqa: F401
from .store_parts.fulltext_index import FTS_INDEX_VERSION  # noqa: F401
from .store_parts.item_ownership import _DOC_STATE_KEY_COL  # noqa: F401
from .store_parts.item_ownership import _DOC_STATE_TABLES  # noqa: F401
from .store_parts.item_ownership import _OWNERSHIP_HASH_COL  # noqa: F401
from .store_parts.maintenance_sweeps import _RECLAIM_CHUNK  # noqa: F401
from .store_parts.source_rows import _WALKING_SOURCE_TYPES  # noqa: F401
from .store_parts.source_rows import AUTO_ADDED_PROP  # noqa: F401
from .store_parts.source_rows import AUTO_REGISTRATION_RETIRED_PROP  # noqa: F401
from .store_parts.source_rows import _without_sync_status  # noqa: F401
from .store_parts.source_rows import is_auto_registered  # noqa: F401

#: The ``store_parts`` owners ``KnowledgeStore`` binds its methods from, lowest layer
#: first: each imports only the owners listed before it (``store_parts/__init__.py``).
_STORE_OWNERS: tuple[str, ...] = (
    "kiro_crew.knowledge.store_parts.entity_graph",
    "kiro_crew.knowledge.store_parts.fulltext_index",
    "kiro_crew.knowledge.store_parts.source_rows",
    "kiro_crew.knowledge.store_parts.item_ownership",
    "kiro_crew.knowledge.store_parts.item_rows",
    "kiro_crew.knowledge.store_parts.schema_migrations",
    "kiro_crew.knowledge.store_parts.maintenance_sweeps",
    "kiro_crew.knowledge.store_parts.content_stats",
    "kiro_crew.knowledge.store_parts.bundle_transfer",
    "kiro_crew.knowledge.store_parts.bundle_ownership",
)

#: Test-only switch. ``False`` in production: every connection keeps SQLite's
#: own thread-affinity guard (``check_same_thread=True``), so a caller that
#: caches ``store.db`` and uses it from another thread is refused with
#: ``ProgrammingError`` instead of racing the owner. ``test/conftest.py`` flips
#: this to ``True`` once per session so ``_close_all_for_tests()`` can close the
#: handles other (usually exited) threads opened -- the one operation the
#: default guard refuses that a teardown needs, since an unclosed connection is
#: a reference cycle on CPython 3.11+ and holds its descriptors until the
#: cyclic collector runs. Only the ``test/`` testpath loads that conftest; the
#: app test trees under ``src/kiro_crew/apps/builtins`` do not, so they never
#: activate the flip themselves (a ``test/`` case run earlier in the same worker
#: leaves it on). Nothing in production may set it.
_ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS = False


class _ThreadAffineTestConnection(sqlite3.Connection):
    """A test-mode connection that keeps the thread-affinity guard in Python.

    Opened with ``check_same_thread=False`` so a teardown on another thread may
    ``close()`` it, but every statement entry point the store uses --
    ``cursor``, ``execute``, ``executemany``, ``executescript``, ``commit`` and
    ``rollback`` -- refuses a thread that did not open it with the same
    ``ProgrammingError`` SQLite raises in production, so the test suite still
    catches a caller that caches ``store.db`` and uses it from a worker. The
    connection-level paths the store never takes (``with conn:``, ``backup``,
    ``iterdump``, ``blobopen``) and the methods of an already-created cursor are
    NOT re-guarded. Only ever constructed under ``_ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS``.
    """

    _owner_ident: int = -1

    def _check_owner(self) -> None:
        current = threading.get_ident()
        if current != self._owner_ident:
            raise sqlite3.ProgrammingError(
                "SQLite objects created in a thread can only be used in that same thread. "
                f"The object was created in thread id {self._owner_ident} and this is "
                f"thread id {current}."
            )

    def cursor(self, *args, **kwargs):  # type: ignore[override]
        self._check_owner()
        return super().cursor(*args, **kwargs)

    def execute(self, *args, **kwargs):  # type: ignore[override]
        self._check_owner()
        return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):  # type: ignore[override]
        self._check_owner()
        return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):  # type: ignore[override]
        self._check_owner()
        return super().executescript(*args, **kwargs)

    def commit(self):  # type: ignore[override]
        self._check_owner()
        return super().commit()

    def rollback(self):  # type: ignore[override]
        self._check_owner()
        return super().rollback()


logger = logging.getLogger(__name__)

#: How long ``maintenance_window`` waits for in-flight ingestion to drain
#: before giving the sweep up for this launch.
MAINTENANCE_WAIT_SECS = 600.0


class IngestionGate:
    """Reader/writer gate between ingestion and store maintenance.

    Ingestion writes a source in several autocommit steps -- the source row,
    its ingestion job, its items, its entities and their mentions -- and the
    orphan sweep's predicates read exactly those half-states as orphans: a
    source without items, an entity without a mention. The sweep runs after the
    listener is up, concurrently with requests, so the two need an ordering
    that is not a clock: ``ingestion_in_flight()`` brackets one whole ingest
    (many may hold it at once) and ``maintenance_window()`` waits until no
    holder remains, then holds the sweep's turn, during which a NEW ingest
    waits at its entry rather than starting under the sweep.

    The wait is bounded. Ingestion that never drains inside ``timeout`` makes
    the window yield ``False`` -- the caller skips its sweep and the next
    launch retries -- rather than either side killing the other. While the
    window is waiting, new ingestion is already held back, so a steady stream
    of ingests cannot starve the sweep indefinitely; a single long ingest can,
    and that is the bounded case.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._ingesting = 0
        self._maintenance = False

    @contextmanager
    def ingestion_in_flight(self, *, admitted: bool = False) -> Iterator[None]:
        """Hold the gate for one ingest.

        *admitted* is for a hold handed on from a current holder to a task it
        starts: the holder's own hold means no sweep is running, so the new
        hold joins it without waiting for a maintenance window that may have
        begun waiting in between -- waiting there would hold the parent's
        release hostage to the window's timeout (the window waits for the
        parent; the parent waits for this entry). An admitted hold still counts,
        so the window keeps waiting for it like any other.
        """
        with self._cond:
            while self._maintenance and not admitted:
                self._cond.wait()
            self._ingesting += 1
        try:
            yield
        finally:
            with self._cond:
                self._ingesting -= 1
                if self._ingesting == 0:
                    self._cond.notify_all()

    @contextmanager
    def maintenance_window(self, timeout: float = MAINTENANCE_WAIT_SECS) -> Iterator[bool]:
        """Yield ``True`` with the store quiescent, ``False`` if it never drained."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._maintenance:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._cond.wait(remaining):
                    break
            if self._maintenance:
                logger.warning(
                    "Knowledge maintenance skipped: another maintenance window "
                    "held for %.0fs", timeout)
                yield False
                return
            self._maintenance = True
            while self._ingesting > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._cond.wait(remaining):
                    break
            if self._ingesting > 0:
                self._maintenance = False
                self._cond.notify_all()
                logger.warning(
                    "Knowledge maintenance skipped: %d ingestion(s) still in flight "
                    "after %.0fs", self._ingesting, timeout)
                yield False
                return
        try:
            yield True
        finally:
            with self._cond:
                self._maintenance = False
                self._cond.notify_all()


# Every query the store runs funnels through the ``db`` property, so one check
# there covers every caller at any stack depth -- including the ones a lexical
# ``async def`` scan cannot see, which is why this guard exists.
#
# Both narrowings below are temporary and exist for the same reason: this store
# still has on-loop callers left -- the watcher's marked cancel-path finalize
# (``# on-loop-io-ok`` in ``knowledge/watcher.py``; the lexical baseline in
# ``.github/sync-io-in-async-baseline.txt`` is now empty, and a marker is an
# exemption, not an offload).
#
# ``dashboard/handlers/knowledge.py`` takes the store through a worker for every
# take of its OWN, endpoints and background tasks alike. ``artifact_ingest.py``'s
# job-status reads run on a worker too: ``ingest_artifact`` reads inside its
# ``_settle`` unit under ``run_to_completion``, and ``reconcile_artifacts``
# reads through ``asyncio.to_thread``.
#
# Two takes stay inline, carried as ``# on-loop-io-ok`` markers, and the
# lexical baseline is empty. The watcher's self-heal rebuild
# finalizes its job row inline on its cancellation path, where an interrupted
# ``to_thread`` could drop the write -- ``start_rebuild_job`` sweeps a stale
# 'processing' row to 'abandoned', so the single-flight guard recovers either
# way. And ``dashboard/state.py`` builds this store lazily, whose migrations run
# under ``allow_on_loop()`` below.
#
# * ``strict_env=STORE_STRICT_ENV`` keeps this store off the SHARED
#   ``KIROCREW_STRICT_ON_LOOP_PERSIST`` switch, which ``setup.py``'s ``test_e2e``
#   and ``ci.yml`` already export into the e2e gateway for history's clean
#   surface. On the shared flag, the watcher's finalize would raise inside the
#   e2e gateway.
# * ``dev_mode_arms_strict=False`` keeps a developer gateway from raising on that
#   same backlog, which would report tracked work as a regression and push the
#   developer to unset ``KIROCREW_DEV_MODE`` -- silencing history_offload.py's guard too.
#
# When that baseline is empty, delete both arguments and this store joins
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


class KnowledgeStore:
    def __init__(self, db_path: str, *, read_only: bool = False):
        self._db_path = db_path
        # Notices for the user about how this library was opened (a damaged
        # file moved aside), the same channel ``TaskStore.warnings`` is. The
        # gateway shows each one on the dashboard.
        self.warnings: list[str] = []
        # A read-only store runs neither the schema DDL nor `_migrate()` and opens
        # every connection with SQLite `mode=ro`, so a write is refused by the
        # engine rather than by convention -- see `open_read_only`.
        self._read_only = read_only
        # One connection PER THREAD. sqlite3 connections carry
        # thread affinity (check_same_thread=True by default), but callers
        # like HybridRetriever.search() run on worker threads via
        # run_in_embed_pool / asyncio.to_thread while the store is created
        # on another thread. A shared connection raises
        # sqlite3.ProgrammingError from those workers (HTTP 500 on
        # /api/knowledge/search-for-context). WAL mode (below) supports
        # concurrent readers alongside a single writer, and busy_timeout
        # serializes rare cross-thread writes.
        self._thread_local = threading.local()
        # Every connection this store has opened, on any thread, so `_close_all_for_tests()`
        # can release them all. Without this a connection a worker thread opened had
        # no close path at all: on CPython 3.11+ an unclosed `sqlite3.Connection`
        # is a reference CYCLE (its statement cache is an `lru_cache` wrapping the
        # connection itself), so dropping the store does not free the descriptor
        # -- only the cyclic collector eventually does. `_generation` is bumped
        # by `_close_all_for_tests()`; a thread whose cached connection predates it reopens
        # lazily instead of touching a handle another thread closed.
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._generation = 0
        # The FTS index rebuild is deliberately NOT done here. This constructor
        # runs on the gateway's boot path (see the construction note below),
        # and a rebuild is data-scaled, so doing it here would stall the
        # gateway at boot for the length of a full reindex. It is triggered
        # instead by the first reader -- `ensure_fts_index_current` -- which
        # always runs on a worker thread.
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
        # The entity graph is materialised on first READ, not here -- see
        # `ensure_graph_loaded`. `_graph` is the backing store for the `graph`
        # property; nothing outside `_load_graph` and that property should touch
        # it. RE-ENTRANT because both the first-touch accessor and `_load_graph`
        # itself acquire it: the accessor holds it across the call so two readers
        # cannot each start a scan, and `_load_graph` acquires it again so that
        # EVERY rebuild -- including the six mutation-refresh call sites, which
        # hold no lock of their own -- serializes against every other. A plain
        # `Lock` would self-deadlock on that nesting.
        self._graph = SimpleDiGraph()
        self._graph_loaded = False
        self._graph_lock = threading.RLock()
        # Orders ingestion against the deferred orphan sweep -- see
        # `IngestionGate`, `ingestion_in_flight` and `maintenance_window`.
        self._ingestion_gate = IngestionGate()
        # This constructor sits on the gateway's boot path: `start_dashboard`
        # builds the lazy `knowledge_store` on a worker thread just before
        # `setup_knowledge_routes()` reads it, BEFORE the socket binds, so it
        # runs on every launch. Other callers (a CLI command, a test, a handler
        # harness) may still build a store on an event-loop thread. That take
        # is deliberate, so the on-loop guard -- which exists to police
        # reader/writer query paths -- would warn spuriously on it. Being on
        # the boot path is not free, though, so neither data-scaled piece of
        # construction sits on the boot path: `_load_graph()` is deferred to
        # the first graph reader (`ensure_graph_loaded`), the same shape the
        # FTS rebuild uses, and the writer-locked orphan sweep is
        # `reclaim_orphans()`, kicked from a worker thread by `start_dashboard`
        # once the listener is up. What remains here is the schema DDL and the
        # per-column ALTERs, which are O(schema), not O(data).
        # The suppression ends with the block: the six non-constructor
        # `_load_graph()` call sites and every query path stay fully guarded.
        if read_only:
            return
        try:
            with _ON_LOOP_DB_GUARD.allow_on_loop():
                self._init_schema()
                self._migrate()
        except BaseException:
            # The store is never handed out, so its connection would only be
            # freed by the cyclic collector -- and an open handle keeps the
            # file from being moved aside on Windows.
            conn = getattr(self._thread_local, "conn", None)
            if conn is not None:
                conn.close()
                self._thread_local.conn = None
            raise

    @classmethod
    def open_recovering(cls, db_path: str) -> "KnowledgeStore":
        """Open the library, moving a damaged file aside instead of failing.

        The gateway builds its store before the listener binds, so a file SQLite
        reports as damaged (``file is not a database``, ``database disk image is
        malformed``) would stop every start. Such a file and its sidecars are
        renamed to ``knowledge.db.corrupt-<utc>`` and an empty library is
        created. The old file is kept, never deleted, and one warning names it,
        in the log and on :attr:`warnings` for the dashboard. When its
        ``-wal`` or ``-journal`` will not move, nothing moves and the error is
        raised: a new library would overwrite those pages. Any other error --
        a locked, busy, read-only or full database -- is raised unchanged.
        """
        try:
            return cls(db_path)
        except sqlite3.DatabaseError as exc:
            if not is_damaged_database_error(exc):
                raise
            reason = str(exc)
        try:
            moved = quarantine_sqlite_file(Path(db_path), data_sidecars_must_move=True)
        except SidecarNotMovable as exc:
            logger.warning(
                "knowledge: %s is damaged (%s) and was kept in place, not replaced: %s",
                db_path,
                reason,
                exc,
            )
            raise
        logger.warning(
            "knowledge: %s was damaged (%s); moved aside as %s and started an empty library. "
            "Sources must be added again; documents added inline exist only in the kept copy.%s",
            db_path,
            reason,
            moved.target.name,
            f" Sidecar(s) left in place: {', '.join(moved.left)}." if moved.left else "",
        )
        store = cls(db_path)
        # Plain words for the dashboard; the path, the SQLite error and any
        # sidecar detail stay in the log above.
        store.warnings.append(
            "Your knowledge library file was damaged, so a new empty library was started. "
            f"The old file was kept as {moved.target.name}. Add your sources again."
        )
        return store

    @classmethod
    def open_read_only(cls, db_path: str) -> "KnowledgeStore":
        """Open an EXISTING library for reading only: no DDL, no migration, no reap.

        The constructor runs `_migrate()` on every open, and that sweep takes the
        writer lock and deletes any itemless source row nothing references. That
        is the right cost for a surface that goes on to write and the wrong one
        for a verb documented as read-only -- `kirocrew knowledge stats` runs in
        a fresh process, so it would re-run the sweep on every invocation. Here
        the file is opened with SQLite `mode=ro`: nothing on this store can
        write, because the engine refuses rather than a convention asking. The
        trade is that a schema behind the code is reported, not repaired -- a
        read that meets a missing table or column raises
        `sqlite3.OperationalError`, and any migrating open (the gateway,
        `kirocrew knowledge dedup --apply`) is the fix.
        """
        return cls(db_path, read_only=True)

    def _connect(self) -> sqlite3.Connection:
        # Production keeps SQLite's thread-affinity guard and registers nothing:
        # its connections are freed exactly as before (by refcount or the cyclic
        # collector), so the registry pins no exited thread's handle. Under the
        # test flag the native guard is relaxed -- which does NOT make the
        # connection shared: `db` still hands every thread its own, and the
        # `_ThreadAffineTestConnection` factory re-applies the guard in Python on
        # every statement entry point the store uses (cursor/execute*/commit/
        # rollback), leaving only `close()` cross-thread -- so `_close_all_for_tests()`,
        # called from whichever thread tears the store down, can release the
        # handles OTHER threads opened; the native check refuses that even for
        # an exited thread.
        test_mode = _ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS
        connect_kwargs: dict = {"timeout": 30, "isolation_level": None}
        if test_mode:
            connect_kwargs.update(check_same_thread=False, factory=_ThreadAffineTestConnection)
        if self._read_only:
            # `as_uri()` percent-encodes the path, which is the escaping SQLite
            # undoes when it parses a URI filename, so a path holding `?` or `#`
            # cannot be read as the start of the query string. journal_mode is
            # left alone: a read-only connection may not change it, and a WAL
            # file is readable as-is.
            uri = Path(self._db_path).resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, **connect_kwargs)
            if test_mode:
                conn._owner_ident = threading.get_ident()
        else:
            # Owner-only before SQLite creates anything: the library holds the
            # user's ingested documents, and SQLite gives -wal/-shm the database
            # file's own mode.
            prepare_owner_only_sqlite(self._db_path)
            conn = sqlite3.connect(self._db_path, **connect_kwargs)
            if test_mode:
                conn._owner_ident = threading.get_ident()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                # A file that is not a database fails here; release the handle so
                # the caller can move the file aside.
                conn.close()
                raise
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        if test_mode:
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    @property
    def db(self) -> sqlite3.Connection:
        """The calling thread's connection, created lazily on first use."""
        _ON_LOOP_DB_GUARD.check()
        conn = getattr(self._thread_local, "conn", None)
        if conn is None or getattr(self._thread_local, "gen", self._generation) != self._generation:
            conn = self._connect()
            self._thread_local.conn = conn
            self._thread_local.gen = self._generation
        return conn

    def ingestion_in_flight(self, *, admitted: bool = False):
        """Bracket one whole ingest so the deferred sweep never sees it half-written.

        Held from the source row through the last mention commit; entering
        waits while :meth:`maintenance_window` holds the sweep's turn.
        """
        return self._ingestion_gate.ingestion_in_flight(admitted=admitted)

    def maintenance_window(self, timeout: float = MAINTENANCE_WAIT_SECS):
        """Wait for ingestion to drain, then hold new ingestion off for the body.

        Yields ``True`` once the store is quiescent; ``False`` -- logged -- when
        ingestion does not drain within ``timeout``, in which case the caller
        skips its sweep. See :class:`IngestionGate`.
        """
        return self._ingestion_gate.maintenance_window(timeout)

    @property
    def graph(self) -> SimpleDiGraph:
        """The entity graph, materialised on first access.

        A backstop, not the intended entry point. Every reader that can run on
        the event loop should call :meth:`ensure_graph_loaded` from a worker
        thread first; this property exists so that a caller nobody found is
        served a CORRECT graph -- and flagged by the on-loop guard if it is on
        the loop -- rather than a silently empty one. An empty graph returned to
        a reader is indistinguishable from "this entity has no neighbours",
        which is the failure mode worth paying a stall to avoid.
        """
        self.ensure_graph_loaded()
        return self._graph

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

    def close(self):
        """Close the CALLING thread's connection; other threads' stay live.

        Per-thread by contract (see `test_knowledge_cross_thread`): a worker
        mid-query must never have its handle closed from under it. In production
        the process exit is what closes the other threads' handles; a test
        teardown uses `_close_all_for_tests()` instead.
        """
        conn = getattr(self._thread_local, "conn", None)
        if conn is not None:
            with self._connections_lock:
                self._connections = [c for c in self._connections if c is not conn]
            conn.close()
            self._thread_local.conn = None

    def _close_all_for_tests(self):
        """Close EVERY connection this store opened, on any thread.

        A test seam, and nothing in production may call it: production has no
        moment at which every thread is provably idle short of process exit,
        and closing a handle from under a worker mid-query is undefined. A test
        teardown has that moment, and needs the close because connections that
        pool threads and exited threads opened have no other close path, and an
        unclosed one holds its descriptors until the cyclic collector runs
        (a `sqlite3.Connection` is a self-cycle on CPython 3.11+). Idempotent, and not final:
        the store stays usable, each thread reopening lazily on its next `db`
        take -- the generation bump is what tells a thread its cached handle was
        closed from elsewhere. Callers must ensure no thread is mid-query.
        """
        if not _ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS:
            raise RuntimeError(
                "KnowledgeStore._close_all_for_tests is a test seam: set "
                "kiro_crew.knowledge.store._ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS before any "
                "store is built (test/conftest.py does, for the test/ testpath only; the app "
                "test trees under src/kiro_crew/apps/builtins do not activate the flip "
                "themselves); production closes by exiting"
            )
        with self._connections_lock:
            conns, self._connections = self._connections, []
            self._generation += 1
        for conn in conns:
            conn.close()
        self._thread_local.conn = None

    # The entity graph and its in-memory copy: ``kiro_crew.knowledge.store_parts.entity_graph``.
    _prune_orphan_entities = _entity_graph._prune_orphan_entities
    ensure_graph_loaded = _entity_graph.ensure_graph_loaded
    _load_graph = _entity_graph._load_graph
    reload_graph = _entity_graph.reload_graph
    add_entity = _entity_graph.add_entity
    find_entity = _entity_graph.find_entity
    merge_entities = _entity_graph.merge_entities
    add_entity_relation = _entity_graph.add_entity_relation
    add_mention = _entity_graph.add_mention
    get_neighbors = _entity_graph.get_neighbors
    get_entity_subgraph = _entity_graph.get_entity_subgraph

    # The full-text index: ``kiro_crew.knowledge.store_parts.fulltext_index``.
    _FTS_REBUILD_BATCH = _fulltext_index._FTS_REBUILD_BATCH
    ensure_fts_index_current = _fulltext_index.ensure_fts_index_current
    _migrate_fts_index = _fulltext_index._migrate_fts_index
    _fts_terms_segmented = _fulltext_index._fts_terms_segmented
    _fts_terms = _fulltext_index._fts_terms
    _fts_index = _fulltext_index._fts_index
    _fts_unindex = _fulltext_index._fts_unindex
    search_items_fts = _fulltext_index.search_items_fts
    _sanitize_fts5 = staticmethod(_fulltext_index._sanitize_fts5)

    # Source rows: ``kiro_crew.knowledge.store_parts.source_rows``.
    _retire_one_in_txn = _source_rows._retire_one_in_txn
    retire_auto_registered_folder = _source_rows.retire_auto_registered_folder
    merge_source_properties = _source_rows.merge_source_properties
    revise_source_properties = _source_rows.revise_source_properties
    add_source = _source_rows.add_source
    get_source_by_uri = _source_rows.get_source_by_uri
    _SOURCE_COLUMNS = _source_rows._SOURCE_COLUMNS
    update_source = _source_rows.update_source

    # Item locations and document ownership: ``kiro_crew.knowledge.store_parts.item_ownership``.
    agent_owned_item_ids = _item_ownership.agent_owned_item_ids
    _adopt_reassigned_item = _item_ownership._adopt_reassigned_item
    detach_source_location_by_hash = _item_ownership.detach_source_location_by_hash
    release_stale_claim = _item_ownership.release_stale_claim
    surviving_group_in_txn = _item_ownership.surviving_group_in_txn
    add_source_location = _item_ownership.add_source_location
    add_source_location_in_txn = _item_ownership.add_source_location_in_txn
    sources_holding_item = _item_ownership.sources_holding_item
    reassign_item_source = _item_ownership.reassign_item_source

    # Item rows and deletes: ``kiro_crew.knowledge.store_parts.item_rows``.
    find_doc_by_content_hash = _item_rows.find_doc_by_content_hash
    add_item = _item_rows.add_item
    get_item = _item_rows.get_item
    _serialize_item = staticmethod(_item_rows._serialize_item)
    _ITEM_COLUMNS = _item_rows._ITEM_COLUMNS
    update_item = _item_rows.update_item
    _delete_item_cascade = _item_rows._delete_item_cascade
    delete_item = _item_rows.delete_item
    delete_items_batch = _item_rows.delete_items_batch
    delete_items_batch_in_txn = _item_rows.delete_items_batch_in_txn
    delete_source_cascade = _item_rows.delete_source_cascade

    # Schema and migrations: ``kiro_crew.knowledge.store_parts.schema_migrations``.
    _init_schema = _schema_migrations._init_schema
    _ensure_search_indexes_if_empty = _schema_migrations._ensure_search_indexes_if_empty
    _SEARCH_INDEXES = _schema_migrations._SEARCH_INDEXES
    has_pending_one_time_work = _schema_migrations.has_pending_one_time_work
    has_embed_state_index = _schema_migrations.has_embed_state_index
    ensure_search_indexes = _schema_migrations.ensure_search_indexes
    _VECTOR_ITEM_COLUMNS = _schema_migrations._VECTOR_ITEM_COLUMNS
    _ensure_vector_generation = _schema_migrations._ensure_vector_generation
    vector_generation = _schema_migrations.vector_generation
    _migrate = _schema_migrations._migrate

    # Maintenance sweeps: ``kiro_crew.knowledge.store_parts.maintenance_sweeps``.
    reclaim_orphans = _maintenance_sweeps.reclaim_orphans
    _adopt_agent_sole_copy_residue = _maintenance_sweeps._adopt_agent_sole_copy_residue
    reclaim_agent_source_residue = _maintenance_sweeps.reclaim_agent_source_residue

    # Library statistics: ``kiro_crew.knowledge.store_parts.content_stats``.
    source_count = _content_stats.source_count
    aggregate_stats = _content_stats.aggregate_stats
    get_stats = _content_stats.get_stats

    # Bundle export and import: ``kiro_crew.knowledge.store_parts.bundle_transfer``.
    export_item = _bundle_transfer.export_item
    export_all = _bundle_transfer.export_all
    import_bundle = _bundle_transfer.import_bundle
    _bundle_group_bounds = _bundle_transfer._bundle_group_bounds
    _bundle_membership_ids = _bundle_transfer._bundle_membership_ids

    # Bundle ownership rows: ``kiro_crew.knowledge.store_parts.bundle_ownership``.
    _bundle_blocked_items = _bundle_ownership._bundle_blocked_items
    _contested_claims = _bundle_ownership._contested_claims
    _items_exist = _bundle_ownership._items_exist
    _import_bundle_state = _bundle_ownership._import_bundle_state
    _claimed_item_ids = _bundle_ownership._claimed_item_ids
    _state_row_owns_items = _bundle_ownership._state_row_owns_items
