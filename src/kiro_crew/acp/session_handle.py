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
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from kiro_crew import model_registry
from kiro_crew.acp import kas_wire
from kiro_crew.acp._dispatch import (
    build_permission_event,
    classify_notification,
    parse_metadata,
    parse_prompt_token_usage,
    parse_session_update,
    parse_text_chunk,
    parse_usage_cost,
    parse_usage_update,
    redact_text,
    reject_option_id,
    set_mode_params,
    set_model_params,
)
from kiro_crew.acp.client import (
    _COMPACTION_FAILED_TURN_BUDGET,
    AcpProcessDied,
    AcpTimeoutError,
    _effective_prompt_timeout_async,
    _is_safe_oauth_url,
    _is_tool_interrupted_marker,
    _raise_acp_error,
    compaction_failure_detail,
    compaction_failure_is_transient,
    format_command_result,
    parse_slash_command,
    prompt_timeout_for_ceiling,
    resolve_usable_model,
)
from kiro_crew.acp.liveness import (
    EVIDENCE_ESTABLISHED_FLAT,
    EVIDENCE_SHELL_CHILD_ABSENT,
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LivenessOracle,
    ToolCallState,
    _consume_future_exception,
    boottime_now,
    consult_offloaded,
)
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.prompt_blocks import build_prompt_blocks, summarize_prompt_structure
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
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
    JSONRPC_METHOD_NOT_FOUND,
    METHOD_CANCEL,
    METHOD_COMMANDS_EXECUTE,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    MODEL_CONFIG_ID,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    STOP_REASON_CANCELLED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
    UPDATE_CURRENT_MODE,
    UPDATE_SESSION_INFO,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
)
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS
from kiro_crew.executors import subprocess_executor
from kiro_crew.metrics.events import CHILD_PERMISSION_DENIED, emit_counter
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# ── Constants ──


@dataclass(frozen=True)
class WatchdogSettings:
    """Resolved ``watchdog.*`` config values, read ONCE at handle construction
    (never inside the dispatch loop). Defaults mirror ``WatchdogConfig`` in
    ``config/loader.py`` so a config-less context (tests, early bootstrap)
    behaves identically to a default config.

    Every idle window must stay strictly inside the turn's own wall-clock
    ceiling — see :func:`_clamp_to_prompt_ceiling` for why and
    :data:`_TURN_CEILING_WINDOW_FRACTION` for the enforced headroom."""

    check_after_secs: float = 60.0
    stale_window_secs: float = 300.0
    tool_stall_suspect_secs: float = 3600.0
    tool_stall_hard_cap_secs: float = 3600.0
    model_silent_probe_secs: float = 900.0
    wellness_sample_secs: float = 3.0
    # Whether a per-agent watchdog_tool_stall_* override was applied to this
    # snapshot. Telemetry-only (the kirocrew.watchdog.action attr): a BOOLEAN,
    # never the agent name — free-form agent names are a cardinality bomb on
    # OTel attrs (metrics/schema.py); per-agent joins happen via the always-on
    # token row store instead.
    agent_override: bool = False


# Fraction of a turn's deadline that a watchdog idle window may occupy. A window
# at or past the deadline is unreachable: the turn's own timeout fires first, so
# the UNKNOWN-verdict branch never runs and the user gets the generic "turn hit
# the limit" card instead of tool-stall recovery (which cancels non-lethally and
# re-drives with a continue-nudge naming the tool and any redirect log). The
# headroom covers the cancel + ack grace so recovery lands inside the same turn.
_TURN_CEILING_WINDOW_FRACTION = 0.9
# watchdog.* keys bounded by the prompt timeout: each is an idle-seconds window
# the dispatch loop compares elapsed idle against. wellness_sample_secs is a
# sampling interval, not a window, so it is not bounded here.
_TURN_BOUNDED_WINDOWS = (
    "check_after_secs",
    "stale_window_secs",
    "tool_stall_suspect_secs",
    "tool_stall_hard_cap_secs",
    "model_silent_probe_secs",
)


def _clamp_to_prompt_ceiling(key: str, value: float, chat_ceiling: float) -> float:
    """Bound one watchdog window to the transport's per-prompt timeout.

    Resolved via :func:`~kiro_crew.acp.client.prompt_timeout_for_ceiling` on the
    caller's ALREADY-LOADED ``chat_turn_timeout_secs`` (no second config read;
    the transport's dispatch loop stops the turn at the same deadline), so it
    is the only safe bound for a snapshot that is taken once per handle and
    reused across prompts. It follows a raised ``agent.chat_turn_timeout_secs``
    and never sits below the 2h default, so a proportionately raised watchdog
    window is honoured instead of being cut to the default's fraction.

    Mirrors the shape of ``turn_dispatch.chat_turn_timeout_secs``'s clamp
    against the same timeout: an out-of-range value is honoured as far as the
    system can honour it, and the clamp is logged at warning level so the
    misconfiguration is visible instead of silently ignored.
    """
    ceiling = prompt_timeout_for_ceiling(chat_ceiling)
    budget = ceiling * _TURN_CEILING_WINDOW_FRACTION
    if value <= budget:
        return value
    logger.warning(
        "watchdog.%s=%.0fs leaves no room inside the %.0fs prompt timeout; "
        "clamping to %.0fs. The turn's own timeout would fire first, so the "
        "larger window cannot take effect.",
        key, value, ceiling, budget,
    )
    return budget


def _warn_if_above_chat_ceiling(key: str, value: float, chat_ceiling: float) -> None:
    """Advisory: a DASHBOARD turn ends at ``agent.chat_turn_timeout_secs``, so a
    window above that can never act there.

    Deliberately not clamped. The same handle also serves callers that pass
    their own, larger prompt timeout (a review run, a cron turn), and shrinking
    every window to the dashboard's ceiling would cancel their live work. So the
    mismatch is reported and left to the operator.
    """
    if 0 < chat_ceiling < value:
        logger.warning(
            "watchdog.%s=%.0fs exceeds agent.chat_turn_timeout_secs=%.0fs — a "
            "dashboard turn ends before this window can act, so a stall there "
            "surfaces as the turn-limit card instead of stall recovery.",
            key, value, chat_ceiling,
        )


def _load_watchdog_settings(crew_agent: str = "") -> WatchdogSettings:
    """Snapshot ``watchdog.*`` from config. Function-level import (mirrors
    ``_sync_effort_levels``) avoids the config -> dashboard -> acp import
    cycle; any failure falls back to defaults rather than breaking a handle.

    ``crew_agent`` is the CANONICAL Kiro Crew agent name — a ``cfg.agents``
    key resolved by the surface that owns the identity (the dashboard slot,
    or a crew-name-passing surface like Slack/cron) and plumbed here through
    provider -> runtime -> handle. Resolution is a direct dict lookup: no
    cross-namespace matching happens here, so a bound kiro agent name (or any
    non-crew name) simply inherits the globals. That crew's
    ``watchdog_tool_stall_*`` overrides overlay the globals (> 0 means
    override; 0 inherits — the same empty-inherits convention as the agent's
    ``model``).
    """
    try:
        # circular import: config.loader -> dashboard -> session -> acp
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        w = cfg.watchdog
        raw = {key: float(getattr(w, key)) for key in _TURN_BOUNDED_WINDOWS}
        overridden = False
        crew = cfg.agents.get(crew_agent) if crew_agent else None
        if crew is not None:
            if crew.watchdog_tool_stall_suspect_secs > 0:
                raw["tool_stall_suspect_secs"] = float(crew.watchdog_tool_stall_suspect_secs)
                overridden = True
            if crew.watchdog_tool_stall_hard_cap_secs > 0:
                raw["tool_stall_hard_cap_secs"] = float(crew.watchdog_tool_stall_hard_cap_secs)
                overridden = True
        # Overrides are applied BEFORE the ceiling pass so a per-agent window is
        # bounded exactly like a global one — an over-ceiling override is clamped
        # with the same warning instead of smuggling past the prompt timeout.
        chat_ceiling = float(cfg.agent.chat_turn_timeout_secs)
        bounded = {}
        for key in _TURN_BOUNDED_WINDOWS:
            value = _clamp_to_prompt_ceiling(key, raw[key], chat_ceiling)
            _warn_if_above_chat_ceiling(key, value, chat_ceiling)
            bounded[key] = value
        return WatchdogSettings(
            wellness_sample_secs=float(w.wellness_sample_secs),
            agent_override=overridden,
            **bounded,
        )
    except Exception:
        logger.debug("watchdog settings load failed — using defaults", exc_info=True)
        return WatchdogSettings()


# How often a WORKING-verdict deferral is logged (evidence trail without spam).
_WORKING_LOG_INTERVAL_SECS = 600.0
# Idle ceiling past which a WORKING deferral stops being routine. Below it, a
# deferral is the expected shape of a long build and logs at INFO. Past it the
# deferral is on course to consume the whole turn budget, so it logs at WARNING
# — the default ``agent.log_level``, without which the one decision that can
# hold a turn silent until its ceiling leaves no trace in production logs. The
# rate limit above still applies, so escalation does not become a spam source.
_WORKING_WARN_AFTER_SECS = 1800.0
# The same mark as a fraction of the turn's own deadline, so escalation still
# happens with room to spare on a turn shorter than the default: the effective
# threshold is whichever of the two is lower.
_WORKING_WARN_DEADLINE_FRACTION = 0.25
# "No deferral logged yet" marker for the rate-limit clock. It cannot be 0.0:
# ``time.monotonic()`` counts from boot on Linux, so on a host up for less than
# the interval above, 0.0 reads as "logged moments ago" and swallows the very
# first deferral line — exactly the evidence a freshly restarted gateway needs.
_WORKING_NEVER_LOGGED = float("-inf")


def _watchdog_evidence_class(evidence: str) -> str:
    """Bucket a free-form oracle evidence string into a closed enum.

    OTel attribute values MUST be low-cardinality (metrics/schema.py): the raw
    evidence carries pids, byte deltas, and command fragments, so only its
    SHAPE is emitted. Buckets: ``established_flat`` (LLM-shaped — runtime-held
    backend socket, flat subtree), ``mcp_flat`` (opaque MCP tool, moving or
    flat), ``shell_absent`` (shell tool in flight with nothing this dispatch
    could have started still running), ``shell`` (other shell-child evidence),
    ``wait`` (the declared-duration wait tool), ``degraded`` (everything else:
    sampling baseline, unreadable /proc, no pid, oracle error — the oracle could
    not attest either way).
    """
    e = evidence or ""
    if e.startswith(EVIDENCE_ESTABLISHED_FLAT):
        return "established_flat"
    if e.startswith(EVIDENCE_SHELL_CHILD_ABSENT):
        # Checked before the "shell child" substring below, which its evidence
        # text also contains.
        return "shell_absent"
    if "mcp subtree" in e:
        return "mcp_flat"
    if "shell child" in e:
        return "shell"
    if e.startswith("wait tool"):
        return "wait"
    return "degraded"


# Unresponsive-cancel budget: after cancel() is sent, if kiro-cli does not
# ack (via a cancelled stopReason on the prompt response) within this window,
# the dispatch loop unblocks the caller with a terminal EVENT_COMPLETE. The
# shared runtime is NOT killed (co-tenant sessions keep running) — mirrors
# AcpClient's _CANCEL_GRACE_SECS floor without the process-kill (which is
# impossible on a multiplexed runtime).
_CANCEL_GRACE_SECS = 10.0
# Commands that must stay on the PROMPT transport even where native
# commands/execute is available: kiro-cli 2.14.0 exits rc=0 WITHOUT a response
# on commands/execute for these (live-probe recorded in compact()'s docstring;
# the probe covered the string form, and no probe exists for their object
# form), and session.py's compaction flow additionally depends on watching
# compaction status mid-PROMPT-stream. Routing them natively would leave the
# dispatch loop draining an unanswered request until its deadline.
_PROMPT_TRANSPORT_COMMANDS = frozenset({"compact", "help"})
# Native command turns are bounded like send_command's 60s RPC wait, not like
# a chat turn: commands/execute answers in well under a second, and neither
# turn watchdog arms on a command turn (no text chunk streamed, no tool
# dispatched), so an unanswered request would otherwise drain silently for the
# full chat-turn ceiling (hours) while holding the session's turn slot.
_COMMAND_TURN_TIMEOUT_SECS = 60.0
# Post-compaction metadata grace: kiro-cli emits fresh _kiro.dev/metadata with
# the real post-compaction contextUsagePercentage ~1s after the completed
# status (live-probe confirmed). Mirrors AcpClient's constant.
_POST_COMPACTION_METADATA_GRACE_SECS = 5.0
# MCP-server-init drain (parity with AcpClient._drain_notifications): after
# set_mode, briefly consume the session queue so MCP-init/oauth/config frames
# are processed before the first prompt, instead of racing into the first turn.
_MCP_DRAIN_DURATION = 1.0
_MCP_DRAIN_IDLE_EXIT = 0.25
# Hard ceiling while NO MCP server has reported yet. The idle shortcut is only
# meaningful once reporting has begun — before the first registration frame,
# queue silence just means the server is still booting (an npx-based stdio
# server spends seconds on npm resolution plus a Node boot before emitting
# anything), so the drain keeps waiting up to this ceiling instead. Sized to
# cover a realistic npx cold start (npm resolve + Node boot, observed 1-6s)
# while bounding the cost for a session whose agent config has no MCP servers
# at all — the one case that pays the full ceiling, since nothing ever arms
# the idle exit. Sessions with fast servers are unaffected: their registration
# frames are staged during session/new and arm the idle exit immediately.
_MCP_DRAIN_NO_REPORT_CEILING = 6.0
# Notification actions that count as "an MCP server reported in" for the
# drain's arming logic. OAuth requests count: a server that asks for OAuth has
# booted and reached its auth step, which is the same liveness signal.
_MCP_DRAIN_REPORT_ACTIONS = frozenset(
    {
        "mcp_server_initialized",
        "mcp_server_init_failure",
        "mcp_oauth_request",
    }
)
_SENTINEL = object()


def parse_advertised_models(resp: dict[str, Any]) -> list[dict[str, str]]:
    """The normalized advertised-model list from a ``session/new``/``session/load``
    response (``[]`` when the backend advertised nothing).

    Accepts both response shapes: a ``models`` object
    ``{availableModels: [...], currentModelId}`` and a bare list under either
    key. A falsy ``models`` value (``{}``, ``[]``, ``None``) falls through to a
    top-level ``availableModels`` — callers that gate on ``models`` themselves
    should pass a wrapped envelope (e.g. ``{"models": models}``) so the
    fallback cannot source the list from a payload their gate never saw. Normalization matches
    :meth:`AcpSessionHandle._normalize_models`, so a
    probe's answer and a session-init snapshot are directly comparable.
    """
    models = resp.get("models") or resp.get("availableModels")
    if isinstance(models, dict):
        avail = models.get("availableModels", [])
        if isinstance(avail, list):
            return AcpSessionHandle._normalize_models(avail)
        return []
    if isinstance(models, list):
        return AcpSessionHandle._normalize_models(models)
    return []


class AcpRuntimeError(Exception):
    """Base error for AcpRuntime operations."""


class AcpRuntimeDead(AcpRuntimeError):
    """Raised when the underlying process has died."""


class AcpRequestTimeout(AcpRuntimeError):
    """Raised when a request's response does not arrive within its budget.

    Distinguished from a plain ``AcpRuntimeError`` so a caller that knows what
    the request was waiting on can attach that context before it reaches the
    user. Subclasses the base so existing ``except AcpRuntimeError`` handlers
    keep catching it.
    """


