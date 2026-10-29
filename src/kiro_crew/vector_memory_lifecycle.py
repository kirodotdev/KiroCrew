"""Opening and closing a ``VectorMemoryStore``'s one SQLite connection.

``init`` takes namespace admission and the named-store use lock, then ``_init_database``
opens SQLite: a member file through its canonical identity, every other file through the
schema ladder of its lineage. It binds the lineage, applies the migrations and the shared
record metadata, and restricts every memory-bearing file to its owner. ``close`` drops the
connection and every population derived from it. The V1 schema (``_SCHEMA_V1``,
``_migrate_v2``, ``_MEMORY_META_TABLE``, ``_MIGRATIONS``) lives here with the code that
applies it, and :mod:`kiro_crew.vector_memory` re-exports it.

This module opens the connection, which ``vector_memory_runtime`` modules never do, so it
lives beside the facade. The functions are bound on ``VectorMemoryStore`` as the class
attributes of the same name and take the store as ``self``. Store fields written: ``_db``,
``_memory_version``, ``_store_use_lock_fd``, ``_space_generation``, ``_faiss_index``,
``_faiss_data_version``, ``_episodic_scoring(_refused)`` and
``_semantic_scoring(_refused)``, and the lineage fields through ``_bind_lineage``. Read:
``_db_path``, ``_faiss_path``, ``_faiss_id_map``, ``_memory_store_name``,
``_member_identity`` and ``_db_lock``.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import memory_record_metadata as record_meta
from kiro_crew import memory_schema, memory_stores, platform_compat, sqlite_wal
from kiro_crew.owner_only_files import prepare_owner_only_sqlite

if TYPE_CHECKING:
    from typing import Callable

    from kiro_crew._sqlite_compat import sqlite3
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")

#: How long a statement of an open waits for another connection's lock.
_BUSY_TIMEOUT_MS = 5000


_SCHEMA_V1 = f"""
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_memory (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_deleted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_semantic_deleted ON semantic_memory(is_deleted);

CREATE TABLE IF NOT EXISTS episodic_memories (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    text TEXT NOT NULL,
    embedding BLOB,
    tags TEXT DEFAULT '[]',
    importance REAL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    last_accessed_at TEXT,
    is_deleted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_episodic_deleted ON episodic_memories(is_deleted);
CREATE INDEX IF NOT EXISTS idx_episodic_created ON episodic_memories(created_at);
CREATE INDEX IF NOT EXISTS idx_episodic_conversation ON episodic_memories(conversation_id);
{memory_schema.MEMORY_EVENTS_SQL}"""


def _migrate_v2(db: sqlite3.Connection) -> None:
    """Add embedding BLOB column (idempotent; SQLite lacks IF NOT EXISTS for ADD COLUMN)."""

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    sqlite3 = vm.sqlite3

    try:
        db.execute("ALTER TABLE semantic_memory ADD COLUMN embedding BLOB")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


# Named separately because it is v1's THIRD migration, applied to files that
# already carry v1's first two. The DDL itself is lineage-agnostic and lives with
# ``memory_events`` in ``memory_schema``.
_MEMORY_META_TABLE = memory_schema.MEMORY_META_SQL


# Every step runs inside the one setup transaction ``_init_database`` opens, so a
# step must not commit: no ``db.commit()`` and no ``executescript`` (which commits
# first). A step that does fails the open at the closing COMMIT.
_MIGRATIONS: list[tuple[int, str, "Callable[[sqlite3.Connection], None] | None"]] = [
    (1, _SCHEMA_V1, None),
    (2, "", _migrate_v2),
    (3, _MEMORY_META_TABLE, None),
]


def _secret_bearing_files(self: VectorMemoryStore) -> tuple[Path, ...]:
    """Every file beside the DB that carries the user's memories.

    All of them, not just the DB, because on Windows the owner-only DIRECTORY is
    not sufficient for a file that already exists: **Bypass Traverse Checking** is
    granted to Everyone by default, so a permissive DACL on the file itself stays
    reachable even inside a tightened directory. The directory governs what SQLite
    and FAISS create from now on; this list is what repairs an existing install.

    - ``-wal`` / ``-shm``: a COMMITTED row lives in the ``-wal`` until a
      checkpoint moves it. Same suffix set ``memory.py`` uses to drop a corrupt
      index.
    - ``memory.faiss`` / ``memory.ids.json``: the embedding index and its
      id map, written with no lockdown of their own.

    Not exhaustive for the data home as a whole -- ``memory.py``'s FTS index
    (``memory_index.db``) and its sidecars carry the same secrets; ``memory.py``
    restricts those itself in ``MemoryStore._restrict_index_files``.
    """
    return (
        Path(f"{self._db_path}-wal"),
        Path(f"{self._db_path}-shm"),
        self._faiss_path,
        self._faiss_path.with_suffix(".ids.json"),
    )


def _restrict_memory_files(self: VectorMemoryStore) -> None:
    """Make every memory-bearing file that exists owner-only.

    Called TWICE by :meth:`init` -- once before the connect and once after -- and
    the ordering is the point of the first call. The owner-only directory does not
    cover a file that already EXISTS on Windows, because Bypass Traverse Checking
    is granted to Everyone by default, so a permissive DACL on the file itself
    stays reachable inside a tightened directory. Restricting before
    ``sqlite3.connect`` means the migrations do not run against a file another
    local user can still write; restricting again after covers whatever SQLite
    just created.

    Missing files are skipped BY AN EXISTENCE CHECK, not by catching the failure:
    on Windows ``restrict_to_owner`` raises plain ``OSError`` for a missing path
    (the in-process DACL write's failure is translated to ``OSError``) rather
    than ``FileNotFoundError``, which only ever comes from the POSIX
    ``os.chmod``. Catching alone would log a false "may be readable by other
    users" warning for each missing file, twice per init. The race between
    the check and the call is benign: a file that appears in between is created by
    SQLite or FAISS inside the already-tightened directory, so it inherits
    owner-only access on both platforms and the next init covers it regardless.

    Any other failure warns rather than raising -- memory being unavailable is a
    supported degraded state, and ``restrict_to_owner`` documents this
    warn-and-continue handler as its caller contract.
    """
    for path in (self._db_path, *self._secret_bearing_files()):
        if not path.exists():
            continue  # SQLite and FAISS create theirs on demand
        try:
            platform_compat.restrict_to_owner(path)
        except OSError:
            logger.warning(
                "Cannot restrict %s to owner; it may be readable by other users",
                path,
                exc_info=True,
            )


@memory_stores.named_store_operation
def init(self: VectorMemoryStore) -> None:
    """Open under namespace admission, then hold the named generation until close."""
    from kiro_crew.memory_startup import require_memory_ready

    require_memory_ready(self._memory_store_name)
    if self._memory_store_name and self._store_use_lock_fd is None:
        from kiro_crew import member_memory_backup

        # Named V1 and V2 stores are both replaced as whole directories.
        # Take admission before opening SQLite so restore cannot replace a live handle.
        self._store_use_lock_fd = member_memory_backup.acquire_store_use_lock(self._db_path)
    # A repeated init() replaces the handle. Close the one it replaces, or the
    # orphan keeps its descriptors until the cyclic collector runs: on CPython
    # 3.11+ an unclosed sqlite3.Connection is a reference cycle (its statement
    # cache is an lru_cache wrapping the connection), so refcounting never
    # frees it.
    with self._db_lock:
        if self._db is not None:
            self._db.close()
            self._db = None
            # The validity token is per-connection (data_version), so a
            # fresh connection's baseline can equal a retained snapshot's;
            # drop both resident scoring sets on the swap or a stale one
            # survives the reconnect. Matches close()'s clears.
            self._episodic_scoring = None
            self._episodic_scoring_refused = None
            self._semantic_scoring = None
            self._semantic_scoring_refused = None
    try:
        self._init_database()
    except BaseException:
        try:
            self.close()
        except Exception:
            logger.warning("Failed to close a partially initialized memory store", exc_info=True)
        raise


def _init_database(self: VectorMemoryStore) -> None:
    """Create DB, apply migrations, and set permissions under admission."""

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _member_identity = vm._member_identity
    sqlite3 = vm.sqlite3

    from kiro_crew.memory_startup import require_memory_ready

    require_memory_ready(self._memory_store_name)
    expected = getattr(self, "_member_identity", None)
    if expected is not None:
        # mode=rw is intentional: admission never provisions a missing file.
        self._db = sqlite3.connect(
            self._db_path.resolve().as_uri() + "?mode=rw",
            uri=True,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        if _member_identity(self._db) != expected:
            raise ValueError("Member memory database identity does not match admission")
        self._bind_lineage(memory_schema.LINEAGE_CREW)
        self._memory_version = 2
        self._db.execute("PRAGMA busy_timeout=5000")
        return
    # Private files must never enter the legacy schema/migration path.
    if self._db_path.exists():
        probe = sqlite3.connect(self._db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if probe.execute("SELECT 1 FROM sqlite_schema WHERE name='member_database'").fetchone():
                raise ValueError("Private memory requires canonical member admission")
        finally:
            probe.close()
    if self._memory_store_name:
        from kiro_crew.config.loader import KiroCrewConfig

        declaration = KiroCrewConfig.load().memory_stores.get(self._memory_store_name)
        if declaration is not None and declaration.memory_version == 2:
            raise ValueError("Private memory requires explicit provisioning and member admission")
    # Owner-only lockdown, in two halves. This directory call covers everything
    # SQLite and FAISS create from here on -- inheritable on Windows, because
    # `make_owner_only_dir` routes through `restrict_dir_to_owner`. The per-file
    # pass below repairs what already EXISTS, which a tightened parent cannot do:
    # Windows grants *Bypass Traverse Checking* to Everyone by default, so a
    # pre-lockdown file stays reachable through it. Full reasoning -- the sidecar
    # file set, the every-init rationale, the Windows lockdown cost, the fail-soft
    # contract -- lives in docs/guides/windows-install.md, "The memory store".
    #
    # SCOPE: this covers the DB's own directory and nothing above it. With the
    # default `db_path` that directory happens to BE the data home
    # (`config_dir()`); with a named memory store it is that store's own
    # directory under `memory_stores/`. The whole-home guarantee does not rest
    # on which of those it is -- `config.paths.ensure_data_home` tightens the
    # home where the home is established, so a home whose crews all use named
    # stores is covered too.
    platform_compat.make_owner_only_dir(self._db_path.parent)
    # BEFORE the connect so the migrations do not run against a file another
    # local user can still write; repeated after it to cover what SQLite created.
    self._restrict_memory_files()
    # And a NEW database is created 0600 before SQLite opens it, so none of
    # its files exists readable even briefly (SQLite copies the database's
    # mode onto -wal/-shm).
    prepare_owner_only_sqlite(self._db_path)
    self._db = sqlite3.connect(str(self._db_path), check_same_thread=False, isolation_level=None)
    self._db.row_factory = sqlite3.Row
    self._db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    # The switch into WAL is not covered by busy_timeout; see kiro_crew.sqlite_wal.
    sqlite_wal.enable_wal(self._db, timeout_secs=_BUSY_TIMEOUT_MS / 1000)
    # synchronous stays at the sqlite default (FULL). NORMAL would drop the
    # per-commit fsync, but under WAL that only survives a process crash --
    # an OS crash or power loss can still lose the unsynced WAL tail, and
    # here that tail is acknowledged semantic memories, lessons and episodic
    # rows. The write-volume problem it was meant to address is handled by
    # debouncing the last_accessed_at touch instead, which removes the
    # commits rather than weakening the ones that remain.

    # Schema setup reads first and writes only when something is pending. The
    # first pass runs with no transaction, so the schema step of an up-to-date
    # file takes no write lock.
    # Anything pending is built in ONE transaction: a new file pays one durable
    # commit, not one per DDL statement (outside a transaction every statement
    # is its own FULL-sync commit), and a failure part-way leaves the file
    # exactly as it was. The plan is derived again under BEGIN IMMEDIATE,
    # because another connection may have built the same steps in between.
    # ``journal_mode`` cannot change inside a transaction, so it stays above.
    # A step that commits ends the transaction early, and the COMMIT below then
    # raises "no transaction is active", so such a step fails the open loudly.
    migrated: list[int] = []
    try:
        if self._schema_steps(migrated):
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                for step in self._schema_steps(migrated):
                    step()
                self._db.execute("COMMIT")
            except BaseException:
                self._db.rollback()
                raise
    finally:
        self._db.isolation_level = ""  # Restore implicit transaction handling
    for ver in migrated:
        logger.info("Applied memory schema migration v%s", ver)

    # The metadata backfill keeps its own transaction: it reads every row, and
    # the write lock the schema transaction holds must not wait on that scan.
    with self._db:
        record_meta.reconcile(self._db)
        if self.algorithm_version == "v1":
            record_meta.limit_v1_accepted_history(self._db)

    # Second pass, after the connect: covers what SQLite has just created. Runs
    # on EVERY init, not only when init created the files -- an existing DB is
    # exactly the one that may have lost its protection since (restored backup,
    # home migration, manual edit, or an install predating this lockdown).
    # ``restrict_to_owner`` rather than ``chmod_safe``, which is a documented
    # no-op on Windows. Cost, file set and fail-soft contract:
    # docs/guides/windows-install.md, "The memory store".
    #
    # CALLER CONTRACT: an async caller must offload this. ``init()`` is
    # blocking end to end — the sqlite connect, the schema migrations, and
    # this lockdown pass (in-process on Windows since the advapi32
    # conversion, but still filesystem work that can stall on a slow
    # volume) — so calling it directly on an event loop stalls every task.
    # All async callers offload: ``eval.runner``, ``slack.gateway`` and
    # ``cli_server._run_task`` via ``asyncio.to_thread``;
    # ``dashboard/handlers/memory_services.py``'s standalone fallback routes
    # through ``_get_vector_store_async``, which offloads the init-bearing
    # path.
    self._restrict_memory_files()

    # Load persisted FAISS index (or rebuild from SQLite embeddings)
    try:
        self.load_faiss_index()
    except Exception:
        logger.warning("FAISS index not loaded (faiss-cpu may not be installed yet)", exc_info=True)


def _schema_steps(self: VectorMemoryStore, migrated: list[int]) -> list[Callable[[], object]]:
    """Every schema write this file still needs, in order. Reads only.

    Empty for an up-to-date file. Also binds the lineage, which a file that
    needs nothing still has to know. *migrated* collects the version of each
    migration step as it runs, for the log line after COMMIT.
    """
    with self._db_lock:
        if self.db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone():
            applied = {row[0] for row in self.db.execute("SELECT version FROM schema_version")}
        else:
            applied = set()
        # WHICH lineage, decided here and nowhere else. This is the single gate:
        # nine call sites construct a store, one of which (security.scan_memory)
        # sits outside the memory subsystem entirely and reaches the default store
        # as a bare VectorMemoryStore(), so a per-call-site check could not cover it.
        #
        # A file that already holds product tables answers itself, so no path is
        # consulted for it and the operator's running memory.db cannot take the
        # crew branch however the predicate below is later edited. Only a file
        # with no product tables asks where it lives.
        detected = memory_schema.detect_lineage(self.db)
        self._bind_lineage(detected or memory_schema.lineage_for_new_file(self._db_path))
        named_store = memory_stores.named_store_of_db(self._db_path)
        self._memory_version = 1
        migrations = (
            memory_schema.MIGRATIONS_CREW
            if self._lineage == memory_schema.LINEAGE_CREW
            else _MIGRATIONS
        )
        steps: list[Callable[[], object]] = [
            functools.partial(self._apply_migration, ver, sql, fn, migrated)
            for ver, sql, fn in migrations
            if ver not in applied
        ]
        # Additive revision metadata is shared by both lineages. It never changes
        # their physical rows or version series; old binaries remain readable.
        if not record_meta.schema_is_current(self.db):
            steps.append(functools.partial(record_meta.ensure_schema, self.db))
        if self._lineage == memory_schema.LINEAGE_CREW:
            # Before its migrations run, a crew file has no memory_meta to read,
            # so the stamp step is planned and reads the stamps when it runs.
            if steps or self._crew_stamps_missing(named_store):
                steps.append(functools.partial(self._stamp_crew_lineage, named_store))
        return steps


def _apply_migration(
    self: VectorMemoryStore,
    ver: int,
    sql: str,
    fn: Callable[[sqlite3.Connection], None] | None,
    migrated: list[int],
) -> None:
    """Run one migration and record its version, inside the setup transaction."""
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    with self._db_lock:
        if sql:
            memory_schema.execute_script(self.db, sql)
        if fn:
            fn(self.db)
        self.db.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (ver, vm._now_iso()),
        )
    migrated.append(ver)


def _crew_stamps_missing(self: VectorMemoryStore, named_store: str | None) -> bool:
    """Whether a crew file still lacks its lineage or store-name stamp."""
    if self._read_meta(memory_schema.LINEAGE_META_KEY) != self._lineage:
        return True
    return bool(named_store) and self._read_meta(memory_schema.STORE_NAME_META_KEY) is None


def _stamp_crew_lineage(self: VectorMemoryStore, named_store: str | None) -> None:
    """Write whichever crew stamp is missing, inside the setup transaction."""
    if self._read_meta(memory_schema.LINEAGE_META_KEY) != self._lineage:
        self._write_meta_in_transaction(memory_schema.LINEAGE_META_KEY, self._lineage)
    if named_store and self._read_meta(memory_schema.STORE_NAME_META_KEY) is None:
        self._write_meta_in_transaction(memory_schema.STORE_NAME_META_KEY, named_store)


def close(self: VectorMemoryStore) -> None:
    with self._db_lock:
        try:
            if self._db:
                self._db.close()
                self._db = None
            self._space_generation += 1
            self._faiss_index = None
            self._faiss_id_map.clear()
            self._faiss_data_version = None
            self._episodic_scoring = None
            self._episodic_scoring_refused = None
            # Clear the semantic twin too: init() can swap the connection on
            # a live object in a long-lived process, and the validity token
            # is per-connection, so a fresh connection's baseline
            # data_version can equal a retained snapshot's — the snapshot
            # must not survive a connection swap.
            self._semantic_scoring = None
            self._semantic_scoring_refused = None
        finally:
            self._release_store_use_lock()


def _release_store_use_lock(self: VectorMemoryStore) -> None:
    fd, self._store_use_lock_fd = self._store_use_lock_fd, None
    if fd is not None:
        from kiro_crew.member_memory_backup import release_store_use_lock

        release_store_use_lock(fd)
