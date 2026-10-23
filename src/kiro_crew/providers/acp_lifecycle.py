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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, AsyncContextManager

from kiro_crew.acp.client import DEFAULT_MODEL, AcpClient
from kiro_crew.config.paths import kiro_sessions_dir
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
    # Re-apply the overlay on every (re)start to cover resume / model swap.
    # (no-op for claude backend — that path applies effort live below.)
    # Off the loop: the cli.json lock is shared with sibling threads and
    # processes, and an acquire on the loop thread makes one attempt and
    # never waits, so a brief overlap would refuse the write.
    await asyncio.to_thread(self._apply_effort_overlay)
    await asyncio.to_thread(self._apply_tool_search_overlay)

    if self.is_acp_runtime_backend:
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
        await self._client.shutdown()
    finally:
        if self.memory_mode != "persistent" and session_id:
            await self.cleanup_session(session_id)
        if self._disposable_work_dir:
            await self._reclaim_work_dir()


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
    """Delete kiro-cli session files (.json + .jsonl) at ~/.kiro/sessions/cli.

    (The claude seam's SDK transcript cleanup, ~/.claude/projects/, is
    re-added by the internal companion alongside its Claude backend.)
    """
    if not session_id:
        return
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
