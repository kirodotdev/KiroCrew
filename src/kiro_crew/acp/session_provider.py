"""AcpSessionProvider — adapts AcpSessionHandle to the LLMProvider interface.

Used by Phase 3 session sharing AND the unified kiro-path provider. When
the kiro backend is active, AcpProvider delegates to an AcpSessionProvider
(backed by AcpRuntime + AcpSessionHandle) instead of AcpClient. This gives:
- Single-reader demux: parent session + N subagents on one process
- LLMProvider interface: SubagentManager, chat_runner, etc. work unchanged
- AcpClient-compatible API: AcpProvider can call the same methods regardless
  of backend (kiro → AcpSessionProvider, CC → AcpClient)

The adapter exposes the SAME public interface as AcpClient so AcpProvider
doesn't need to branch on every method call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from kiro_crew.acp.client import (
    _NOT_LOGGED_IN_MESSAGE,
    DEFAULT_MODEL,
    AcpAuthRequired,
    AcpError,
    AcpModelUnavailable,
    AcpProcessDied,
    advertised_model_ids,
    model_is_unusable,
)
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeDead, AcpRuntimeError, AcpSessionHandle
from kiro_crew.acp.session_handle import WatchdogSettings
from kiro_crew.acp.types import (
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_KIRO_IDENTITY_STORE,
    STOP_REASON_END_TURN,
)
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS
from kiro_crew.mcp_gateway.claim import schedule_claim
from kiro_crew.providers.base import CancelOutcome, LLMEvent, LLMProvider

logger = logging.getLogger(__name__)


class AcpSessionProvider(LLMProvider):
    """LLMProvider adapter over an AcpSessionHandle on a shared runtime.

    Exposes the same API surface as AcpClient so AcpProvider can treat them
    interchangeably. For methods that only apply to the Claude backend
    (permission modes), this returns safe no-op values.

    Lifecycle:
    - For subagents: created by SubagentManager, shutdown() destroys session
      but does NOT kill the shared runtime.
    - For parent sessions (unified path): created by AcpProvider, shutdown()
      kills the runtime (entire session group dies together).
    """

    def __init__(
        self,
        handle: AcpSessionHandle,
        runtime: AcpRuntime,
        *,
        owns_runtime: bool = False,
    ) -> None:
        self._handle = handle
        self._runtime = runtime
        # When True, shutdown() kills the runtime (parent session owns it).
        # When False, shutdown() only destroys the session handle (subagent).
        self._owns_runtime = owns_runtime
        self._resumed_flag: bool = False
        self._resume_session_id: str = ""
        # Warm-pool correlation keys (parity with AcpClient); set by rekey().
        self._session_key: str = ""
        self._channel_id: str | None = None

    # ── LLMProvider interface ──

    async def start(self) -> None:
        """No-op — the session handle is already initialized."""

    async def new_conversation(self) -> None:
        """Reset to a fresh conversation on the SAME warm runtime (kiro path).

        Parity with ``AcpClient.new_conversation`` — the cheap clean-slate reuse
        primitive a warm worker pool relies on: create a brand-new ``session/new``
        on the *already-running* kiro-cli process (paying only session/new + the
        MCP-init drain), then destroy the old session so its context/transcript
        and MCP children are freed on the shared process. This SKIPS the expensive
        parts of a cold start — subprocess spawn + the ACP ``initialize`` handshake
        — which is exactly what makes pooled reuse faster than teardown+respawn.

        If the runtime is dead there is nothing warm to reuse; the caller (pool)
        detects that via ``is_alive()``/``is_process_alive()`` and replaces the
        worker, so this raises rather than silently respawning a new process here
        (the runtime is owned by this provider's lifecycle, not recreated in place).
        """
        if not self._runtime.is_alive():
            raise AcpProcessDied("Runtime is not alive — cannot start a new conversation")
        old = self._handle
        # Create the fresh session BEFORE destroying the old one so a failure
        # leaves the provider still pointing at a usable handle (no window where
        # self._handle references a terminated session).
        new_handle = await self._runtime.create_session(
            cwd=self._runtime._work_dir,
            agent=self._runtime._agent or None,
        )
        # Re-apply the configured non-default model to the fresh session. A new
        # session/new reverts to the agent-config default model, so a warm worker
        # configured with a non-default model (via the cold-start set_model in
        # AcpProvider._start_kiro_runtime_impl, recorded on the old handle) would
        # silently run every reused task on the wrong model without this. Mirrors
        # that cold-start handshake: skip the "auto" sentinel (let kiro pick).
        #
        # If the re-apply FAILS we must NOT commit the fresh handle — a
        # wrong-model session would silently run every subsequent pooled step on
        # the default model. Tear the fresh session down and raise so the caller
        # (WorkerPool) performs its hard-reset fallback and the old, correctly
        # configured handle is not destroyed below.
        prior_model = getattr(old, "model", "")
        if isinstance(prior_model, str) and prior_model and prior_model != DEFAULT_MODEL:
            try:
                await new_handle.set_model(prior_model)
            except Exception as exc:
                logger.warning(
                    "new_conversation: failed to re-apply model %s to fresh session; "
                    "tearing it down and signalling reset",
                    prior_model,
                    exc_info=True,
                )
                try:
                    await new_handle.destroy()
                except Exception:
                    logger.debug(
                        "new_conversation: fresh-session teardown after model "
                        "re-apply failure also failed",
                        exc_info=True,
                    )
                raise AcpError(f"failed to re-apply model {prior_model} to fresh session") from exc
        self._handle = new_handle
        # Best-effort teardown of the old session on the shared process so its
        # context doesn't linger (RSS growth). Never let cleanup mask success.
        try:
            await old.destroy()
        except Exception:
            logger.debug("new_conversation: old session destroy failed", exc_info=True)

    def set_keep_transcript(self, value: bool) -> None:
        """Mark the underlying session handle to keep (or delete) its
        transcript files at destroy(). Set True by SubagentManager before
        teardown so the transcript survives as spawn_continue's resume
        material; the tombstone pruner / conversation TTL sweep owns its
        eventual deletion."""
        try:
            self._handle.keep_transcript = value
        except Exception:  # pragma: no cover - handle types without the attr
            logger.debug("set_keep_transcript: handle rejected attribute", exc_info=True)

    @property
    def child_fidelity_aware(self) -> bool:
        """See AcpSessionHandle.child_fidelity_aware."""
        return getattr(self._handle, "child_fidelity_aware", False)

    @child_fidelity_aware.setter
    def child_fidelity_aware(self, value: bool) -> None:
        if hasattr(self._handle, "child_fidelity_aware"):
            self._handle.child_fidelity_aware = value

    async def shutdown(self) -> None:
        """Destroy the session and optionally kill the runtime.

        - Parent sessions (owns_runtime=True): kill the entire runtime.
        - Subagent sessions (owns_runtime=False): cancel any in-flight turn,
          then destroy the handle only.
        """
        if self._owns_runtime:
            try:
                await self._runtime.kill(expected=True)  # deliberate session teardown
            except Exception:
                logger.debug("AcpSessionProvider.shutdown: runtime kill failed", exc_info=True)
        else:
            # Session-sharing subagent: the runtime is SHARED with co-tenant
            # sessions (parent + sibling subagents), so we must NOT kill it.
            # But destroy() only unregisters this session's queue — it does not
            # tell kiro-cli to stop an in-flight prompt. Reaping a subagent
            # mid-turn (timeout / user cancel) would otherwise leave the abandoned
            # prompt running on the shared process: it keeps burning credits, its
            # frames get dropped (unknown-session), and it can wedge the next
            # prompt on that sessionId with "already in progress". So cancel the
            # session's turn first (best-effort, bounded so an unresponsive
            # runtime can't turn shutdown into a hang), then destroy the handle.
            # The destroy is in a `finally` because the cancel above can be
            # left through a door `except Exception` does not cover:
            # `asyncio.CancelledError` is a `BaseException`. That is not a
            # theoretical exit — the session-restart path runs
            # `asyncio.wait_for(p.shutdown(), timeout=_SHUTDOWN_TIMEOUT_SECS)`
            # inside an `asyncio.gather`, so both a shutdown that outruns the
            # budget and a cancelled restart task deliver a cancellation into
            # this coroutine, at whatever await it is sitting on.
            #
            # Sequentially, that skipped the destroy entirely — and the destroy
            # is where this arm's two invariants live: `terminate_session`
            # evicts the session from the SHARED kiro-cli process (it is the
            # only RSS reclaim on a runtime nothing here is allowed to kill),
            # and the transcript unlink is the only thing that removes
            # `~/.kiro/sessions/cli/{sid}.json(+.jsonl)`, as the comment below
            # says. Nothing retries: every caller drops the provider afterwards.
            try:
                if self._handle.is_turn_active:
                    try:
                        await asyncio.wait_for(self._handle.cancel(), timeout=5.0)
                    except Exception:
                        logger.debug(
                            "AcpSessionProvider.shutdown: session cancel failed", exc_info=True
                        )
            finally:
                try:
                    await self._handle.destroy()
                except Exception:
                    logger.debug("AcpSessionProvider.shutdown: destroy failed", exc_info=True)
            # destroy() deletes the shared-subagent session transcript
            # (~/.kiro/sessions/cli/{sid}.json+.jsonl); no separate cleanup call
            # needed. cleanup_session() below remains for the LLMProvider API.

    async def cleanup_session(self, session_id: str = "") -> None:
        """Delete this session's kiro-cli transcript files (.json + .jsonl).

        Overrides the no-op LLMProvider.cleanup_session so shared-subagent
        sessions don't leak transcripts on the shared runtime. Mirrors
        AcpProvider.cleanup_session.
        """
        sid = session_id or getattr(self._handle, "session_id", "") or ""
        if not sid:
            return
        sessions_dir = kiro_sessions_dir().resolve()
        for suffix in (".json", ".jsonl"):
            target = (sessions_dir / f"{sid}{suffix}").resolve()
            # Guard against a crafted sessionId escaping the sessions dir.
            if target.parent != sessions_dir:
                logger.error("cleanup_session: path traversal blocked for %s", target)
                return
            try:
                target.unlink(missing_ok=True)
            except OSError:
                logger.warning("cleanup_session: failed to delete %s", target, exc_info=True)

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        """Send a prompt and yield LLMEvent objects until the turn completes."""
        try:
            async for event in self._handle.prompt(message):
                yield event
        except AcpRuntimeDead as exc:
            # Translate the shared-runtime death into the exception types
            # chat_runner handles (parity with AcpClient): auth-expiry ->
            # AcpAuthRequired (non-retryable login prompt); otherwise
            # AcpProcessDied. Without this, AcpRuntimeDead (an AcpRuntimeError,
            # NOT an AcpError) escapes both the AcpProcessDied and AcpError
            # handlers and surfaces as an unhandled crash.
            if self._runtime.saw_not_logged_in():
                raise AcpAuthRequired(_NOT_LOGGED_IN_MESSAGE) from exc
            raise AcpProcessDied(str(exc)) from exc
        except AcpRuntimeError as exc:
            # Base AcpRuntimeError (e.g. prompt()'s "turn already active"
            # concurrent-prompt guard) is also OUTSIDE the AcpError hierarchy;
            # keep the provider surface within AcpError so chat_runner catches
            # it instead of hitting its generic `except Exception`.
            raise AcpError(str(exc)) from exc

    async def steer(self, message: str) -> bool:
        """Forward a mid-turn steer to the session handle (kiro _session/steer)."""
        return await self._guarded(self._handle.steer(message))

    @property
    def last_steer_monotonic(self) -> float:
        """Monotonic time of the handle's last steer (0.0 if never steered)."""
        return float(getattr(self._handle, "last_steer_monotonic", 0.0) or 0.0)

    @property
    def supports_steer(self) -> bool:
        """True when the backing handle supports mid-turn steer (kiro-cli)."""
        return self._handle.supports_steer

    async def stream_command(self, command: str) -> AsyncIterator[LLMEvent]:
        """Execute a slash command natively via ``_kiro.dev/commands/execute``.

        Routes through AcpSessionHandle.stream_command so kiro-cli executes the
        command itself and returns its structured output deterministically —
        no LLM round-trip. (Previously delegated to stream(), which sent the
        command through session/prompt: a full model turn that summarized the
        output instead of returning it.) The handle keeps /compact, /help, and
        non-kiro backends (KAS) on the prompt transport — see its docstring.
        Same exception translation as stream(): everything leaving this
        surface stays within AcpError.
        """
        try:
            async for event in self._handle.stream_command(command):
                yield event
        except AcpRuntimeDead as exc:
            raise self._translate_dead(exc) from exc
        except AcpRuntimeError as exc:
            raise AcpError(str(exc)) from exc

    def _translate_dead(self, exc: AcpRuntimeDead) -> AcpProcessDied | AcpAuthRequired:
        """Map a shared-runtime death (AcpRuntimeDead — an AcpRuntimeError OUTSIDE
        the AcpError hierarchy) to the AcpError-hierarchy exception every caller
        expects: AcpAuthRequired on auth-expiry, else AcpProcessDied. Keeps the
        ENTIRE AcpSessionProvider surface within AcpError (+ asyncio.TimeoutError)
        so a runtime.send_* failure never escapes to a caller that only catches
        AcpError (e.g. chat_runner) and lands on its generic `except Exception`
        (raw error card, no retry/reset). Mirrors stream()'s translation."""
        if self._runtime.saw_not_logged_in():
            return AcpAuthRequired(_NOT_LOGGED_IN_MESSAGE)
        return AcpProcessDied(str(exc))

    async def _guarded(self, awaitable: Any) -> Any:
        """Await a runtime-touching handle coroutine, translating AcpRuntimeDead
        into the AcpError hierarchy (see _translate_dead), and any other base
        AcpRuntimeError into a generic AcpError so nothing outside AcpError
        escapes the provider surface."""
        try:
            return await awaitable
        except AcpRuntimeDead as exc:
            raise self._translate_dead(exc) from exc
        except AcpRuntimeError as exc:
            raise AcpError(str(exc)) from exc

    async def approve_tool(
        self, request_id: str | int, option_id: str | None = None, *, always: bool = False
    ) -> None:
        """Approve a pending tool permission request. Accepts an explicit
        option_id (signature parity with AcpClient.approve_tool); falls back to
        allow_always/allow_once from `always`."""
        resolved = option_id or ("allow_always" if always else "allow_once")
        await self._guarded(self._handle.approve_tool(request_id, option_id=resolved))

    async def reject_tool(self, request_id: str | int) -> None:
        """Reject a pending tool permission request."""
        await self._guarded(self._handle.reject_tool(request_id))

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        """Cancel the current turn."""
        if not self._handle.is_turn_active:
            return "no_turn"
        try:
            await self._handle.cancel(grace_secs=wait_ack_timeout)
            if wait_ack_timeout > 0:
                done = await self._handle.wait_turn_done(timeout=wait_ack_timeout)
                return "acked" if done else "timeout"
            return "acked"
        except AcpRuntimeDead:
            return "error"
        except Exception:
            logger.warning("AcpSessionProvider.cancel failed", exc_info=True)
            return "error"

    def context_usage_pct(self) -> float:
        """Return last known context usage percentage."""
        return self._handle.last_prompt_stats.context_pct

    def context_usage_unknown(self) -> bool:
        """True when the 0% reading is a post-compaction unknown, not an empty
        transcript."""
        return self._handle.last_prompt_stats.context_pct_unknown

    def context_window_tokens(self) -> int:
        """Return the context window size in tokens."""
        return self._handle.last_prompt_stats.context_window_tokens

    def context_used_tokens(self) -> int:
        """Return tokens used in the current context."""
        return self._handle.last_prompt_stats.context_used_tokens

    def billing_stats(self) -> object | None:
        """Live per-turn billing stats (public — see LLMProvider).

        The same object ``last_prompt_stats`` exposes and the context accessors
        above read; declaring it here is what makes the accounting path's read a
        stated capability instead of a search for that attribute name. The handle
        installs a fresh object as each turn begins, which is the identity the
        accounting path compares against.
        """
        return self._handle.last_prompt_stats

    @property
    def session_id(self) -> str:
        """The ACP session ID."""
        return self._handle.session_id

    def is_alive(self) -> bool:
        """True if the underlying runtime is still alive."""
        return self._runtime.is_alive()

    def is_process_alive(self) -> bool:
        """True if the runtime process exists and has not exited."""
        return self._runtime.is_alive()

    @property
    def process_instance(self) -> str:
        """Per-spawn identity of the shared runtime's current process (see base).

        A direct read on purpose: a `getattr` hedge would convert a future
        wiring break into "no banner is ever live", indistinguishable from
        correct expiry.
        """
        return self._runtime.process_instance

    @property
    def exit_code(self) -> int | None:
        """Runtime process exit code (None if still running)."""
        proc = getattr(self._runtime, "_process", None)
        return proc.returncode if proc else None

    def touch_activity(self) -> None:
        """Refresh activity timestamp on the runtime."""
        self._runtime._last_activity = time.monotonic()

    def rekey(
        self,
        session_key: str,
        channel_id: str | None = None,
        crew_agent: str = "",
        watchdog: WatchdogSettings | None = None,
    ) -> None:
        """Re-key for a different session on warm-pool claim (parity with
        AcpClient.rekey). session.py:1309 calls provider.client.rekey(...); when
        the pooled provider is kiro-shared, provider.client is THIS class, so a
        missing rekey() would AttributeError on claim. Stores the correlation
        keys and refreshes runtime activity so the just-claimed process is not
        idle-reaped.

        ``crew_agent`` is the claiming session's canonical crew identity: the
        pooled runtime was spawned before any crew claimed it, so both the
        runtime default (future sessions, e.g. new_conversation) and the live
        handle's watchdog snapshot are rebound here — the identity travels
        with the session, not the pool key. Empty means "no crew" and rebinds
        to the globals, so a recycled runtime never carries a previous crew's
        windows. ``watchdog`` is the pre-resolved snapshot from the async
        caller (resolved off-loop); None makes rebind load it synchronously."""
        self._session_key = session_key
        self._channel_id = channel_id
        self._runtime._crew_agent = crew_agent
        self._handle.rebind_watchdog(crew_agent, settings=watchdog)
        self._runtime._last_activity = time.monotonic()
        # Parity with AcpClient.rekey: the handle's prompt stats describe the
        # session this runtime served BEFORE the handoff; leaking them lets
        # check_context_usage() compact the new, empty session (#2932).
        self._handle.last_prompt_stats.reset_context_state()
        # Claim-push: re-target every MCP stub connection under the shared
        # runtime's PID to the claiming session (see AcpClient.rekey for the
        # rationale). Fire-and-forget; no-ops without a gateway socket.
        schedule_claim(
            self._runtime._mcp_gateway_socket,
            self._runtime.pid,
            session_key,
            channel_id,
        )

    @property
    def _agent(self) -> str:
        """Agent/mode name from the backing runtime (parity with AcpClient._agent).
        Read by session.py session-info introspection via provider.client._agent;
        a missing attribute AttributeErrors that (unguarded) code path whenever a
        kiro-shared session is listed."""
        return self._runtime._agent

    # ── AcpClient-compatible API ──
    # These methods mirror AcpClient's public interface so AcpProvider can
    # call them without branching on backend type.

    async def ensure_ready(self) -> None:
        """Verify the runtime is alive. No-op equivalent of AcpClient.ensure_ready().
        Raises within the AcpError hierarchy (AcpProcessDied / AcpAuthRequired) so
        callers that catch AcpError see it — NOT the raw AcpRuntimeError."""
        if not self._runtime.is_alive():
            raise self._translate_dead(AcpRuntimeDead("Runtime is not alive"))

    @property
    def backend(self) -> str:
        """ACP backend identifier, delegated to the runtime that serves it.

        Not a constant: this provider fronts whichever backend ``AcpRuntime``
        spawned, and it replaces the placeholder ``AcpClient`` on
        ``AcpProvider._client`` once startup completes — so it is the only
        remaining place a consumer can read the backend back off a started
        provider. Reporting kiro unconditionally would persist every KAS
        session under the kiro label.
        """
        return self._runtime.acp_backend

    @property
    def manual_compact_unsupported_backend(self) -> str | None:
        """Backend id when a manual ``/compact`` cannot be served, else ``None``.

        Same ``ACP_BACKENDS_COMPACT`` membership answer as
        ``AcpProvider.manual_compact_unsupported_backend`` (#7800), for the
        bare shared-subagent shape that is handed out without the
        ``AcpProvider`` wrapper.
        """
        backend = self.backend
        if not isinstance(backend, str) or backend in ACP_BACKENDS_COMPACT:
            return None
        return backend

    @property
    def uses_kiro_identity_store(self) -> bool:
        """True when this provider's child signs in from kiro-cli's own store.

        Membership in ``ACP_BACKENDS_KIRO_IDENTITY_STORE`` (harness-parity
        H5/H14), read off the runtime's backend for the same reason
        :attr:`backend` is: this provider fronts whichever backend the runtime
        spawned.
        """
        return self._runtime.acp_backend in ACP_BACKENDS_KIRO_IDENTITY_STORE

    def has_active_turn(self) -> bool:
        """True if a prompt turn is currently in progress.

        A METHOD (not a property) to match AcpClient.has_active_turn — every
        caller (AcpProvider.cancel, chat_handlers) invokes it with parens, so a
        @property here raised `TypeError: 'bool' object is not callable` on the
        kiro path.
        """
        return self._handle.is_turn_active

    def has_unfinished_turn(self) -> bool:
        """True if the native turn has not reached its done boundary —
        INDEPENDENT of cancel state (unlike :meth:`has_active_turn`).

        Parity with ``AcpClient.has_unfinished_turn``: reports a
        cancelled-but-not-yet-acked turn as still unfinished so the shutdown
        drain waits for its ack before the shared runtime is killed. Delegates
        to the handle's ``has_unfinished_turn`` (which omits the cancelled
        exclusion that ``is_turn_active`` applies).
        """
        return self._handle.has_unfinished_turn

    async def wait_turn_done(self, timeout: float = 30.0) -> str:
        """Wait for the current turn to finish; return its stop_reason (str) or
        raise asyncio.TimeoutError.

        MUST match AcpClient.wait_turn_done's contract (str, not bool): the
        shared AcpProvider.cancel() checks `reason in (CANCELLED, END_TURN)` and
        catches asyncio.TimeoutError. Returning the handle's bool made that
        check always False → cancel always reported "timeout" → a spurious hard
        kill of the SHARED runtime (killing co-tenant sessions).
        """
        done = await self._handle.wait_turn_done(timeout=timeout)
        if not done:
            raise asyncio.TimeoutError()
        # Synthetic-terminal paths (tool-interrupted / unresponsive-cancel /
        # stale) set _turn_done WITHOUT a stopReason, leaving _last_stop_reason
        # "". Returning "" makes AcpProvider.cancel()'s `reason in (CANCELLED,
        # END_TURN)` check False → it reports a timeout → spurious HARD KILL of
        # the SHARED runtime (killing co-tenant sessions). Treat a done-but-empty
        # turn as a benign END_TURN so cancel() completes cleanly.
        return self._handle._last_stop_reason or STOP_REASON_END_TURN

    def is_responsive(self, stale_threshold: float = 600.0) -> bool:
        """True if runtime is alive AND has had activity within threshold."""
        return self._handle.is_responsive(stale_threshold)

    async def cancel_session(self, grace_secs: float = 0.0) -> None:
        """Cancel the current session turn (alias for cancel()).

        Accepts grace_secs for signature parity with AcpClient.cancel_session —
        AcpProvider.cancel() calls this with grace_secs=wait_ack_timeout, so
        omitting it raised TypeError on every kiro-path cancel.

        MUST NOT raise, matching AcpClient.cancel_session's swallow-all contract:
        AcpProvider.cancel() only catches AcpError, so a raised AcpRuntimeDead
        (from handle.cancel() -> runtime.send_notification() when the runtime
        died mid-turn) would escape to session.stop_turn() and crash the stop
        handler (500). handle.cancel() records _cancelled BEFORE the
        notification, so the turn still terminates via the grace path / queue
        poison even when the notification fails.
        """
        try:
            await self._handle.cancel(grace_secs=grace_secs)
        except Exception:
            logger.debug(
                "AcpSessionProvider.cancel_session: cancel notification failed "
                "(runtime may be dead); cancel state already recorded",
                exc_info=True,
            )

    # ── Commands & Config ──

    async def send_command(self, command: str, args: dict[str, Any] | None = None) -> str:
        """Execute a kiro slash command. Returns response text."""
        return await self._guarded(self._handle.send_command(command, args))

    async def set_config_option(self, config_id: str, value: str) -> None:
        """Set a session config option (e.g. effort level)."""
        await self._guarded(self._handle.set_config_option(config_id, value))

    async def compact(self, context: str = "") -> None:
        """Trigger context compaction."""
        await self._guarded(self._handle.compact(context))

    async def wait_for_compaction(
        self, timeout: float = COMPACT_WAIT_TIMEOUT_SECS
    ) -> dict[str, str]:
        """Wait for compaction completed/failed event."""
        return await self._guarded(self._handle.wait_for_compaction(timeout))

    async def _drain_post_compaction_metadata(self) -> None:
        """Grace-drain for kiro's post-compaction metadata (delegates to the
        handle). Called by ``AcpProvider.wait_for_compaction`` on its cached
        mid-turn result so the shared-runtime path reports real numbers."""
        await self._guarded(self._handle._drain_post_compaction_metadata())

    # ── Model & Effort ──

    async def set_model(self, model_id: str) -> None:
        """Switch the active model.

        An explicit pick the account cannot run is REFUSED here rather than
        silently downgraded (the opposite of the spawn path in ``providers.acp``,
        which withholds an inherited default): the user asked for this exact
        model, so reporting success while running another one would be a lie.
        Raises :class:`AcpModelUnavailable` so the caller surfaces it as a user
        error instead of recovering with a session reset — a reset here would
        destroy the live conversation and still land on a different model.

        A refusal is never issued on the session-init snapshot alone. That
        snapshot is one answer, captured at one instant, and a lookup racing a
        token refresh can answer with the default tier — freezing a
        false "not entitled" verdict into the session for its whole life. So a
        would-be refusal first revalidates against a fresh backend probe
        (:meth:`AcpSessionHandle.refresh_available_models`) and only stands if
        the fresh answer ALSO lacks the model. A failed probe keeps the stale
        verdict (fail-safe: no evidence, no entitlement granted).
        """
        advertised = advertised_model_ids(self._handle.available_models)
        if model_is_unusable(model_id, advertised):
            fresh = advertised_model_ids(
                await self._guarded(self._handle.refresh_available_models())
            )
            if model_is_unusable(model_id, fresh or advertised):
                raise AcpModelUnavailable(model_id, fresh or advertised)
        await self._guarded(self._handle.set_model(model_id))

    async def set_mode(self, agent_name: str) -> None:
        """Switch the active agent via session/set_mode."""
        await self._guarded(self._handle.set_mode(agent_name))

    # ── State (mirrors AcpClient properties/attributes) ──

    @property
    def _model(self) -> str:
        """Current model name (AcpClient-compatible attribute)."""
        return self._handle.model

    @_model.setter
    def _model(self, value: str) -> None:
        """Set model name (AcpClient-compatible attribute)."""
        self._handle._model = value

    @property
    def served_model(self) -> str:
        """Backend-resolved model id serving this session (``""`` until known).

        Public delegation to :attr:`AcpSessionHandle.served_model` — covers
        both the explicit ``set_model`` path and the backend-default path
        (``currentModelId``), unlike ``_model`` which only reflects the
        former.
        """
        return self._handle.served_model

    @property
    def agent_version(self) -> str:
        """The version the backing process runs — see :attr:`AcpSessionHandle.agent_version`."""
        return self._handle.agent_version

    @property
    def _session_id(self) -> str:
        """Session ID (AcpClient-compatible attribute)."""
        return self._handle.session_id

    @property
    def _work_dir(self) -> Path:
        """Working directory (AcpClient-compatible attribute)."""
        return self._runtime._work_dir

    @property
    def cwd(self) -> str:
        """The directory THIS session is bound to, not the runtime's.

        Overrides the ``LLMProvider`` default ("") so reuse validation reads the real
        path through the public capability rather than probing a private attribute.
        Reads it off the HANDLE: a shared runtime carries sessions opened against
        different projects, so answering with the runtime's own directory would report a
        workspace this session never bound, and reuse validation would evict a live
        session -- losing its conversation -- for failing to be somewhere it never was.
        Falls back to the runtime for a handle predating the record, which is the
        single-session case where the two agree anyway.
        """
        return str(getattr(self._handle, "_bound_cwd", "") or self._work_dir)

    @property
    def _permission_mode(self) -> str:
        """Permission mode — always empty for kiro (no CC permission modes)."""
        return ""

    @_permission_mode.setter
    def _permission_mode(self, value: str) -> None:
        """No-op setter — kiro has no permission modes."""

    @property
    def acp_config_options(self) -> list[dict[str, Any]]:
        """Config options reported by ACP."""
        return self._handle.config_options

    def available_models(self) -> list[dict[str, str]]:
        """Models advertised by the backend."""
        return self._handle.available_models

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        """Drain OAuth requests captured while the shared session initialized."""
        return self._handle.pop_pending_oauth_requests()

    def mcp_session_report(self) -> McpSessionReport:
        """This session's MCP registration report."""
        return self._handle.mcp_session_report()

    def get_valid_effort_levels(self) -> list[str]:
        """Valid effort levels from config options."""
        return self._handle.get_valid_effort_levels()

    def supports_config_option(self, config_id: str) -> bool:
        """Whether the session advertised a config option with this id."""
        return self._handle.supports_config_option(config_id)

    def supports_permission_mode(self, mode: str) -> bool:
        """Whether the session supports a CC permission mode. Always False for kiro."""
        return False

    async def set_permission_mode(self, mode: str) -> None:
        """No-op — kiro has no Claude backend permission modes."""

    @property
    def last_prompt_stats(self):
        """Per-turn statistics (context usage, credits, etc.)."""
        return self._handle.last_prompt_stats

    @property
    def last_compaction_transient(self) -> bool:
        """Whether that failure is worth retrying (from the live session handle).

        Coerced to a real ``bool`` because the consumer compares against
        ``True`` — a truthy stand-in must not read as a verdict.
        """
        return getattr(self._handle, "last_compaction_transient", False) is True

    # ── Streaming (AcpClient-compatible method name) ──

    def stream_events(self, message: str) -> AsyncIterator[LLMEvent]:
        """Send a prompt and yield events. AcpClient-compatible name for stream().

        Delegates to stream() (NOT self._handle.prompt() directly) so it
        inherits the AcpRuntimeDead -> AcpProcessDied / AcpAuthRequired
        translation. Returning the raw handle iterator let AcpRuntimeDead (an
        AcpRuntimeError, not an AcpError) escape chat_runner's handlers on a
        runtime death at prompt start -> unhandled crash instead of retry/login.
        """
        return self.stream(message)

    @property
    def resumed(self) -> bool:
        """Whether the session was restored via session/load."""
        return self._resumed_flag

    @resumed.setter
    def resumed(self, value: bool) -> None:
        self._resumed_flag = value

    def set_resume_session_id(self, sid: str) -> None:
        """Store a session ID for future resume via session/load."""
        self._resume_session_id = sid

    # NOTE: no load_session() here. Resume is performed once, up front, by
    # AcpProvider._start_kiro_runtime via AcpRuntime.load_session() (direct
    # session/load under the transcript's original sid). A per-provider resume
    # method would re-introduce the mismatched-sid load that killed the runtime.

    # ── PID (for orphan tracking) ──

    @property
    def _pid(self) -> int | None:
        """PID of the runtime process."""
        return self._runtime.pid

    @property
    def _child_pids(self) -> dict[int, Any]:
        """Child PIDs of the runtime (for process sweep)."""
        return getattr(self._runtime, "_child_pids", {})

    @property
    def _start_time(self) -> int | None:
        """Process start time (for PID recycle detection)."""
        return getattr(self._runtime, "_start_time", None)
