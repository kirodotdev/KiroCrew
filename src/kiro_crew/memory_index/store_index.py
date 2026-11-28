"""One memory store's index: built in the background, kept fresh from the change log.

This is the part every backend shares, and where the guarantees live:

- **Fresh.** Before a search, the changes the store's change log holds past the
  backend's ``applied_seq`` are read back from the store and applied, so a
  write from any process is searchable at the next query. Past
  :data:`CATCH_UP_MAX` changes the index is rebuilt instead, and the caller
  scans until it is.
- **Never a deleted memory.** A search answers candidate *identities* only. The
  caller reads those rows from the store with its own ``is_deleted = 0`` guard.
- **Build, then swap.** A rebuild reads one snapshot of the store on a
  background thread and swaps in only when it finished; until then the caller
  scans as it does without an index.
- **One embedding space.** A build records the store's embedding signature; a
  store whose signature changed rebuilds.

``memory.index.mode`` decides whether any of it runs: ``off`` (default) never
builds and keeps no change log, ``shadow`` builds and compares the index's
candidates with the scan's answer without changing it, ``on`` answers from the
index whenever it is ready.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from kiro_crew import memory_v2
from kiro_crew.memory_index import changelog
from kiro_crew.memory_index.build_child import CHILD_TIMEOUT_SECS
from kiro_crew.memory_index.contract import (
    EmbeddingSpace,
    IndexDoc,
    IndexQuery,
    MemoryIndex,
    StoreRef,
)
from kiro_crew.memory_index.registry import create_index_backend

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

INDEX_MODES: Final = ("off", "shadow", "on")
#: Changes applied in line before a search; more than this rebuilds instead.
CATCH_UP_MAX: Final = 2000
#: Applied log rows a catch-up lets pile up before it trims them, so one write
#: transaction covers many recalls instead of one each.
TRIM_EVERY: Final = 256
#: Vector candidates asked of the backend first; deepened (x4 each time, up to
#: :data:`VECTOR_K_MAX`) until the ranking cannot reach past the last one.
VECTOR_K: Final = 256
VECTOR_K_MAX: Final = 16384
#: Term candidates per search; a search that fills it hands back to the scan.
TERMS_K: Final = 4096
#: How far below the admission floor the vector side still reads: the index's
#: i8 scalars move a cosine by up to about this much, and recall re-scores every
#: candidate from the store's float32 vector.
FLOOR_MARGIN: Final = 0.02
_EMBED_SIG_KEY: Final = "embedding_space_sig"
_INDEX_DIR_SUFFIX: Final = ".index"

# One background build at a time per process, across stores: a build is CPU-heavy
# and must not take several cores from live turns.
_build_slot = threading.BoundedSemaphore(1)


@dataclass(frozen=True)
class IndexSettings:
    mode: str = "off"
    backend: str = "local"
    exact_rows: int = 20_000


def settings_from(memory_cfg: object) -> IndexSettings:
    """``memory.index`` as the store reads it; anything unreadable is ``off``."""
    section = getattr(memory_cfg, "index", None)
    mode = getattr(section, "mode", "off")
    backend = getattr(section, "backend", "local")
    exact_rows = getattr(section, "exact_rows", 20_000)
    return IndexSettings(
        mode=mode if mode in INDEX_MODES else "off",
        backend=backend if isinstance(backend, str) and backend else "local",
        exact_rows=(
            exact_rows
            if isinstance(exact_rows, int) and not isinstance(exact_rows, bool)
            else 20_000
        ),
    )


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _unit(blob: bytes | None, dim: int) -> bytes | None:
    if not blob or len(blob) != dim * 4:
        return None
    values = struct.unpack(f"{dim}f", blob)
    norm = math.sqrt(sum(v * v for v in values))
    if not norm or not math.isfinite(norm):
        return None
    return struct.pack(f"{dim}f", *(v / norm for v in values))


def fact_search_text(key: str, value_json: str, identity: dict | None) -> str:
    """The text V2 fact admission reads for a row: its label and visible value."""
    details = ", ".join(
        str(identity[field])
        for field in ("subject", "predicate", "scope")
        if identity and identity.get(field)
    )
    label = f"{key} ({details})" if details else key
    return f"{label.replace('.', ' ').replace('_', ' ')} {memory_v2.visible_json(value_json)}"


def semantic_kind(key: str) -> str:
    if key.startswith("lesson."):
        return "directive"
    return "pref" if key.startswith("pref.") else "fact"


def _semantic_doc(row: Any, identity: dict | None, dim: int) -> IndexDoc:
    text = fact_search_text(row["key"], row["value_json"], identity)
    kind = semantic_kind(row["key"])
    return IndexDoc(
        ("semantic", row["key"]),
        kind,
        _unit(row["embedding"], dim),
        _normalized(text),
        " ".join(sorted(memory_v2.terms(text))),
        {"kind": kind, "rel": "semantic"},
    )


def _episode_doc(row: Any, dim: int) -> IndexDoc:
    text = str(row["text"])
    return IndexDoc(
        ("episodic", row["id"]),
        "episode",
        _unit(row["embedding"], dim),
        _normalized(text),
        " ".join(sorted(memory_v2.terms(text))),
        {"kind": "episode", "rel": "episodic"},
    )


def _identities(db: Any, keys: list[str] | None) -> dict[str, dict]:
    sql = (
        "SELECT record_id, subject, predicate, scope FROM memory_record_meta "
        "WHERE kind IN ('fact','directive') AND (subject != '' OR predicate != '')"
    )
    params: list[str] = []
    if keys is not None:
        if not keys:
            return {}
        sql += f" AND record_id IN ({', '.join('?' for _ in keys)})"
        params = [f"key:{key}" for key in keys]
    try:
        rows = db.execute(sql, params).fetchall()
    except Exception:  # a file with no record metadata yet carries no identities
        return {}
    return {
        row[0].removeprefix("key:"): {"subject": row[1], "predicate": row[2], "scope": row[3]}
        for row in rows
    }


class StoreIndex:
    """The index of one ``VectorMemoryStore``; created by :func:`index_for`."""

    def __init__(self, store: VectorMemoryStore, settings: IndexSettings) -> None:
        self._store = store
        self.settings = settings
        # The store's own lock, not a second one: recall already holds it when it
        # asks for candidates, and two locks taken in either order would deadlock.
        self._lock = store._db_lock
        self._backend: MemoryIndex | None = None
        self._ready = False
        self._building = False
        self._failed = ""
        self._open_failed = False
        self._owner_fd: int | None = None
        self._log_reinstalled = False
        self._closed = False
        self._trimmed_seq = 0
        self._space = self._current_space()
        #: Build in a spawned child (its own interpreter lock) when the backend can
        #: adopt a generation written elsewhere; otherwise on this thread.
        self.in_child = True
        #: Bound the wait for a child independently of the caller's deadline.
        self.child_timeout = CHILD_TIMEOUT_SECS
        #: ``shadow`` comparisons: kind -> [rows checked, rows the index missed].
        self.shadow: dict[str, list[int]] = {}
        #: Searches handed back to the scan because a limit was reached, per kind.
        self.fallbacks: dict[str, int] = {}

    def record_shadow(self, kind: str, checked: int, missed: int) -> None:
        with self._lock:
            totals = self.shadow.setdefault(kind, [0, 0])
            totals[0] += checked
            totals[1] += missed

    # -- setup ------------------------------------------------------------

    def _current_space(self) -> EmbeddingSpace:
        from kiro_crew import embeddings_shared

        store = self._store
        recorded = store._read_meta(_EMBED_SIG_KEY) if hasattr(store, "_read_meta") else None
        signature = recorded or embeddings_shared.default_embedding_space_signature()
        with store._db_lock:
            log_id = changelog.log_id(store.db) if store.db is not None else ""
        # The log's install id is part of what a build covers: a log another
        # process stripped and reinstalled restarts its counter, and a build made
        # against the old log must not read as current against the new one.
        return EmbeddingSpace(
            signature=f"{signature}:{store._embedding_dim}:{log_id}", dim=store._embedding_dim
        )

    def _index_dir(self) -> str | None:
        path = getattr(self._store, "_db_path", None)
        return str(path) + _INDEX_DIR_SUFFIX if path else None

    def _ensure_log(self) -> bool:
        store = self._store
        with store._db_lock:
            if changelog.is_installed(store.db, store._lineage):
                return True
            if store.db.in_transaction:
                return False  # never install inside, or roll back, someone else's write
            try:
                store.db.execute("BEGIN IMMEDIATE")
            except Exception:
                logger.warning(
                    "memory index: change log install skipped (store busy)", exc_info=True
                )
                return False
            try:
                changelog.install(store.db, store._lineage)
                store.db.execute("COMMIT")
            except Exception:
                store.db.execute("ROLLBACK")
                logger.warning("memory index: could not install the change log", exc_info=True)
                return False
            # Writes made while no log existed reached no index: whatever build is
            # on disk is not to be trusted, however its seqs compare. The new log's
            # id is part of the space, so builds from here on carry it.
            self._ready = False
            self._log_reinstalled = True
            self._space = self._current_space()
            self._drop_backend()
        return True

    def _drop_backend(self) -> None:
        """Forget the open backend and close it, best effort: a derived index that
        fails to close is rebuilt, never allowed to fail the store."""
        backend, self._backend = self._backend, None
        if backend is not None:
            try:
                backend.close()
            except Exception:
                logger.warning("memory index: backend close failed", exc_info=True)

    def _own_index_dir(self) -> bool:
        """Take the index directory's owner lock, once. One process keeps a store's
        index on disk; any other process opening the same store scans instead."""
        directory = self._index_dir()
        if directory is None or self._owner_fd is not None:
            return True
        import os

        from kiro_crew.owner_only_files import mkdirs_owner_only, owner_only_opener
        from kiro_crew.platform_lock_compat import try_acquire_lock

        try:
            mkdirs_owner_only(directory)
            fd = owner_only_opener(os.path.join(directory, "owner.lock"), os.O_RDWR | os.O_CREAT)
        except OSError:
            logger.warning("memory index: cannot open %s", directory, exc_info=True)
            return False
        if not try_acquire_lock(fd, exclusive=True):
            os.close(fd)
            self._failed = "another process owns this store's index"
            return False
        self._owner_fd = fd
        return True

    def _open_backend(self) -> MemoryIndex | None:
        if self._backend is not None:
            return self._backend
        if self._open_failed:
            return None
        if not self._own_index_dir():
            return None
        try:
            backend = create_index_backend(self.settings.backend)
            backend.open(
                StoreRef(
                    store_id=str(getattr(self._store, "_db_path", "")), index_dir=self._index_dir()
                ),
                self._space,
                {"exact_rows": self.settings.exact_rows},
            )
        except Exception as exc:
            self._failed = f"{type(exc).__name__}: {exc}"
            self._open_failed = True
            logger.warning(
                "memory index: backend %r did not open", self.settings.backend, exc_info=True
            )
            # This process owns the index, so nothing else reads the log: drop it
            # rather than let every write grow it with no index to trim it.
            with self._store._db_lock:
                changelog.strip_now(self._store.db)
            return None
        self._backend = backend
        status = backend.status()
        self._ready = (
            status.state == "ready"
            and status.space_signature == self._space.signature
            and not self._log_reinstalled
        )
        return backend

    # -- freshness --------------------------------------------------------

    def _docs_for(
        self, changes: list[tuple[int, str, str]]
    ) -> tuple[list[IndexDoc], list[tuple[str, str]]]:
        store = self._store
        dim = store._embedding_dim
        keys = sorted({ident for _seq, rel, ident in changes if rel == "semantic"})
        ids = sorted({ident for _seq, rel, ident in changes if rel == "episodic"})
        live: list[IndexDoc] = []
        seen: set[tuple[str, str]] = set()
        with store._db_lock:
            identities = _identities(store.db, keys)
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                marks = ", ".join("?" for _ in chunk)
                for row in store.db.execute(
                    f"SELECT key, value_json, embedding FROM semantic_memory WHERE is_deleted = 0 AND key IN ({marks})",
                    chunk,
                ):
                    live.append(_semantic_doc(row, identities.get(row["key"]), dim))
                    seen.add(("semantic", row["key"]))
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ", ".join("?" for _ in chunk)
                for row in store.db.execute(
                    f"SELECT id, text, embedding FROM episodic_memories WHERE is_deleted = 0 AND id IN ({marks})",
                    chunk,
                ):
                    live.append(_episode_doc(row, dim))
                    seen.add(("episodic", row["id"]))
        gone = [("semantic", key) for key in keys if ("semantic", key) not in seen]
        gone += [("episodic", i) for i in ids if ("episodic", i) not in seen]
        return live, gone

    def _catch_up(self, backend: MemoryIndex) -> bool:
        """Apply logged changes the backend has not seen. False: too many, rebuild."""
        store = self._store
        applied = backend.status().applied_seq
        with store._db_lock:
            current = changelog.current_seq(store.db)
            if current < applied:
                return False  # the log was recreated under this index: rebuild
            if current == applied:
                return True
            oldest = changelog.oldest_seq(store.db)
            if oldest and oldest > applied + 1:
                return False  # trimmed past what this index saw
            changes = changelog.changes_after(store.db, applied, CATCH_UP_MAX + 1)
        if len(changes) > CATCH_UP_MAX:
            return False
        live, gone = self._docs_for(changes)
        last = changes[-1][0] if changes else current
        # Only the last write advances the seq: when an earlier one lands and a
        # later one fails, the saved seq still says those changes are pending,
        # so the next catch-up applies them all again (both writes are idempotent).
        backend.remove(gone, seq=applied if live else last)
        if live:
            backend.upsert(live, seq=last)
        # Rows the backend has applied are never read again: a reopened index that
        # lags them rebuilds from the store, not from the log.
        if last - self._trimmed_seq >= TRIM_EVERY:
            self._trim(last)
        return True

    def _rebuilt(self, backend: MemoryIndex) -> Any:
        """A finished build, from a child process when the backend can adopt one."""
        from kiro_crew.memory_index.local import LocalIndex

        store = self._store
        if isinstance(backend, LocalIndex) and backend.index_root is not None and self.in_child:
            from kiro_crew.memory_index.build_child import run_build_in_child

            root = backend.index_root
            built = run_build_in_child(
                str(store._db_path),
                str(root),
                self._space.signature,
                self._space.dim,
                timeout=self.child_timeout,
            )
            if built is not None:
                return backend.adopt(built[0], build_seq=built[1])
        docs, seq = snapshot_docs(str(store._db_path), store._embedding_dim)
        return backend.rebuild(iter(docs), build_seq=seq)

    def _build(self) -> None:
        try:
            with _build_slot:
                with self._lock:
                    # A store closed while this build waited for the slot keeps no
                    # index: reopening here would take an owner lock nobody frees.
                    backend = None if self._closed else self._open_backend()
                if backend is None:
                    return
                handle = self._rebuilt(backend)
                seq = handle.build_seq  # type: ignore[attr-defined]
                with self._lock:
                    if self._backend is not backend or self._closed:
                        _discard(backend, handle)  # closed or replaced while it ran
                        return
                    backend.swap(handle)
                    self._catch_up(backend)
                    self._ready = True
                    self._log_reinstalled = False
                    self._failed = ""
                self._trim(seq)
        except Exception as exc:
            self._failed = f"{type(exc).__name__}: {exc}"
            logger.warning("memory index: build failed", exc_info=True)
        finally:
            with self._lock:
                self._building = False

    def _trim(self, upto: int) -> None:
        store = self._store
        with store._db_lock:
            if store.db.in_transaction:
                return  # a caller's open transaction is not this trim's to end
            try:
                store.db.execute("BEGIN IMMEDIATE")
                changelog.trim(store.db, upto)
                store.db.execute("COMMIT")
                self._trimmed_seq = max(self._trimmed_seq, upto)
            except Exception:
                if store.db.in_transaction:
                    store.db.execute("ROLLBACK")
                logger.debug("memory index: log trim skipped", exc_info=True)

    def _schedule_build(self) -> None:
        with self._lock:
            if self._building:
                return
            self._building = True
            self._ready = False
        threading.Thread(target=self._build, name="memory-index-build", daemon=True).start()

    # -- searching --------------------------------------------------------

    def _usable(self) -> MemoryIndex | None:
        """The backend when it can answer now; otherwise schedule what it needs."""
        if self.settings.mode == "off":
            return None
        with self._lock:
            if self._closed:
                return None
            if self._space != self._current_space():
                self._space = self._current_space()
                self._drop_backend()
                self._ready = False
            if self._building:
                return None
            if self._open_failed:
                return None  # no log either: an index that cannot open must not grow one
            try:
                # Only the owner reads and trims the log, so only the owner installs
                # it: a second process on the store scans and leaves the file alone.
                if not self._own_index_dir():
                    return None
                if not self._ensure_log():
                    return None
                backend = self._open_backend()
                if backend is None:
                    return None
                caught_up = self._ready and self._catch_up(backend)
            except Exception:
                # A full disk or a damaged index must not fail recall: scan, and
                # rebuild on the next search.
                logger.warning("memory index: catch-up failed; scanning", exc_info=True)
                self._ready = False
                return None
            if not caught_up:
                self._schedule_build()
                return None
            return backend

    def candidates(
        self,
        kind: str,
        query_text: str,
        query_vector: list[float] | None,
        *,
        min_match: int,
        floor: float,
    ) -> set[str] | None:
        """Every identity of *kind* (``semantic`` or ``episodic``) the caller could
        admit, or ``None`` when the index cannot say and the caller must scan.

        That is every row holding *min_match* query terms (exact), and every row
        whose cosine reaches *floor*: the vector search deepens until its last
        hit is under it. Below ``memory.index.exact_rows`` vectors that search
        compares every vector, so the set is exact; above it the backend searches
        an approximate graph and can miss a row near the floor, which ``shadow``
        counts before ``on`` is used. Either side running into its limit answers
        ``None``: a cut-off candidate set would silently change what recall returns.
        """
        backend = self._usable()
        if backend is None:
            return None
        rel = "semantic" if kind == "semantic" else "episodic"
        filters = {"rel": rel}
        found: set[str] = set()
        try:
            with self._lock:
                if query_vector is not None and len(query_vector) == self._space.dim:
                    blob = _unit(
                        struct.pack(f"{len(query_vector)}f", *query_vector), self._space.dim
                    )
                    if blob is not None:
                        nearest = _vector_candidates(backend, blob, filters, floor)
                        if nearest is None:
                            self.record_fallback(kind)
                            return None
                        found.update(nearest)
                terms = sorted(memory_v2.terms(query_text))
                if terms:
                    query = IndexQuery(
                        "terms",
                        TERMS_K,
                        text=" ".join(terms),
                        filters=filters,
                        min_match=min(min_match, len(terms)),
                    )
                    hits = backend.search(query)
                    if len(hits) >= TERMS_K:
                        self.record_fallback(kind)
                        return None
                    found.update(hit.ident[1] for hit in hits)
        except Exception:
            logger.warning("memory index: search failed; scanning", exc_info=True)
            return None
        return found

    def record_fallback(self, kind: str) -> None:
        """Count a search the index handed back to the scan."""
        self.fallbacks[kind] = self.fallbacks.get(kind, 0) + 1

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._ready = False
            try:
                self._drop_backend()
            finally:
                # Released even when the backend fails to close, so the next
                # handle on this store can own the index.
                if self._owner_fd is not None:
                    import os

                    from kiro_crew.platform_lock_compat import release_lock

                    release_lock(self._owner_fd)
                    os.close(self._owner_fd)
                    self._owner_fd = None


def _discard(backend: MemoryIndex, handle: Any) -> None:
    """Drop a finished build that will never be swapped in."""
    from kiro_crew.memory_index.local import LocalIndex

    if isinstance(backend, LocalIndex):
        backend.discard(handle)


def snapshot_docs(db_path: str, dim: int) -> tuple[list[IndexDoc], int]:
    """Every live memory of the store at *db_path* as a document, and the log seq
    the snapshot covers.

    Read through its own read-only connection in one read transaction, so a
    build neither holds the store's lock nor blocks its writers, and the seq and
    the rows describe the same instant.
    """
    from pathlib import Path

    from kiro_crew._sqlite_compat import resolve_sqlite3
    from kiro_crew.sqlite_uri import read_only_uri

    sqlite3 = resolve_sqlite3()
    conn = sqlite3.connect(read_only_uri(Path(db_path)), uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    docs: list[IndexDoc] = []
    try:
        conn.execute("BEGIN")
        seq = changelog.current_seq(conn)
        identities = _identities(conn, None)
        for row in conn.execute(
            "SELECT key, value_json, embedding FROM semantic_memory WHERE is_deleted = 0"
        ):
            docs.append(_semantic_doc(row, identities.get(row["key"]), dim))
        for row in conn.execute(
            "SELECT id, text, embedding FROM episodic_memories WHERE is_deleted = 0"
        ):
            docs.append(_episode_doc(row, dim))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return docs, seq


def _vector_candidates(
    backend: MemoryIndex, blob: bytes, filters: dict, floor: float
) -> list[str] | None:
    """Every vector at or above *floor* (with a margin for the index's coarser
    scalars), or ``None`` past :data:`VECTOR_K_MAX` hits."""
    k = VECTOR_K
    while True:
        hits = backend.search(IndexQuery("vector", k, vector=blob, filters=filters))
        if len(hits) < k or hits[-1].score < floor - FLOOR_MARGIN:
            return [hit.ident[1] for hit in hits]
        if k >= VECTOR_K_MAX:
            return None
        k *= 4


def index_log_unwanted(store: VectorMemoryStore) -> bool:
    """A store built from config with ``memory.index.mode: off`` that still holds
    the change log: nothing trims it any more, so ``init()`` strips it. A store
    opened with no config must not strip the log another process relies on."""
    settings = getattr(store, "_index_settings", None)
    return settings is not None and settings.mode == "off" and changelog.is_present(store.db)


def index_for(store: VectorMemoryStore) -> StoreIndex | None:
    """The store's index when ``memory.index.mode`` is not ``off``."""
    with store._db_lock:
        settings: IndexSettings = getattr(store, "_index_settings", IndexSettings())
        current: StoreIndex | None = getattr(store, "_memory_index", None)
        if settings.mode == "off":
            if current is not None:
                current.close()
                store._memory_index = None
                # Switched off while running: nothing trims the log from here on.
                # An index turned back on reinstalls it and rebuilds.
                changelog.strip_now(store.db)
            return None
        if (
            current is None
            or current.settings.backend != settings.backend
            or current.settings.exact_rows != settings.exact_rows
        ):
            if current is not None:
                current.close()
            current = StoreIndex(store, settings)
            store._memory_index = current
        current.settings = settings
        return current
