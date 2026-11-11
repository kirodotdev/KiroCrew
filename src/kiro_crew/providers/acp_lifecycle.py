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
``_defer_replay_sid_promotion``, ``_explicit_effort_default_projected``,
``_fence_explicit_effort_default_rewrite``, ``_history_replay_needed``,
``_on_gate_acquired``, ``_on_gate_queued``, ``essential_delivery``, ``member_context``,
``memory_mode``, ``start_priority``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, AsyncContextManager, Literal

from kiro_crew.acp.client import DEFAULT_MODEL, AcpClient
from kiro_crew.acp.types import ACP_BACKENDS_EFFORT_OVERLAY, METHOD_SESSION_DELETE
from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_EFFORT_AT_LAUNCH,
    ACP_BACKENDS_SHARED_RUNTIME_ONLY_FOR_CHATS,
)
from kiro_crew.agent_sdk.claude_transcripts import delete_claude_session_files
from kiro_crew.agent_sdk.codex_transcripts import delete_codex_session_files, spawn_env_overrides
from kiro_crew.agent_sdk.opencode_transcripts import delete_opencode_session_rows
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread
from kiro_crew.providers.acp_cli_overlay import (
    _EFFORT_OVERLAY_MODEL_OVERRIDE,
    _WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS,
    _await_overlay_write,
    _clear_cli_overlay_effort,
    _cli_overlay_effort_models,
    _hold_workspace_effort_overlay_fence,
    _hold_workspace_effort_overlay_fence_under_admission,
    _write_cli_overlay,
)
from kiro_crew.providers.acp_model_effort import launch_effort_level
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


