"""A chat session on a SHARED runtime: how it leaves.

Chat runtime sharing (the ``chat_runtime_sharing`` feature preview) places
several dashboard chats on one KAS engine process. A provider built for that
carries ``shared_runtime=True``, and its shutdown leaves the process rather than
ending it, unless it held the last lease (:func:`shutdown_shared`). Its turns are
not serialized: the engine admits one turn per session and runs turns of
different sessions in parallel (``acp.chat_runtime_sharing.KAS_CHAT_SHARING_MIN_ENGINE``).

Each function here whose first parameter is ``self`` is an ``AcpSessionProvider``
method: the class binds it by name, so it reads and writes the provider's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields written: ``_runtime_lease``.

Instance fields read: ``_handle``, ``_runtime``, ``_runtime_lease``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.runtime_ownership import RUNTIME_OWNERSHIP

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.session_provider import AcpSessionProvider

#: The provider's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_provider")


async def shutdown_shared(self: AcpSessionProvider) -> None:
    """Leave a process that may still be serving other chat sessions.

    This session drops its lease and kills the process only when the table hands
    the runtime back, which it does for the LAST holder alone. The session itself
    is evicted either way, keeping its resume record: on kiro-cli that is the
    teardown with ``keep_transcript`` set; on KAS the teardown verb DELETES the
    record, so the eviction runs inside ``keep_record_on_teardown``, which sends
    ``session/close`` instead (``AcpRuntime.evict_session``).

    The in-flight turn is cancelled FIRST: an abandoned prompt keeps running on a
    process nothing here may kill. Everything after the cancel is in a
    ``finally``, so a cancellation delivered into the cancel (the restart path's
    ``wait_for`` budget) cannot skip the release. The release runs as one task this
    frame only waits on, so its RESULT -- the runtime to kill, or None -- survives
    a cancellation landing in the wait; that cancellation is re-raised once the
    teardown is complete.

    The eviction runs BEFORE the release. Unregistering a session pops its queue
    by session id with no owner check (``unregister_session``), and the lease is
    what keeps a second start of this same chat off this process
    (``RuntimeOwnership`` admits no second lease for one session key). Released
    first, a resume of this chat could join, register the same session id, and
    lose its queue to this eviction, so its answer would never arrive.
    """
    cancel_hooks = getattr(self._handle, "_cancel_hook_tasks", None)
    if callable(cancel_hooks):
        cancel_hooks()
    last: AcpRuntime | None = None
    try:
        try:
            if self._handle.is_turn_active:
                await asyncio.wait_for(self._handle.cancel(), timeout=5.0)
        except Exception:
            logger.debug("AcpSessionProvider.shutdown: shared cancel failed", exc_info=True)
    finally:
        cancelled: BaseException | None = None
        # The transcript is this session's resume material and ``destroy`` unlinks
        # it unless told otherwise; the setter refuses for a non-persistent mode,
        # whose files are meant to go.
        self.set_keep_transcript(True)
        try:
            if (
                self.memory_mode == "persistent"
                and getattr(self._runtime, "acp_backend", None) == ACP_BACKEND_KAS
            ):
                with self._runtime.keep_record_on_teardown(self._handle.session_id):
                    await self._handle.destroy()
            else:
                await self._handle.destroy()
        except asyncio.CancelledError as exc:
            # Kept for the end: the lease below must still be returned.
            cancelled = exc
        except Exception:
            logger.debug("AcpSessionProvider.shutdown: shared evict failed", exc_info=True)
        lease = self._runtime_lease
        if lease is not None:
            release_task = asyncio.ensure_future(RUNTIME_OWNERSHIP.release(lease))
            while not release_task.done():
                try:
                    await asyncio.shield(release_task)
                except asyncio.CancelledError as exc:
                    cancelled = exc
            self._runtime_lease = None
            try:
                last = cast("AcpRuntime | None", release_task.result())
            except Exception:
                logger.debug("AcpSessionProvider.shutdown: shared release failed", exc_info=True)
        if last is not None:
            try:
                await last.kill(expected=True, reason="provider shutdown (last shared session)")
            except Exception:
                logger.debug("AcpSessionProvider.shutdown: shared kill failed", exc_info=True)
        if cancelled is not None:
            raise cancelled
