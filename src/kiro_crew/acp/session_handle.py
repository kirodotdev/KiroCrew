"""AcpSessionHandle — one multiplexed ACP session on a shared runtime.

Split out of ``runtime.py`` to keep the two responsibilities in separate files:

- ``session_handle.py`` (this file): the per-session API surface — one
  ``sessionId`` + its ``asyncio.Queue``, the prompt/cancel/approve/reject event
  loop, and the per-session stale/stall watchdog. Depends only on the runtime
  *protocol* (``AcpRuntimeProtocol``), the shared dispatch parser, and the ACP
  types — never on the concrete ``AcpRuntime``.
- ``runtime.py``: ``AcpRuntime`` — owns the subprocess + single-reader demux and
  constructs handles.

The runtime exceptions (``AcpRuntimeError`` / ``AcpRuntimeDead``) and the
``AcpRuntimeProtocol`` live here (the lower layer) so ``runtime.py`` imports them
from this module without a circular import; ``runtime.py`` re-exports them so
existing ``from kiro_crew.acp.runtime import AcpSessionHandle`` call sites keep
working.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from kiro_crew import model_registry
from kiro_crew.acp._dispatch import (
    build_permission_event,
    classify_notification,
    parse_metadata,
    parse_session_update,
    parse_text_chunk,
    parse_usage_update,
    redact_text,
    set_mode_params,
    set_model_params,
)
from kiro_crew.acp.client import (
    AcpError,
    AcpProcessDied,
    AcpTimeoutError,
    _is_safe_oauth_url,
    _is_tool_interrupted_marker,
    _is_transient_raw_error,
)
from kiro_crew.acp.liveness import (
    EVIDENCE_ESTABLISHED_FLAT,
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LivenessOracle,
    ToolCallState,
)
from kiro_crew.acp.types import (
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_STEER_CLEARED,
    EVENT_STEER_CONSUMED,
    EVENT_STEER_QUEUED,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    METHOD_CANCEL,
    METHOD_COMMANDS_EXECUTE,
    METHOD_PROMPT,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    STOP_REASON_CANCELLED,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
    TurnUsage,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# ── Constants ──

_DEFAULT_PROMPT_TIMEOUT = 7200.0  # 2 hours


@dataclass(frozen=True)
class WatchdogSettings:
    """Resolved ``watchdog.*`` config values, read ONCE at handle construction
    (never inside the dispatch loop). Defaults mirror ``WatchdogConfig`` in
    ``config/loader.py`` so a config-less context (tests, early bootstrap)
    behaves identically to a default config."""

    check_after_secs: float = 60.0
    stale_window_secs: float = 300.0
    tool_stall_suspect_secs: float = 600.0
    tool_stall_hard_cap_secs: float = 2700.0
    model_silent_probe_secs: float = 900.0
    wellness_sample_secs: float = 3.0


def _load_watchdog_settings() -> WatchdogSettings:
    """Snapshot ``watchdog.*`` from config. Function-level import (mirrors
    ``_sync_effort_levels``) avoids the config -> dashboard -> acp import
    cycle; any failure falls back to defaults rather than breaking a handle."""
    try:
        # circular import: config.loader -> dashboard -> session -> acp
        from kiro_crew.config.loader import KiroCrewConfig

        w = KiroCrewConfig.load().watchdog
        return WatchdogSettings(
            check_after_secs=float(w.check_after_secs),
            stale_window_secs=float(w.stale_window_secs),
            tool_stall_suspect_secs=float(w.tool_stall_suspect_secs),
            tool_stall_hard_cap_secs=float(w.tool_stall_hard_cap_secs),
            model_silent_probe_secs=float(w.model_silent_probe_secs),
            wellness_sample_secs=float(w.wellness_sample_secs),
        )
    except Exception:
        logger.debug("watchdog settings load failed — using defaults", exc_info=True)
        return WatchdogSettings()


# How often a WORKING-verdict deferral is logged (evidence trail without spam).
_WORKING_LOG_INTERVAL_SECS = 600.0
# Unresponsive-cancel budget: after cancel() is sent, if kiro-cli does not
# ack (via a cancelled stopReason on the prompt response) within this window,
# the dispatch loop unblocks the caller with a terminal EVENT_COMPLETE. The
# shared runtime is NOT killed (co-tenant sessions keep running) — mirrors
# AcpClient's _CANCEL_GRACE_SECS floor without the process-kill (which is
# impossible on a multiplexed runtime).
_CANCEL_GRACE_SECS = 10.0
# MCP-server-init drain (parity with AcpClient._drain_notifications): after
# set_mode, briefly consume the session queue so MCP-init/oauth/config frames
# are processed before the first prompt, instead of racing into the first turn.
_MCP_DRAIN_DURATION = 1.0
_MCP_DRAIN_IDLE_EXIT = 0.25
_SENTINEL = object()


class AcpRuntimeError(Exception):
    """Base error for AcpRuntime operations."""


class AcpRuntimeDead(AcpRuntimeError):
    """Raised when the underlying process has died."""


class AcpRuntimeProtocol(Protocol):
    """Minimal interface that AcpSessionHandle needs from AcpRuntime."""

    _last_activity: float

    @property
    def pid(self) -> int | None:
        """Subprocess pid (sandbox launcher parent under the Linux namespace
        sandbox) — the liveness oracle scans its descendant tree for evidence."""
        ...

    async def send_request(self, method: str, params: dict[str, Any]) -> int:
        ...

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        ...

    async def send_response(self, request_id: str | int, result: dict[str, Any]) -> None:
        ...

    async def send_error(self, request_id: str | int, code: int, message: str) -> None:
        ...

    def unregister_session(self, session_id: str) -> None:
        ...

    async def terminate_session(self, session_id: str) -> None:
        ...

    def is_alive(self) -> bool:
        ...


class AcpSessionHandle:
    """Handle for a single ACP session on a shared runtime.

    Owns one sessionId + asyncio.Queue. Reads events from the queue (fed by
    AcpRuntime's reader task) and provides prompt/cancel/approve/reject API.
    """

    def __init__(
        self,
        session_id: str,
        queue: asyncio.Queue[JsonRpcMessage | None],
        runtime: AcpRuntimeProtocol,
        watchdog: WatchdogSettings | None = None,
    ) -> None:
        self._session_id = session_id
        self._queue = queue
        self._runtime = runtime
        # Watchdog windows are snapshotted here (construction time) so the
        # dispatch loop never reads config; the liveness oracle carries the
        # per-session evidence state (tracked child, counter samples).
        self._watchdog = watchdog if watchdog is not None else _load_watchdog_settings()
        self._oracle = LivenessOracle(sample_min_secs=self._watchdog.wellness_sample_secs)
        # Snapshot of the most recent EVENT_TOOL_CALL (title/redacted input/
        # dispatch time/shell flag) — the oracle's attribution key. Cleared on
        # EVENT_TOOL_RESULT alongside _tool_dispatched.
        self._inflight_tool: ToolCallState | None = None
        # Last monotonic ts a WORKING-verdict deferral was logged (rate limit).
        self._working_logged_ts = 0.0
        self._cancelled = False
        # Unresponsive-cancel tracking (mirrors AcpClient._cancel_ts /
        # _cancel_grace_secs). Set by cancel(); the dispatch loop uses them to
        # unblock the caller if kiro-cli never acks the cancel.
        self._cancel_ts = 0.0
        self._cancel_grace_secs = _CANCEL_GRACE_SECS
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._stale_eligible = False
        # Set when a genuine stale turn is probed via session/cancel; read by the
        # unresponsive-cancel branch to distinguish a confirmed wedge (signal
        # auto-recovery) from an ordinary unacked cancel (unblock caller).
        self._stale_probe = False
        self._tool_dispatched = False
        self._last_stop_reason = ""
        # toolCallId -> redacted input string, written by the shared parser so a
        # later tool result can recover its originating input (mirrors AcpClient).
        self._tool_call_inputs: dict[str, str] = {}
        # toolCallId -> is_shell, cached from the tool_call notification so the
        # later permission_request event (which carries no trusted kind) can
        # inherit the canonical shell signal. Mirrors AcpClient's cache and is
        # the ONLY trusted source build_permission_event reads for is_shell.
        self._tool_call_is_shell: dict[str, bool] = {}
        # toolCallId -> raw structured params (dict) cached from the tool_call
        # notification so the later permission_request event can carry
        # raw_tool_params for the governance keystone (sensitive-path /
        # write-protected-config) checks. Mirrors AcpClient's _tool_call_params.
        self._tool_call_raw_params: dict[str, dict] = {}
        # Server names for which a mid-session MCP OAuth banner was already
        # emitted, so we don't spam duplicates. Discarded on the matching
        # server_initialized / server_init_failure so a later token-expiry
        # retry can re-surface. Instance-scoped (NOT reset per turn) — mirrors
        # AcpClient._oauth_emitted_servers.
        self._oauth_emitted_servers: set[str] = set()
        # JSON-RPC request id -> {"once","always","reject"} optionId map, so
        # approve_tool / reject_tool echo the exact ids the agent advertised
        # (kiro "allow_once"/"allow_always"; claude-agent-acp "allow"/"reject").
        self._permission_options: dict[str | int, dict[str, str]] = {}
        # req_ids of in-flight _wait_for_response calls (send_command /
        # set_config_option / compact). The prompt dispatch loop shares this
        # session's queue, so when it dequeues one of these responses it uses
        # this set to hand it back promptly (vs. dropping / holding to turn end).
        self._awaited_responses: set[int] = set()
        self.last_prompt_stats = AcpPromptStats()
        # State tracking (populated from session/new response via store_session_config)
        self._model: str = ""
        # Model id kiro-cli RESOLVED the session to (from currentModelId in the
        # session/new|load response), kept separate from _model (the user-picked
        # alias) so it feeds ONLY the context-window backfill — never slot.model
        # (mirrors AcpClient._resolved_model_id; avoids the profile-id
        # pinning trap where a resolved profile id poisons slot.model).
        self._resolved_model_id: str = ""
        self._config_options: list[dict[str, Any]] = []
        self._available_models: list[dict[str, str]] = []

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_turn_active(self) -> bool:
        # Factor _cancelled (parity with AcpClient.has_active_turn) so a second
        # cancel() is a no-op early-return instead of re-sending session/cancel.
        # Also require the runtime alive (parity: AcpClient checks _is_process_alive)
        # so a turn on a dead runtime reads inactive -> AcpProvider.cancel() returns
        # "no_turn" instead of firing cancel_session on a corpse.
        return (
            (not self._turn_done.is_set())
            and (not self._cancelled)
            and self._runtime.is_alive()
        )

    async def wait_turn_done(self, timeout: float = 30.0) -> bool:
        """Wait for the current turn to complete. Returns True if done, False on timeout."""
        try:
            await asyncio.wait_for(self._turn_done.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ── Prompt ──

    async def prompt(
        self, message: str, timeout: float = _DEFAULT_PROMPT_TIMEOUT
    ) -> AsyncIterator[AcpEvent]:
        """Send session/prompt and yield AcpEvent objects until the turn completes.

        Dispatches events from the per-session queue with the same logic as
        AcpClient._dispatch_events. Detects turn boundaries via the JSON-RPC
        response matching the prompt's request_id.
        """
        # Guard against concurrent prompts on the same handle: a second call
        # would clear _turn_done and race on the shared _queue, corrupting
        # turn state and losing events. Each caller should use its own handle.
        if not self._turn_done.is_set():
            raise AcpRuntimeError("A turn is already active on this session handle")

        self._cancelled = False
        self._cancel_ts = 0.0
        self._turn_done.clear()
        # Reset the stored stop_reason: only a real `complete` response sets it,
        # and the synthetic-terminal paths (cancel-unacked / stale / tool-stall /
        # timeout) call _turn_done.set() WITHOUT updating it. Without this reset,
        # wait_turn_done() would return the PREVIOUS turn's reason (e.g. a stale
        # "end_turn" making a timed-out cancel look acked, or "" → a spurious
        # hard kill of the shared runtime). Mirrors AcpClient.
        self._last_stop_reason = ""
        self._stale_eligible = False
        # Set when a genuine stale turn is probed via session/cancel; read by the
        # unresponsive-cancel branch to distinguish a confirmed wedge (signal
        # auto-recovery) from an ordinary unacked cancel (unblock caller).
        self._stale_probe = False
        self._tool_dispatched = False
        self._inflight_tool = None
        self._oracle.reset()
        self._working_logged_ts = 0.0
        self._tool_call_inputs.clear()
        self._tool_call_is_shell.clear()
        self._tool_call_raw_params.clear()
        self._permission_options.clear()

        # Drain frames left over from a prior abandoned turn. The cancel-unacked
        # / stale / tool-stall / timeout paths synthesize a terminal
        # EVENT_COMPLETE and return while the real kiro-cli turn keeps emitting
        # frames into this (unbounded) queue. Without draining, those leftover
        # tool_call/text_chunk/subagent frames bleed into THIS turn's stream and
        # the queue grows without bound. The abandoned turn's prompt response is
        # already skipped via is_response_for; its notifications are not, so drop
        # them here before the new turn begins.
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        self.last_prompt_stats = AcpPromptStats(
            context_pct=self.last_prompt_stats.context_pct,
            context_used_tokens=self.last_prompt_stats.context_used_tokens,
            context_window_tokens=self.last_prompt_stats.context_window_tokens,
        )

        # send_request must be inside the turn-state guard: _turn_done was just
        # cleared above, so if the request raises (e.g. AcpRuntimeDead on a
        # broken pipe) before the try/finally below is entered, _turn_done would
        # stay cleared forever — is_turn_active would report True permanently and
        # every future prompt() on this handle would be rejected. Re-set it on
        # failure so the handle stays reusable.
        try:
            req_id = await self._runtime.send_request(
                METHOD_PROMPT,
                {"sessionId": self._session_id, "prompt": [{"type": "text", "text": message}]},
            )
        except Exception:
            self._turn_done.set()
            raise

        try:
            async for event in self._dispatch_events(req_id, timeout):
                yield event
        finally:
            if not self._turn_done.is_set():
                self._turn_done.set()

    # ── Cancel ──

    async def cancel(self, grace_secs: float = 0.0, _stale_probe: bool = False) -> None:
        """Send session/cancel notification.

        Records the cancel time + grace budget so the dispatch loop can unblock
        the caller if kiro-cli never acks the cancel (no cancelled stopReason on
        the prompt response). On a shared runtime we cannot force-kill the
        process (co-tenant sessions would die), so recovery is a synthesized
        terminal event rather than a hard kill.

        ``_stale_probe`` marks a watchdog probe cancel (internal). A genuine
        (non-probe) cancel SUPERSEDES any pending probe: the flag is cleared so
        the eventual ack is attributed to the user, not reclassified to
        auto-recovery.
        """
        self._stale_probe = _stale_probe
        self._cancelled = True
        self._cancel_ts = time.monotonic()
        self._cancel_grace_secs = max(_CANCEL_GRACE_SECS, grace_secs)
        # cancel is a JSON-RPC notification (no id, no response) — use
        # send_notification so we don't register an unanswerable routing entry.
        await self._runtime.send_notification(
            METHOD_CANCEL,
            {"sessionId": self._session_id},
        )

    # ── Tool Approval ──

    async def approve_tool(self, request_id: str | int, option_id: str | None = None) -> None:
        """Approve a pending permission request.

        ``option_id`` overrides the auto-resolved id when provided. Otherwise the
        optionIds the agent advertised (recorded by build_permission_event) are
        consulted — picking the "always" variant when the caller asked for the
        "allow_always" id, else the "once" variant. Falls back to the kiro
        literals when nothing was recorded. This keeps kiro-cli
        ("allow_once"/"allow_always") and claude-agent-acp ("allow"/"allow_always")
        working without the caller knowing the backend.
        """
        resolved_id = option_id
        recorded = self._permission_options.pop(request_id, None)
        if recorded:
            if resolved_id is None:
                resolved_id = recorded.get("once") or recorded.get("always")
            elif resolved_id == OPTION_ALLOW_ALWAYS:
                resolved_id = recorded.get("always") or recorded.get("once") or resolved_id
            elif resolved_id == OPTION_ALLOW_ONCE:
                resolved_id = recorded.get("once") or resolved_id
        if resolved_id is None:
            resolved_id = OPTION_ALLOW_ONCE
        await self._runtime.send_response(
            request_id,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": resolved_id}},
        )

    async def reject_tool(self, request_id: str | int) -> None:
        """Reject a pending permission request.

        Prefers a clean ``selected`` reject using the reject optionId the agent
        advertised (claude-agent-acp offers ``reject`` → behavior:"deny",
        surfacing a clear "permission denied" rather than the cryptic "Tool use
        aborted" the adapter throws on a ``cancelled`` outcome). Falls back to
        ``cancelled`` when no reject option was advertised (kiro-cli), which kiro
        handles as an ordinary rejection.
        """
        recorded = self._permission_options.pop(request_id, None)
        reject_id = recorded.get("reject") if recorded else None
        if reject_id:
            await self._runtime.send_response(
                request_id,
                {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": reject_id}},
            )
        else:
            await self._runtime.send_response(
                request_id,
                {"outcome": {"outcome": OUTCOME_CANCELLED}},
            )

    # ── Session Configuration ──

    async def set_mode(self, agent_name: str) -> None:
        """Activate an agent via session/set_mode."""
        await self._runtime.send_request(
            METHOD_SET_MODE,
            set_mode_params(self._session_id, agent_name),
        )

    async def set_model(self, model_id: str) -> None:
        """Switch model via session/set_model."""
        await self._runtime.send_request(
            METHOD_SET_MODEL,
            set_model_params(self._session_id, model_id),
        )
        self._model = model_id
        # Parity with AcpClient.set_model: keep _resolved_model_id in sync so
        # _backfill_context_window looks up the NEW model's window after a switch
        # (otherwise the context meter converts pct against the stale session/new
        # model until the next session refresh).
        self._resolved_model_id = model_id

    async def steer(self, message: str) -> bool:
        """Inject a mid-turn steer into the running turn via kiro-cli's
        ``_session/steer`` ext-method. Fire-and-forget (mirrors AcpClient.steer).

        The reply streams back inside the SAME in-flight session/prompt; the
        authoritative signal is the steering_consumed notification (surfaced as
        EVENT_STEER_CONSUMED). We do not await the request response — the
        reader resolves/pops it harmlessly. Returns False for an empty message
        or no active session.
        """
        text = (message or "").strip()
        if not text or not self._session_id:
            return False
        wrapped = f"<user_message>\n{text}\n</user_message>"
        await self._runtime.send_request(
            "_session/steer",
            {"sessionId": self._session_id, "message": wrapped},
        )
        return True

    @property
    def supports_steer(self) -> bool:
        """True — AcpRuntime is kiro-cli only, which supports _session/steer."""
        return True

    # ── Commands & Config ──

    async def send_command(self, command: str, args: dict[str, Any] | None = None) -> str:
        """Execute a kiro slash command (e.g. '/compact', '/effort').

        Returns the response text (if any). Mirrors AcpClient.send_command.
        """
        if args:
            cmd_name = command.strip().split(None, 1)[0].lstrip("/")
            payload: dict[str, Any] = {
                "sessionId": self._session_id,
                "command": {"command": cmd_name, "args": args},
            }
        else:
            payload = {"sessionId": self._session_id, "command": command}
        req_id = await self._runtime.send_request(METHOD_COMMANDS_EXECUTE, payload)
        try:
            msg = await self._wait_for_response(req_id, timeout=60.0)
            result = msg.result or {}
            raw = result.get("text", "") or result.get("message", "") if isinstance(result, dict) else ""
            # Two-pass redaction (URLs + credentials) before returning — command
            # output is backend-echoed text that reaches the dashboard. Explicit
            # here (rather than redact_text) so the security control is auditable
            # at the external surface, matching AcpClient.send_command exactly.
            text = str(raw)
            text, _ = redact_exfiltration_urls(text)
            text, _ = redact_credentials(text)
            return text
        except AcpTimeoutError:
            return ""

    async def set_config_option(self, config_id: str, value: str) -> None:
        """Set a session config option (e.g. effort level).

        Sends session/set_config_option JSON-RPC request.
        """
        req_id = await self._runtime.send_request(
            METHOD_SET_CONFIG_OPTION,
            {"sessionId": self._session_id, "configId": config_id, "value": value},
        )
        await self._wait_for_response(req_id, timeout=10.0)

    # ── Compaction ──

    async def compact(self, context: str = "") -> None:
        """Trigger context compaction via /compact command."""
        cmd = "/compact"
        if context:
            cmd = f"/compact {context}"
        await self.send_command(cmd)

    async def wait_for_compaction(self, timeout: float = 120.0) -> dict[str, str]:
        """Wait for compaction completed/failed event from the session queue.

        Returns {"type": "completed"|"failed"|"timeout", "summary": "..."}.
        Drains the queue looking for COMPACTION_STATUS notifications.
        """
        deadline = time.monotonic() + timeout
        buffered: list[JsonRpcMessage] = []
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(
                        self._queue.get(), timeout=min(remaining, 5.0)
                    )
                except asyncio.TimeoutError:
                    continue
                if msg is None:
                    # Re-poison so the live turn / next consumer also sees death.
                    self._queue.put_nowait(None)
                    raise AcpProcessDied("Runtime died while waiting for compaction")
                # Check for compaction status
                if msg.method == "_kiro.dev/compaction/status":
                    params = msg.params or {}
                    status = params.get("status", {})
                    s_type = status.get("type", "") if isinstance(status, dict) else str(status)
                    if s_type in ("completed", "failed"):
                        # Redact backend-echoed summary before it reaches callers
                        # (compact() surfaces this to the dashboard).
                        return {
                            "type": s_type,
                            "summary": redact_text(str(params.get("summary", "") or "")),
                        }
                    continue
                # Track metadata if it arrives (also consumes it).
                if msg.method == "_kiro.dev/metadata":
                    self._track_metadata(msg)
                    continue
                # Any other frame belongs to a concurrent live turn — buffer it
                # (do not drop) so its dispatch loop / usage meter still sees it.
                buffered.append(msg)
            return {"type": "timeout"}
        finally:
            for _m in buffered:
                self._queue.put_nowait(_m)

    # ── Responsiveness ──

    def is_responsive(self, stale_threshold: float = 600.0) -> bool:
        """True if runtime is alive AND has had activity within threshold seconds."""
        if not self._runtime.is_alive():
            return False
        return (time.monotonic() - self._runtime._last_activity) < stale_threshold

    # ── State tracking ──

    @property
    def model(self) -> str:
        """Current model name for this session."""
        return self._model

    @property
    def config_options(self) -> list[dict[str, Any]]:
        """ACP-reported configOptions (effort, model, mode selectors)."""
        return self._config_options

    @property
    def available_models(self) -> list[dict[str, str]]:
        """Models advertised by the backend at session init."""
        return list(self._available_models)

    def supports_config_option(self, config_id: str) -> bool:
        """Whether the session advertised a config option with this id.

        Returns True when no config options were reported yet (lazy backend).
        """
        if not self._config_options:
            return True
        return any(
            isinstance(opt, dict) and opt.get("id") == config_id
            for opt in self._config_options
        )

    def get_valid_effort_levels(self) -> list[str]:
        """Return valid effort levels from config options, preserving order."""
        for opt in self._config_options:
            if not isinstance(opt, dict):
                continue
            if opt.get("id") == "effort":
                options = opt.get("options", [])
                if isinstance(options, list):
                    return [
                        o.get("value", "")
                        for o in options
                        if isinstance(o, dict) and o.get("value")
                    ]
        return []

    def store_session_config(self, resp: dict[str, Any]) -> None:
        """Extract configOptions and available models from session/new or session/load response.

        Called after create_session() or load() to populate state.
        """
        config_options = resp.get("configOptions")
        if isinstance(config_options, list):
            self._config_options = config_options
            self._sync_effort_levels()
        models = resp.get("models") or resp.get("availableModels")
        if isinstance(models, dict):
            # Record the resolved model id (kiro-cli's currentModelId) so
            # _backfill_context_window can look up the window on pct-only
            # metadata even when the user never explicitly switched models.
            current_model_id = models.get("currentModelId")
            if isinstance(current_model_id, str) and current_model_id:
                self._resolved_model_id = current_model_id
            avail = models.get("availableModels", [])
            if isinstance(avail, list):
                self._available_models = self._normalize_models(avail)
        elif isinstance(models, list):
            self._available_models = self._normalize_models(models)

    @staticmethod
    def _normalize_models(advertised: list[Any]) -> list[dict[str, str]]:
        """Normalize advertised models to ``{modelId, name, description}`` with
        guaranteed keys (parity with AcpClient._capture_available_models), so the
        dashboard model dropdown gets a stable shape regardless of backend."""
        captured: list[dict[str, str]] = []
        for m in advertised:
            if not isinstance(m, dict):
                continue
            model_id = m.get("modelId") or m.get("value") or ""
            if not model_id:
                continue
            captured.append({
                "modelId": str(model_id),
                "name": str(m.get("name") or model_id),
                "description": str(m.get("description") or ""),
            })
        return captured

    def _sync_effort_levels(self) -> None:
        """Push ACP-reported effort levels to the global validation set (parity
        with AcpClient._sync_effort_levels). Without this, the unified kiro path
        never refreshes the reasoning-effort allow-list. Function-level import
        avoids the chat_persistence -> dashboard -> session -> acp import cycle."""
        levels = self.get_valid_effort_levels()
        if levels:
            # circular import: chat_persistence -> dashboard -> session -> acp
            from kiro_crew.dashboard.chat_persistence import update_reasoning_effort_values
            update_reasoning_effort_values(levels)

    # NOTE: resume is done via AcpRuntime.load_session() (issues session/load
    # DIRECTLY under the transcript's own sid). The old per-handle load() is
    # intentionally removed: it issued session/load with sessionId=this handle's
    # sid — which on the resume path is a FRESH session/new sid, not the
    # transcript's — so kiro-cli replayed the old transcript on top of a freshly
    # primed session and died / refused. See AcpRuntime.load_session for the fix.

    async def destroy(self) -> None:
        """Terminate this session on kiro-cli, delete its transcript, unregister.

        Sends ``_kiro.dev/session/terminate`` (via the runtime) so the shared
        kiro-cli process frees this session's transcript/context and reaps its
        MCP children — NOT just a local queue unregister. Without the terminate,
        a finished session's state stays resident in the multiplexed process
        forever, so RSS climbs with cumulative sessions (the background-runtime
        unbounded-growth bug). ``terminate_session`` is best-effort + bounded and
        ALWAYS unregisters the queue, so teardown neither hangs nor raises.

        Each session on a shared runtime (a ``_bg`` op or a session-sharing
        subagent) is a distinct ``session/new`` with its own persisted
        ``~/.kiro/sessions/cli/{sid}.json``(+``.jsonl``). The shared runtime is
        not killed on teardown, so we also delete the transcript here — otherwise
        these files would accumulate for the gateway lifetime (titles/
        suggestions/folders/nav run on nearly every chat). Only ephemeral
        sessions call destroy(): main-chat sessions are torn down via
        ``owns_runtime=True`` → ``runtime.kill()`` and intentionally keep their
        transcript for ``session/load`` resume, so cleaning up here is safe.
        """
        await self._runtime.terminate_session(self._session_id)
        self._cleanup_transcript()

    def _cleanup_transcript(self) -> None:
        """Best-effort delete of this session's kiro-cli transcript files."""
        sid = self._session_id
        if not sid:
            return
        sessions_dir = (Path.home() / ".kiro" / "sessions" / "cli").resolve()
        for suffix in (".json", ".jsonl"):
            target = (sessions_dir / f"{sid}{suffix}").resolve()
            # Guard against a crafted sessionId escaping the sessions dir.
            if target.parent != sessions_dir:
                logger.error("destroy: path traversal blocked for %s", target)
                return
            try:
                target.unlink(missing_ok=True)
            except OSError:
                logger.warning("destroy: failed to delete transcript %s", target, exc_info=True)

    # ── Internal dispatch ──

    async def _wait_for_response(self, req_id: int, timeout: float = 30.0) -> JsonRpcMessage:
        """Drain queue until we get the response for req_id.

        Non-matching frames are NOT dropped: a command/config call
        (send_command / compact / set_config_option) can run concurrently with a
        live prompt turn, and both read this shared queue. Silently discarding
        non-matching frames here would steal the in-flight turn's text/tool
        frames (wedging its dispatch loop). Instead we buffer them and re-inject
        them in the finally so the turn's consumer still sees them (mirrors
        AcpClient.wait_for_compaction).
        """
        deadline = time.monotonic() + timeout
        buffered: list[JsonRpcMessage] = []
        self._awaited_responses.add(req_id)
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(self._queue.get(), timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    continue
                if msg is None:
                    # Runtime died: re-poison for the live turn / next consumer.
                    self._queue.put_nowait(None)
                    raise AcpProcessDied("Runtime process died while waiting for response")
                if msg.is_response_for(req_id):
                    if msg.error:
                        # Classify the raw JSON-RPC error so the chat_runner /
                        # llm_helpers retry ladder recognizes transient backend 5xx
                        # (e.g. a mid-stream InternalServerError surfaced as -32603)
                        # instead of surfacing a bare error card. The kiro raise
                        # sites previously lacked the transient= flag, so the string
                        # fallback classifier missed the raw "InternalServerError"
                        # dict. Mirrors client._raise_acp_error.
                        raise AcpError(
                            f"ACP error: {msg.error}",
                            transient=_is_transient_raw_error(msg.error),
                        )
                    return msg
                # Not our response — buffer (do not drop) for re-injection.
                buffered.append(msg)
            raise AcpTimeoutError(f"Timeout waiting for response to request {req_id}")
        finally:
            self._awaited_responses.discard(req_id)
            for _m in buffered:
                self._queue.put_nowait(_m)

    async def _dispatch_events(
        self, req_id: int, timeout: float
    ) -> AsyncIterator[AcpEvent]:
        """Core event dispatch loop. Yields AcpEvent objects from the session queue."""
        deadline = time.monotonic() + timeout
        last_data_ts = time.monotonic()

        _buffered: list[JsonRpcMessage] = []
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                # Unresponsive-cancel recovery: cancel() was sent but kiro-cli has
                # not acked (no cancelled stopReason) within the grace budget. On a
                # shared runtime we cannot kill the process (co-tenants would die),
                # so unblock the caller with a synthesized terminal event instead.
                if (
                    self._cancelled
                    and self._cancel_ts
                    and not self._turn_done.is_set()
                    and (time.monotonic() - self._cancel_ts) > self._cancel_grace_secs
                ):
                    self._turn_done.set()
                    if self._stale_probe:
                        # Single-shot consumption, mirroring the turn-complete
                        # reclassification branch.
                        self._stale_probe = False
                        # A genuine stale turn was probed via session/cancel and kiro
                        # never acked within the grace window → CONFIRMED WEDGE (a
                        # done-but-missing-frame turn would have acked and completed
                        # normally via the turn-complete branch). Signal the dashboard
                        # to reset+resume and re-drive with a continue-nudge
                        # (auto-recovery) instead of orphaning the turn until the
                        # user's next message collides with "prompt already in
                        # progress". Complementary to the stuck-session
                        # surfacing (which handles what this cannot recover).
                        logger.warning(
                            "Stale turn on session %s unrecovered after %.1fs cancel "
                            "grace — signalling auto-recovery",
                            self._session_id, self._cancel_grace_secs,
                        )
                        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_STALE_RECOVER,
                                       usage=TurnUsage(credits=self.last_prompt_stats.credits))
                        return
                    logger.warning(
                        "Cancel unacked after %.1fs on session %s — unblocking caller "
                        "(runtime kept alive for co-tenants)",
                        self._cancel_grace_secs, self._session_id,
                    )
                    yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="error: cancel unacked",
                                   usage=TurnUsage(credits=self.last_prompt_stats.credits))
                    return

                try:
                    msg = await asyncio.wait_for(
                        self._queue.get(), timeout=min(remaining, 5.0)
                    )
                except asyncio.TimeoutError:
                    # ── Verdict-driven watchdogs ──
                    # Wellness (the liveness oracle) is the detector; timeouts
                    # govern only the UNKNOWN class. Idle clocks: the stale clock
                    # folds in the runtime's stderr/keepalive clock (_last_activity
                    # — kiro streams thinking_tokens on STDERR during reasoning);
                    # the tool clock keys off session-queue frames only (keepalive
                    # and progress frames for the session reset last_data_ts, so a
                    # legitimately-streaming tool keeps the watchdog satisfied).
                    if self._cancelled:
                        continue
                    wd = self._watchdog
                    now = time.monotonic()

                    if self._tool_dispatched:
                        _tool_idle = now - last_data_ts
                        if _tool_idle <= wd.check_after_secs:
                            continue
                        verdict, evidence = await self._consult_oracle_offloaded(model_wait=False)
                        if verdict == VERDICT_WORKING:
                            self._log_working_deferral(_tool_idle, evidence)
                            continue
                        # UNKNOWN acts at the suspect window; the hard cap governs
                        # only the stale/model-wait branch below (it bounds the
                        # extended UNKNOWN deferral windows there).
                        _acting = (
                            verdict in (VERDICT_DEAD, VERDICT_STUCK_INPUT)
                            or _tool_idle > wd.tool_stall_suspect_secs
                        )
                        if not _acting:
                            continue  # UNKNOWN, within budget — keep waiting
                        async for ev in self._end_stalled_tool(verdict, evidence, _tool_idle):
                            yield ev
                        return

                    if self._stale_eligible:
                        _stale_idle = now - max(last_data_ts, self._runtime._last_activity)
                        if _stale_idle <= wd.check_after_secs:
                            continue
                        verdict, evidence = await self._consult_oracle_offloaded(model_wait=True)
                        if verdict == VERDICT_WORKING:
                            self._log_working_deferral(_stale_idle, evidence)
                            continue
                        if verdict != VERDICT_DEAD:
                            # UNKNOWN: probe only past the window. An established-
                            # but-flat backend connection is probably a non-streamed
                            # server-side think — probing it cancels + regenerates
                            # the think, so it gets the extended window. The hard
                            # cap bounds any UNKNOWN deferral absolutely.
                            window = (
                                wd.model_silent_probe_secs
                                if evidence.startswith(EVIDENCE_ESTABLISHED_FLAT)
                                else wd.stale_window_secs
                            )
                            if _stale_idle <= min(window, wd.tool_stall_hard_cap_secs):
                                continue
                        # DEAD, or UNKNOWN past its window: probe via session/cancel.
                        # The probe is NON-LETHAL either way — a live turn's cancel
                        # ack is reclassified to STOP_REASON_STALE_RECOVER in the
                        # turn-complete branch (auto-recovery, never "cancelled by
                        # user"), and an unacked cancel confirms the wedge via the
                        # unresponsive-cancel branch at the loop top.
                        logger.warning(
                            "Stale turn on session %s (idle %.0fs, verdict=%s: %s) — "
                            "probing via session/cancel",
                            self._session_id, _stale_idle, verdict, evidence,
                        )
                        try:
                            await asyncio.wait_for(
                                self.cancel(_stale_probe=True), timeout=5.0
                            )
                        except Exception:
                            logger.debug(
                                "stale-probe session/cancel failed for %s",
                                self._session_id, exc_info=True,
                            )
                    continue

                if msg is None:
                    # Runtime process died — sentinel
                    raise AcpProcessDied("Runtime process died during prompt")

                last_data_ts = time.monotonic()
                self.last_prompt_stats.event_count += 1

                # Turn-complete response
                if msg.is_response_for(req_id):
                    if msg.error:
                        # Classify the raw JSON-RPC error so the chat_runner /
                        # llm_helpers retry ladder recognizes transient backend 5xx
                        # (e.g. a mid-stream InternalServerError surfaced as -32603)
                        # instead of surfacing a bare error card. The kiro raise
                        # sites previously lacked the transient= flag, so the string
                        # fallback classifier missed the raw "InternalServerError"
                        # dict. Mirrors client._raise_acp_error.
                        raise AcpError(
                            f"ACP error: {msg.error}",
                            transient=_is_transient_raw_error(msg.error),
                        )
                    result = msg.result or {}
                    reason = ""
                    if isinstance(result, dict):
                        reason = result.get("stopReason", "") or ""
                    if self._stale_probe and reason == STOP_REASON_CANCELLED:
                        # Probe-ack reclassification (the non-lethal harness for
                        # every watchdog probe): kiro-cli acks session/cancel on a
                        # LIVE mid-generation turn too, so a probe-induced
                        # "cancelled" must NOT surface as a user cancellation (the
                        # turn would die silently — the original session-killer).
                        # Rewrite to STOP_REASON_STALE_RECOVER so the dashboard
                        # auto-recovers (reset + resume + continue-nudge). An
                        # oracle mistake therefore costs a regeneration, never a
                        # session. Genuine user cancels (no _stale_probe) pass
                        # through unchanged.
                        logger.info(
                            "Stale-probe cancel acked on session %s — reclassifying "
                            "to %s for auto-recovery",
                            self._session_id, STOP_REASON_STALE_RECOVER,
                        )
                        reason = STOP_REASON_STALE_RECOVER
                        # Single-shot: the flag is consumed here so a later genuine
                        # cancel can never be misattributed to a stale probe.
                        self._stale_probe = False
                    self._last_stop_reason = reason
                    self._tool_dispatched = False
                    self._turn_done.set()
                    yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=reason,
                                   usage=TurnUsage(credits=self.last_prompt_stats.credits))
                    return
                if msg.method is None and msg.id is not None:
                    # Response frame for a DIFFERENT req_id: a concurrent
                    # command/config call (send_command / compact /
                    # set_config_option) shares this session queue. Re-inject it
                    # IMMEDIATELY — not buffered until this turn ends — so that
                    # caller's _wait_for_response (registered in
                    # _awaited_responses while active) picks it up promptly
                    # instead of spuriously timing out. asyncio.sleep(0) yields so
                    # the waiting consumer is scheduled to dequeue it before we
                    # loop back (otherwise get() on the now-nonempty queue would
                    # let us re-grab our own re-injection). If no caller is
                    # waiting (already timed out / gave up), drop it — buffering
                    # it to turn end would only leak a stray frame into the next
                    # turn.
                    if msg.id in self._awaited_responses:
                        self._queue.put_nowait(msg)
                        await asyncio.sleep(0)
                    else:
                        logger.debug(
                            "Dropping stray response frame id=%s (no waiter)", msg.id
                        )
                    continue

                # Dispatch by method
                action = self._classify(msg)

                if action == "permission":
                    yield self._build_permission_event(msg)
                elif action == "server_request_unknown":
                    await self._runtime.send_error(msg.id, -32601, "Method not found")
                elif action == "update":
                    for ev in self._handle_update(msg):
                        yield ev
                        # kiro-cli's built-in security filter can abort a turn's
                        # tools and emit ONLY this text marker — never a `complete`
                        # response. Synthesize one so the caller exits instead of
                        # hanging until the 2h prompt timeout. Mirrors
                        # AcpClient._dispatch_events.
                        if ev.kind == EVENT_TEXT_CHUNK and _is_tool_interrupted_marker(ev.text):
                            self._emit_tool_interrupted_sel("_dispatch_events")
                            self._tool_dispatched = False
                            self._turn_done.set()
                            yield AcpEvent(kind=EVENT_COMPLETE,
                                           usage=TurnUsage(credits=self.last_prompt_stats.credits))
                            return
                elif action == "steer":
                    # Mid-turn steer lifecycle echo from kiro-cli (_session/steer).
                    # queued carries the pending snapshot; consumed carries injected
                    # text. Never trust backend-echoed steer text: redact before it
                    # can reach any surface. Mirrors AcpClient._dispatch_events.
                    params = msg.params or {}
                    _upd = params.get("update")
                    _upd = _upd if isinstance(_upd, dict) else {}
                    _disc = str(_upd.get("sessionUpdate") or "")
                    _text = redact_text(str(_upd.get("content") or _upd.get("message") or ""))
                    if _disc in ("steering_queued", "AgentExecutionUserMessageQueued"):
                        yield AcpEvent(kind=EVENT_STEER_QUEUED, text=_text)
                    elif _disc in ("steering_consumed", "AgentExecutionSteeringInjected"):
                        yield AcpEvent(kind=EVENT_STEER_CONSUMED, text=_text)
                    elif _disc == "steering_cleared":
                        yield AcpEvent(kind=EVENT_STEER_CLEARED)
                elif action == "metadata":
                    self._track_metadata(msg)
                elif action == "compaction":
                    params = msg.params or {}
                    status = params.get("status", {})
                    status_type = status.get("type", "") if isinstance(status, dict) else str(status)
                    # Compaction summary is backend-echoed text (LLM-influenced)
                    # that reaches the dashboard — redact exfil URLs/credentials
                    # before surfacing it (parity with other text surfaces).
                    summary = redact_text(str(params.get("summary", "") or ""))
                    yield AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=summary)
                elif action == "clear":
                    yield AcpEvent(kind=EVENT_CLEAR_STATUS)
                elif action == "agent_switched":
                    params = msg.params or {}
                    yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=params.get("agentName", ""))
                elif action == "subagent_list":
                    params = msg.params or {}
                    subs = params.get("subagents")
                    if isinstance(subs, list):
                        yield AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=subs)
                elif action == "subagent_activity":
                    params = msg.params or {}
                    ssid = str(params.get("sessionId") or "")
                    upd = params.get("update") or {}
                    upd = upd if isinstance(upd, dict) else {}
                    tcid = str(upd.get("toolCallId") or "")
                    # Single-source the text-shape read via the shared parser so the
                    # sub-agent text path matches the main one (content.text + flat).
                    _su_text_val, _su_thinking = parse_text_chunk(upd)
                    su_text = _su_text_val or ""
                    su_kind = str(upd.get("sessionUpdate") or "")
                    if ssid and tcid:
                        yield AcpEvent(
                            kind=EVENT_SUBAGENT_ACTIVITY,
                            sub_session_id=ssid,
                            tool_call_id=tcid,
                            title=redact_text(str(upd.get("title") or "")),
                        )
                    elif ssid and su_text and su_kind == "agent_message_chunk" and not _su_thinking:
                        yield AcpEvent(
                            kind=EVENT_SUBAGENT_ACTIVITY,
                            sub_session_id=ssid,
                            text=redact_text(su_text),
                        )
                elif action == "mcp_oauth_request":
                    params = msg.params or {}
                    server_name = str(params.get("serverName") or params.get("name") or "")
                    oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
                    # Parity with AcpClient (client.py mcp_oauth_request): reject
                    # unsafe-scheme URLs BEFORE recording dedupe (so a later safe
                    # retry still gets through), drop empty serverName (can't
                    # correlate the later initialized/failure discard), and dedupe
                    # per server so token-expiry retries don't spam banners. MCP
                    # server names/URLs can be LLM-influenced, so the scheme check
                    # is defense-in-depth.
                    if not _is_safe_oauth_url(oauth_url):
                        if oauth_url:
                            logger.warning(
                                "ACP: refusing unsafe mid-session MCP OAuth URL for %s",
                                server_name or "(unknown)",
                            )
                        continue
                    if not server_name:
                        logger.warning(
                            "ACP: dropping mid-session MCP OAuth request with empty serverName"
                        )
                        continue
                    if server_name in self._oauth_emitted_servers:
                        logger.debug(
                            "ACP: dropping duplicate mid-session MCP OAuth request for %s",
                            server_name,
                        )
                        continue
                    self._oauth_emitted_servers.add(server_name)
                    yield AcpEvent(
                        kind=EVENT_MCP_OAUTH_REQUEST,
                        server_name=server_name,
                        oauth_url=oauth_url,
                    )
                elif action == "mcp_server_initialized":
                    params = msg.params or {}
                    server_name = str(params.get("serverName") or params.get("name") or "")
                    if server_name:
                        # Allow re-emission of oauth_request if this server's token
                        # expires later (mirrors AcpClient).
                        self._oauth_emitted_servers.discard(server_name)
                        yield AcpEvent(
                            kind=EVENT_MCP_SERVER_INITIALIZED,
                            server_name=server_name,
                        )
                elif action == "mcp_server_init_failure":
                    params = msg.params or {}
                    server_name = str(params.get("serverName") or params.get("name") or "")
                    err = str(params.get("error") or "")
                    if err:
                        # MCP init errors can carry connection strings / tokens from
                        # a failed server startup (LLM-influenceable) — scrub exfil
                        # URLs + credentials before this text reaches the dashboard
                        # banner (EVENT_MCP_SERVER_INIT_FAILURE.text).
                        err, _ = redact_exfiltration_urls(err)
                        err, _ = redact_credentials(err)
                    if server_name:
                        # Banner is in a failed state — clear dedupe so kiro-cli's
                        # next oauth retry for this server surfaces a new banner
                        # instead of being silently dropped (mirrors AcpClient).
                        self._oauth_emitted_servers.discard(server_name)
                        yield AcpEvent(
                            kind=EVENT_MCP_SERVER_INIT_FAILURE,
                            server_name=server_name,
                            text=err,
                        )

            # Timeout — no complete received. Yield a terminal EVENT_COMPLETE with a
            # distinguishing stop_reason so callers that break on EVENT_COMPLETE can
            # tell this apart from a normal turn end.
            self._turn_done.set()
            yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="timeout",
                           usage=TurnUsage(credits=self.last_prompt_stats.credits))
        finally:
            for _m in _buffered:
                self._queue.put_nowait(_m)

    async def _consult_oracle_offloaded(self, *, model_wait: bool) -> tuple[str, str]:
        """Oracle verdict, offloaded off the event loop.

        The oracle's evidence gathering is a synchronous /proc filesystem walk
        (``iter_descendants`` + per-descendant reads + ``os.readlink`` on
        ``/proc/<pid>/fd/*``, which can block on a wedged fd), so it runs on
        ``subprocess_executor()`` — same treatment as the runtime's RSS probe —
        bounded so a hung /proc read can't wedge the watchdog itself. Any
        failure degrades to UNKNOWN, never to a kill.
        """
        pid = getattr(self._runtime, "pid", None)
        call: Callable[..., tuple[str, str]]
        if model_wait:
            call = self._oracle.check_model_wait
            args: tuple[Any, ...] = (pid,)
        else:
            tool = self._inflight_tool
            if tool is None:
                return VERDICT_UNKNOWN, "no in-flight tool state"
            call = self._oracle.check_tool
            args = (pid, tool)
        try:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(subprocess_executor(), call, *args),
                timeout=10.0,
            )
        except Exception:
            logger.debug("oracle consultation failed/timed out", exc_info=True)
            return VERDICT_UNKNOWN, "oracle offload error"

    def _log_working_deferral(self, idle: float, evidence: str) -> None:
        """Evidence trail for a WORKING deferral, rate-limited to one line per
        interval so a 40-minute build doesn't spam the journal."""
        now = time.monotonic()
        if now - self._working_logged_ts < _WORKING_LOG_INTERVAL_SECS:
            return
        self._working_logged_ts = now
        logger.info(
            "Watchdog deferral on session %s: idle %.0fs but verdict WORKING (%s)",
            self._session_id, idle, evidence,
        )

    async def _end_stalled_tool(
        self, verdict: str, evidence: str, idle: float
    ) -> AsyncIterator[AcpEvent]:
        """Cancel THIS session and end the turn with the tool-stall stop reason.

        Session-scoped recovery on a SHARED runtime: never kill the process
        (co-tenant sessions would die) — session/cancel drops only this
        session's in-flight prompt. Bounded (5s) so an unresponsive runtime
        can't turn stall recovery into a second stall. The terminal event
        carries the tool title / redacted command / evidence so chat_runner's
        dedicated recovery can build a targeted continue-nudge (with log-file
        hint and, for STUCK_INPUT, the re-run-non-interactively advice)
        instead of blindly re-running the original user message.
        """
        tool = self._inflight_tool
        logger.warning(
            "Tool stall on session %s (idle %.0fs, verdict=%s: %s) — cancelling "
            "session (runtime kept alive for co-tenants)",
            self._session_id, idle, verdict, evidence,
        )
        try:
            await asyncio.wait_for(self.cancel(), timeout=5.0)
        except Exception:
            logger.debug(
                "session/cancel after tool stall failed for %s",
                self._session_id, exc_info=True,
            )
        self._turn_done.set()
        yield AcpEvent(
            kind=EVENT_COMPLETE,
            stop_reason=STOP_REASON_TOOL_STALL,
            title=(tool.title if tool else ""),
            tool_input=(tool.command if tool else ""),
            text=f"verdict={verdict}; idle_secs={int(idle)}; {evidence}",
            usage=TurnUsage(credits=self.last_prompt_stats.credits),
        )

    def _track_metadata(self, msg: JsonRpcMessage) -> None:
        """Capture per-turn context usage + kiro billing credits from _kiro.dev/metadata.

        Mirrors AcpClient._track_metadata so sessions on the shared runtime get the
        same per-turn credit attribution. kiro bills in credits (token fields are 0
        for the acp provider), streamed as meteringUsage entries with unit="credit".
        Accumulated across the turn; reset per turn by the AcpPromptStats re-init in
        prompt().
        """
        params = msg.params or {}
        pct, credits = parse_metadata(params)
        if pct is not None:
            try:
                pct_f = float(pct)
                self.last_prompt_stats.context_pct = pct_f
                self._backfill_context_window(pct_f)
            except (TypeError, ValueError):
                pass
        self.last_prompt_stats.credits += credits

    def _backfill_context_window(self, pct: float) -> None:
        """Derive window/used tokens from the model registry when kiro-cli 2.10+
        metadata gives only a percentage (no usage_update {used, size}).

        Mirrors AcpClient._backfill_context_window so the shared-runtime path
        reports the same context-meter token counts. No-op once a real
        usage_update has set the window. Keys on ``_resolved_model_id`` (kiro's
        ``currentModelId`` from the session/new|load response, also synced by
        set_model) first, then falls back to ``_model`` (the user-picked alias).
        """
        if self.last_prompt_stats.context_window_tokens > 0:
            return  # a real usage_update already set it
        model_id = self._resolved_model_id or self._model
        if not model_id:
            return
        window = model_registry.window(model_id)
        if window <= 0:
            return
        self.last_prompt_stats.context_window_tokens = window
        self.last_prompt_stats.context_used_tokens = round(window * pct / 100.0)

    def _emit_tool_interrupted_sel(self, site: str) -> None:
        """Emit a SEL audit + WARNING when kiro-cli's security filter cancels tools.

        Mirrors AcpClient._emit_tool_interrupted_sel: a permission decision
        KiroCrew observes but does not control (kiro-cli denied tool execution).
        Best-effort — a failed audit must not break the turn. This handle has no
        KiroCrew session_key, so the ACP sessionId is recorded in metadata for
        correlation.
        """
        logger.warning(
            "kiro-cli cancelled tool use(s) [site=%s session=%s]", site, self._session_id
        )
        try:
            sel().log_tool_invocation(
                session_key="",
                source="acp",
                tool_name="kiro_cli_security_filter",
                tool_kind="client_built_in",
                outcome="denied",
                metadata={
                    "site": site,
                    "reason": "tool_interrupted_marker",
                    "session_id": self._session_id,
                },
            )
        except Exception:
            logger.warning(
                "SEL audit failed for tool_interrupted at %s", site, exc_info=True
            )

    async def drain_init(
        self,
        duration: float = _MCP_DRAIN_DURATION,
        idle_exit: float = _MCP_DRAIN_IDLE_EXIT,
    ) -> None:
        """Drain MCP-init / oauth / config frames from the queue after set_mode.

        Parity with AcpClient._drain_notifications. During session setup there is
        no in-flight prompt, so every frame on this session's queue is an
        init-time notification. Draining them here keeps them out of the first
        turn's event stream and gives MCP servers a brief window to report in
        before the first prompt races ahead. Bounded by ``duration`` with early
        exit after ``idle_exit`` seconds of silence. ``config_option_update``
        frames refresh cached configOptions; everything else is logged/discarded.
        Best-effort — never raises.
        """
        deadline = time.monotonic() + duration
        drained = 0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                msg = await asyncio.wait_for(
                    self._queue.get(), timeout=min(remaining, idle_exit)
                )
            except asyncio.TimeoutError:
                break  # queue went quiet — MCP servers done reporting
            if msg is None:
                # Runtime died during init — re-poison so the next consumer sees it.
                await self._queue.put(None)
                break
            drained += 1
            try:
                action = classify_notification(msg)
                if action == "update":
                    params = msg.params or {}
                    update = params.get("update") or {}
                    if (
                        isinstance(update, dict)
                        and update.get("sessionUpdate") == "config_option_update"
                    ):
                        cfg = update.get("configOptions")
                        if isinstance(cfg, list):
                            self._config_options = cfg
                            self._sync_effort_levels()
                elif action == "mcp_server_init_failure":
                    p = msg.params or {}
                    logger.info(
                        "MCP server init failure on %s: %s",
                        self._session_id, p.get("serverName") or "",
                    )
            except Exception:
                logger.debug("drain_init: error processing init frame", exc_info=True)
        if drained:
            logger.debug(
                "drain_init: drained %d init frame(s) for %s", drained, self._session_id
            )

    def _classify(self, msg: JsonRpcMessage) -> str:
        """Classify a notification message into an action string."""
        return classify_notification(msg)

    def _build_permission_event(self, msg: JsonRpcMessage) -> AcpEvent:
        """Build an AcpEvent for a permission request via the shared parser.

        Delegates to _dispatch.build_permission_event so this transport reads the
        kiro/claude payload shape (toolCall-nested title/kind/toolCallId, option
        normalization, is_shell from the trusted tool_call cache) identically to
        AcpClient. Records the advertised optionIds so approve/reject echo them.
        """
        event, recorded = build_permission_event(
            msg,
            tool_input_cache=self._tool_call_inputs,
            shell_cache=self._tool_call_is_shell,
            raw_params_cache=self._tool_call_raw_params,
        )
        if recorded is not None and event.request_id != "":
            self._permission_options[event.request_id] = recorded
        return event

    def _handle_update(self, msg: JsonRpcMessage) -> list[AcpEvent]:
        """Process a session/update notification and return events."""
        params = msg.params or {}
        update = params.get("update") or {}
        if not isinstance(update, dict):
            return []

        session_update = update.get("sessionUpdate", "")

        # usage_update updates context stats only — it is not an AcpEvent.
        # parse_usage_update reconciles the flat (AcpClient) and nested shapes.
        if session_update == "usage_update":
            used, size = parse_usage_update(update)
            if used is not None and size:
                try:
                    if size > 0:
                        self.last_prompt_stats.context_pct = round((used / size) * 100, 1)
                        self.last_prompt_stats.context_used_tokens = int(used)
                        self.last_prompt_stats.context_window_tokens = int(size)
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            return []

        # config_option_update: ACP pushes updated configOptions (e.g. after
        # model switch rebuilds effort options). State update, no event emitted.
        if session_update == "config_option_update":
            config_options = update.get("configOptions")
            if isinstance(config_options, list):
                self._config_options = config_options
                self._sync_effort_levels()
            return []

        # All other session/update kinds go through the single shared parser so
        # AcpRuntime and AcpClient cannot drift on frame shape or redaction. The
        # parser writes redacted tool inputs into our caller-owned cache AND the
        # trusted shell signal into _tool_call_is_shell (which the permission
        # event later reads); we then derive per-session stale/stall bookkeeping
        # from the returned events.
        events = parse_session_update(
            update,
            tool_input_cache=self._tool_call_inputs,
            shell_cache=self._tool_call_is_shell,
            raw_params_cache=self._tool_call_raw_params,
        )
        for ev in events:
            if ev.kind == EVENT_TEXT_CHUNK:
                self.last_prompt_stats.text_chunks += 1
                self._stale_eligible = True
            elif ev.kind == EVENT_TOOL_CALL:
                self._stale_eligible = False
                self._tool_dispatched = True
                # Attribution snapshot for the liveness oracle: title + the
                # already-redacted input + dispatch time + the trusted shell
                # flag. A new dispatch resets the oracle's tracked child and
                # counter samples so evidence never bleeds across tools.
                self._inflight_tool = ToolCallState(
                    title=ev.title,
                    command=ev.tool_input,
                    dispatch_ts=time.monotonic(),
                    is_shell=ev.is_shell,
                )
                self._oracle.reset()
            elif ev.kind == EVENT_TOOL_RESULT:
                self._tool_dispatched = False
                self._inflight_tool = None
        return events
