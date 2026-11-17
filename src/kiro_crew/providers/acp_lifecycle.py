"""Sequence an ``AcpProvider`` session's start and shutdown.

The start sequence over the overlay, transport and effort steps, shutdown, the registry
claim on a derived run work directory and its reclaim, and the native session-file
cleanup.

Each function here whose first parameter is ``self`` is an ``AcpProvider`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_disposable_work_dir``, ``_effort_per_model``,
``_work_dir_claim_probe``.

Instance fields read: ``_client``, ``_configured_model``, ``essential_delivery``,
``member_context``, ``memory_mode``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, AsyncContextManager

from kiro_crew.acp.client import DEFAULT_MODEL, AcpClient
from kiro_crew.acp.types import METHOD_SESSION_DELETE
from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_EFFORT_AT_LAUNCH,
    ACP_BACKENDS_SHARED_RUNTIME_ONLY_FOR_CHATS,
)
from kiro_crew.agent_sdk.claude_transcripts import delete_claude_session_files
from kiro_crew.agent_sdk.codex_transcripts import delete_codex_session_files, spawn_env_overrides
from kiro_crew.agent_sdk.opencode_transcripts import delete_opencode_session_rows
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread
from kiro_crew.providers.acp_model_effort import launch_effort_level
from kiro_crew.providers.cleanup import _is_safe_path
from kiro_crew.session_work_dir import mark_run_dir, reclaim_session_work_dir

if TYPE_CHECKING:
    from kiro_crew.providers.acp import AcpProvider

#: The provider's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.providers.acp")


def _bound_remotely(session_key: str | None) -> bool:
    """The runtime arm's test: no choices file is local without a read; unreadable is remote."""
    from kiro_crew.providers.acp_runtime_start import _choices_file_may_exist
    from kiro_crew.session_environment import session_runs_remotely

    return (
        bool(session_key)
        and _choices_file_may_exist(session_key)
        and session_runs_remotely(session_key)
    )


async def _chat_takes_shared_runtime(provider: AcpProvider) -> bool:
    """Whether this start, on a harness shared only for chats, got a shared placement.

    The placement is resolved here, before the arm is chosen, and kept for the
    runtime start to use (``_start_kiro_runtime_impl``), so a start the placement
    refuses keeps its own ``AcpClient`` process exactly as before. A session bound
    to a remote workspace never shares: the per-chat arm refuses it as it always has.
    """
    from kiro_crew.providers.acp_runtime_start import resolve_prejoined_chat_share

    if getattr(provider._client, "_fork_request", None):
        # A native rewind fork (Edit & Resend) is armed: only the per-chat client
        # forks and loads the session, so this start keeps its own process.
        return False
    if await asyncio.to_thread(_bound_remotely, getattr(provider._client, "_session_key", None)):
        return False
    return await resolve_prejoined_chat_share(provider)


async def start(self: AcpProvider) -> None:
    if self.memory_mode not in {"persistent", "incognito", "temporary"}:
        raise ValueError("Invalid session memory mode")
    self._client.memory_mode = self.memory_mode
    if self.memory_mode != "persistent":
        self._client._resume_session_id = ""
    self.essential_delivery.invalidate()
    if self._disposable_work_dir:
        # The ONE place the directory's provenance is written: before any
        # writer puts a file in it, on every (re)start, idempotent. The
        # sweep reclaims only a directory that carries this mark and names a
        # dead predecessor of this data home (session_work_dir); the shutdown
        # reclaim below keys on the flag and on the mark naming this process.
        await asyncio.to_thread(mark_run_dir, Path(self._client._work_dir))
    # The workspace cli.json is read and written here and nowhere on the event
    # loop: construction touches no file. Off the loop: the cli.json lock is
    # shared with sibling threads and processes, and an acquire on the loop
    # thread makes one attempt and never waits, so a brief overlap would refuse
    # the write.
    await self._recover_effort_overlay()
    # Re-apply the overlay on every (re)start to cover resume / model swap.
    # (no-op for claude backend — that path applies effort live below.)
    await asyncio.to_thread(self._apply_effort_overlay)
    await asyncio.to_thread(self._apply_tool_search_overlay)

    if self.is_acp_runtime_backend or (
        # A harness the shared runtime serves only for a chat that shares (claude):
        # the placement is asked first, and only a chat that got one takes this arm.
        # Every other backend reads the set and goes on (harness-parity H13).
        self._client.backend in ACP_BACKENDS_SHARED_RUNTIME_ONLY_FOR_CHATS
        and await _chat_takes_shared_runtime(self)
    ):
        # ── Kiro unified path: AcpRuntime + AcpSessionHandle ──
        # Spawn a runtime, create/resume a session, wrap in
        # AcpSessionProvider. One process hosts parent + all subagent
        # sessions (session sharing).
        await self._start_kiro_runtime()
        if self.is_kiro_backend:
            await self._note_zero_tool_servers()
    else:
        # ── CC path: legacy AcpClient ──
        # Only the runtime arm can fence a remote workspace, so refuse here.
        if await asyncio.to_thread(_bound_remotely, getattr(self._client, "_session_key", None)):
            from kiro_crew.acp.runtime import AcpRuntimeError
            from kiro_crew.session_environment import REMOTE_NEEDS_KAS

            raise AcpRuntimeError(REMOTE_NEEDS_KAS)
        self._client.member_context = self.member_context
        if self._client.backend in ACP_BACKENDS_EFFORT_AT_LAUNCH:
            # The launch reads it (``--effort``); a change relaunches.
            self._client._launch_effort = launch_effort_level(self)
        await self._client.ensure_ready()
        # Startup records the advertised spelling of the pin, or the backend
        # default when every spelling was refused; the level follows a pin.
        running = self._client._model
        if running != DEFAULT_MODEL and self._configured_model in self._effort_per_model:
            level = self._effort_per_model.pop(self._configured_model)
            self._effort_per_model.setdefault(running, level)
        # An enrolled member's saved spec is confirmed against what this session
        # actually consumed, once the session exists. Called on THIS arm only: the
        # Kiro and KAS runtimes take the branch above, so their construction paths
        # gain no call, await or branch for it (harness-parity H13). A no-op for
        # every session that projects nothing.
        if isinstance(self._client, AcpClient):
            await self._client.confirm_member_projection()

    await self._bind_unbound_effort()
    await self._apply_initial_effort()


