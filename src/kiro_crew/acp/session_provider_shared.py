"""A chat session on a SHARED runtime: its turn gate and how it leaves.

Chat runtime sharing (the ``chat_runtime_sharing`` feature preview) places
several dashboard chats on one agent process. A provider built for that carries
``shared_runtime=True`` and two behaviours an unshared one does not have:

* its turns run inside the process's chat turn gate (``AcpRuntime.chat_turn_gate``,
  a no-op on KAS), reentrant for the task that holds it so a ``/compact`` can hold
  it through its deferred receipt (:func:`chat_turn_gate`);
* its shutdown leaves the process rather than ending it, unless it held the last
  lease (:func:`shutdown_shared`).

Each function here whose first parameter is ``self`` is an ``AcpSessionProvider``
method: the class binds it by name, so it reads and writes the provider's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields written: ``_chat_gate_holder``, ``_runtime_lease``.

Instance fields read: ``_chat_gate_holder``, ``_handle``, ``_runtime``,
``_runtime_lease``, ``_shared_runtime``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.runtime_ownership import RUNTIME_OWNERSHIP

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.session_provider import AcpSessionProvider

#: The provider's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_provider")


class _ChatTurnScope:
    """The scope :func:`chat_turn_gate` opens.

    A class rather than a generator context manager so the bound member keeps its
    own code object and qualname. Holds the runtime's gate unless this provider is
    unshared, the runtime has no gate (a test double), or the current task already
    holds it through this provider (the reentrant ``/compact`` hold).
    """

    def __init__(self, provider: AcpSessionProvider) -> None:
        self._provider = provider
        self._gate: Any = None

    async def __aenter__(self) -> None:
        provider = self._provider
        if not getattr(provider, "_shared_runtime", False):
            return
        factory = getattr(provider._runtime, "chat_turn_gate", None)
        if factory is None:
            return
        current = asyncio.current_task()
        holder = getattr(provider, "_chat_gate_holder", None)
        if holder is not None and holder is current:
            return
        gate = factory()
        await gate.__aenter__()
        self._gate = gate
        provider._chat_gate_holder = current

    async def __aexit__(self, *exc: Any) -> None:
        gate, self._gate = self._gate, None
        if gate is None:
            return
        self._provider._chat_gate_holder = None
        await gate.__aexit__(*exc)


def chat_turn_gate(self: AcpSessionProvider) -> _ChatTurnScope:
    """Serialize this turn against co-tenant chat turns on a shared process.

    Only a SHARED chat session (founder or joiner) enters the runtime's
    per-process gate; an unshared session and a subagent skip it (a subagent runs
    while its principal's turn is open, so contending for the lock would
    deadlock). Reentrant for the current task, which is what lets a ``/compact``
    hold the gate across the command turn AND its deferred ``wait_for_compaction``
    receipt (``hold_chat_turn_gate``): without that the gate would drop between
    the two and a co-tenant could start a turn into which this session's ownerless
    compaction terminal is fanned.
    """
    return _ChatTurnScope(self)


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
