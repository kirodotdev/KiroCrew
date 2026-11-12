"""The session chunk store: chunk references, vectors, and text awaiting a vector.

``sessions/.index/session_vectors.db`` sits beside the keyword index and shares
its lifecycle and its deletion contract (RFC ``rfc-session-semantic-search``
sections 7.1 and 12). The index worker writes chunks; vectors are filled later.

A chunk's text is kept in ``pending_text`` only until its vector is written, in
the same transaction that writes the vector. Identical text is stored once and
referenced from every session holding it, so a drop removes a chunk only when no
remaining reference points at it.

The store holds transcript-derived data, so it opens owner-only with
``secure_delete`` on, and :meth:`SessionVectorStore.drop` truncates the WAL and
reads the checkpoint's verdict, as ``SessionSearchIndex.drop`` does.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.history_chunks import Chunk
from kiro_crew.owner_only_files import mkdirs_owner_only, prepare_owner_only_sqlite
from kiro_crew.sqlite_wal import enable_wal

VECTORS_FILENAME = "session_vectors.db"

_STORE_VERSION = 1
_BUSY_TIMEOUT_MS = 10_000
_CONNECT_TIMEOUT_SECS = 30
_WAL_TRUNCATE_ATTEMPTS = 3
_WAL_TRUNCATE_RETRY_SECS = 0.05

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS chunk("
    " chunk_id    INTEGER PRIMARY KEY,"
    " text_hash   BLOB    NOT NULL UNIQUE,"
    " token_count INTEGER NOT NULL,"
    " vector      BLOB,"
    " signature   TEXT,"
    " embedded_at REAL)",
    "CREATE TABLE IF NOT EXISTS pending_text("
    " chunk_id INTEGER PRIMARY KEY,"
    " text     TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS chunk_ref("
    " session_key   TEXT    NOT NULL,"
    " ordinal       INTEGER NOT NULL,"
    " message_index INTEGER NOT NULL,"
    " char_start    INTEGER NOT NULL,"
    " char_end      INTEGER NOT NULL,"
    " chunk_id      INTEGER NOT NULL,"
    " PRIMARY KEY (session_key, ordinal)) WITHOUT ROWID",
    "CREATE INDEX IF NOT EXISTS chunk_ref_by_chunk ON chunk_ref(chunk_id)",
    "CREATE TABLE IF NOT EXISTS session_state("
    " session_key TEXT PRIMARY KEY,"
    " mtime_ns    INTEGER NOT NULL,"
    " size        INTEGER NOT NULL,"
    " dev         TEXT    NOT NULL,"
    " ino         TEXT    NOT NULL,"
    " chunker     TEXT    NOT NULL)",
)


def _identity(st: os.stat_result) -> tuple[int, int, str, str]:
    # dev and ino as text: an unsigned 64-bit value overflows a SQLite INTEGER.
    return st.st_mtime_ns, st.st_size, str(st.st_dev), str(st.st_ino)


class SessionVectorStore:
    """Chunk store for one session directory, opened lazily per thread.

    Nothing opens or creates the file until a write or read needs it, so a host
    with semantic search off never has one, and a delete on such a host finds
    nothing to remove.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        #: Set when a drop committed but its WAL truncate did not happen, so the
        #: dropped bytes may still be in the WAL; :meth:`settle` retries it.
        self.erasure_owed = False

    def store_exists(self) -> bool:
        """Whether a store file is on disk. An unreadable path answers ``True``."""
        try:
            return self._db_path.exists()
        except OSError:
            return True

    def _ensure_open(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        mkdirs_owner_only(self._db_path.parent)
        prepare_owner_only_sqlite(self._db_path)
        conn = sqlite3.connect(
            str(self._db_path), timeout=_CONNECT_TIMEOUT_SECS, isolation_level=None
        )
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        enable_wal(conn, timeout_secs=_BUSY_TIMEOUT_MS / 1000)
        # Freed pages are zeroed so a dropped chunk's text and vector do not stay
        # readable in the file; drop() truncates the WAL for the same reason.
        conn.execute("PRAGMA secure_delete=ON")
        with self._schema_lock:
            if not self._schema_ready:
                self._init_schema(conn)
                self._schema_ready = True
        self._local.conn = conn
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version and version != _STORE_VERSION:
            for table in ("chunk_ref", "pending_text", "chunk", "session_state"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            version = 0
        for statement in _SCHEMA:
            conn.execute(statement)
        if not version:
            conn.execute(f"PRAGMA user_version={_STORE_VERSION}")

    def fresh_keys(self, stats: dict[str, os.stat_result], chunker: str) -> set[str]:
        """Keys of *stats* whose stored state matches the file and *chunker*."""
        if not stats:
            return set()
        rows = (
            self._ensure_open()
            .execute("SELECT session_key, mtime_ns, size, dev, ino, chunker FROM session_state")
            .fetchall()
        )
        fresh = set()
        for key, mtime_ns, size, dev, ino, stored_chunker in rows:
            st = stats.get(key)
            if st is not None and stored_chunker == chunker:
                if (mtime_ns, size, dev, ino) == _identity(st):
                    fresh.add(key)
        return fresh

    def indexed_keys(self) -> set[str]:
        rows = self._ensure_open().execute("SELECT session_key FROM session_state").fetchall()
        return {row[0] for row in rows}

    def sync_session(
        self, key: str, st: os.stat_result, chunker: str, chunks: Sequence[Chunk]
    ) -> None:
        """Replace *key*'s references with *chunks*, in one transaction."""
        conn = self._ensure_open()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            old_ids = self._delete_refs(conn, [key])
            for ordinal, chunk in enumerate(chunks):
                conn.execute(
                    "INSERT OR IGNORE INTO chunk(text_hash, token_count) VALUES (?, ?)",
                    (chunk.text_hash, chunk.token_count),
                )
                chunk_id, has_vector = conn.execute(
                    "SELECT chunk_id, vector IS NOT NULL FROM chunk WHERE text_hash=?",
                    (chunk.text_hash,),
                ).fetchone()
                if not has_vector:
                    conn.execute(
                        "INSERT OR IGNORE INTO pending_text(chunk_id, text) VALUES (?, ?)",
                        (chunk_id, chunk.text),
                    )
                conn.execute(
                    "INSERT INTO chunk_ref VALUES (?, ?, ?, ?, ?, ?)",
                    (key, ordinal, chunk.message_index, chunk.char_start, chunk.char_end, chunk_id),
                )
            conn.execute(
                "INSERT OR REPLACE INTO session_state VALUES (?, ?, ?, ?, ?, ?)",
                (key, *_identity(st), chunker),
            )
            self._delete_orphans(conn, old_ids)

    def drop(self, keys: Iterable[str]) -> bool:
        """Remove *keys*, reporting whether the removal is known to have happened.

        ``False`` on any failure, because ``delete_session`` refuses to unlink a
        transcript while derived data may remain. No file on disk is success.
        """
        keys = list(keys)
        if not keys or not self.store_exists():
            return True
        try:
            conn = self._ensure_open()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                old_ids = self._delete_refs(conn, keys)
                conn.executemany(
                    "DELETE FROM session_state WHERE session_key=?", [(key,) for key in keys]
                )
                self._delete_orphans(conn, old_ids)
        except Exception:
            return False
        # Outside the transaction: a checkpoint cannot run inside one. The rows
        # are gone from here on, so any failure to truncate is an owed erasure.
        try:
            truncated = self._truncate_wal(conn)
        except Exception:
            truncated = False
        self.erasure_owed = not truncated
        return truncated

    def settle(self) -> bool:
        """Retry an owed WAL truncate; ``True`` when nothing is owed afterwards."""
        if not self.erasure_owed:
            return True
        try:
            self.erasure_owed = not self._truncate_wal(self._ensure_open())
        except Exception:
            return False
        return not self.erasure_owed

    @staticmethod
    def _delete_refs(conn: sqlite3.Connection, keys: Sequence[str]) -> set[int]:
        ids: set[int] = set()
        for key in keys:
            rows = conn.execute(
                "SELECT chunk_id FROM chunk_ref WHERE session_key=?", (key,)
            ).fetchall()
            ids.update(row[0] for row in rows)
            conn.execute("DELETE FROM chunk_ref WHERE session_key=?", (key,))
        return ids

    @staticmethod
    def _delete_orphans(conn: sqlite3.Connection, chunk_ids: Iterable[int]) -> None:
        for chunk_id in chunk_ids:
            if conn.execute(
                "SELECT 1 FROM chunk_ref WHERE chunk_id=? LIMIT 1", (chunk_id,)
            ).fetchone():
                continue
            conn.execute("DELETE FROM pending_text WHERE chunk_id=?", (chunk_id,))
            conn.execute("DELETE FROM chunk WHERE chunk_id=?", (chunk_id,))

    def _truncate_wal(self, conn: sqlite3.Connection) -> bool:
        for attempt in range(_WAL_TRUNCATE_ATTEMPTS):
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row is None or int(row[0]) == 0:
                return True
            if attempt + 1 < _WAL_TRUNCATE_ATTEMPTS:
                time.sleep(_WAL_TRUNCATE_RETRY_SECS)
        return False

    def close(self) -> None:
        """Release the calling thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None