async def shutdown(self: AcpProvider) -> None:
    session_id = self.session_id
    try:
        # pi and goose keep a record only the harness can remove (pi's JSONL in a folder
        # its settings choose, goose's own session store), so each is asked over
        # session/delete. claude, codex and opencode advertise the verb too but are not
        # asked: cleanup_session removes their records from disk, and codex's delete would
        # archive the rollout away from the path that removes it.
        if (
            self.memory_mode != "persistent"
            and session_id
            and (self.is_pi_backend or self.is_goose_backend)
        ):
            await _delete_harness_session(self._client, session_id)
    finally:
        try:
            await self._client.shutdown()
        finally:
            try:
                if self.memory_mode != "persistent" and session_id:
                    await self.cleanup_session(session_id)
            finally:
                # A cancel delivered during the transcript delete must not skip the reclaim.
                if self._disposable_work_dir:
                    await self._reclaim_work_dir()


async def _delete_harness_session(client: AcpClient, session_id: str) -> None:
    """Ask the harness to delete its record of a disposable session, while it still runs.

    The harness's own answer is used rather than a second guess at its paths. Sent
    only when the handshake advertised ``sessionCapabilities.delete`` and the
    process is alive. The answer is awaited for ``_TERMINATE_TIMEOUT``, also while
    the gateway shuts down; a failure is logged and the shutdown goes on.

    shortcut: a process that died before the shutdown keeps its record, since
    nothing is left to ask. Upgrade by reading the harness's store from disk.
    """
    from kiro_crew.acp.runtime import _TERMINATE_TIMEOUT

    init = client.initialize_result
    capabilities = init.get("agentCapabilities") if isinstance(init, dict) else None
    session_caps = (
        capabilities.get("sessionCapabilities") if isinstance(capabilities, dict) else None
    )
    if not isinstance(session_caps, dict) or not isinstance(session_caps.get("delete"), dict):
        logger.warning("session cleanup: the adapter offers no session/delete")
        return
    if not client._is_process_alive():
        logger.warning("session cleanup: the process ended before the delete")
        return
    try:
        req_id = await client._send_request(METHOD_SESSION_DELETE, {"sessionId": session_id})
        await _await_answer(client, req_id, _TERMINATE_TIMEOUT)
    except Exception:
        logger.warning("session cleanup: session/delete failed", exc_info=True)


async def _await_answer(client: AcpClient, req_id: int, timeout: float) -> None:
    """Read stdout until *req_id* is answered or *timeout* passes.

    Not ``_wait_for_response``: that one stops at once when the gateway is shutting
    down, which is exactly when a gateway stop ends every open chat, so the delete
    would never be waited for. Other frames are dropped: the process ends next.
    """
    deadline = time.monotonic() + timeout
    await client._claim_stdout()
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            msg = await client._read_message(timeout=min(remaining, 1.0), check_cancel=False)
            if msg is not None and msg.is_response_for(req_id):
                if msg.error:
                    logger.warning("session cleanup: session/delete was refused")
                return
        logger.warning("session cleanup: session/delete got no answer in time")
    finally:
        client._release_stdout()


