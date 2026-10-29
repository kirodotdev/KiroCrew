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
# How long a message turn waits for in-flight preparation before answering that
# recovery is still running (a transient, per-message grace, not a verdict).
MEMORY_ADMISSION_WAIT_SECONDS = 30.0
# The last-resort backstop on the preparation pass. The worker runs restore and
# store open (SQLite, FAISS, file renames) with no step-level timeout, so a
# wedged file lock or a stuck I/O op would otherwise leave the fence preparing
# forever: every turn would then keep answering MEMORY_ADMISSION_WAIT_SECONDS of
# "still running" with no terminal state and no way forward but a restart that
# re-enters the same hang. This is NOT a performance budget for a legitimate
# pass -- it is sized well above the realistic worst case (a large pending
# restore plus legacy store/schedule repair plus a full index rebuild) so a slow
# but progressing boot is never false-failed; it exists only to break an
# otherwise-infinite wait. Past it the boot supervisor converts the hang into a
# failed fence, so turns get an actionable terminal error instead.
MEMORY_PREPARATION_DEADLINE_SECONDS = 1800.0


class MemoryStartupUnavailable(UnknownMemoryStore):
    """Memory cannot be used while gateway recovery is pending or failed."""


class MemoryStartup:
    """One preparing fence, then a failure fence for each affected store."""

    def __init__(self) -> None:
        self.error = ""
        self.store_errors: dict[str, str] = {}
        self.store_remedies: dict[str, str] = {}
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

    def fail_store(self, store: str, error: Exception, *, remedy: str = "") -> None:
        """Retain this store's failure until gateway restart, even after cancel.

        *remedy* replaces the default "pending restore" advice when the caller
        knows the failure has another cause (a damaged database file).
        """
        name = store or DEFAULT_MEMORY_STORE
        with _lock:
            self.store_errors[name] = str(error)
            if remedy:
                self.store_remedies[name] = remedy

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


def damaged_database_remedy(path: object, error: BaseException) -> str:
    """The repair advice for a damaged memory database, or ``""`` for any other failure.

    A damaged ``memory.db`` has no pending restore to cancel, so the default
    advice sent users looking for one. The file is left exactly where it is:
    it holds the store's only copy of its memories, so moving it aside is the
    user's decision, not a startup side effect.
    """
    from kiro_crew.sqlite_quarantine import is_damaged_database_error

    if not is_damaged_database_error(error):
        return ""
    where = f" ({path})" if path else ""
    return (
        f"The memory database file{where} is damaged; it was left untouched. Stop the "
        "gateway, copy that file and any -wal/-shm file beside it somewhere safe, then "
        "rename them (for example to memory.db.corrupt) so a new one is created, and "
        "restart. Do not delete it."
    )


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


def memory_prepared() -> bool:
    """Whether :func:`require_memory_prepared` admits the caller now, without raising."""
    try:
        require_memory_prepared()
    except MemoryStartupUnavailable:
        return False
    return True


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


def memory_startup_preparing() -> bool:
    """Is this process's memory startup still preparing? Never raises.

    False when no startup runs in this process (the CLI), once it is ready, and
    once it failed or stopped: :func:`require_memory_prepared` tells those apart.
    """
    with _lock:
        startup = _active
        return startup is not None and not (startup.error or startup.stopped or startup.ready)


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
            name = store or DEFAULT_MEMORY_STORE
            remedy = (_active.store_remedies.get(name, "") if _active else "") or (
                "Inspect or cancel this store's pending restore, then restart the gateway."
            )
            raise MemoryStartupUnavailable(f"Memory recovery failed for {name!r}: {error} {remedy}")
