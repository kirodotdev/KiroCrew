"""The ``exact`` backend: every search scores every document it holds.

It is the reference the conformance kit compares other backends against, and
the answer for a store too small to need an approximate index. It keeps nothing
on disk.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import ClassVar

import numpy as np

from kiro_crew.memory_index.contract import (
    CAP_FILTERS,
    CAP_SUBSTRING,
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

FORMAT_VERSION = 1


class _ExactBuild(BuildHandle):
    def __init__(self, docs: dict[Ident, IndexDoc], build_seq: int) -> None:
        self.docs = docs
        self.build_seq = build_seq


def _matches(doc: IndexDoc, filters: Mapping[str, str]) -> bool:
    return all(doc.filters.get(key) == value for key, value in filters.items())


class ExactIndex:
    name: ClassVar[str] = "exact"
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {CAP_VECTOR, CAP_TERMS, CAP_SUBSTRING, CAP_FILTERS}
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._docs: dict[Ident, IndexDoc] = {}
        self._space: EmbeddingSpace | None = None
        self._built_seq = 0
        self._applied_seq = 0
        # Ready only once a full build was swapped in: an index opened empty must
        # not answer for a store it has never read.
        self._built = False

    def open(self, store: StoreRef, space: EmbeddingSpace, config: Mapping[str, object]) -> None:
        with self._lock:
            self._space = space

    def _require_open(self) -> EmbeddingSpace:
        if self._space is None:
            raise MemoryIndexError("open the index before using it")
        return self._space

    def _checked(self, docs: Iterable[IndexDoc], space: EmbeddingSpace) -> dict[Ident, IndexDoc]:
        out: dict[Ident, IndexDoc] = {}
        for doc in docs:
            if not isinstance(doc, IndexDoc):
                raise MemoryIndexError("upsert takes IndexDoc values")
            if doc.vector is not None:
                if len(doc.vector) != space.dim * 4:
                    raise MemoryIndexError(f"{doc.ident!r} has a vector of the wrong dimension")
                # One NaN would make every later vector search score a non-finite Hit.
                if not np.isfinite(np.frombuffer(doc.vector, dtype=np.float32)).all():
                    raise MemoryIndexError(f"{doc.ident!r} has a non-finite vector")
            out[doc.ident] = doc
        return out

    def upsert(self, docs: Sequence[IndexDoc], *, seq: int) -> None:
        with self._lock:
            self._docs.update(self._checked(docs, self._require_open()))
            self._applied_seq = max(self._applied_seq, seq)

    def remove(self, idents: Sequence[Ident], *, seq: int) -> None:
        with self._lock:
            self._require_open()
            for ident in idents:
                self._docs.pop(ident, None)
            self._applied_seq = max(self._applied_seq, seq)

    def search(self, query: IndexQuery) -> list[Hit]:
        with self._lock:
            space = self._require_open()
            docs = [doc for doc in self._docs.values() if _matches(doc, query.filters)]
        if query.mode == "vector":
            return _vector_hits(docs, query, space)
        if query.mode == "terms":
            wanted = set(query.text.split()) if query.text else set()
            scored = [(len(wanted & set(doc.terms.split())), doc.ident) for doc in docs]
            hits = [(float(score), ident) for score, ident in scored if score >= query.min_match]
        else:
            words = query.text.split() if query.text else []
            hits = [(1.0, doc.ident) for doc in docs if all(word in doc.text for word in words)]
        hits.sort(key=lambda hit: (-hit[0], hit[1]))
        return [Hit(ident, score) for score, ident in hits[: query.k]]

    def rebuild(self, source: Iterable[IndexDoc], *, build_seq: int) -> BuildHandle:
        space = self._require_open()
        return _ExactBuild(self._checked(source, space), build_seq)

    def swap(self, handle: BuildHandle) -> None:
        if not isinstance(handle, _ExactBuild):
            raise MemoryIndexError("swap takes a build this backend made")
        with self._lock:
            self._docs = dict(handle.docs)
            self._built_seq = handle.build_seq
            self._built = True
            # The build replaced whatever upserts landed since it started, so the
            # index now holds exactly the build: the caller replays newer seqs.
            self._applied_seq = handle.build_seq

    def status(self) -> IndexStatus:
        with self._lock:
            return IndexStatus(
                backend=self.name,
                state="ready" if self._space is not None and self._built else "missing",
                built_seq=self._built_seq,
                applied_seq=self._applied_seq,
                rows=len(self._docs),
                space_signature=self._space.signature if self._space else "",
                format_version=FORMAT_VERSION,
            )

    def close(self) -> None:
        with self._lock:
            self._docs = {}
            self._space = None


def _vector_hits(docs: list[IndexDoc], query: IndexQuery, space: EmbeddingSpace) -> list[Hit]:
    assert query.vector is not None  # IndexQuery guarantees it for vector mode
    if len(query.vector) != space.dim * 4:
        raise MemoryIndexError("the query vector has the wrong dimension")
    with_vectors = [doc for doc in docs if doc.vector is not None]
    if not with_vectors:
        return []
    matrix = np.frombuffer(
        b"".join(doc.vector for doc in with_vectors if doc.vector), dtype=np.float32
    )
    scores = matrix.reshape(len(with_vectors), space.dim) @ np.frombuffer(
        query.vector, dtype=np.float32
    )
    order = sorted(
        range(len(with_vectors)), key=lambda i: (-float(scores[i]), with_vectors[i].ident)
    )
    return [Hit(with_vectors[i].ident, float(scores[i])) for i in order[: query.k]]
