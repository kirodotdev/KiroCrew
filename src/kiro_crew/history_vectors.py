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
from kiro_crew.sqlite_wal import enable_wal, wal_truncated

VECTORS_FILENAME = "session_vectors.db"

_STORE_VERSION = 1
_BUSY_TIMEOUT_MS = 10_000
_CONNECT_TIMEOUT_SECS = 30

#: ``AUTOINCREMENT`` so a dropped chunk's id is never given to a new chunk: a vector
#: embedded from the dropped text must not land on another chunk's row.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS chunk("
    " chunk_id    INTEGER PRIMARY KEY AUTOINCREMENT,"
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
    # One counter every write to a reference or a chunk advances, through triggers,
    # so a reader can tell the vectors it holds are stale whichever writer moved
    # them and whatever rowids or counts came back the same.
    "CREATE TABLE IF NOT EXISTS store_revision("
    " id INTEGER PRIMARY KEY CHECK (id = 1),"
    " n  INTEGER NOT NULL)",
    "INSERT OR IGNORE INTO store_revision VALUES (1, 0)",
    *(
        f"CREATE TRIGGER IF NOT EXISTS {table}_{event.lower()}_revision AFTER {event} ON {table}"
        " BEGIN UPDATE store_revision SET n = n + 1 WHERE id = 1; END"
        for table in ("chunk_ref", "chunk")
        for event in ("INSERT", "UPDATE", "DELETE")
    ),
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

    @property
    def db_path(self) -> Path:
        """The store file this instance opens."""
        return self._db_path

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
            for table in ("chunk_ref", "pending_text", "chunk", "session_state", "store_revision"):
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

    def pending_batch(self, limit: int) -> list[tuple[int, str]]:
        """Up to *limit* ``(chunk id, text)`` pairs still waiting for a vector."""
        rows = (
            self._ensure_open()
            .execute("SELECT chunk_id, text FROM pending_text ORDER BY chunk_id LIMIT ?", (limit,))
            .fetchall()
        )
        return [(row[0], row[1]) for row in rows]

    def pending_count(self) -> int:
        return int(self._ensure_open().execute("SELECT count(*) FROM pending_text").fetchone()[0])

    def chunk_owners(self, chunk_ids: Iterable[int]) -> dict[int, set[str]]:
        """The sessions referencing each of *chunk_ids*; a chunk with none is absent."""
        conn = self._ensure_open()
        owners: dict[int, set[str]] = {}
        for chunk_id in chunk_ids:
            rows = conn.execute(
                "SELECT DISTINCT session_key FROM chunk_ref WHERE chunk_id=?", (chunk_id,)
            ).fetchall()
            if rows:
                owners[chunk_id] = {row[0] for row in rows}
        return owners

    def vector_rows(self, signature: str) -> list[tuple[str, int, int, int, bytes]]:
        """Every ``(session, message index, start, end, vector)`` embedded in *signature*'s space.

        One row per reference, so a chunk two sessions hold appears under both.
        A vector from another space is never returned.
        """
        rows = (
            self._ensure_open()
            .execute(
                "SELECT r.session_key, r.message_index, r.char_start, r.char_end, c.vector"
                " FROM chunk_ref r JOIN chunk c USING (chunk_id)"
                " WHERE c.vector IS NOT NULL AND c.signature = ?",
                (signature,),
            )
            .fetchall()
        )
        return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]

    def vectors_fingerprint(self, signature: str) -> tuple[int, int]:
        """A cheap value that changes when :meth:`vector_rows` would answer differently.

        The store revision, which a trigger advances on every write to ``chunk_ref``
        or ``chunk``, and the count of vectors in *signature*'s space.
        """
        conn = self._ensure_open()
        revision = conn.execute("SELECT n FROM store_revision WHERE id = 1").fetchone()
        vectors = conn.execute(
            "SELECT count(*) FROM chunk WHERE signature = ?", (signature,)
        ).fetchone()[0]
        return int(revision[0]) if revision else 0, int(vectors)

    def write_vectors(self, vectors: Sequence[tuple[int, bytes]], signature: str) -> int:
        """Store each vector and delete its pending text, in one transaction.

        A chunk dropped since it was read matches no row and is skipped; its
        pending text went with it. Returns how many vectors were written.
        """
        conn = self._ensure_open()
        written = 0
        now = time.time()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for chunk_id, vector in vectors:
                cursor = conn.execute(
                    "UPDATE chunk SET vector=?, signature=?, embedded_at=? WHERE chunk_id=?",
                    (vector, signature, now, chunk_id),
                )
                if cursor.rowcount:
                    conn.execute("DELETE FROM pending_text WHERE chunk_id=?", (chunk_id,))
                    written += 1
        return written

    def release_stale(self, signature: str) -> int:
        """Clear vectors from another vector space, and re-queue their sessions.

        A cleared chunk has no text left to embed, so every session referencing
        one has its chunker signature blanked: the chunk pass then finds it stale,
        re-chunks it and restores the text. Its ``session_state`` row stays,
        so the prune still sees it if it turns private or leaves the window first.
        Returns how many vectors were cleared.
        """
        conn = self._ensure_open()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cleared = conn.execute(
                "UPDATE chunk SET vector=NULL, signature=NULL, embedded_at=NULL "
                "WHERE vector IS NOT NULL AND signature IS NOT ?",
                (signature,),
            ).rowcount
            if cleared:
                conn.execute(
                    "UPDATE session_state SET chunker='' WHERE session_key IN ("
                    " SELECT r.session_key FROM chunk_ref r JOIN chunk c USING (chunk_id)"
                    " LEFT JOIN pending_text p USING (chunk_id)"
                    " WHERE c.vector IS NULL AND p.chunk_id IS NULL)"
                )
        return cleared

    def embedded_keys(self, signature: str) -> set[str]:
        """Sessions whose every chunk carries a vector in *signature*'s space."""
        rows = (
            self._ensure_open()
            .execute(
                "SELECT session_key FROM session_state WHERE session_key NOT IN ("
                " SELECT r.session_key FROM chunk_ref r JOIN chunk c USING (chunk_id)"
                " WHERE c.vector IS NULL OR c.signature IS NOT ?)",
                (signature,),
            )
            .fetchall()
        )
        return {row[0] for row in rows}

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
            truncated = wal_truncated(conn)
        except Exception:
            truncated = False
        self.erasure_owed = not truncated
        return truncated

    def settle(self) -> bool:
        """Retry an owed WAL truncate; ``True`` when nothing is owed afterwards."""
        if not self.erasure_owed:
            return True
        try:
            self.erasure_owed = not wal_truncated(self._ensure_open())
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

    def close(self) -> None:
        """Release the calling thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None
