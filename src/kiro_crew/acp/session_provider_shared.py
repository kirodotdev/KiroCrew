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
        from kiro_crew.bot_chat_ending_fence import note_closed_session

        forget_closed_session(session_id, runtime)
        note_closed_session(session_id)
    return confirmed


#: Prefixes of the tokens a close with no native session id is tracked under. A
#: token is a local key only: no host holds it, so it is never sent to one, and a
#: host answering "not found" for it says nothing (:func:`close_handle`).
LOCAL_CLOSE_TOKEN_PREFIXES = ("unconfirmed-teardown:", "unconfirmed-start:")


def is_local_close_token(session_id: object) -> bool:
    """Whether *session_id* is a local tracking token, not a native session id."""
    return isinstance(session_id, str) and session_id.startswith(LOCAL_CLOSE_TOKEN_PREFIXES)


def local_close_token(prefix: str, owner: Any, session_key: str, attempt: str = "") -> str:
    """The token a close with no native id is kept under: *prefix* (one of
    :data:`LOCAL_CLOSE_TOKEN_PREFIXES`) and the identity it belongs to -- the chat
    (*session_key*), the process owner, and the process that owner ran then. An
    *attempt* names one start request: its token is settled by that start alone."""
    assert prefix in LOCAL_CLOSE_TOKEN_PREFIXES, prefix
    return f"{prefix}{_identity(owner, session_key)}" + (f"#{attempt}" if attempt else "")


def _identity(owner: Any, session_key: str) -> str:
    return f"{session_key}|{id(owner)}|{id(getattr(owner, '_process', None))}"


def _same_start(token: str, handle: Any, session_key: str) -> bool:
    """Whether a native id that *handle* reports for *session_key* is the one the
    start kept under *token* was waiting for. Only for an owner that runs that one
    chat's one session (``AcpClient``): the token must name this chat, this owner
    and the process it runs now. A shared runtime runs many sessions, so no id it
    reports later can be tied to a start that never got one."""
    owner = _owner(handle)
    if not session_key or getattr(owner, "_session_key", None) != session_key:
        return False
    return any(token == f"{p}{_identity(owner, session_key)}" for p in LOCAL_CLOSE_TOKEN_PREFIXES)


def close_handle(handle: Any, session_id: str | None, session_key: str = "") -> tuple[Any, str]:
    """The ``(handle, id)`` a pending close is kept and retried under.

    A real native id keeps *handle* (the host's answer settles it). With no id,
    the id is a local token naming the chat and process owner
    (:func:`local_close_token`), and a process owner is judged by its tree alone
    (:class:`RuntimeTreeGone`): a close sent under the token would be answered
    "not found" and read as confirmed. A handle that never asks a host (the tree
    and kill handles here, ``local_close_handle``) is kept as is.
    """
    if session_id and not is_local_close_token(session_id):
        return handle, session_id
    token = session_id or local_close_token("unconfirmed-teardown:", _owner(handle), session_key)
    if getattr(handle, "local_close_handle", False) is True:
        return handle, token
    return RuntimeTreeGone(handle), token


def merge_close_handle(held: list, handle: Any, session_id: str, session_key: str = "") -> None:
    """Add ``(handle, session_id)`` to *held*, a Bot app's pending closes, once.

    A real id that the start kept under a local token got late replaces that token
    (:func:`_same_start`); any other token stays, with its tree handle, until its own
    evidence settles it. A codex tree handle kept under the token stays a tree
    handle under the real id: a host's close answer is weaker evidence there and
    never takes over from it."""
    if not is_local_close_token(session_id):
        kept = [
            (h, s)
            for h, s in held
            if is_local_close_token(s)
            and _owner(h) is _owner(handle)
            and _same_start(s, handle, session_key)
        ]
        held[:] = [entry for entry in held if entry not in kept]
        for h, _token in kept:
            if _tree_evidence_needed(h) and (h, session_id) not in held:
                held.append((h, session_id))
    if (handle, session_id) not in held:
        held.append((handle, session_id))


def _tree_evidence_needed(handle: Any) -> bool:
    """Whether *handle* is a tree handle for a host whose close leaves the session's
    servers running (codex), so only the tree's death may settle it."""
    from kiro_crew.acp_backends import ACP_BACKEND_CODEX

    owner = getattr(handle, "runtime", None)
    return isinstance(handle, RuntimeTreeGone) and (
        getattr(owner, "acp_backend", None) == ACP_BACKEND_CODEX
    )


def _owner(handle: Any) -> Any:
    """The process owner *handle* watches: a tree handle's runtime, else itself."""
    return getattr(handle, "runtime", None) or handle


def replace_local_token(
    pending: dict, handle: Any, session_id: str | None, session_key: str = ""
) -> bool:
    """Give the pending close a start kept under a local token its real native
    *session_id*, when that start is *session_key*'s on *handle*'s process
    (:func:`_same_start`). True when one was replaced, so the caller needs no new
    entry; a token of any other session is left as it is."""
    if not session_id or is_local_close_token(session_id):
        return False
    swapped = False
    for held in pending.values():
        if any(
            is_local_close_token(s)
            and _owner(h) is _owner(handle)
            and _same_start(s, handle, session_key)
            for h, s in held
        ):
            merge_close_handle(held, handle, session_id, session_key)
            swapped = True
    return swapped


