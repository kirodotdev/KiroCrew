"""The ``AcpClient`` protocol timeouts and turn budgets, and how a prompt's timeout is resolved.

Every bound the direct client puts on the child's handshake, its reads and waits, a
turn's stall, stale-turn and compaction-failure budgets, the cancel grace window and the
respawn backoff lives here, beside the resolution of a prompt's own timeout from its
ceiling or deadline.
"""

from __future__ import annotations

import asyncio
import logging

from kiro_crew.recovery.ladder import L3_ACP_RUNTIME as _L3_ACP_RUNTIME
from kiro_crew.recovery.ladder import LADDER as _LADDER
from kiro_crew.turn_limit import current_turn_limit_secs

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


# A self-updating ACP adapter can briefly disappear or remain locked while its
# executable is replaced. Delay the one permitted startup retry past that window.
# The delay is the L3 (ACP runtime) rung's base on the shared recovery ladder --
# one schedule for every layer that rebuilds a runtime (RFC overload-resilience
# §7) -- read at import so the sleep site stays a plain constant.
_ACP_RESPAWN_BACKOFF_S = _LADDER.layer(_L3_ACP_RUNTIME).base_secs


# Minimum seconds between throttled debug summaries of the suppressed-line count,
# so the suppression itself stays observable without re-introducing a flood.
_SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS = 60.0


#: How often a steer waiting for its answer looks at whether the turn's read loop
#: is parked on an approval, whose time is not charged to the steer's wait.
_STEERING_WAIT_SLICE_SECS = 0.25


# Max consecutive empty reads before checking if process is alive
_MAX_CONSECUTIVE_EMPTY = 5


# Timeouts for session initialization steps
_INIT_TIMEOUT = 240.0  # 4 min — MCP servers can be slow to initialize
# set_mode/set_model: fire-and-forget.  kiro-cli accepts these commands
# but usually never sends a JSON-RPC response — MCP servers load
# asynchronously.  Any late responses land in _buffer and are harmlessly
# skipped by _process_message() during the next prompt read loop.
_DRAIN_DURATION = 1.0  # hard cap on draining MCP server init notifications
# Idle early-exit: once no init notification has arrived for this long, MCP
# servers have gone quiet and we stop draining instead of always waiting the full
# _DRAIN_DURATION. The cap still bounds genuinely slow servers; the idle window
# short-circuits the common fast case (servers quiet well under the cap), cutting
# time-to-first-token on new sessions without risking a missed banner from an
# active server. Must stay strictly below _DRAIN_DURATION, otherwise the hard cap
# fires first and the idle path becomes dead code.
_DRAIN_IDLE_EXIT = 0.5
_DEFAULT_PROMPT_TIMEOUT = 14400.0  # 4 hours — mirrors constants.CHAT_TURN_TIMEOUT
# Slack the transport leaves ABOVE the configured turn ceiling. The dashboard's
# own deadline (turn_dispatch._bounded_turn) must always fire first so the user
# sees the "turn hit the N-hour limit" card; a transport cut at the same instant
# would race it and report a raw timeout instead.
_PROMPT_TIMEOUT_MARGIN_SECS = 60.0


def prompt_timeout_for_ceiling(configured: float) -> float:
    """Pure transport-timeout math for an already-known turn ceiling.

    Extracted from :func:`resolve_prompt_timeout` so callers that ALREADY hold
    a loaded config (e.g. ``session_handle._load_watchdog_settings``) can bound
    against the ceiling without a second synchronous ``KiroCrewConfig.load()``.
    """
    if configured <= 0:
        return _DEFAULT_PROMPT_TIMEOUT
    if configured <= _DEFAULT_PROMPT_TIMEOUT:
        # At or below the default the transport keeps its historical wait —
        # byte-identical behaviour for every existing install. The margin is
        # only added ABOVE the default, where the transport must outlive the
        # raised dashboard ceiling.
        return _DEFAULT_PROMPT_TIMEOUT
    return configured + _PROMPT_TIMEOUT_MARGIN_SECS


