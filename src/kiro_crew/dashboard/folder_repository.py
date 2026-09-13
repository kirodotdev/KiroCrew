"""Persistence and transaction boundaries for dashboard chat folders."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, TypeVar

from kiro_crew.dashboard.snapshot_commit import commit_snapshot_while_holding_the_lock
from kiro_crew.loop_lock import LoopBoundLock

FOLDERS_FILE = "folders.json"

_T = TypeVar("_T")
_JsonWriter = Callable[[Path, Any], None]


class FolderRepository:
    """Own the folder store's load, write, and serialized mutation rules."""

    def __init__(self, logger_provider: Callable[[], logging.Logger]) -> None:
        self._logger_provider = logger_provider

    def load(self, path: Path, current: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """Return usable rows from *path*, retaining *current* on store failure.

        The second element says whether the returned rows ARE the committed vocabulary,
        and the distinction it carries is load-bearing rather than informational:

        * ``False`` — the vocabulary is UNKNOWN. The file was absent, was not a
          list, or could not be read. A reader must FAIL OPEN, because an absent
          file cannot be told apart from a store that was deleted or is
          unreadable; treating that as an authoritative empty vocabulary would
          make every persisted ``folder_id`` dangling, so restore would prune
          them all and the next save would unfile every conversation.
        * ``True`` — the vocabulary is KNOWN, INCLUDING a legitimately
          empty one (the user deleted their last folder). That IS the vocabulary,
          so pruning a ``folder_id`` naming no known folder is correct.

        Reported from here rather than recovered by the caller because only this
        method can tell the three failure shapes apart: each of them returns
        *current*, so the returned ROWS alone cannot distinguish "unreadable"
        from "parsed an empty list".

        A BOOL rather than the set itself, so this method is not a second spelling of
        the derivation: the caller turns these rows into the committed set through
        :meth:`DashboardState.publish_committed_folder_ids`, the one place that decides
        which ids qualify. The rows handed back are already filtered to non-empty
        ``str`` ids, so that helper filters nothing further here rather than offering a
        second opinion.
        """
        try:
            if not path.exists():
                return current, False
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                self._logger_provider().warning(
                    "folders.json is a %s, not a list — ignoring it",
                    type(raw).__name__,
                )
                return current, False
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
            return kept, True
        except Exception:
            self._logger_provider().warning("Failed to load folders", exc_info=True)
            return current, False

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
        on_committed: Callable[[list[dict[str, Any]]], None] | None = None,
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

        It receives the SNAPSHOT that was serialized and confirmed, not the live
        list, so a signal derived from the store's contents cannot disagree with
        the bytes that actually landed.
        """
        async with lock:
            before = [dict(folder) for folder in folders_provider()]
            changed, value = mutate(folders_provider())
            if not changed:
                return value
            path = path_provider()
            snapshot = [dict(folder) for folder in folders_provider()]

            # CANCELLATION-ATOMIC, via the one shared spelling of the protocol -- see
            # ``commit_snapshot_while_holding_the_lock`` for why each of its three parts
            # is load-bearing. The tag side calls the same helper, so the two
            # vocabularies cannot drift apart.
            #
            # The pre-mutation restore lives HERE rather than as a helper parameter: the
            # helper propagates the write's error precisely so this arm is reached, which
            # is the same shape the tag side already uses. A cancellation must NOT restore
            # -- the shielded write is still completing and its bytes will land -- and the
            # helper only re-raises the write's own failure, never the cancellation, so
            # ``except Exception`` cannot see one.
            write = asyncio.ensure_future(asyncio.to_thread(write_confirmed, path, snapshot))
            try:
                await commit_snapshot_while_holding_the_lock(
                    write,
                    publish=lambda: on_committed(snapshot) if on_committed is not None else None,
                )
            except Exception:
                folders_provider()[:] = before
                raise
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
