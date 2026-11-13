"""Notes' process-local admission plus the shared document-lock vault mutex."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.apps.document_store import DocumentStore
from kiro_crew.atomic_write import pinned_parent_replace_supported
from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread
from kiro_crew.loop_lock import LoopBoundLock


def supported() -> bool:
    return (
        not platform_compat.IS_WINDOWS
        and pinned_fs.supports_pinned_walk()
        and pinned_parent_replace_supported()
    )


class VaultWriteLock(LoopBoundLock):
    """Keep native loop admission and serialize the backend with the gateway.

    The internal label selects only a central-cache lock; no leaf is created in
    the vault. All contexts acquire the vault mutex before a note mutex.
    """

    def __init__(self, local_path: str):
        super().__init__()
        self.local_path = local_path
        self._contexts: dict[asyncio.Task[Any] | None, Any] = {}

    async def __aenter__(self):
        await super().__aenter__()
        if not supported():
            return
        context = DocumentStore(Path(self.local_path)).locked_path("__vault_write__")
        try:
            try:
                await drained_to_thread(context.__enter__)
            except asyncio.CancelledError:
                # The worker successfully entered before cancellation is
                # re-raised by drained_to_thread. Close that acquired context.
                await drained_to_thread(context.__exit__, None, None, None)
                raise
            self._contexts[asyncio.current_task()] = context
        except BaseException:
            self.release()
            raise

    async def __aexit__(self, *exc_info):
        context = self._contexts.pop(asyncio.current_task(), None)
        try:
            if context is not None:
                await drained_to_thread(context.__exit__, *exc_info)
        finally:
            self.release()


_locks: dict[str, VaultWriteLock] = {}


def vault_write_lock(local_path: str) -> VaultWriteLock:
    if local_path not in _locks:
        _locks[local_path] = VaultWriteLock(local_path)
    return _locks[local_path]