def resolve_prompt_timeout() -> float:
    """Per-prompt transport timeout, honouring every deadline layered above it.

    ``agent.chat_turn_timeout_secs`` may be raised above
    :data:`_DEFAULT_PROMPT_TIMEOUT` (up to the loader's ``CHAT_TURN_TIMEOUT_MAX``)
    for long unattended turns. The transport wait must then outlive the
    dashboard's ceiling — otherwise the transport cuts the turn first and the
    larger configured value is a limit the system does not honour (the exact
    dishonesty ``turn_dispatch.chat_turn_timeout_secs`` clamps against).

    This ONE wait is shared by every prompt dispatch, so it bounds against the
    LARGEST such deadline rather than the turn ceiling alone.
    ``agent.subagent_timeout_secs`` is one of them: a subagent's outer
    ``asyncio.wait_for`` runs on that value while its prompt runs on this wait,
    so a transport cut below it kills a healthy subagent early and reports a
    transport failure rather than the deadline the operator configured. ``0``
    there is the "use the default" sentinel, resolved the same way the manager
    resolves it.

    The other is the limit of the chat turn this prompt runs in
    (:func:`kiro_crew.turn_limit.current_turn_limit_secs`). A session can carry
    its own turn time limit above the global one, and only the dispatch knows
    it, so the dispatch publishes it for the turn and the wait follows it here.

    Never returns less than :data:`_DEFAULT_PROMPT_TIMEOUT`: a LOWERED turn
    ceiling is enforced by the dashboard's own deadline, and shrinking the
    transport wait with it would also shrink the budget of non-dashboard
    callers (subagents, review runs) that share this default.

    Config is imported lazily: ``config.loader`` reaches this module through
    ``acp.session_handle``, so a module-level import would be a cycle.
    """
    turn_limit = current_turn_limit_secs() or 0.0
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.constants import SUBAGENT_TIMEOUT_SECS

        agent = KiroCrewConfig.load().agent
        subagent = float(agent.subagent_timeout_secs) or float(SUBAGENT_TIMEOUT_SECS)
        configured = max(float(agent.chat_turn_timeout_secs), subagent, turn_limit)
    except Exception:
        logger.debug("turn-ceiling config unavailable; transport keeps default", exc_info=True)
        return prompt_timeout_for_ceiling(turn_limit)
    return prompt_timeout_for_ceiling(configured)


def resolve_prompt_timeout_for_deadline(deadline: float) -> float:
    """Resolve one transport budget around an immutable outer deadline.

    The ordinary resolver preserves the historical four-hour floor exactly.
    A caller that has already captured its own deadline needs a stricter
    guarantee: ACP must expire after that owner, including when the deadline is
    exactly the floor. The larger live chat-turn budget still wins. Callers
    retain the returned value for the whole operation so a config reload cannot
    shorten later prompts in the same operation.
    """
    configured = resolve_prompt_timeout()
    if deadline <= 0:
        return configured
    return max(configured, float(deadline) + _PROMPT_TIMEOUT_MARGIN_SECS)


def _effective_prompt_timeout(timeout: float | None) -> float:
    """An explicit caller timeout wins; ``None`` resolves from config."""
    return float(timeout) if timeout is not None else resolve_prompt_timeout()


async def _effective_prompt_timeout_async(timeout: float | None) -> float:
    """Async twin of :func:`_effective_prompt_timeout` for prompt dispatch.

    The ``None`` path reads config from disk (:func:`resolve_prompt_timeout`
    → ``KiroCrewConfig.load()``: stat, read, validate), so resolving it inline
    in an ``async def`` would block the event loop for every session sharing
    it. Offload to a thread, matching this module's convention for filesystem
    work (see ``_resolve_kiro_bin_async``).
    """
    if timeout is not None:
        return float(timeout)
    return await asyncio.to_thread(resolve_prompt_timeout)


