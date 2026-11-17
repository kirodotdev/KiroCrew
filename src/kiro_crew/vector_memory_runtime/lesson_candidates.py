"""The rows one lesson write compares against: a resident index instead of a full scan.

A full scan loads every active lesson and runs containment, keyword overlap and cosine
against each, in Python, on the calling thread, so its cost grows with the lesson count
(19,428 on one install). :func:`candidate_lesson_rows` returns only the rows that could
trip one of those checks, and ``write_lesson`` runs the same checks on them.

The index holds, per active lesson, its lowercased rule text, its keyword set and a
256-bit random-hyperplane sketch of its stored vector (32 bytes). A write asks it for
three groups of keys:

* rows whose text contains the rule, or is contained in it (one ``in`` per resident
  text, no JSON decode and no regex);
* rows sharing enough keywords to reach the 50% overlap rule (posting lists, then the
  exact ratio);
* the rows whose sketch is nearest the new vector (Hamming distance, one numpy pass).
  Cosine above 0.85 is an angle under 32 degrees, which flips about 45 of 256 bits
  (sd 6), so a cut at 72 bits and the 64 nearest in scope keep such a row in the set
  with probability above 0.9999.

The index only NOMINATES keys. Every nominated row is re-read from SQLite and the old
per-row checks run on that fresh row, so a stale index entry can cost a missed
candidate, never a wrong supersede. A row with no stored vector has no sketch and is
never a cosine candidate: the write path does not embed it. The backfill sweep
(``backfill_missing_embeddings``) gives it a vector.

The index is built once from the table, then kept current in place: every write that
calls ``_invalidate_semantic_scoring(key)`` re-reads that one row, and the argument-less
call (a bulk change) drops it. Another process committing moves ``PRAGMA data_version``,
which drops it too; the next write rebuilds it. Without numpy, or without
``data_version``, :func:`candidate_lesson_rows` returns ``None`` and the caller scans
every row as before.

All state is guarded by the store's ``_db_lock``.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import threading
import weakref
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.vector_memory_runtime import lessons as _lessons

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger("kiro_crew.vector_memory")

_SKETCH_BITS = 256
_SKETCH_BYTES = _SKETCH_BITS // 8
#: Hamming cut and neighbour cap for the cosine candidates. A row at cosine 0.85 sits
#: near 45 bits (sd 6); unrelated text vectors sit above 80.
_SKETCH_MAX_DISTANCE = 72
_SKETCH_NEIGHBOURS = 64
_KEY_CHUNK = 500
_BUILD_LOCK = threading.Lock()
_STATES_LOCK = threading.Lock()
_PROJECTIONS: dict[int, Any] = {}
_POPCOUNT: list[Any] = []

_LESSON_SELECT = (
    "SELECT key, value_json, embedding, source, confidence FROM semantic_memory "
    "WHERE is_deleted = 0 AND key LIKE 'lesson.%'"
)


class _Entry(NamedTuple):
    scope: str | None
    words: frozenset[str]
    width: int  # float32 count of the stored vector; 0 when it has none
    owner: bool  # source is ``user_explicit``
    confidence: float


class _State:
    """One store's index and the bookkeeping of a build in flight."""

    __slots__ = ("index", "dirty", "poisoned", "seen_write")

    def __init__(self) -> None:
        self.index: LessonCandidateIndex | None = None
        # Whether this store has already asked for candidates once. The first ask is
        # answered by a scan: building the index costs about two scans, so a one-write
        # process (the CLI) never pays for it.
        self.seen_write = False
        # Keys written while a build reads the table, and whether a bulk change landed.
        self.dirty: set[str] | None = None
        self.poisoned = False


# Per store, held here rather than on the store class, whose members are a pinned surface.
_STATES: weakref.WeakKeyDictionary[Any, _State] = weakref.WeakKeyDictionary()


def _state(store: VectorMemoryStore) -> _State:
    state = _STATES.get(store)
    if state is None:
        with _STATES_LOCK:
            state = _STATES.setdefault(store, _State())
    return state


