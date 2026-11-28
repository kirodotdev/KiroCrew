"""The ``local`` backend: usearch for vectors, SQLite FTS5 for words.

Everything lives in one generation directory under ``StoreRef.index_dir``:

- ``words.db``: a ``docs`` table (one row per document: ident, kind, the term
  string, the filters as JSON) and one FTS5 index over the ``memory_v2.terms``
  string (``words_terms``, ``detail=none``, ``columnsize=0``) whose
  ``fts5vocab`` table gives each term's document count. The memory text itself
  is not stored: recall reads rows from the store.
- ``vectors.usearch``: an HNSW index (cosine, :data:`VECTOR_DTYPE` scalars) keyed
  by ``docs.n``. Recall re-scores every candidate from the store's own float32
  vector, so the index's coarser scalars only choose candidates.

Substring search (``trigram``) is not served here yet: no caller of this backend
asks for it, and it would roughly double ``words.db``.

``current`` names the live generation. A rebuild writes a new generation beside
it and ``swap`` repoints ``current`` with one atomic rename, so a reader opens
either the old generation or the new one, never a mix. The vector file is saved
on ``swap`` and ``close``; a generation whose saved vectors lag its words is
reported ``stale``, and the caller rebuilds.

With no ``index_dir`` everything stays in memory (tests, conformance).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from kiro_crew._sqlite_compat import resolve_sqlite3
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.memory_index.contract import (
    CAP_FILTERS,
    CAP_TERMS,
    CAP_VECTOR,
    BuildHandle,
    EmbeddingSpace,
    Hit,
    Ident,
    IndexDoc,
    IndexQuery,
    IndexStatus,
    MemoryIndexError,
    StoreRef,
)

logger = logging.getLogger(__name__)
sqlite3 = resolve_sqlite3()

FORMAT_VERSION = 2
#: Below this many vectors a vector search is exact; ``config["exact_rows"]`` overrides it.
DEFAULT_EXACT_ROWS = 20_000
#: Scalar type of the stored vectors, and the HNSW graph's connectivity (M).
VECTOR_DTYPE = "i8"
CONNECTIVITY = 16

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS docs (n INTEGER PRIMARY KEY, rel TEXT NOT NULL, ident TEXT NOT NULL, "
    "kind TEXT NOT NULL, terms TEXT NOT NULL, filters TEXT NOT NULL, "
    "has_vector INTEGER NOT NULL, UNIQUE (rel, ident))",
    "CREATE VIRTUAL TABLE IF NOT EXISTS words_terms USING fts5(terms, content='docs', "
    "content_rowid='n', detail='none', columnsize=0, tokenize='ascii')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS words_vocab USING fts5vocab(words_terms, 'row')",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def _usearch() -> Any:
    try:
        from usearch.index import Index  # deferred: a native module, imported on first use
    except ImportError as exc:
        from kiro_crew.extras import install_hint

        raise MemoryIndexError(
            "The local memory index needs the memory-index extra. "
            f"Install its dependency with: {install_hint('memory-index')}"
        ) from exc
    return Index


def _quote(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


class _Generation:
    """One open generation: the words database and the vector index."""

    def __init__(self, path: Path | None, space: EmbeddingSpace) -> None:
        index_cls = _usearch()
        self.path = path
        self.space = space
        if path is not None:
            from kiro_crew.owner_only_files import prepare_owner_only_sqlite

            # The term strings are derived from memories: owner-only, like the store.
            prepare_owner_only_sqlite(path / "words.db")
        self.db = sqlite3.connect(
            str(path / "words.db") if path else ":memory:", check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        for statement in _SCHEMA:
            self.db.execute(statement)
        self.db.commit()
        vectors = path / "vectors.usearch" if path else None
        #: A built generation whose vector file is gone: its words alone would
        #: read ready and drop every memory recall admits by cosine only.
        self.vectors_missing = bool(
            vectors is not None and not vectors.exists() and self.meta("space_signature")
        )
        if vectors is not None and vectors.exists():
            self.vectors = index_cls.restore(str(vectors))
        else:
            self.vectors = index_cls(
                ndim=space.dim, metric="cos", dtype=VECTOR_DTYPE, connectivity=CONNECTIVITY
            )

    def meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_meta(self, **values: object) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in values.items()],
        )

    def save_vectors(self) -> None:
        if self.path is not None:
            partial = self.path / "vectors.usearch.partial"
            self.vectors.save(str(partial))
            from kiro_crew.owner_only_files import restrict_file_to_owner_soft

            restrict_file_to_owner_soft(partial)
            replace_with_retry(partial, self.path / "vectors.usearch")
        self.set_meta(vectors_seq=self.meta("applied_seq", "0"))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


def _check_doc(doc: IndexDoc, space: EmbeddingSpace) -> None:
    if not isinstance(doc, IndexDoc):
        raise MemoryIndexError("upsert takes IndexDoc values")
    if doc.vector is not None:
        if len(doc.vector) != space.dim * 4:
            raise MemoryIndexError(f"{doc.ident!r} has a vector of the wrong dimension")
        if not np.isfinite(np.frombuffer(doc.vector, dtype=np.float32)).all():
            raise MemoryIndexError(f"{doc.ident!r} has a non-finite vector")


def _drop(gen: _Generation, ident: Ident) -> None:
    row = gen.db.execute(
        "SELECT n, terms, has_vector FROM docs WHERE rel = ? AND ident = ?", ident
    ).fetchone()
    if row is None:
        return
    gen.db.execute(
        "INSERT INTO words_terms (words_terms, rowid, terms) VALUES ('delete', ?, ?)",
        (row["n"], row["terms"]),
    )
    gen.db.execute("DELETE FROM docs WHERE n = ?", (row["n"],))
    if row["has_vector"] and row["n"] in gen.vectors:
        gen.vectors.remove(row["n"])


def _put(gen: _Generation, doc: IndexDoc) -> None:
    _drop(gen, doc.ident)
    cursor = gen.db.execute(
        "INSERT INTO docs (rel, ident, kind, terms, filters, has_vector) VALUES (?, ?, ?, ?, ?, ?)",
        (
            doc.ident[0],
            doc.ident[1],
            doc.kind,
            doc.terms,
            json.dumps(dict(doc.filters), sort_keys=True),
            int(doc.vector is not None),
        ),
    )
    n = int(cursor.lastrowid or 0)
    gen.db.execute("INSERT INTO words_terms (rowid, terms) VALUES (?, ?)", (n, doc.terms))
    if doc.vector is not None:
        gen.vectors.add(n, np.frombuffer(doc.vector, dtype=np.float32))


def _build_generation(
    root: Path | None, space: EmbeddingSpace, source: Iterable[IndexDoc], build_seq: int
) -> tuple[_Generation, str | None]:
    """Write a complete generation (under *root*, or in memory) and return it open."""
    name = f"gen-{uuid.uuid4().hex}" if root is not None else None
    path = None
    if root is not None and name is not None:
        path = root / name
        path.mkdir(mode=0o700)
    gen = _Generation(path, space)
    try:
        for count, doc in enumerate(source, 1):
            _check_doc(doc, space)
            _put(gen, doc)
            if count % 512 == 0:
                gen.db.commit()
        gen.db.execute("INSERT INTO words_terms (words_terms) VALUES ('optimize')")
        gen.set_meta(
            built_seq=build_seq,
            applied_seq=build_seq,
            space_signature=space.signature,
            format_version=FORMAT_VERSION,
        )
        gen.save_vectors()
    except BaseException:
        gen.close()
        if path is not None:
            _remove_generation(root, path)
        raise
    return gen, name


_GENERATION_NAME = re.compile(r"gen-[0-9a-f]{32}")


def _is_generation_name(name: str) -> bool:
    """A name this module wrote (``gen-`` and a uuid4 hex), never a path."""
    return _GENERATION_NAME.fullmatch(name) is not None


def _remove_generation(root: Path | None, path: Path) -> None:
    """Delete one generation directory, and only one: a real ``gen-<hex>``
    directory directly under *root*, never through a link."""
    if (
        root is None
        or path.parent != root
        or not _is_generation_name(path.name)
        or path.is_symlink()
    ):
        logger.warning("memory index: refusing to delete %s (not a generation of %s)", path, root)
        return
    shutil.rmtree(path, ignore_errors=True)


def build_generation(
    root: str, space: EmbeddingSpace, source: Iterable[IndexDoc], build_seq: int
) -> str:
    """Write a generation under *root* and close it; the owner adopts it by name.

    For a build in another process: it never touches ``current`` or the live
    generation, so only the process holding the index swaps.
    """
    gen, name = _build_generation(Path(root), space, source, build_seq)
    gen.close()
    assert name is not None
    return name


class _LocalBuild(BuildHandle):
    def __init__(self, gen: _Generation, name: str | None, build_seq: int) -> None:
        self.gen = gen
        self.name = name
        self.build_seq = build_seq


class LocalIndex:
    name: ClassVar[str] = "local"
    capabilities: ClassVar[frozenset[str]] = frozenset({CAP_VECTOR, CAP_TERMS, CAP_FILTERS})

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._gen: _Generation | None = None
        self._root: Path | None = None
        self._space: EmbeddingSpace | None = None
        self._exact_rows = DEFAULT_EXACT_ROWS
        # Set at open when the saved vectors lag the saved words (a crash between
        # saves); cleared by the next swap.
        self._opened_stale = False

    # -- lifecycle --------------------------------------------------------

    def open(self, store: StoreRef, space: EmbeddingSpace, config: Mapping[str, object]) -> None:
        exact_rows = config.get("exact_rows", DEFAULT_EXACT_ROWS)
        if isinstance(exact_rows, int) and not isinstance(exact_rows, bool) and exact_rows >= 0:
            self._exact_rows = exact_rows
        with self._lock:
            self._space = space
            self._root = Path(store.index_dir) if store.index_dir else None
            if self._root is not None:
                from kiro_crew.owner_only_files import mkdirs_owner_only

                mkdirs_owner_only(self._root)
            self._gen = self._open_current()
            self._drop_other_generations()
            self._opened_stale = self._gen.vectors_missing or (
                self._gen.path is not None
                and self._gen.meta("vectors_seq", self._gen.meta("applied_seq", "0"))
                != self._gen.meta("applied_seq", "0")
            )

    def _open_current(self) -> _Generation:
        assert self._space is not None
        if self._root is None:
            return _Generation(None, self._space)
        pointer = self._root / "current"
        name = pointer.read_text(encoding="utf-8").strip() if pointer.exists() else ""
        if name and not _is_generation_name(name):
            # The pointer is a file in the data home that the gateway trusts; a
            # name that is not one of ours never becomes a path it writes or deletes.
            logger.warning("memory index pointer names no generation; starting empty")
            name = ""
        if not name:
            name = f"gen-{uuid.uuid4().hex}"
            (self._root / name).mkdir(mode=0o700)
            self._point_at(name)
        try:
            return _Generation(self._root / name, self._space)
        except (sqlite3.DatabaseError, RuntimeError, OSError, ValueError):
            logger.warning(
                "memory index generation %s is unreadable; starting empty", name, exc_info=True
            )
            fresh = f"gen-{uuid.uuid4().hex}"
            (self._root / fresh).mkdir(mode=0o700)
            self._point_at(fresh)
            return _Generation(self._root / fresh, self._space)

    def _drop_other_generations(self) -> None:
        """Delete generations ``current`` does not name: builds a crash or a closed
        store left behind. Only the index's owner opens it, so none is in use."""
        if self._root is None or self._gen is None or self._gen.path is None:
            return
        for entry in self._root.iterdir():
            if entry.is_dir() and _is_generation_name(entry.name) and entry != self._gen.path:
                _remove_generation(self._root, entry)

    def _point_at(self, name: str) -> None:
        assert self._root is not None
        partial = self._root / "current.partial"
        partial.write_text(name, encoding="utf-8")
        replace_with_retry(partial, self._root / "current")

    def _require(self) -> tuple[_Generation, EmbeddingSpace]:
        if self._gen is None or self._space is None:
            raise MemoryIndexError("open the index before using it")
        return self._gen, self._space

    def close(self) -> None:
        with self._lock:
            if self._gen is not None:
                try:
                    # Opened stale: the vectors in memory lag the saved words, so
                    # saving them would mark them current. Only a rebuild's swap
                    # clears the flag.
                    if not self._opened_stale:
                        self._gen.save_vectors()
                except (OSError, RuntimeError, sqlite3.Error):
                    # Best effort: unsaved vectors leave vectors_seq behind the
                    # words, so the next open reads stale and rebuilds.
                    logger.warning("memory index: saving vectors failed", exc_info=True)
                finally:
                    self._gen.close()
            self._gen = None

    # -- writes -----------------------------------------------------------

    def upsert(self, docs: Sequence[IndexDoc], *, seq: int) -> None:
        with self._lock:
            gen, space = self._require()
            for doc in docs:
                _check_doc(doc, space)
            for doc in docs:
                _put(gen, doc)
            gen.set_meta(applied_seq=max(int(gen.meta("applied_seq", "0")), seq))
            gen.db.commit()

    def remove(self, idents: Sequence[Ident], *, seq: int) -> None:
        with self._lock:
            gen, _space = self._require()
            for ident in idents:
                _drop(gen, ident)
            gen.set_meta(applied_seq=max(int(gen.meta("applied_seq", "0")), seq))
            gen.db.commit()

    def rebuild(self, source: Iterable[IndexDoc], *, build_seq: int) -> BuildHandle:
        with self._lock:
            _gen, space = self._require()
            root = self._root
        gen, name = _build_generation(root, space, source, build_seq)
        return _LocalBuild(gen, name, build_seq)

    @property
    def index_root(self) -> Path | None:
        """The directory generations live in, or ``None`` when kept in memory."""
        return self._root

    def discard(self, handle: BuildHandle) -> None:
        """Close a build that will not be swapped in and delete its generation."""
        if not isinstance(handle, _LocalBuild):
            return
        handle.gen.close()
        if handle.gen.path is not None:
            _remove_generation(self._root, handle.gen.path)

    def adopt(self, name: str, *, build_seq: int) -> BuildHandle:
        """A build another process wrote with :func:`build_generation`, ready to ``swap``."""
        with self._lock:
            _gen, space = self._require()
            root = self._root
        if root is None or not _is_generation_name(name):
            raise MemoryIndexError("adopt takes a generation name under this index")
        gen = _Generation(root / name, space)
        if (
            int(gen.meta("built_seq", "-1")) != build_seq
            or gen.meta("space_signature") != space.signature
            or gen.vectors_missing
        ):
            gen.close()
            raise MemoryIndexError("that generation is not the build it was said to be")
        return _LocalBuild(gen, name, build_seq)

    def swap(self, handle: BuildHandle) -> None:
        if not isinstance(handle, _LocalBuild):
            raise MemoryIndexError("swap takes a build this backend made")
        with self._lock:
            old = self._gen
            old_path = old.path if old else None
            if self._root is not None and handle.name is not None:
                self._point_at(handle.name)
            self._gen = handle.gen
            self._opened_stale = False
            if old is not None:
                old.close()
            if old_path is not None and old_path != handle.gen.path:
                _remove_generation(self._root, old_path)

    # -- reads ------------------------------------------------------------

    def status(self) -> IndexStatus:
        with self._lock:
            if self._gen is None:
                return IndexStatus(self.name, "missing", 0, 0, 0, "", FORMAT_VERSION, "not open")
            gen = self._gen
            built = int(gen.meta("built_seq", "0"))
            applied = int(gen.meta("applied_seq", "0"))
            signature = gen.meta("space_signature", "")
            rows = int(gen.db.execute("SELECT count(*) FROM docs").fetchone()[0])
            if self._opened_stale:
                state, detail = "stale", "the saved vectors lag the words; rebuild"
            elif signature and gen.meta("format_version", "0") != str(FORMAT_VERSION):
                state, detail = "stale", "built with another index format; rebuild"
            elif signature and self._space is not None and signature != self._space.signature:
                state, detail = "stale", "built for another embedding space"
            elif not signature:
                state, detail = "missing", "never built"
            else:
                state, detail = "ready", ""
            return IndexStatus(self.name, state, built, applied, rows, signature, FORMAT_VERSION, detail)  # type: ignore[arg-type]

    def search(self, query: IndexQuery) -> list[Hit]:
        with self._lock:
            gen, space = self._require()
            if query.mode == "vector":
                return self._vector(gen, space, query)
            if query.mode == "terms":
                return self._terms(gen, query)
            raise MemoryIndexError("the local backend does not serve substring search")

    def _filtered(
        self, gen: _Generation, ns: list[int], filters: Mapping[str, str]
    ) -> dict[int, Any]:
        if not ns:
            return {}
        rows: dict[int, Any] = {}
        for start in range(0, len(ns), 500):
            chunk = ns[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            for row in gen.db.execute(
                f"SELECT n, rel, ident, terms, filters FROM docs WHERE n IN ({marks})", chunk
            ):
                if filters:
                    held = json.loads(row["filters"])
                    if any(held.get(key) != value for key, value in filters.items()):
                        continue
                rows[int(row["n"])] = row
        return rows

    def _vector(self, gen: _Generation, space: EmbeddingSpace, query: IndexQuery) -> list[Hit]:
        assert query.vector is not None
        if len(query.vector) != space.dim * 4:
            raise MemoryIndexError("the query vector has the wrong dimension")
        total = len(gen.vectors)
        if total == 0:
            return []
        vector = np.frombuffer(query.vector, dtype=np.float32)
        exact = total < self._exact_rows
        want = query.k
        while True:
            count = min(total, want * 4 if query.filters else want)
            found = gen.vectors.search(vector, count, exact=exact)
            keys = [int(key) for key in found.keys]
            distances = [float(d) for d in found.distances]
            rows = self._filtered(gen, keys, query.filters)
            hits = [
                (1.0 - distance, (rows[key]["rel"], rows[key]["ident"]))
                for key, distance in zip(keys, distances)
                if key in rows
            ]
            if len(hits) >= query.k or count >= total:
                break
            want *= 4
        hits.sort(key=lambda hit: (-hit[0], hit[1]))
        return [Hit(ident, score) for score, ident in hits[: query.k]]

    def _document_counts(self, gen: _Generation, terms: list[str]) -> dict[str, int]:
        counts = {term: 0 for term in terms}
        for start in range(0, len(terms), 500):
            chunk = terms[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            for term, doc in gen.db.execute(
                f"SELECT term, doc FROM words_vocab WHERE term IN ({marks})", chunk
            ):
                counts[str(term)] = int(doc)
        return counts

    def _terms(self, gen: _Generation, query: IndexQuery) -> list[Hit]:
        assert query.text is not None
        wanted = sorted(set(query.text.split()))
        counts = self._document_counts(gen, wanted)
        # A document holding min_match of the |Q| terms holds at least one of the
        # |Q| - min_match + 1 rarest ones, so only those are looked up.
        probe = sorted(wanted, key=lambda term: (counts[term], term))[
            : len(wanted) - query.min_match + 1
        ]
        probe = [term for term in probe if counts[term]]
        if not probe:
            return []
        ns = self._match(gen, "words_terms", " OR ".join(_quote(term) for term in probe), probe)
        rows = self._filtered(gen, ns, query.filters)
        wanted_set = set(wanted)
        hits = []
        for row in rows.values():
            matched = len(wanted_set & set(str(row["terms"]).split()))
            if matched >= query.min_match:
                hits.append((float(matched), (row["rel"], row["ident"])))
        hits.sort(key=lambda hit: (-hit[0], hit[1]))
        return [Hit(ident, score) for score, ident in hits[: query.k]]

    @staticmethod
    def _match(gen: _Generation, table: str, expression: str, tokens: list[str]) -> list[int]:
        try:
            return [
                int(row[0])
                for row in gen.db.execute(
                    f"SELECT rowid FROM {table} WHERE {table} MATCH ?", (expression,)
                )
            ]
        except sqlite3.OperationalError:
            # A token the tokenizer splits in two reads as a phrase, which a
            # detail=none table refuses. shortcut: fall back to reading the term
            # strings; it scans the documents, so it is reported. Upgrade path: a
            # custom tokenizer that keeps memory_v2.terms tokens whole.
            logger.warning("memory index: %s MATCH refused %r; scanning", table, tokens)
            wanted = set(tokens)
            found = []
            for n, value in gen.db.execute("SELECT n, terms FROM docs"):
                if wanted & set(str(value).split()):
                    found.append(int(n))
            return found