_READ_TIMEOUT = 20.0
_OPENCODE_PERMISSION_REFINEMENT_TIMEOUT = 2.0
# Steers sent as a second ``session/prompt`` (``ACP_BACKENDS_STEER_CONCURRENT_PROMPT``).
# A steer the running loop read is answered at the same idle as the turn, so the
# turn's clean end waits only this long for its answer.
_CONCURRENT_STEER_GRACE_SECS = 2.0
# How long a steer that ran a loop of its own is waited for after its cancel, so
# none of that loop's frames reach the next turn.
_CONCURRENT_STEER_CANCEL_SECS = 5.0
# At most this many such steers per turn, each at most this long. A steer past
# either bound takes the caller's queue path instead.
_MAX_CONCURRENT_STEERS = 16
_MAX_CONCURRENT_STEER_CHARS = 64_000
# After a compaction `completed` status, kiro-cli emits a fresh
# `_kiro.dev/metadata` with the real post-compaction contextUsagePercentage
# about ~1s later (live-probe confirmed). Wait up to this long for it so the
# meter can report accurate numbers instead of the reset/unknown fallback.
_POST_COMPACTION_METADATA_GRACE_SECS = 5.0
# After streaming content, if no new data arrives for this many seconds,
# treat the turn as done.  Handles kiro-cli silently finishing without
# sending the JSON-RPC `result` response.
_STALE_TURN_TIMEOUT = 90.0
# After a tool is DISPATCHED, if no data of ANY kind (tool result, progress
# update, permission request, completion) arrives for this many seconds, treat
# the turn as a dead stall and exit.  Unlike _STALE_TURN_TIMEOUT this does NOT
# require _stale_eligible (which is cleared the moment a tool_call is yielded),
# so it catches the "tool dispatched but never resolves" hang that otherwise
# runs to the caller's full prompt timeout — e.g. a cron job dispatching a tool
# that silently never returns, burning the whole job timeout.  Long real tools
# keep resetting the timer via tool_call_update progress frames and tool
# results, so this only trips on a genuine stall.
#
# CONTRACT FOR BACKEND AUTHORS: a backend that emits NO frame at all while a
# tool runs gets exactly this window for the whole tool, so a >10min build must
# either stream tool_call_update progress frames or ping the session-keepalive
# endpoint (both reset the clock).  This window — not the ~90s stale-turn
# cutoff — is what governs an open tool call's silence.
_TOOL_STALL_TIMEOUT = 600.0


# After a compaction `failed` status, kiro-cli can leave the turn it was
# compacting for unanswered: no session/prompt response and no end_turn ever
# arrive, so the read loop drains in silence to the caller's full prompt
# ceiling (hours) and the slot is never released — the user waits it out or
# presses Stop. Once a failure has been seen, treat this much
# BACKEND SILENCE as a dead turn and end it with
# STOP_REASON_COMPACTION_FAILED. Any stdout frame resets the clock, so a
# backend that recovers and keeps streaming is unaffected and stays governed
# by _STALE_TURN_TIMEOUT / _TOOL_STALL_TIMEOUT. Deliberately does NOT fold in
# _last_activity (stderr/keepalive): a wedged post-compaction turn that keeps
# writing stderr must still be reaped.
_COMPACTION_FAILED_TURN_BUDGET = 60.0
_CANCEL_GRACE_SECS = 10.0  # grace window for cooperative cancel ack
# Absolute safety cap for _wait_for_response's activity-based deadline. The
# per-call deadline resets on every received frame (so a long session/load
# replay that streams the whole transcript as notifications is not killed),
# but never extends past this hard ceiling.
_WAIT_RESPONSE_MAX_TIMEOUT = 600.0  # 10 min absolute ceiling
# Upper bound on the offloaded ACP-layer SEL audit emit. Auditing is best-effort
# and must never gate tool dispatch, so a wedged SEL backend is abandoned (the
# worker thread may leak, which is survivable) after this timeout.
_SEL_AUDIT_TIMEOUT_SECONDS = 5.0
