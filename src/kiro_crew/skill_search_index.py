"""Persistent term index for ``skill_search`` — body discovery without the disk.

``SkillsLoader.search_skills`` scores a query against each skill's key, name and
description, and consults the skill BODY only when that metadata misses entirely.
Read straight from the files, that fallback costs one file read per skill, so the
query that needs the body MOST — one whose words appear in no metadata — is the
one that reads every ``SKILL.md`` on the machine, at a cost that grows with total
body bytes rather than with the number of matches.

This module stores each unconfined skill's term VOCABULARY (the distinct tokens of
its body, from the same tokenizer the query goes through) in one SQLite file beside
the usage ledger, and answers "which skills carry a term starting with X" from an
index. The cost then follows the query.

Two boundaries are deliberate:

* **Confined project bodies are never indexed.** A project skill is read through
  the descriptor-pinned reader under a byte cap, and its text belongs to that
  checkout; copying its terms into a home-level database would outlive the grant
  and expose them to a session that never opened the project. Those skills keep
  the read-at-search path.
* **Prefix, not free substring.** A stored term matches a query term that is a
  prefix of it, so ``deploy`` still reaches a body that says ``deployment``. A
  query term sitting strictly INSIDE a body word stops matching — the query is
  tokenized the same way, so a term that is neither a whole word nor a word's
  start is rare, and the range scan this buys is what removes the corpus-sized
  cost.

Best effort throughout: an unusable database (read-only home, a lock held past the
timeout, a corrupt file) makes every method return ``None`` so the caller keeps its
own file-reading fallback. An unconfined body above the index ceiling also declines
the current sync, preserving the legacy full-body search instead of indexing a
misleading prefix. Indexed bodies pass through the repository's descriptor-pinned,
no-link reader before their terms can reach SQLite. Nothing here is durable state —
deleting the file costs one re-index.

The same file also stores the **catalog snapshot**: which skills a given root set
enumerated to. That is what lets a fresh process answer a turn without walking the
tree, and it is why a snapshot records only what EXISTS — never who may read it, and
never what a file CONTAINED. Mapping scope, disabled apps, project consent and
``repo_scope`` are re-evaluated live on top of a served snapshot, metadata freshness
is still decided by a live stat against ``skill_metadata``, and every body still goes
through the descriptor-pinned no-link reader. So a stored row can neither widen what
an agent reaches nor vouch for bytes that changed since; the worst it can do is name
a file whose own read then refuses it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Iterable, Sequence

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.memory_recall import recall_terms

logger = logging.getLogger(__name__)

#: Co-located with ``skill_usage.json`` in the Kiro Crew home, so runtime state
#: travels together and a home copy carries a warm index.
SKILL_SEARCH_INDEX_FILENAME = "skill_search_index.sqlite3"

#: Bumped when the table shape changes; a mismatch drops and rebuilds rather than
#: migrating, because every row is derived data one read can regenerate.
_SCHEMA_VERSION = 5

#: Another process may be indexing the same skill. Wait briefly, then give up and
#: let the caller read files this once rather than block a chat turn on a lock.
_BUSY_TIMEOUT_SECS = 2.0

#: Above this size, decline the index for the current search so the caller uses
#: the legacy full-body reader. Indexing only a prefix would silently hide terms
#: later in an otherwise valid global skill.
_MAX_INDEXED_BODY_BYTES = 1_000_000

#: Ceilings on what a stored CATALOG may claim. The index is an agent-writable
#: crew-home leaf, so its row count and field widths are adversary-controlled, and
#: `catalog_snapshot` materializes every row it is handed — an unbounded table
#: would be an out-of-memory crash on every load, with no self-correcting path.
#: Both are far above any real install (a 5,000-skill tree stores 5,000 rows, and a
#: skill path is a filesystem path) and far below anything that threatens the
#: process, so a snapshot past either is a corrupt or hostile table rather than a
#: large one. Exceeding a ceiling rejects the WHOLE snapshot instead of dropping
#: rows: a truncated catalog would be served as if it were complete.
_MAX_CATALOG_ROWS = 100_000
_MAX_CATALOG_FIELD_CHARS = 4_096

#: Lone surrogates cannot be encoded, so an increment that lands in that block
#: skips past it to the first scalar value above.
_SURROGATE_START = 0xD800
_SURROGATE_END = 0xDFFF

_DDL = """
CREATE TABLE IF NOT EXISTS skill_index_schema (
    version INTEGER NOT NULL,
    tokenizer TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_body (
    key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_term (
    term TEXT NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (term, key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS skill_metadata (
    path TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_meta_term (
    term TEXT NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (term, path)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS skill_meta_term_path ON skill_meta_term(path);
CREATE TABLE IF NOT EXISTS skill_catalog_scope (
    scope TEXT PRIMARY KEY,
    built_at REAL NOT NULL,
    epoch INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_catalog (
    scope TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    key TEXT NOT NULL,
    path TEXT NOT NULL,
    confine_root TEXT NOT NULL,
    PRIMARY KEY (scope, ordinal)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS skill_catalog_epoch (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    epoch INTEGER NOT NULL
);
INSERT OR IGNORE INTO skill_catalog_epoch (id, epoch) VALUES (0, 0);
"""

_DROP = """
DROP TABLE IF EXISTS skill_catalog;
DROP TABLE IF EXISTS skill_catalog_scope;
DROP TABLE IF EXISTS skill_catalog_epoch;
DROP TABLE IF EXISTS skill_term;
DROP TABLE IF EXISTS skill_metadata;
DROP TABLE IF EXISTS skill_meta_term;
DROP TABLE IF EXISTS skill_body;
DROP TABLE IF EXISTS skill_index_schema;
"""

#: Exercises the tokenizer decisions a stored term depends on: case folding,
#: punctuation and separator handling, digits, a minimum length, a hyphenated
#: compound, an accent, and a non-Latin script. Changing any of them changes this
#: string's tokens, which is exactly when stored terms stop matching new queries.
_TOKENIZER_PROBE = "Rollback the CI-2 deploy_now; a Ärger 数据 x ab abc."


def tokenizer_signature() -> str:
    """Fingerprint of the TOKENIZER, so a change to it invalidates the index.

    ``recall_terms`` is shared, evolving code. Stored terms are its output, so a
    change in how it splits or normalizes silently leaves rows that fail to match a
    new query — a miss, not an error, and therefore invisible. Deriving the
    signature from the tokenizer's ANSWER on a fixed probe means no future editor
    has to remember to bump anything: the rebuild follows from the behaviour change
    itself.

    Source hashing was rejected: a comment or rename would discard every row for
    no behavioural reason.
    """
    terms = recall_terms(_TOKENIZER_PROBE)
    joined = "\u0000".join(sorted(terms))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def prefix_upper_bound(term: str) -> str | None:
    """Exclusive upper bound of the range of strings starting with *term*.

    Increments the last code point, which is above every string starting with
    *term* in code-point order AND in the UTF-8 byte order a BINARY-collated
    index compares. Appending a high sentinel does NOT hold: ``\\uffff`` encodes
    as ``EF BF BF`` while an astral character starts at ``F0``, so a stored term
    like ``𠀀𠀁`` sorted ABOVE ``𠀀\\uffff`` and its own prefix missed it.

    ``None`` means no bound is representable — the last code point is already the
    maximum — and the caller then scans from the lower bound alone.
    """
    if not term:
        return None
    last = ord(term[-1]) + 1
    if _SURROGATE_START <= last <= _SURROGATE_END:
        last = _SURROGATE_END + 1
    if last > 0x10FFFF:
        return None
    return term[:-1] + chr(last)


def _is_transient(exc: BaseException) -> bool:
    """Is *exc* a database that is merely BUSY rather than unusable?

    Another process holding the write lock past the two-second timeout says
    nothing about this file's health, so latching on it would turn one unlucky
    moment into a process-lifetime downgrade to reading every body. SQLite
    reports both as ``OperationalError``; only the lock wording is transient.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def body_fingerprint(path: str | Path) -> str | None:
    """Cheap body-file identity for staleness checks.

    Device, inode and change time distinguish a same-path replacement even when
    it preserves modification time and byte size. Reading a digest here would
    defeat the warm-index path by reading every body before deciding whether it
    changed. An unreadable path returns ``None``, which the caller treats as
    "cannot index this one".
    """
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_ctime_ns}:" f"{stat.st_mtime_ns}:{stat.st_size}"


class SkillSearchIndex:
    """Term vocabulary per skill key, persisted in one SQLite file.

    One connection serves every caller, guarded by a lock, and opened with
    ``check_same_thread=False``. A skill search runs off the gateway event loop —
    the dashboard route hands it to a thread, the MCP tool runs in its own
    subprocess — so two searches legitimately arrive on different threads. A
    thread-bound connection would raise there, and because an exception latches
    this index unusable, ONE such call would drop every later search back to
    reading files for the life of the process.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._unusable = False
        # Re-entrant: the public methods take it and then call _db(), which takes
        # it again. SQLite serializes writers anyway; this serializes the shared
        # connection object, which is what check_same_thread=False stops policing.
        self._lock = threading.RLock()
        self.pending_keys: frozenset[str] = frozenset()

    # ── connection ──

    def close(self) -> None:
        """Close this process's SQLite handle; safe to call more than once."""
        with self._lock:
            conn, self._conn = self._conn, None
            self._unusable = True
            if conn is not None:
                conn.close()

    def _db(self) -> sqlite3.Connection | None:
        """The open connection, or ``None`` once this index is unusable.

        A failure latches -- retrying a broken database on every search would pay
        the same exception and log line per call -- EXCEPT a busy or locked one,
        which says only that a neighbour held the write lock longer than the
        timeout. Latching on that would spend the rest of the process reading
        every body over one contended moment.
        """
        with self._lock:
            if self._unusable:
                return None
            if self._conn is not None:
                return self._conn
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(self._path), timeout=_BUSY_TIMEOUT_SECS, check_same_thread=False
                )
                # WAL so a reader is never blocked by the indexing writer; NORMAL
                # because a lost commit only costs a re-index.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(_DDL)
                signature = tokenizer_signature()
                row = conn.execute("SELECT version, tokenizer FROM skill_index_schema").fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO skill_index_schema (version, tokenizer) VALUES (?, ?)",
                        (_SCHEMA_VERSION, signature),
                    )
                elif int(row[0]) != _SCHEMA_VERSION or str(row[1]) != signature:
                    # A tokenizer change is as invalidating as a schema change:
                    # stored terms are its output, so old rows would simply stop
                    # matching new queries with no error to notice.
                    conn.executescript(_DROP)
                    conn.executescript(_DDL)
                    conn.execute(
                        "INSERT INTO skill_index_schema (version, tokenizer) VALUES (?, ?)",
                        (_SCHEMA_VERSION, signature),
                    )
                conn.commit()
            except (sqlite3.Error, OSError, ValueError) as exc:
                logger.warning(
                    "skill-search-index: unusable at %s; search falls back to reading bodies",
                    self._path,
                    exc_info=True,
                )
                if not _is_transient(exc):
                    self._unusable = True
                return None
            self._conn = conn
            return conn

    # ── writing ──

    def metadata_snapshot(self) -> dict[str, tuple[str, dict]]:
        """One bulk read of derived global metadata; project grants never enter it."""
        with self._lock:
            db = self._db()
            if db is None:
                return {}
            try:
                result = {}
                for path, fingerprint, raw in db.execute(
                    "SELECT path, fingerprint, metadata FROM skill_metadata"
                ):
                    meta = json.loads(raw)
                    if isinstance(meta, dict) and all(
                        isinstance(k, str) and isinstance(v, str) for k, v in meta.items()
                    ):
                        result[path] = (fingerprint, meta)
                return result
            except (sqlite3.Error, ValueError):
                return {}

    def store_metadata(self, rows: Sequence[tuple[str, str, dict]]) -> bool:
        """Publish only metadata parsed from the caller's safely admitted bytes."""
        if not rows:
            return True
        with self._lock:
            db = self._db()
            if db is None:
                return False
            try:
                db.executemany(
                    "INSERT OR REPLACE INTO skill_metadata(path, fingerprint, metadata) VALUES (?, ?, ?)",
                    [
                        (path, fingerprint, json.dumps(meta, ensure_ascii=False))
                        for path, fingerprint, meta in rows
                    ],
                )
                for path, _fingerprint, meta in rows:
                    db.execute("DELETE FROM skill_meta_term WHERE path = ?", (path,))
                    terms = recall_terms(
                        f"{meta.get('_catalog_key', '')} {meta.get('name', '')} {meta.get('description', '')}".lower()
                    )
                    db.executemany(
                        "INSERT OR IGNORE INTO skill_meta_term(term, path) VALUES (?, ?)",
                        [(term, path) for term in terms],
                    )
                db.commit()
                return True
            except (sqlite3.Error, OSError):
                try:
                    db.rollback()
                except sqlite3.Error:
                    pass
                logger.debug("skill-search-index: metadata persistence unavailable", exc_info=True)
                return False

    def metadata_matches(self, terms: Iterable[str]) -> dict[str, set[str]] | None:
        """Lookup metadata candidates through term ranges, without scanning rows."""
        with self._lock:
            db = self._db()
            if db is None:
                return None
            found: dict[str, set[str]] = {}
            try:
                for term in terms:
                    upper = prefix_upper_bound(term)
                    if upper is None:
                        rows = db.execute(
                            "SELECT DISTINCT path FROM skill_meta_term WHERE term >= ?", (term,)
                        )
                    else:
                        rows = db.execute(
                            "SELECT DISTINCT path FROM skill_meta_term WHERE term >= ? AND term < ?",
                            (term, upper),
                        )
                    for (path,) in rows:
                        found.setdefault(path, set()).add(term)
                return found
            except sqlite3.Error:
                return None

    # ── catalog snapshot ──

    def catalog_epoch(self) -> int | None:
        """The current invalidation epoch, or ``None`` when unreadable.

        A catalog build reads this BEFORE it walks and hands it back to
        :meth:`store_catalog`, which refuses a write whose epoch has moved. That
        is what stops a walk already in flight in one process from republishing a
        pre-mutation enumeration over an invalidation another process just made —
        an in-process generation counter cannot see that.
        """
        with self._lock:
            db = self._db()
            if db is None:
                return None
            try:
                row = db.execute("SELECT epoch FROM skill_catalog_epoch WHERE id = 0").fetchone()
            except sqlite3.Error:
                return None
            return int(row[0]) if row is not None else None

    def catalog_snapshot(self, scope: str) -> tuple[list[tuple[str, str, str]], float] | None:
        """The stored enumeration for *scope*, plus the wall clock it was built at.

        ``None`` means there is nothing to serve — an unusable database, or a
        scope this machine has never enumerated — and the caller must then build
        the list itself. A scope that WAS enumerated and found nothing returns an
        empty row list, which is a different answer: a machine with no skills
        must not be re-walked on every turn just because the answer is nothing.

        Rows come back in stored ``ordinal`` order because enumeration order IS
        precedence: the first key wins, so a re-ordered snapshot could hand a
        caller a different file for the same skill name than the walk would.

        A table past ``_MAX_CATALOG_ROWS``, or carrying a field wider than
        ``_MAX_CATALOG_FIELD_CHARS``, is rejected WHOLE and answers ``None`` — the
        caller then walks. This file is an agent-writable crew-home leaf, so those
        are adversary-controlled numbers, and materializing an unbounded table would
        be an out-of-memory crash on every load; dropping only the offending rows
        would instead serve a truncated catalog as though it were complete.
        """
        with self._lock:
            db = self._db()
            if db is None:
                return None
            try:
                scope_row = db.execute(
                    "SELECT built_at FROM skill_catalog_scope WHERE scope = ?", (scope,)
                ).fetchone()
                if scope_row is None:
                    return None
                rows = [
                    (str(key), str(path), str(confine_root))
                    for key, path, confine_root in db.execute(
                        "SELECT key, path, confine_root FROM skill_catalog "
                        "WHERE scope = ? ORDER BY ordinal LIMIT ?",
                        (scope, _MAX_CATALOG_ROWS + 1),
                    )
                ]
                if len(rows) > _MAX_CATALOG_ROWS:
                    logger.warning(
                        "skill-search-index: refusing a catalog of more than %d rows",
                        _MAX_CATALOG_ROWS,
                    )
                    return None
                if any(len(field) > _MAX_CATALOG_FIELD_CHARS for row in rows for field in row):
                    logger.warning("skill-search-index: refusing a catalog row field over cap")
                    return None
                return rows, float(scope_row[0])
            except (sqlite3.Error, ValueError, TypeError, MemoryError):
                return None

    def store_catalog(
        self, scope: str, rows: Sequence[tuple[str, str, str]], *, epoch: int | None
    ) -> str:
        """Replace *scope*'s stored enumeration with *rows*, in one transaction.

        Rows are ``(key, path, confine_root)``, where ``confine_root`` carries the
        empty string when the row is unconfined. Replacing rather than merging is
        deliberate: a merge keeps a row for a skill the walk does not find, and a
        deleted skill must not stay discoverable.

        *epoch* is the value :meth:`catalog_epoch` returned before the walk began.

        Three outcomes, and the caller must tell them apart because they mean
        opposite things about the rows it holds:

        ``"stored"``
            the rows are durable.
        ``"stale"``
            the epoch moved, so the rows describe the tree as it was before a
            mutation somebody has already recorded. The caller must NOT serve them.
        ``"unavailable"``
            there is no usable database (a read-only home, a corrupt file, an epoch
            that could not be read). The rows are fine; only persistence is missing,
            so refusing to serve them would make every turn re-walk instead.

        The epoch check runs inside an ``IMMEDIATE`` transaction, so the write lock
        is held before it is read. ``self._lock`` serializes this process only; a
        deferred transaction would leave the check and the insert open to another
        process's :meth:`drop_catalog` landing between them, which is exactly the
        stale republish the epoch exists to stop.
        """
        if epoch is None:
            return "unavailable"
        with self._lock:
            db = self._db()
            if db is None:
                return "unavailable"
            try:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT epoch FROM skill_catalog_epoch WHERE id = 0"
                ).fetchone()
                if current is None or int(current[0]) != epoch:
                    db.rollback()
                    return "stale"
                db.execute("DELETE FROM skill_catalog WHERE scope = ?", (scope,))
                db.executemany(
                    "INSERT INTO skill_catalog(scope, ordinal, key, path, confine_root) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (scope, ordinal, key, path, confine_root)
                        for ordinal, (key, path, confine_root) in enumerate(rows)
                    ],
                )
                db.execute(
                    "INSERT INTO skill_catalog_scope (scope, built_at, epoch) VALUES (?, ?, ?) "
                    "ON CONFLICT(scope) DO UPDATE SET built_at = excluded.built_at, "
                    "epoch = excluded.epoch",
                    (scope, time.time(), epoch),
                )
                db.commit()
                return "stored"
            except (sqlite3.Error, OSError, ValueError, TypeError):
                try:
                    db.rollback()
                except sqlite3.Error:
                    pass
                logger.debug("skill-search-index: catalog persistence unavailable", exc_info=True)
                return "unavailable"

    def drop_catalog(self) -> None:
        """Forget every stored enumeration and bump the epoch.

        Called when a skill is created, updated or deleted. It clears ALL scopes
        rather than the mutating one: a scope is derived from a root set plus a
        project key, and one skills root is shared by every scope that includes
        it, so narrowing the delete would leave a stale row under a neighbouring
        scope. The whole table is derived data one walk regenerates, which is why
        the cheap answer is also the correct one.

        The epoch bump is the other half, and it is not optional: without it a
        walk that started before this call would store its pre-mutation rows
        afterwards and silently undo the invalidation for every later process. It
        shares one ``IMMEDIATE`` transaction with the delete so a concurrent
        :meth:`store_catalog` cannot land between the two.
        """
        with self._lock:
            db = self._db()
            if db is None:
                return
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM skill_catalog")
                db.execute("DELETE FROM skill_catalog_scope")
                db.execute("UPDATE skill_catalog_epoch SET epoch = epoch + 1 WHERE id = 0")
                db.commit()
            except (sqlite3.Error, OSError):
                try:
                    db.rollback()
                except sqlite3.Error:
                    pass
                logger.debug("skill-search-index: catalog invalidation failed", exc_info=True)

    def sync(
        self,
        rows: Sequence[tuple[str, str, str]],
        *,
        live_keys: Iterable[str] | None = None,
        budget_seconds: float | None = None,
        canonical_roots: dict[str, str] | None = None,
    ) -> frozenset[str] | None:
        """Bring the index up to date for *rows* of ``(key, path, fingerprint)``.

        Only a key whose stored fingerprint differs is re-read, so a warm index
        costs one small query. Returns ``None`` when the database is unusable --
        the signal the caller needs to read every body itself -- and otherwise the
        set of keys this index declines to answer for, which the caller reads
        directly. That set is normally empty.

        Declining PER KEY rather than for the whole call is deliberate: one
        pathological body would otherwise send an entire catalog back to reading
        files on every search, which is the cost this index exists to remove.

        *live_keys*, when given, is the full set of keys currently visible to the
        caller. It prunes rows for skills that are gone, which is space only:
        scoring maps hits onto keys the caller already holds, so a stale row is
        inert either way.
        """
        with self._lock:
            return self._sync_locked(rows, live_keys, budget_seconds, canonical_roots)

    def _sync_locked(
        self,
        rows: Sequence[tuple[str, str, str]],
        live_keys: Iterable[str] | None,
        budget_seconds: float | None = None,
        canonical_roots: dict[str, str] | None = None,
    ) -> frozenset[str] | None:
        """``sync`` with the connection lock already held."""
        db = self._db()
        if db is None:
            return None
        deferred: set[str] = set()
        pending: set[str] = set()
        deadline = time.monotonic() + budget_seconds if budget_seconds is not None else None
        try:
            stored = dict(db.execute("SELECT key, fingerprint FROM skill_body").fetchall())
            for key, path, fingerprint in rows:
                if stored.get(key) == fingerprint:
                    continue
                if deadline is not None and time.monotonic() >= deadline:
                    pending.add(key)
                    deferred.add(key)
                    continue
                root = (canonical_roots or {}).get(key)
                terms = (
                    self._read_terms(path, canonical_root=root) if root else self._read_terms(path)
                )
                if terms is None:
                    # Either the hardened reader refused these bytes or the body is
                    # past the index ceiling. Drop the old vocabulary so a stale
                    # body stays unsearchable, store no fingerprint so the next
                    # search retries, and hand the key back for a direct read --
                    # the caller's reader decides, so this file is no less
                    # searchable than it was before an index existed.
                    db.execute("DELETE FROM skill_term WHERE key = ?", (key,))
                    db.execute("DELETE FROM skill_body WHERE key = ?", (key,))
                    deferred.add(key)
                    continue
                db.execute("DELETE FROM skill_term WHERE key = ?", (key,))
                db.executemany(
                    "INSERT OR IGNORE INTO skill_term (term, key) VALUES (?, ?)",
                    [(term, key) for term in terms],
                )
                db.execute(
                    "INSERT INTO skill_body (key, fingerprint) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET fingerprint = excluded.fingerprint",
                    (key, fingerprint),
                )
            if live_keys is not None:
                live = set(live_keys)
                gone = [key for key in stored if key not in live]
                if gone:
                    db.executemany("DELETE FROM skill_term WHERE key = ?", [(k,) for k in gone])
                    db.executemany("DELETE FROM skill_body WHERE key = ?", [(k,) for k in gone])
            db.commit()
            self.pending_keys = frozenset(pending)
            return frozenset(deferred)
        except (sqlite3.Error, OSError) as exc:
            logger.warning(
                "skill-search-index: sync failed; search falls back to reading bodies",
                exc_info=True,
            )
            if not _is_transient(exc):
                self._unusable = True
            return None

    def _read_terms(
        self, path: str | Path, *, canonical_root: str | None = None
    ) -> frozenset[str] | None:
        """Tokenize one body file, or ``None`` when this index will not answer for it.

        Bytes reach SQLite only through the repository's descriptor-pinned no-link
        reader, so a link, a hardlinked inode, a non-regular file, a sensitive
        target or a file swapped between validation and open cannot put terms in
        the database. A body past the index ceiling is refused for the same
        reason a prefix would be wrong: a term after the cut would read as absent.

        Both answers mean "ask the caller to read this one", never "this body has
        no terms" -- the distinction the caller needs to keep a file the reader
        declines exactly as searchable as it was before.
        """
        try:
            raw = safe_read_file_bytes_nolink(
                str(path),
                max_bytes=_MAX_INDEXED_BODY_BYTES,
                within_root=canonical_root,
                within_root_is_canonical=canonical_root is not None,
            )
        except FileTooLargeError:
            return None
        if raw is None:
            return None
        text = raw.decode("utf-8", errors="replace")
        return recall_terms(text.lower())

    # ── reading ──

    def body_hits(self, keys: Sequence[str], terms: Iterable[str]) -> dict[str, int] | None:
        """How many of *terms* each key in *keys* carries in its body.

        One prefix range scan per term, counting a term once per key — the same
        quantity the file-reading path produced by asking whether each query term
        appears in the body at all. ``None`` means the database is unusable.
        """
        with self._lock:
            return self._body_hits_locked(keys, terms)

    def body_matches(self, keys: Sequence[str], terms: Iterable[str]) -> dict[str, set[str]] | None:
        """Return term identities so coverage and rarity share one index snapshot."""
        with self._lock:
            found: dict[str, set[str]] = {}
            for term in terms:
                hits = self._body_hits_locked(keys, [term])
                if hits is None:
                    return None
                for key in hits:
                    found.setdefault(key, set()).add(term)
            return found

    def _body_hits_locked(self, keys: Sequence[str], terms: Iterable[str]) -> dict[str, int] | None:
        """``body_hits`` with the connection lock already held."""
        db = self._db()
        if db is None:
            return None
        wanted = set(keys)
        hits: dict[str, int] = {}
        if not wanted:
            return hits
        try:
            for term in terms:
                if not term:
                    continue
                upper = prefix_upper_bound(term)
                if upper is None:
                    rows = db.execute(
                        "SELECT DISTINCT key FROM skill_term WHERE term >= ?", (term,)
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT DISTINCT key FROM skill_term WHERE term >= ? AND term < ?",
                        (term, upper),
                    ).fetchall()
                for (key,) in rows:
                    if key in wanted:
                        hits[key] = hits.get(key, 0) + 1
        except (sqlite3.Error, OSError) as exc:
            logger.warning(
                "skill-search-index: lookup failed; search falls back to reading bodies",
                exc_info=True,
            )
            if not _is_transient(exc):
                self._unusable = True
            return None
        return hits
