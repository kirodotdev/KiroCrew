"""One gateway's deferred restore barrier, not a cross-process store lock.

Ordinary CLI/store construction never activates a staged restore. The gateway
closes this in-memory barrier before wiring services, publishes one tracked task
at the readiness boundary, and authorizes that worker to restore and initialize
memory after readiness. Agent turn admission and memory-capable background
services wait for the task. Existing restore journals and file locks still own
crash recovery and cross-process assumptions.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from typing import Iterator

from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, UnknownMemoryStore

_lock = threading.RLock()
_active: MemoryStartup | None = None
MEMORY_ADMISSION_WAIT_SECONDS = 30.0


class MemoryStartupUnavailable(UnknownMemoryStore):
    """Memory cannot be used while gateway recovery is pending or failed."""


class MemoryStartup:
    """One preparing fence, then a failure fence for each affected store."""

    def __init__(self) -> None:
        self.error = ""
        self.store_errors: dict[str, str] = {}
        self.ready = False
        self.stopped = False
        self._worker: int | None = None

    @classmethod
    def begin(cls) -> MemoryStartup:
        global _active
        with _lock:
            if _active is not None:
                raise MemoryStartupUnavailable("Another gateway is still preparing memory.")
            _active = cls()
            return _active

    @contextmanager
    def worker(self) -> Iterator[None]:
        """Authorize only this activation thread, never its async callers."""
        global _active
        with _lock:
            if self.stopped or _active is not self or self._worker is not None:
                raise MemoryStartupUnavailable("Memory startup was stopped.")
            self._worker = threading.get_ident()
        try:
            yield
        finally:
            with _lock:
                self._worker = None
                if self.stopped and _active is self:
                    _active = None

    def complete(self) -> bool:
        with _lock:
            if self.stopped or _active is not self:
                return False
            self.ready = True
            return True

    def fail(self, error: Exception) -> None:
        """A structural startup failure prevents the whole pass from completing."""
        with _lock:
            self.error = str(error)

    def fail_store(self, store: str, error: Exception) -> None:
        """Retain this store's failure until gateway restart, even after cancel."""
        with _lock:
            self.store_errors[store or DEFAULT_MEMORY_STORE] = str(error)

    def stop(self) -> bool:
        """Fence new work; return whether the caller owns final handle cleanup."""
        with _lock:
            self.stopped = True
            self.ready = False
            return self._worker is None

    def release(self) -> None:
        """Release only this owner's completed shutdown, never a successor."""
        global _active
        with _lock:
            if self.stopped and self._worker is None and _active is self:
                _active = None


def require_memory_prepared() -> None:
    """Admit a maintenance pass only; each data access still checks its store."""
    with _lock:
        startup = _active
        if startup is None or startup._worker == threading.get_ident():
            return
        if startup.error:
            raise MemoryStartupUnavailable(
                f"Memory recovery failed: {startup.error} "
                "Inspect or cancel the pending restore, then restart the gateway."
            )
        if startup.stopped:
            raise MemoryStartupUnavailable("Memory startup is stopping. Retry after restart.")
        if startup.ready:
            return
        raise MemoryStartupUnavailable(
            "Memory is being restored and prepared during gateway startup. "
            "Retry when recovery completes."
        )


async def wait_for_memory_preparation(task: asyncio.Future | None) -> None:
    """Give one turn a short grace period without cancelling shared recovery."""
    with _lock:
        if _active is not None and (_active.error or _active.stopped):
            require_memory_prepared()
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=MEMORY_ADMISSION_WAIT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise MemoryStartupUnavailable(
                "Memory preparation is still running. Retry shortly, or inspect memory "
                "recovery in the dashboard. This message did not start an agent."
            ) from exc
    # A completed worker may have recorded a structural failure rather than
    # opened memory. Store-specific failures are checked by the selected store.
    require_memory_prepared()


def memory_store_startup_error(store: str = DEFAULT_MEMORY_STORE) -> str:
    """Owner recovery diagnostics remain available without opening live data."""
    with _lock:
        return _active.store_errors.get(store or DEFAULT_MEMORY_STORE, "") if _active else ""


def memory_restore_startup_status(store: str = DEFAULT_MEMORY_STORE) -> dict:
    """Supplement journals with this gateway's failed activation, including after cancel."""
    error = memory_store_startup_error(store)
    return (
        {"activation_failed": True, "restore_error": error, "restart_required": True}
        if error
        else {}
    )


def require_memory_ready(store: str = DEFAULT_MEMORY_STORE, *, allow_failed: bool = False) -> None:
    """Fail before opening this canonical store, including cached handles."""
    with _lock:
        require_memory_prepared()
        error = memory_store_startup_error(store)
        if error and not allow_failed:
            raise MemoryStartupUnavailable(
                f"Memory recovery failed for {store or DEFAULT_MEMORY_STORE!r}: {error} "
                "Inspect or cancel this store's pending restore, then restart the gateway."
            )