class AcpRuntimeProtocol(Protocol):
    """Minimal interface that AcpSessionHandle needs from AcpRuntime."""

    _last_activity: float

    @property
    def pid(self) -> int | None:
        """Subprocess pid (sandbox launcher parent under the Linux namespace
        sandbox) — the liveness oracle scans its descendant tree for evidence."""
        ...

    @property
    def acp_backend(self) -> str:
        """Which ACP backend the process speaks.

        The handle needs it because the backends disagree on verbs, not just on
        payloads — the model, for one, is a ``session/set_model`` request on
        kiro-cli and a session config option on KAS.
        """
        ...

    @property
    def supports_image_prompt(self) -> bool:
        """Whether the agent advertised ``promptCapabilities.image``.

        Read by :meth:`AcpSessionHandle.prompt` to decide if an image may travel
        as an inline block. Fails closed, so a backend that never handshaked
        gets text only.
        """
        ...

    @property
    def agent_version(self) -> str:
        """``agentInfo.version`` from the handshake — the version the process runs.

        ``""`` until the handshake completes; a capability gate reading it
        fails closed on that.
        """
        ...

    async def send_request(self, method: str, params: dict[str, Any]) -> int:
        ...

    async def probe_advertised_models(self) -> list[dict[str, str]]:
        """Fresh advertised-model snapshot from a throwaway ``session/new``
        (``[]`` = probe failed / advertised nothing — never evidence)."""
        ...

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        ...

    async def send_response(self, request_id: str | int, result: dict[str, Any]) -> None:
        ...

    async def send_error(self, request_id: str | int, code: int, message: str) -> None:
        ...

    def mark_turn_active(self, session_id: str, active: bool) -> None:
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
        crew_agent: str = "",
        bound_cwd: str = "",
    ) -> None:
        self._session_id = session_id
        self._queue = queue
        self._runtime = runtime
        # The directory THIS session was opened against, which on a shared runtime is
        # not the runtime's own: sessions for different projects live on one process.
        self._bound_cwd = bound_cwd
        # When True, destroy() skips the transcript unlink (subagent
        # continuability: the transcript is spawn_continue's resume material).
        self.keep_transcript = False
        # Watchdog windows are snapshotted here (construction time) so the
        # dispatch loop never reads config; the liveness oracle carries the
        # per-session evidence state (tracked child, counter samples).
        # ``crew_agent`` is the CANONICAL crew identity resolved by the surface
        # that owns it and plumbed down (see _load_watchdog_settings): it keys
        # the per-agent watchdog_tool_stall_* overrides by direct config lookup.
        # It must NEVER become an OTel metric attribute (free-form =>
        # cardinality bomb; see metrics/schema.py) — telemetry carries the
        # agent_override BOOLEAN. An explicit ``watchdog`` always wins
        # verbatim: the async creation paths (runtime.create_session /
        # load_session) resolve it off-loop and hand it in, so the synchronous
        # load below is only the fallback for direct constructions (tests).
        self._crew_agent = crew_agent
        self._watchdog = watchdog if watchdog is not None else _load_watchdog_settings(crew_agent)
        self._oracle = LivenessOracle(sample_min_secs=self._watchdog.wellness_sample_secs)
        # Keep the executor future, not an await-scoped flag: wait_for can time
        # out while the underlying thread continues its /proc walk. A pending
        # future makes the next watchdog tick answer UNKNOWN instead of
        # submitting a second job, so a wedged walk cannot stack blocked workers
        # in the shared subprocess_executor().
        self._consult_future: asyncio.Future[tuple[str, str]] | None = None
        # Snapshot of the most recent EVENT_TOOL_CALL (title/redacted input/
        # dispatch time/shell flag) — the oracle's attribution key. Cleared on
        # EVENT_TOOL_RESULT alongside _tool_dispatched.
        self._inflight_tool: ToolCallState | None = None
        # Last monotonic ts a WORKING-verdict deferral was logged (rate limit).
        self._working_logged_ts = _WORKING_NEVER_LOGGED
        # Terminal compaction status captured by compact() while draining its
        # own prompt turn (kiro-cli may emit _kiro.dev/compaction/status
        # BEFORE end_turn). wait_for_compaction() consumes it first so the
        # drain never strands a caller into a spurious 120s timeout.
        self._compact_result: dict[str, str] | None = None
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
        # Monotonically increasing count of NOTIFICATION frames delivered to this
        # session by the shared queue. Incremented in _wait_for_response whenever
        # it consumes a notification (not a response) from the queue while
        # buffering for a concurrent command call. The TOCTOU guard in
        # _dispatch_events snapshots this before the oracle await and compares
        # after: an advance means a real activity frame arrived while the oracle
        # was executing (even if _wait_for_response had consumed it from the
        # queue in the meantime). Pure queue-depth checks cannot see frames that
        # are temporarily held in a concurrent consumer's buffer list.
        self._ingress_seq: int = 0
        # Consumer-park accounting, read by the idle clocks in _dispatch_events
        # and by external observers via parked_for_secs(). `_parked_total` is
        # cumulative for the turn; `_parked_since` is set only while suspended at
        # a yield in prompt().
        self._parked_total: float = 0.0
        self._parked_since: float | None = None
        # Monotonic timestamp of a `failed` compaction status seen this turn
        # (None otherwise). Arms the post-failure budget in _dispatch_events,
        # which ends an abandoned turn instead of draining to the ceiling.
        self._compaction_failed_at: float | None = None
        # Retryability of the LAST failed compaction, read by the dashboard's
        # STOP_REASON_COMPACTION_FAILED branch to decide between re-queuing the
        # abandoned message and giving up. Public (no leading underscore)
        # because that consumer reaches it through getattr on whichever of the
        # two client classes is serving the slot. Verdict only — see the twin
        # comment in AcpClient for why the reason text is not forwarded.
        self.last_compaction_transient: bool = False
        # Consumers that implement the low-fidelity child downgrade (dashboard
        # card / interactive approver) opt IN; for everyone else the handle
        # itself fail-closes low-fidelity child permission requests below, so
        # a consumer that predates the fidelity contract can never auto-approve
        # a child on agent-authored context. Full-fidelity child events flow to
        # every consumer unchanged (mode parity everywhere).
        self.child_fidelity_aware: bool = False
        # User-visible notices for permission requests this handle answered
        # itself (fail-close gate below, pre-turn drain): flushed as
        # EVENT_SUBAGENT_ACTIVITY at the next dispatch so the rejection shows
        # on the child's crew card instead of vanishing into the log.
        self._pending_reject_notices: list[tuple[str, str]] = []
        # In-flight SEL audit tasks for handle-owned permission rejections
        # (fail-close gate, pre-turn drain) — retained so they cannot be
        # garbage-collected mid-flight. Mirrors AcpRuntime._audit_tasks.
        self._audit_tasks: set[asyncio.Task[None]] = set()
        # Set when a permission event is yielded, cleared when it is answered.
        # Distinguishes "waiting for a human" (legitimate, bounded elsewhere)
        # from "the consumer stopped pulling for some other reason".
        self._awaiting_permission: bool = False
        # toolCallId -> redacted input string, written by the shared parser so a
        # later tool result can recover its originating input (mirrors AcpClient).
        self._tool_call_inputs: dict[str, str] = {}
        # Same-key provenance for ``_tool_call_inputs``.  The cached text is
        # intentionally redacted; this bit lets an approval surface fail closed
        # without retaining or forwarding the removed secret bytes.
        self._tool_call_input_redacted: dict[str, bool] = {}
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
        # toolCallId -> trusted MCP server name (_meta.kiro.mcpServerName) cached
        # from the tool_call notification so the later permission_request event
        # can carry mcp_server_name (empty on the permission payload). This is
        # what lets hooks.on_tool_call's app-own-server auto-approve fire on the
        # permission path. Mirrors _tool_call_is_shell.
        self._tool_call_mcp_server: dict[str, str] = {}
        # Trusted tool name (_meta.kiro.toolName) cached like _tool_call_mcp_server
        # so the permission event can rebuild mcp__<server>__<tool> for per-tool
        # governance in the app-own-server auto-approve.
        self._tool_call_tool_name: dict[str, str] = {}
        # Server names for which a mid-session MCP OAuth banner was already
        # emitted, so we don't spam duplicates. Discarded on the matching
        # server_initialized / server_init_failure so a later token-expiry
        # retry can re-surface. Instance-scoped (NOT reset per turn) — mirrors
        # AcpClient._oauth_emitted_servers.
        self._oauth_emitted_servers: set[str] = set()
        # OAuth requests collected by drain_init(). Dashboard startup drains
        # this list through AcpSessionProvider after create_session returns.
        self._pending_oauth_requests: list[dict[str, str]] = []
        # What THIS session's MCP servers reported at init — parity with
        # AcpClient._mcp_report. On the shared runtime the frames are staged
        # per sessionId before this handle's queue exists, so the report is
        # genuinely this session's and not the process's.
        self._mcp_report = McpSessionReport()
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
        # Last KAS mode id seen on a current_mode_update, so a re-assert of the
        # already-current mode does not surface a spurious agent-switch echo
        # (kiro-cli only emits on a real _kiro.dev/agent/switched). None = unseen.
        self._last_kas_mode_id: str | None = None
        # KAS sub-agent roster keyed by agentSubtaskId. Each entry is shaped for
        # EVENT_SUBAGENT_LIST consumption by _native_subagent_sync in chat_runner.
        self._kas_subagent_roster: dict[str, dict[str, Any]] = {}

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

    @property
    def has_unfinished_turn(self) -> bool:
        """True if the native turn has not reached its done boundary and the
        runtime is alive — INDEPENDENT of ``_cancelled`` (unlike
        :attr:`is_turn_active`).

        A turn that has been ``cancel()``'d but whose turn-done ack has not yet
        arrived still holds the native turn open; the shutdown drain must still
        wait on it before the runtime is killed, or kiro-cli's session lock is
        left held (the empty-response-after-restart bug).
        """
        return (not self._turn_done.is_set()) and self._runtime.is_alive()

    async def wait_turn_done(self, timeout: float = 30.0) -> bool:
        """Wait for the current turn to complete. Returns True if done, False on timeout."""
        try:
            await asyncio.wait_for(self._turn_done.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ── Prompt ──

    async def prompt(
        self, message: str, timeout: float | None = None
    ) -> AsyncIterator[AcpEvent]:
        """Send session/prompt and yield AcpEvent objects until the turn completes.

        Dispatches events from the per-session queue with the same logic as
        AcpClient._dispatch_events. Detects turn boundaries via the JSON-RPC
        response matching the prompt's request_id.

        ``timeout=None`` (every dashboard turn) resolves from
        ``agent.chat_turn_timeout_secs`` so the transport wait follows a raised
        turn ceiling instead of cutting the turn at the 2h default underneath it.
        """

        async def _build() -> tuple[str, dict[str, Any]]:
            # Offloaded: the builder stats and reads image files (up to
            # MAX_IMAGE_BYTES each) and base64-encodes them. Inline, that
            # blocking I/O runs on the gateway loop and pauses every other
            # session's streaming for the duration.
            prompt_blocks = await asyncio.to_thread(
                build_prompt_blocks,
                message,
                allow_image=self._runtime.supports_image_prompt,
            )
            # Content-free outbound STRUCTURE diagnostics (issue #6022): one
            # line per turn build recording block counts, per-type counts, and
            # the serialized byte size — NEVER any block text or bytes — so an
            # operator can tell a stale/invalid model id apart from a
            # structurally malformed payload the next time a turn is rejected
            # as "Improperly formed request". summarize_prompt_structure is
            # itself no-raise, so this cannot break the live turn.
            logger.debug(
                "acp prompt structure for session %s: %s",
                self._session_id,
                summarize_prompt_structure(prompt_blocks),
            )
            return METHOD_PROMPT, {
                "sessionId": self._session_id,
                # An image reaches the model ONLY as an image block. Sending a
                # local image path as a single text block would ship a
                # filesystem path as prose (Slack, dashboard) and the model
                # would never see the picture. Gated on the agent's advertised
                # capability; when it is absent the path stays in the text as a
                # tool-openable reference rather than being dropped.
                "prompt": prompt_blocks,
            }

        # Explicit aclose in a finally: abandoning THIS wrapper (gen.aclose()
        # at any yield) must finalize the inner turn generator NOW — its
        # finally is what unmarks the turn and re-sets _turn_done. Left to the
        # event loop's async-generator GC hook, the handle would read as
        # turn-active until some later collection pass.
        turn = self._run_turn(_build, timeout)
        try:
            async for event in turn:
                yield event
        finally:
            await turn.aclose()

    async def stream_command(
        self, command: str, timeout: float | None = None
    ) -> AsyncIterator[AcpEvent]:
        """Execute a slash command natively and yield AcpEvents until it completes.

        Sends ``_kiro.dev/commands/execute`` with the TuiCommand OBJECT form
        (``{command, args}``) — kiro-cli 2.14.0 exits without a response on the
        STRING form (see :meth:`compact`), so the object form is load-bearing —
        and drains ``session/update`` events with the same turn discipline as
        :meth:`prompt`. The command's own output arrives in the RESPONSE result
        (message/data), not as update chunks, so the dispatch loop
        surfaces it as a text chunk (``extract_command_result=True``). Mirrors
        AcpClient.stream_command for the shared runtime.

        Two carve-outs keep the PROMPT transport (delegate to :meth:`prompt`):

        - ``_PROMPT_TRANSPORT_COMMANDS`` (/compact, /help) — kiro-cli 2.14.0
          returns no response for these over commands/execute, and the
          compaction flow (session.py, Slack !compact) depends on watching
          ``compaction/status`` on the prompt stream.
        - a non-kiro backend (KAS) — ``_kiro.dev/commands/execute`` is
          kiro-cli-specific; KAS sessions keep degrading softly through
          session/prompt instead of erroring on an unimplemented method.

        The native turn is bounded at ``_COMMAND_TURN_TIMEOUT_SECS`` (matching
        send_command's RPC wait) because neither turn watchdog arms on a
        command turn; an explicit ``timeout`` still wins.
        """
        cmd_name, cmd_args = parse_slash_command(command)
        # Positive capability gate: only the kiro harness implements
        # _kiro.dev/commands/execute, so native execution requires
        # acp_backend == ACP_BACKEND_KIRO — every other harness (KAS today,
        # any harness added later) fails CLOSED onto the prompt transport
        # instead of inheriting a kiro-only RPC (harness parity H5/H6).
        native = (
            self._runtime.acp_backend == ACP_BACKEND_KIRO
            and cmd_name not in _PROMPT_TRANSPORT_COMMANDS
        )
        if not native:
            async for event in self.prompt(command, timeout=timeout):
                yield event
            return

        async def _build() -> tuple[str, dict[str, Any]]:
            return METHOD_COMMANDS_EXECUTE, {
                "sessionId": self._session_id,
                "command": {"command": cmd_name, "args": cmd_args},
            }

        # Same deterministic finalization as prompt(): see the comment there.
        turn = self._run_turn(
            _build,
            timeout if timeout is not None else _COMMAND_TURN_TIMEOUT_SECS,
            extract_command_result=True,
        )
        try:
            async for event in turn:
                yield event
        finally:
            await turn.aclose()

    async def _run_turn(
        self,
        build_request: Callable[[], Awaitable[tuple[str, dict[str, Any]]]],
        timeout: float | None,
        *,
        extract_command_result: bool = False,
    ) -> AsyncGenerator[AcpEvent, None]:
        """Shared turn lifecycle for :meth:`prompt` and :meth:`stream_command`.

        Owns the concurrent-turn guard, the per-turn state resets, the pre-turn
        stale-frame drain, the request send, event dispatch, and turn-done
        bookkeeping. ``build_request`` returns the JSON-RPC ``(method, params)``
        to send; it runs BEFORE the turn is marked active (see the comment at
        the send site below).

        ``timeout=None`` (every dashboard turn) resolves from
        ``agent.chat_turn_timeout_secs`` so the transport wait follows a raised
        turn ceiling instead of cutting the turn at the 2h default underneath it.
        """
        timeout = await _effective_prompt_timeout_async(timeout)
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
        # Park state is per-turn: carrying it across would charge the previous
        # turn's consumer time to this one, and a permission left unanswered when
        # the last turn died would mask this turn's stalls forever.
        self._parked_total = 0.0
        self._parked_since = None
        self._awaiting_permission = False
        self._retire_liveness_state()
        self._working_logged_ts = _WORKING_NEVER_LOGGED
        self._tool_call_inputs.clear()
        self._tool_call_input_redacted.clear()
        self._tool_call_is_shell.clear()
        self._tool_call_raw_params.clear()
        self._tool_call_mcp_server.clear()
        self._tool_call_tool_name.clear()
        self._permission_options.clear()
        # Per-turn reset (parity with kiro-cli's authoritative full subagent_list
        # each turn): otherwise a completed sub-agent from a prior turn stays in
        # the roster and is re-emitted in the next turn's EVENT_SUBAGENT_LIST,
        # which the fresh per-turn _native_tracker resurrects as a duplicate
        # spawn/done card — and the roster would grow unbounded for the session.
        self._kas_subagent_roster.clear()

        # Drain frames left over from a prior abandoned turn. The cancel-unacked
        # / stale / tool-stall / timeout paths synthesize a terminal
        # EVENT_COMPLETE and return while the real kiro-cli turn keeps emitting
        # frames into this (unbounded) queue. Without draining, those leftover
        # tool_call/text_chunk/subagent frames bleed into THIS turn's stream and
        # the queue grows without bound. The abandoned turn's prompt response is
        # already skipped via is_response_for; its notifications are not, so drop
        # them here before the new turn begins.
        #
        # EXCEPTION — permission REQUESTS are answered, never dropped: a
        # server→client request discarded here strands the backend's response
        # oneshot and wedges the requesting (sub)agent's whole tool batch until
        # process teardown — the 2026-08-15 2h crew stall. A request stranded
        # from an abandoned turn (or routed here for a backend child between
        # turns) gets the fail-closed reject; the live turn's requests are
        # handled by the dispatch loop as before.
        # A DROPPED frame is invisible to every layer above: the abandoned turn's
        # output vanishes here with nothing to show it existed, and a turn that
        # loses its terminal this way reaches the dashboard as an empty response
        # with no attributable cause. Count them and say how many, ONCE. Never
        # what they were: a frame carries model text, tool arguments and tool
        # results, and none of that belongs in a log — nor its size, which leaks
        # response length. The count is bounded by the queue, and the log line is
        # one per turn regardless of how many frames drained.
        _stale_dropped = 0
        while True:
            try:
                stale = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if (
                stale is not None
                and stale.id is not None
                and stale.method is not None
                and stale.is_method(METHOD_REQUEST_PERMISSION)
            ):
                # Answer with the request's OWN advertised reject option: the
                # per-turn _permission_options map was just cleared, so a bare
                # reject_tool would always take its `cancelled` fallback —
                # which claude-agent-acp renders as "Tool use aborted" (reads
                # as a failure) instead of a clean policy denial. Repopulate
                # the map from the stranded frame's own options first.
                _stale_params = stale.params if isinstance(stale.params, dict) else {}
                _reject_id = reject_option_id(_stale_params)
                if _reject_id is not None:
                    self._permission_options[stale.id] = {"reject": _reject_id}
                _stale_sid = str(_stale_params.get("sessionId") or "")
                _stale_tc = _stale_params.get("toolCall")
                _stale_title = (
                    _stale_tc.get("title") if isinstance(_stale_tc, dict) else ""
                ) or "<unknown tool>"
                try:
                    await self.reject_tool(stale.id)
                except asyncio.CancelledError:
                    # The prompt was cancelled mid-drain: put the request back
                    # for the NEXT drain instead of dropping it un-answered
                    # (a dropped server→client request strands the backend's
                    # oneshot — the exact hang this drain exists to prevent).
                    self._queue.put_nowait(stale)
                    # _turn_done was already cleared for THIS turn, and the
                    # BaseException guard that would restore it wraps only
                    # the send_request below — re-raising from here without
                    # setting it would leave the handle permanently
                    # turn-active and every later prompt() rejected.
                    self._turn_done.set()
                    raise
                except Exception:
                    # A reject that failed to SEND left the child waiting on
                    # a stranded oneshot with no proof any answer reached the
                    # backend. The pipe cannot be trusted — escalate to the
                    # runtime's dead-marking so the child's wait dies with
                    # the process instead of hanging invisibly.
                    logger.exception("failed to answer stranded permission request")
                    _md = getattr(self._runtime, "_mark_dead", None)
                    if _md is not None:
                        _md("pre-turn drain reject failed")
                    continue
                logger.warning(
                    "rejected permission request id=%s stranded in the "
                    "pre-turn drain (abandoned turn or between-turns child "
                    "frame) — answering so the backend cannot hang",
                    stale.id,
                )
                # Crew-card notice ONLY for a child-origin strand: the card
                # keys on sub_session_id, so the parent's own session id (an
                # abandoned parent-turn request) can never match a card —
                # emitting it would name the wrong actor. The parent case is
                # covered by the WARNING + SEL record.
                if _stale_sid and _stale_sid != self._session_id:
                    self._pending_reject_notices.append((_stale_sid, str(_stale_title)))
                self._audit_handle_reject(
                    stale.id,
                    str(_stale_title),
                    "stranded_request_pre_turn_drain",
                    sub_session_id=(
                        _stale_sid if _stale_sid != self._session_id else ""
                    ),
                )
            else:
                # Everything that is not a permission request is DISCARDED, which
                # is correct (it belongs to a turn nobody is reading any more) but
                # was silent. Count it.
                _stale_dropped += 1

        if _stale_dropped:
            logger.warning(
                "pre-turn drain discarded %d leftover frame(s) from a prior "
                "abandoned turn on this session; those frames — possibly "
                "including that turn's terminal — reached no consumer",
                _stale_dropped,
            )

        self.last_prompt_stats = self.last_prompt_stats.carry_over()

        # send_request must be inside the turn-state guard: _turn_done was just
        # cleared above, so if the request raises (e.g. AcpRuntimeDead on a
        # broken pipe) before the try/finally below is entered, _turn_done would
        # stay cleared forever — is_turn_active would report True permanently and
        # every future prompt() on this handle would be rejected. Re-set it on
        # failure so the handle stays reusable.
        #
        # The guard catches BaseException, not Exception: asyncio.CancelledError
        # derives from BaseException, and BOTH awaits below are cancellation
        # points. A turn cancelled or timed out while the prompt is still being
        # assembled would otherwise wedge the handle permanently — the exact
        # failure this guard exists to prevent, just arriving by a different
        # exception hierarchy. Re-raised unchanged, so cancellation still
        # propagates.
        try:
            # Build the request FIRST (for prompts, the slow, cancellable
            # image-encoding part — see prompt()'s _build), then mark the turn
            # active immediately before the write: a child permission frame
            # read by the runtime between the write and the mark would
            # otherwise be auto-answered as "between turns" even though this
            # owner's turn had begun. Marking pre-build instead would claim an
            # active turn during a long image-encoding stint in which nothing
            # consumes the queue. The BaseException guard unmarks on any
            # failure so a dead write cannot leave the session permanently
            # routed-to.
            _method, _params = await build_request()
            _mark = getattr(self._runtime, "mark_turn_active", None)
            if _mark is not None:
                _mark(self._session_id, True)
            req_id = await self._runtime.send_request(_method, _params)
        except BaseException:
            self._turn_done.set()
            _mark = getattr(self._runtime, "mark_turn_active", None)
            if _mark is not None:
                _mark(self._session_id, False)
            raise

        # Did a terminal reach the consumer, and did this generator finish of its
        # own accord? Together these answer a question no layer above can: the
        # dashboard reads "no EVENT_COMPLETE" as an empty response and cannot tell
        # whether the backend never closed the turn or the consumer simply walked
        # away. Only a CLEAN exhaustion is reported, which is what makes the
        # warning spam-free: a consumer close (GeneratorExit), a cancellation, and
        # any raised error all leave `_exhausted_clean` False and are already
        # logged by whoever caused them.
        _yielded_terminal = False
        _exhausted_clean = False
        try:
            # Surface any drain-time rejections (see the pre-turn drain above)
            # as crew-card activity before the turn's own events — the user
            # sees WHY a child's tool failed instead of an unexplained error.
            # INSIDE the try/finally: these are yields, i.e. abandonment
            # points. A consumer that closes the stream at a notice yield
            # would otherwise skip mark_turn_active(False)/_turn_done and
            # wedge the handle as permanently turn-active.
            # Snapshot-and-clear BEFORE yielding: the rejects were already
            # sent, so a consumer that abandons the stream mid-notice must
            # not see the same notices replayed at the next turn's start.
            _notices = list(self._pending_reject_notices)
            self._pending_reject_notices.clear()
            for _n_sid, _n_title in _notices:
                yield AcpEvent(
                    kind=EVENT_SUBAGENT_ACTIVITY,
                    sub_session_id=_n_sid,
                    text=(
                        "⛔ permission auto-rejected (stranded between turns): "
                        f"{redact_text(str(_n_title)[:4096])[:120]}"
                    ),
                )
            async for event in self._dispatch_events(
                req_id, timeout, extract_command_result=extract_command_result
            ):
                # Park accounting. The consumer holds this event from here until
                # it comes back for the next one, and that interval is CONSUMER
                # time, not backend silence: the dispatch loop is suspended at
                # its own yield throughout, so its idle clocks would otherwise
                # charge a consumer-side await to the runtime. Measured at this
                # single choke point because `_dispatch_events` yields from 15
                # places and every one of them funnels through this `async for`.
                self._parked_since = time.monotonic()
                if event.kind == EVENT_COMPLETE:
                    # Set BEFORE the yield: a consumer that closes the stream ON
                    # the terminal still received it, and marking it after would
                    # report a lost terminal that was in fact delivered.
                    _yielded_terminal = True
                try:
                    yield event
                finally:
                    # `finally`, not a trailing statement: an abandoned generator
                    # unwinds with GeneratorExit and would otherwise leave
                    # `_parked_since` set forever, which reads from outside as a
                    # turn parked since the abandonment.  Guard against None:
                    # a turn boundary (line ~517) may reset _parked_since before
                    # a lingering generator's finally fires on GC.
                    if self._parked_since is not None:
                        self._parked_total += time.monotonic() - self._parked_since
                        self._parked_since = None
            # Reached only when the dispatch loop returned on its own — not on a
            # close, a cancel, or an exception.
            _exhausted_clean = True
        finally:
            if _mark is not None:
                _mark(self._session_id, False)
            if not self._turn_done.is_set():
                self._turn_done.set()
            if _exhausted_clean and not _yielded_terminal:
                # The dispatch loop synthesizes a terminal on every path it knows
                # about (timeout, stale, tool stall, cancel-unacked), so reaching
                # here means one of its exits has none — and the consumer is left
                # deciding what an unclosed turn means. Content-free by
                # construction: this line carries no count, no text and no ids,
                # because the only fact it has to report is that it happened.
                logger.warning(
                    "prompt stream for this session ended without a terminal "
                    "completion event; the caller will see the turn as producing "
                    "nothing"
                )

    # ── Turn park state (readable from OUTSIDE the turn) ──

    def parked_for_secs(self) -> float:
        """Seconds the consumer has been holding the current event; 0.0 if not parked.

        This is the one signal the in-band watchdog structurally cannot report on
        itself. That watchdog is the ``except asyncio.TimeoutError`` arm of
        :meth:`_dispatch_events`, an async generator, so it only advances when a
        consumer pulls it — a consumer that awaits inside its own ``async for``
        body freezes the generator at the yield and the arm never executes again
        for the rest of the turn. It is not slow or mis-configured there; it is
        not called. An observer with its own timer reads this instead.
        """
        since = self._parked_since
        if since is None:
            return 0.0
        # Clamped: a monotonic clock cannot go backwards, but a negative duration
        # leaking into a caller's threshold comparison would read as "not parked".
        return max(0.0, time.monotonic() - since)

    @property
    def parked_since(self) -> float | None:
        """Monotonic timestamp the current park began, or None if not parked.

        Exposed so an observer can latch on a park's IDENTITY rather than its
        duration: a park that outlives the observer's tick would otherwise be
        re-reported on every pass.
        """
        return self._parked_since

    @property
    def awaiting_permission(self) -> bool:
        """True while a permission event has been yielded and not yet answered.

        A turn parked here is waiting for a HUMAN, which is not a stall: that wait
        is already bounded by ``agent.tool_approval_timeout_secs``. An external
        observer must exclude it — otherwise every approval prompt reads as a
        stalled turn, and two components end up racing to end the same wait on
        different budgets.
        """
        return self._awaiting_permission

    def _end_human_wait(self) -> None:
        """Close the human-wait segment of the current park.

        The consumer is still parked when a permission is answered — it resolves
        the approval and then finishes its own branch (an IM send, a hook, a
        transcript write) before coming back for the next event. Banking the wait
        into ``_parked_total`` and restarting ``_parked_since`` keeps the in-band
        correction exact (it wants the WHOLE park, all of which was consumer time
        from the runtime's point of view) while making ``parked_for_secs()``
        measure only what the consumer itself has spent since the answer.

        Without this the observer reports a park whose duration is almost
        entirely the human's thinking time — the same misattribution the in-band
        clocks were fixed to avoid, reappearing one layer out.
        """
        self._awaiting_permission = False
        if self._parked_since is not None:
            now = time.monotonic()
            self._parked_total += max(0.0, now - self._parked_since)
            self._parked_since = now

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
        # Answered — the turn is no longer waiting on a human. Also closes the
        # human-wait segment of the park so an observer does not attribute the
        # person's thinking time to the consumer (see _end_human_wait).
        self._end_human_wait()
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
        ``cancelled`` when no deny-shaped option was advertised — NOT a per-tool
        signal: kiro-cli maps it to cancelling the TURN, auto-denying every
        later tool call in it without prompting (#7681).
        """
        recorded = self._permission_options.pop(request_id, None)
        # Answered (see approve_tool) — a rejection ends the human wait too.
        self._end_human_wait()
        reject_id = recorded.get("reject") if recorded else None
        if reject_id:
            await self._runtime.send_response(
                request_id,
                {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": reject_id}},
            )
        else:
            # Same last-resort warning as AcpClient.reject_tool — this is the
            # second of the two ``cancelled`` fallback sites, and the silent
            # cascade is the bug report's whole complaint (#7681).
            logger.warning(
                "reject_tool: no deny option advertised for req=%s; answering "
                "'cancelled', which the backend may treat as cancelling the "
                "remainder of the turn's tool calls",
                request_id,
            )
            await self._runtime.send_response(
                request_id,
                {"outcome": {"outcome": OUTCOME_CANCELLED}},
            )

    def _audit_handle_reject(
        self,
        request_id: str | int | None,
        title: str,
        error: str,
        sub_session_id: str = "",
    ) -> None:
        """SEL-audit a permission request this handle rejected ITSELF.

        The fail-close fidelity gate and the pre-turn drain answer requests
        that never reach a consumer, so no consumer-side audit fires — every
        permission decision must still leave a SEL record (repo convention;
        the runtime's unregistered-session auto-reject does the same).
        Off-loop (``asyncio.to_thread``) and AFTER the reject was sent: sel()
        may do blocking filesystem work on first use, and an audit failure
        must not undo or delay the already-made decision. Title is
        backend/LLM-authored: bounded then redacted before it is stored.
        """
        safe_title = redact_text(str(title)[:4096])[:120] if title else "<unknown>"
        rid = request_id if isinstance(request_id, (str, int)) else ""
        # Hang-resilience series: handle-owned denials (fail-close fidelity
        # gate, pre-turn drain). CHILD-origin only — the pre-turn drain also
        # answers abandoned PARENT-turn requests (sub_session_id empty), and
        # counting those would corrupt the child-denial series.
        if sub_session_id:
            emit_counter(
                CHILD_PERMISSION_DENIED,
                {"surface": "session_handle", "reason": error},
            )

        def _audit() -> None:
            try:
                from kiro_crew.sel import sel

                sel().log_tool_invocation(
                    # Child rejections carry the CHILD's id in the key —
                    # attributing them only to the parent would erase the
                    # traceability the audit exists to provide.
                    session_key=(
                        f"acp:{self._session_id}:{sub_session_id}"
                        if sub_session_id
                        else f"acp:{self._session_id}"
                    ),
                    agent="kirocrew",
                    source="acp_session_handle",
                    tool_name=safe_title,
                    outcome="denied",
                    request_id=rid,
                    error=error,
                )
            except Exception:
                logger.exception("SEL audit for handle-rejected permission failed")

        audit_task = asyncio.ensure_future(asyncio.to_thread(_audit))
        self._audit_tasks.add(audit_task)
        audit_task.add_done_callback(self._audit_tasks.discard)

    # ── Session Configuration ──

    async def set_mode(self, agent_name: str) -> None:
        """Activate an agent via session/set_mode."""
        await self._runtime.send_request(
            METHOD_SET_MODE,
            set_mode_params(self._session_id, agent_name),
        )

    async def set_model(self, model_id: str) -> None:
        """Switch model via session/set_model.

        This is the shared-runtime SUBSTITUTE path (background one-liners, tips,
        contradiction sweep, and any caller that did not pre-guard an explicit
        user pick). ``resolve_usable_model`` maps the request to what the account
        can run: a served id is sent; ``"auto"`` is sent only when the backend
        advertises it; and anything else — ``"auto"`` on a partition that doesn't
        serve it, or an unentitled concrete id — resolves to ``""``,
        meaning **inherit the session's backend default** (the served model
        ``session/new`` assigned). So this path never puts an unserved model on
        the wire, exactly like the interactive ``_wire_model_id``
        reset-to-default. Explicit user picks raise instead, upstream in
        ``AcpSessionProvider.set_model`` / ``AcpClient.set_model``.
        """
        resolved = resolve_usable_model(model_id, self._advertised_model_ids())
        if not resolved:
            # Inherit the backend default — nothing to send. For the ephemeral
            # _bg session the current model IS session/new's served default.
            return
        if self._runtime.acp_backend == ACP_BACKEND_KAS:
            # KAS implements no ``session/set_model``; the model is one of its
            # session config options instead. Same effect, different verb — so
            # the bookkeeping below is shared rather than duplicated.
            await self.set_config_option(MODEL_CONFIG_ID, resolved)
        else:
            await self._runtime.send_request(
                METHOD_SET_MODEL,
                set_model_params(self._session_id, resolved),
            )
        self._model = resolved
        # Parity with AcpClient.set_model: keep _resolved_model_id in sync so
        # _backfill_context_window looks up the NEW model's window after a switch
        # (otherwise the context meter converts pct against the stale session/new
        # model until the next session refresh).
        self._resolved_model_id = resolved
        # Also rebase the meter stats themselves — the old model's window and
        # its authoritative usage_update no longer describe this session
        # (mirrors AcpClient.set_model).
        win = (
            model_registry.model_window(resolved)
            if model_registry.has_known_window(resolved)
            else None
        )
        self.last_prompt_stats.rebase_to_window(win or 0)

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
        # Stamped HERE, at the innermost write, because this is the one point
        # every steer funnels through: the dashboard steers the inner client
        # directly while the IM transports steer the provider wrapper, and both
        # end up on this line. A reader of the stamp therefore needs no
        # per-transport wiring. See ``last_steer_monotonic``.
        self._last_steer_monotonic = time.monotonic()
        return True

    # Monotonic stamp of the last steer handed to the backend, 0.0 when this
    # session has never been steered. Read by the dashboard's keepalive route to
    # decide whether a sleeping `wait` should return early: a steer can only be
    # injected at a model-inference boundary, and an in-flight tool call is the
    # absence of one, so a sleep that outlasts the steer would hold the user's
    # correction in the backend's queue until it elapses.
    #
    # Deliberately monotonic, not wall clock: it is only ever compared against
    # another monotonic stamp taken in the same process (the sleep's start), and
    # mixing the two clocks is how a suspend-resume silently reorders them.
    _last_steer_monotonic: float = 0.0

    @property
    def last_steer_monotonic(self) -> float:
        """Monotonic time of the last steer written to the backend (0.0 if none)."""
        return self._last_steer_monotonic

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
        """Trigger context compaction via a ``/compact`` prompt.

        Sent through ``session/prompt`` (drained to turn end), NOT through
        ``_kiro.dev/commands/execute``: kiro-cli 2.14.0 exits rc=0 without a
        response on the STRING form of commands/execute (live-probe confirmed
        for /compact and /help alike; the object form used by /effort is
        fine). The prompt transport matches the dashboard's manual /compact
        and Slack's !compact: kiro ACKs the prompt (end_turn) and then emits
        ``_kiro.dev/compaction/status``, which ``wait_for_compaction()``
        picks up from the session queue.
        """
        cmd = "/compact"
        if context:
            cmd = f"/compact {context}"
        # Capture a terminal status emitted MID-TURN (before end_turn) while
        # draining — otherwise it would be consumed and lost, stranding a
        # subsequent wait_for_compaction() until timeout even though the
        # compact succeeded. wait_for_compaction() consumes this cache first.
        self._compact_result = None
        async for event in self.prompt(cmd):
            if event.kind == EVENT_COMPACTION_STATUS and event.text in (
                "completed",
                "failed",
            ):
                self._compact_result = {
                    "type": event.text,
                    "summary": event.title or "",
                }

    async def wait_for_compaction(
        self, timeout: float = COMPACT_WAIT_TIMEOUT_SECS
    ) -> dict[str, str]:
        """Wait for compaction completed/failed event from the session queue.

        Returns {"type": "completed"|"failed"|"timeout", "summary": "..."}.
        Consumes the result compact() captured mid-turn if there is one,
        otherwise drains the queue looking for COMPACTION_STATUS
        notifications (the async-after-end_turn case).
        """
        cached = self._compact_result
        if cached is not None:
            self._compact_result = None
            if cached.get("type") == "completed":
                # The dispatch loop reset the stats when it captured this
                # mid-turn; kiro's fresh post-compaction metadata arrives ~1s
                # after the completed status — wait briefly so callers can
                # broadcast the REAL compacted usage instead of the unknown
                # fallback.
                await self._drain_post_compaction_metadata()
            return cached
        deadline = time.monotonic() + timeout
        # ONE buffer for this call AND the nested grace drain, restored at ONE
        # point (the finally below) strictly BEFORE any re-poison. Separate
        # buffers restored at different times invert the order around a death
        # sentinel: the nested drain would re-queue ``None`` while this frame
        # buffer was still held, so a concurrent command's already-received
        # response would land BEHIND the poison and its consumer would see
        # process death despite a completed command.
        buffered: list[JsonRpcMessage] = []
        poisoned = False
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
                    # Re-poison (in the finally, AFTER the buffered frames are
                    # restored) so the live turn / next consumer also sees death.
                    poisoned = True
                    raise AcpProcessDied("Runtime died while waiting for compaction")
                # Check for compaction status
                if msg.method == "_kiro.dev/compaction/status":
                    params = msg.params or {}
                    status = params.get("status", {})
                    s_type = status.get("type", "") if isinstance(status, dict) else str(status)
                    if s_type in ("completed", "failed"):
                        if s_type == "completed":
                            # This drain path bypasses the prompt dispatch
                            # loop, so it must drop the stale counts itself —
                            # mirrors AcpClient._handle_compaction_status.
                            self.last_prompt_stats.reset_after_compaction()
                            poisoned = await self._drain_post_compaction_metadata(
                                buffered=buffered
                            )
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
            if poisoned:
                self._queue.put_nowait(None)

    async def _drain_post_compaction_metadata(
        self,
        grace: float = _POST_COMPACTION_METADATA_GRACE_SECS,
        buffered: list[JsonRpcMessage] | None = None,
    ) -> bool:
        """Drain the session queue for kiro's post-compaction metadata.

        kiro-cli emits a fresh ``_kiro.dev/metadata`` with the real
        post-compaction ``contextUsagePercentage`` about a second after the
        ``completed`` status (live-probe confirmed). The compaction reset
        cleared the authoritative flag, so applying it re-derives accurate
        counts against the kept served window. Returns on the first metadata
        frame carrying a real percentage — a credits-only/empty metadata frame
        is consumed but does not end the drain (the usage frame behind it
        would be stranded). Gives up quietly at the grace deadline.

        ``buffered``: when the caller (``wait_for_compaction``) passes its own
        frame buffer, non-metadata frames are appended to it and the CALLER
        restores everything at one point before any re-poison — two buffers
        restored at different times would invert the order around a death
        sentinel and strand the caller's frames behind the ``None``. Without
        a shared buffer this method restores (and re-poisons) itself.
        Returns True when the poison sentinel was consumed, so a sharing
        caller re-queues it after the single restore.
        """
        own_buffer = buffered is None
        frames: list[JsonRpcMessage] = [] if buffered is None else buffered
        deadline = time.monotonic() + grace
        poisoned = False
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if msg is None:
                    poisoned = True
                    return True
                if msg.method == "_kiro.dev/metadata":
                    mparams = msg.params or {}
                    if mparams.get("meteringUsage"):
                        # Late compaction credits. This drain runs BETWEEN
                        # turns on the auto-compact path — credits tracked
                        # here land in a stats window nothing reads and the
                        # next prompt's re-init wipes them. Pass the frame
                        # through untouched instead: the re-queue hands it to
                        # the next turn's dispatch loop, which bills it like
                        # any other metering frame (the pre-drain behavior).
                        frames.append(msg)
                        continue
                    self._track_metadata(msg)
                    if mparams.get("contextUsagePercentage") is not None:
                        return False
                    continue
                frames.append(msg)
            return False
        finally:
            if own_buffer:
                for _m in frames:
                    self._queue.put_nowait(_m)
                if poisoned:
                    self._queue.put_nowait(None)

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
    def served_model(self) -> str:
        """Backend-resolved model id serving this session (``""`` until known).

        Prefers the explicit ``set_model`` assignment (``_model``), falling
        back to the ``session/new|load`` response's ``currentModelId``
        (``_resolved_model_id``) so a session running on the backend-selected
        DEFAULT is still readable — ``_model`` stays ``""`` on that path.
        Both sources are backend-confirmed; the requested alias is never
        reported here. May be a profile-form id, which is a valid wire id.
        """
        return self._model or self._resolved_model_id

    @property
    def agent_version(self) -> str:
        """The version the shared process RUNS (``""`` until its handshake).

        Delegates to the runtime because the handshake is per process, not per
        session: every handle on one runtime reports the same value.
        """
        return self._runtime.agent_version

    @property
    def config_options(self) -> list[dict[str, Any]]:
        """ACP-reported configOptions (effort, model, mode selectors)."""
        return self._config_options

    @property
    def available_models(self) -> list[dict[str, str]]:
        """Models advertised by the backend at session init."""
        return list(self._available_models)

    def _advertised_model_ids(self) -> list[str]:
        """Advertised model ids, for the model-rejection error path.

        Parity with ``AcpClient._advertised_model_ids``. Empty when the backend
        advertised nothing (no session yet, or a backend that omits ``models``),
        which the error path reads as "entitlement unknown" and leaves the
        transient/capacity handling alone.

        This handle is the shared-runtime path every dashboard chat takes, so
        without it the entitlement discrimination in ``_model_is_unentitled``
        would only ever fire for direct-spawn ``AcpClient`` sessions.
        """
        ids = []
        for entry in self._available_models:
            model_id = entry.get("modelId") if isinstance(entry, dict) else None
            if isinstance(model_id, str) and model_id.strip():
                ids.append(model_id)
        return ids

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

    def rebind_watchdog(
        self, crew_agent: str, settings: WatchdogSettings | None = None
    ) -> None:
        """Re-snapshot the watchdog windows for a new canonical crew identity.

        Called on warm-pool rekey: the pooled runtime was spawned before any
        crew claimed it, so the construction-time snapshot cannot know the
        claiming crew's ``watchdog_tool_stall_*`` overrides — the identity
        travels with the SESSION, not the pool key. The dispatch loop reads
        ``self._watchdog`` on every tick, so the swap takes effect at the next
        watchdog check; an empty ``crew_agent`` (a claim with no crew) rebinds
        to the globals so a recycled runtime never carries a previous crew's
        windows. The oracle keeps its per-session evidence state — only its
        sampling floor follows the new snapshot.

        ``settings`` is the pre-resolved snapshot: an ASYNC caller (the
        warm-pool claim) resolves it off-loop and hands it in, making the
        no-event-loop-I/O property an explicit data dependency rather than a
        cache-timing contract; None loads synchronously (config-cache hit in
        practice) for callers without an off-loop path.
        """
        self._crew_agent = crew_agent
        self._watchdog = settings if settings is not None else _load_watchdog_settings(crew_agent)
        self._oracle._sample_min_secs = self._watchdog.wellness_sample_secs

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
                # The shape walk is delegated to the canonical parser so this
                # snapshot and a probe's answer stay directly comparable
                # (#6382). The envelope is the same checked-binding discipline
                # as the client's, though here the parser's dict-or-list
                # fallback is unreachable by construction (the inner dict
                # always has a key). The isinstance check above is the
                # assignment gate: a non-list ``availableModels`` must not
                # clobber a previously-stored list. A well-formed EMPTY list
                # still overwrites — that asymmetry with
                # ``AcpClient._capture_available_models`` (non-empty guard) is
                # pre-existing policy, deliberately unchanged here.
                self._available_models = parse_advertised_models(
                    {"models": {"availableModels": avail}}
                )
            # A backend may advertise its model list without echoing
            # ``currentModelId`` (it is best-effort in the ACP shape). When it
            # names exactly one model that IS the served model unambiguously, so
            # adopt it as the resolved id — otherwise ``served_model`` reports
            # ``""`` for the whole session on an unpinned run (no ``set_model``,
            # no ``currentModelId``), which is why the panel's model chip stays
            # blank until completion fills it from a different source. With two
            # or more advertised and no ``currentModelId`` the served choice is
            # genuinely unknown, so leave it empty rather than guess.
            if not self._resolved_model_id and len(self._available_models) == 1:
                self._resolved_model_id = self._available_models[0]["modelId"]
        elif isinstance(models, list):
            self._available_models = parse_advertised_models({"availableModels": models})

    @staticmethod
    def _normalize_models(advertised: list[Any]) -> list[dict[str, str]]:
        """Normalize advertised models to ``{modelId, name, description}`` with
        guaranteed keys — the canonical normalization. Every consumer
        (``parse_advertised_models``, and through it ``store_session_config``,
        ``AcpClient._capture_available_models``, and the pooled-runtime
        entitlement probe ``AcpRuntime.probe_advertised_models``) shares this
        shape, so probe answers and session-init snapshots are directly
        comparable and the dashboard model dropdown gets a stable shape
        regardless of backend."""
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

    async def refresh_available_models(self) -> list[dict[str, str]]:
        """Re-resolve the advertised-model snapshot against the live backend.

        ``_available_models`` is otherwise written once, from this session's own
        ``session/new`` — an answer the backend resolved from the account state
        it held at that instant. When that answer was degraded (a lookup racing
        a token refresh answers with the default tier), the session refuses
        models the account actually has, for its whole life. Every consumer of
        entitlement — the explicit-pick refusal, the prompt-time pin withhold,
        the dashboard picker filter — reads through this handle's snapshot, so
        one refresh heals them all.

        The snapshot is replaced only by a NON-EMPTY probe result: a failed or
        empty probe is not evidence about entitlement, so the prior snapshot is
        kept. Returns the probe result either way, so callers can distinguish
        "revalidated" from "could not revalidate".
        """
        fresh = await self._runtime.probe_advertised_models()
        if fresh:
            self._available_models = list(fresh)
        return fresh

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
        ALWAYS unregisters the queue, so teardown neither hangs nor fails on a
        dead or slow runtime -- but see the `finally` below for the one
        exception it cannot swallow.

        Each session on a shared runtime (a ``_bg`` op or a session-sharing
        subagent) is a distinct ``session/new`` with its own persisted
        ``~/.kiro/sessions/cli/{sid}.json``(+``.jsonl``). The shared runtime is
        not killed on teardown, so we also delete the transcript here — otherwise
        these files would accumulate for the gateway lifetime (titles/
        suggestions/folders/nav run on nearly every chat). Only ephemeral
        sessions call destroy(): main-chat sessions are torn down via
        ``owns_runtime=True`` → ``runtime.kill()`` and intentionally keep their
        transcript for ``session/load`` resume, so cleaning up here is safe.

        Exception: ``keep_transcript=True`` (set by SubagentManager before
        teardown) skips the transcript deletion — subagent transcripts are the
        resume material for ``spawn_continue`` and are lifecycle-managed by the
        tombstone pruner / conversation TTL sweep instead. ``terminate_session``
        still runs unconditionally: it is the RSS reclaim on the multiplexed
        process; only the unlink is deferred.
        """
        # The unlink runs in a `finally`, for the same reason
        # `terminate_session` unregisters the queue in one: that method swallows
        # `Exception`, but `asyncio.CancelledError` is a `BaseException` and
        # propagates straight out of the await. Cancellation is exactly when
        # teardown runs -- gateway shutdown, an abandoned turn -- so the
        # sequential form skipped the cleanup on the path that produces the most
        # of these files, and every survivor is permanent: nothing else deletes
        # an ephemeral session's transcript.
        try:
            await self._runtime.terminate_session(self._session_id)
        finally:
            if not getattr(self, "keep_transcript", False):
                self._cleanup_transcript()

    def _cleanup_transcript(self) -> None:
        """Best-effort delete of this session's kiro-cli transcript files.

        A NO-OP on the KAS backend: this unlinks from kiro-cli's sessions dir and
        KAS keeps its own store, so nothing here matches. The ``keep_transcript``
        guard therefore protects nothing on KAS — that backend's session record is
        already gone, removed by the same verb that freed the session.
        """
        sid = self._session_id
        if not sid:
            return
        sessions_dir = kiro_sessions_dir().resolve()
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
                        # Delegate to the shared raise helper so this path gets
                        # the SAME treatment as AcpClient: actionable prose from
                        # _format_acp_error (model-unavailable / throttle / auth /
                        # 5xx), credential+URL redaction, the transient= verdict
                        # for the chat_runner / llm_helpers retry ladder, and the
                        # AcpPromptBusy subclass for a concurrent in-flight
                        # prompt. Raising a bare f"ACP error: {msg.error}" here
                        # put the raw JSON-RPC dict in front of the user.
                        # The advertised ids let the shared entitlement
                        # discriminator tell "your plan lacks this model"
                        # (terminal) from a capacity blip (retryable).
                        _raise_acp_error(msg.error, self._advertised_model_ids())
                    return msg
                # Not our response — buffer (do not drop) for re-injection,
                # and advance the ingress sequence for EVERY buffered frame:
                # the TOCTOU guard in _dispatch_events snapshots it before the
                # oracle await and compares after, so frames consumed by this
                # concurrent waiter are still detected regardless of queue
                # depth at the time of the check. Responses count too — a
                # buffered response can be the prompt turn's own terminal
                # frame, and skipping it let the watchdog cancel a turn whose
                # completion was sitting in this buffer. A spurious bump only
                # defers one watchdog tick, so over-counting is fail-safe.
                self._ingress_seq += 1
                buffered.append(msg)
            raise AcpTimeoutError(f"Timeout waiting for response to request {req_id}")
        finally:
            self._awaited_responses.discard(req_id)
            for _m in buffered:
                self._queue.put_nowait(_m)

    async def _dispatch_events(
        self, req_id: int, timeout: float, *, extract_command_result: bool = False
    ) -> AsyncIterator[AcpEvent]:
        """Core event dispatch loop. Yields AcpEvent objects from the session queue.

        ``extract_command_result`` (commands/execute turns): the command's
        output arrives in the RESPONSE result rather than as session/update
        chunks — surface it as a text chunk before the terminal event.
        """
        deadline = time.monotonic() + timeout
        last_data_ts = time.monotonic()
        # Cleared per turn: armed by the compaction branch below on a
        # `failed` status, then read by the post-failure budget check at the
        # loop top (mirrors AcpClient._prompt_loop).
        self._compaction_failed_at = None
        # Whether a native agent-switch notification already reported the
        # switch this turn; guards the result-extracted fallback below from
        # double-emitting EVENT_AGENT_SWITCHED (mirrors AcpClient).
        saw_agent_switch = False
        # Consumer time already accounted for at the moment `last_data_ts` was
        # taken. The idle clocks below measure BACKEND silence, so any park that
        # happens after this point must be subtracted from them.
        parked_at_data = self._parked_total
        # SESSION-ATTRIBUTABLE twin of (last_data_ts, parked_at_data), read by
        # the post-compaction-failure budget AND the tool-idle watchdog. On a
        # shared runtime, ownerless global notifications are fanned out to every
        # co-tenant queue (msg.fanout_no_owner), so another session's steady
        # traffic would keep resetting last_data_ts and defer both clocks on
        # work this session never produced — for the budget, out to the
        # multi-hour outer deadline that is the exact hang it exists to bound.
        #
        # The STALE clock deliberately keeps reading last_data_ts: it already
        # folds in the runtime-wide _last_activity (bumped on every stdout
        # line), so runtime-global traffic is inside its contract by
        # construction and narrowing its queue term would change nothing.
        last_own_data_ts = last_data_ts
        parked_at_own_data = parked_at_data

        _buffered: list[JsonRpcMessage] = []
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                # Post-compaction-failure budget: automatic compaction reported
                # `failed` and the backend has since gone silent past the budget,
                # so no prompt response or end_turn is coming. End the turn with
                # an explicit stop reason so the caller releases the slot instead
                # of draining to the chat-turn ceiling (issue #3583). Consumer
                # park time is subtracted, like every other idle clock here, so a
                # long human approval cannot be charged to the backend.
                #
                # SUSPENDED while a tool is in flight, for the same reason as
                # AcpClient's twin: a silent long tool dispatched after the failed
                # compaction is live work, and reaping it at 60s would cancel the
                # session out from under it. The tool-stall watchdog below owns
                # that case; this budget re-arms when the tool resolves.
                #
                # Clock asymmetry with AcpClient's twin is INTENTIONAL: the shared
                # runtime fans ownerless frames out to every co-tenant, so this
                # side keys off SESSION-ATTRIBUTABLE frames (last_own_data_ts)
                # where the dedicated-process side can trust last_data_ts.
                if self._compaction_failed_at is not None and not self._tool_dispatched:
                    _compact_idle = max(
                        0.0,
                        (time.monotonic() - max(last_own_data_ts, self._compaction_failed_at))
                        - max(0.0, self._parked_total - parked_at_own_data),
                    )
                    if _compact_idle > _COMPACTION_FAILED_TURN_BUDGET:
                        logger.warning(
                            "Compaction failed on session %s and no prompt response "
                            "arrived for %.0fs — ending the turn.",
                            self._session_id,
                            _compact_idle,
                        )
                        self._compaction_failed_at = None
                        self._turn_done.set()
                        yield AcpEvent(
                            kind=EVENT_COMPLETE,
                            stop_reason=STOP_REASON_COMPACTION_FAILED,
                            usage=self.last_prompt_stats.to_turn_usage(),
                        )
                        return

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
                                       usage=self.last_prompt_stats.to_turn_usage())
                        return
                    logger.warning(
                        "Cancel unacked after %.1fs on session %s — unblocking caller "
                        "(runtime kept alive for co-tenants)",
                        self._cancel_grace_secs, self._session_id,
                    )
                    yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="error: cancel unacked",
                                   usage=self.last_prompt_stats.to_turn_usage())
                    return

                try:
                    msg = await asyncio.wait_for(
                        self._queue.get(), timeout=min(remaining, 5.0)
                    )
                except asyncio.TimeoutError:
                    # ── Verdict-driven watchdogs ──
                    # Wellness (the liveness oracle) is the detector; timeouts
                    # govern only the UNKNOWN class. Idle clocks: the stale clock
                    # folds in the runtime's activity clock (_last_activity —
                    # advanced by stdout lines, outbound requests and
                    # notifications, and the /api/session-keepalive touch, but
                    # NOT by a response or error we send back, and NOT by
                    # stderr: AcpRuntime's stderr drain only rings the
                    # _stderr_lines buffer, so a kiro-cli reasoning burst on
                    # stderr does not move this clock); the tool clock keys off
                    # this session's OWN queue frames only (keepalive and
                    # progress frames for the session reset last_own_data_ts, so
                    # a legitimately-streaming tool keeps the watchdog
                    # satisfied, while a co-tenant's ownerless fanned-out frame
                    # does not defer it).
                    if self._cancelled:
                        continue
                    wd = self._watchdog
                    now = time.monotonic()
                    # Consumer time since the last frame. Both clocks below are
                    # meant to measure how long the RUNTIME has been silent, but
                    # the loop is suspended at its yield for the whole of a
                    # consumer-side await, so without this the wait a consumer
                    # spends on an approval, an IM send, or a hook is charged to
                    # the backend — and a turn can be cancelled moments after a
                    # human approves it. Subtracted rather than clamped forward so
                    # a burst of short parks accumulates correctly.
                    _parked = max(0.0, self._parked_total - parked_at_data)

                    if self._tool_dispatched:
                        # Session-attributable clock: an ownerless fanned-out
                        # frame is at most one co-tenant's traffic and nothing
                        # says whose, so it must not stand in for progress on
                        # THIS session's in-flight tool.
                        #
                        # Its park correction is taken from the matching
                        # baseline. Reusing `_parked` (measured from the newer
                        # last_data_ts) would leave a park between the two
                        # baselines unsubtracted, inflating idle and making this
                        # branch QUICKER to cancel a live turn — the one
                        # direction a clock change here must never take by
                        # accident.
                        _own_parked = max(0.0, self._parked_total - parked_at_own_data)
                        _tool_idle = max(0.0, (now - last_own_data_ts) - _own_parked)
                        if _tool_idle <= wd.check_after_secs:
                            continue
                        # F2 — TOCTOU guard: two complementary signals cover
                        # the two delivery paths for a frame that arrives DURING
                        # the oracle await (up to 10 s in an executor, event
                        # loop yielded).
                        #
                        # Path A — no concurrent _wait_for_response: the frame
                        # sits in _queue until the dispatch loop consumes it.
                        # qsize() advances when the frame lands.
                        #
                        # Path B — concurrent _wait_for_response: it dequeues
                        # the frame (qsize unchanged), buffers it, and re-
                        # injects it later. _ingress_seq (incremented in
                        # _wait_for_response for notification frames) advances.
                        #
                        # Combining both signals means an UNKNOWN-over-window
                        # cancel cannot fire while real activity is in-flight on
                        # either delivery path.
                        _ingress_before = self._ingress_seq
                        _q_depth_before = self._queue.qsize()
                        verdict, evidence = await self._consult_oracle_offloaded(model_wait=False)
                        # TOCTOU recheck — activity on either path prevents the cancel.
                        if (
                            self._ingress_seq != _ingress_before
                            or self._queue.qsize() > _q_depth_before
                        ):
                            # Both clocks advance. Neither signal can name the
                            # arriving frame's owner (it has not been dequeued),
                            # so this deliberately keeps the guard's existing
                            # fail-safe over-count — the same trade _ingress_seq
                            # already documents at its increment. A co-tenant
                            # frame landing inside the oracle await therefore
                            # still defers the tool clock, but by ONE tick: when
                            # it is dequeued the ownership check below leaves
                            # last_own_data_ts alone, so the unbounded deferral
                            # this branch used to allow cannot re-form.
                            last_data_ts = time.monotonic()
                            last_own_data_ts = last_data_ts
                            parked_at_own_data = self._parked_total
                            continue
                        if verdict == VERDICT_WORKING:
                            self._log_working_deferral(_tool_idle, evidence, timeout)
                            continue
                        # UNKNOWN acts at the suspect window. The suspect
                        # default (1h) is BUILD-scale forbearance — an LLM-shaped
                        # stall (flat subtree whose only live evidence is an
                        # established backend socket: a model turn riding inside
                        # a tool, e.g. kiro-cli use_subagent) narrows to the
                        # model-silent budget, because its longest legitimate
                        # silent gap is minutes, not hours. Keyed STRICTLY on
                        # the oracle's evidence TAGS — established_flat, or
                        # shell_child_absent for a shell command with no process
                        # to its name. Untagged evidence (a quiet build's
                        # unmatched-but-live tree, a quiet MCP tool) keeps the
                        # full window.
                        # F3 — hard cap: watchdog_tool_stall_hard_cap_secs is
                        # the absolute ceiling for UNKNOWN forbearance. Apply
                        # min(suspect_window, hard_cap) so the configured cap
                        # always bounds the effective window. WORKING deferred
                        # unconditionally above; DEAD/STUCK_INPUT act
                        # immediately regardless of the window.
                        # WORKING was already deferred above; the action below
                        # is the existing non-lethal tool-stall recovery.
                        _suspect = wd.tool_stall_suspect_secs
                        _narrowed = evidence.startswith(EVIDENCE_ESTABLISHED_FLAT)
                        if _narrowed:
                            _suspect = min(wd.model_silent_probe_secs, _suspect)
                        elif evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT):
                            # The oracle can see the runtime's tree and nothing
                            # in it is young enough to be this dispatch's child:
                            # the shell command is not running. Build-scale
                            # forbearance exists for a QUIET build, not for an
                            # absent one — a sub-second command whose result
                            # frame was lost is never observed alive, so without
                            # this it collects the full suspect window while the
                            # matched-then-gone fork of the same state acts on
                            # CHILD_EXIT_GRACE_SECS. Narrowed to the ordinary
                            # silence window rather than to that grace: absence
                            # is inferred from start times, so the verdict stays
                            # UNKNOWN and the action stays the non-lethal
                            # session cancel at a few minutes.
                            _narrowed = True
                            _suspect = min(wd.stale_window_secs, _suspect)
                        _suspect = min(_suspect, wd.tool_stall_hard_cap_secs)
                        _acting = (
                            verdict in (VERDICT_DEAD, VERDICT_STUCK_INPUT)
                            or _tool_idle > _suspect
                        )
                        if not _acting:
                            continue  # UNKNOWN, within budget — keep waiting
                        self._emit_watchdog_metric(
                            "cancel", verdict, evidence, _tool_idle,
                            window="narrowed" if _narrowed else "standard",
                        )
                        async for ev in self._end_stalled_tool(verdict, evidence, _tool_idle):
                            yield ev
                        return

                    if self._stale_eligible:
                        # `_parked` is measured from the last QUEUE frame, while
                        # this clock can key off the newer stderr/keepalive
                        # activity instead. When it does, some of `_parked`
                        # predates the reference point and is subtracted twice
                        # over — which only ever makes this branch MORE patient,
                        # never quicker to probe, so it errs toward leaving a
                        # working turn alone.
                        _stale_idle = max(
                            0.0,
                            (now - max(last_data_ts, self._runtime._last_activity)) - _parked,
                        )
                        if _stale_idle <= wd.check_after_secs:
                            continue
                        # TOCTOU guard — the tool branch's two frame-path
                        # signals PLUS the runtime activity clock, because this
                        # branch's idle measurement (unlike the tool clock)
                        # folds in _last_activity: snapshot all three before
                        # the oracle await (up to 10 s, event loop yielded) and
                        # recheck after. Path A: a frame stays in _queue →
                        # qsize grows. Path B: _wait_for_response buffers it →
                        # _ingress_seq advances. Either advance means a live
                        # activity frame arrived during the oracle; reset the
                        # stale clock and continue rather than probing a live
                        # turn on a stale idle measurement.
                        _stale_ingress_before = self._ingress_seq
                        _stale_q_before = self._queue.qsize()
                        _stale_runtime_before = self._runtime._last_activity
                        verdict, evidence = await self._consult_oracle_offloaded(model_wait=True)
                        if (
                            self._ingress_seq != _stale_ingress_before
                            or self._queue.qsize() > _stale_q_before
                        ):
                            last_data_ts = time.monotonic()
                            continue
                        # Path C: stderr/keepalive/stdin traffic advanced the
                        # runtime clock without a session frame — activity that
                        # would have deferred this probe had it landed one tick
                        # earlier must defer it now too. last_data_ts is NOT
                        # reset (it means "last session frame" and also feeds
                        # the frames-only tool clock); the next iteration's
                        # max(last_data_ts, _last_activity) re-derives the
                        # stale clock from the newer runtime activity itself.
                        if self._runtime._last_activity > _stale_runtime_before:
                            continue
                        if verdict == VERDICT_WORKING:
                            self._log_working_deferral(_stale_idle, evidence, timeout)
                            continue
                        _flat_wait = evidence.startswith(EVIDENCE_ESTABLISHED_FLAT)
                        if verdict != VERDICT_DEAD:
                            # UNKNOWN: probe only past the window. An established-
                            # but-flat backend connection is probably a non-streamed
                            # server-side think — probing it cancels + regenerates
                            # the think, so it gets the extended window. The hard
                            # cap bounds any UNKNOWN deferral absolutely.
                            window = (
                                wd.model_silent_probe_secs
                                if _flat_wait
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
                        # ``window`` = "extended" when the established_flat
                        # model-wait probe window (model_silent_probe_secs, 900s)
                        # governed the decision instead of the ordinary stale
                        # window (stale_window_secs, 300s). The established_flat
                        # case is an EXTENSION for model-wait (silence of a
                        # non-streamed think), not a narrowing as on the tool
                        # branch — emitting "extended" lets dashboards distinguish
                        # the two cases correctly.
                        self._emit_watchdog_metric(
                            "probe", verdict, evidence, _stale_idle,
                            window="extended" if _flat_wait else "standard",
                        )
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
                parked_at_data = self._parked_total
                if not msg.fanout_no_owner:
                    # Only a frame attributable to THIS session defers the
                    # tool-idle watchdog and the post-compaction-failure budget
                    # (see the twin's init note). Provenance is the
                    # discriminator, not the frame's method: the same kind can
                    # arrive routed (this session's own progress) or fanned out
                    # (a co-tenant's), and only the runtime knows which.
                    last_own_data_ts = last_data_ts
                    parked_at_own_data = parked_at_data
                self.last_prompt_stats.event_count += 1

                # Turn-complete response
                if msg.is_response_for(req_id):
                    if msg.error:
                        # Same as _wait_for_response: route through the shared
                        # raise helper so a mid-turn failure surfaces actionable
                        # prose instead of the raw JSON-RPC dict, keeps its
                        # transient verdict for the retry ladder, and raises
                        # AcpPromptBusy when the backend reports a concurrent
                        # in-flight prompt. Advertised ids feed the entitlement
                        # discriminator (see _wait_for_response).
                        _raise_acp_error(msg.error, self._advertised_model_ids())
                    result = msg.result or {}
                    reason = ""
                    if isinstance(result, dict):
                        reason = result.get("stopReason", "") or ""
                    self._track_prompt_usage(result)
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
                    if extract_command_result and isinstance(result, dict):
                        # commands/execute returns its output in the RESPONSE
                        # result (message/data), not via session/update
                        # chunks — surface it as a text chunk. The helper
                        # two-pass redacts (URLs + credentials) before
                        # returning: command output is backend-echoed text
                        # that reaches the dashboard.
                        text = format_command_result(result)
                        if text:
                            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)
                        if not saw_agent_switch:
                            data = result.get("data")
                            if isinstance(data, dict) and data.get("agent"):
                                agent_info = data["agent"]
                                name = (
                                    agent_info.get("name", "")
                                    if isinstance(agent_info, dict)
                                    else ""
                                )
                                if name:
                                    yield AcpEvent(
                                        kind=EVENT_AGENT_SWITCHED, text=name
                                    )
                    self._last_stop_reason = reason
                    self._tool_dispatched = False
                    self._turn_done.set()
                    yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=reason,
                                   usage=self.last_prompt_stats.to_turn_usage())
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
                    _perm_event = self._build_permission_event(msg)
                    if _perm_event.child_low_fidelity and not self.child_fidelity_aware:
                        # This consumer never opted into the child-fidelity
                        # contract: it would run its ordinary hook/trust
                        # auto-approve on agent-authored context. Answer
                        # fail-closed here instead of yielding — the request
                        # is REJECTED (never dropped), the child gets a tool
                        # error, nothing can hang, and no title-only approve
                        # can occur on any consumer surface.
                        logger.warning(
                            "rejecting low-fidelity child permission request "
                            "id=%s for fidelity-unaware consumer (child=%s)",
                            _perm_event.request_id,
                            _perm_event.sub_session_id,
                        )
                        await self.reject_tool(_perm_event.request_id)
                        self._audit_handle_reject(
                            _perm_event.request_id,
                            _perm_event.title or "",
                            "child_low_fidelity_unaware_consumer",
                            sub_session_id=_perm_event.sub_session_id or "",
                        )
                        yield AcpEvent(
                            kind=EVENT_SUBAGENT_ACTIVITY,
                            sub_session_id=_perm_event.sub_session_id,
                            # Backend-controlled title: bound before redaction
                            # and cap the display, consistent with the drain
                            # notice and the [:4096] pre-redaction bounds.
                            text=(
                                "⛔ permission auto-rejected (missing security "
                                "context): "
                                f"{redact_text(str(_perm_event.title or '<unknown tool>')[:4096])[:120]}"
                            ),
                        )
                        continue
                    # Mark BEFORE the yield: the consumer parks on this event, and
                    # an observer reading the park mid-flight must be able to tell
                    # "waiting for a human" from "the consumer stopped pulling".
                    self._awaiting_permission = True
                    yield _perm_event
                elif action == "server_request_unknown":
                    await self._runtime.send_error(
                        msg.id, JSONRPC_METHOD_NOT_FOUND, "Method not found"
                    )
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
                                           usage=self.last_prompt_stats.to_turn_usage())
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
                    # A compaction notification carrying no sessionId is fanned
                    # out to EVERY co-tenant queue (AcpRuntime marks the copies
                    # fanout_no_owner once more than one session is registered),
                    # so at most one recipient actually compacted and nothing in
                    # the frame says which.  Only a frame this session OWNS may
                    # touch this session's per-turn state, in both directions:
                    # an ownerless `failed` would arm the budget in every quiet
                    # peer and reap its live turn (every consumer resets the
                    # session on that terminal), and an ownerless `completed`
                    # would disarm a peer's legitimate budget and restore the
                    # #3583 hang this fix exists to close.  Same trust boundary
                    # the budget's own clock already draws (last_own_data_ts) and
                    # the subagent roster already draws (runtime_global=).  A lone
                    # session's frame is left unmarked and genuinely is its own,
                    # so single-session behaviour is unchanged.  The event still
                    # surfaces either way — only the mutations are gated.
                    owns_frame = not msg.fanout_no_owner
                    if status_type == "completed" and owns_frame:
                        # The pre-compaction counts (and their authoritative
                        # context_tokens_from_usage flag) no longer describe
                        # the session — drop them so the context meter resets
                        # and the next telemetry can re-derive real numbers.
                        # Mirrors AcpClient._handle_compaction_status.
                        self._compaction_failed_at = None
                        self.last_prompt_stats.reset_after_compaction()
                    # Compaction summary is backend-echoed text (LLM-influenced)
                    # that reaches the dashboard — redact exfil URLs/credentials
                    # before surfacing it (parity with other text surfaces).
                    summary = redact_text(str(params.get("summary", "") or ""))
                    if status_type == "failed":
                        if owns_frame:
                            # Arm the bounded post-failure wait (the budget check
                            # at the loop top) and carry the notification's own
                            # reason so the notice stops collapsing to
                            # "unknown error".
                            self._compaction_failed_at = time.monotonic()
                            self.last_compaction_transient = (
                                compaction_failure_is_transient(params)
                            )
                        summary = compaction_failure_detail(params)
                    yield AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=summary)
                elif action == "clear":
                    yield AcpEvent(kind=EVENT_CLEAR_STATUS)
                elif action == "agent_switched":
                    saw_agent_switch = True
                    params = msg.params or {}
                    yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=params.get("agentName", ""))
                elif action == "subagent_list":
                    params = msg.params or {}
                    subs = params.get("subagents")
                    if isinstance(subs, list):
                        # The roster notification carries no sessionId, so the
                        # runtime fans it out to every co-tenant. Carry that
                        # provenance through: a subagent consumer needs to tell
                        # it apart from the SAME event kind produced by the
                        # routed KAS lifecycle path below, which does belong to
                        # this session.
                        #
                        # Deliberately ``fanout_no_owner`` and NOT the report
                        # path's ``_owns_mcp_frame``: this event feeds the
                        # subagent idle-stall clock, whose contract treats a
                        # LONE session as the owner of an ownerless roster
                        # (the flag is only set once a second queue registers).
                        # The stricter frame-must-name-me test would mark a
                        # lone session's roster global, the clock would ignore
                        # it, and an active subagent would read as stalled.
                        # The MCP report constructions below use the strict
                        # test because publishing a co-tenant's server as our
                        # own is the error THERE; here the error is the
                        # opposite one.
                        yield AcpEvent(
                            kind=EVENT_SUBAGENT_LIST,
                            subagents=subs,
                            runtime_global=msg.fanout_no_owner,
                        )
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
                    request = self._accept_oauth_request(msg)
                    if request is None:
                        continue
                    yield AcpEvent(
                        kind=EVENT_MCP_OAUTH_REQUEST,
                        server_name=request["serverName"],
                        oauth_url=request["oauthUrl"],
                        runtime_global=not self._owns_mcp_frame(msg),
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
                            runtime_global=not self._owns_mcp_frame(msg),
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
                            runtime_global=not self._owns_mcp_frame(msg),
                        )

            # Timeout — no complete received. Yield a terminal EVENT_COMPLETE with a
            # distinguishing stop_reason so callers that break on EVENT_COMPLETE can
            # tell this apart from a normal turn end.
            self._turn_done.set()
            yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="timeout",
                           usage=self.last_prompt_stats.to_turn_usage())
        finally:
            for _m in _buffered:
                self._queue.put_nowait(_m)

    def _retire_liveness_state(self) -> None:
        """Release the tracked consult and swap in a fresh, configured oracle.

        Both boundaries that drop the evidence baseline — turn start in
        ``prompt()`` and each new tool dispatch — retire rather than
        ``reset()``, because ``_consult_oracle_offloaded`` submits a BOUND oracle
        method to ``subprocess_executor()``: a walk whose await already timed out
        keeps running and keeps a reference to the instance it was handed. Samples
        are keyed ``"io"``/``"cpu"`` with no PID, so clearing in place lets that
        late writer repopulate the baseline the next generation reads, and since
        any nonzero delta counts as movement a flat tick then reads WORKING.

        The tool path has a second, sharper version of the same hazard that the
        capture path does not: ``_check_shell_child`` matches a descendant against
        the *dispatched* command and stores it as ``_tracked_child`` for exact
        exit detection on later ticks. A walk carrying the PREVIOUS tool's
        ``ToolCallState`` therefore writes a child of the previous command into
        the live oracle, and the new tool's next tick reports
        ``WORKING "shell child N alive"`` on a process that has nothing to do with
        it. Retiring confines both writes to an instance nobody reads.

        Retirement is not a semantic change for the cross-tick tracked-child
        contract itself: ``fresh()`` starts with exactly the state ``reset()``
        produced (no tracked child, no grace timestamp, no samples), and the
        consult resolves ``self._oracle`` at submission, so every tick after the
        boundary binds and accumulates on the new instance the way it did before.

        The future must be retired TOGETHER with the oracle. Replacing only the
        oracle would leave a walk wedged in the previous generation answering
        every later tick "prior consult still in flight", so the new generation
        would never sample its own process — the tool branch would then run on
        UNKNOWN and end a healthy tool call at the suspect window.

        Releasing the future costs at most one abandoned worker per boundary
        instead of one per tick. ``fresh()`` rather than a default construction so
        the per-session ``wellness_sample_secs`` (and an injected /proc root or
        clock in tests) survives the swap.
        """
        prior_consult = self._consult_future
        self._consult_future = None
        if prior_consult is not None:
            if prior_consult.done():
                _consume_future_exception(prior_consult)
            else:
                prior_consult.add_done_callback(_consume_future_exception)
        self._oracle = self._oracle.fresh()

    async def _consult_oracle_offloaded(self, *, model_wait: bool) -> tuple[str, str]:
        """Oracle verdict, offloaded off the event loop.

        The oracle's evidence gathering is a synchronous /proc filesystem walk
        (``iter_descendants`` + per-descendant reads + ``os.readlink`` on
        ``/proc/<pid>/fd/*``, which can block on a wedged fd), so it runs on
        ``subprocess_executor()`` — same treatment as the runtime's RSS probe —
        bounded so a hung /proc read can't wedge the watchdog itself. Any
        failure degrades to UNKNOWN, never to a kill.

        A timed-out await does not stop its executor thread. The one-outstanding-
        walk bound (otherwise a permanently wedged /proc read grows a new blocked
        worker every ``check_after_secs`` and starves the shared pool that
        teardown's ``_get_child_pids`` also draws from), the refused-submission-
        reads-UNKNOWN contract, and exception retrieval all live in the shared
        :func:`consult_offloaded` guard; only which oracle check runs, and
        against which pid and tool state, is decided here.
        """
        pid = getattr(self._runtime, "pid", None)
        call: Callable[..., tuple[str, str]]
        if model_wait:
            call = self._oracle.check_model_wait
            args: tuple[Any, ...] = (pid,)
        else:
            tool = self._inflight_tool
            if tool is None:
                # Resolved before the in-flight guard on purpose: this answer is
                # pure handle state and needs no worker, so a wedged walk must
                # not mask why the tool branch has nothing to check.
                return VERDICT_UNKNOWN, "no in-flight tool state"
            call = self._oracle.check_tool
            args = (pid, tool)

        return await consult_offloaded(
            self,
            call,
            args,
            executor_factory=subprocess_executor,
            log_label="oracle consultation",
        )

    def _log_working_deferral(self, idle: float, evidence: str, turn_timeout: float) -> None:
        """Evidence trail for a WORKING deferral, rate-limited to one line per
        interval so a 40-minute build doesn't spam the journal.

        Escalates to WARNING once idle passes the lower of
        :data:`_WORKING_WARN_AFTER_SECS` and
        :data:`_WORKING_WARN_DEADLINE_FRACTION` of this turn's own deadline, so a
        deferral long enough to matter is visible at the default log level on a
        short turn as well as a default-length one.
        """
        now = time.monotonic()
        if now - self._working_logged_ts < _WORKING_LOG_INTERVAL_SECS:
            return
        self._working_logged_ts = now
        warn_after = min(
            _WORKING_WARN_AFTER_SECS, turn_timeout * _WORKING_WARN_DEADLINE_FRACTION
        )
        logger.log(
            logging.WARNING if idle >= warn_after else logging.INFO,
            "Watchdog deferral on session %s: idle %.0fs but verdict WORKING (%s)",
            self._session_id, idle, evidence,
        )
        # Telemetry rides the same rate limit as the log line: one deferral
        # point per interval per session, so an hours-long WORKING build contributes a
        # bounded handful of points instead of one per 5s dispatch tick.
        self._emit_watchdog_metric("deferral", VERDICT_WORKING, evidence, idle)

    def _emit_watchdog_metric(
        self, action: str, verdict: str, evidence: str, idle: float, *,
        window: str = "standard",
    ) -> None:
        """Emit kirocrew.watchdog.action + kirocrew.watchdog.idle.duration (best-effort).

        One counter point + one histogram point per watchdog DECISION —
        ``deferral`` (WORKING, rate-limited via _log_working_deferral),
        ``probe`` (the non-lethal session/cancel stale probe), and ``cancel``
        (tool-stall recovery via _end_stalled_tool). Attrs are all closed
        enums (metrics/schema.py cardinality rule): the free-form evidence is
        bucketed by :func:`_watchdog_evidence_class`; ``window`` is one of:
        "standard" (default), "narrowed" (a tool-branch tag reduces the
        build-scale suspect window — established_flat to the model-silent budget,
        shell_child_absent to the ordinary silence window), or "extended"
        (model-wait established_flat extends the 300s stale window to the
        model-silent probe window for a non-streamed server-side think).
        ``agent_override`` is the per-agent-override BOOLEAN from the settings
        snapshot — deliberately NOT the agent name (per-agent joins happen via
        the always-on token row store, not OTel attrs). Failures never reach
        the dispatch loop.
        """
        try:
            # circular import: importing get_recorder at module top would form
            # config.loader -> ... -> acp.client -> metrics.provider ->
            # config.loader (provider reads KiroCrewConfig). Keep it lazy so
            # provider is never loaded during config.loader's import chain
            # (mirrors AcpClient.ensure_ready's emit).
            from kiro_crew.metrics.provider import get_recorder

            attrs: dict[str, str | int | bool | float] = {
                "action": action,
                "verdict": verdict,
                "evidence_class": _watchdog_evidence_class(evidence),
                "window": window,
                "agent_override": bool(self._watchdog.agent_override),
            }
            rec = get_recorder()
            rec.counter("kirocrew.watchdog.action", attrs=attrs)
            # ms, like every other kirocrew duration histogram: the dashboard's
            # generic aggregation reports all histograms under *_ms keys, so a
            # seconds-unit instrument would render 1000x off there.
            rec.histogram(
                "kirocrew.watchdog.idle.duration",
                float(idle) * 1000.0,
                unit="ms",
                attrs={"action": action, "evidence_class": attrs["evidence_class"]},
            )
        except Exception:  # telemetry must never break the watchdog
            logger.debug("watchdog metric emit failed", exc_info=True)

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
            usage=self.last_prompt_stats.to_turn_usage(),
        )

    def _track_prompt_usage(self, result: Any) -> None:
        """Fold a PromptResponse's turn-scoped token counts into the stats.

        Mirrors ``AcpClient._track_prompt_usage``: the claude-agent-acp adapter
        reports per-turn token counts on the prompt response; kiro-cli's
        response carries only ``stopReason``, so ``parse_prompt_token_usage``
        returns None there and the stats are untouched (harness parity).
        """
        tokens = parse_prompt_token_usage(result)
        if tokens is not None:
            self.last_prompt_stats.apply_prompt_token_usage(*tokens)

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
        # A real usage_update is authoritative for context_pct + token counts;
        # kiro's metadata percentage can measure a different window, so applying
        # it here would desync the headline % from the "used / total" token text.
        # sanitize_pct is the shared coercion (the AcpClient path uses it too),
        # so the two metadata paths cannot drift: it clamps NaN/±inf/out-of-range
        # and returns None for a missing or unparseable value.
        pct_f = self.last_prompt_stats.sanitize_pct(pct)
        if pct_f is not None and not self.last_prompt_stats.context_tokens_from_usage:
            self.last_prompt_stats.context_pct = pct_f
            self.last_prompt_stats.note_pct_reported()
            self._backfill_context_window(pct_f)
        self.last_prompt_stats.credits += credits

    def _backfill_context_window(self, pct: float) -> None:
        """Derive window/used tokens from a percentage-only reading.

        Thin wrapper binding this handle's resolved model id (kiro-agent
        ``currentModelId``, else the user-picked alias); the shared logic lives
        on ``AcpPromptStats.backfill_context_window`` (the AcpClient path
        delegates to the same method, so the two can no longer drift).
        """
        self.last_prompt_stats.backfill_context_window(
            pct, self._resolved_model_id or self._model
        )

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

    def queued_frame_count(self) -> int:
        """How many frames are waiting on this session's queue right now.

        For a caller that has to decide which frames predate a request it is
        about to send: read this first, then send. The answer is only meaningful
        at that instant, which is why it is the caller's to take rather than
        something ``drain_init`` re-derives later.
        """
        return self._queue.qsize()

    def _owns_mcp_frame(self, msg: JsonRpcMessage) -> bool:
        """Whether *msg* names THIS session as the server registration's owner.

        The one spelling of MCP-frame ownership on the shared runtime, used by
        both the raw-report path and the events that feed the live one, so the
        two cannot answer it differently.

        A POSITIVE test, deliberately: the runtime's ``fanout_no_owner`` marks a
        sessionless frame only once more than one queue is registered, because
        its original consumer (the subagent idle-stall clock) is right to treat
        a lone session as the sole owner of whatever arrives. This view is not —
        it publishes server names and failure reasons as "what THIS session
        mounted", so a co-tenant that emits a sessionless frame before it has
        registered its own queue would otherwise have its servers attributed
        here. Requiring the frame to name us refuses that regardless of how many
        queues exist, and it stays correct when a third transport arrives.
        """
        params = msg.params if isinstance(msg.params, dict) else {}
        return bool(self._session_id) and params.get("sessionId") == self._session_id

    def _accept_oauth_request(self, msg: JsonRpcMessage) -> dict[str, str] | None:
        """Validate and deduplicate one MCP OAuth notification."""
        params = msg.params if isinstance(msg.params, dict) else {}
        server_name = str(params.get("serverName") or params.get("name") or "")
        oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
        if not _is_safe_oauth_url(oauth_url):
            if oauth_url:
                logger.warning(
                    "ACP: refusing unsafe MCP OAuth URL for %s",
                    server_name or "(unknown)",
                )
            return None
        if not server_name:
            logger.warning("ACP: dropping MCP OAuth request with empty serverName")
            return None
        if server_name in self._oauth_emitted_servers:
            logger.debug("ACP: dropping duplicate MCP OAuth request for %s", server_name)
            return None
        self._oauth_emitted_servers.add(server_name)
        return {"serverName": server_name, "oauthUrl": oauth_url}

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        """Drain OAuth requests captured while this session initialized."""
        pending = list(self._pending_oauth_requests)
        self._pending_oauth_requests.clear()
        return pending

    def mcp_session_report(self) -> McpSessionReport:
        """This session's MCP registration report — parity with AcpClient.

        Does NOT drain: the report is the session's standing answer to "which
        servers actually started here". An unreported server means *not
        reported*, never *not mounted*.
        """
        return self._mcp_report

    async def drain_init(
        self,
        duration: float = _MCP_DRAIN_DURATION,
        idle_exit: float = _MCP_DRAIN_IDLE_EXIT,
        no_report_ceiling: float | None = None,
        stale_report_frames: int = 0,
    ) -> None:
        """Drain MCP-init / oauth / config frames from the queue after set_mode.

        Parity with AcpClient._drain_notifications. During session setup there is
        no in-flight prompt, so every frame on this session's queue is an
        init-time notification. Draining them here keeps them out of the first
        turn's event stream and gives MCP servers a window to report in before
        the first prompt races ahead.

        The idle shortcut means "quiet AFTER the servers reported", not "quiet,
        therefore done": until the first MCP registration frame (initialized /
        init_failure / oauth_request) is observed, queue silence is treated as a
        server still booting and the drain keeps waiting, bounded by
        ``no_report_ceiling`` (defaults to ``_MCP_DRAIN_NO_REPORT_CEILING``,
        resolved at call time so tests can shrink the module constant). Once a
        report has been seen, the drain allows up to ``duration`` more and exits
        after ``idle_exit`` seconds of silence, so warm sessions — whose
        registration frames were staged during session/new — arm immediately and
        pay no extra latency. ``config_option_update`` frames refresh cached
        configOptions; OAuth requests remain drainable via
        ``pop_pending_oauth_requests``; everything else is logged/discarded.
        Best-effort — never raises.

        ``stale_report_frames``: how many frames on the queue describe the roster
        that initialized during ``session/new``. For a session whose mode was
        then SWITCHED via ``set_mode``, that is the PRE-switch agent's roster,
        and the switched-to agent's own servers may still be booting. Those
        frames are still drained and processed normally; they just neither arm
        the idle shortcut nor enter the report.

        The COUNT is the caller's to measure, and it must be read before the
        ``set_mode`` request goes out — the only moment "already queued" and
        "pre-switch" mean the same thing. Measuring it here instead would count
        the switched-to agent's own registrations, which the backend can emit
        before it answers set_mode, and consume them without recording: a
        session left at a false "no report" for as long as its servers keep
        talking.
        """
        if no_report_ceiling is None:
            no_report_ceiling = _MCP_DRAIN_NO_REPORT_CEILING
        start = time.monotonic()
        deadline = start + duration
        hard_deadline = start + max(duration, no_report_ceiling)
        # A nonpositive ceiling means "do not hold for a first report" — the
        # caller knows no MCP server can register (MCP-free runtime). The idle
        # shortcut is then active from the start, i.e. the pre-fix behavior.
        reported = no_report_ceiling <= 0.0
        # Exactly the frames the caller counted before it sent set_mode are the
        # pre-switch agent's. A count, not a test for emptiness: a queue that the
        # ACTIVE agent refills before the backlog is drained never goes empty, so
        # an emptiness test would keep the flag set for the whole drain and skip
        # recording every report the switched-to agent makes. And it comes from
        # the CALLER rather than a qsize() read here, because by the time this
        # runs the active agent's own registrations may already have landed.
        stale_frames = max(0, stale_report_frames)
        drained = 0
        while True:
            now = time.monotonic()
            limit = deadline if reported else hard_deadline
            if now >= limit:
                break
            remaining = limit - now
            try:
                msg = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=min(remaining, idle_exit) if reported else remaining,
                )
            except asyncio.TimeoutError:
                # Armed: queue went quiet after reporting — servers are done.
                # Unarmed: the no-report ceiling elapsed with nothing to show.
                break
            if msg is None:
                # Runtime died during init — re-poison so the next consumer sees it.
                await self._queue.put(None)
                break
            drained += 1
            # This frame is the pre-switch agent's iff the snapshot still has
            # room for it; the counter is spent here so a refill cannot buy a
            # later frame the same treatment.
            stale_backlog = stale_frames > 0
            if stale_backlog:
                stale_frames -= 1
            try:
                action = classify_notification(msg)
                if not stale_backlog:
                    # Deliberately narrower than the "still drained and
                    # processed normally" treatment the stale backlog gets
                    # otherwise: those frames describe the PRE-switch agent's
                    # roster, so recording one could show a server the current
                    # agent does not have as mounted here. Skipping it can
                    # instead leave a server that is genuinely up looking
                    # unreported until it reports again — and that is the safe
                    # direction, because the report renders an unreported server
                    # as "no report", never as "not mounted".
                    self._mcp_report.record_frame(msg, owned=self._owns_mcp_frame(msg))
                if not reported and not stale_backlog and action in _MCP_DRAIN_REPORT_ACTIONS:
                    # First server report: arm the idle shortcut and give the
                    # remaining servers up to ``duration`` from this point.
                    reported = True
                    deadline = time.monotonic() + duration
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
                elif action == "mcp_oauth_request":
                    request = self._accept_oauth_request(msg)
                    if request is not None:
                        self._pending_oauth_requests.append(request)
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
        _perm_params = msg.params if isinstance(msg.params, dict) else {}
        event, recorded = build_permission_event(
            msg,
            tool_input_cache=self._tool_call_inputs,
            tool_input_redacted_cache=self._tool_call_input_redacted,
            shell_cache=self._tool_call_is_shell,
            raw_params_cache=self._tool_call_raw_params,
            mcp_server_name_cache=self._tool_call_mcp_server,
            tool_name_cache=self._tool_call_tool_name,
            # ORIGIN-BOUND provenance: cache entries are keyed by the
            # emitting frame's sessionId, so a child cannot replay a consumed
            # parent toolCallId to inherit trusted params for a different
            # operation, while same-origin repeat frames still resolve.
            cache_scope=str(_perm_params.get("sessionId") or self._session_id),
        )
        if recorded is not None and event.request_id != "":
            self._permission_options[event.request_id] = recorded
        # A frame the runtime routed here for a backend-internal subagent
        # carries the CHILD's sessionId, not this handle's. Mark the origin so
        # the policy consumer can tell reduced-fidelity requests apart. Child
        # tool_call/refinement frames ARE routed into the caches above (same
        # parser as slot-owned frames), so a well-behaved child carries full
        # structured context; the low-fidelity downgrade applies only when the
        # provenance flags say the context never arrived (frame race, drop).
        frame_sid = str((msg.params or {}).get("sessionId") or "")
        if frame_sid and frame_sid != self._session_id:
            event.sub_session_id = frame_sid
        return event

    def _handle_kas_update(self, session_update: str, update: dict) -> list[AcpEvent] | None:
        """Map a KAS-only ``session/update`` discriminant to Crew events.

        Returns a (possibly empty) event list for a discriminant KAS uses in
        place of a kiro-cli ``_kiro.dev/*`` method, or ``None`` when the
        discriminant is not KAS-specific (ordinary chunk/tool frames) so the
        caller falls through to the shared parser. Only ever reached on the KAS
        backend.
        """
        if session_update == UPDATE_CURRENT_MODE:
            # KAS agent-switch echo (kiro-cli: _kiro.dev/agent/switched). The new
            # mode id stands in for kiro-cli's agentName. KAS re-emits this to
            # report the CURRENT mode, so suppress only a repeat of the
            # already-emitted mode (a no-op re-assert); every actual change is
            # emitted. First-sight is NOT suppressed: the session's initial
            # current_mode_update is drained pre-prompt without reaching here, so
            # the first frame that does reach here is a real switch that must not
            # be dropped.
            mode_id = update.get("currentModeId")
            if not (isinstance(mode_id, str) and mode_id):
                return []
            if mode_id == self._last_kas_mode_id:
                return []
            self._last_kas_mode_id = mode_id
            return [AcpEvent(kind=EVENT_AGENT_SWITCHED, text=mode_id)]
        if session_update == UPDATE_SESSION_INFO:
            return self._handle_kas_session_info(update)
        # available_commands_update / any other KAS discriminant falls through to
        # the shared parser, which already returns [] for it — Crew surfaces no
        # available-commands UI for any backend (kiro-cli's
        # _kiro.dev/commands/available is likewise unconsumed), so there is
        # nothing to render and no separate branch is needed.
        return None

    def _handle_kas_session_info(self, update: dict) -> list[AcpEvent]:
        """Map a KAS ``session_info_update`` (``_meta.kiro`` union) to events.

        This one discriminant carries what kiro-cli splits across separate
        methods: ``context_usage`` (the context meter) and ``turn_completion``
        (per-turn billing) together reconstruct the single ``_kiro.dev/metadata``
        frame, while the ``summarization_*`` kinds are KAS's compaction status
        (kiro-cli: ``_kiro.dev/compaction/status``) and the ``steering_*`` kinds
        are KAS's mid-turn steer echo (kiro-cli: ``session/update`` steer
        discriminants, handled by the "steer" action).
        """
        kiro = kas_wire.kiro_meta(update)
        if kiro is None:
            return []
        kind = kiro.get(kas_wire.FIELD_KIND)
        if kind == kas_wire.KIND_CONTEXT_USAGE:
            self._apply_kas_context_pct(kiro.get(kas_wire.FIELD_USAGE_PERCENTAGE))
            return []
        if kind == kas_wire.KIND_TURN_COMPLETION:
            self._apply_kas_turn_completion(kiro)
            return []
        if kind in kas_wire.SUMMARIZATION_KINDS:
            if kind == kas_wire.KIND_SUMMARIZATION_COMPLETED:
                # Pre-compaction counts no longer describe the session — drop
                # them so the meter resets and fresh telemetry re-derives real
                # numbers (parity with the kiro-cli compaction handler).
                self.last_prompt_stats.reset_after_compaction()
                self._compaction_failed_at = None
                status_type = "completed"
            elif kind == kas_wire.KIND_SUMMARIZATION_FAILED:
                status_type = "failed"
                # Parity with AcpClient._handle_compaction_status: log the WHOLE
                # frame at WARNING. This branch previously logged nothing at
                # all, so a KAS summarization failure left the chat row as the
                # only record of it — and when the row's reason collapsed to a
                # placeholder there was nothing to grep server-side and no way
                # to learn which field the reason actually arrived in.
                # redact_text, not the bare frame: conversationSummary rides in
                # this payload, so an unredacted dump would persist whatever the
                # conversation contained -- a pasted credential included -- into
                # gateway.log. Same scrub the notice below applies, for the same
                # reason.
                logger.warning("KAS summarization failed — raw frame: %s", redact_text(str(kiro)))
                # KAS is the third producer of a failed compaction status and
                # rides the SAME dispatch loop, so it gets the same bounded
                # post-failure wait — a KAS turn abandoned after failed
                # summarization must not drain to the ceiling either.
                self._compaction_failed_at = time.monotonic()
                self.last_compaction_transient = compaction_failure_is_transient(kiro)
            else:
                status_type = "started"
            # conversationSummary is backend-echoed, LLM-influenced text that
            # reaches the dashboard — redact exfil URLs/credentials first.
            summary = redact_text(str(kiro.get(kas_wire.FIELD_CONVERSATION_SUMMARY, "") or ""))
            if status_type == "failed":
                # conversationSummary is empty on failure, so the notice would
                # collapse to "unknown error" here too — carry KAS's own reason
                # (redacted + bounded by the helper).
                summary = compaction_failure_detail(kiro)
            return [AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=summary)]
        if kind in kas_wire.STEERING_KINDS:
            # KAS mid-turn steer echo. kiro-cli sends these as `session/update`
            # discriminants (handled by the "steer" action); KAS instead puts the
            # kind under `_meta.kiro`, so route it here. `injected` is the
            # settling signal (→ EVENT_STEER_CONSUMED, which _settle_consumed_steers
            # consumes); queued/cleared mirror the kiro path. Never trust
            # backend-echoed steer text — redact before it reaches any surface.
            if kind == kas_wire.KIND_STEERING_CLEARED:
                return [AcpEvent(kind=EVENT_STEER_CLEARED)]
            text = redact_text(str(kiro.get(kas_wire.FIELD_CONTENT) or ""))
            steer_kind = (
                EVENT_STEER_CONSUMED
                if kind == kas_wire.KIND_STEERING_INJECTED
                else EVENT_STEER_QUEUED
            )
            return [AcpEvent(kind=steer_kind, text=text)]
        return []

    def _handle_kas_subagent(self, update: dict) -> list[AcpEvent] | None:
        """Route KAS PARENT sub-agent frames to EVENT_SUBAGENT_LIST.

        Returns a list of events when the frame is a PARENT sub-agent lifecycle
        frame (``kind:"agent-subtask"`` or ``pipeline``), or ``None`` when it is
        a child nested tool or an ordinary frame that should fall through to the
        shared parser (so caches populate and tool events render).
        """
        kiro = kas_wire.kiro_meta(update)
        if kiro is None:
            return None

        agent_subtask_id = kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
        pipeline = kiro.get(kas_wire.FIELD_PIPELINE)

        if not agent_subtask_id and not pipeline:
            return None

        # Pipeline frame: one entry per stage.
        if isinstance(pipeline, dict):
            stages = pipeline.get(kas_wire.FIELD_STAGES)
            if isinstance(stages, list):
                for stage in stages:
                    if not isinstance(stage, dict):
                        continue
                    s_id = stage.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
                    if not isinstance(s_id, str) or not s_id:
                        continue
                    s_status = str(stage.get("status") or "in_progress")
                    s_name = str(stage.get("name") or stage.get("role") or "")
                    self._kas_subagent_roster[s_id] = {
                        "sessionId": s_id,
                        "sessionName": s_name,
                        "agentName": s_name,
                        "initialQuery": s_name,
                        "status": {"type": s_status, "message": ""},
                    }
            return [AcpEvent(
                kind=EVENT_SUBAGENT_LIST,
                subagents=list(self._kas_subagent_roster.values()),
            )]

        # Individual agent-subtask frame (kind == "agent-subtask") → PARENT.
        is_parent = kiro.get(kas_wire.FIELD_KIND) == kas_wire.KIND_AGENT_SUBTASK

        if is_parent:
            subtask_id = str(agent_subtask_id)
            status = str(update.get("status") or "in_progress")
            title = str(update.get("title") or "")
            name = title.replace("Sub-agent: ", "") if title.startswith("Sub-agent: ") else title
            self._kas_subagent_roster[subtask_id] = {
                "sessionId": subtask_id,
                "sessionName": title,
                "agentName": name,
                "initialQuery": title,
                "status": {"type": status, "message": ""},
            }
            return [AcpEvent(
                kind=EVENT_SUBAGENT_LIST,
                subagents=list(self._kas_subagent_roster.values()),
            )]

        # Child nested tool_call/tool_call_update (has agentSubtaskId but NOT
        # kind:"agent-subtask" or pipeline) → return None so the caller falls
        # through to parse_session_update (populates caches + renders tool
        # events). The caller prepends the activity prefix separately.
        return None

    def _handle_kas_subagent_chunk(self, update: dict) -> list[AcpEvent] | None:
        """Route KAS agent_message_chunk with agentSubtaskId to activity.

        Returns a list when the chunk belongs to a child sub-agent, else None
        (fall through to normal chunk handling).
        """
        kiro = kas_wire.kiro_meta(update)
        if kiro is None:
            return None
        subtask_id = kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
        if not isinstance(subtask_id, str) or not subtask_id:
            return None
        # Must NOT have kind:"agent-subtask" or pipeline — those are parent frames
        if (
            kiro.get(kas_wire.FIELD_KIND) == kas_wire.KIND_AGENT_SUBTASK
            or kiro.get(kas_wire.FIELD_PIPELINE)
        ):
            return None
        text, _thinking = parse_text_chunk(update)
        if not text or _thinking:
            # A child's private reasoning (thinking/reasoning content) must not
            # surface as visible sub-agent activity — parity with the kiro native
            # subagent path, which only forwards non-thinking agent_message_chunk.
            return []
        return [AcpEvent(
            kind=EVENT_SUBAGENT_ACTIVITY,
            sub_session_id=subtask_id,
            text=redact_text(text),
        )]

    def _build_child_tool_activity_prefix(self, update: dict) -> list[AcpEvent]:
        """Build an EVENT_SUBAGENT_ACTIVITY prefix for a child nested tool frame.

        Called when ``_handle_kas_subagent`` returns None (child tool) so the
        activity attribution is emitted BEFORE the tool events from
        ``parse_session_update``.
        """
        kiro = kas_wire.kiro_meta(update)
        if kiro is None:
            return []
        subtask_id = kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
        if not isinstance(subtask_id, str) or not subtask_id:
            return []
        tool_call_id = str(update.get("toolCallId") or "")
        if not tool_call_id:
            return []
        title = redact_text(str(update.get("title") or ""))
        return [AcpEvent(
            kind=EVENT_SUBAGENT_ACTIVITY,
            sub_session_id=subtask_id,
            tool_call_id=tool_call_id,
            title=title,
        )]

    def _apply_kas_context_pct(self, pct: object) -> None:
        """Apply a KAS ``context_usage`` percentage to the context meter.

        A real ``usage_update`` is authoritative (``context_tokens_from_usage``)
        and must not be clobbered. ``sanitize_pct`` (shared with the kiro-cli
        metadata path) clamps NaN/±inf/out-of-range and returns None when the
        value is absent or unparseable, so malformed telemetry degrades to
        "no reading" rather than aborting the active turn.
        """
        pct_f = self.last_prompt_stats.sanitize_pct(pct)
        if pct_f is None or self.last_prompt_stats.context_tokens_from_usage:
            return
        self.last_prompt_stats.context_pct = pct_f
        self.last_prompt_stats.note_pct_reported()
        self._backfill_context_window(pct_f)

    def _apply_kas_turn_completion(self, kiro: dict) -> None:
        """Set per-turn credits from a KAS ``turn_completion`` frame.

        Delegates the ``promptTurnSummaries`` credit sum (only ``unit ==
        "credit"`` entries count; the acp provider bills in credits) to
        ``kas_wire.turn_credits``. The frame carries the whole turn's summary, so
        the total is ASSIGNED, not accumulated: a duplicate or resume-replayed
        ``turn_completion`` reports the same total and must not inflate the
        displayed cost. A malformed frame (``turn_credits`` returns ``None``)
        leaves the prior value untouched rather than zeroing.
        """
        total = kas_wire.turn_credits(kiro)
        if total is not None:
            self.last_prompt_stats.credits = total

    def _handle_update(self, msg: JsonRpcMessage) -> list[AcpEvent]:
        """Process a session/update notification and return events."""
        params = msg.params or {}
        update = params.get("update") or {}
        if not isinstance(update, dict):
            return []

        # A frame the runtime routed here for a backend-internal subagent
        # carries the CHILD's sessionId. Its tool_call/refinement updates are
        # parsed with the SAME shared parser and the SAME per-toolCallId
        # caches as this handle's own tool calls — that is what gives a later
        # child permission request real command bytes (tool_input, is_shell,
        # raw params), so the policy gates evaluate it with main-agent
        # fidelity instead of the LLM-authored title. The parsed events are
        # re-tagged as subagent activity (crew monitor), NOT emitted as this
        # session's own transcript events — a child's text chunks and tool
        # cards must not render as parent output.
        frame_sid = str(params.get("sessionId") or "")
        if frame_sid and frame_sid != self._session_id:
            child_events = parse_session_update(
                update,
                tool_input_cache=self._tool_call_inputs,
                tool_input_redacted_cache=self._tool_call_input_redacted,
                shell_cache=self._tool_call_is_shell,
                raw_params_cache=self._tool_call_raw_params,
                mcp_server_name_cache=self._tool_call_mcp_server,
                tool_name_cache=self._tool_call_tool_name,
                cache_scope=frame_sid,
            )
            out: list[AcpEvent] = []
            for ev in child_events:
                if ev.kind == EVENT_TOOL_CALL and ev.tool_call_id:
                    out.append(
                        AcpEvent(
                            kind=EVENT_SUBAGENT_ACTIVITY,
                            sub_session_id=frame_sid,
                            tool_call_id=ev.tool_call_id,
                            title=ev.title,
                        )
                    )
                elif ev.kind == EVENT_TEXT_CHUNK and ev.text:
                    out.append(
                        AcpEvent(
                            kind=EVENT_SUBAGENT_ACTIVITY,
                            sub_session_id=frame_sid,
                            text=ev.text,
                        )
                    )
                # Thinking chunks, tool results, and refinements update the
                # caches above but emit nothing: the crew monitor only shows
                # coarse activity, and the caches are the security payload.
            return out

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
                        # Mark authoritative so metadata pct cannot clobber it.
                        self.last_prompt_stats.context_tokens_from_usage = True
                        self.last_prompt_stats.note_pct_reported()
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            # Session-cumulative billing cost (claude seam); kiro never sends
            # the key so this is None on the kiro path. Delta'd per turn on
            # the stats object (monotonic guard lives there).
            cost = parse_usage_cost(update)
            if cost is not None:
                self.last_prompt_stats.apply_cost_cumulative(cost)
            return []

        # config_option_update: ACP pushes updated configOptions (e.g. after
        # model switch rebuilds effort options). State update, no event emitted.
        if session_update == "config_option_update":
            config_options = update.get("configOptions")
            if isinstance(config_options, list):
                self._config_options = config_options
                self._sync_effort_levels()
            return []

        # KAS folds signals that kiro-cli sends as separate top-level
        # ``_kiro.dev/*`` methods (agent switch, per-turn metadata, compaction
        # status) into ``session/update`` discriminants instead. kiro-cli never
        # emits these discriminants (verified against its source), so this
        # KAS-gated branch restores the same displays without touching the kiro
        # path — a positive ``== ACP_BACKEND_KAS`` check for harness parity (H5).
        # Returns None only for a non-KAS-specific discriminant, so ordinary
        # chunk/tool frames still fall through to the shared parser below.
        if self._runtime.acp_backend == ACP_BACKEND_KAS:
            kas_events = self._handle_kas_update(session_update, update)
            if kas_events is not None:
                return kas_events

        # KAS sub-agent progress: tool_call/tool_call_update frames carrying
        # _meta.kiro.agentSubtaskId or _meta.kiro.pipeline are sub-agent
        # lifecycle frames — intercept PARENT frames and route to the native
        # sub-agent WS path (EVENT_SUBAGENT_LIST) instead of rendering them as
        # ordinary tool calls. CHILD nested tool frames (agentSubtaskId present,
        # but not a parent) emit an activity prefix AND fall through to the
        # shared parser (caches populate + tool events render).
        # Positive KAS gate (H5).
        if self._runtime.acp_backend == ACP_BACKEND_KAS:
            if session_update in ("tool_call", "tool_call_update"):
                kas_sub_events = self._handle_kas_subagent(update)
                if kas_sub_events is not None:
                    return kas_sub_events
                _child_prefix = self._build_child_tool_activity_prefix(update)
                if _child_prefix:
                    # Child nested tool: run the shared parser for its cache
                    # SIDE EFFECTS ONLY — the trusted _tool_call_is_shell signal
                    # + redacted input that a later permission/result reads — but
                    # return ONLY the activity. Surfacing the tool events would
                    # render the child tool as a top-level tool; the sub-agent
                    # card is fed by the roster + activity, not the raw frame.
                    parse_session_update(
                        update,
                        tool_input_cache=self._tool_call_inputs,
                        tool_input_redacted_cache=self._tool_call_input_redacted,
                        shell_cache=self._tool_call_is_shell,
                        raw_params_cache=self._tool_call_raw_params,
                        mcp_server_name_cache=self._tool_call_mcp_server,
                        tool_name_cache=self._tool_call_tool_name,
                        cache_scope=self._session_id,
                    )
                    return _child_prefix
            if session_update == "agent_message_chunk":
                kas_chunk_events = self._handle_kas_subagent_chunk(update)
                if kas_chunk_events is not None:
                    return kas_chunk_events

        # All other session/update kinds go through the single shared parser so
        # AcpRuntime and AcpClient cannot drift on frame shape or redaction. The
        # parser writes redacted tool inputs into our caller-owned cache AND the
        # trusted shell signal into _tool_call_is_shell (which the permission
        # event later reads); we then derive per-session stale/stall bookkeeping
        # from the returned events.
        events = parse_session_update(
            update,
            tool_input_cache=self._tool_call_inputs,
            tool_input_redacted_cache=self._tool_call_input_redacted,
            shell_cache=self._tool_call_is_shell,
            raw_params_cache=self._tool_call_raw_params,
            mcp_server_name_cache=self._tool_call_mcp_server,
            tool_name_cache=self._tool_call_tool_name,
            cache_scope=self._session_id,
        )
        for ev in events:
            if ev.kind == EVENT_TEXT_CHUNK:
                self.last_prompt_stats.text_chunks += 1
                self._stale_eligible = True
            elif ev.kind == EVENT_TOOL_CALL:
                self._stale_eligible = False
                self._tool_dispatched = True
                # Attribution snapshot for the liveness oracle: title + the
                # already-redacted input + dispatch time on BOTH clocks (monotonic
                # for elapsed spans, boot for dating a child process against this
                # dispatch) + the parking this turn has banked so far, which
                # bounds how far this stamp can lag the runtime's actual spawn
                # (the park is banked when the consumer returns, i.e. before this
                # frame is processed, so it is complete here) + the trusted shell
                # flag. A new dispatch retires the oracle so its tracked child
                # and counter samples never bleed across tools — including from a
                # walk still running against the previous tool's command.
                self._inflight_tool = ToolCallState(
                    title=ev.title,
                    command=ev.tool_input,
                    dispatch_ts=time.monotonic(),
                    dispatch_boot_ts=boottime_now(),
                    dispatch_parked_secs=self._parked_total,
                    is_shell=ev.is_shell,
                    tool_name=ev.tool_name,
                )
                self._retire_liveness_state()
            elif ev.kind == EVENT_TOOL_RESULT:
                self._tool_dispatched = False
                self._inflight_tool = None
        return events
