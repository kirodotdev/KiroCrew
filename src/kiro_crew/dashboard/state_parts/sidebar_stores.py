"""The durable sidebar stores: chat folders, cron folders, chat pins, tags and boards.

The folder repository, the folder-tree guard and the durable tag snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypeVar

from kiro_crew.dashboard.folder_repository import FolderRepository

logger = logging.getLogger("kiro_crew.dashboard.state")


_FOLDER_REPOSITORY = FolderRepository(lambda: logger)


#: Return type of a mutate_folders callback.
_T = TypeVar("_T")


def _safe_folder_tree(folders: object) -> list[dict[str, Any]]:
    """Well-formed folder entries safe to ship on the slots broadcast frame.

    The loader validates disk rows, but lightweight states and test doubles can
    bypass it. Keep the broadcast hot path tolerant of an unset or malformed
    in-memory value.
    """
    if not isinstance(folders, list):
        return []
    return [f for f in folders if isinstance(f, dict) and isinstance(f.get("id"), str)]


@dataclass(frozen=True)
class _DurableTagSnapshot:
    """A positively read tag snapshot, or positive absence when ``present`` is false."""

    present: bool
    tags: list[dict[str, Any]]
    unparsed: list[Any]