class TreeSurvivors:
    """The retry handle of a kill whose tree was not confirmed dead.

    Holds the PIDs that outlived the kill (``_unreaped_pids``); the pending-close
    drain calls :meth:`evict_session` like a runtime's, and it confirms only once
    every one of them is gone. With no PID to watch it never confirms.
    """

    local_close_handle = True

    def __init__(self, pids: Any, receipt: Any = None) -> None:
        self.pids = tuple(pids or ())
        self.receipt = receipt

    async def evict_session(self, session_id: str) -> bool:
        if windows_receipt_retired(self.receipt):
            return True  # the cleanup sweep drained the tree the kill left pending
        if not self.pids:
            return False
        from kiro_crew.session_pid_tracking import _pid_gone_or_unmanaged

        return await asyncio.to_thread(lambda: all(_pid_gone_or_unmanaged(p) for p in self.pids))


def windows_receipt_retired(receipt: Any) -> bool:
    """Whether a Windows tree cleanup a kill left pending (``WindowsTreeDrainPending``)
    has since been drained and retired by the cleanup sweep. The receipt is the
    process's ``_windows_cleanup_state``, kept on its owner as
    ``_windows_cleanup_receipt``; ``None`` (no pending cleanup) proves nothing."""
    return receipt is not None and getattr(receipt, "retired", False) is True


class RuntimeTreeGone:
    """The retry handle of a close that does not end the session's MCP servers
    (``AcpSessionProvider.close_leaves_mcp_servers``): they run until the process
    does. Confirms once *runtime*'s tree is confirmed dead, after a kill that left
    survivors once every PID it could not show gone is gone, or for an owner that
    never launched a process (``_spawned`` False) at once."""

    local_close_handle = True

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def evict_session(self, session_id: str) -> bool:
        if getattr(self.runtime, "process_tree_confirmed_dead", False) is True:
            return True
        receipt = getattr(self.runtime, "_windows_cleanup_receipt", None)
        if windows_receipt_retired(receipt):
            return True  # the sweep drained the Windows tree the kill left pending
        is_alive = getattr(self.runtime, "is_alive", None)
        if callable(is_alive) and is_alive():
            return False
        pids = tuple(getattr(self.runtime, "_unreaped_pids", ()) or ())
        if not pids and getattr(self.runtime, "_spawned", None) is False:
            return True  # an owner that never launched a process left nothing running
        return await TreeSurvivors(pids).evict_session(session_id)


def note_unconfirmed_teardown(
    self: Any, runtime: Any, session_id: str | None, *, session_key: str | None = None
) -> None:
    """Keep *runtime* and *session_id* as this provider's unconfirmed close.

    ``discard_conversation(confirm_teardown=True)`` reads these fields and answers
    unconfirmed; the handle is registered for retry in ``bot_chat_app_tools`` now,
    so a shutdown reached outside ``discard_conversation`` (idle reset, eviction,
    a refused start) keeps it too. The Bot's mounted record is left as it is. A
    session with no id is tracked under a local token and judged by its process
    tree (:func:`close_handle`), never by a close sent under the token.
    """
    key = session_key if session_key is not None else getattr(self, "_session_key", None)
    runtime, token = close_handle(runtime, session_id, key or "")
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
        # nothing, so its handle is kept too. A host whose close leaves the
        # session's MCP servers running (codex) settles only with its tree dead.
        if getattr(self, "close_leaves_mcp_servers", False) is True:
            note_unconfirmed_teardown(self, RuntimeTreeGone(self._runtime), sid)
        elif not (destroyed and teardown_confirmed(self._runtime, sid, killed=False)):
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
            killed = False
            try:
                await last.kill(expected=True, reason="provider shutdown (last shared session)")
                killed = True
            except Exception:
                logger.debug("AcpSessionProvider.shutdown: shared kill failed", exc_info=True)
            # The kill settles an unconfirmed close only when it returned with the
            # whole tree confirmed dead (``teardown_confirmed``); a kill that raised
            # or left survivors keeps the unconfirmed state and its retry handle.
            try:
                if (
                    killed
                    and self._kas_shutdown_unconfirmed
                    and teardown_confirmed(last, self._kas_unconfirmed_session_id, killed=True)
                ):
                    unconfirmed_sid = getattr(self, "_kas_unconfirmed_session_id", None)
                    if unconfirmed_sid:
                        try:
                            from kiro_crew.bot_chat_app_tools import confirm_last_lease_killed

                            confirm_last_lease_killed(unconfirmed_sid, last)
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
                logger.debug("AcpSessionProvider.shutdown: kill settle failed", exc_info=True)
        if cancelled is not None:
            raise cancelled
