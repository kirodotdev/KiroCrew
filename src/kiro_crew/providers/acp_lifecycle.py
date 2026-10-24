"""Sequence an ``AcpProvider`` session's start and shutdown.

The start sequence over the overlay, transport and effort steps, shutdown, the registry
claim on a derived run work directory and its reclaim, and the native session-file
cleanup.

Each function here whose first parameter is ``self`` is an ``AcpProvider`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_disposable_work_dir``, ``_effort_per_model``,
``_explicit_effort_default_applied``, ``_explicit_effort_default_pending``,
``_work_dir_claim_probe``.

Instance fields read: ``_client``, ``_configured_model``,
``_defer_replay_sid_promotion``, ``_fence_explicit_effort_default_rewrite``,
``_history_replay_needed``, ``_on_gate_acquired``, ``_on_gate_queued``, ``essential_delivery``,
``member_context``, ``memory_mode``, ``start_priority``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, AsyncContextManager, Literal

from kiro_crew.acp.client import DEFAULT_MODEL, AcpClient
from kiro_crew.acp.types import ACP_BACKENDS_EFFORT_OVERLAY
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.providers.acp_cli_overlay import (
    _EFFORT_OVERLAY_MODEL_OVERRIDE,
    _WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS,
    _await_overlay_write,
    _clear_cli_overlay_effort,
    _cli_overlay_effort_models,
    _hold_workspace_effort_overlay_fence,
    _hold_workspace_effort_overlay_window_under_admission,
    _write_cli_overlay,
)
from kiro_crew.providers.cleanup import _is_safe_path
from kiro_crew.session_work_dir import mark_run_dir, reclaim_session_work_dir
from kiro_crew.workspace_cli_settings import CLI_SETTINGS_LOCK_TIMEOUT_SECS

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


async def _finish_unpinned_explicit_effort_default(
    provider: AcpProvider, effort_models_at_spawn: set[str] | None
) -> Literal["applied", "push", "restart", "pending"]:
    """Finish an unpinned explicit Default after startup.

    ``applied`` means no live update is needed, ``push`` means consume only after
    the projected level is pushed live, ``restart`` means a cleared entry needs a
    fresh runtime, and ``pending`` means the intent could not be applied yet.
    """
    model = provider.served_model
    if not model:
        logger.warning(
            "Explicit effort Default remains pending because the served model is unknown"
        )
        return "pending"
    if effort_models_at_spawn is None:
        logger.warning(
            "Explicit effort Default remains pending for served model %s because the "
            "workspace overlay was not projected or could not be read before spawn",
            model,
        )
        return "pending"
    if not provider.supports_effort() or model not in effort_models_at_spawn:
        return "applied"

    level = provider._resolve_effort()
    try:
        async with _hold_workspace_effort_overlay_fence(
            provider._client._work_dir, "write", CLI_SETTINGS_LOCK_TIMEOUT_SECS
        ) as acquired:
            if not acquired:
                raise TimeoutError("the workspace overlay write fence timed out")
            with provider._fence_explicit_effort_default_rewrite():
                if level:
                    await _await_overlay_write(
                        lambda: _write_cli_overlay(
                            provider._client._work_dir,
                            model,
                            level,
                            timeout=CLI_SETTINGS_LOCK_TIMEOUT_SECS,
                            replace_unowned=True,
                        )
                    )
                elif not await _await_overlay_write(
                    lambda: _clear_cli_overlay_effort(
                        provider._client._work_dir,
                        model,
                        timeout=CLI_SETTINGS_LOCK_TIMEOUT_SECS,
                        owned_only=False,
                    )
                ):
                    raise OSError("the workspace overlay could not be rewritten")
    except Exception as exc:
        logger.warning(
            "Explicit effort Default remains pending for served model %s because the "
            "workspace overlay rewrite failed: %s",
            model,
            exc,
        )
        return "pending"
    return "push" if level else "restart"


async def _restart_for_explicit_effort_default(
    provider: AcpProvider,
    placeholder_client: AcpClient,
    initial_resume_session_id: str,
    initial_history_replay_needed: bool,
    initial_defer_replay_sid_promotion: bool,
    project_and_start: Callable[[bool], Awaitable[set[str] | None]],
) -> bool:
    """Restart once from the start-entry resume state; report whether Default took.

    Raises when the first runtime cannot be stopped or the replacement still reads
    its served model's effort entry, so the start fails instead of publishing it.
    """
    first_client = provider._client
    if first_client is placeholder_client:
        logger.warning(
            "Explicit effort Default remains pending because the running session "
            "cannot be restarted"
        )
        return False
    if getattr(first_client, "resumed", False):
        logger.info(
            "Explicit effort Default removed the served model's workspace entry, but a "
            "session restored with session/load keeps the level it last ran at until its "
            "next fresh session"
        )
        return True
    first_session_id = getattr(first_client, "_session_id", "") or ""
    try:
        await first_client.shutdown()
    except Exception as exc:
        logger.warning(
            "Explicit effort Default remains pending because shutting down the "
            "first runtime failed: %s",
            exc,
        )
        raise RuntimeError("shutting down the first runtime failed") from exc

    process_alive = getattr(first_client, "is_alive", None)
    if callable(process_alive) and process_alive():
        logger.warning(
            "Explicit effort Default remains pending because the first runtime did not stop"
        )
        raise RuntimeError("the first runtime did not stop")

    # A persistent shutdown keeps the first session's transcript, and the
    # replacement restores the original resume id, so that session can never be
    # loaded again and no session-map row names its files for the prune to reach.
    # Its runtime has stopped, so deleting them here races nothing.
    if first_session_id and first_session_id != initial_resume_session_id:
        try:
            await provider.cleanup_session(first_session_id)
        except Exception as exc:
            logger.warning(
                "Explicit effort Default could not delete the restarted first session's "
                "files for %s: %s",
                first_session_id,
                exc,
            )

    # Repeat the first start's resume decision. A new chat stays fresh, while a
    # Tool Search replay start receives the same persisted id and chooses replay
    # again. The replay flags are put back because they are the start's outputs,
    # not inputs to that decision: allocation reads them after ``start()`` returns
    # to arm ``provider_switch_replay``, and ``defer_replay_sid_promotion`` is read
    # from the provider the same way, so a flag only the first attempt's outcome
    # set must not be published for a replacement that did not produce it.
    placeholder_client.memory_mode = provider.memory_mode
    placeholder_client._resume_session_id = initial_resume_session_id
    provider._history_replay_needed = initial_history_replay_needed
    provider._defer_replay_sid_promotion = initial_defer_replay_sid_promotion
    provider._client = placeholder_client
    try:
        second_effort_models = await project_and_start(True)
    except Exception as exc:
        provider._client = placeholder_client
        logger.warning(
            "Explicit effort Default remains pending because the replacement runtime "
            "failed to start: %s",
            exc,
        )
        raise

    second_model = provider.served_model
    if not second_model:
        logger.warning(
            "Explicit effort Default remains pending because the replacement runtime's "
            "served model is unknown"
        )
        return False
    if second_effort_models is None:
        logger.warning(
            "Explicit effort Default remains pending for replacement model %s because "
            "the workspace overlay was not projected or could not be read before spawn",
            second_model,
        )
        return False
    if provider.supports_effort() and second_model in second_effort_models:
        logger.warning(
            "Explicit effort Default start fails because replacement model %s still "
            "had its own effort entry; intent remains pending and no third runtime "
            "was started",
            second_model,
        )
        raise RuntimeError("replacement runtime still reads its served model's effort entry")
    if getattr(provider._client, "resumed", False):
        logger.info(
            "Explicit effort Default removed the served model's workspace entry, but a "
            "session restored with session/load keeps the level it last ran at until its "
            "next fresh session"
        )
    return True


async def start(self: AcpProvider) -> None:
    if self.memory_mode not in {"persistent", "incognito", "temporary"}:
        raise ValueError("Invalid session memory mode")
    placeholder_client = self._client
    self._client.memory_mode = self.memory_mode
    if self.memory_mode != "persistent":
        self._client._resume_session_id = ""
    initial_resume_session_id = self._client._resume_session_id or ""
    initial_history_replay_needed = self._history_replay_needed
    initial_defer_replay_sid_promotion = self._defer_replay_sid_promotion
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
    # Off the loop: on the loop thread the settings lock is single-shot, so
    # a start that met another chat's change_effort mid-write gave up at once
    # and spawned into the level that chat left. Off-loop the startup
    # ceiling (the method's default) is a real wait, short enough for a
    # launch to pay.
    # An explicit Default for a concrete pin replaces that model before spawn
    # and disarms only after that projection succeeds. An unpinned start keeps
    # the ownership guard, records the remaining entries before spawn, and
    # disarms only after the served model runs its Default.
    # Only for the harnesses that READ the overlay (ACP_BACKENDS_EFFORT_OVERLAY),
    # the same set the writers, reads and clears are gated on. A harness that
    # never opens cli.json would otherwise snapshot and rewrite a file it does
    # not read, removing the entry a kiro-cli chat in the same work directory
    # spawns on, and either pay a restart for it (KAS) or, unable to restart,
    # never consume the intent and repeat that removal at every start (claude);
    # its own level arrives live instead.
    finish_unpinned_explicit_default = (
        self._explicit_effort_default_pending
        and self._spawn_effort_model == DEFAULT_MODEL
        and self._client.backend in ACP_BACKENDS_EFFORT_OVERLAY
    )
    fence_mode = self._effort_overlay_fence_mode()

    async def project_overlay(capture_effort_models: bool) -> set[str] | None:
        projection_model_token = _EFFORT_OVERLAY_MODEL_OVERRIDE.set(self._spawn_effort_model)
        try:
            if finish_unpinned_explicit_default:
                with self._fence_explicit_effort_default_rewrite():
                    projected = bool(await _await_overlay_write(self._apply_effort_overlay))
            elif self._explicit_effort_default_pending:
                with self._fence_explicit_effort_default_rewrite():
                    await _await_overlay_write(self._project_explicit_effort_default)
                projected = self._explicit_effort_default_applied
            else:
                projected = bool(await _await_overlay_write(self._apply_effort_overlay))
            if capture_effort_models and projected:
                effort_models = await _await_overlay_write(
                    lambda: _cli_overlay_effort_models(
                        self._client._work_dir, CLI_SETTINGS_LOCK_TIMEOUT_SECS
                    )
                )
                if effort_models is None or isinstance(effort_models, set):
                    return effort_models
                raise TypeError("effort overlay snapshot returned an invalid value")
            return None
        finally:
            _EFFORT_OVERLAY_MODEL_OVERRIDE.reset(projection_model_token)

    async def start_runtime() -> None:
        if self.is_acp_runtime_backend:
            # ── Kiro unified path: AcpRuntime + AcpSessionHandle ──
            # Spawn a runtime, create/resume a session, wrap in
            # AcpSessionProvider. One process hosts parent + all subagent
            # sessions (session sharing).
            await self._start_kiro_runtime()
        else:
            # ── CC path: legacy AcpClient ──
            # Only the runtime arm can fence a remote workspace, so refuse here.
            if await asyncio.to_thread(
                _bound_remotely, getattr(self._client, "_session_key", None)
            ):
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

    async def start_under_held_window(
        acquired: bool, capture_effort_models: bool
    ) -> set[str] | None:
        if not acquired:
            logger.error(
                "ACP Default effort overlay fence wait timed out for work directory %s; "
                "refusing to start before a gateway write can finish",
                self._client._work_dir,
            )
            raise RuntimeError(
                "Default effort overlay fence wait timed out for work directory "
                f"{self._client._work_dir}"
            )
        effort_models = await project_overlay(capture_effort_models)
        await _await_overlay_write(self._apply_tool_search_overlay)
        await start_runtime()
        return effort_models

    async def project_under_admitted_window_and_start(
        capture_effort_models: bool,
    ) -> set[str] | None:
        from kiro_crew.acp.runtime_spawn_entry import handed_down_cold_start_admission

        def admission() -> AsyncContextManager[object]:
            return handed_down_cold_start_admission(
                self.start_priority,
                on_gate_queued=self._on_gate_queued,
                on_gate_acquired=self._on_gate_acquired,
            )

        async with _hold_workspace_effort_overlay_window_under_admission(
            self._client._work_dir,
            admission,
            _WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS,
        ) as acquired:
            return await start_under_held_window(acquired, capture_effort_models)

    async def project_and_start(capture_effort_models: bool) -> set[str] | None:
        if fence_mode == "window":
            # Only kiro-cli reads the overlay (ACP_BACKENDS_EFFORT_OVERLAY), and it
            # always runs on the shared runtime, so a window start is always a
            # runtime start. Neither the window nor the permit is taken first:
            # the start waits for the work directory holding nothing, queues for
            # the permit holding nothing on the fence, and takes the window only
            # with the permit already in hand. So a start that is merely queued
            # holds no window, and a start waiting on a fence holds no
            # gateway-wide admission slot. The permit it ends up with is handed
            # to this start's first spawn.
            return await project_under_admitted_window_and_start(capture_effort_models)
        if fence_mode == "write":
            async with _hold_workspace_effort_overlay_fence(
                self._client._work_dir, "write", CLI_SETTINGS_LOCK_TIMEOUT_SECS
            ) as acquired:
                if acquired:
                    effort_models = await project_overlay(capture_effort_models)
                else:
                    self._log_effort_overlay_projection_skipped("write")
                    effort_models = None
        else:
            effort_models = await project_overlay(capture_effort_models)
        await _await_overlay_write(self._apply_tool_search_overlay)
        await start_runtime()
        return effort_models

    effort_models_at_spawn = await project_and_start(finish_unpinned_explicit_default)
    default_needs_push = False
    if finish_unpinned_explicit_default:
        outcome = await _finish_unpinned_explicit_effort_default(self, effort_models_at_spawn)
        if outcome == "applied":
            self._explicit_effort_default_applied = True
            self._explicit_effort_default_pending = False
        elif outcome == "push":
            default_needs_push = True
        elif outcome == "restart" and await _restart_for_explicit_effort_default(
            self,
            placeholder_client,
            initial_resume_session_id,
            initial_history_replay_needed,
            initial_defer_replay_sid_promotion,
            project_and_start,
        ):
            self._explicit_effort_default_applied = True
            self._explicit_effort_default_pending = False

    if self.is_acp_runtime_backend and self.is_kiro_backend:
        # After the window fence, not under it: every overlay write to this
        # settings folder waits for open windows and gives up when its wait
        # runs out, so these reads must not lengthen the hold.
        await self._note_zero_tool_servers()
    await self._bind_unbound_effort()
    effort_reasserted = await self._reassert_projected_effort()
    if default_needs_push and effort_reasserted:
        self._explicit_effort_default_applied = True
        self._explicit_effort_default_pending = False
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
