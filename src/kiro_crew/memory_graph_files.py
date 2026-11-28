"""The memory graph's file names, statuses and limits, with no heavy imports.

The layout (:mod:`kiro_crew.memory_graph_layout`) is derived data: positions and
maps rebuilt from the store on the next graph read, never memory text. Backups
read these names to drop the file instead of carrying it, and the dashboard
registers its graph routes with these constants at boot, so they live here and
not beside the layout code, which imports numpy.
"""

from __future__ import annotations

from collections.abc import Sequence

from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME

#: The layout file, beside the store's own ``memory.db``.
GRAPH_LAYOUT_FILE = "graph_layout.sqlite"
#: Suffix of the file a running build writes before it is adopted.
BUILDING_SUFFIX = ".building"

#: What a graph route answers while the layout is current, and before the first
#: build is in.
STATUS_READY = "ready"
STATUS_BUILDING = "building"
#: Most nodes one box request returns.
PAGE_CAP = 2000
#: Characters of a node's label: enough to recognise it on hover.
LABEL_CHARS = 80


def building_name(pid: int) -> str:
    """The name a build in process *pid* writes before the file is adopted."""
    return f".{GRAPH_LAYOUT_FILE}.{pid}{BUILDING_SUFFIX}"


def is_graph_layout_file(rel_parts: Sequence[str]) -> bool:
    """Whether the data-home-relative path *rel_parts* is a store's layout file, or a
    build of one or its SQLite journal: beside the Global ``memory.db`` or in ``memory_stores/<name>/``.
    Lexical only, like the backup checks that ask it."""
    if len(rel_parts) == 1:
        name = rel_parts[0]
    elif len(rel_parts) == 3 and rel_parts[0] == MEMORY_STORES_DIR_NAME:
        name = rel_parts[2]
    else:
        return False
    # SQLite writes journals beside both adopted layouts and builds in progress.
    stem = name.removesuffix("-journal").removesuffix("-wal").removesuffix("-shm")
    return stem == GRAPH_LAYOUT_FILE or (
        stem.startswith(f".{GRAPH_LAYOUT_FILE}.") and stem.endswith(BUILDING_SUFFIX)
    )
