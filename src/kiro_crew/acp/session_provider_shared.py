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
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.runtime_ownership import RUNTIME_OWNERSHIP

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.session_provider import AcpSessionProvider

#: The provider's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_provider")


def teardown_confirmed(runtime: Any, session_id: str | None, *, killed: bool) -> bool:
    """Whether a Bot chat session's teardown is confirmed, for every process owner.

    The one judge for every harness (kiro, KAS, claude, codex and the AcpClient
    hosts), shared runtime or not. Three answers count: the host acknowledged
    ``session/close``; the host answered that it does not hold the session (both
    leave *session_id* out of the runtime's ``_unconfirmed_teardowns``,
    ``AcpRuntime.evict_session``); or, after a kill (*killed*), the process owner's
    whole tree is confirmed dead (``process_tree_confirmed_dead``, which
    ``AcpRuntime.kill`` and ``AcpClient._reset_state`` write). A kill that returned,
    raised or found no session is none of them. Without a kill, a session with no
    id has no host answer to read, so it is not confirmed.
    """
    if killed:
        confirmed = getattr(runtime, "process_tree_confirmed_dead", False) is True
    else:
        confirmed = bool(session_id) and session_id not in getattr(
            runtime, "_unconfirmed_teardowns", ()
        )
    if confirmed and session_id:
        # The one place a Bot chat's per-session mount record leaves: its close is
        # confirmed here, for every runtime.
        from kiro_crew.bot_chat_app_tools import forget_closed_session

        forget_closed_session(session_id)
    return confirmed


class TreeSurvivors:
    """The retry handle of a kill whose tree was not confirmed dead.

    Holds the PIDs that outlived the kill (``_unreaped_pids``); the pending-close
    drain calls :meth:`evict_session` like a runtime's, and it confirms only once
    every one of them is gone. With no PID to watch it never confirms.
    """

    def __init__(self, pids: Any) -> None:
        self.pids = tuple(pids or ())

    async def evict_session(self, session_id: str) -> bool:
        if not self.pids:
            return False
        from kiro_crew.session_pid_tracking import _pid_gone_or_unmanaged

        return await asyncio.to_thread(lambda: all(_pid_gone_or_unmanaged(p) for p in self.pids))


def note_unconfirmed_teardown(
    self: Any, runtime: Any, session_id: str | None, *, session_key: str | None = None
) -> None:
    """Keep *runtime* and *session_id* as this provider's unconfirmed close.

    ``discard_conversation(confirm_teardown=True)`` reads these fields and answers
    unconfirmed; the handle is registered for retry in ``bot_chat_app_tools`` now,
    so a shutdown reached outside ``discard_conversation`` (idle reset, eviction,
    a refused start) keeps it too. The Bot's mounted record is left as it is. A
    session with no id is tracked under a token naming the handle, so the retry
    still has something to settle.
    """
    key = session_key if session_key is not None else getattr(self, "_session_key", None)
    token = session_id or f"unconfirmed-teardown:{id(runtime)}"
    self._kas_shutdown_unconfirmed = True
    self._kas_unconfirmed_runtime = runtime
    self._kas_unconfirmed_session_id = token
    if key is None:
        return
    try:
        from kiro_crew.bot_chat_app_tools import note_unconfirmed_close

        note_unconfirmed_close(key, runtime, token)
    except Exception:  # noqa: BLE001
        logger.warning(
            "AcpSessionProvider.shutdown: note_unconfirmed_close failed for %s",
            key,
            exc_info=True,
        )


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
        sid = getattr(self._handle, "session_id", None)
        destroyed = False
        try:
            if (
                self.memory_mode == "persistent"
                and getattr(self._runtime, "acp_backend", None) == ACP_BACKEND_KAS
            ):
                with self._runtime.keep_record_on_teardown(self._handle.session_id):
                    await self._handle.destroy()
            else:
                await self._handle.destroy()
            destroyed = True
        except asyncio.CancelledError as exc:
            # Kept for the end: the lease below must still be returned.
            cancelled = exc
        except Exception:
            logger.debug("AcpSessionProvider.shutdown: shared evict failed", exc_info=True)
        # Every branch, KAS or not: the close is settled only by the host's answer,
        # which ``evict_session`` / ``terminate_session`` record in the runtime's
        # _unconfirmed_teardowns. A destroy that raised or was cancelled settled
        # nothing, so its handle is kept too.
        if not (destroyed and teardown_confirmed(self._runtime, sid, killed=False)):
            note_unconfirmed_teardown(self, self._runtime, sid)
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
                # The kill settles an unconfirmed close only with the whole tree
                # confirmed dead (``teardown_confirmed``); otherwise the
                # unconfirmed state and its retry handle stay.
                if self._kas_shutdown_unconfirmed and teardown_confirmed(
                    last, self._kas_unconfirmed_session_id, killed=True
                ):
                    unconfirmed_sid = getattr(self, "_kas_unconfirmed_session_id", None)
                    if unconfirmed_sid:
                        try:
                            from kiro_crew.bot_chat_app_tools import confirm_last_lease_killed

                            confirm_last_lease_killed(unconfirmed_sid)
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "AcpSessionProvider.shutdown: confirm_last_lease_killed failed",
                                exc_info=True,
                            )
                    self._kas_shutdown_unconfirmed = False
                elif self._kas_shutdown_unconfirmed:
                    logger.debug(
                        "AcpSessionProvider.shutdown: kill returned but process tree "
                        "not fully confirmed dead; retaining unconfirmed close state"
                    )
            except Exception:
                logger.debug("AcpSessionProvider.shutdown: shared kill failed", exc_info=True)
        if cancelled is not None:
            raise cancelled