def set_work_dir_claim_probe(
    self: AcpProvider,
    probe: Callable[[], AsyncContextManager[bool]],
) -> None:
    """Install the registry claim that guards the final reclaim decision."""
    self._work_dir_claim_probe = probe


def disown_work_dir(self: AcpProvider) -> None:
    """Drop this provider's claim on its derived work directory.

    The factory flag says the directory was derived from a one-run KEY; it
    cannot say whether this instance is the one the registry kept for that
    key. When two providers for the same key race and one is discarded, or a
    recycled session's successor is already registered, the discarded
    provider and the live one share the directory, so the registry tells the
    discarded one to leave it -- the live sibling reclaims it at its own
    shutdown. A marker nonce could not carry this: whichever instance wrote
    the marker can be the one torn down while the other lives.
    """
    self._disposable_work_dir = False


async def _reclaim_work_dir(self: AcpProvider) -> None:
    """Remove this run's derived work dir only after its tree and claim end.

    Shutdown can retain PID tracking when a root or descendant survives, so
    its return alone is not proof that no process still uses this directory
    as its cwd. The registry claim is the other half: it holds the registry
    lock from the final no-successor decision through the off-loop reclaim,
    so a new holder cannot register in between. If cancellation arrives
    during that reclaim, shutdown waits for its worker before releasing the
    claim, then propagates the cancellation. An absent, denied, or broken
    claim fails closed and leaves the directory for the sweep. The reclaim
    itself accepts a missing marker (the flag is its provenance) and, when a
    marker is present, requires it to name this data home and this process
    exactly; nothing about another process is probed.
    """
    if getattr(self._client, "process_tree_confirmed_dead", None) is not True:
        logger.debug("retaining run work dir until its process tree is confirmed dead")
        return
    claim_probe = self._work_dir_claim_probe
    if claim_probe is None:
        logger.debug("retaining run work dir because no registry claim probe is installed")
        return
    work_dir = Path(self._client._work_dir)
    try:
        async with claim_probe() as claimed:
            if not claimed:
                logger.debug("retaining claimed run work dir %s", work_dir)
                return
            reclaim = asyncio.ensure_future(asyncio.to_thread(reclaim_session_work_dir, work_dir))
            try:
                removed = await asyncio.shield(reclaim)
            except asyncio.CancelledError:
                try:
                    await reclaim
                except BaseException:
                    pass
                raise
    except Exception:
        logger.debug("work dir reclaim failed for %s", work_dir, exc_info=True)
        return
    if removed:
        logger.debug("reclaimed run work dir %s", work_dir)


async def cleanup_session(self: AcpProvider, session_id: str) -> None:
    """Delete the native session files of a disposable session.

    kiro-cli's replay log is ``<sid>.json`` + ``.jsonl`` under
    ``~/.kiro/sessions/cli``. A Claude session's record is Claude Code's own
    store instead (``agent_sdk.claude_transcripts``), spelled from the client's work
    directory, and a Codex session's is its rollout under the ``CODEX_HOME`` its
    child ran with, the client's or runtime's ``extra_env`` over the gateway's
    (``agent_sdk.codex_transcripts``). An OpenCode session's is its rows in
    OpenCode's SQLite store, found the same way (``agent_sdk.opencode_transcripts``).
    Each is deleted off the loop, to completion even when cancelled. A pi or goose
    session's record went before its process ended (``_delete_harness_session``).
    """
    if not session_id:
        return
    if self.is_claude_backend:
        work_dir = str(getattr(self._client, "_work_dir", "") or "")
        await drained_to_thread(delete_claude_session_files, session_id, work_dir)
        return
    if self.is_codex_backend:
        await drained_to_thread(delete_codex_session_files, session_id, spawn_env_overrides(self))
        return
    if self.is_opencode_backend:
        await drained_to_thread(delete_opencode_session_rows, session_id, spawn_env_overrides(self))
        return
    await drained_to_thread(_delete_kiro_session_files, session_id)


def _delete_kiro_session_files(session_id: str) -> None:
    """Unlink kiro-cli's replay log of ``session_id``: blocking disk work, run in a thread."""
    sessions_dir = kiro_sessions_dir()
    for suffix in (".json", ".jsonl"):
        target = sessions_dir / f"{session_id}{suffix}"
        if not _is_safe_path(target, sessions_dir):
            logger.error("cleanup_session: path traversal blocked for %s", target)
            return
        try:
            target.unlink(missing_ok=True)
        except OSError:
            logger.warning("cleanup_session: failed to delete %s", target, exc_info=True)
