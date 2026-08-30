"""Persistence and transaction boundaries for dashboard chat folders."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, TypeVar

from kiro_crew.loop_lock import LoopBoundLock

FOLDERS_FILE = "folders.json"

_T = TypeVar("_T")
_JsonWriter = Callable[[Path, Any], None]


class FolderRepository:
    """Own the folder store's load, write, and serialized mutation rules."""

    def __init__(self, logger_provider: Callable[[], logging.Logger]) -> None:
        self._logger_provider = logger_provider

    def load(self, path: Path, current: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return usable rows from *path*, retaining *current* on store failure."""
        try:
            if not path.exists():
                return current
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                self._logger_provider().warning(
                    "folders.json is a %s, not a list — ignoring it",
                    type(raw).__name__,
                )
                return current
            kept = [
                folder
                for folder in raw
                if isinstance(folder, dict) and isinstance(folder.get("id"), str) and folder["id"]
            ]
            if len(kept) != len(raw):
                self._logger_provider().warning(
                    "dropped %d unusable folder row(s) from folders.json (not a dict, or no id)",
                    len(raw) - len(kept),
                )
            return kept
        except Exception:
            self._logger_provider().warning("Failed to load folders", exc_info=True)
            return current

    @staticmethod
    def save(path: Path, folders: list[dict[str, Any]], write_json: _JsonWriter) -> None:
        write_json(path, folders)

    async def mutate(
        self,
        folders_provider: Callable[[], list[dict[str, Any]]],
        lock: LoopBoundLock,
        mutate: Callable[[list[dict[str, Any]]], tuple[bool, _T]],
        path_provider: Callable[[], Path],
        write_confirmed: Callable[[Path, list[dict[str, Any]]], None],
        on_committed: Callable[[], None] | None = None,
    ) -> _T:
        """Serialize one mutation and retain it only after a confirmed off-loop write.

        The callback mutates the live list while the store lock is held.  Only
        the blocking write crosses the thread boundary, and it receives a
        snapshot rather than reading a list that the event loop may mutate.
        A failed write restores the previous list before the lock is released,
        so readers never observe state that is about to be rolled back.

        ``on_committed`` also runs under the lock, after persistence is proven.
        Keeping post-commit signals in the same critical section prevents two
        concurrent transactions from collapsing a monotonic generation bump.
        It is deliberately skipped for no-op and rolled-back transactions.
        """
        async with lock:
            before = [dict(folder) for folder in folders_provider()]
            changed, value = mutate(folders_provider())
            if not changed:
                return value
            path = path_provider()
            snapshot = [dict(folder) for folder in folders_provider()]
            try:
                await asyncio.to_thread(write_confirmed, path, snapshot)
            except Exception:
                folders_provider()[:] = before
                raise
            if on_committed is not None:
                on_committed()
            return value

    @staticmethod
    async def read(
        folders_provider: Callable[[], list[dict[str, Any]]],
        lock: LoopBoundLock,
        read: Callable[[list[dict[str, Any]]], _T],
    ) -> _T:
        """Expose only committed folder state to a synchronous reader."""
        async with lock:
            return read(folders_provider())

    @staticmethod
    def write_confirmed(
        path: Path,
        snapshot: list[dict[str, Any]],
        write_json: _JsonWriter,
    ) -> None:
        """Write *snapshot* and raise unless the complete value landed."""
        write_json(path, snapshot)
        try:
            on_disk = json.loads(path.read_bytes())
        except Exception as exc:
            raise OSError(f"folder store unreadable after write: {path.name}") from exc
        if on_disk != snapshot:
            raise OSError(f"folder store did not persist as intended: {path.name}")

    @staticmethod
    def breadcrumb(folders: list[dict[str, Any]], folder_id: str, separator: str = " › ") -> str:
        """Render a cycle-safe root-to-leaf path for *folder_id*."""
        if not folder_id:
            return ""
        by_id = {
            folder["id"]: folder
            for folder in folders
            if isinstance(folder, dict) and folder.get("id")
        }
        names: list[str] = []
        seen: set[str] = set()
        current_id = folder_id
        while current_id and current_id in by_id and current_id not in seen:
            seen.add(current_id)
            folder = by_id[current_id]
            names.append(str(folder.get("name", "")))
            current_id = str(folder.get("parent_id") or "")
        names.reverse()
        return separator.join(name for name in names if name)
