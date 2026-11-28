"""Which memory index backends exist, by name.

Only the built-in backends: ``exact`` (the reference scan) and ``local``. A
public registration hook waits for the first real second backend, so its
contract is shaped by a caller instead of being frozen before one exists.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from kiro_crew.memory_index.contract import MemoryIndex, MemoryIndexError
from kiro_crew.memory_index.exact import ExactIndex
from kiro_crew.memory_index.local import LocalIndex

IndexFactory = Callable[[], MemoryIndex]

_BUILTINS: Final[dict[str, IndexFactory]] = {
    ExactIndex.name: ExactIndex,
    LocalIndex.name: LocalIndex,
}


def backend_names() -> list[str]:
    return sorted(_BUILTINS)


def create_index_backend(name: str) -> MemoryIndex:
    """A new, unopened backend instance for *name*."""
    factory = _BUILTINS.get(name)
    if factory is None:
        raise MemoryIndexError(f"no memory index backend named {name!r}")
    return factory()
