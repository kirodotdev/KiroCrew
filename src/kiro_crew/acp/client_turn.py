"""Run an ``AcpClient`` prompt turn and dispatch the frames it reads.

The kiro prompt loop, ``send_message`` and ``send_message_stream``, ``stream_events``, the frame classifier
and dispatcher, the prompt response reader, the opencode shell-permission deferral and the liveness
consult.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_active_run_id``, ``_active_tool_calls``, ``_cancelled``,
``_claude_compaction_pending``, ``_codex_compaction_pending``,
``_compaction_failed_at``, ``_compaction_failed_turn``, ``_last_stop_reason``,
``_oauth_emitted_servers``, ``_observed_tool_calls``, ``_permission_options``,
``_prompt_or_tool_seen``, ``_skill_reads``, ``_stale_eligible``,
``_steers_awaiting_echo``, ``_tool_call_diff_path``, ``_tool_call_harness_tool_name``,
``_tool_call_identity_unreadable``, ``_tool_call_input_redacted``,
``_tool_call_inputs``, ``_tool_call_is_shell``, ``_tool_call_mcp_server``,
``_tool_call_params``, ``_tool_call_tool_name``, ``_tool_call_unclassified``,
``_tool_dispatched``, ``_turn_done``, ``last_prompt_stats``.

Instance fields read: ``_agent_accepts_image``, ``_buffer``, ``_last_activity``, ``_liveness_oracle``,
``_permission_gate_events``, ``_pid``, ``_process``, ``_session_id``, ``_steer_turn``,
``_turn_lock``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing
from typing import TYPE_CHECKING, AsyncGenerator, AsyncIterator

from kiro_crew.acp._dispatch import (
    DRAIN_YIELD_AFTER_S,
    MAX_ACTIVE_TOOL_CALLS,
    _loggable_request_id,
    error_is_refusal_terminal,
    parse_message_id,
)
from kiro_crew.acp.client_timeouts import (
    _COMPACTION_FAILED_TURN_BUDGET,
    _MAX_CONSECUTIVE_EMPTY,
    _OPENCODE_PERMISSION_REFINEMENT_TIMEOUT,
    _READ_TIMEOUT,
    _STALE_TURN_TIMEOUT,
    _TOOL_STALL_TIMEOUT,
    _effective_prompt_timeout_async,
)
from kiro_crew.acp.client_transport import _is_safe_oauth_url
from kiro_crew.acp.client_turn_control import format_command_result
from kiro_crew.acp.liveness import EVIDENCE_SAMPLING, VERDICT_WORKING, consult_offloaded
from kiro_crew.acp.prompt_blocks import build_prompt_blocks
from kiro_crew.acp.transport_errors import (
    AcpProcessDied,
    AcpTimeoutError,
    _raise_acp_error,
    compaction_failure_detail,
    registration_rate_limited_error,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_OPENCODE,
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_STEER_CONSUMED,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    METHOD_AGENT_SWITCHED,
    METHOD_CLEAR_STATUS,
    METHOD_COMPACTION_STATUS,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_METADATA,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_UPDATE,
    METHOD_SUBAGENT_LIST_UPDATE,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_END_TURN,
    TERMINAL_TOOL_STATUSES,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


# Emitted by kiro-cli as a plain agent_message_chunk when its built-in, non-overridable
# security filter cancels every tool use in an assistant turn (e.g. shell commands
# containing "credentials").  After this text kiro-cli returns to an idle state waiting
# for the next user prompt and NEVER sends a ``complete`` response for the in-flight
# ``session/prompt`` — so without special handling Kiro Crew waits the full prompt timeout.
# Treating this chunk as end-of-turn unblocks the caller; the text itself is still
# yielded so the user/agent sees what happened.  We use an exact (stripped) match so
# the detection does not fire if the model merely quotes the marker string in prose.
_TOOL_INTERRUPTED_MARKER = "Tool uses were interrupted, waiting for the next user prompt"


def _is_tool_interrupted_marker(chunk: str) -> bool:
    """Exact match against the kiro-cli security-filter interrupt marker."""
    return chunk.strip() == _TOOL_INTERRUPTED_MARKER


# ── Prompt Loop Helpers ──


def _process_message(self: AcpClient, msg: JsonRpcMessage, req_id: int) -> str:
    """Classify a message into an action string.

    Actions: "complete", "error", "steer_answer", "permission", "update",
    "metadata", "server_request_unknown", "skip".
    """
    if msg.is_response_for(req_id):
        return "error" if msg.error else "complete"

    steer_turn = getattr(self, "_steer_turn", None)
    if msg.method is None and steer_turn is not None and msg.id in steer_turn.steers:
        return "steer_answer"

    if msg.is_method(METHOD_REQUEST_PERMISSION):
        return "permission"

    if msg.is_method(METHOD_SESSION_UPDATE):
        self._note_active_run(msg)
        return "update"

    if msg.method is None and msg.id is not None and self._settle_steer_answer(msg):
        return "skip"

    if msg.is_method(METHOD_METADATA):
        return "metadata"

    if msg.is_method(METHOD_COMPACTION_STATUS):
        return "compaction"

    if msg.is_method(METHOD_CLEAR_STATUS):
        return "clear"

    if msg.is_method(METHOD_AGENT_SWITCHED):
        return "agent_switched"

    if msg.is_method(METHOD_MCP_OAUTH_REQUEST):
        return "mcp_oauth_request"

    if msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
        return "mcp_server_initialized"

    if msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
        return "mcp_server_init_failure"

    if msg.is_method(METHOD_SUBAGENT_LIST_UPDATE):
        return "subagent_list"

    if msg.is_method(METHOD_KIRO_SESSION_UPDATE):
        return "subagent_activity"

    # Unknown server→client REQUEST (has both method AND id). Per JSON-RPC
    # the agent blocks until it gets a response, so it must be answered
    # (with -32601 by the dispatch sites) rather than silently skipped —
    # otherwise the agent hangs forever. Known requests (request_permission)
    # are handled above; only genuinely unrecognized requests reach here.
    if msg.method is not None and msg.id is not None:
        return "server_request_unknown"

    return "skip"


async def _defer_opencode_shell_permission(
    self: AcpClient, msg: JsonRpcMessage, remaining: float
) -> bool:
    """Queue a matching command update before its OpenCode shell permission."""
    params = msg.params if isinstance(msg.params, dict) else {}
    tool_call = params.get("toolCall")
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    tool_call_id = tool_call.get("toolCallId")
    if not isinstance(tool_call_id, str) or self._tool_call_is_shell.get(tool_call_id) is not True:
        return False
    cached = self._tool_call_params.get(tool_call_id, {})
    if isinstance(cached, dict) and cached.get("command"):
        return False

    # OpenCode can ask permission before its command-bearing update. Read
    # only the next frame to preserve unrelated event order. The request's
    # own rawInput must never fill the trusted security cache.
    following = await self._read_message(
        timeout=min(_OPENCODE_PERMISSION_REFINEMENT_TIMEOUT, remaining)
    )
    if following is None:
        return False

    follow_params = following.params if isinstance(following.params, dict) else {}
    update = follow_params.get("update")
    update = update if isinstance(update, dict) else {}
    raw_input = update.get("rawInput")
    buffer_capacity = self._buffer.maxlen
    if not (
        following.is_method(METHOD_SESSION_UPDATE)
        and isinstance(params.get("sessionId"), str)
        and follow_params.get("sessionId") == params.get("sessionId")
        and update.get("sessionUpdate") == "tool_call_update"
        and update.get("toolCallId") == tool_call_id
        and update.get("status") == "in_progress"
        and isinstance(raw_input, dict)
        and isinstance(raw_input.get("command"), str)
        and raw_input["command"]
        and not update.get("content")
        and not update.get("rawOutput")
        and (buffer_capacity is None or len(self._buffer) <= buffer_capacity - 2)
    ):
        self._buffer.appendleft(following)
        return False

    # The normal dispatcher caches the command before building permission.
    self._buffer.appendleft(msg)
    self._buffer.appendleft(following)
    return True


async def _prompt_loop(
    self: AcpClient,
    req_id: int,
    timeout: float,
) -> AsyncGenerator[tuple[str, JsonRpcMessage], None]:
    """Core prompt read loop. Yields (action, msg) pairs.

    Always releases ``_turn_done`` on exit — including abnormal exits
    (process death, cancel-grace exceeded, or a caller that raises on an
    ``error`` action and closes this generator). Without the ``finally``,
    those paths bypass the callers' trailing ``_turn_done.set()`` and a
    ``wait_turn_done`` waiter (the cooperative-stop ack) blocks for its
    full budget before escalating to a session-losing hard kill.
    """
    # L1 turn-lock: serialize the whole read turn on this client's single
    # stdout StreamReader so two _bg streaming turns can't both park on
    # readline() and trip "readuntil() called while another coroutine is
    # already waiting". Acquired here (every streaming consumer funnels
    # through _prompt_loop), released in the finally — see the __init__
    # comment for the finalization + coverage caveats.
    await self._turn_lock.acquire()
    try:
        # Retire the liveness state HERE, under the lock, because this is the
        # one point every prompt path funnels through: send_message (via
        # _read_prompt_response), send_message_stream, and _dispatch_events.
        # Retiring in a caller's prologue instead would (a) miss the direct
        # prompt APIs, leaving their next turn gated by the previous turn's
        # wedged walk, and (b) run BEFORE this acquire, letting a queued turn
        # clear the active turn's tracked consult and so allow a second walk
        # while the first is still pending.
        self._retire_liveness_state()
        # A goose run id and the steers awaiting their echo belong to one
        # turn: the new prompt's run id is announced by a frame this loop
        # has not read yet, and a steer the last turn never echoed was
        # discarded with that run and already queued by its caller.
        self._active_run_id = None
        self._steers_awaiting_echo = {}
        self._compaction_failed_at = None
        self._compaction_failed_turn = False
        self._claude_compaction_pending = False
        self._codex_compaction_pending = False
        deadline = time.monotonic() + timeout
        consecutive_empty = 0
        last_data_ts = time.monotonic()
        # Consumer park accounting (mirrors AcpSessionHandle._dispatch_events):
        # the interval between this generator's yield and its resume is
        # CONSUMER time — a human approval prompt parks the whole generator
        # chain at that yield — so the post-compaction-failure idle clock
        # below must subtract it or a long approval wait reads as backend
        # silence and the budget reaps a live turn. `parked_at_data`
        # snapshots the accumulator when `last_data_ts` is taken, so only
        # park time accrued SINCE the last frame is excluded.
        parked_total = 0.0
        parked_at_data = 0.0
        _last_yield = time.monotonic()

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            # Post-compaction-failure budget: a `failed` status arrived and the
            # backend has since gone silent past the budget, so no prompt
            # response or end_turn is coming. Stop reading so _dispatch_events
            # ends the turn explicitly and the runner releases the slot,
            # instead of draining to `deadline` (hours).
            #
            # SUSPENDED while a tool is in flight: kiro-cli can recover from a
            # failed compaction and dispatch a tool, and a legitimately silent
            # long tool (a build, a spawned subagent) would then be reaped at
            # 60s — killing valid work that the tool-stall watchdog already
            # governs on its own, much longer, liveness-gated budget. A tool
            # dispatch is positive evidence the turn is alive, so this budget
            # hands off to _TOOL_STALL_TIMEOUT and re-arms when the tool
            # resolves (_tool_dispatched cleared in _dispatch_events).
            #
            # Clock asymmetry with AcpSessionHandle's twin is INTENTIONAL: this
            # path owns a dedicated process, so every frame is its own and the
            # park accounting below is what it needs; the shared-runtime twin
            # instead needs session-attributable frames (a co-tenant's fanout
            # must not defer the reap). Keep both when touching either.
            if self._compaction_failed_at is not None and not self._tool_dispatched:
                _compact_idle = max(
                    0.0,
                    (time.monotonic() - max(self._compaction_failed_at, last_data_ts))
                    - max(0.0, parked_total - parked_at_data),
                )
                if _compact_idle > _COMPACTION_FAILED_TURN_BUDGET:
                    logger.warning(
                        "Compaction failed for req %d and no prompt response "
                        "arrived for %.0fs — ending the turn.",
                        req_id,
                        _compact_idle,
                    )
                    self._compaction_failed_turn = True
                    return

            # Cooperative yield, placed where the previous frame is fully
            # handled and the next one is not yet read: no frame is held in a
            # local here, so a cancellation landing on this yield drops
            # nothing. `_read_message` is NOT a suspension point when input is
            # already buffered -- it returns `self._buffer.popleft()` outright,
            # and `StreamReader.readline` returns without awaiting when the
            # line is already in its buffer -- so without this a burst drains
            # inside one task step. Twin of
            # AcpSessionHandle._dispatch_events; both read the same budget.
            _now = time.monotonic()
            if _now - _last_yield >= DRAIN_YIELD_AFTER_S:
                await asyncio.sleep(0)
                _last_yield = time.monotonic()

            msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
            if msg is None:
                consecutive_empty += 1
                if consecutive_empty >= _MAX_CONSECUTIVE_EMPTY and not self._is_process_alive():
                    rc = self._process.returncode if self._process else "?"
                    # A death whose retained stderr shows a throttled
                    # registration is endpoint throttling, not a crash:
                    # raise the typed transient subclass so the retry
                    # ladders recover it (a respawn through ensure_ready
                    # genuinely retries registration on this transport).
                    _throttled = await self._registration_throttle_line()
                    if _throttled is not None:
                        raise registration_rate_limited_error(
                            f"Process exited during prompt (exit code {rc})", _throttled
                        )
                    raise AcpProcessDied(f"Process exited during prompt (exit code {rc})")
                # Staleness check: if caller set _stale_eligible (text was
                # streamed) and kiro-cli has gone silent, exit early.
                # Fold in _last_activity (refreshed by the stderr drain) so a
                # turn that is still streaming thinking_tokens on stderr —
                # between its final text chunk and its next tool call — is not
                # mistaken for silence. Using only the stdout clock
                # (last_data_ts) trips a false stale-turn on every multi-turn
                # reasoning step, burning ~_STALE_TURN_TIMEOUT s per turn and
                # reaping long subagent runs at the timeout.
                last_seen = max(last_data_ts, self._last_activity)
                if self._stale_eligible:
                    # Consult the liveness oracle on EVERY silent read once
                    # text has streamed — not only at the timeout. The oracle
                    # needs a prior sample to compute a movement delta (its
                    # first call after a boundary retirement always reads
                    # UNKNOWN/"sampling"),
                    # so a single consult at the 90s mark would always reap.
                    # Sampling each silent read primes the baseline and keeps
                    # the movement window recent, mirroring the kiro path's
                    # per-tick consult. The consult is /proc-based and
                    # offloaded so it cannot block the read loop, and degrades
                    # to a non-WORKING verdict on any error (fail toward
                    # reaping). In production, silent reads recur at the
                    # ~_READ_TIMEOUT cadence, giving well-spaced samples.
                    #
                    # Snapshot the idle window BEFORE awaiting the consult.
                    # The walk is OUR OWN probe and a cold one costs tens of
                    # milliseconds (executor warm-up, then the subtree walk);
                    # reading the clock AFTER it charges that latency to the
                    # BACKEND's silence budget, so on a loaded host the first
                    # silent read could cross the cutoff carrying the only
                    # verdict the oracle can give before it has a baseline,
                    # and reap a turn that was streaming a moment earlier.
                    _idle_secs = time.monotonic() - last_seen
                    verdict, evidence = await self._consult_liveness_model_wait()
                    if _idle_secs > _STALE_TURN_TIMEOUT:
                        # Past the cutoff: a backend still doing work (CPU/IO
                        # movement in its subtree — a long model generation or
                        # a spawned build) reads WORKING and we keep waiting.
                        # Only WORKING extends; any other verdict
                        # (DEAD/UNKNOWN/STUCK_INPUT) preserves the conservative
                        # end-the-turn behavior, so hang recovery is never
                        # weakened (still bounded by _DEFAULT_PROMPT_TIMEOUT).
                        if verdict == VERDICT_WORKING:
                            logger.debug(
                                "Stale-turn deferral for req %d — idle %.0fs but "
                                "backend WORKING (%s)",
                                req_id,
                                _idle_secs,
                                evidence,
                            )
                            continue
                        if evidence == EVIDENCE_SAMPLING:
                            # The oracle stored a BASELINE and has nothing to
                            # compare it against yet, so this verdict is
                            # structurally incapable of reading WORKING: it
                            # means "ask me again", not "idle". Reaping on it
                            # ends a live turn on no evidence at all — the
                            # per-silent-read consulting above exists to make
                            # that unlikely, and deferring here makes it
                            # impossible rather than merely improbable. The
                            # next silent read has a baseline and answers for
                            # real, so recovery is delayed by one read, not
                            # weakened.
                            logger.debug(
                                "Stale-turn deferral for req %d — idle %.0fs but the "
                                "liveness baseline is still priming (%s)",
                                req_id,
                                _idle_secs,
                                evidence,
                            )
                            continue
                        if self._tool_dispatched:
                            # An OPEN TOOL CALL is positive evidence the turn
                            # is alive, so it defers this cutoff whatever the
                            # verdict says. Only kiro-cli streams
                            # tool_call_update progress frames while a tool
                            # runs; a backend that runs the tool to completion
                            # and only then reports the result is silent for
                            # the whole tool, and with text streamed before
                            # the dispatch this cutoff would tear the turn down
                            # MID-TOOL — reporting truncated work as complete.
                            # Deliberately NOT a `continue`:
                            # falling through hands the silence to the
                            # tool-stall watchdog below, which governs exactly
                            # this case on its own much longer budget and
                            # RECOVERS the slot instead of completing the
                            # turn. Mirrors the compaction-failed budget's
                            # suspension above; _tool_dispatched is cleared
                            # when the tool resolves, so the cutoff re-arms
                            # for the model-wait silence it is actually for.
                            logger.debug(
                                "Stale-turn deferral for req %d — idle %.0fs with a tool "
                                "call still open (liveness=%s: %s); tool-stall watchdog "
                                "governs",
                                req_id,
                                _idle_secs,
                                verdict,
                                evidence,
                            )
                        else:
                            logger.warning(
                                "Stale turn detected for req %d — no data for %.0fs after text was streamed "
                                "(liveness=%s: %s). Treating as complete.",
                                req_id,
                                _idle_secs,
                                verdict,
                                evidence,
                            )
                            return
                # Tool-stall watchdog: a tool was dispatched but NOTHING has
                # come back (no result, no progress, no permission) for the
                # stall window.  This is the silent-hang case where
                # _stale_eligible is False (cleared on tool_call) so the
                # check above never fires.
                #
                # Recovery (not just detection): the original bare ``return``
                # abandoned the turn but left the kiro-cli child ALIVE
                # mid-prompt, so the slot wedged and every later prompt hit
                # "Prompt already in progress" until the whole backend was
                # killed by hand.  Instead, kill the wedged child so the next
                # prompt cold-starts, and raise AcpProcessDied so the existing
                # recovery path takes over: the dashboard resets the session
                # and re-queues the message (bounded by _acp_pipe_death_retries
                # → "Session stuck" after 3), and cron/other callers surface a
                # clean error instead of a hung turn.  _kill_process only
                # touches the subprocess/pipes (never _turn_lock), so it is
                # safe to call here — the finally still releases the lock.
                #
                # Use max(last_data_ts, _last_activity) so that MCP tools
                # which ping /api/session-keepalive (wait, spawn_sub_agents)
                # keep the watchdog satisfied even though no JSON-RPC frames
                # arrive on stdout during their execution.
                _tool_last_seen = max(last_data_ts, self._last_activity)
                if (
                    self._tool_dispatched
                    and (time.monotonic() - _tool_last_seen) > _TOOL_STALL_TIMEOUT
                ):
                    _stall_idle = time.monotonic() - _tool_last_seen
                    logger.warning(
                        "Tool stall detected for req %d — tool dispatched but no data for %.0fs. "
                        "Killing agent to recover the slot.",
                        req_id,
                        _stall_idle,
                    )
                    await self._kill_process(force=True)
                    raise AcpProcessDied(
                        f"tool stalled — no data for {_stall_idle:.0f}s; agent killed to recover"
                    )
                continue

            consecutive_empty = 0
            last_data_ts = time.monotonic()
            parked_at_data = parked_total
            # NB: do NOT clear _tool_dispatched here.  The last_data_ts reset
            # above already prevents false positives for tools that stream
            # progress frames (each frame restarts the _TOOL_STALL_TIMEOUT
            # countdown).  Clearing the flag on every inbound frame would
            # disarm the watchdog after a single progress frame, so a tool
            # that emits one frame then silently stalls would hang anyway —
            # exactly the bug this watchdog targets.  The flag is cleared
            # only when a tool actually resolves or the turn completes
            # (see _dispatch_events).
            self.last_prompt_stats.event_count += 1

            # The answer to a steer sent into this turn is read here, by
            # the one consumer of stdout, and handed to the waiting steer.
            if self._settle_steering_answer(msg):
                continue

            action = self._process_message(msg, req_id)
            if (
                action == "permission"
                and self.backend == ACP_BACKEND_OPENCODE
                and await self._defer_opencode_shell_permission(msg, remaining)
            ):
                self.last_prompt_stats.event_count -= 1
                continue
            # Single yield chokepoint: everything downstream (dispatch,
            # chat runner, a human answering an approval card) runs while
            # this generator is suspended here, so the whole gap is
            # consumer time. `finally` so an abandoned generator's
            # GeneratorExit still closes the park.
            _parked_since = time.monotonic()
            try:
                yield action, msg
            finally:
                parked_total += max(0.0, time.monotonic() - _parked_since)
    finally:
        self._turn_lock.release()
        # Nothing reads a steer's answer once this loop is gone, so the turn
        # stops taking steers on every exit.
        if self._steering_turn_req == req_id:
            self._steering_turn_req = None
        # Release any cooperative-stop waiter regardless of how the loop
        # ends. The callers set the precise stop reason on the clean
        # "complete" path before this runs (idempotent); on abnormal exit
        # the reason stays "" → provider.cancel reports "timeout" →
        # escalates to hard kill, the correct outcome for a dead turn.
        if not self._turn_done.is_set():
            self._turn_done.set()


async def _consult_liveness_model_wait(self: AcpClient) -> tuple[str, str]:
    """Liveness verdict for the stale-turn gate, offloaded off the loop.

    The oracle walks ``/proc`` for the backend subprocess subtree — a
    synchronous filesystem walk that can block on a wedged fd — so it runs
    on ``subprocess_executor()`` under a bounded timeout, the same treatment
    the runtime's other /proc probes get. Any failure or timeout degrades to
    a non-WORKING verdict so the caller falls through to ending the turn
    (fail toward reaping, never toward hanging).

    Scope, mirroring the shared-runtime oracle this converges onto:
    - Linux-only evidence. On a host without ``/proc`` the verdict is
      UNKNOWN → the turn is reaped at the cutoff exactly as before this
      gate existed, so the change is behavior-preserving off Linux and a
      strict improvement on the gateway's Linux deploy target.
    - Subtree-aggregate movement. A busy *unrelated* descendant (e.g. an
      MCP child polling) can read WORKING even if the model turn itself is
      wedged with a lost completion frame, extending that turn to the
      ``_DEFAULT_PROMPT_TIMEOUT`` backstop rather than reaping at 90s. This
      is an inherent property of the shared ``LivenessOracle`` (the kiro
      path has it too); tighter per-branch attribution belongs in
      ``liveness.py``, shared by both callers, not here.

    A timed-out await does not stop its executor thread. The one-outstanding-
    walk bound, the refused-submission-reads-UNKNOWN contract, and exception
    retrieval all live in the shared :func:`consult_offloaded` guard.
    ``_prompt_loop`` retires the tracked future at turn start under
    ``_turn_lock``, so a walk abandoned by one turn never gates the next (at
    the cost of one abandoned worker per turn, versus one per silent read
    before this guard existed).
    """
    return await consult_offloaded(
        self,
        self._liveness_oracle.check_model_wait,
        (getattr(self, "_pid", None),),
        executor_factory=subprocess_executor,
        log_label="liveness consult",
    )


# ── Public API ──


async def send_message(self: AcpClient, message: str, timeout: float | None = None) -> str:
    """Send a prompt and return the full response text."""
    timeout = await _effective_prompt_timeout_async(timeout)
    self._cancelled = False
    self._turn_done.clear()
    await self.ensure_ready()

    req_id = await self._send_prompt(message)
    return await self._read_prompt_response(req_id, timeout)


async def send_message_stream(
    self: AcpClient, message: str, timeout: float | None = None
) -> AsyncIterator[str]:
    """Send a prompt and yield text chunks as they arrive."""
    timeout = await _effective_prompt_timeout_async(timeout)
    # NOTE: PreToolUse/PostToolUse hooks are intentionally NOT fired on this
    # streaming path today. No audit_source (worker-pool) consumer uses
    # send_message_stream — hook instrumentation lives on the _read_prompt_response
    # path (_maybe_fire_pre_tool_hooks / _maybe_fire_post_tool_hooks). If a future
    # streaming subagent adopts this method, mirror that Pre/Post instrumentation here.
    self._cancelled = False
    self._turn_done.clear()
    await self.ensure_ready()

    req_id = await self._send_prompt(message)
    self.last_prompt_stats = self.last_prompt_stats.carry_over()

    # aclosing(): _prompt_loop holds _turn_lock and releases it in its
    # finally. Consumers below `return` on "complete" without exhausting the
    # loop, which leaves the async-generator SUSPENDED — and CPython
    # finalizes async-gens via a *deferred* scheduled athrow, not at the
    # return point, so the lock would stay held past the turn (next _bg
    # caller blocks = the freeze). aclosing() runs aclose() deterministically
    # on block exit, firing the finally and releasing the lock immediately.
    async with aclosing(self._prompt_loop(req_id, timeout)) as _loop:
        async for action, msg in _loop:
            if action == "complete":
                reason = ""
                result = msg.result or {}
                if isinstance(result, dict):
                    reason = result.get("stopReason", "") or ""
                self._track_prompt_usage(result)
                # Close out an automatic claude compaction. The returned
                # event is discarded — this API yields str — but the context
                # counts it drops are what the meter reads next turn.
                self._settle_claude_compaction(reason)
                self._settle_codex_compaction(reason)
                reason, _ = self.last_prompt_stats.terminal_refusal(reason)
                self._last_stop_reason = reason
                self._turn_done.set()
                return
            if action == "error":
                if error_is_refusal_terminal(msg.error, self.last_prompt_stats.refusal):
                    # The refusal's own -32603 terminal (see
                    # _dispatch_events). This API yields str, so the fold
                    # lands on _last_stop_reason and the turn ends cleanly
                    # rather than raising a deterministic decline.
                    reason, _ = self.last_prompt_stats.terminal_refusal("")
                    self._last_stop_reason = reason
                    self._turn_done.set()
                    return
                _raise_acp_error(msg.error, self._advertised_model_ids(), backend=self.backend)
            if action == "permission":
                await self._handle_permission(msg)
            elif action == "server_request_unknown":
                await self._reject_unknown_server_request(msg)
            elif action == "update":
                self._track_usage_update(msg)
                self._note_background_launch(msg)
                # Apply the codex compaction state change; this API yields
                # str so the event has nowhere to go, but the context counts
                # it drops are what the meter reads next turn.
                self._codex_compaction_event(msg)
                # The gate tripwire holds on every reader that answers
                # permission frames, this text-only one included.
                await self._tripwire_pi_gate(msg)
                await self._tripwire_goose_mode(msg)
                chunk, is_thinking = self._extract_text_chunk(msg)
                if chunk and not is_thinking:
                    # A claude compaction notice is a control frame wearing
                    # assistant text. This API yields str, so the event has
                    # nowhere to go — but the state change still applies and
                    # the chunk is still forwarded. Recognizing a notice is a
                    # guess about bare prose, so dropping it here would let
                    # one wrong guess erase a real answer with nothing left
                    # to recover it from.
                    self._claude_compaction_event(chunk)
                    self.last_prompt_stats.text_chunks += 1
                    yield chunk
                    if _is_tool_interrupted_marker(chunk):
                        self._emit_tool_interrupted_sel("send_message_stream")
                        # send_message_stream yields only text chunks (str),
                        # not AcpEvent objects. Tool-result events are a
                        # different shape and cannot be yielded here; callers
                        # of this API do not consume them. Unlike
                        # _dispatch_events (which yields AcpEvent and must
                        # drain tool results before EVENT_COMPLETE), we just
                        # return — no further text will arrive from kiro-cli.
                        return
                # On a session with a spec deny set, the full extractor rather
                # than the stats-only tracker: this loop answers permission
                # requests through _handle_permission, and the refusal there
                # identifies a call ONLY from the provenance the extractor caches
                # (raw params by toolCallId) -- stats alone would leave every
                # request unidentified, and the auto-approve site then refuses
                # them all rather than run a switched-off tool. The same extractor
                # writes the classification cache the identity-drift refusals read,
                # so a harness in ACP_BACKENDS_META_IDENTITY runs it too. On a
                # session that judges nothing, the tracker's byte-identical
                # behaviour on every other backend is kept.
                if self._judges_permission_requests:
                    self._extract_tool_event(msg)
                else:
                    self._track_tool_call(msg)
            elif action == "metadata":
                self._track_metadata(msg)
            elif action == "compaction":
                self._handle_compaction_status(msg)

    # Loop ended without "complete" — timeout or process death.
    self._last_stop_reason = ""
    self._turn_done.set()


async def stream_events(
    self: AcpClient,
    message: str,
    timeout: float | None = None,
    *,
    allow_image: bool = True,
) -> AsyncIterator[AcpEvent]:
    """Send a prompt and yield AcpEvent objects (text, tool_call, permission, complete).

    ``allow_image=False`` sends *message* as text only: no path in it is
    read or inlined.
    """
    timeout = await _effective_prompt_timeout_async(timeout)
    self._cancelled = False
    self._turn_done.clear()
    await self.ensure_ready()
    req_id = await self._send_prompt(message, allow_image=allow_image)
    # aclosing(): a consumer that closes this stream early must close the
    # dispatcher too, so its finally records the turn's unanswered steers
    # as orphans before the next _send_prompt settles them.
    async with aclosing(self._dispatch_prompt_events(req_id, timeout)) as events:
        async for event in events:
            yield event


async def _dispatch_events(
    self: AcpClient,
    req_id: int,
    timeout: float,
    *,
    extract_agent_from_result: bool = False,
) -> AsyncGenerator[AcpEvent, None]:
    """Shared event dispatch loop for prompts and commands."""
    self.last_prompt_stats = self.last_prompt_stats.carry_over()
    self._tool_call_inputs.clear()
    self._tool_call_input_redacted.clear()
    self._tool_call_is_shell.clear()
    self._tool_call_unclassified.clear()
    self._skill_reads.clear()
    self._tool_call_mcp_server.clear()
    self._tool_call_tool_name.clear()
    self._tool_call_harness_tool_name.clear()
    self._tool_call_identity_unreadable.clear()
    self._tool_call_params.clear()
    self._tool_call_diff_path.clear()
    # Reset the per-turn observed-tool-call bookkeeping (see __init__).
    self._observed_tool_calls.clear()
    # Clear stale permission options so an aborted/cancelled request from
    # a prior turn cannot leak into this one (memory + correctness).
    self._permission_options.clear()
    getattr(self, "_permission_gate_events", {}).clear()
    self._stale_eligible = False
    self._tool_dispatched = False
    if self._active_tool_calls.evicted_count:
        # The previous turn's overflow, counted and said once (the same
        # sentence the session handle says for its map).
        logger.warning(
            "tool-call open set evicted %d id(s) at its bound of %d last turn; the "
            "tool-stall clock stayed armed for that turn",
            self._active_tool_calls.evicted_count,
            MAX_ACTIVE_TOOL_CALLS,
        )
    self._active_tool_calls.clear()
    got_complete = False
    saw_agent_switch = False
    # A prompt turn takes ``_session/steering`` steers; a command turn does
    # not. Steers accepted into an earlier turn were settled or dropped there.
    self._steers_accepted = []
    self._steering_turn_req = None if extract_agent_from_result else req_id

    async for action, msg in self._prompt_loop(req_id, timeout):
        if action != "update":
            logger.debug(
                "ACP event: method=%s id=%s action=%s",
                _loggable_request_id(msg.method),
                _loggable_request_id(msg.id),
                action,
            )

        # Reset staleness only on events that indicate active work.
        # Passive updates (usage_update, tool_call_update after completion,
        # available_commands) must NOT reset it — they can arrive after the
        # final text chunk when kiro-cli has finished but hasn't sent the
        # complete response yet.
        if action != "update":
            self._stale_eligible = False

        if action == "complete":
            got_complete = True
            result = msg.result or {}
            reason = ""
            if isinstance(result, dict):
                reason = result.get("stopReason", "") or ""
            self._track_prompt_usage(result)
            if extract_agent_from_result and isinstance(result, dict):
                # commands/execute returns output in result fields,
                # not via session/update chunks — yield as text.
                text = format_command_result(result)
                if text:
                    yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)
                if not saw_agent_switch:
                    data = result.get("data", {})
                    if isinstance(data, dict) and data.get("agent"):
                        agent_info = data["agent"]
                        name = agent_info.get("name", "") if isinstance(agent_info, dict) else ""
                        if name:
                            yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=name)
            # Flush any remaining tool results before completing
            for tr_event in await self._read_new_tool_results():
                yield tr_event
            # An automatic claude compaction never sends its own terminal —
            # close it out here, BEFORE EVENT_COMPLETE, so a consumer that
            # reads the terminal to leave its compacting state sees it
            # inside the turn rather than after the turn it belongs to.
            _compaction_settle = self._settle_claude_compaction(reason)
            if _compaction_settle is not None:
                yield _compaction_settle
            # The codex reading of the same hole: a compaction that errored
            # sent no terminal, so the turn ending is where it is closed out.
            _codex_settle = self._settle_codex_compaction(reason)
            if _codex_settle is not None:
                yield _codex_settle
            reason, _refusal = self.last_prompt_stats.terminal_refusal(reason)
            # Turn is over — disarm the stall watchdog.
            self._tool_dispatched = False
            self._last_stop_reason = reason
            _consumed = self._release_accepted_steers(req_id, reason, _refusal)
            self._turn_done.set()
            for _injected in _consumed:
                yield AcpEvent(kind=EVENT_STEER_CONSUMED, text=_injected)
            yield AcpEvent(
                kind=EVENT_COMPLETE,
                stop_reason=reason,
                refusal=_refusal,
                usage=self.last_prompt_stats.to_turn_usage(),
            )
            return
        if action == "error":
            if error_is_refusal_terminal(msg.error, self.last_prompt_stats.refusal):
                # See AcpSessionHandle: a content-filter refusal can
                # terminate as a bare -32603. The reason is already on the
                # stats; surface it as the refusal terminal, never raise.
                #
                # Flush pending tool results first, exactly as the
                # `complete` branch does: a tool that finished just before
                # the filtered inference would otherwise have its result
                # dropped, or emitted into the NEXT turn.
                reason, _refusal = self.last_prompt_stats.terminal_refusal("")
                for tr_event in await self._read_new_tool_results():
                    yield tr_event
                self._tool_dispatched = False
                self._last_stop_reason = reason
                self._turn_done.set()
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=reason,
                    refusal=_refusal,
                    usage=self.last_prompt_stats.to_turn_usage(),
                )
                return
            _raise_acp_error(msg.error, self._advertised_model_ids(), backend=self.backend)
        if action == "permission":
            permission_event = self._build_permission_event(msg)
            if permission_event is None:
                if msg.id is not None:
                    await self._send_error(msg.id, -32600, "invalid request id")
                continue
            # Three refusals before the consumer's gate sees the request: a
            # zero-tool spec, a switched-off tool, and a harness identity that is
            # absent or names an unmounted server. The third matters HERE as much
            # as on the auto-approve site -- a frame nothing classified reaches
            # the consumer as a non-shell tool, so the command-deny tier never
            # sees the command it carries.
            if not (
                await self._deny_zero_tools(permission_event)
                or await self._deny_spec_disabled_tool(permission_event)
                or await self._refuse_identity_drift(permission_event)
            ):
                # The loop is parked here until the approval is answered, and
                # nothing reads stdout meanwhile, so no steer is sent then.
                self._parked_on_permission = True
                try:
                    yield permission_event
                finally:
                    self._parked_on_permission = False
        elif action == "server_request_unknown":
            await self._reject_unknown_server_request(msg)
        elif action == "steer_answer" and self._steer_turn is not None:
            # Answered before the turn's own terminal: kept for the settle.
            self._steer_turn.answers[msg.id] = (msg, False)
        elif action == "update":
            self._track_usage_update(msg)
            self._note_background_launch(msg)
            _steer_echo = self._steer_echo_event(msg)
            if _steer_echo is not None:
                yield _steer_echo
            # codex reports compaction as a marked tool_call pair rather than
            # as text, so it is read off the FRAME here instead of off a
            # chunk below. Yielded and then fallen through: the frame is
            # still a tool call this turn made.
            _codex_compaction = self._codex_compaction_event(msg)
            if _codex_compaction is not None:
                yield _codex_compaction
            chunk, is_thinking = self._extract_text_chunk(msg)
            _notice_chunk = False
            if chunk and not is_thinking:
                # The claude backend reports compaction as plain assistant
                # text. Emit the compaction status event every consumer
                # already handles, then fall through and yield the chunk too:
                # the event is a side effect, not a replacement for the text.
                # The chunk carries ``control_notice`` so a consumer can show
                # it without counting it as the turn's answer — the ACP layer
                # owns the classification, and nothing downstream re-parses.
                _compaction_event = self._claude_compaction_event(chunk)
                if _compaction_event is not None:
                    yield _compaction_event
                    _notice_chunk = True
            if chunk:
                # Before yielding text, check for tool results from JSONL
                for tr_event in await self._read_new_tool_results():
                    yield tr_event
                kind = EVENT_THINKING_CHUNK if is_thinking else EVENT_TEXT_CHUNK
                if not is_thinking:
                    self.last_prompt_stats.text_chunks += 1
                    self._stale_eligible = not self._active_tool_calls.possibly_open
                    self._prompt_or_tool_seen = True
                yield AcpEvent(
                    kind=kind,
                    text=chunk,
                    control_notice=_notice_chunk,
                    message_id=parse_message_id((msg.params or {}).get("update")),
                )
                if not is_thinking and _is_tool_interrupted_marker(chunk):
                    # kiro-cli's built-in security filter cancelled the turn's tools.
                    # It will not send a ``complete`` response — synthesize one so the
                    # caller exits instead of waiting out the prompt timeout.
                    # (_emit_tool_interrupted_sel logs + audits the cancellation.)
                    self._emit_tool_interrupted_sel("_dispatch_events")
                    got_complete = True
                    for tr_event in await self._read_new_tool_results():
                        yield tr_event
                    yield AcpEvent(
                        kind=EVENT_COMPLETE,
                        usage=self.last_prompt_stats.to_turn_usage(),
                    )
                    return
            tool_event = self._extract_tool_event(msg)
            if tool_event:
                self._stale_eligible = False
                self._prompt_or_tool_seen = True
                # Arm the tool-stall watchdog: if no further data arrives
                # within _TOOL_STALL_TIMEOUT, _prompt_loop treats the turn
                # as dead instead of hanging to the full prompt timeout.
                self._active_tool_calls.add(tool_event.tool_call_id or "")
                self._tool_dispatched = True
                # Record every observed tool_call so PostToolUse can recover
                # tool_name from _observed_tool_calls (see
                # _maybe_fire_post_tool_hooks).
                if tool_event.tool_call_id:
                    self._observed_tool_calls[tool_event.tool_call_id] = (
                        tool_event.title or "unknown",
                        tool_event.tool_kind or "",
                    )
                # Check for results from previous tool before yielding new tool_call
                for tr_event in await self._read_new_tool_results():
                    yield tr_event
                # ACP-layer tool audit for clients with no external audit
                # loop (e.g. app worker pools). No-op unless audit_source is set.
                await self._maybe_audit_tool_call(tool_event)
                # Co-located with the SEL audit Pre-side: fire the PreToolUse
                # HOOK ENGINE so app/worker-pool subagents reach hook parity
                # with the main agent / SubagentManager. PostToolUse fires
                # separately on the tool_result branch below (fire_tool_hooks
                # is Pre-only). No-op unless audit_source is set.
                await self._skill_reads.note(tool_event)
                await self._maybe_fire_pre_tool_hooks(tool_event)
                yield tool_event
            # Real-time tool result from `tool_call_update` session updates.
            # kiro-cli emits these the moment a tool completes — fires before
            # the JSONL flush, so the inline pill gets its output the instant
            # the tool finishes instead of waiting for the next tool call or
            # message end.  See `_extract_tool_call_update` for the dual-path
            # (content blocks vs rawOutput) details.
            await self._tripwire_pi_gate(msg)
            await self._tripwire_goose_mode(msg)
            tool_result_event = self._extract_tool_call_update(msg)
            if tool_result_event:
                if tool_result_event.tool_status in TERMINAL_TOOL_STATUSES:
                    self._active_tool_calls.discard(tool_result_event.tool_call_id or "")
                    self._tool_dispatched = self._active_tool_calls.possibly_open
                    self._stale_eligible = not self._active_tool_calls.possibly_open
                # Fire the PostToolUse HOOK ENGINE now that the tool RESULT
                # (and its output) exists — the Pre-vs-Post split is required
                # because fire_tool_hooks above is PreToolUse-only. No-op
                # unless audit_source is set.
                self._skill_reads.credit(tool_result_event)
                await self._maybe_fire_post_tool_hooks(tool_result_event)
                self._tripwire_spec_disabled_tool(tool_result_event)
                yield tool_result_event
            # claude-agent-acp emits a separate `tool_call_update` carrying
            # the refined title / kind / rawInput once `chunk.input` finishes
            # streaming (the initial `tool_call` arrives with empty input and
            # generic title like "Terminal"/"grep").  Yield as a refinement
            # event so the dashboard pill + persisted message can be
            # patched in place — see `EVENT_TOOL_CALL_UPDATE` in chat_runner.
            tool_refine_event = self._extract_tool_call_refinement(msg)
            if tool_refine_event:
                await self._skill_reads.note(tool_refine_event)
                yield tool_refine_event
        elif action == "metadata":
            self._track_metadata(msg)
        elif action == "compaction":
            self._handle_compaction_status(msg)
            params = msg.params or {}
            status = params.get("status", {})
            status_type = status.get("type", "") if isinstance(status, dict) else str(status)
            summary = params.get("summary", "")
            if status_type == "failed":
                # The notice reads AcpEvent.title, and `summary` is empty on
                # failure — carry the notification's own reason so the row
                # stops collapsing to "unknown error".
                summary = compaction_failure_detail(params)
            yield AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=summary)
        elif action == "clear":
            yield AcpEvent(kind=EVENT_CLEAR_STATUS)
        elif action == "subagent_list":
            params = msg.params or {}
            _subs = params.get("subagents")
            logger.debug(
                "ACP subagent_list received: %s entries",
                len(_subs) if isinstance(_subs, list) else "n/a",
            )
            if isinstance(_subs, list):
                # A roster means children exist: a spawned child can mutate
                # state before its first activity frame is observed, so the
                # roster itself closes the registration-throttle window.
                if _subs:
                    self._prompt_or_tool_seen = True
                # No runtime_global marking here: AcpClient owns a dedicated
                # process with a single session, so an ownerless frame from
                # it is this session's own roster, never a co-tenant's.
                yield AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=_subs)
        elif action == "subagent_activity":
            # _kiro.dev/session/update: a sub-agent session's own update,
            # tagged with its sessionId. Carries either:
            # - tool_call_chunk (inner tool starting) with toolCallId/title
            # - agent_message_chunk with text (sub-agent's streamed output)
            params = msg.params or {}
            _ssid = str(params.get("sessionId") or "")
            _upd_raw = params.get("update")
            _upd = _upd_raw if isinstance(_upd_raw, dict) else {}
            _su_kind = str(_upd.get("sessionUpdate") or "")
            _tcid = str(_upd.get("toolCallId") or "")
            # Prefer the nested ``content.text`` shape kiro-cli 2.10.0 emits
            # (via the shared chunk extractor); fall back to the flat
            # top-level ``text`` for older payloads. Fall back on a falsy
            # chunk (None OR empty string) so an empty nested content.text
            # does not shadow a populated flat ``text``.
            _su_chunk, _su_thinking = self._extract_text_chunk(msg)
            _su_text = str(_su_chunk or (_upd.get("text") or ""))
            # A frame naming THIS session is this turn's own stream, not a
            # child's. kiro-cli sends the extension spelling for the parent's
            # own tool-call chunk as well as for a child's update, and the
            # sessionId is what separates them; treating the parent's as a
            # child would put a sub-agent card on the session the user is
            # already looking at.
            if _ssid and _ssid == (self._session_id or ""):
                continue
            if _ssid and _tcid:
                # A child's tool call is this process's side effect for
                # replay purposes — the parent prompt spawned it, so a
                # replay would re-run it. Close the registration-throttle
                # window, exactly as the shared-runtime handle does for its
                # fanned-out child tool calls.
                self._prompt_or_tool_seen = True
                # Sub-agent output is LLM-influenced — redact the title before
                # it reaches the dashboard/persisted message.
                _su_title, _ = redact_exfiltration_urls(str(_upd.get("title") or ""))
                _su_title, _ = redact_credentials(_su_title)
                yield AcpEvent(
                    kind=EVENT_SUBAGENT_ACTIVITY,
                    sub_session_id=_ssid,
                    tool_call_id=_tcid,
                    title=_su_title,
                )
            elif _ssid and _su_text and _su_kind == "agent_message_chunk" and not _su_thinking:
                # Sub-agent's streamed text output — the real content; redact
                # exfil URLs + credentials the sub-agent may have emitted.
                # Skip reasoning/thinking blocks (is_thinking) — those are the
                # sub-agent's internal reasoning, not user-visible output, and
                # the flat pre-port read never surfaced them.
                # Observed child output also closes the registration-throttle
                # window: a child whose tool frame was lost or differently
                # spelled must not read as "did nothing".
                self._prompt_or_tool_seen = True
                _su_text, _ = redact_exfiltration_urls(_su_text)
                _su_text, _ = redact_credentials(_su_text)
                yield AcpEvent(
                    kind=EVENT_SUBAGENT_ACTIVITY,
                    sub_session_id=_ssid,
                    text=_su_text,
                )
        elif action == "agent_switched":
            saw_agent_switch = True
            params = msg.params or {}
            agent_name = params.get("agentName", "")
            self._note_agent_switched(params if isinstance(params, dict) else None)
            yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=agent_name)
        elif action == "mcp_oauth_request":
            params = msg.params or {}
            server_name = str(params.get("serverName") or params.get("name") or "")
            oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
            # Reject unsafe-scheme URLs *before* recording dedupe so a later
            # safe retry for the same server still gets through.
            if not _is_safe_oauth_url(oauth_url):
                if oauth_url:
                    logger.warning(
                        "ACP: refusing unsafe mid-session MCP OAuth URL for %s",
                        server_name or "(unknown)",
                    )
                continue
            # Without a server_name we can't correlate this banner with the
            # later server_initialized/server_init_failure notification (the
            # discard path keys on server_name only).
            if not server_name:
                logger.warning("ACP: dropping mid-session MCP OAuth request with empty serverName")
                continue
            if server_name in self._oauth_emitted_servers:
                logger.debug(
                    "ACP: dropping duplicate mid-session MCP OAuth request for %s",
                    server_name,
                )
                continue
            self._oauth_emitted_servers.add(server_name)
            logger.info("ACP: MCP OAuth request mid-session for %s", server_name)
            yield AcpEvent(
                kind=EVENT_MCP_OAUTH_REQUEST,
                server_name=server_name,
                oauth_url=oauth_url,
            )
        elif action == "mcp_server_initialized":
            params = msg.params or {}
            server_name = str(params.get("serverName") or params.get("name") or "")
            if server_name:
                logger.info("ACP: MCP server initialized: %s", server_name)
                # Allow re-emission of oauth_request if this server's token expires later.
                self._oauth_emitted_servers.discard(server_name)
                yield AcpEvent(
                    kind=EVENT_MCP_SERVER_INITIALIZED,
                    server_name=server_name,
                )
        elif action == "mcp_server_init_failure":
            params = msg.params or {}
            server_name = str(params.get("serverName") or params.get("name") or "")
            err = str(params.get("error") or "")
            if server_name:
                logger.warning("ACP: MCP server init failure: %s — %s", server_name, err)
                # The current banner is in a closed (failed) state — clear
                # the dedupe entry so kiro-cli's next oauth_request retry
                # for this server surfaces a new banner instead of being
                # silently dropped.
                self._oauth_emitted_servers.discard(server_name)
                yield AcpEvent(
                    kind=EVENT_MCP_SERVER_INIT_FAILURE,
                    server_name=server_name,
                    text=err,
                )

    if not got_complete:
        self._last_stop_reason = ""
        self._turn_done.set()
        if self._compaction_failed_turn:
            # Compaction failed and the turn was abandoned by the backend.
            # Terminate explicitly — checked BEFORE the stale-turn branch so
            # a turn that had streamed text does not report a normal
            # end_turn, and before AcpTimeoutError so callers get the real
            # cause. The user-facing notice is already appended by the
            # compaction-status path; this only ends the turn.
            self._compaction_failed_turn = False
            self._last_stop_reason = STOP_REASON_COMPACTION_FAILED
            yield AcpEvent(
                kind=EVENT_COMPLETE,
                stop_reason=STOP_REASON_COMPACTION_FAILED,
                usage=self.last_prompt_stats.to_turn_usage(),
            )
            return
        # If text was streamed, this is a stale turn (kiro-cli finished
        # but never sent `result`).  Yield a synthetic complete so callers
        # finalize normally instead of showing a timeout error.
        if self._stale_eligible:
            logger.info(
                "Synthesizing EVENT_COMPLETE after stale turn (chunks=%d)",
                self.last_prompt_stats.text_chunks,
            )
            yield AcpEvent(
                kind=EVENT_COMPLETE,
                stop_reason=STOP_REASON_END_TURN,
                synthetic_completion=True,
                usage=self.last_prompt_stats.to_turn_usage(),
            )
            return
        raise AcpTimeoutError()


# ── Private Helpers ──


async def _send_prompt(self: AcpClient, message: str, *, allow_image: bool = True) -> int:
    await self._settle_orphan_steers()
    # Shared with AcpSessionHandle.prompt via prompt_blocks so the two paths
    # cannot drift.
    return await self._send_request(
        METHOD_PROMPT,
        {
            "sessionId": self._session_id,
            # Offloaded: see the note in session_handle.prompt -- image
            # reads and base64 encoding must not block the event loop.
            "prompt": await asyncio.to_thread(
                build_prompt_blocks,
                message,
                allow_image=allow_image,
                agent_accepts_image=self._agent_accepts_image,
            ),
        },
    )


async def _read_prompt_response(self: AcpClient, req_id: int, timeout: float) -> str:
    output: list[str] = []
    self.last_prompt_stats = self.last_prompt_stats.carry_over()

    async for action, msg in self._prompt_loop(req_id, timeout):
        if action == "complete":
            reason = ""
            result = msg.result or {}
            if isinstance(result, dict):
                reason = result.get("stopReason", "") or ""
            self._track_prompt_usage(result)
            # See send_message_stream: settle for the context counts, drop
            # the event this API cannot yield.
            self._settle_claude_compaction(reason)
            self._settle_codex_compaction(reason)
            # Fold a metadata refusal onto the terminal, as the streaming
            # paths do, so a caller reading ``last_stop_reason`` sees the
            # refusal and does not retry a deterministic decline.
            reason, _ = self.last_prompt_stats.terminal_refusal(reason)
            self._last_stop_reason = reason
            self._turn_done.set()
            return "".join(output)
        if action == "error":
            if error_is_refusal_terminal(msg.error, self.last_prompt_stats.refusal):
                # A content-filter refusal terminating as a bare -32603 (see
                # _dispatch_events). This API returns the turn's text, so
                # return what streamed (the canned explanation) under the
                # refusal stop reason rather than raising: an AcpError here
                # would discard the reason, feed the retry ladder a
                # deterministic decline, and retire a healthy worker.
                reason, _ = self.last_prompt_stats.terminal_refusal("")
                self._last_stop_reason = reason
                self._turn_done.set()
                return "".join(output)
            _raise_acp_error(msg.error, self._advertised_model_ids(), backend=self.backend)
        if action == "permission":
            await self._handle_permission(msg)
        elif action == "server_request_unknown":
            await self._reject_unknown_server_request(msg)
        elif action == "update":
            self._track_usage_update(msg)
            self._note_background_launch(msg)
            # See send_message_stream: settle the codex compaction for the
            # context counts, drop the event this API cannot return.
            self._codex_compaction_event(msg)
            chunk, is_thinking = self._extract_text_chunk(msg)
            if chunk and not is_thinking:
                # Apply the claude compaction state change, then KEEP the
                # chunk. This path returns one string callers treat as the
                # agent's answer, and a caller that wants the notice out of
                # that string subtracts it with
                # strip_claude_compaction_notices; dropping it here would
                # silently truncate a real answer on a misclassification.
                self._claude_compaction_event(chunk)
                output.append(chunk)
                self.last_prompt_stats.text_chunks += 1
                if _is_tool_interrupted_marker(chunk):
                    self._emit_tool_interrupted_sel("_read_prompt_response")
                    return "".join(output)  # see _dispatch_events for rationale
            self._track_tool_call(msg)
            # Mirror _dispatch_events for the send_message (worker-pool)
            # dispatch path: send_message drives tools through here, not
            # through the stream _dispatch_events, so without this
            # app/worker-pool subagents never reached hook/SEL parity. All
            # gating stays inside the _maybe_* methods (self._audit_source),
            # so main-chat send_message callers (audit_source=None) are no-op.
            tool_event = self._extract_tool_event(msg)
            if tool_event:
                # Record every observed tool_call so PostToolUse can recover
                # tool_name from _observed_tool_calls (see _maybe_fire_post_tool_hooks).
                if tool_event.tool_call_id:
                    self._observed_tool_calls[tool_event.tool_call_id] = (
                        tool_event.title or "unknown",
                        tool_event.tool_kind or "",
                    )
                await self._maybe_audit_tool_call(tool_event)
                await self._skill_reads.note(tool_event)
                await self._maybe_fire_pre_tool_hooks(tool_event)
            await self._tripwire_pi_gate(msg)
            await self._tripwire_goose_mode(msg)
            tool_result_event = self._extract_tool_call_update(msg)
            if tool_result_event:
                self._skill_reads.credit(tool_result_event)
                await self._maybe_fire_post_tool_hooks(tool_result_event)
                self._tripwire_spec_disabled_tool(tool_result_event)
        elif action == "metadata":
            self._track_metadata(msg)
        elif action == "compaction":
            self._handle_compaction_status(msg)

    self._last_stop_reason = ""
    self._turn_done.set()
    raise AcpTimeoutError(partial_output="".join(output))