def _projection(np: Any, width: int) -> Any:
    matrix = _PROJECTIONS.get(width)
    if matrix is None:
        if len(_PROJECTIONS) >= 4:
            _PROJECTIONS.clear()
        rng = np.random.default_rng(0x1E5504 + width)
        matrix = _PROJECTIONS[width] = rng.standard_normal((width, _SKETCH_BITS), dtype=np.float32)
    return matrix


def _sketches(np: Any, matrix: Any, width: int) -> Any:
    """Packed sign bits of each row of *matrix* (n x width) against the width's hyperplanes."""
    return np.packbits(matrix @ _projection(np, width) > 0, axis=1)


def _popcount(np: Any) -> Any:
    if not _POPCOUNT:
        _POPCOUNT.append(np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8))
    return _POPCOUNT[0]


def _blob_width(blob: object) -> int:
    if isinstance(blob, bytes) and len(blob) >= 4 and len(blob) % 4 == 0:
        return len(blob) // 4
    return 0


def _parse(store: VectorMemoryStore, row: Any) -> tuple[str, _Entry, bytes | None] | None:
    """``(text, entry, vector blob)`` for a lesson row the scans would read, else ``None``.

    The same rows ``lesson_rows_in_scope`` and the scan's text guard keep: a value that
    decodes, a usable scope, and some rule text.
    """
    try:
        decoded = json.loads(row["value_json"])
    except (ValueError, TypeError):
        return None
    if _lessons._lesson_scope_unusable(decoded):
        return None
    text = _lessons._lesson_embed_text(decoded)
    if not text:
        return None
    lowered = text.lower()
    blob = row["embedding"]
    width = _blob_width(blob)
    try:
        confidence = float(row["confidence"] or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    entry = _Entry(
        _lessons._lesson_scope(decoded),
        frozenset(sys.intern(word) for word in store._lesson_keywords(lowered)),
        width,
        row["source"] == "user_explicit",
        confidence,
    )
    return lowered, entry, blob if width else None


class LessonCandidateIndex:
    """Resident per-lesson text, keywords and vector sketch, keyed by lesson key."""

    def __init__(self, data_version: int) -> None:
        self.data_version = data_version
        self.texts: dict[str, str] = {}
        self.entries: dict[str, _Entry] = {}
        self.postings: dict[str, set[str]] = {}
        self.sketches: dict[int, dict[str, bytes]] = {}
        # Keys patched while their transaction was open: re-read once it has ended,
        # because a rollback leaves no other trace.
        self.pending: set[str] = set()

    def _forget(self, key: str) -> None:
        old = self.entries.pop(key, None)
        if old is None:
            return
        del self.texts[key]
        for word in old.words:
            keys = self.postings.get(word)
            if keys is not None:
                keys.discard(key)
                if not keys:
                    del self.postings[word]
        if old.width:
            self.sketches.get(old.width, {}).pop(key, None)

    def _add(self, key: str, text: str, entry: _Entry) -> None:
        self.texts[key] = text
        self.entries[key] = entry
        for word in entry.words:
            self.postings.setdefault(word, set()).add(key)

    def refresh(self, store: VectorMemoryStore, key: str) -> None:
        """Bring *key*'s entry in line with the table (absent, tombstoned or new)."""
        from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

        rows = store._fetch_all_locked(f"{_LESSON_SELECT} AND key = ?", (key,))
        self._forget(key)
        parsed = _parse(store, rows[0]) if rows else None
        if parsed is None:
            return
        text, entry, blob = parsed
        self._add(key, text, entry)
        if blob is not None:
            matrix = vm.np.frombuffer(blob, dtype=vm.np.float32).reshape(1, entry.width)
            self.sketches.setdefault(entry.width, {})[key] = _sketches(vm.np, matrix, entry.width)[
                0
            ].tobytes()

    def reconcile(self, store: VectorMemoryStore) -> None:
        if not self.pending:
            return
        for key in tuple(self.pending):
            self.refresh(store, key)
        if not store.db.in_transaction:
            self.pending.clear()

    def query(
        self,
        np: Any,
        *,
        rule_norm: str,
        rule_lower: str,
        rule_words: set[str],
        scope: str | None,
        vector: Sequence[float] | None,
        source: str,
        confidence: float,
    ) -> set[str]:
        """Keys of in-scope rows that could trip a containment, overlap or cosine check."""
        entries = self.entries
        found = {
            key
            for key, text in self.texts.items()
            if (rule_norm in text or text in rule_lower) and entries[key].scope == scope
        }
        if rule_words:
            # overlap / max(|rule|, |row|) >= 0.5 needs at least half the rule's words.
            need = math.ceil(len(rule_words) / 2)
            shared: Counter[str] = Counter()
            for word in rule_words:
                shared.update(self.postings.get(word, ()))
            for key, count in shared.items():
                if count < need:
                    continue
                entry = entries[key]
                if entry.scope == scope and count / max(len(rule_words), len(entry.words)) >= 0.5:
                    found.add(key)
        if vector:
            found.update(self._nearest(np, vector, scope, source, confidence))
        return found

    def _nearest(
        self,
        np: Any,
        vector: Sequence[float],
        scope: str | None,
        source: str,
        confidence: float,
    ) -> list[str]:
        """In-scope keys within the sketch cut: every row that can veto the write, and
        the nearest ``_SKETCH_NEIGHBOURS`` of the rest."""
        width = len(vector)
        by_key = self.sketches.get(width)
        if not by_key:
            return []
        keys = list(by_key)
        packed = np.frombuffer(b"".join(by_key.values()), dtype=np.uint8)
        packed = packed.reshape(len(keys), _SKETCH_BYTES)
        query = _sketches(np, np.asarray(vector, dtype=np.float32).reshape(1, width), width)[0]
        distance = _popcount(np)[np.bitwise_xor(packed, query)].sum(axis=1, dtype=np.int32)
        close = np.flatnonzero(distance <= _SKETCH_MAX_DISTANCE)
        out: list[str] = []
        plain = 0
        for position in close[np.argsort(distance[close], kind="stable")]:
            key = keys[int(position)]
            entry = self.entries[key]
            if entry.scope != scope:
                continue
            # A row that outranks the write (``_outranks_write``) vetoes it in the
            # authority pre-pass, so none may fall outside the cap. A ``user_explicit``
            # write has no veto.
            vetoes = source != "user_explicit" and (entry.owner or entry.confidence > confidence)
            if vetoes:
                out.append(key)
            elif plain < _SKETCH_NEIGHBOURS:
                out.append(key)
                plain += 1
        return out


def _build(store: VectorMemoryStore, version: int) -> LessonCandidateIndex:
    """One pass over every active lesson; the only full read, paid once per rebuild."""
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    np = vm.np
    index = LessonCandidateIndex(version)
    by_width: dict[int, list[tuple[str, bytes]]] = {}
    for row in store._fetch_all_locked(_LESSON_SELECT, scan="semantic"):
        parsed = _parse(store, row)
        if parsed is None:
            continue
        text, entry, blob = parsed
        index._add(row["key"], text, entry)
        if blob is not None:
            by_width.setdefault(entry.width, []).append((row["key"], blob))
    for width, held in by_width.items():
        sketched = index.sketches.setdefault(width, {})
        for start in range(0, len(held), 1024):
            chunk = held[start : start + 1024]
            matrix = np.frombuffer(b"".join(blob for _, blob in chunk), dtype=np.float32)
            packed = _sketches(np, matrix.reshape(len(chunk), width), width)
            for (key, _), bits in zip(chunk, packed):
                sketched[key] = bits.tobytes()
    return index


def note_key_write(store: VectorMemoryStore, key: str) -> None:
    """Keep the index current after a write to *key*. Called with ``_db_lock`` held."""
    if not key.startswith("lesson."):
        return
    state = _state(store)
    if state.dirty is not None:
        state.dirty.add(key)
    index = state.index
    if index is None:
        return
    try:
        if store._sqlite_data_version() != index.data_version:
            state.index = None
            return
        index.refresh(store, key)
        if store.db.in_transaction:
            index.pending.add(key)
    except Exception:  # defensive: a failed patch must only cost a rebuild
        logger.debug("lesson candidate patch failed for %r; dropping the index", key, exc_info=True)
        state.index = None


def drop(store: VectorMemoryStore) -> None:
    """Forget the index after a change the caller cannot name a key for."""
    state = _state(store)
    # Under the lock the installer publishes under, so a drop cannot land between its
    # last check and its publication.
    with store._db_lock:
        if state.dirty is not None:
            state.poisoned = True
        state.index = None


def _fresh_rows(
    store: VectorMemoryStore, keys: Sequence[str], repo_scope: str | None
) -> list[dict]:
    """The nominated rows as the table holds them now, in ``get_lessons`` order."""
    rows: list[dict] = []
    for start in range(0, len(keys), _KEY_CHUNK):
        chunk = keys[start : start + _KEY_CHUNK]
        marks = ",".join("?" * len(chunk))
        rows.extend(
            dict(row)
            for row in store._fetch_all_locked(
                "SELECT * FROM semantic_memory "
                f"WHERE is_deleted = 0 AND key LIKE 'lesson.%' AND key IN ({marks})",
                tuple(chunk),
            )
        )
    scoped = _lessons.lesson_rows_in_scope(rows, repo_scope)
    # ORDER BY updated_at DESC, key: ``reverse`` keeps equal stamps in key order.
    scoped.sort(key=lambda row: row["key"])
    scoped.sort(key=lambda row: row["updated_at"], reverse=True)
    return scoped


def candidate_lesson_rows(
    store: VectorMemoryStore,
    *,
    rule: str,
    key: str,
    repo_scope: str | None,
    rule_emb: Sequence[float] | None,
    rule_words: set[str],
    source: str,
    confidence: float,
) -> list[dict] | None:
    """The in-scope lesson rows a write of *rule* must compare against, or ``None``.

    ``None`` means "no index here" (no numpy, no ``data_version``, or a build that kept
    moving under concurrent writes): the caller reads every row instead.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    rule_norm = rule.strip().lower()
    if not vm._HAS_NUMPY or not rule_norm:
        return None
    try:
        nominated = _nominate(
            store, rule, rule_norm, repo_scope, rule_emb, rule_words, source, confidence
        )
        if nominated is None:
            return None
        # The write's own key is always compared, whatever the index knows of it.
        nominated.add(key)
        return _fresh_rows(store, sorted(nominated), repo_scope)
    except Exception:  # defensive: an index failure must cost the scan, never the write
        logger.warning("lesson candidate index failed; scanning every lesson", exc_info=True)
        _state(store).index = None
        return None


def _nominate(
    store: VectorMemoryStore,
    rule: str,
    rule_norm: str,
    repo_scope: str | None,
    rule_emb: Sequence[float] | None,
    rule_words: set[str],
    source: str,
    confidence: float,
) -> set[str] | None:
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    for _attempt in range(2):
        with store._db_lock:
            version = store._sqlite_data_version()
            if version is None:
                return None
            state = _state(store)
            index = state.index
            if index is None and not state.seen_write:
                state.seen_write = True
                return None
            if index is not None and index.data_version == version:
                index.reconcile(store)
                return index.query(
                    vm.np,
                    rule_norm=rule_norm,
                    rule_lower=rule.lower(),
                    rule_words=rule_words,
                    scope=repo_scope,
                    vector=rule_emb,
                    source=source,
                    confidence=confidence,
                )
        _install(store)
    return None


def _install(store: VectorMemoryStore) -> None:
    """Build the index outside ``_db_lock`` and install it if nothing moved underneath."""
    state = _state(store)
    # A caller already inside ``_db_lock`` must not wait on a build another thread runs
    # (that thread takes ``_db_lock`` last): it skips the build and its write scans.
    held = getattr(store._db_lock, "held_by_current_thread", None)
    if not _BUILD_LOCK.acquire(blocking=not (held is not None and held())):
        return
    try:
        with store._db_lock:
            version = store._sqlite_data_version()
            index = state.index
            if version is None or (index is not None and index.data_version == version):
                return
            state.dirty = set()
            state.poisoned = False
        built: LessonCandidateIndex | None = None
        try:
            built = _build(store, version)
        finally:
            with store._db_lock:
                dirty = state.dirty or set()
                poisoned = state.poisoned
                state.dirty = None
                state.poisoned = False
                if built is not None and not poisoned and store._sqlite_data_version() == version:
                    for key in dirty:
                        built.refresh(store, key)
                    if store.db.in_transaction:
                        built.pending.update(dirty)
                    state.index = built
    finally:
        _BUILD_LOCK.release()
