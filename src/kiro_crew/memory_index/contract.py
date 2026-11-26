"""The memory index contract: what a vector and word index backend must do.

A backend only generates candidates. It never decides what a caller may see:
the caller re-reads every hit from the store and drops a row that is deleted or
outside its guard, so a backend that returns a stale or unknown id costs a
wasted lookup, never a leaked memory. Freshness (which writes a backend has
applied) is reported through ``seq`` numbers the caller hands in, so every
backend is judged by the same change counter.

Text handed to a backend is already normalized by the caller (NFKC, casefold),
and vectors are already L2-normalized, so a backend compares what it is given
and never re-normalizes.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Final, Literal, Protocol, runtime_checkable

#: Search by cosine against a query vector.
CAP_VECTOR: Final = "vector"
#: Search by whole normalized terms (``memory_v2.terms`` output).
CAP_TERMS: Final = "terms"
#: Search by substring of the normalized text, in any language.
CAP_SUBSTRING: Final = "substring"
#: Apply ``IndexQuery.filters`` itself. Without it the caller filters.
CAP_FILTERS: Final = "filters"
CAPABILITIES: Final = frozenset({CAP_VECTOR, CAP_TERMS, CAP_SUBSTRING, CAP_FILTERS})

SearchMode = Literal["vector", "terms", "substring"]
SEARCH_MODES: Final = ("vector", "terms", "substring")
_MODE_CAPABILITY: Final = {"vector": CAP_VECTOR, "terms": CAP_TERMS, "substring": CAP_SUBSTRING}

IndexState = Literal["ready", "building", "missing", "stale", "failed"]
INDEX_STATES: Final = ("ready", "building", "missing", "stale", "failed")

#: ``(relation, id)``: ``("items", memory_items.id)`` on a V2 store,
#: ``("semantic", key)`` or ``("episodic", id)`` on V1.
Ident = tuple[str, str]


class MemoryIndexError(Exception):
    """A request the backend refuses: a malformed document or query, or a use
    before ``open``. Never raised for a missing ident."""


def _check_ident(ident: object) -> None:
    if not (
        isinstance(ident, tuple)
        and len(ident) == 2
        and all(isinstance(part, str) and part for part in ident)
    ):
        raise MemoryIndexError(
            f"an ident is a (relation, id) pair of non-empty strings, not {ident!r}"
        )


@dataclass(frozen=True)
class EmbeddingSpace:
    """The vector space a build belongs to.

    ``signature`` is ``embeddings_shared.embedding_space_signature(model_id, dim)``;
    a build whose signature differs from the active one is stale.
    """

    signature: str
    dim: int

    def __post_init__(self) -> None:
        if (
            not self.signature
            or isinstance(self.dim, bool)
            or not isinstance(self.dim, int)
            or self.dim < 1
        ):
            raise MemoryIndexError("an embedding space needs a signature and a positive dimension")


@dataclass(frozen=True)
class StoreRef:
    """Which store an index serves. ``index_dir`` is where a local backend may
    keep derived files; ``None`` means keep nothing on disk."""

    store_id: str
    index_dir: str | None = None


@dataclass(frozen=True)
class IndexDoc:
    ident: Ident
    kind: str
    #: Packed float32, L2-normalized, ``EmbeddingSpace.dim`` values; ``None`` when
    #: the row has no vector yet.
    vector: bytes | None
    #: Normalized search text.
    text: str
    #: ``" ".join(sorted(memory_v2.terms(text)))``.
    terms: str
    filters: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_ident(self.ident)
        if self.vector is not None and (not isinstance(self.vector, bytes) or len(self.vector) % 4):
            raise MemoryIndexError("a vector is packed float32 bytes")


@dataclass(frozen=True)
class IndexQuery:
    """One search.

    - ``vector``: ``vector`` is a packed float32 query; hits are best cosine first.
    - ``terms``: ``text`` is whitespace-separated terms; a hit holds at least
      ``min_match`` of them, and more matched terms rank first. Every document
      holding ``min_match`` terms is a hit, up to ``k``.
    - ``substring``: ``text`` is whitespace-separated words; a hit contains every
      word as a substring of its text.
    """

    mode: SearchMode
    k: int
    vector: bytes | None = None
    text: str | None = None
    filters: Mapping[str, str] = field(default_factory=dict)
    min_match: int = 1

    def __post_init__(self) -> None:
        if self.mode not in SEARCH_MODES:
            raise MemoryIndexError(f"unknown search mode {self.mode!r}")
        if isinstance(self.k, bool) or not isinstance(self.k, int) or self.k < 1:
            raise MemoryIndexError("k is a positive integer")
        if self.mode == "vector":
            if not isinstance(self.vector, bytes) or not self.vector or len(self.vector) % 4:
                raise MemoryIndexError("a vector search needs a packed float32 vector")
        elif not isinstance(self.text, str) or not self.text.split():
            raise MemoryIndexError(f"a {self.mode} search needs text")
        if self.mode == "terms" and self.text is not None:
            wanted = len(set(self.text.split()))
            if isinstance(self.min_match, bool) or not isinstance(self.min_match, int):
                raise MemoryIndexError("min_match is an integer")
            if not 1 <= self.min_match <= wanted:
                raise MemoryIndexError(f"min_match is 1 to {wanted} for this query")

    @property
    def capability(self) -> str:
        return _MODE_CAPABILITY[self.mode]


@dataclass(frozen=True)
class Hit:
    ident: Ident
    score: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.score):
            raise MemoryIndexError("a hit's score is finite")


@dataclass(frozen=True)
class IndexStatus:
    backend: str
    state: IndexState
    #: The change-log seq the last ``rebuild`` covered.
    built_seq: int
    #: The highest seq handed to ``upsert`` or ``remove`` since the last
    #: ``swap``, or that swap's ``build_seq``.
    applied_seq: int
    rows: int
    #: The embedding space of the current build ("" before any build).
    space_signature: str
    format_version: int
    detail: str = ""


class BuildHandle:
    """An opaque finished build, waiting for ``swap``. Each backend subclasses it."""


@runtime_checkable
class MemoryIndex(Protocol):
    """A vector and word index for one memory store.

    ``upsert`` and ``remove`` are idempotent per ident; removing an ident the
    index never held does nothing. A search never sees a build before ``swap``,
    and never a mix of two builds. ``swap`` replaces the whole content with the
    build, so it sets ``applied_seq`` back to the build's ``build_seq``: the
    caller re-applies every change newer than that.
    """

    name: ClassVar[str]
    capabilities: ClassVar[frozenset[str]]

    def open(
        self, store: StoreRef, space: EmbeddingSpace, config: Mapping[str, object]
    ) -> None: ...

    def upsert(self, docs: Sequence[IndexDoc], *, seq: int) -> None: ...

    def remove(self, idents: Sequence[Ident], *, seq: int) -> None: ...

    def search(self, query: IndexQuery) -> list[Hit]: ...

    def rebuild(self, source: Iterable[IndexDoc], *, build_seq: int) -> BuildHandle: ...

    def swap(self, handle: BuildHandle) -> None: ...

    def status(self) -> IndexStatus: ...

    def close(self) -> None: ...
