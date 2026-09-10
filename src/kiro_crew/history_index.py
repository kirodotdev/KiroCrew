"""SQLite FTS5 candidate index for session content search.

Why an index at all
-------------------
``SessionCatalogProjection.search_sessions`` scores the ``_SEARCH_SCAN_WINDOW``
most recent session files on every query. Each is read whole and
``json.loads``-ed line by line to recover its ``content`` strings, then folded.
Measured on a real 2.96 GB / 1362-file corpus that is 6.7 s per keystroke, of
which ~85% is read + parse — and the parse is nearly all waste: message lines
carry a ``meta`` blob (``file_changes`` before/after text, tool ``input`` /
``output``) that dominates the bytes while contributing nothing searchable. One
52 MB session held 47.3 MB of ``meta`` against 2.4 MB of ``content``, and across
the corpus only ~6% of the bytes on disk are searchable text at all.

The in-memory fold/snippet memos cannot cover a corpus this size: their byte
budgets hold about 100 of the 500 files before refusing admission, so a warm
query re-reads the rest and lands within 30% of cold. Covering the window in
memory would mean holding the corpus resident.

What this is, and what it is NOT
--------------------------------
This index is a **candidate filter and a text source**, never a ranker. The
existing ranking is substring COUNTS per needle per field (see
:func:`kiro_crew.history_search.parse_search_query` and the score expression in
``search_sessions``), which BM25 cannot express — re-ranking through FTS5 would
silently reorder every existing result. Instead:

1. Needles the tokenizers can answer are looked up and intersected, yielding a
   SUPERSET of the true content hits. A superset because needles that cannot be
   looked up are simply not applied as filters — not because the lookup is
   approximate: ``trigram`` matches substrings exactly, the same relation
   ``str.count`` tests.
2. The unchanged scoring code then runs on those candidates only, taking their
   folded text from this store instead of from the JSONL.

A session the index does not vouch for is scanned exactly as before, so the
index can be empty, partial, stale, or absent without changing one result.

Two tokenizers, because one cannot serve both scripts
-----------------------------------------------------
``trigram`` indexes 3-character windows, so it answers substring lookups for
needles of 3+ characters — including ``"pull request 4411"`` and ``"runtime.py"``,
whose spaces and punctuation a word tokenizer would split on. It cannot answer a
1- or 2-character term: there is no entry, and MATCH returns zero rows, which for
a candidate filter is the one failure that loses results.

That gap is exactly where CJK lives. ``parse_search_query`` expands a multi-char
CJK run into one REQUIRED needle per character (plus scoring-only bigrams), so
every Chinese query's gate is made of 1-character needles and trigram can filter
none of it. ``unicode61`` is no help on the raw text either: it treats a whole run
of CJK as ONE token, so a 2-character query does not match a document whose run
merely STARTS with those characters — a longer run is a different token, and a
query for characters in the middle of one matches nothing at all.

So a second column carries the document's DISTINCT CJK characters, space
separated, indexed with ``unicode61``: each character becomes its own token and a
single-character lookup is a token lookup. It is bounded by the character
inventory rather than document length (a few thousand entries at most), which is
why it costs almost nothing next to the trigram index.

The title boundary
------------------
``search_sessions`` gates on title OR content, and this index holds content
only. Titles are short and already in memory from ``list_sessions``, so the
caller unions the index's candidates with the keys whose TITLE satisfies the
gate (``needles_match_text``). Indexing titles here instead would mean storing
the title inside the searchable document, which would then have to be excluded
again from the text the scorer reads — a second projection to keep in sync for
no gain.

Freshness is not a write hook
-----------------------------
Session files are mutated through at least eight paths — append, metadata edit,
rotation, compaction, consolidation, clear-closed, delete, and the dashboard's
own per-turn ``_save_slot_to_history`` rewrite, which notably does NOT call
``_invalidate_cache``. Hanging correctness on hooking all of them invites a
silent miss the day a ninth appears.

Freshness is therefore decided by comparing the caller's ``os.stat`` against the
``(mtime_ns, size)`` recorded with each row; a row is trusted only when both
match. A mismatch downgrades that session to the scan path, so the index is
never *wrong*, only sometimes *incomplete* — the failure direction worth having.
``st_size`` rides along with ``st_mtime_ns`` specifically to catch the
mtime-preserving rewrites this codebase performs (``_restore_mtime``), which a
timestamp alone cannot see.

Rows are written by the background backfill, off the event loop and budgeted.
There is deliberately no append-time write here: the session just appended to is
one stale file on the scan path, and one file was never the cost that mattered.
Making appends incremental means splitting a session across several rows so only
the tail is re-indexed — the next step, not this one.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

from kiro_crew._sqlite_compat import fts5_available, is_cjk_char, sqlite3

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.history_search import SearchNeedle

#: Shortest needle the trigram tokenizer can answer. Below this there is no
#: index entry and MATCH returns nothing, so such needles are left to the exact
#: scoring pass (which still applies them in full) rather than used as filters.
_MIN_TRIGRAM_CHARS = 3

#: Version of the stored term representation. A bump makes existing rows
#: unreadable rather than subtly mismatched: the store drops and rebuilds
#: instead of serving rows indexed under different tokenization rules.
_INDEX_VERSION = 2

_BUSY_TIMEOUT_MS = 10_000
_CONNECT_TIMEOUT_SECS = 30

#: Attempts, and the pause between them, for the WAL truncation that makes a
#: dropped session's bytes unreadable. A checkpoint reports BUSY rather than
#: raising when a reader still holds the WAL, and that is a transient state, so a
#: couple of short retries turn an ordinary overlap into a success instead of a
#: refused deletion. What they must never do is turn a persistent BUSY into a
#: reported success.
_LOGGER = logging.getLogger(__name__)

_WAL_TRUNCATE_ATTEMPTS = 3
_WAL_TRUNCATE_RETRY_SECS = 0.05

#: Name of the index file, kept beside the session directory it describes so a
#: pod or a test home gets its own rather than sharing the live one.
INDEX_FILENAME = "session_index.db"


def cjk_inventory(folded: str) -> str:
    """Space-separated distinct CJK characters of *folded*, in first-seen order.

    The projection the ``unicode61`` column indexes. Order is stable only to keep
    the value diffable while debugging; nothing depends on it.
    """
    seen: dict[str, None] = {}
    for ch in folded:
        if ch not in seen and is_cjk_char(ch):
            seen[ch] = None
    return " ".join(seen)


class SessionSearchIndex:
    """Candidate index over session folded text, with stat-based freshness.

    One instance per session directory. Every method degrades to a no-op or a
    "cannot help" value when the index is unavailable, so callers need no
    feature test beyond :attr:`available`.
    """

    #: Set once a store failure has been reported, so a broken index warns instead
    #: of only ever looking slow -- and warns once rather than per session.
    _sync_failure_warned = False

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self.available = fts5_available()
        if self.available:
            try:
                self._ensure_open()
            except Exception:
                # A corrupt or unwritable index must degrade to the scan path,
                # never break search. Latched so we stop retrying per query.
                self.available = False

    # ------------------------------------------------------------------ setup

    def _ensure_open(self) -> sqlite3.Connection:
        """Return this THREAD's connection, opening it on first use.

        One connection per thread, as ``KnowledgeStore`` does — not one shared
        connection, which is a correctness bug rather than a style choice. Three
        different threads reach this store: the gateway's backfill loop through
        ``asyncio.to_thread``, the search path through ``run_in_executor``, and
        ``delete_session`` on whichever thread deletes. On a SHARED connection a
        second ``BEGIN IMMEDIATE`` raises ``cannot start a transaction within a
        transaction`` — a nesting error that ``busy_timeout`` cannot help with,
        because nothing is waiting on a lock — and one thread's rollback can abort
        another's in-flight write, tearing a row that was supposed to move
        atomically.

        With a connection each, writers contend the normal way (WAL plus
        ``busy_timeout``) and a reader never blocks behind a writer at all.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=_CONNECT_TIMEOUT_SECS,
            isolation_level=None,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        # This store keeps a copy of each session's message text, so a deleted
        # session's content must not survive as readable bytes in a freed page.
        # secure_delete zeroes pages as they are released; drop() truncates the
        # WAL afterwards, which is where their pre-image would otherwise remain.
        conn.execute("PRAGMA secure_delete=ON")
        # Schema work runs once per store, not once per thread: the version gate
        # DROPs tables, and two threads doing that concurrently would race.
        with self._schema_lock:
            if not self._schema_ready:
                self._init_schema(conn)
                self._schema_ready = True
        self._local.conn = conn
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        """Create the schema, dropping rows written under an older version.

        The version gate drops rather than migrates: rows are a pure derivative
        of files still on disk, so a rebuild costs one backfill pass and removes
        any chance of mixing term representations inside one index.
        """
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version and version != _INDEX_VERSION:
            for table in ("session_fts", "session_cjk", "session_entry"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            version = 0
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS session_fts "
            "USING fts5(folded, tokenize='trigram')"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS session_cjk "
            "USING fts5(chars, tokenize='unicode61')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_entry("
            " session_key TEXT PRIMARY KEY,"
            " fts_rowid   INTEGER NOT NULL,"
            " mtime_ns    INTEGER NOT NULL,"
            " size        INTEGER NOT NULL,"
            " dev         INTEGER NOT NULL,"
            " ino         INTEGER NOT NULL,"
            " doc_chars   INTEGER NOT NULL,"
            " raw_texts   BLOB    NOT NULL)"
        )
        if not version:
            conn.execute(f"PRAGMA user_version={_INDEX_VERSION}")

    # ------------------------------------------------------------ bookkeeping

    def fresh_keys(self, stats: dict[str, os.stat_result]) -> dict[str, int]:
        """Return ``{key: fts_rowid}`` for rows matching the caller's stat.

        *stats* maps session key to the ``os.stat_result`` the caller already
        took. A key absent from the result is one the caller must scan: either
        never indexed, or indexed against a different revision of the file.

        The search path calls :meth:`shortlist` instead, which answers this and
        the candidate question from ONE snapshot. This stays for callers that
        need freshness alone.
        """
        if not self.available or not stats:
            return {}
        try:
            return self._fresh_from(self._ensure_open(), stats)
        except Exception:
            return {}

    @staticmethod
    def _fresh_from(conn: sqlite3.Connection, stats: dict[str, os.stat_result]) -> dict[str, int]:
        """Freshness for *stats*, read through an already-open connection."""
        rows = conn.execute(
            "SELECT session_key, fts_rowid, mtime_ns, size, dev, ino FROM session_entry"
        ).fetchall()
        fresh: dict[str, int] = {}
        for key, rowid, mtime_ns, size, dev, ino in rows:
            st = stats.get(key)
            if st is None:
                continue
            if (
                st.st_mtime_ns == mtime_ns
                and st.st_size == size
                and st.st_dev == dev
                and st.st_ino == ino
            ):
                fresh[key] = rowid
        return fresh

    def sync(
        self,
        key: str,
        *,
        mtime_ns: int,
        size: int,
        dev: int,
        ino: int,
        texts: Sequence[str],
    ) -> None:
        """Record *key*'s message *texts* as of the given stat.

        Both projections the search path needs are derived here rather than by
        the caller, so the folded document can never drift from the raw texts it
        came from: the fold is ``"\\x00".join(texts).casefold()`` and
        ``doc_chars`` counts the ORIGINAL characters, matching
        ``SessionCatalogProjection._build_folded`` exactly. The scorer must not be
        able to tell which source its text came from.

        The raw texts are stored compressed and unindexed: they exist only so
        snippet building stops re-reading and re-parsing the JSONL, which
        measured as 858 ms of a 947 ms query once matching itself was indexed.

        Everything moves in one transaction. A half-written session would
        otherwise be vouched for by the entry while its text was missing.
        """
        if not self.available:
            return
        folded = "\x00".join(texts).casefold()
        doc_chars = sum(len(t) for t in texts)
        blob = zlib.compress(json.dumps(list(texts), ensure_ascii=False).encode("utf-8"), 6)
        try:
            conn = self._ensure_open()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                old = conn.execute(
                    "SELECT fts_rowid FROM session_entry WHERE session_key=?", (key,)
                ).fetchone()
                if old is not None:
                    conn.execute("DELETE FROM session_fts WHERE rowid=?", (old[0],))
                    conn.execute("DELETE FROM session_cjk WHERE rowid=?", (old[0],))
                cur = conn.execute("INSERT INTO session_fts(folded) VALUES (?)", (folded,))
                rowid = cur.lastrowid
                # Same rowid in both FTS tables so one entry addresses both.
                conn.execute(
                    "INSERT INTO session_cjk(rowid, chars) VALUES (?,?)",
                    (rowid, cjk_inventory(folded)),
                )
                conn.execute(
                    "INSERT INTO session_entry("
                    " session_key, fts_rowid, mtime_ns, size, dev, ino,"
                    " doc_chars, raw_texts)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(session_key) DO UPDATE SET"
                    "  fts_rowid=excluded.fts_rowid, mtime_ns=excluded.mtime_ns,"
                    "  size=excluded.size, dev=excluded.dev, ino=excluded.ino,"
                    "  doc_chars=excluded.doc_chars,"
                    "  raw_texts=excluded.raw_texts",
                    (key, rowid, mtime_ns, size, dev, ino, doc_chars, blob),
                )
        except Exception:
            # Losing one row costs one scanned file on the next query, so this must
            # not raise. It must not be INVISIBLE either: a schema or parameter
            # mismatch here presents only as "nothing is ever indexed", which reads
            # as a performance mystery rather than an error. Warned once per process,
            # matching how the search memos report their first refusal.
            if not SessionSearchIndex._sync_failure_warned:
                SessionSearchIndex._sync_failure_warned = True
                _LOGGER.warning(
                    "session search index could not store %r; search falls back to "
                    "scanning the transcripts (further failures are silent)",
                    key,
                    exc_info=True,
                )

    def raw_texts(self, key: str, st: os.stat_result) -> list[str] | None:
        """*key*'s original message texts, but only if the row matches *st*.

        Freshness is re-checked here rather than trusted from the caller because
        the snippet pass runs after the scoring pass, without the lock the fold
        took — so the file may have moved on in between. A stale answer would
        show a preview line absent from the session's current text.
        """
        if not self.available:
            return None
        try:
            row = (
                self._ensure_open()
                .execute(
                    "SELECT mtime_ns, size, dev, ino, raw_texts FROM session_entry"
                    " WHERE session_key=?",
                    (key,),
                )
                .fetchone()
            )
        except Exception:
            return None
        if row is None:
            return None
        if (
            row[0] != st.st_mtime_ns
            or row[1] != st.st_size
            or row[2] != st.st_dev
            or row[3] != st.st_ino
        ):
            return None
        try:
            texts = json.loads(zlib.decompress(row[4]).decode("utf-8"))
        except Exception:
            return None
        return texts if isinstance(texts, list) else None

    def drop(self, keys: Iterable[str]) -> bool:
        """Remove *keys*, reporting whether the removal is known to have happened.

        Returns ``False`` on any failure INSTEAD of swallowing it, because one
        caller is a deletion funnel: this store holds a copy of each session's
        message text, so a failure reported as success lets a permanent delete
        complete while the text it deleted stays readable on disk. Keys that were
        never indexed are absent, which is success.

        After a successful removal the WAL is checkpointed with TRUNCATE. With
        ``secure_delete`` on (set at open) the freed pages are zeroed in the
        database file, but the pre-image of those pages lives in the WAL until it
        is truncated — both steps are needed for the bytes to be GONE rather than
        merely unreferenced. The checkpoint's own verdict is read, so a BUSY
        checkpoint is reported as failure rather than as erasure.
        """
        if not self.available:
            return False
        keys = list(keys)
        if not keys:
            return True
        try:
            conn = self._ensure_open()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                for key in keys:
                    row = conn.execute(
                        "SELECT fts_rowid FROM session_entry WHERE session_key=?", (key,)
                    ).fetchone()
                    if row is None:
                        continue
                    conn.execute("DELETE FROM session_fts WHERE rowid=?", (row[0],))
                    conn.execute("DELETE FROM session_cjk WHERE rowid=?", (row[0],))
                    conn.execute("DELETE FROM session_entry WHERE session_key=?", (key,))
            # Outside the transaction: a checkpoint cannot run inside one.
            return self._truncate_wal(conn)
        except Exception:
            return False

    def _truncate_wal(self, conn: sqlite3.Connection) -> bool:
        """Truncate the WAL, reporting whether it actually happened.

        ``PRAGMA wal_checkpoint(TRUNCATE)`` does NOT raise when it cannot proceed:
        it returns ``(busy, log_pages, checkpointed_pages)`` with ``busy=1``, so a
        caller that ignores the row reports an erasure it did not perform. A reader
        outliving ``busy_timeout`` is what makes it busy, so a few short retries
        clear the ordinary case without weakening the verdict.
        """
        for attempt in range(_WAL_TRUNCATE_ATTEMPTS):
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row is None or int(row[0]) == 0:
                return True
            if attempt + 1 < _WAL_TRUNCATE_ATTEMPTS:
                time.sleep(_WAL_TRUNCATE_RETRY_SECS)
        return False

    def store_exists(self) -> bool:
        """Whether an index file is on disk, regardless of :attr:`available`.

        A caller that must not destroy a transcript while a copy of its text could
        still be readable has to tell "there is no index" from "there is an index
        this process cannot open". The first is safe; the second is not. An
        unreadable path answers ``True``, because unprovable absence is not absence.
        """
        try:
            return self._db_path.exists()
        except OSError:
            return True

    def indexed_keys(self) -> set[str]:
        """Every key currently carrying a row, for reconciling against disk."""
        if not self.available:
            return set()
        try:
            rows = self._ensure_open().execute("SELECT session_key FROM session_entry").fetchall()
        except Exception:
            return set()
        return {r[0] for r in rows}

    # ----------------------------------------------------------------- lookup

    @staticmethod
    def _lookups(needles: Sequence["SearchNeedle"]) -> list[tuple[int, str, str]]:
        """``(needle position, table, MATCH expression)`` for each lookupable needle.

        Only REQUIRED needles are considered: a scoring-only needle does not
        gate, so filtering on one would drop rows that legitimately match.

        A needle carrying alternative spellings (a forge reference) is satisfied
        by ANY of them, so it becomes one OR group — and only when EVERY spelling
        is long enough to look up, since a group missing one spelling would
        exclude documents that satisfy the needle through it.

        One entry per needle rather than one combined expression: the caller has
        to union each needle's content hits with that needle's TITLE hits before
        intersecting, because the gate is per-needle across title OR content. A
        pre-intersected set cannot be corrected for titles afterwards.
        """
        out: list[tuple[int, str, str]] = []
        for pos, needle in enumerate(needles):
            if not needle.required:
                continue
            spellings = (needle.text, *needle.alts)
            if len(spellings) == 1 and len(needle.text) == 1 and is_cjk_char(needle.text):
                out.append((pos, "session_cjk", needle.text))
                continue
            if any(len(s) < _MIN_TRIGRAM_CHARS for s in spellings):
                continue
            out.append((pos, "session_fts", _or_group(spellings)))
        return out

    @staticmethod
    def _candidates_from(
        conn: sqlite3.Connection, lookups: list[tuple[int, str, str]]
    ) -> dict[int, set[str]]:
        """Run *lookups* through an already-open connection."""
        out: dict[int, set[str]] = {}
        for pos, table, expr in lookups:
            rows = conn.execute(
                f"SELECT e.session_key FROM {table} t"
                f" JOIN session_entry e ON e.fts_rowid = t.rowid"
                f" WHERE {table} MATCH ?",
                (expr,),
            ).fetchall()
            out[pos] = {r[0] for r in rows}
        return out

    def shortlist(
        self, stats: dict[str, os.stat_result], needles: Sequence["SearchNeedle"]
    ) -> tuple[dict[str, int], dict[int, set[str]] | None]:
        """Freshness and candidates, read from ONE database snapshot.

        Reading them separately races the backfill, and the race silently drops a
        result. A row present when freshness is read and dropped before the
        candidate lookups leaves its session BOTH vouched for (so the caller does
        not treat it as unvouched) and absent from every candidate set (so the
        caller excludes it) — losing a session the scan path would have returned.
        The backfill drops rows for sessions that left ITS window snapshot, and a
        session at the window boundary is routinely in one snapshot and out of the
        other, so this is an ordinary interleaving rather than a rare one.

        Swapping the two calls does not fix it, it only moves the hole to a row
        that ARRIVES between them: that session would be absent from candidates
        and present in freshness, and excluded just the same. One snapshot is the
        fix. WAL gives a read transaction a stable view, so both answers describe
        the same index state and the caller's ``unvouched`` set is derived from it.

        Returns ``({}, None)`` when unavailable or on error, which degrades the
        caller to a full scan rather than to a wrong answer.
        """
        if not self.available:
            return ({}, None)
        lookups = self._lookups(needles)
        try:
            conn = self._ensure_open()
            conn.execute("BEGIN")
            try:
                fresh = self._fresh_from(conn, stats) if stats else {}
                candidates = self._candidates_from(conn, lookups) if lookups else None
                return (fresh, candidates)
            finally:
                conn.execute("COMMIT")
        except Exception:
            return ({}, None)

    def document(self, rowid: int, session_key: str) -> tuple[int, str] | None:
        """Return ``(doc_chars, folded)`` for *rowid*, but only if it is still *session_key*'s.

        The rowid alone is not an identity. SQLite reuses a freed rowid, and the
        backfill frees them: when the oldest in-window session departs it drops the
        row holding the current max ``fts_rowid``, and the next ``sync`` can reuse
        that number for a different session. A caller holding a rowid from an
        earlier snapshot would then be handed ANOTHER session's text and score its
        own session against it -- silently dropping or misranking a hit the scan
        path returns, which is exactly the equivalence guarantee this index exists
        to preserve.

        So the key is part of the lookup, matching the guard :meth:`raw_texts`
        already applies on the snippet path. A mismatch answers ``None``, which
        sends the caller back to reading the transcript.
        """
        if not self.available:
            return None
        try:
            row = (
                self._ensure_open()
                .execute(
                    "SELECT e.doc_chars, f.folded FROM session_fts f"
                    " JOIN session_entry e ON e.fts_rowid = f.rowid"
                    " WHERE f.rowid=? AND e.session_key=?",
                    (rowid, session_key),
                )
                .fetchone()
            )
        except Exception:
            return None
        if row is None:
            return None
        return (int(row[0]), row[1] or "")

    # ------------------------------------------------------------ maintenance

    def optimize(self) -> None:
        """Merge segments and reclaim the pages a delete left behind.

        Two separate jobs, both on this slow cadence rather than per write.

        Merging: FTS5 removes a row by writing a delete marker into a new segment,
        so a long-lived index pays for its own history on every query until
        segments merge.

        Reclaiming: a dropped row's trigram postings survive the DELETE. Only the
        content table's pages are zeroed by ``secure_delete``; the tombstoned
        postings sit in segment pages that are not freed, so 3-character fragments
        of a deleted session's text stay readable in the file. Measured on a small
        index seeded with noise plus one doomed session carrying a unique word:

            optimize only        fragments 2 -> 2
            VACUUM only          fragments 2 -> 2
            optimize + VACUUM    fragments 2 -> 0
            rebuild              fragments 2 -> 0

        So the merge alone does NOT reclaim them and neither does ``VACUUM`` alone;
        the pair does, and only once a checkpoint follows. In WAL mode ``VACUUM``
        writes the rewritten database into the WAL, so the old pages stay in the main
        file until a checkpoint replaces them -- measured per file, the fragments sit
        in the db after the VACUUM and are gone after the truncate. All three rewrite
        the whole index, which at the measured 513 MB is not something an interactive
        delete can pay -- so :meth:`drop` does the part that is cheap and bounded
        (zeroing the content pages, truncating the WAL) and this pass bounds the
        fragments' lifetime to one maintenance interval.
        """
        if not self.available:
            return
        try:
            conn = self._ensure_open()
            with conn:
                conn.execute("INSERT INTO session_fts(session_fts) VALUES('optimize')")
                conn.execute("INSERT INTO session_cjk(session_cjk) VALUES('optimize')")
            # Outside the transaction: neither VACUUM nor a checkpoint runs inside one.
            conn.execute("VACUUM")
            self._truncate_wal(conn)
        except Exception:
            pass

    def close(self) -> None:
        """Release the CALLING thread's connection.

        Per-thread by construction, so this cannot close another thread's handle;
        a thread that never opened one has nothing to close. Connections belonging
        to threads that have exited are released when their ``threading.local``
        storage is collected.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None


def _or_group(spellings: Sequence[str]) -> str:
    """FTS5 MATCH expression satisfied by any one of *spellings*."""
    quoted = " OR ".join('"' + s.replace('"', '""') + '"' for s in spellings)
    return f"({quoted})"
