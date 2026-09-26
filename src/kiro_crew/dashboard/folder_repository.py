"""Persistence and transaction boundaries for dashboard chat folders."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from kiro_crew.loop_lock import LoopBoundLock

FOLDERS_FILE = "folders.json"

_T = TypeVar("_T")
_JsonWriter = Callable[[Path, Any], None]


class FolderRepository:
    """Own the folder store's load, write, and serialized mutation rules."""

    def __init__(self, logger_provider: Callable[[], logging.Logger]) -> None:
        self._logger_provider = logger_provider
        # Set when disk may not match memory after a guarded transaction: a
        # rollback that could not land, a rollback image that could not be
        # removed, or a rollback image honored at load. The next mutation
        # re-persists memory and removes the image, under the lock, first.
        self._rewrite_pending = False
        # Set by :meth:`load` when a rollback image exists but cannot be read:
        # the main file may hold a refused change, so nothing is loaded from it
        # and every write is refused until a later load finds no such image.
        self._store_blocked = False

    @staticmethod
    def rollback_image_path(path: Path) -> Path:
        """Where a guarded transaction records the list it may have to restore.

        Written (confirmed) BEFORE the speculative write and removed only once
        the transaction is decided and disk matches memory; :meth:`load` prefers
        it over the main file, so a refused change can never outlive a restart.
        """
        return path.with_name(path.name + ".rollback")

    @staticmethod
    async def _to_completion(fn: Callable[..., None], *args: Any) -> tuple[bool, bool]:
        """Run *fn* in a worker and wait for it to FINISH.

        Returns ``(ok, cancelled)``. A cancellation of the caller does not
        abandon the worker: it is shielded and drained, so a write can never
        land after the caller has released the store lock (over a newer edit).
        The caller re-raises the cancellation once its bookkeeping is done.
        """
        task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        ok = not task.cancelled() and task.exception() is None
        return ok, cancelled

    @staticmethod
    def _remove(path: Path) -> None:
        path.unlink(missing_ok=True)

    async def _settle(
        self,
        write_confirmed: Callable[[Path, list[dict[str, Any]]], None],
        path: Path,
        rows: list[dict[str, Any]],
        *,
        attempts: int,
    ) -> tuple[bool, bool]:
        """Make disk equal *rows* and drop the rollback image.

        Returns ``(settled, cancelled)``: *settled* is True only when the main
        file holds *rows* AND the image is gone. Retried *attempts* times. When
        it cannot finish, the image stays (so a restart restores it), the
        divergence is logged, and ``_rewrite_pending`` makes the next mutation
        try again first; callers must not publish a change over an unsettled
        store.
        """
        cancelled = False
        for _ in range(attempts):
            landed, was_cancelled = await self._to_completion(
                write_confirmed, path, [dict(f) for f in rows]
            )
            cancelled = cancelled or was_cancelled
            if landed:
                removed, was_cancelled = await self._to_completion(
                    self._remove, self.rollback_image_path(path)
                )
                cancelled = cancelled or was_cancelled
                if removed:
                    self._rewrite_pending = False
                    return True, cancelled
            if cancelled:
                break
        self._rewrite_pending = True
        self._logger_provider().error(
            "folder store could not be settled; the rollback image is kept and "
            "is restored on the next start unless a later folder write settles it"
        )
        return False, cancelled

    def load(self, path: Path, current: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return usable rows from *path*, retaining *current* on store failure.

        A rollback image (:meth:`rollback_image_path`) wins over the main file:
        it exists only while a guarded transaction was undecided or its
        rollback could not land, so the main file may hold a refused change.
        The image is honored and the next mutation settles disk to it.
        """
        image = self.rollback_image_path(path)
        self._store_blocked = False
        try:
            if image.exists():
                restored = self._rows(image)
                if restored is not None:
                    self._logger_provider().warning(
                        "folders.json has an unsettled rollback image; restoring it"
                    )
                    self._rewrite_pending = True
                    return restored
                image_unusable: BaseException | None = None
            else:
                image_unusable = None
                restored = []
        except Exception as exc:
            image_unusable, restored = exc, None
        if restored is None:
            # The image exists but cannot be read, so the main file may hold a
            # refused change: fail closed. Keep *current*, and refuse every
            # mutation (settling would write memory over the file) until an
            # operator inspects or removes the image and the store is reloaded.
            self._store_blocked = True
            self._logger_provider().error(
                "folder rollback image %s is unreadable; folders.json is not loaded and "
                "folder changes are refused until it is inspected or removed",
                image,
                exc_info=image_unusable,
            )
            return current
        try:
            if not path.exists():
                return current
            rows = self._rows(path)
            return current if rows is None else rows
        except Exception:
            self._logger_provider().warning("Failed to load folders", exc_info=True)
            return current

    def _rows(self, path: Path) -> list[dict[str, Any]] | None:
        """Usable folder rows from *path*, or ``None`` when it is not a list."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            self._logger_provider().warning(
                "folders.json is a %s, not a list — ignoring it",
                type(raw).__name__,
            )
            return None
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

    def save(self, path: Path, folders: list[dict[str, Any]], write_json: _JsonWriter) -> None:
        if self._store_blocked:
            raise OSError(
                "folder store has an unreadable rollback image; changes are refused "
                "until it is inspected or removed"
            )
        write_json(path, folders)

    async def mutate(
        self,
        folders_provider: Callable[[], list[dict[str, Any]]],
        lock: LoopBoundLock,
        mutate: Callable[[list[dict[str, Any]]], tuple[bool, _T]],
        path_provider: Callable[[], Path],
        write_confirmed: Callable[[Path, list[dict[str, Any]]], None],
        on_committed: Callable[[], None] | None = None,
        revalidate: Callable[[], _T | None] | None = None,
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

        ``revalidate`` re-decides the mutation's precondition AFTER the confirmed
        write, still under the lock: the write suspends, and a fact the callback
        judged (a caller's provenance) can change while it is in flight. Such a
        GUARDED transaction first records the previous list as a confirmed
        rollback image (:meth:`rollback_image_path`), which :meth:`load` prefers
        over the main file, so the speculative write is never durable on its own.
        A non-``None`` answer rolls the transaction back -- the previous list is
        restored in memory and settled to disk -- and is returned in place of the
        callback's value; ``on_committed`` does not run. Every write and removal
        here is shielded and drained under the lock (a cancellation cannot leave
        one landing later over a newer edit), and one that cannot finish keeps
        the image in place and leaves ``_rewrite_pending`` for the next mutation.
        """
        async with lock:
            if self._store_blocked:
                raise OSError(
                    "folder store has an unreadable rollback image; changes are refused "
                    "until it is inspected or removed"
                )
            if self._rewrite_pending:
                settled, cancelled = await self._settle(
                    write_confirmed,
                    path_provider(),
                    [dict(folder) for folder in folders_provider()],
                    attempts=1,
                )
                if cancelled:
                    raise asyncio.CancelledError
                if not settled:
                    # Publishing over an unsettled store could leave a restart to
                    # restore the image over this change: refuse until it settles.
                    raise OSError("folder store is unsettled; retry once it can be written")
            before = [dict(folder) for folder in folders_provider()]
            changed, value = mutate(folders_provider())
            if not changed:
                return value
            path = path_provider()
            snapshot = [dict(folder) for folder in folders_provider()]
            if revalidate is None:
                try:
                    await asyncio.to_thread(write_confirmed, path, snapshot)
                except Exception:
                    folders_provider()[:] = before
                    raise
                if on_committed is not None:
                    on_committed()
                return value
            return await self._guarded_commit(
                folders_provider,
                write_confirmed,
                path,
                before,
                snapshot,
                value,
                on_committed,
                revalidate,
            )

    async def _guarded_commit(
        self,
        folders_provider: Callable[[], list[dict[str, Any]]],
        write_confirmed: Callable[[Path, list[dict[str, Any]]], None],
        path: Path,
        before: list[dict[str, Any]],
        snapshot: list[dict[str, Any]],
        value: _T,
        on_committed: Callable[[], None] | None,
        revalidate: Callable[[], _T | None],
    ) -> _T:
        """The ``revalidate`` half of :meth:`mutate`, run under its lock.

        Publishes (``on_committed``, the returned value) only once disk is
        settled to the decided list: on any path that cannot settle, memory is
        restored to *before* -- which the retained rollback image also restores
        on a restart -- and the call raises.
        """
        image = self.rollback_image_path(path)

        def _abandon(cancelled: bool, reason: str) -> None:
            folders_provider()[:] = before
            if cancelled:
                raise asyncio.CancelledError
            raise OSError(reason)

        recorded, cancelled = await self._to_completion(
            write_confirmed, image, [dict(f) for f in before]
        )
        if cancelled or not recorded:
            # Nothing speculative reached the main file; drop any partial image
            # so it cannot mask a later committed write. An image that cannot
            # be removed (even a valid one, left by a cancelled record) would
            # be restored over every later edit at the next load, so the next
            # mutation must settle disk and remove it first.
            image_removed, _ = await self._to_completion(self._remove, image)
            if not image_removed:
                self._rewrite_pending = True
            _abandon(cancelled, "folder rollback image could not be recorded")

        written, cancelled = await self._to_completion(write_confirmed, path, snapshot)
        if cancelled or not written:
            await self._settle(write_confirmed, path, before, attempts=3)
            _abandon(cancelled, "folder change could not be written")

        refused = revalidate()
        if refused is not None:
            folders_provider()[:] = before
            _settled, cancelled = await self._settle(write_confirmed, path, before, attempts=3)
            if cancelled:
                raise asyncio.CancelledError
            # Memory holds *before*; an unsettled main file is covered by the
            # retained image on a restart and by the next mutation's settle.
            return refused

        # Decided: the speculative write IS the commit. Removing the image is
        # what makes it durable across a restart, so nothing is published
        # until it is gone.
        removed, cancelled = await self._to_completion(self._remove, image)
        if not removed:
            settled, was_cancelled = await self._settle(write_confirmed, path, snapshot, attempts=3)
            cancelled = cancelled or was_cancelled
            if not settled:
                _abandon(cancelled, "folder change could not be made durable")
        if on_committed is not None:
            on_committed()
        if cancelled:
            raise asyncio.CancelledError
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
    async def hold(
        folders_provider: Callable[[], list[dict[str, Any]]],
        lock: LoopBoundLock,
        section: Callable[[list[dict[str, Any]]], Awaitable[_T]],
    ) -> _T:
        """Run an awaitable *section* while the store lock is held.

        For a critical section that must exclude folder writers but whose own
        work belongs off the loop (a file lock, an unlink) -- the same shape
        :meth:`mutate` uses for its confirmed write. *section* receives a
        SNAPSHOT of the committed list, never the live one: it must not
        mutate folder state, and a stale copy cannot leak past the hold.
        """
        async with lock:
            return await section([dict(folder) for folder in folders_provider()])

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
