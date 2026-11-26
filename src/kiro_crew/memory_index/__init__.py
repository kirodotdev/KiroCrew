"""A pluggable vector and word index for memory stores.

``contract`` says what a backend does, ``registry`` names the built-in
backends, and ``conformance`` checks a backend against the contract. ``exact``
is the built-in reference backend.
"""

from kiro_crew.memory_index.contract import (
    CAPABILITIES,
    BuildHandle,
    EmbeddingSpace,
    Hit,
    Ident,
    IndexDoc,
    IndexQuery,
    IndexStatus,
    MemoryIndex,
    MemoryIndexError,
    StoreRef,
)
from kiro_crew.memory_index.registry import backend_names, create_index_backend

__all__ = [
    "CAPABILITIES",
    "BuildHandle",
    "EmbeddingSpace",
    "Hit",
    "Ident",
    "IndexDoc",
    "IndexQuery",
    "IndexStatus",
    "MemoryIndex",
    "MemoryIndexError",
    "StoreRef",
    "backend_names",
    "create_index_backend",
]