async def _finish_unpinned_explicit_effort_default(
    provider: AcpProvider, effort_models_at_spawn: set[str] | None
) -> Literal["applied", "push", "restart", "pending"]:
    """Finish explicit Default for the model startup actually served.

    Used by unpinned starts and by pinned starts whose pin was substituted.
    ``applied`` means no live update is needed, ``push`` means consume only after
    the projected level is pushed live, ``restart`` means a cleared entry needs a
    fresh runtime, and ``pending`` means the intent could not be applied yet.

    A backend that reports the ``auto`` sentinel serves a model no effort level
    can be keyed under, so the Default needs no file change and is applied once
    the file was read before the spawn; a backend that reports no model at all,
    or a file that was not read before the spawn, leaves it pending. Entries the
    projection kept for concrete models stay: they are levels Kiro Crew did not
    write, an explicit Default removes only the served model's entry, and the
    pre-spawn projection warns about them.
    """
    backend_reported_auto = provider._backend_reported_model == DEFAULT_MODEL
    model = provider.served_model
    if not model and not backend_reported_auto:
        logger.warning(
            "Explicit effort Default remains pending because the served model is unknown"
        )
        return "pending"
    if effort_models_at_spawn is None:
        logger.warning(
            "Explicit effort Default remains pending for served model %s because the "
            "workspace overlay was not projected or could not be read before spawn",
            model or DEFAULT_MODEL,
        )
        return "pending"
    if backend_reported_auto:
        return "applied"
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
    Returns False, leaving the Default pending, when the running session cannot
    be restarted, or when the replacement's backend reports no model or its file
    was not read before its spawn. A replacement whose backend reports the
    ``auto`` sentinel reads no entry, so the Default took, as in
    :func:`_finish_unpinned_explicit_effort_default`.
    """
    first_client = provider._client
    if first_client is placeholder_client:
        logger.warning(
            "Explicit effort Default remains pending because the running session "
            "cannot be restarted"
        )
        return False
    if getattr(first_client, "_shared_runtime", False):
        # A shared process read the workspace file when it spawned, and a
        # replacement session joins that same process, so a restart cannot
        # apply the Default; its shutdown would only evict this session.
        logger.warning(
            "Explicit effort Default remains pending because the session runs on a "
            "shared chat runtime"
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

    backend_reported_auto = provider._backend_reported_model == DEFAULT_MODEL
    second_model = provider.served_model
    if not second_model and not backend_reported_auto:
        logger.warning(
            "Explicit effort Default remains pending because the replacement runtime's "
            "served model is unknown"
        )
        return False
    if second_effort_models is None:
        logger.warning(
            "Explicit effort Default remains pending for replacement model %s because "
            "the workspace overlay was not projected or could not be read before spawn",
            second_model or DEFAULT_MODEL,
        )
        return False
    # A replacement whose backend reports ``auto`` serves a model no effort level
    # can be keyed under, so it read no entry and the cleared Default took. Entries
    # kept for other models are levels Kiro Crew did not write, and they stay.
    if (
        not backend_reported_auto
        and provider.supports_effort()
        and second_model in second_effort_models
    ):
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
    # Apply the overlay on every (re)start, the first spawn included, to cover
    # resume / model swap.
    # (no-op for claude backend — that path applies effort live below.)
    # Off the loop: on the loop thread the settings lock is single-shot, so
    # a start that met another chat's change_effort mid-write gave up at once
    # and spawned into the level that chat left. Off-loop the startup
    # ceiling (the method's default) is a real wait, short enough for a
    # launch to pay.
    # An explicit Default records the remaining entries before spawn and
    # disarms only after startup confirms the pin was served or the served model
    # runs its Default. A concrete pin is still replaced before spawn, while an
    # unpinned start keeps the ownership guard.
    # Only for the harnesses that READ the overlay (ACP_BACKENDS_EFFORT_OVERLAY),
    # the same set the writers, reads and clears are gated on. A harness that
    # never opens cli.json would otherwise snapshot and rewrite a file it does
    # not read, removing the entry a kiro-cli chat in the same work directory
    # spawns on, and either pay a restart for it (KAS) or, unable to restart,
    # never consume the intent and repeat that removal at every start (claude);
    # its own level arrives live instead.
    explicit_default_model = self._spawn_effort_model
    finish_explicit_default_after_start = (
        self._explicit_effort_default_pending
        and self._client.backend in ACP_BACKENDS_EFFORT_OVERLAY
    )
    finish_unpinned_explicit_default = (
        finish_explicit_default_after_start and explicit_default_model == DEFAULT_MODEL
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
                projected = (
                    self._explicit_effort_default_projected or self._explicit_effort_default_applied
                )
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

    def admission() -> AsyncContextManager[object]:
        from kiro_crew.acp.runtime_spawn_entry import handed_down_cold_start_admission

        return handed_down_cold_start_admission(
            self.start_priority,
            on_gate_queued=self._on_gate_queued,
            on_gate_acquired=self._on_gate_acquired,
        )

    async def project_under_admitted_window_and_start(
        capture_effort_models: bool,
    ) -> set[str] | None:
        async with _hold_workspace_effort_overlay_fence_under_admission(
            self._client._work_dir,
            "window",
            admission,
            _WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS,
        ) as acquired:
            return await start_under_held_window(acquired, capture_effort_models)

    async def project_under_admitted_write_and_start() -> set[str] | None:
        # The snapshot an explicit Default is judged by must still be the file
        # kiro-cli reads, so no peer may project its own level between the two.
        async with _hold_workspace_effort_overlay_fence_under_admission(
            self._client._work_dir, "write", admission, CLI_SETTINGS_LOCK_TIMEOUT_SECS
        ) as acquired:
            if acquired:
                effort_models = await project_overlay(True)
                await _await_overlay_write(self._apply_tool_search_overlay)
                await start_runtime()
                return effort_models
        self._log_effort_overlay_projection_skipped("write")
        await _await_overlay_write(self._apply_tool_search_overlay)
        await start_runtime()
        return None

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
        if fence_mode == "write" and capture_effort_models:
            return await project_under_admitted_write_and_start()
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

    effort_models_at_spawn = await project_and_start(finish_explicit_default_after_start)
    # A Default that resolves to a level is spent only once the post-spawn push
    # lands: a peer can project its own level between this start's projection and
    # kiro-cli's read, so the file alone does not prove what the session runs.
    consume_default_after_reassert = False
    if finish_explicit_default_after_start:
        served_pinned_model = (
            explicit_default_model != DEFAULT_MODEL
            and effort_models_at_spawn is not None
            and self.served_model == explicit_default_model
        )
        if served_pinned_model:
            consume_default_after_reassert = True
        else:
            outcome = await _finish_unpinned_explicit_effort_default(self, effort_models_at_spawn)
            if outcome in ("applied", "push"):
                consume_default_after_reassert = True
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
    if consume_default_after_reassert and effort_reasserted:
        self._explicit_effort_default_applied = True
        self._explicit_effort_default_pending = False
    await self._apply_initial_effort()


async def shutdown(self: AcpProvider) -> None:
    session_id = self.session_id
    try:
        if self.memory_mode != "persistent" and session_id and self.is_pi_backend:
            await _delete_pi_session(self._client, session_id)
    finally:
        try:
            await self._client.shutdown()
        finally:
            try:
                if self.is_kas_backend:
                    # The ``_client.shutdown()`` above already ended a non-persistent
                    # session with ``_kiro/session/delete`` (the handle's ``destroy``),
                    # so a second delete would only leave a ``released_sessions``
                    # entry for a record the engine does not hold.
                    pass
                elif self.memory_mode != "persistent" and session_id:
                    await self.cleanup_session(session_id)
            finally:
                # A cancel delivered during the transcript delete must not skip the reclaim.
                if self._disposable_work_dir:
                    await self._reclaim_work_dir()


async def _delete_pi_session(client: AcpClient, session_id: str) -> None:
    """Ask pi to delete its record of a disposable session, while it still runs.

    pi writes the session to a JSONL file whose folder its own settings choose, and
    pi-acp keeps a map from the id to that file. pi-acp's ``session/delete`` removes
    both, so pi's own answer is used rather than a second guess at its paths. Sent
    only when the handshake advertised ``sessionCapabilities.delete`` and the
    process is alive. The answer is awaited for ``_TERMINATE_TIMEOUT``, also while
    the gateway shuts down; a failure is logged and the shutdown goes on.

    shortcut: a pi process that died before the shutdown keeps the file, since
    nothing is left to ask. Upgrade by reading pi-acp's session map from disk.
    """
    from kiro_crew.acp.runtime import _TERMINATE_TIMEOUT

    init = client.initialize_result
    capabilities = init.get("agentCapabilities") if isinstance(init, dict) else None
    session_caps = (
        capabilities.get("sessionCapabilities") if isinstance(capabilities, dict) else None
    )
    if not isinstance(session_caps, dict) or not isinstance(session_caps.get("delete"), dict):
        logger.warning("pi session cleanup: the adapter offers no session/delete")
        return
    if not client._is_process_alive():
        logger.warning("pi session cleanup: the process ended before the delete")
        return
    try:
        req_id = await client._send_request(METHOD_SESSION_DELETE, {"sessionId": session_id})
        await _await_answer(client, req_id, _TERMINATE_TIMEOUT)
    except Exception:
        logger.warning("pi session cleanup: session/delete failed", exc_info=True)


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
                    logger.warning("pi session cleanup: session/delete was refused")
                return
        logger.warning("pi session cleanup: session/delete got no answer in time")
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
    Each is deleted off the loop, to completion even when cancelled. A pi session's
    record went before its process ended (``_delete_pi_session``). A KAS session's
    record is in the engine's own store, so the call goes to the
    ``AcpSessionProvider`` the runtime start put in ``_client``; its
    ``delete_native_record`` sends ``_kiro/session/delete``, or schedules it in
    ``released_sessions`` when this provider's runtime is already shut down.
    """
    if not session_id:
        return
    if self.is_kas_backend:
        from kiro_crew.acp.session_provider import AcpSessionProvider

        if isinstance(self._client, AcpSessionProvider):
            await self._client.cleanup_session(session_id)
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
